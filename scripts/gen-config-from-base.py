#!/usr/bin/env python3
"""从飞书多维表格 feishu-folder-structure 重生成 00-config 下的配置 JSON。

数据源（source of truth）: 飞书 Base SLQeb85ieaOu1KsnKlPcEVGindd
产物:
  - 00-config/feishu-folder-tree.json    完整目录树（含 folder token）
  - 00-config/wps-folder-mapping.json    WPS源路径 → 飞书路径 映射表

用法:
  python scripts/gen-config-from-base.py --inspect      # 打印表清单+字段+样本，用于对齐字段
  python scripts/gen-config-from-base.py                # 重生成两个 JSON

凭证: 环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET（优先 tenant_access_token）
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "00-config"
TREE_PATH = CONFIG_DIR / "feishu-folder-tree.json"
MAPPING_PATH = CONFIG_DIR / "wps-folder-mapping.json"

FEISHU_BASE = "https://open.feishu.cn/open-apis"
APP_TOKEN = "SLQeb85ieaOu1KsnKlPcEVGindd"


def get_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        sys.exit("错误：请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    r = requests.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    d = r.json()
    if d.get("code") != 0:
        sys.exit(f"错误：获取 tenant_access_token 失败: {d.get('msg', r.text)}")
    return d["tenant_access_token"]


def api_get(url: str, token: str, params: dict = None) -> dict:
    for attempt in range(3):
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         params=params or {}, timeout=30)
        d = r.json()
        if d.get("code") == 0:
            return d
        if d.get("code") in (99991661, 99991663):  # token 过期/限流
            time.sleep(2)
            continue
        sys.exit(f"错误：API {url} code={d.get('code')} msg={d.get('msg')}")
    sys.exit(f"错误：API {url} 重试耗尽")


def list_tables(token: str) -> list:
    url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables"
    items, page = [], None
    while True:
        params = {"page_size": 100}
        if page:
            params["page_token"] = page
        d = api_get(url, token, params)
        items.extend(d["data"].get("items", []))
        if not d["data"].get("has_more"):
            break
        page = d["data"].get("page_token")
    return items


def list_fields(token: str, table_id: str) -> list:
    url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields"
    d = api_get(url, token, {"page_size": 100})
    return d["data"].get("items", [])


def fetch_records(token: str, table_id: str) -> list:
    url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records"
    items, page = [], None
    while True:
        params = {"page_size": 500}
        if page:
            params["page_token"] = page
        d = api_get(url, token, params)
        items.extend(d["data"].get("items", []))
        if not d["data"].get("has_more"):
            break
        page = d["data"].get("page_token")
    return items


def cell_text(v):
    """把飞书单元格值规整为纯文本。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):
                parts.append(x.get("text") or x.get("name") or "")
            else:
                parts.append(str(x))
        return "".join(parts).strip()
    if isinstance(v, dict):
        return (v.get("text") or v.get("name") or "").strip()
    return str(v).strip()


def inspect(token: str):
    tables = list_tables(token)
    print(f"Base {APP_TOKEN} 共 {len(tables)} 张表：")
    for t in tables:
        print(f"  - {t['name']}  (table_id={t['table_id']})")
    for t in tables:
        fields = list_fields(token, t["table_id"])
        recs = fetch_records(token, t["table_id"])
        names = [f["field_name"] for f in fields]
        print(f"\n=== 表「{t['name']}」 字段 {len(fields)} 个 / 记录 {len(recs)} 条 ===")
        print("  字段：" + ", ".join(names))
        # 每字段非空计数
        print("  非空计数：")
        for n in names:
            cnt = sum(1 for r in recs if cell_text(r.get("fields", {}).get(n)))
            print(f"    {n}: {cnt}")
        # 列8 / 字段 1 非空样本（判断是否含 token/notes）
        for probe in ["列8", "字段 1"]:
            hits = [r for r in recs if cell_text(r.get("fields", {}).get(probe))]
            print(f"  「{probe}」非空样本（前5/{len(hits)}）：")
            for r in hits[:5]:
                row = {k: cell_text(v) for k, v in r.get("fields", {}).items() if cell_text(v)}
                print("    " + json.dumps(row, ensure_ascii=False))
        # 含 WPS 源路径 的样本
        wps_field = "WPS 源路径(空置则无源路径）"
        wps_rows = [r for r in recs if cell_text(r.get("fields", {}).get(wps_field))]
        print(f"  含 WPS 源路径 行数：{len(wps_rows)}；样本前3：")
        for r in wps_rows[:3]:
            row = {k: cell_text(v) for k, v in r.get("fields", {}).items() if cell_text(v)}
            print("    " + json.dumps(row, ensure_ascii=False))
        # 六级目录非空样本
        deep = [r for r in recs if cell_text(r.get("fields", {}).get("六级目录"))]
        print(f"  六级目录非空行数：{len(deep)}；样本前3：")
        for r in deep[:3]:
            row = {k: cell_text(v) for k, v in r.get("fields", {}).items() if cell_text(v)}
            print("    " + json.dumps(row, ensure_ascii=False))


