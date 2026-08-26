#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen-archive-matcher.py — 凭证归档匹配器（转正版，FINCHN-710）

背景：磐沄 FY-2024 归档曾发生 6 处同模式错放——入账表「一附多证」行（凭证字号与记账期间
均为多值，如「转-14、转-38」↔「202408、202406」）拆目标时多个凭证号共用了第一个记账期间，
copy 挂错月。根因在 /tmp 一次性匹配脚本（未入库、无校验、取首项兜底）。本脚本把匹配逻辑
转正入库，两道硬闸：
  闸1 按位配对：凭证字号[i] ↔ 记账期间[i]；两列表长度不一致 → 整行进存疑，禁止取首项兜底
  闸2 归前校验：每个目标（月,凭证号）回查凭证列表——凭证须存在于该月；且金额或摘要与附件
     吻合（金额=任一明细行或借/贷合计与附件金额差≤0.02；摘要=关键词在附件名与凭证摘要/科目
     双侧命中）。不吻合 → 存疑，不落盘
别名同源判定（2026-08-26 用户裁定「合同号不一致禁判同源」，FINCHN-710 f 项）：缺件行与池内
未匹配文件做同源提示时，任一侧含合同号 → 两侧合同号集合必须完全一致，否则一律不同源——
固定金额撞形也拦（起因：09/收-1 华夏收汇单被旧别名判定误指 INF2024004 Q3 发票，同为
USD 261,000）。aliasHints 仅进 report 供人工审阅，绝不自动落盘。

输出：
  --out    归档计划 JSON：[{token,name,target{period,vno}}]，archive-panyun-vouchers.py
           直接消费的格式。同月多凭证号合并为一条（vno 顿号连接，执行脚本原生 copy）；
           跨月一附多证拆成每月一条（同 token 多条，plan 里每月的目标都为真）
  --report 匹配报告 JSON：crossMonthCopies（跨月补挂清单）/ suspect（存疑）/ missingAttachments
           （入账表有、影像池无）/ unmatchedPool（影像池有、入账表无）/ aliasHints（缺件行的
           同源别名提示，review-only 绝不自动落盘）/ 逐目标校验证据 / 统计

用法（全显式路径，不猜默认）：
  python3 scripts/gen-archive-matcher.py \
    --entry /tmp/panyun-2024-entry.json \
    --invoice-entry /tmp/panyun-2024-invoice.json \
    --pool /tmp/panyun-2024-source.json --pool /tmp/panyun-2024-source2.json \
    --vouchers /tmp/panyun-2024-voucher.json \
    --out 04-upload-logs/archive-plan-panyun-fy2024.json \
    --report 04-upload-logs/archive-match-report-panyun-fy2024.json

跨月一附多证补挂（执行脚本行为不动：move 原生只搬第一个月，跨月第二份走本脚本服务端 copy）：
  python3 scripts/gen-archive-matcher.py --exec-cross-copies \
    --report 04-upload-logs/archive-match-report-panyun-fy2024.json \
    --archive-root <归档根folder token>            # dry-run
  # 确认后加 --apply 真执行；每笔 copy 后复拉目标夹验证，已存在则跳过（幂等）

