#!/usr/bin/env python
"""交付件「**信号日 vs 执行日**」口径探针（**只读**，不写任何文件、不联网、不跑 LLM）。

【为什么需要（2026-11-05 00:2x 实测，专题档 §37）】
M-J 审计发现交付件有**两个真缺陷**（都出在**用户下单依据**上，报告侧没有）：

① 🔴 **计划件从未说明「信号日 ≠ 执行日」**
   引擎口径 `config.EXECUTION_LAG_DAYS = 1`：**T 日收盘出信号 / T+1 收盘执行**
   （报告 §一 口径表有写，但**计划件没有**）。
   旧计划件头部只写「**调仓节点：2026-12-01** …… 距今 **20 个交易日**」，
   且纪律条写「下一个调仓节点当日要按当日收盘价重新折算整手股数」
   ⇒ 读者会以为 **12-01 当天下单**
   ⇒ 那等于拿 **11-03 的旧信号**、用 12-01 的价格成交
   ⇒ **比回测口径早一天，不在回测假设内**（§十四.7 的持仓区间 (exec_i, exec_{i+1}] 也是按 exec 日算的）。
   实测正确口径：**信号日 2026-12-01 ⇒ 执行日 2026-12-02**（两者都在交易日历内）。

② 🔴 **「距今 20 个交易日」起算点写错**
   20 是**自信号日 asof 2026-11-03** 起算的交易日数；
   旧文案写「距今」⇒ 读者会从**读到文件的当天**起算（本例 2026-11-05 ⇒ 只有 **19** 个交易日）
   ⇒ 倒计时差 1 天。

【本探针做什么】
**不信任产物自述**：执行日**独立重算**（信号日 + lag 个交易日，用日历并集）再比对；
并用**负例**证明这些断言**真的会拦人**（把产物改坏 ⇒ 对应断言必须 FAIL）。

【用法】
    PYTHONHASHSEED=0 /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_execution_date.py

【副作用】
**零写入**：只读官方产物，不建 scratch 目录。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys

from pathlib import Path

#: 研究链根（探针目录不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ETF_ROOT = Path(__file__).resolve().parents[1]
if str(_ETF_ROOT) not in sys.path:
    sys.path.insert(0, str(_ETF_ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）

RUN_ROOT = C.ETF_RUN_ROOT
QLIB_CALENDAR = Path(C.PROVIDER_URI) / "calendars" / "day.txt"
AK_CALENDAR = C.ETF_DATA_ROOT / "trade_calendar.csv"

#: 引擎口径：信号→执行的滞后交易日数（config.EXECUTION_LAG_DAYS）
EXPECTED_LAG = 1


def load_calendar_union() -> list[str]:
    """qlib 历史日历 ∪ akshare 完整日历（qlib 只到 asof，跨月要用并集）。

    🔴 只用 qlib 日历推不出 12-01/12-02（qlib 末行 = asof 2026-11-03）⇒ 必须并集。
    """
    days: set[str] = set()
    if QLIB_CALENDAR.exists():
        for line in QLIB_CALENDAR.read_text(encoding="utf-8").splitlines():
            v = line.strip()[:10]
            if len(v) == 10 and v[4] == "-" and v[7] == "-":
                days.add(v)
    if AK_CALENDAR.exists():
        df = AK_CALENDAR.read_text(encoding="utf-8").splitlines()
        for line in df[1:]:
            v = line.strip()[:10]
            if len(v) == 10 and v[4] == "-" and v[7] == "-":
                days.add(v)
    return sorted(days)


def shift_trading_day(cal: list[str], base: str, lag: int) -> str | None:
    """返回 base 之后第 lag 个交易日（lag=0 ⇒ base 本身）；推不出来返回 None。"""
    if lag <= 0:
        return base
    after = [d for d in cal if d > base]
    if len(after) < lag:
        return None
    return after[lag - 1]


def evaluate(plan: dict, md_text: str, report: dict, cal: list[str]) -> list[tuple[str, bool, str]]:
    """对给定产物跑**全部**断言，返回 [(名称, 是否通过, 详情), ...]。

    🔴 本函数**只依赖入参** ⇒ 负例可以直接喂「改坏的产物」，不必重跑流水线。
    """
    out: list[tuple[str, bool, str]] = []
    nxt = plan.get("next_rebalance") or {}
    rep_nxt = report.get("next_rebalance") or {}
    disc = plan.get("execution_discipline") or {}

    signal_date = str(nxt.get("signal_date") or nxt.get("target_date") or "")
    exec_date = str(nxt.get("execution_date") or "")
    lag = nxt.get("execution_lag_days")

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, bool(ok), detail))

    # P1 执行日必须算出来（不许空）
    add("P1 执行日必须算出来（不许空、不许编）",
        bool(exec_date),
        f"execution_date={exec_date or '（空）'}")

    # P2 信号日 ≠ 执行日（lag≥1 ⇒ 必然不同）
    add("P2 信号日 ≠ 执行日",
        bool(signal_date) and bool(exec_date) and signal_date != exec_date,
        f"signal={signal_date}｜exec={exec_date}")

    # P3 执行日 == 信号日 + lag 个交易日（**独立重算**）
    expect_exec = shift_trading_day(cal, signal_date, EXPECTED_LAG) if signal_date else None
    add("P3 执行日 = 信号日 + 1 个交易日（独立重算）",
        expect_exec is not None and exec_date == expect_exec,
        f"独立重算={expect_exec}｜产物={exec_date}")

    # P4 执行日必须在交易日历并集内
    add("P4 执行日在交易日历并集内",
        bool(exec_date) and exec_date in set(cal),
        f"calendar_last={cal[-1] if cal else '-'}｜exec={exec_date}")

    # P5 滞后天数必须等于引擎口径
    add("P5 execution_lag_days == 引擎口径 EXECUTION_LAG_DAYS",
        lag == EXPECTED_LAG,
        f"产物 lag={lag}｜引擎口径={EXPECTED_LAG}")

    # P6 计划 md 头部必须写明「执行日 = <日期>」
    add("P6 计划 md 必须写明「执行日 = <日期>」",
        (f"**执行日 = {exec_date}**" in md_text) if exec_date else False,
        f"查找串='**执行日 = {exec_date}**'")

    # P7 计划 md 必须写明「信号日」（且不是只写「调仓节点」）
    add("P7 计划 md 必须写明「信号日」",
        ("调仓信号日" in md_text) and bool(signal_date) and signal_date in md_text,
        f"signal={signal_date}")

    # P8 倒计时天数必须自**信号日**起算，且数字与报告一致（不许写「距今」）
    n_days = nxt.get("trading_days_until")
    ok8 = (
        isinstance(n_days, int)
        and f"**{n_days} 个交易日**" in md_text
        and "自信号日起算" in md_text
    )
    add("P8 倒计时天数自信号日起算（数字与报告一致、不许写「距今」）",
        ok8,
        f"trading_days_until={n_days}｜md 含「自信号日起算」={'自信号日起算' in md_text}")

    # P9 每条 action 的执行说明必须同时含「信号日」与「执行日」
    actions = plan.get("actions") or []
    bad9 = [
        str(a.get("code"))
        for a in actions
        if "信号日" not in str(a.get("execution_note") or "")
        or "执行日" not in str(a.get("execution_note") or "")
    ]
    add("P9 每条 action 的执行说明必须同时写「信号日」与「执行日」",
        bool(actions) and not bad9,
        f"actions={len(actions)}｜不合规={bad9 or '无'}")

    # P10 plan JSON 必须有 execution_discipline 结构化字段
    ok10 = (
        bool(disc)
        and str(disc.get("signal_date") or "") == signal_date
        and str(disc.get("execution_date") or "") == exec_date
    )
    add("P10 plan JSON 必须有 execution_discipline（信号日/执行日）",
        ok10,
        f"disc={json.dumps(disc, ensure_ascii=False)[:120]}")

    # P11 计划 md 必须声明「股数是按 asof 收盘价算的样例」并要求重算
    ok11 = ("样例" in md_text) and ("重算" in md_text)
    add("P11 计划 md 必须声明股数是样例值且要求重算",
        ok11,
        f"含「样例」={'样例' in md_text}｜含「重算」={'重算' in md_text}")

    # P12 计划件与分析报告的 next_rebalance 必须同源（执行日一致）
    rep_exec = str(rep_nxt.get("execution_date") or "")
    add("P12 计划件与报告的 next_rebalance 同源（执行日一致）",
        bool(rep_exec) and rep_exec == exec_date,
        f"报告={rep_exec}｜计划={exec_date}")

    return out


def _fmt_rows(rows: list[tuple[str, bool, str]]) -> list[str]:
    lines: list[str] = []
    for name, ok, detail in rows:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
        lines.append(f"        {detail}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="交付件 · 信号日 vs 执行日口径探针")
    ap.add_argument("--plan", default=str(RUN_ROOT / "final_rebalance_plan.json"))
    ap.add_argument("--md", default=str(RUN_ROOT / "final_rebalance_plan.md"))
    ap.add_argument("--report", default=str(RUN_ROOT / "etf_analysis_report.json"))
    args = ap.parse_args()

    plan_path, md_path, rep_path = Path(args.plan), Path(args.md), Path(args.report)
    for p, label in ((plan_path, "计划 json"), (md_path, "计划 md"), (rep_path, "分析报告 json")):
        if not p.exists():
            print(f"🔴 缺{label}：{p}")
            return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    md_text = md_path.read_text(encoding="utf-8")
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    cal = load_calendar_union()

    failures = 0
    lines: list[str] = []
    print("=" * 78)
    print("交付件 · 信号日 vs 执行日口径探针（只读；信号日 ≠ 执行日）")
    print(f"交付件：{plan_path}")
    print("-" * 78)

    rows = evaluate(plan, md_text, report, cal)
    lines += _fmt_rows(rows)
    failures += sum(1 for _n, ok, _d in rows if not ok)

    # 🔴 负例写法纪律（2026-11-05 00:2x 实测踩坑，专题档 §37）：
    #   **md 是不可变字符串 ⇒ 改 md 的负例必须把新字符串「返回」给调用方**；
    #   若在函数里 `m2 = m2.replace(...)`（按值传参）⇒ 调用方拿到的还是**原 md**
    #   ⇒ 负例**根本没改到被断言的对象** ⇒ 断言照样 PASS ⇒ 守卫生效失败（假阴性）。
    #   ⇒ 本探针的负例函数统一签名 `fn(p2, r2) -> 新 md`（json 侧就地改 dict）。
    def _mut(fn) -> tuple[dict, str, dict]:
        p2 = copy.deepcopy(plan)
        r2 = copy.deepcopy(report)
        m2 = fn(p2, r2)
        return p2, m2, r2

    def _n1(p2: dict, r2: dict) -> str:
        """md 删掉「**执行日 = <日期>**」"""
        exec_date = str((p2.get("next_rebalance") or {}).get("execution_date") or "")
        return md_text.replace(f"**执行日 = {exec_date}**", "**执行日（未标注）**")

    def _n2(p2: dict, r2: dict) -> str:
        """把执行日改成信号日本身 ⇒ P2/P3 必须 FAIL"""
        nxt = p2.get("next_rebalance") or {}
        nxt["execution_date"] = nxt.get("signal_date") or nxt.get("target_date")
        return md_text

    def _n3(p2: dict, r2: dict) -> str:
        """把执行日改成非交易日（2026-12-05 周六）⇒ P4 必须 FAIL"""
        (p2.get("next_rebalance") or {})["execution_date"] = "2026-12-05"
        return md_text

    def _n4(p2: dict, r2: dict) -> str:
        """md 里的 20 改成 19 ⇒ P8 必须 FAIL"""
        n = (p2.get("next_rebalance") or {}).get("trading_days_until")
        return md_text.replace(f"**{n} 个交易日**", f"**{int(n) - 1} 个交易日**")

    def _n5(p2: dict, r2: dict) -> str:
        """把 lag 改成 0 ⇒ P5 必须 FAIL"""
        (p2.get("next_rebalance") or {})["execution_lag_days"] = 0
        return md_text

    def _n6(p2: dict, r2: dict) -> str:
        """报告与计划的执行日不一致 ⇒ P12 必须 FAIL"""
        (r2.get("next_rebalance") or {})["execution_date"] = "2026-12-03"
        return md_text

    negatives = [
        ("N1 md 删掉「执行日 = 日期」⇒ P6 必须 FAIL", _n1, ["P6 计划 md 必须写明「执行日 = <日期>」"]),
        ("N2 执行日改成信号日 ⇒ P2/P3 必须 FAIL", _n2,
         ["P2 信号日 ≠ 执行日", "P3 执行日 = 信号日 + 1 个交易日（独立重算）"]),
        ("N3 执行日改成非交易日（周六）⇒ P4 必须 FAIL", _n3,
         ["P4 执行日在交易日历并集内"]),
        ("N4 md 倒计时天数改成 19 ⇒ P8 必须 FAIL", _n4,
         ["P8 倒计时天数自信号日起算（数字与报告一致、不许写「距今」）"]),
        ("N5 execution_lag_days 改成 0 ⇒ P5 必须 FAIL", _n5,
         ["P5 execution_lag_days == 引擎口径 EXECUTION_LAG_DAYS"]),
        ("N6 报告与计划执行日不一致 ⇒ P12 必须 FAIL", _n6,
         ["P12 计划件与报告的 next_rebalance 同源（执行日一致）"]),
    ]
    for title, fn, expect_fail in negatives:
        p2, m2, r2 = _mut(fn)
        got = {name: ok for name, ok, _d in evaluate(p2, m2, r2, cal)}
        bad = [n for n in expect_fail if got.get(n, True)]
        ok = not bad
        failures += 0 if ok else 1
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {title}")
        lines.append(
            "        "
            + "；".join(
                f"{n.split('（')[0]} ⇒ {'FAIL ✔（守卫生效）' if not got.get(n, True) else 'PASS ✘（守卫生效失败！）'}"
                for n in expect_fail
            )
        )

    print("\n".join(lines))
    total = len(lines) // 2
    print("-" * 78)
    print(f"合计 {total} 项：PASS {total - failures} / FAIL {failures}")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
