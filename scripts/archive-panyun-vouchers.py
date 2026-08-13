#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
archive-panyun-vouchers.py — 磐沄 FY-2024 原始凭证归档（年→月→凭证号）

把 dry-run 匹配好的 563 个源文件 move 到归档库
  c.会计凭证/FY-2024/{MM}-2024/{凭证号}/
复用 upload-to-feishu.py 底层原语（tenant token / ensure_folder_path 建夹 /
feishu_request 自带限流 + 429 退避 + token 刷新）。

授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
数据源：/tmp/panyun-2024-match2.json -> newRes.auto（每条 {token,name,target{period,vno}}）。

用法：
  # 0) 从 match2 抽取 563 条 auto 落到稳定 plan 文件（断点/续跑依据）
  python3 archive-panyun-vouchers.py --init-plan

  # 1) dry-run 某月（不写任何东西，只解析目标夹）
  python3 archive-panyun-vouchers.py --month 06

  # 2) 真执行某月（先 --limit 1 验权限，再整月）
  python3 archive-panyun-vouchers.py --month 06 --apply --limit 1
  python3 archive-panyun-vouchers.py --month 06 --apply

  # 3) 复拉验证某月（列出归档库该月所有凭证号夹 + 文件数，对账期望值）
  python3 archive-panyun-vouchers.py --month 06 --verify