操作顺序建议：按月份从小到大 --apply（原件落最小月）→ --exec-cross-copies 补大月副本 →
--verify 对账。顺序颠倒不出错（两夹最终都有文件，只是原件落位月不同）。
"""
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ── 解析原语 ────────────────────────────────────────────────────────────────
_LIST_SPLIT = re.compile(r"[、，,/]+")
_VNO_PERIOD_PREFIX = re.compile(r"^20\d{4}\s+")


def split_list(s):
    """'转-14、转-38' -> ['转-14','转-38']；空/None -> []"""
    if s is None:
        return []
    return [p.strip() for p in _LIST_SPLIT.split(str(s)) if p.strip()]


def split_vnos(s):
    """凭证字号列表：逐项剥掉发票入账表带的期间前缀（'202407 转-31' -> '转-31'）再拆多值。"""
    return [(_VNO_PERIOD_PREFIX.sub("", v) or v).strip()
            for v in split_list(s) if (_VNO_PERIOD_PREFIX.sub("", v) or v).strip()]


_PERIOD_RE = [
    re.compile(r"^(20\d{2})[年\-/\.]?(\d{1,2})[期月]?"),
    re.compile(r"^(\d{1,2})[\-/](20\d{2})"),
]


def parse_period(s):
    """'202412'/'2024年12期'/'12-2024' -> '202412'；解析不了返回 None"""
    s = str(s or "").strip()
    if not s:
        return None
    m = _PERIOD_RE[0].match(s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    else:
        m = _PERIOD_RE[1].match(s)
        if not m:
            return None
        mo, y = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12):
        return None
    return f"{y}{mo:02d}"


def month_label(period):
    # 与 archive-panyun-vouchers.py 一致："202406" -> "06-2024"（MM-YYYY）
    return f"{period[4:6]}-{period[0:4]}"


_FN_DATE_RE = re.compile(r"\d{1,2}-\d{1,2}-20\d{2}|20\d{2}\d{2}\d{2}")
# 年月类 token：YYYYMM / MM-YYYY / YYYY年MM月 / YYYY年 / Qn-YYYY 的数字段——一律不是金额
_FN_PERIOD_RE = re.compile(r"(?<!\d)(?:20\d{2}(?:0[1-9]|1[0-2])|(?:0?[1-9]|1[0-2])-20\d{2}"
                           r"|20\d{2}年(?:0?[1-9]|1[0-2])月?|20\d{2}年)(?!\d)")
_FN_AMT_RE = re.compile(r"(\d[\d,]*\.\d{2})(?!\d)")
# 千分位分组整数（不限位数）：1,000,000 / 126,900 —— 七位数以上金额全靠它
_FN_GROUP_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(?![\d,])")
# 裸整数兜底：相邻不得是字母数字（防 INF2024007 合同号段被抠走当金额）
_FN_INT_RE = re.compile(r"(?<![\w.])(\d{4,9})(?![\w.])")


def amount_from_filename(name):
    """从附件名提取金额：先抠日期与年月 token（防「202412办公室租赁款」的 202412
    被当金额取走、真金额反而漏掉），再找两位小数，再找千分位分组整数（不限位数，
    2026-08-26 修 1,000,000 提不出），最后 4-9 位裸整数兜底（相邻须为非字母数字，
    防 INF2024007 合同号段误入）。"""
    s = _FN_DATE_RE.sub(" ", str(name or ""))
    s = _FN_PERIOD_RE.sub(" ", s)
    stem = Path(s).stem if "." in s else s
    m = _FN_AMT_RE.search(stem)
    if m:
        return float(m.group(1).replace(",", ""))
    m = _FN_GROUP_RE.search(stem)
    if m:
        return float(m.group(1).replace(",", ""))
    m = _FN_INT_RE.search(stem)
    if m:
        return float(m.group(1))
    return None


# 摘要/附件双向关键词（校准自磐沄 FY-2024 实际摘要；命中任一即视为摘要吻合）
KEYWORDS = [
    "餐费", "福利", "利息", "结息", "水单", "社保", "公积金", "工资", "薪酬", "租金",
    "房租", "物业", "水电", "话费", "电话费", "通讯", "网络", "宽带", "办公", "机票",
    "交通", "差旅", "增值税", "附加", "完税", "印花", "税", "押金", "服务费", "咨询费",
    "手续费", "佣金", "会员", "采购", "软件", "电脑", "家具", "装修", "工程", "施工",
    "保洁", "清洁", "快递", "物流", "运费", "保险", "借款", "拆借", "报销", "罚款",
    "收汇", "付汇", "承兑", "明细表", "计算表", "申报表", "缴款",
    "水费", "电费", "餐饮", "住宿", "出差", "收入", "咨询", "代理", "饮用水", "零食",
]


# ── 输入装载 ────────────────────────────────────────────────────────────────
def load_entry_rows(entry_path):
    """原始凭证入账表（read-spreadsheet.py 导出的 {e1..e4}）-> [{name,amount,vno,period,src}]"""
    E = json.loads(Path(entry_path).read_text(encoding="utf-8"))
    out = []
    sheets = E if isinstance(E, list) else None
    if sheets is None:
        sheets = []
        for k in sorted(E.keys()):
            sheets.append((k, E[k]))
    else:
        sheets = [("flat", sheets)]
    for k, rows in sheets:
        for r in rows[3:]:
            if not r or not r[0] or not str(r[0]).strip():
                continue
            out.append({"name": str(r[0]).strip(), "amount": r[3],
                        "vno": r[6], "period": r[7], "src": f"entry:{k}"})
    return out


def load_invoice_rows(inv_path):
    """进项/销项发票入账表 -> [{name,amount,vno,period,src}]（列：影像9/金额6/凭证字号12/期间13）"""
    rows = json.loads(Path(inv_path).read_text(encoding="utf-8"))
    out = []
    for r in rows[3:]:
        if not r or not (r[9] and str(r[9]).strip()):
            continue
        out.append({"name": str(r[9]).strip(), "amount": r[6],
                    "vno": r[12], "period": r[13], "src": "invoice"})
    return out


def load_pool(pool_paths):
    """影像池清单（可多份）-> 按名索引 [{name,token,path}]（重名保留多 token）"""
    by_name, all_files = {}, []
    for p in pool_paths:
        for f in json.loads(Path(p).read_text(encoding="utf-8")):
            if not f or not f.get("name"):
                continue
            all_files.append(f)
            by_name.setdefault(f["name"], []).append(f)
    return all_files, by_name


def load_vouchers(v_path):
    """凭证列表（记账凭证PDF提取）-> {(yyyymm, vno): {summaries,lines,sum_dr,sum_cr,blob}}"""
    rows = json.loads(Path(v_path).read_text(encoding="utf-8"))
    idx, months = {}, set()
    for r in rows[3:]:
        if not r or not r[1]:
            continue
        m = re.match(r"^(20\d{2})-(\d{2})", str(r[0] or ""))
        if not m:
            continue
        ym = m.group(1) + m.group(2)
        vno = str(r[1]).strip()
        months.add(ym)
        e = idx.setdefault((ym, vno), {"summaries": [], "lines": [], "sum_dr": 0.0, "sum_cr": 0.0})
        summ = str(r[2] or "")
        acct = str(r[3] or "")
        dr = float(r[4]) if r[4] not in (None, "", "-") else 0.0
        cr = float(r[5]) if r[5] not in (None, "", "-") else 0.0
        e["summaries"].append(summ)
        e["lines"].append((dr, cr))
        e["sum_dr"] += dr
        e["sum_cr"] += cr
        e["blob"] = e.get("blob", "") + " " + summ + " " + acct
    rng = (min(months), max(months)) if months else (None, None)
    return idx, rng


# ── 闸1：按位配对 ───────────────────────────────────────────────────────────
def pair_row_targets(row):
    """一行入账记录 -> (month_groups, suspect_reason)
    month_groups: [('202408', ['转-14']), ('202406', ['转-38'])] 按首次出现顺序
    返回 suspect_reason 非 None 时整行进存疑（禁止取首项兜底）。"""
    vnos = split_vnos(row.get("vno"))
    periods = [parse_period(p) for p in split_list(row.get("period"))]
    if not vnos:
        return None, None  # 无凭证字号：不属归档目标，走 entryNoVoucher 报告，不算存疑
    if any(p is None for p in periods) or not periods:
        return None, "period-unparseable"
    if len(vnos) != len(periods):
        return None, f"pair-length-mismatch(vno={len(vnos)},period={len(periods)})"
    groups = []  # [(period, [vno...])] 保序
    for vno, per in zip(vnos, periods):
        for g in groups:
            if g[0] == per:
                if vno not in g[1]:
                    g[1].append(vno)
                break
        else:
            groups.append((per, [vno]))
    return groups, None


# ── 闸2：归前校验 ───────────────────────────────────────────────────────────
def validate_target(period, vno, attach_name, attach_amount, vidx, vrng):
    """(月,凭证号) 目标校验 -> (ok, reason, evidence)"""
    key = (period, vno)
    v = vidx.get(key)
    if v is None:
        lo, hi = vrng
        if lo and not (lo <= period <= hi):
            return False, "month-out-of-voucher-list-range", {"range": [lo, hi]}
        return False, "voucher-not-in-list", {}
    A = attach_amount if attach_amount is not None else amount_from_filename(attach_name)
    amount_pass, amount_hit = False, None
    if A is not None:
        for dr, cr in v["lines"]:
            if abs(abs(dr) - A) <= 0.02 or abs(abs(cr) - A) <= 0.02:
                amount_pass, amount_hit = True, f"line:{abs(dr) or abs(cr):.2f}"
                break
        if not amount_pass and abs(abs(v["sum_dr"]) - A) <= 0.02:
            amount_pass, amount_hit = True, f"sum_dr:{abs(v['sum_dr']):.2f}"
        if not amount_pass and abs(abs(v["sum_cr"]) - A) <= 0.02:
            amount_pass, amount_hit = True, f"sum_cr:{abs(v['sum_cr']):.2f}"
    kws = [k for k in KEYWORDS if k in attach_name and k in v["blob"]]
    summary_pass = bool(kws)
    ev = {"voucher_summaries": v["summaries"][:2],
          "attach_amount": A, "amount_pass": amount_pass, "amount_hit": amount_hit,
          "summary_pass": summary_pass, "kw_hits": kws[:3]}
    if not amount_pass and not summary_pass:
        return False, "amount-summary-mismatch", ev
    return True, None, ev


# ── 别名同源判定（收紧版：合同号不一致禁判同源，review-only）────────────────
_CONTRACT_RE = re.compile(r"\b(?:INF|CONT|CTR|HT)[-_]?\d{3,}\b", re.IGNORECASE)


def contract_tokens(name):
    """附件名中的合同号集合：INF2024007 / CONT-001 / CTR-2024 等（大写归一、去连字符）。"""
    return frozenset(m.group(0).upper().replace("-", "").replace("_", "")
                     for m in _CONTRACT_RE.finditer(str(name or "")))


def same_source(name_a, name_b):
    """两附件名是否判「同源别名」→ (ok, evidence)。
    硬条件（用户裁定 2026-08-26）：任一侧含合同号 → 两侧合同号集合必须完全一致，
    否则一律不同源——固定金额撞形也拦（INF2024007 收汇单 ≠ INF2024004 Q3 发票，
    即便都是 USD 261,000）。单侧有合同号同样不放行（拿不出合同证据不判同源）。
    合同号一致后还需金额一致或双侧关键词命中；双侧均无合同号时须同日+同额
    （08-25 全量扫实证口径），纯关键词命中不判同源（弱证据对会笛卡尔式泛滥）。"""
    ca, cb = contract_tokens(name_a), contract_tokens(name_b)
    aa, ab = amount_from_filename(name_a), amount_from_filename(name_b)
    amt_eq = aa is not None and ab is not None and abs(aa - ab) <= 0.02
    if ca or cb:
        if ca != cb:
            return False, f"contract-mismatch({sorted(ca)}≠{sorted(cb)})"
        if amt_eq:
            return True, f"contract+amount:{aa:g}"
        common = [k for k in KEYWORDS if k in str(name_a) and k in str(name_b)]
        if common:
            return True, f"contract+kw:{common[0]}"
        return False, "no-evidence"
    da = _FN_DATE_RE.findall(str(name_a or ""))
    db = _FN_DATE_RE.findall(str(name_b or ""))
    if amt_eq and da and db and sorted(da) == sorted(db):
        return True, f"same-day+amount:{aa:g}"
    return False, "no-evidence"


# ── 主匹配流程 ──────────────────────────────────────────────────────────────
def run_match(args):
    entry_rows = load_entry_rows(args.entry)
    if args.invoice_entry:
        entry_rows += load_invoice_rows(args.invoice_entry)
    pool_files, pool_by_name = load_pool(args.pool)
    vidx, vrng = load_vouchers(args.vouchers)

    plan_by_key = {}          # (token,period,vnojoin) -> plan entry（去重）
    cross_copies = []         # [{token,name,primary:{period,vno},copies:[{period,vno}]}]
    suspects, missing, no_voucher_rows = [], [], []
    validated_evidence = []

    # 同名行目标合并：原始凭证入账表常载完整跨月配对、发票入账表只载本张发票的
    # 那一腿（子集）——有行能覆盖全量并集时用该行；没有行覆盖 → 冲突进存疑
    by_name_rows = {}
    for row in entry_rows:
        by_name_rows.setdefault(row["name"], []).append(row)

    def choose_groups(rows):
        """同名多行 → (chosen_groups, bad_reason, union_covered)"""
        parsed = [pair_row_targets(r) for r in rows]
        goods = [(r, g) for r, (g, bad) in zip(rows, parsed) if bad is None and g is not None]
        if any(bad for (g, bad) in parsed if bad):
            return None, next(bad for (g, bad) in parsed if bad), False
        if not goods:
            return None, None, False  # 全部无凭证字号
        union = []
        for _, g in goods:
            for per, vnos in g:
                for ug in union:
                    if ug[0] == per:
                        for v in vnos:
                            if v not in ug[1]:
                                ug[1].append(v)
                        break
                else:
                    union.append((per, list(vnos)))
        usig = {(p, v) for p, vs in union for v in vs}
        for r, g in goods:
            if {(p, v) for p, vs in g for v in vs} == usig:
                return g, None, True
        return None, "target-conflict(同名行目标不一致且无行覆盖并集)", False

    matched_names = set()
    for name in sorted(by_name_rows):
        rows = by_name_rows[name]
        files = pool_by_name.get(name, [])
        first = rows[0]
        groups, bad, covered = choose_groups(rows)
        if bad:
            suspects.append({"name": name, "src": first["src"], "reason": bad,
                             "vno": first.get("vno"), "period": first.get("period")})
            continue
        if groups is None:
            no_voucher_rows.append({"name": name, "src": first["src"]})
            continue
        if not files:
            missing.append({"name": name, "src": first["src"],
                            "vno": first.get("vno"), "period": first.get("period")})
            continue
        matched_names.add(name)
        # 逐目标闸2校验（任一目标不过 → 整行存疑，不落盘）
        row_amount = first.get("amount")
        row_amount = float(row_amount) if isinstance(row_amount, (int, float)) else None
        for period, vnos in groups:
            for vno in vnos:
                ok, reason, ev = validate_target(period, vno, name, row_amount, vidx, vrng)
                validated_evidence.append({"name": name, "period": period, "vno": vno,
                                           "ok": ok, "reason": reason, **ev})
                if not ok:
                    suspects.append({"name": name, "src": first["src"], "reason": reason,
                                     "target": {"period": period, "vno": vno},
                                     "vno": first.get("vno"), "period": first.get("period")})
                    break
            else:
                continue
            break
        else:
            # 全部目标通过 → 落盘：每月一条 plan；首月为主目标，其余月进 crossMonthCopies
            for f in files:
                for gi, (period, vnos) in enumerate(groups):
                    vjoin = "、".join(vnos)
                    plan_by_key[(f["token"], period, vjoin)] = {
                        "token": f["token"], "name": name,
                        "target": {"period": period, "vno": vjoin}}
                if len(groups) > 1:
                    cross_copies.append({
                        "token": f["token"], "name": name,
                        "primary": {"period": groups[0][0], "vno": "、".join(groups[0][1])},
                        "copies": [{"period": p, "vno": "、".join(v)} for p, v in groups[1:]]})

    plan = sorted(plan_by_key.values(),
                  key=lambda x: (x["target"]["period"], x["target"]["vno"], x["name"]))
    matched_pool = {f["name"] for f in pool_files if f["name"] in matched_names}
    unmatched_pool = [f for f in pool_files if f["name"] not in matched_names]

    # 别名同源提示（review-only）：缺件行 × 池内未匹配文件，收紧判据=合同号必须一致。
    # 只提示不落盘——是否采纳由人工审阅 report.aliasHints 决定；每行封顶 5 条防弱证据泛滥。
    alias_hints = []
    for m in missing:
        n_this = 0
        for f in unmatched_pool:
            ok, ev = same_source(m["name"], f["name"])
            if ok:
                alias_hints.append({"name": m["name"], "pool_name": f["name"],
                                    "pool_token": f.get("token"), "evidence": ev})
                n_this += 1
                if n_this >= 5:
                    break

    report = {
        "stats": {
            "entry_rows": len(entry_rows), "plan_entries": len(plan),
            "unique_names": len(by_name_rows), "matched_names": len(matched_names),
            "suspects": len(suspects), "missing_attachments": len(missing),
            "unmatched_pool": len(unmatched_pool), "no_voucher_rows": len(no_voucher_rows),
            "cross_month_files": len(cross_copies), "alias_hints": len(alias_hints),
        },
        "by_month": {},
        "suspect": suspects, "missingAttachments": missing,
        "unmatchedPool": [f["name"] for f in unmatched_pool][:500],
        "noVoucherRows": no_voucher_rows,
        "crossMonthCopies": cross_copies,
        "aliasHints": alias_hints,
        "targetValidation": validated_evidence,
        "voucher_list_range": list(vrng),
        "inputs": {"entry": args.entry, "invoice_entry": args.invoice_entry,
                   "pool": args.pool, "vouchers": args.vouchers},
    }
    for x in plan:
        ml = month_label(x["target"]["period"])
        report["by_month"][ml] = report["by_month"].get(ml, 0) + 1

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    s = report["stats"]
    print(f"匹配完成：入账行 {s['entry_rows']} | 唯一附件名 {s['unique_names']} | "
          f"影像池 {len(pool_files)} 文件")
    print(f"plan {s['plan_entries']} 条 → {args.out}")
    print(f"  存疑 {s['suspects']} | 池中缺件 {s['missing_attachments']} | "
          f"池未匹配 {s['unmatched_pool']} | 无凭证号行 {s['no_voucher_rows']}")
    if alias_hints:
        print(f"  别名同源提示 {len(alias_hints)} 条（report.aliasHints，人工审阅，不自动落盘）")
    print(f"  跨月一附多证 {s['cross_month_files']} 文件（补挂清单在 report.crossMonthCopies）")
    for ml in sorted(report["by_month"]):
        print(f"    {ml}: {report['by_month'][ml]} 条")
    if suspects:
        by_reason = {}
        for x in suspects:
            by_reason[x["reason"].split("(")[0]] = by_reason.get(x["reason"].split("(")[0], 0) + 1
        print("  存疑按原因:", json.dumps(by_reason, ensure_ascii=False))
        print(f"  存疑明细见 {args.report} → suspect[]（人工审阅后重跑或修数据）")


# ── 跨月一附多证补挂执行（服务端 copy，幂等，dry-run 默认） ──────────────────
def run_exec_cross_copies(args):
    import importlib.util
    spec = importlib.util.spec_from_file_location("U", HERE / "upload-to-feishu.py")
    U = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(U)

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    copies = report.get("crossMonthCopies", [])
    if not copies:
        print("report 无 crossMonthCopies，无需补挂")
        return
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token，检查 FEISHU_APP_ID/FEISHU_APP_SECRET")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[CROSS-COPY {mode}] {len(copies)} 文件，archive_root={args.archive_root}")
    stats = {"copied": 0, "exists": 0, "fail": 0}
    for c in copies:
        for dst in c["copies"]:
            ml = month_label(dst["period"])
            d = U.ensure_folder_path(token, args.archive_root, Path(ml) / dst["vno"])
            if not d:
                print(f"  ✗ 建夹失败 {ml}/{dst['vno']} <- {c['name']}"); stats["fail"] += 1
                continue
            existing = [it["name"] for it in U.list_folder_live(token, d) if it.get("type") == "file"]
            if c["name"] in existing:
                print(f"  ✓ 已有 {ml}/{dst['vno']} {c['name']}（跳过）"); stats["exists"] += 1
                continue
            if not args.apply:
                print(f"  [DRY] {ml}/{dst['vno']} <- copy {c['name']}"); stats["copied"] += 1
                continue
            url = f"{U.FEISHU_BASE}/drive/v1/files/{c['token']}/copy"
            ok2, new_tok = False, None
            for _ in range(3):
                r = U.feishu_request("POST", url, headers=U.get_headers(token),
                                     json={"type": "file", "folder_token": d, "name": c["name"]})
                try:
                    b = r.json()
                    if b.get("code") == 0:
                        ok2, new_tok = True, (b.get("data") or {}).get("file_token")
                        break
                except Exception:
                    pass
            # 复拉验证（不凭返回 code 声称成功）
            if ok2:
                after = [it["name"] for it in U.list_folder_live(token, d) if it.get("type") == "file"]
                ok2 = c["name"] in after
            if ok2:
                print(f"  + {ml}/{dst['vno']} <- copy {c['name']}（new {new_tok}，复拉已验证）")
                stats["copied"] += 1
            else:
                print(f"  ✗ {ml}/{dst['vno']} copy 失败/复拉未见 {c['name']}"); stats["fail"] += 1
    print(f"DONE cross-copy {mode} {stats}")


def main():
    ap = argparse.ArgumentParser(description="凭证归档匹配器（按位配对+归前校验，FINCHN-710）")
    ap.add_argument("--entry", help="原始凭证入账表 JSON（{e1..e4}）")
    ap.add_argument("--invoice-entry", help="进项/销项发票入账表 JSON")
    ap.add_argument("--pool", action="append", default=[], help="影像池清单 JSON（可多次）")
    ap.add_argument("--vouchers", help="凭证列表 JSON（记账凭证PDF提取）")
    ap.add_argument("--out", help="归档计划输出 JSON（consumer 格式）")
    ap.add_argument("--report", help="匹配报告输出 JSON")
    ap.add_argument("--exec-cross-copies", action="store_true",
                    help="按 report.crossMonthCopies 补跨月 copy（配 --report/--archive-root）")
    ap.add_argument("--archive-root", help="归档根 folder token（exec-cross-copies 用）")
    ap.add_argument("--apply", action="store_true", help="exec-cross-copies 真执行（默认 dry-run）")
    args = ap.parse_args()
    if args.exec_cross_copies:
        if not args.report or not args.archive_root:
            sys.exit("--exec-cross-copies 需配 --report 与 --archive-root")
        run_exec_cross_copies(args)
        return
    for flag, val in [("--entry", args.entry), ("--pool", args.pool),
                      ("--vouchers", args.vouchers), ("--out", args.out), ("--report", args.report)]:
        if not val:
            sys.exit(f"缺少必填参数 {flag}（或用 --exec-cross-copies 子模式）")
    run_match(args)


if __name__ == "__main__":
    main()
