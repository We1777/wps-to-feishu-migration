#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen-archive-report.py — 磐沄 FY-2024 归档报告（实测飞书 + 交叉比对）

每个批次归档完后跑一次，输出含 5 字段的归档报告：
  凭证号 / 凭证归档的附件名 / 附件数 / 是否匹配凭证上的附件数 / 归档附件的原文件夹

数据来源：
  - 实拉归档库该月所有「凭证号夹 + 内含文件」（飞书 live，= 归档真相）
  - 凭证列表「附件」注明张数（/tmp/panyun-2024-voucher.json，idx6）
  - match2 的 path 字段（各附件归档前所在原类别夹）（/tmp/panyun-2024-match2.json）

模式：
  收集：python3 gen-archive-report.py --month 06 [--fy 2024]
       → /tmp/panyun-{mm}-{yyyy}-arch-report.json + 打印汇总
  上传：python3 gen-archive-report.py --upload <xlsx路径>
       → 上传到飞书「待定事项」文件夹，stdout 出链接

授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
"""
import argparse, json, re, sys
from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("U", HERE / "upload-to-feishu.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

ARCHIVE_ROOT = "CR2ifqHVUldrbxdg7OKckhKBnvh"   # C3_北京磐沄 / c.会计凭证/FY-2024
ENTITY_LABEL = "磐沄"
REPORT_FOLDER = "ZcMgfJEzKlXTqvdMmnycipJwnpe"  # 飞书「待定事项」
DOMAIN = "lcnzfxq3rlhh.feishu.cn"

VOUCHER_FILE = Path("/tmp/panyun-2024-voucher.json")
MATCH_FILE = Path("/tmp/panyun-2024-match2.json")
SOURCE_FILE = Path("/tmp/panyun-2024-source2.json")


def find_month_folder(token, month, year):
    """在 ARCHIVE_ROOT 下找月份夹（兼容 MM-YYYY 与旧 MM-YY）。"""
    candidates = {f"{month}-{year}", f"{month}-{year[2:]}"}
    for c in U.list_folder_live(token, ARCHIVE_ROOT):
        if c.get("name") in candidates and c.get("type") == "folder":
            return c
    return None


def load_stated_attach(period):
    """{(period, vno): 凭证注明附件张数}。凭证列表按(期间+凭证字号)聚合，同证多行注明数应一致。"""
    out = {}
    if not VOUCHER_FILE.exists():
        print(f"[warn] 凭证列表不存在：{VOUCHER_FILE}", file=sys.stderr)
        return out
    for r in json.load(open(VOUCHER_FILE, encoding="utf-8")):
        if len(r) < 7:
            continue
        date, vno, attach = r[0], r[1], r[6]
        if not date or not vno:
            continue
        ds = str(date)[:7]
        if not re.match(r"\d{4}-\d{2}", ds):
            continue
        p = ds.replace("-", "")
        n = None
        if attach not in (None, ""):
            try:
                n = int(str(attach).strip())
            except Exception:
                n = None
        if n is not None:
            out[(p, vno)] = n
        elif (p, vno) not in out:
            out[(p, vno)] = None
    return out


def load_orig_folder():
    """{filename: 原类别夹名}。match2 优先（权威），source2 兜底。"""
    out = {}
    if MATCH_FILE.exists():
        for x in json.load(open(MATCH_FILE, encoding="utf-8"))["newRes"]["auto"]:
            parts = x.get("path", "").split("/")
            out[x["name"]] = parts[1] if len(parts) > 1 else ""
    if SOURCE_FILE.exists():
        for x in json.load(open(SOURCE_FILE, encoding="utf-8")):
            nm = x.get("name")
            if not nm or nm in out:
                continue
            parts = x.get("path", "").split("/")
            out[nm] = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    return out


def is_voucher_pdf(name):
    """记账凭证 PDF（凭证本身，非原始凭证附件）——不计入附件数。"""
    return "记账凭证" in name


def collect(month, year):
    period = f"{year}{month}"
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token，检查 FEISHU_APP_ID/FEISHU_APP_SECRET")
    mf = find_month_folder(token, month, year)
    if not mf:
        sys.exit(f"归档库 {ARCHIVE_ROOT} 下未找到月份夹 {month}-{year}（或 {month}-{year[2:]}）")
    print(f"月份夹：{mf['name']} (token {mf['token'][:8]}...)", file=sys.stderr)

    stated = load_stated_attach(period)
    orig = load_orig_folder()

    vfolders = sorted(
        (c for c in U.list_folder_live(token, mf["token"]) if c.get("type") == "folder"),
        key=lambda c: c.get("name", ""),
    )

    vouchers = []
    for vf in vfolders:
        vno = vf["name"]
        files = sorted(
            (c for c in U.list_folder_live(token, vf["token"]) if c.get("type") == "file"),
            key=lambda c: c.get("name", ""),
        )
        atts = [f for f in files if not is_voucher_pdf(f["name"])]
        vpdfs = [f["name"] for f in files if is_voucher_pdf(f["name"])]
        archived_n = len(atts)
        stated_n = stated.get((period, vno))
        if stated_n is None:
            match = "凭证列表无该号"
        elif archived_n == stated_n:
            match = "匹配"
        elif archived_n > stated_n:
            match = f"超额{archived_n - stated_n}"
        else:
            match = f"缺件{stated_n - archived_n}"
        vouchers.append({
            "vno": vno,
            "archived_count": archived_n,
            "stated_count": stated_n,
            "match": match,
            "attachments": [{"name": f["name"], "orig_folder": orig.get(f["name"], "未知")}
                            for f in atts],
            "voucher_pdfs": vpdfs,
        })

    fully = [v for v in vouchers if v["match"] == "匹配"]
    over = [v for v in vouchers if v["match"].startswith("超额")]
    under = [v for v in vouchers if v["match"].startswith("缺件")]
    nomap = [v for v in vouchers if v["match"] == "凭证列表无该号"]

    report = {
        "entity": ENTITY_LABEL, "fy": f"FY-{year}", "month": month,
        "month_folder": mf["name"], "period": period,
        "stats": {
            "voucher_folders": len(vouchers),
            "archived_attachments": sum(v["archived_count"] for v in vouchers),
            "matched": len(fully), "over": len(over),
            "under": len(under), "not_in_list": len(nomap),
        },
        "vouchers": vouchers,
    }
    out = Path(f"/tmp/panyun-{month}-{year}-arch-report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["stats"], ensure_ascii=False, indent=2))
    for tag, lst in (("超额", over), ("缺件", under), ("凭证列表无", nomap)):
        if lst:
            print(f"[{tag}] " + ", ".join(
                f'{v["vno"]}(档{v["archived_count"]}/注{v["stated_count"]})' for v in lst),
                file=sys.stderr)
    print(f"→ {out}", file=sys.stderr)


def rename_month(month, year):
    """把月份夹改名为 MM-YYYY（实测飞书是否支持 folder rename）。"""
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token")
    mf = find_month_folder(token, month, year)
    if not mf:
        sys.exit(f"未找到月份夹 {month}-{year[2:]} 或 {month}-{year}")
    newname = f"{month}-{year}"
    if mf["name"] == newname:
        print(f"已是 {newname}，无需改名"); return
    ft = mf["token"]
    # 试 1: PATCH /drive/v1/files/{token}?type=folder
    url = f"{U.FEISHU_BASE}/drive/v1/files/{ft}"
    r = U.feishu_request("PATCH", url, headers=U.get_headers(token),
                         params={"type": "folder"}, json={"name": newname})
    print(f"[试 PATCH] {r.status_code} {r.text[:300]}", file=sys.stderr)
    if r.status_code == 200:
        try:
            if r.json().get("code") == 0:
                print(f"OK 改名 → {newname}"); return
        except Exception:
            pass
    # 试 2: POST /drive/v1/files/{token}/rename
    url2 = f"{U.FEISHU_BASE}/drive/v1/files/{ft}/rename"
    r2 = U.feishu_request("POST", url2, headers=U.get_headers(token),
                          json={"name": newname, "type": "folder"})
    print(f"[试 POST rename] {r2.status_code} {r2.text[:300]}", file=sys.stderr)
    if r2.status_code == 200:
        try:
            if r2.json().get("code") == 0:
                print(f"OK 改名 → {newname}"); return
        except Exception:
            pass
    sys.exit("飞书 drive API 不支持文件夹改名（PATCH/rename 均失败）。需在飞书网页端手动改名，或新建 06-2024 后搬入再删旧夹。")


def upload(xlsx_path):
    fp = Path(xlsx_path)
    if not fp.exists():
        sys.exit(f"文件不存在：{fp}")
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token")
    r = U.upload_file(token, REPORT_FOLDER, fp)
    if r.get("status") == "success":
        ft = r["file_token"]
        print(f"https://{DOMAIN}/file/{ft}")
    else:
        sys.exit(f"上传失败：{r}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="磐沄归档报告生成器")
    ap.add_argument("--month", help="月份，如 06")
    ap.add_argument("--fy", default="2024", help="财年，默认 2024")
    ap.add_argument("--upload", help="上传指定 xlsx 到飞书待定事项")
    ap.add_argument("--rename-month", action="store_true", help="把月份夹改名为 MM-YYYY（实测飞书支持否）")
    a = ap.parse_args()
    if a.upload:
        upload(a.upload)
    elif a.rename_month and a.month:
        rename_month(a.month, a.fy)
    elif a.month:
        collect(a.month, a.fy)
    else:
        ap.error("需要 --month 或 --upload")