"""
import argparse, json, os, re, sys, tempfile, time, hashlib
from pathlib import Path
import importlib.util
import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
spec = importlib.util.spec_from_file_location("U", HERE / "upload-to-feishu.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

ENTITY = "panyun"
ENTITY_LABEL = "磐沄"
ARCHIVE_ROOT = "CR2ifqHVUldrbxdg7OKckhKBnvh"   # C3_北京磐沄 / c.会计凭证/FY-2024
MATCH_FILE = Path("/tmp/panyun-2024-match2.json")
PLAN_FILE = REPO / "04-upload-logs" / "archive-plan-panyun-fy2024.json"
LOG_DIR = REPO / "04-upload-logs"
FY = "FY-2024"


def month_label(period):
    # "202406" -> "06-2024" (MM-YYYY，4位年；2026-08-13 用户确认后续月份统一此格式)
    return f"{period[4:6]}-{period[0:4]}"


_VNO_SPLIT = re.compile(r"[、，,/]")


def split_vno(vno):
    """'转-33、转-14' -> ['转-33', '转-14']；单凭证号原样返回。"""
    parts = [p.strip() for p in _VNO_SPLIT.split(vno) if p.strip()]
    return parts or [vno]


def move_one(token, file_token, dest_folder):
    """move 文件到目标夹，返回 (ok, err)。"""
    url = f"{U.FEISHU_BASE}/drive/v1/files/{file_token}/move"
    for _ in range(3):
        r = U.feishu_request("POST", url, headers=U.get_headers(token),
                             json={"type": "file", "folder_token": dest_folder})
        try:
            body = r.json()
            if body.get("code") == 0:
                return True, ""
            err = f'{body.get("code")}:{body.get("msg", "")}'
        except Exception as e:
            err = f"exc:{e}"
        time.sleep(3)
    return False, err


def download_bytes(token, file_token):
    """下载飞书 drive 文件二进制（用于一附多证 copy）。失败返回 None。"""
    url = f"{U.FEISHU_BASE}/drive/v1/medias/{file_token}/download"
    r = U.feishu_request("GET", url, headers=U.get_headers(token), allow_redirects=True)
    ct = r.headers.get("Content-Type", "")
    if "application/json" in ct:
        try:
            b = r.json()
            if b.get("code") != 0:
                return None
        except Exception:
            pass
    if not r.content:
        return None
    return r.content


def upload_copy(token, data, file_name, dest_folder):
    """把 bytes 以 file_name 上传到目标夹（一附多证的第二+夹）。返回新 file_token 或 None。"""
    with tempfile.NamedTemporaryFile(suffix=os.path.basename(file_name), delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        # 文件名保留中文原名
        target = os.path.join(os.path.dirname(tmp), os.path.basename(file_name))
        os.rename(tmp, target)
        tmp = target
        res = U.upload_file(token, dest_folder, Path(tmp))
        return res.get("file_token") if res.get("status") == "success" else None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def ckpt_path(month):
    key = f"{ARCHIVE_ROOT}|{ENTITY}|{FY}|{month or 'all'}"
    h = hashlib.md5(key.encode()).hexdigest()[:10]
    return LOG_DIR / f"archive-ckpt-{ENTITY}-{h}.jsonl"


def load_plan(month=None, token=None):
    if not PLAN_FILE.exists():
        sys.exit(f"plan 不存在，先跑 --init-plan：{PLAN_FILE}")
    plan = json.load(open(PLAN_FILE))
    if month:
        plan = [x for x in plan if x["target"]["period"].endswith(month)]
    if token:
        plan = [x for x in plan if x["token"] == token]
    return plan


def already_done(cp):
    done = set()
    if cp.exists():
        for line in cp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("status") == "success" and r.get("token"):
                    done.add(r["token"])
            except Exception:
                pass
    return done


def init_plan():
    data = json.load(open(MATCH_FILE))
    auto = data["newRes"]["auto"]
    LOG_DIR.mkdir(exist_ok=True)
    PLAN_FILE.write_text(json.dumps(auto, ensure_ascii=False, indent=0), encoding="utf-8")
    # 概览
    from collections import Counter
    pc = Counter(month_label(x["target"]["period"]) for x in auto)
    print(f"plan 写入 {PLAN_FILE}：{len(auto)} 条")
    for m in sorted(pc):
        print(f"  {m}: {pc[m]}")


def do_move(args):
    files = load_plan(args.month, getattr(args, "token", None))
    if args.limit:
        files = files[:args.limit]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {ENTITY_LABEL} {FY} month={args.month or 'ALL'} files={len(files)} root={ARCHIVE_ROOT}")

    cp = ckpt_path(args.month)
    LOG_DIR.mkdir(exist_ok=True)
    # dry-run：纯计算目标路径，不碰飞书（不取 token、不建夹、不 move）
    if not args.apply:
        from collections import Counter
        byv = Counter()
        multi = 0
        for x in files:
            ml = month_label(x["target"]["period"])
            vs = split_vno(x["target"]["vno"])
            if len(vs) > 1:
                multi += 1
            for v in vs:
                byv[(ml, v)] += 1
        print(f"dry-run 目标凭证号夹：{len(byv)} 个（不建夹、不 move）；其中一附多证文件 {multi} 个（将 copy 到第二凭证号夹）")
        for (ml, vno), n in sorted(byv.items()):
            print(f"  {ml}/{vno:<10} <- {n} 文件")
        print("确认无误后用 --apply 落地。")
        return

    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token，检查 FEISHU_APP_ID/FEISHU_APP_SECRET")
    done = set() if args.fresh else already_done(cp)
    logf = open(cp, "a", encoding="utf-8")
    stats = {"success": 0, "skip_ckpt": 0, "fail": 0, "copies": 0}
    for i, x in enumerate(files):
        ft, name, t = x["token"], x["name"], x["target"]
        ml = month_label(t["period"])
        vnos = split_vno(t["vno"])          # 一附多证 -> 多个凭证号
        if ft in done:
            stats["skip_ckpt"] += 1
            continue
        ts = time.strftime("%F %T")
        # 先确保所有目标凭证号夹（单凭证号只有一个）
        dests = []
        ensure_ok = True
        for v in vnos:
            d = U.ensure_folder_path(token, ARCHIVE_ROOT, Path(ml) / v)
            if not d:
                ensure_ok = False
                break
            dests.append((v, d))
        if not ensure_ok:
            stats["fail"] += 1
            logf.write(json.dumps({"status": "fail", "stage": "ensure", "token": ft,
                                   "name": name, "tgt": f"{ml}/{t['vno']}", "ts": ts}, ensure_ascii=False) + "\n")
            logf.flush()
            continue
        # 主凭证号：move 原件（离开源夹、进归档）
        primary_v, primary_d = dests[0]
        ok, err = move_one(token, ft, primary_d)
        copies_info = []
        if ok and len(dests) > 1:
            # 一附多证：下载原件 bytes，上传 copy 到其余凭证号夹
            print(f"    [一附多证] {name}: dests={len(dests)} -> copy 到 {[v for v,_ in dests[1:]]}", flush=True)
            data = download_bytes(token, ft)
            print(f"    [一附多证] download bytes={len(data) if data else None}", flush=True)
            if data:
                for v, d in dests[1:]:
                    nt = upload_copy(token, data, name, d)
                    copies_info.append({"vno": v, "folder": d, "ok": bool(nt),
                                        "new_token": nt})
                    if nt:
                        stats["copies"] += 1
            else:
                err = "primary-moved-but-copy-download-failed"
        status = "success" if ok else "fail"
        stats[status] += 1
        logf.write(json.dumps({"status": status, "token": ft, "name": name,
                               "tgt": f"{ml}/{t['vno']}", "primary": primary_v,
                               "primary_folder": primary_d, "copies": copies_info,
                               "err": "" if ok else err, "ts": ts}, ensure_ascii=False) + "\n")
        logf.flush()
        if (i + 1) % 25 == 0 or i == len(files) - 1:
            print(f"  [{i + 1}/{len(files)}] {stats}")
    logf.close()
    print(f"DONE {mode} {stats}  ckpt={cp}")


def do_verify(args):
    """复拉归档库某月，列出所有凭证号夹及文件数，与期望(plan)对账。"""
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token")
    month_folder = month_label(f"2024{args.month}")  # "06" -> 202406 -> 06-2024
    # 定位月份夹 token
    mfolder_tok = U.ensure_folder_path(token, ARCHIVE_ROOT, Path(month_folder))
    if not mfolder_tok:
        sys.exit(f"月份夹不存在且无法创建：{month_folder}")
    vfolders = [it for it in U.list_folder_live(token, mfolder_tok) if it.get("type") == "folder"]
    plan = load_plan(args.month)
    from collections import Counter
    # 一附多证：拆开，每个凭证号夹各计 1 件
    expect_by_vno = Counter()
    for x in plan:
        for v in split_vno(x["target"]["vno"]):
            expect_by_vno[v] += 1
    actual_total, matched, mismatch = 0, 0, []
    print(f"VERIFY {ENTITY_LABEL} {FY}/{month_folder}：{len(vfolders)} 个凭证号夹（期望 {len(expect_by_vno)}）")
    for vf in sorted(vfolders, key=lambda x: x["name"]):
        files = [it for it in U.list_folder_live(token, vf["token"]) if it.get("type") == "file"]
        actual_total += len(files)
        exp = expect_by_vno.get(vf["name"], 0)
        flag = "OK" if len(files) == exp else ("+超" if len(files) > exp else "-缺")
        if flag != "OK":
            mismatch.append((vf["name"], len(files), exp))
        else:
            matched += 1
        print(f"  {vf['name']:<10} 实际 {len(files):>3}  期望 {exp:>3}  {flag}")
    expect_total = sum(expect_by_vno.values())   # 含一附多证的 copy 件
    print(f"\n凭证号夹对账：{matched} 一致 / {len(mismatch)} 不一致；实际文件总数 {actual_total}，期望 {expect_total}（原件 {len(plan)} + 一附多证 copy {expect_total - len(plan)}）")
    if mismatch:
        print("不一致清单：", mismatch)


def probe_download(file_token):
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token")
    H = U.get_headers(token)
    url = f"{U.FEISHU_BASE}/drive/v1/medias/{file_token}/download"
    r = U.feishu_request("GET", url, headers=H, allow_redirects=True)
    print(f"GET {url}")
    print(f"  status={r.status_code} ct={r.headers.get('Content-Type')} len={len(r.content)}")
    print(f"  body head: {r.content[:300]!r}")


def feishu_copy_file(token, file_token, file_name, dest_folder):
    """服务端 copy 文件到目标夹（POST /drive/v1/files/{token}/copy），返回 (ok, new_token/err)。
    比 download+upload 稳：不过本地、不吃带宽、原子。一附多证补 copy 用。"""
    url = f"{U.FEISHU_BASE}/drive/v1/files/{file_token}/copy"
    for _ in range(3):
        r = U.feishu_request("POST", url, headers=U.get_headers(token),
                             json={"type": "file", "folder_token": dest_folder, "name": file_name})
        try:
            body = r.json()
            if body.get("code") == 0:
                ft = (body.get("data") or {}).get("file_token")
                return True, ft
            err = f'{body.get("code")}:{body.get("msg", "")}'
        except Exception as e:
            err = f"exc:{e}"
        time.sleep(3)
    return False, err


def do_copy_missing(args):
    """补一附多证缺失的 copy：对某月每个一附多证文件，确保每个目标凭证号夹都有一份。
    primary(dests[0]) 已有原件；其余夹按名查，缺则用服务端 copy 补。"""
    plan = load_plan(args.month)
    multi = [x for x in plan if len(split_vno(x["target"]["vno"])) > 1]
    if not multi:
        print(f"[COPY-MISSING] month={args.month} 无一附多证文件，无需补 copy")
        return
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[COPY-MISSING {mode}] {ENTITY_LABEL} {FY} month={args.month} 一附多证文件 {len(multi)} 个")
    stats = {"fixed": 0, "already": 0, "fail": 0}
    for x in multi:
        ft, name, t = x["token"], x["name"], x["target"]
        ml = month_label(t["period"])
        vnos = split_vno(t["vno"])
        dests = {}
        ensure_ok = True
        for v in vnos:
            d = U.ensure_folder_path(token, ARCHIVE_ROOT, Path(ml) / v)
            if not d:
                ensure_ok = False; break
            dests[v] = d
        if not ensure_ok:
            print(f"  ✗ {name}: 建夹失败"); stats["fail"] += 1; continue
        for v in vnos[1:]:                      # primary 已有原件，只补其余夹
            d = dests[v]
            existing = [it["name"] for it in U.list_folder_live(token, d) if it.get("type") == "file"]
            if name in existing:
                print(f"  ✓ {ml}/{v} 已有 {name}（跳过）"); stats["already"] += 1; continue
            if args.apply:
                ok2, res = feishu_copy_file(token, ft, name, d)
                if ok2:
                    print(f"  + {ml}/{v} <- copy {name}（new {res}）"); stats["fixed"] += 1
                else:
                    print(f"  ✗ {ml}/{v} copy 失败 {name}: {res}"); stats["fail"] += 1
            else:
                print(f"  [DRY] {ml}/{v} 缺 {name}（将 copy）"); stats["fixed"] += 1
    print(f"DONE copy-missing {mode} {stats}")


def main():
    ap = argparse.ArgumentParser(description="磐沄 FY-2024 凭证归档")
    ap.add_argument("--init-plan", action="store_true", help="从 match2 抽 auto 写 plan 文件")
    ap.add_argument("--month", help='两位月份如 06（对齐 plan 的 period 后两位）')
    ap.add_argument("--token", help="只处理指定 file_token（单文件外科测试用）")
    ap.add_argument("--apply", action="store_true", help="真执行 move（默认 dry-run）")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--fresh", action="store_true", help="忽略断点从头")
    ap.add_argument("--verify", action="store_true", help="复拉验证某月")
    ap.add_argument("--copy-missing", action="store_true", help="补一附多证缺失的 copy（配 --month）")
    ap.add_argument("--probe-download", help="诊断：尝试下载某 file_token 并打印响应")
    args = ap.parse_args()
    if args.probe_download:
        probe_download(args.probe_download)
    elif args.init_plan:
        init_plan()
    elif args.copy_missing:
        if not args.month:
            sys.exit("--copy-missing 需配 --month")
        do_copy_missing(args)
    elif args.verify:
        if not args.month:
            sys.exit("--verify 需配 --month")
        do_verify(args)
    else:
        do_move(args)


if __name__ == "__main__":
    main()
