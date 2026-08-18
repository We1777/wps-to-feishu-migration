#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
read-spreadsheet.py — 读取飞书原生电子表格（sheets）到 JSON/控制台

用法：
  python3 read-spreadsheet.py <spreadsheet_token> [--sheet ID或标题] [--out out.json]

  不带 --sheet 时列出全部 sheet（id/标题/行列数）。
  带 --sheet 时读整表 values 输出（首行为表头）。

认证：tenant_access_token（复用 upload-to-feishu.py）。
授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
"""
import argparse, json, sys
from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("U", HERE / "upload-to-feishu.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)


def list_sheets(token: str, sstoken: str):
    url = f"{U.FEISHU_BASE}/sheets/v3/spreadsheets/{sstoken}/sheets/query?page_size=100"
    r = U.feishu_request("GET", url, headers=U.get_headers(token))
    data = r.json()
    if data.get("code") != 0:
        return None, f"code={data.get('code')} msg={data.get('msg')}"
    items = (data.get("data") or {}).get("sheets") or []
    out = [{"id": s.get("sheet_id"), "title": s.get("title"),
            "rows": s.get("grid_properties", {}).get("rowCount"),
            "cols": s.get("grid_properties", {}).get("columnCount")} for s in items]
    return out, None


def read_values(token: str, sstoken: str, sheet_id: str):
    # 整表读取；range 走 URL 编码，列上限用 AZ（飞书大 range 常见坑：超界返回非 JSON）
    url = f"{U.FEISHU_BASE}/sheets/v2/spreadsheets/{sstoken}/values/{sheet_id}!A1:AZ20000"
    r = U.feishu_request("GET", url, headers=U.get_headers(token))
    try:
        data = r.json()
    except Exception:
        return None, f"http={r.status_code} non-json body[:200]={r.text[:200]}"
    if data.get("code") != 0:
        return None, f"code={data.get('code')} msg={data.get('msg')}"
    vv = ((data.get("data") or {}).get("valueRange") or {}).get("values") or []
    return vv, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sstoken")
    ap.add_argument("--sheet")
    ap.add_argument("--out")
    args = ap.parse_args()

    token = U._refresh_token()
    sheets, err = list_sheets(token, args.sstoken)
    if err:
        print(json.dumps({"error": f"list_sheets: {err}"}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(sheets, ensure_ascii=False, indent=1))

    if not args.sheet:
        return
    target = next((s for s in sheets if s["id"] == args.sheet or s["title"] == args.sheet), None)
    if not target:
        print(json.dumps({"error": f"sheet not found: {args.sheet}"}, ensure_ascii=False))
        sys.exit(1)
    values, err = read_values(token, args.sstoken, target["id"])
    if err:
        print(json.dumps({"error": f"read_values: {err}"}, ensure_ascii=False))
        sys.exit(1)
    payload = {"sheet": target, "n_rows": len(values), "values": values}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"[saved] {args.out} rows={len(values)}")
    else:
        for row in values[:50]:
            print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
