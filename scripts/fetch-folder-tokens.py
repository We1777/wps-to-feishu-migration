#!/usr/bin/env python3
"""遍历飞书云盘（从 target-space.json 的 4 个根文件夹）抓取所有已建文件夹的 token，
按路径 (area, level1..level6) 建立 path→token 映射，回填 feishu-folder-tree.preview.json。

- 只读遍历（drive/v1/files GET），封装调用，不裸 curl。
- 输出：
    00-config/feishu-folder-tree.preview.json  （回填 token，--apply 时写回，否则只报告）
    stdout: 命中 / 仍缺失 的路径清单

凭证: 环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET（tenant_access_token）
"""

import os
import sys
import json
import time
import requests
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "target-space.json"
PREVIEW_PATH = PROJECT_ROOT / "00-config" / "feishu-folder-tree.preview.json"
# 断点缓存：把云盘遍历结果(path→token)落盘，下次 --resume 直接读，跳过全量遍历。
CACHE_PATH = PROJECT_ROOT / "04-upload-logs" / "token-scan-cache.json"
FEISHU_BASE = "https://open.feishu.cn/open-apis"
MAX_RETRIES = 3
RETRY_DELAY = 2


def get_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        sys.exit("错误：请设置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    r = requests.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=30,
    )
    d = r.json()
    if d.get("code") != 0:
        sys.exit(f"错误：获取 tenant_access_token 失败: {d.get('msg', r.text)}")
    return d["tenant_access_token"]


def list_children(token: str, folder_token: str) -> list:
    """列出某文件夹下的直接子项（含分页）。返回 [{name, token, type}]。"""
    out = []
    page = None
    url = f"{FEISHU_BASE}/drive/v1/files"
    while True:
        params = {"folder_token": folder_token, "page_size": 200}
        if page:
            params["page_token"] = page
        for attempt in range(MAX_RETRIES):
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                             params=params, timeout=30)
            d = r.json()
            if d.get("code") == 0:
                break
            if d.get("code") in (99991661, 99991663):  # 过期/限流
                time.sleep(RETRY_DELAY)
                continue
            print(f"  警告：列举 {folder_token} 失败: {d.get('msg', r.text)}")
            return out
        data = d.get("data", {})
        for f in data.get("files", []):
            out.append({"name": f.get("name"), "token": f.get("token"),
                        "type": f.get("type")})
        if data.get("has_more") and data.get("next_page_token"):
            page = data["next_page_token"]
        else:
            break
    return out


def walk(token: str, area: str, folder_token: str, prefix: tuple, path_map: dict):
    """递归遍历，把每个子文件夹的 (area, *levels) → token 写进 path_map。"""
    children = list_children(token, folder_token)
    for c in children:
        if c["type"] != "folder":
            continue
        levels = prefix + (c["name"],)
        key = (area,) + levels
        path_map[key] = c["token"]
        walk(token, area, c["token"], levels, path_map)


# 已知的「Base 路径 ← 云盘实际路径」别名（drive 里被改名/挪走，Base 未同步）。
# 每条: tree 路径前缀 tuple -> 云盘实际路径前缀 tuple。匹配失败时按前缀重写再试。
PREFIX_ALIASES = [
    # C4 顶层在云盘实为「…（已清算）」
    (("归档库", "C4_北京磐曜科技有限公司"),
     ("归档库", "C4_北京磐曜科技有限公司（已清算）")),
    # 银行电子回单 已从 工作台/收件箱 挪到 归档库/0_CHN汇总
    (("工作台", "📥 收件箱", "银行电子回单"),
     ("归档库", "0_CHN汇总", "银行电子回单")),
]


def resolve_key(k: tuple, path_map: dict):
    if k in path_map:
        return path_map[k]
    for src, dst in PREFIX_ALIASES:
        if k[:len(src)] == src:
            alt = dst + k[len(src):]
            if alt in path_map:
                return path_map[alt]
    return None