import re

TABLE_ID = "tbl5JpQvqdMMtrxF"
AREA_F = "飞书区域"
LEVELS_F = ["一级目录", "二级目录", "三级目录", "四级目录", "五级目录", "六级目录"]
WPS_F = "WPS 源路径(空置则无源路径）"
NOTE_F = "字段 1"


def default_view_id(token: str) -> str:
    url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/views"
    d = api_get(url, token, {"page_size": 100})
    views = d["data"].get("items", [])
    grid = next((v for v in views if v.get("view_type") == "grid"), views[0] if views else None)
    if not grid:
        sys.exit("错误：表内无视图，无法确定行顺序")
    return grid["view_id"]


def fetch_records_ordered(token: str, view_id: str) -> list:
    url = f"{FEISHU_BASE}/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records"
    items, page = [], None
    while True:
        params = {"page_size": 500, "view_id": view_id}
        if page:
            params["page_token"] = page
        d = api_get(url, token, params)
        items.extend(d["data"].get("items", []))
        if not d["data"].get("has_more"):
            break
        page = d["data"].get("page_token")
    return items


def entity_code(level1: str) -> str:
    m = re.match(r"^([CHS]\d+)_", level1)
    return m.group(1) if m else level1


def reconstruct(records: list):
    """按视图顺序 forward-fill 还原每行完整路径。返回 node 列表。
    node = {area, levels:[...], depth, wps_source, note}
    """
    cur_area = ""
    cur_levels = ["", "", "", "", "", ""]
    nodes = []
    for r in records:
        f = r.get("fields", {})
        area = cell_text(f.get(AREA_F))
        row_levels = [cell_text(f.get(L)) for L in LEVELS_F]
        wps = cell_text(f.get(WPS_F)).replace("\\", "/")
        note = cell_text(f.get(NOTE_F))

        if area:
            cur_area = area
            # 新区域块边界：清空上一区域残留的继承层级，避免区域锚点行
            # （只填区域、层级为空）错误继承上一区域的深层目录形成幽灵节点
            cur_levels = ["", "", "", "", "", ""]
        # 找本行设置的最深层级
        set_idx = [i for i, v in enumerate(row_levels) if v]
        if set_idx:
            deepest = max(set_idx)
            for i in idx_range(set_idx):
                cur_levels[i] = row_levels[i]
            # 清空比 deepest 更深的继承值（新分支）
            for i in range(deepest + 1, 6):
                cur_levels[i] = ""
            depth = deepest + 1  # 1-based 层级数
        else:
            # 无任何层级：可能是纯 wps 挂在当前节点，或空行
            depth = len([v for v in cur_levels if v])

        if not cur_area and not any(cur_levels) and not wps:
            continue  # 完全空行，跳过

        # 无论哪条分支都剔除中间空层级段（如 "银行电子回单//Aspire"）：
        # 某些行只设最深层而跳过中间列，idx_range 会往空隙写入空串，位置切片会保留成幽灵空层。
        levels_now = [cur_levels[i] for i in range(depth) if cur_levels[i]] if set_idx else [v for v in cur_levels if v]
        nodes.append({
            "area": cur_area,
            "levels": levels_now,
            "depth": len(levels_now),
            "wps_source": wps,
            "note": note,
        })
    return nodes


def idx_range(set_idx):
    return range(min(set_idx), max(set_idx) + 1)


