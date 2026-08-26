#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test-archive-matcher.py — gen-archive-matcher.py 回归测试（FINCHN-710 scope d）

以磐沄 FY-2024 实际发现的 6 处错放为 fixture（餐费明细表 7,940/7,030/6,050/7,370/6,020、
利息计算表 2,213.08，凭证行数据取自当期真实凭证列表），断言按位配对后不再错放；
另覆盖：长度不一致禁兜底、凭证不存在/月份超范围、金额摘要双不吻合、同名冲突、
同月多证合条、跨月补挂清单、缺件报告。全部自造 mini 输入（/tmp），不依赖线上数据。
"""
import json, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MATCHER = HERE / "gen-archive-matcher.py"

# ── 真实 6 案例（名称/凭证字号/记账期间/金额 均照抄入账表原行） ─────────────
FIXTURES = [
    # (name, vno, period, amount, [(month,vno)...按位期望], 历史错放月(旧bug会把全部vno放首月))
    ("20240630 磐沄员工福利餐费明细表 7,940.pdf", "转-14、转-38", "202408、202406", 7940,
     [("202408", "转-14"), ("202406", "转-38")], "202408"),
    ("20240731 磐沄员工福利餐费明细表 7,370.pdf", "转-11、转-51", "202409、202407", 7370,
     [("202409", "转-11"), ("202407", "转-51")], "202409"),
    ("20240831 磐沄员工福利餐费明细表 7,030.pdf", "转-40、转-13", "202408、202410", 7030,
     [("202408", "转-40"), ("202410", "转-13")], "202408"),
    ("20240930 磐沄员工福利餐费明细表 6,050.pdf", "转-16、转-35", "202411、202409", 6050,
     [("202411", "转-16"), ("202409", "转-35")], "202411"),
    ("20241031 磐沄员工福利餐费明细表 6,020.pdf", "转-56、转-9", "202410、202412", None,
     [("202410", "转-56"), ("202412", "转-9")], "202410"),
    ("20240709 关联方借款利息增值税及其附加计算表 2,213.08.pdf", "转-6、转-19", "202409、202407", 2213.08,
     [("202409", "转-6"), ("202407", "转-19")], "202409"),
]

# ── mini 凭证列表：6 案例涉及的 12 个 (月,凭证号)，行数据取自真实凭证 ────────
VOUCHERS = [
    # (yyyymm, vno, summary, [(dr,cr)...])
    ("202406", "转-38", "计提当月员工福利餐费", [(4120, 0), (2320, 0), (360, 0), (1140, 0), (0, 7940)]),
    ("202408", "转-14", "07-2024 对公付款 - 1_202406 员工福利餐费", [(4120, 0), (2320, 0), (360, 0), (1140, 0), (0, 7940)]),
    ("202409", "转-11", "09-2024 对公付款 - 1_07-2024 员工福利餐费", [(3430, 0), (1960, 0), (630, 0), (1350, 0), (0, 7370)]),
    ("202407", "转-51", "计提当月员工福利餐费", [(3430, 0), (1960, 0), (630, 0), (1350, 0), (0, 7370)]),
    ("202408", "转-40", "计提当月员工福利餐费", [(3220, 0), (1980, 0), (630, 0), (1200, 0), (0, 7030)]),
    ("202410", "转-13", "09-2024 对公付款 - 1_08-2024 员工福利餐费", [(3220, 0), (1980, 0), (630, 0), (1200, 0), (0, 7030)]),
    ("202411", "转-16", "10-2024 对公付款 - 1_09-2024 员工福利餐费", [(2830, 0), (1690, 0), (390, 0), (1140, 0), (0, 6050)]),
    ("202409", "转-35", "计提当月员工福利餐费", [(2830, 0), (1690, 0), (390, 0), (1140, 0), (0, 6050)]),
    ("202410", "转-56", "计提当月员工福利餐费", [(2760, 0), (1640, 0), (480, 0), (1140, 0), (0, 6020)]),
    ("202412", "转-9", "11-2024 对公付款 - 1_10-2024 员工福利餐费", [(2760, 0), (1640, 0), (480, 0), (1140, 0), (0, 6020)]),
    ("202409", "转-6", "磐曜科技资金拆借利息收入确认", [(36884.75, 0), (-34796.93, 0), (0, 2087.82)]),
    ("202407", "转-19", "202406 增值税及其附加计提_磐曜科技资金拆借视同销售利息增值税",
     [(2087.82, 0), (0, 2087.82), (73.07, 0), (31.31, 0), (20.88, 0), (0, 73.07), (0, 31.31), (0, 20.88)]),
    # 同月多证案例用
    ("202412", "转-20", "结息及利息收入确认", [(181.11, 0), (0, 181.11)]),
    ("202412", "收-2", "活期存款结息", [(181.11, 0), (0, 181.11)]),
]

HDR3 = [["电子档案"], [], ["附件名称", "附件备注", "附件小类", "附件金额", "附件所属组", "上传时间", "凭证字号", "记账期间"]]


def entry_row(name, amount, vno, period):
    return [name, None, None, amount, None, None, vno, period, None, None, None]


def build_inputs(tmp):
    rows = [entry_row(n, a, v, p) for (n, v, p, a, _, _) in FIXTURES]
    # 边界案例行
    rows += [
        entry_row("20240815 长度不一致测试 餐费 100.pdf", 100, "转-7、转-8", "202408"),          # 长度不匹配→存疑
        entry_row("20240820 凭证不存在测试 餐费 200.pdf", 200, "转-99", "202408"),               # 凭证不在列表→存疑
        entry_row("20241231 月份超范围测试 餐费 300.pdf", 300, "转-40", "202501"),               # 超凭证列表范围→存疑
        entry_row("20240815 无关附件名 500.pdf", 500, "转-40", "202408"),                        # 金额摘要双不吻合→存疑
        entry_row("20240825 同名冲突A 餐费 400.pdf", 400, "转-40", "202408"),                    # 同名冲突→存疑（两行目标不同）
        entry_row("20240825 同名冲突A 餐费 400.pdf", 400, "转-13", "202410"),
        entry_row("20241221 磐沄 华夏银行 入账水单 活期账户利息收入 181.11.pdf", 181.11,
                  "转-20、收-2", "202412、202412"),                                              # 同月多证→合一条
        entry_row("20241115 池中缺件测试 餐费 900.pdf", 900, "转-13", "202410"),                # 影像池无此文件→缺件
        entry_row("20240710 无凭证号行 附件.pdf", 50, None, None),                               # 无凭证号→noVoucherRows
    ]
    (tmp / "entry.json").write_text(json.dumps({"e1": HDR3 + rows}, ensure_ascii=False), encoding="utf-8")

    vrows = [["凭证列表"], ["北京磐沄科技有限公司"], ["日期", "凭证字号", "摘要", "科目", "借方金额", "贷方金额"]]
    for ym, vno, summ, lines in VOUCHERS:
        for dr, cr in lines:
            vrows.append([f"{ym[:4]}-{ym[4:]}-15", vno, summ, "220201_应付职工薪酬", dr or None, cr or None])
    (tmp / "vouchers.json").write_text(json.dumps(vrows, ensure_ascii=False), encoding="utf-8")

    names = [n for (n, *_ ) in FIXTURES] + [
        "20240815 长度不一致测试 餐费 100.pdf", "20240820 凭证不存在测试 餐费 200.pdf",
        "20241231 月份超范围测试 餐费 300.pdf", "20240815 无关附件名 500.pdf",
        "20240825 同名冲突A 餐费 400.pdf", "20241221 磐沄 华夏银行 入账水单 活期账户利息收入 181.11.pdf",
        "20240710 池外多余文件 餐费 77.pdf",
    ]
    pool = [{"name": n, "token": f"tok{i:03d}", "path": f"FY-2024/x/{n}"} for i, n in enumerate(names)]
    (tmp / "pool.json").write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")


def main():
    fails = []

    def check(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory(prefix="arch-match-test-") as td:
        tmp = Path(td)
        build_inputs(tmp)
        r = subprocess.run([sys.executable, str(MATCHER),
                            "--entry", str(tmp / "entry.json"),
                            "--pool", str(tmp / "pool.json"),
                            "--vouchers", str(tmp / "vouchers.json"),
                            "--out", str(tmp / "plan.json"),
                            "--report", str(tmp / "report.json")],
                           capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode != 0:
            print(r.stderr)
            sys.exit("matcher 运行失败")
        plan = json.loads((tmp / "plan.json").read_text(encoding="utf-8"))
        rep = json.loads((tmp / "report.json").read_text(encoding="utf-8"))

        targets = {(x["target"]["period"], x["target"]["vno"], x["name"]) for x in plan}
        by_name = {}
        for x in plan:
            by_name.setdefault(x["name"], []).append((x["target"]["period"], x["target"]["vno"]))

        print("\n== 用例1：6 处历史错放按位配对（核心回归）==")
        for name, vno, period, amt, expect, bug_month in FIXTURES:
            got = sorted(by_name.get(name, []))
            check(got == sorted(expect),
                  f"{name[-28:]}: 目标 {got} == 期望 {sorted(expect)}")
            for em, ev in expect:
                check((em, ev, name) in targets, f"  ({em},{ev}) 在 plan")
                wrong_m = bug_month if em != bug_month else "其它月"
                check((wrong_m, ev, name) not in targets or (wrong_m == em),
                      f"  ({wrong_m},{ev}) 不在 plan（旧 bug 错放位）")

        print("\n== 用例2：跨月补挂清单 ==")
        cc = {c["name"]: c for c in rep["crossMonthCopies"]}
        check(len(rep["crossMonthCopies"]) == 6, f"跨月文件 6 个（实际 {len(rep['crossMonthCopies'])}）")
        c2 = cc.get("20240731 磐沄员工福利餐费明细表 7,370.pdf")
        check(bool(c2) and c2["primary"] == {"period": "202409", "vno": "转-11"}
              and c2["copies"] == [{"period": "202407", "vno": "转-51"}],
              "转-11/转-51 主目标=首配对月202409，补挂=202407（按位非按月排序）")

        print("\n== 用例3：闸1 长度不一致禁兜底 ==")
        check(all(x["name"] != "20240815 长度不一致测试 餐费 100.pdf" for x in plan),
              "长度不一致行不落盘")
        check(any(s["name"] == "20240815 长度不一致测试 餐费 100.pdf"
                  and s["reason"].startswith("pair-length-mismatch") for s in rep["suspect"]),
              "进存疑 pair-length-mismatch")

        print("\n== 用例4：闸2 凭证不存在/月份超范围/金额摘要双不吻合 ==")
        rs = {s["name"]: s["reason"] for s in rep["suspect"]}
        check(rs.get("20240820 凭证不存在测试 餐费 200.pdf") == "voucher-not-in-list", "凭证不存在→存疑")
        check(rs.get("20241231 月份超范围测试 餐费 300.pdf") == "month-out-of-voucher-list-range", "月份超范围→存疑")
        check(rs.get("20240815 无关附件名 500.pdf") == "amount-summary-mismatch", "金额摘要双不吻合→存疑")

        print("\n== 用例5：同名冲突 ==")
        check(rs.get("20240825 同名冲突A 餐费 400.pdf", "").startswith("target-conflict"),
              "同名不同目标→存疑")

        print("\n== 用例6：同月多证合条（执行脚本原生 copy 路径）==")
        same = [x for x in plan if x["name"].endswith("活期账户利息收入 181.11.pdf")]
        check(len(same) == 1 and same[0]["target"] == {"period": "202412", "vno": "转-20、收-2"},
              f"单条 vno 顿号连接（实际 {same}）")
        check(all(c["name"] != "20241221 磐沄 华夏银行 入账水单 活期账户利息收入 181.11.pdf"
                  for c in rep["crossMonthCopies"]), "同月多证不进跨月补挂")

        print("\n== 用例7：缺件与池外报告 ==")
        check(any(m["name"] == "20241115 池中缺件测试 餐费 900.pdf" for m in rep["missingAttachments"]),
              "入账有池无→missingAttachments")
        check("20240710 池外多余文件 餐费 77.pdf" in rep["unmatchedPool"], "池有入账无→unmatchedPool")
        check(any(n["name"] == "20240710 无凭证号行 附件.pdf" for n in rep["noVoucherRows"]),
              "无凭证号行→noVoucherRows")

        print("\n== 用例8：统计自洽 ==")
        s = rep["stats"]
        check(s["plan_entries"] == len(plan), "plan_entries 与 plan 长度一致")
        check(s["suspects"] == len(rep["suspect"]), "suspects 数一致")
        expect_plan = 6 * 2 + 1  # 6 案例各 2 目标条 + 同月多证 1 条
        check(len(plan) == expect_plan, f"plan 条数 {len(plan)} == 期望 {expect_plan}")

    print()
    if fails:
        print(f"FAILED：{len(fails)} 项未过")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
