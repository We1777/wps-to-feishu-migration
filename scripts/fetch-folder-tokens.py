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


def build_key(entry: dict) -> tuple:
    levels = tuple(entry.get(f"level{i}", "") for i in range(1, 7)
                   if entry.get(f"level{i}", ""))
    return (entry["area"],) + levels


def main():
    apply = "--apply" in sys.argv
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    roots = cfg["root_folders"]  # {name: {folder_token, ...}}
    token = get_token()

    path_map = {}
    for area, meta in roots.items():
        ft = meta["folder_token"]
        # 根文件夹本身也可作为 area 顶层
        path_map[(area,)] = ft
        print(f"遍历根文件夹 {area} ({ft}) ...")
        walk(token, area, ft, tuple(), path_map)
    print(f"云盘共抓到 {len(path_map)} 个文件夹路径")

    tree = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    hit, still_missing, already = 0, [], 0
    for e in tree:
        if e.get("type") != "folder":
            continue
        if e.get("token"):
            already += 1
            continue
        k = build_key(e)
        if k in path_map:
            e["token"] = path_map[k]
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