def load_old_tokens():
    """从旧 feishu-folder-tree.json 建 路径→token 映射，用于保留 token。"""
    if not TREE_PATH.exists():
        return {}
    old = json.loads(TREE_PATH.read_text(encoding="utf-8"))
    m = {}
    for e in old:
        key = tuple([e.get("area", "")] + [e.get(f"level{i}", "") for i in range(1, 5) if e.get(f"level{i}", "")])
        if e.get("token"):
            m[key] = e["token"]
    return m


def build_tree(nodes, old_tokens):
    seen = set()
    out = []
    for n in nodes:
        path = tuple([n["area"]] + n["levels"])
        if path in seen:
            continue
        seen.add(path)
        entry = {"area": n["area"]}
        for i in range(6):
            entry[f"level{i+1}"] = n["levels"][i] if i < len(n["levels"]) else ""
        entry["token"] = old_tokens.get(path, "")
        entry["type"] = "folder"
        entry["wps_source"] = n["wps_source"]
        entry["notes"] = n["note"]
        out.append(entry)
    return out


def build_mapping(nodes):
    out = []
    for n in nodes:
        if not n["wps_source"]:
            continue
        feishu_path = [n["area"]] + n["levels"]
        out.append({
            "wps_source": n["wps_source"],
            "feishu_path": feishu_path,
            "entity_code": entity_code(n["levels"][0]) if n["levels"] else n["area"],
            "note": n["note"] or None,
        })
    return out


def generate(token: str, apply: bool):
    view_id = default_view_id(token)
    records = fetch_records_ordered(token, view_id)
    nodes = reconstruct(records)
    old_tokens = load_old_tokens()

    tree = build_tree(nodes, old_tokens)
    mapping = build_mapping(nodes)

    kept = sum(1 for e in tree if e["token"])
    lost = sum(1 for e in tree if not e["token"])
    print(f"记录 {len(records)} 条 → 节点 {len(nodes)} 个")
    print(f"目录树 {len(tree)} 条（保留 token {kept} / 无 token 新增 {lost}）")
    print(f"WPS 映射 {len(mapping)} 条")

    tree_out = json.dumps(tree, ensure_ascii=False, indent=2)
    mapping_obj = {
        "_comment": "WPS云盘→飞书云盘 文件夹映射表 (自动生成自飞书多维表格 feishu-folder-structure)",
        "_generated": os.environ.get("GEN_DATE", ""),
        "_source_bitable": APP_TOKEN,
        "_total_mappings": len(mapping),
        "mappings": mapping,
    }
    mapping_out = json.dumps(mapping_obj, ensure_ascii=False, indent=2)

    suffix = "" if apply else ".preview"
    tp = TREE_PATH.with_suffix(f"{suffix}.json") if suffix else TREE_PATH
    mp = MAPPING_PATH.with_suffix(f"{suffix}.json") if suffix else MAPPING_PATH
    tp.write_text(tree_out, encoding="utf-8")
    mp.write_text(mapping_out, encoding="utf-8")
    print(f"已写出：{tp.name} / {mp.name}")

    # 预览：前后各若干条重建路径
    print("\n重建路径样例（前8 / 后4）：")
    paths = ["/".join([n["area"]] + n["levels"]) for n in nodes]
    for p in paths[:8] + ["  ..."] + paths[-4:]:
        print("  " + p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="打印表结构与样本")
    ap.add_argument("--apply", action="store_true", help="直接覆盖正式配置（默认写 .preview）")
    ap.add_argument("--dump-raw", metavar="OUT", help="按视图顺序导出原始记录到 JSON（只读诊断用）")
    args = ap.parse_args()

    token = get_token()
    if args.inspect:
        inspect(token)
        return
    if args.dump_raw:
        view_id = default_view_id(token)
        records = fetch_records_ordered(token, view_id)
        rows = []
        for idx, r in enumerate(records):
            f = r.get("fields", {})
            rows.append({
                "i": idx,
                "area": cell_text(f.get(AREA_F)),
                "levels": [cell_text(f.get(L)) for L in LEVELS_F],
                "wps": cell_text(f.get(WPS_F)),
                "note": cell_text(f.get(NOTE_F)),
            })
        Path(args.dump_raw).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已导出 {len(rows)} 条原始记录 → {args.dump_raw}")
        return
    generate(token, apply=args.apply)


if __name__ == "__main__":
    main()
