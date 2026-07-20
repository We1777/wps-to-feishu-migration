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
        print(f"\n=== 表「{t['name']}」 字段 {len(fields)} 个 / 记录 {len(recs)} 条 ===")
        print("  字段：" + ", ".join(f["field_name"] for f in fields))
        for r in recs[:3]:
            row = {k: cell_text(v) for k, v in r.get("fields", {}).items()}
            print("  样本：" + json.dumps(row, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="打印表结构与样本")
    args = ap.parse_args()

    token = get_token()
    if args.inspect:
        inspect(token)
        return

    print("生成模式尚未启用——请先 --inspect 对齐字段后再实现生成逻辑。")


if __name__ == "__main__":
    main()