def save_cache(path_map: dict):
    """把 path_map 落盘为断点缓存。key(tuple) 序列化为 list。"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(path_map),
        "paths": [{"key": list(k), "token": v} for k, v in path_map.items()],
    }
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"已保存断点缓存 {CACHE_PATH.name}（{len(path_map)} 条，{payload['saved_at']}）")


def load_cache() -> dict:
    """读断点缓存，还原成 path_map(tuple→token)。缓存不存在则报错退出。"""
    if not CACHE_PATH.exists():
        sys.exit(f"错误：无断点缓存 {CACHE_PATH.name}，请先做一次全量遍历（不带 --resume）")
    payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    path_map = {tuple(p["key"]): p["token"] for p in payload.get("paths", [])}
    print(f"读入断点缓存 {CACHE_PATH.name}（{len(path_map)} 条，存于 {payload.get('saved_at')}）")
    return path_map


def build_key(entry: dict) -> tuple:
    levels = tuple(entry.get(f"level{i}", "") for i in range(1, 7)
                   if entry.get(f"level{i}", ""))
    return (entry["area"],) + levels


def parse_args(argv: list):
    """解析参数。返回 (apply, resume, rescan_areas, locate_tokens)。
    --rescan <area> 可重复；剩余非 -- 参数视为要定位的 token。"""
    apply = resume = False
    rescan, locate = [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--apply":
            apply = True
        elif a == "--resume":
            resume = True
        elif a == "--rescan":
            i += 1
            if i >= len(argv):
                sys.exit("错误：--rescan 后需跟一个 area 名（如 归档库）")
            rescan.append(argv[i])
        elif a.startswith("--"):
            sys.exit(f"错误：未知参数 {a}")
        else:
            locate.append(a)
        i += 1
    return apply, resume, rescan, locate


def main():
    apply, resume, rescan_areas, locate = parse_args(sys.argv[1:])
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    roots = cfg["root_folders"]  # {name: {folder_token, ...}}
    for a in rescan_areas:
        if a not in roots:
            sys.exit(f"错误：--rescan 的 area『{a}』不在根文件夹中，可选：{list(roots)}")

    # 决定要遍历哪些根：--resume 时只遍历 --rescan 指定的分支，其余从缓存读；
    # 否则全量遍历 4 个根。
    if resume:
        path_map = load_cache()
        walk_areas = rescan_areas  # 只重爬问题分支，可为空(纯读缓存)
    else:
        path_map = {}
        walk_areas = list(roots)  # 全量

    token = get_token() if walk_areas else None
    for area in walk_areas:
        ft = roots[area]["folder_token"]
        path_map[(area,)] = ft  # 根文件夹本身也可作为 area 顶层
        print(f"遍历根文件夹 {area} ({ft}) ...")
        walk(token, area, ft, tuple(), path_map)
    print(f"当前 path_map 共 {len(path_map)} 个文件夹路径"
          + ("（含缓存）" if resume else ""))

    # 遍历过云盘就刷新断点缓存（纯读缓存续跑则不覆盖）
    if walk_areas or not resume:
        save_cache(path_map)

    if locate:
        tok2path = {v: k for k, v in path_map.items()}
        for tk in locate:
            k = tok2path.get(tk)
            print(f"\n[定位] {tk} => " + (" / ".join(k) if k else "未在4个根下找到"))
        return

    tree = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    hit, still_missing, already = 0, [], 0
    for e in tree:
        if e.get("type") != "folder":
            continue
        if e.get("token"):
            already += 1
            continue
        k = build_key(e)
        tk = resolve_key(k, path_map)
        if tk:
            e["token"] = tk
            hit += 1
        else:
            still_missing.append(" / ".join(k))

    print(f"\n已有 token: {already}  |  本次回填命中: {hit}  |  仍缺失: {len(still_missing)}")
    if still_missing:
        print("\n=== 仍匹配不上的路径（云盘里没找到同名文件夹）===")
        for p in still_missing:
            print("  ✗", p)

    if apply:
        PREVIEW_PATH.write_text(json.dumps(tree, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"\n已写回 {PREVIEW_PATH.name}")
    else:
        print("\n(未加 --apply，仅报告，未写回)")


if __name__ == "__main__":
    main()
