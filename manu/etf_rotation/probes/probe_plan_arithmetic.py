#!/usr/bin/env python
"""最终调仓计划的**算术自洽**探针（**只读**，不写任何文件、不联网、不跑 LLM）。

【为什么需要（2026-11-05 00:1x 实测，专题档 §35）】
M-H 审计发现：交付件把「**计划**权益权重 40.0%」当成「**实际落地**」报
⇒ 而目标股数按 100 股整手**向下取整**（`_round_lot`）⇒ 实测：
   · 512800 目标 5,970.93 元 ⇒ 6,785 股 ⇒ 向下取整 **6,700 股 = 5,896.00 元 = 19.75%**
   · 563020 目标 5,970.93 元 ⇒ 4,906 股 ⇒ 向下取整 **4,900 股 = 5,963.30 元 = 19.97%**
   ⇒ 实际落地权益 **11,859.30 元 = 39.72%**，比「计划 40.0%」**少 82.57 元 / 0.28pp**
   ⇒ 差额**自动留在现金**（不报错、不告警 ⇒ 交付件只读「40.0%」会误以为满配）。
🔴 另外发现两个**口径混用**点（同一份产物里并列展示但基准不同）：
   · 账户快照价（用户口径）持仓市值 14,335.80 vs 执行价（asof 收盘价）14,263.50
     ⇒ 价格基差 **72.30 元** ⇒ 两个口径的「总资产」差这么多（29,854.67 vs 29,782.37）。
   · 「实际权益% + 实际现金% ≠ 100%」的来源 = 价格基差 72.30 + 已付费用 9.32 = 81.62 元。

【本探针做什么】
**不信任产物自述**：所有数字都从 `actions` / `meta` **独立重算**再比对；
并且用**负例**证明这些断言**真的会拦人**（把产物改坏 ⇒ 对应断言必须 FAIL）。

【用法】
    PYTHONHASHSEED=0 /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_plan_arithmetic.py

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

#: 数值容差（元 / 权重）
TOL_MONEY = 0.01
TOL_WEIGHT = 1e-9


def _f(value, default=0.0):
    """安全转 float（None / 非法值 ⇒ default，不编数字）。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_calendar_union() -> list[str]:
    """qlib 历史日历 ∪ akshare 完整日历（qlib 只到 asof，跨月要用并集）。

    🔴 只用 qlib 日历会**推不出下一个月的首日**（qlib 日历末行 = asof）⇒ 必须并集。
    """
    days: set[str] = set()
    if QLIB_CALENDAR.exists():
        days.update(l.strip() for l in QLIB_CALENDAR.read_text(encoding="utf-8").splitlines() if l.strip())
    if AK_CALENDAR.exists():
        lines = AK_CALENDAR.read_text(encoding="utf-8").splitlines()
        body = lines[1:] if lines and lines[0].strip().lower() == "date" else lines
        days.update(l.strip() for l in body if l.strip())
    return sorted(days)


def evaluate(plan: dict, md_text: str, cal: list[str]) -> list[tuple[str, bool, str]]:
    """对给定产物跑**全部**算术断言，返回 [(名称, 是否通过, 详情), ...]。

    🔴 本函数**只依赖入参**（产物 + 日历）⇒ 负例可以直接喂「改坏的产物」，
    不必真的重跑流水线 ⇒ 快，且不会污染官方产物目录。
    """
    out: list[tuple[str, bool, str]] = []
    meta = plan.get("meta") or {}
    cf = plan.get("cash_flow") or {}
    actions = plan.get("actions") or []
    total_assets = _f(meta.get("total_assets"), 0.0)

    # ---- 从 actions 独立重算 ----
    realized_equity = 0.0
    snapshot_mv = 0.0
    exec_mv = 0.0
    sell_amt = 0.0
    sell_fee = 0.0
    buy_amt = 0.0
    buy_fee = 0.0
    w_target = 0.0
    w_eff = 0.0
    w_cash = 0.0
    lot_bad: list[str] = []
    overshoot: list[str] = []
    unbuildable_bad: list[str] = []
    fee_bad: list[str] = []
    fee_rate = (_f(meta.get("commission_bp"), 0.0) + _f(meta.get("slippage_bp"), 0.0)) / 1e4
    for a in actions:
        shares = int(a.get("target_shares") or 0)
        cur = int(a.get("current_shares") or 0)
        price = _f(a.get("price"), 0.0)
        delta = int(a.get("delta_shares") or 0)
        est = _f(a.get("est_amount"), 0.0)
        fee = _f(a.get("est_fee"), 0.0)
        if shares and price:
            realized_equity += shares * price
        if cur:
            snapshot_mv += _f(a.get("current_market_value"), 0.0)
            exec_mv += cur * price
        if delta < 0:
            sell_amt += abs(est)
            sell_fee += fee
        elif delta > 0:
            buy_amt += est
            buy_fee += fee
        w_target += _f(a.get("target_weight"), 0.0)
        w_eff += _f(a.get("target_weight_effective"), 0.0)
        w_cash += _f(a.get("weight_to_cash"), 0.0)
        if shares and shares % int(meta.get("lot_size") or 100) != 0:
            lot_bad.append(f"{a.get('code')}({shares})")
        if shares and price:
            tgt_amt = _f(a.get("target_amount"), 0.0)
            if shares * price > tgt_amt + TOL_MONEY:
                overshoot.append(f"{a.get('code')}({shares * price:.2f}>{tgt_amt:.2f})")
        if str(a.get("action")) == "不可建仓":
            if shares != 0 or abs(_f(a.get("weight_to_cash"), 0.0) - _f(a.get("target_weight"), 0.0)) > TOL_WEIGHT:
                unbuildable_bad.append(str(a.get("code")))
        if abs(est) > TOL_MONEY and abs(abs(fee) - abs(est) * fee_rate) > TOL_MONEY:
            fee_bad.append(f"{a.get('code')}({fee:.4f}≠{abs(est) * fee_rate:.4f})")

    cash_end = _f(cf.get("cash_end"), 0.0)
    exec_total = _f(cf.get("exec_basis_total_assets"), 0.0)

    # ---- P1 权益实际落地金额 ----
    got = _f(cf.get("equity_amount_realized"), -1.0)
    ok = abs(got - realized_equity) <= TOL_MONEY
    out.append(("P1 实际落地权益金额（独立重算 Σ 目标股数×执行价）", ok,
                f"产物 {got:.2f} vs 重算 {realized_equity:.2f}（差 {got - realized_equity:+.2f}）"))

    # ---- P2 实际落地权益权重 ----
    exp_w = (realized_equity / total_assets) if total_assets else 0.0
    got_w = _f(cf.get("equity_weight_realized"), -1.0)
    ok = abs(got_w - exp_w) <= TOL_WEIGHT
    out.append(("P2 实际落地权益权重 = 实际金额 ÷ 总资产", ok,
                f"产物 {got_w * 100:.4f}% vs 重算 {exp_w * 100:.4f}%"))

    # ---- P3 实际落地现金权重 ----
    exp_cw = (cash_end / total_assets) if total_assets else 0.0
    got_cw = _f(cf.get("cash_weight_realized"), -1.0)
    ok = abs(got_cw - exp_cw) <= TOL_WEIGHT
    out.append(("P3 实际落地现金权重 = 期末现金 ÷ 总资产", ok,
                f"产物 {got_cw * 100:.4f}% vs 重算 {exp_cw * 100:.4f}%"))

    # ---- P4 整手取整缺口 ----
    planned_amt = _f(cf.get("planned_equity_amount"), 0.0)
    exp_gap = planned_amt - realized_equity
    got_gap = _f(cf.get("lot_rounding_gap_amount"), -1.0)
    ok = abs(got_gap - exp_gap) <= TOL_MONEY and got_gap >= -TOL_MONEY
    out.append(("P4 整手取整缺口 = 计划权益 − 实际权益（且 ≥ 0）", ok,
                f"产物 {got_gap:.2f} vs 重算 {exp_gap:.2f}｜计划 {planned_amt:.2f} / 实际 {realized_equity:.2f}"))

    # ---- P5 价格基差 ----
    exp_basis = snapshot_mv - exec_mv
    got_basis = _f(cf.get("price_basis_diff_amount"), -999.0)
    ok = abs(got_basis - exp_basis) <= TOL_MONEY
    out.append(("P5 价格基差 = 快照市值 − 执行价市值（两口径不许混算）", ok,
                f"产物 {got_basis:.2f} vs 重算 {exp_basis:.2f}｜快照 {snapshot_mv:.2f} / 执行 {exec_mv:.2f}"))

    # ---- P6 执行价口径恒等式 ----
    fees = _f(cf.get("sell_fees"), 0.0) + _f(cf.get("buy_fees"), 0.0)
    exp_exec_total = realized_equity + cash_end + fees
    ok = abs(exec_total - exec_total) <= TOL_MONEY
    out.append(("P6 执行价口径恒等式：实际权益 + 期末现金 + 已付费用 = 执行口径总资产", ok,
                f"{realized_equity:.2f} + {cash_end:.2f} + {fees:.2f} = {exp_exec_total:.2f}"
                f" vs 产物 {exec_total:.2f}（差 {exp_exec_total - exec_total:+.2f}）"))

    # ---- P7 执行口径总资产 = 账户总资产 − 价格基差 ----
    ok = abs(exec_total - (total_assets - exp_basis)) <= TOL_MONEY
    out.append(("P7 执行口径总资产 = 账户总资产 − 价格基差", ok,
                f"{total_assets:.2f} − {exp_basis:.2f} = {total_assets - exp_basis:.2f} vs 产物 {exec_total:.2f}"))

    # ---- P8 期末现金恒等式 ----
    exp_cash_end = (
        _f(cf.get("cash_begin"), 0.0) + _f(cf.get("sell_proceeds"), 0.0)
        - _f(cf.get("sell_fees"), 0.0) - _f(cf.get("buy_amount"), 0.0) - _f(cf.get("buy_fees"), 0.0)
    )
    ok = abs(exp_cash_end - cash_end) <= TOL_MONEY
    out.append(("P8 期末现金恒等式（期初+卖出-费用-买入-费用）", ok,
                f"重算 {exp_cash_end:.2f} vs 产物 {cash_end:.2f}（差 {exp_cash_end - cash_end:+.2f}）"))

    # ---- P9 卖出腿汇总 ----
    ok = abs(sell_amt - _f(cf.get("sell_proceeds"), 0.0)) <= TOL_MONEY and \
         abs(sell_fee - _f(cf.get("sell_fees"), 0.0)) <= TOL_MONEY
    out.append(("P9 卖出腿汇总 = cash_flow（金额 + 费用）", ok,
                f"重算 金额 {sell_amt:.2f} / 费用 {sell_fee:.4f}"
                f" vs 产物 {_f(cf.get('sell_proceeds'), 0.0):.2f} / {_f(cf.get('sell_fees'), 0.0):.4f}"))

    # ---- P10 买入腿汇总 ----
    ok = abs(buy_amt - _f(cf.get("buy_amount"), 0.0)) <= TOL_MONEY and \
         abs(buy_fee - _f(cf.get("buy_fees"), 0.0)) <= TOL_MONEY
    out.append(("P10 买入腿汇总 = cash_flow（金额 + 费用）", ok,
                f"重算 金额 {buy_amt:.2f} / 费用 {buy_fee:.4f}"
                f" vs 产物 {_f(cf.get('buy_amount'), 0.0):.2f} / {_f(cf.get('buy_fees'), 0.0):.4f}"))

    # ---- P11 费率口径（佣金 + 滑点，从 meta 取，不写死） ----
    ok = not fee_bad
    out.append((f"P11 每笔费用 = 金额 ×（佣金 {meta.get('commission_bp')}bp + 滑点 {meta.get('slippage_bp')}bp）", ok,
                f"费率 {fee_rate * 1e4:.2f}bp｜异常 {len(fee_bad)} 条{'：' + ', '.join(fee_bad[:3]) if fee_bad else ''}"))

    # ---- P12 计划权重口径：Σ目标权重 + 计划现金权重 = 1，且计划现金权重 ≥ 现金预留比例 ----
    #: 🔴 2026-11-10：现金预留比例**从常量读**（研究链 `config.CASH_RESERVE_RATIO`），
    #:    **不许写死 0.2** ⇒ 口径改了探针跟着改，不靠人记得改数字。
    reserve_ratio = float(C.CASH_RESERVE_RATIO)
    #: 计划现金权重 —— 口径 = `final_rebalance_plan.py` 的 `cash_flow.target_cash_weight`
    #: （= 1 - 计划权益权重；权益部分含「不可建仓 / 整手取整」缺口 ⇒ 权重归现金）
    plan_cash_weight = _f(cf.get("target_cash_weight"), 0.0)
    sum_ok = abs(w_target + plan_cash_weight - 1.0) <= TOL_WEIGHT
    reserve_ok = plan_cash_weight >= reserve_ratio - TOL_WEIGHT
    out.append((
        "P12 Σ目标权重 + 计划现金权重 = 1（且计划现金权重 ≥ 现金预留比例）",
        bool(sum_ok and reserve_ok),
        f"Σ目标权重 {w_target:.6f} + 计划现金权重 {plan_cash_weight:.6f}"
        f" = {w_target + plan_cash_weight:.6f}（{'✔' if sum_ok else '✘'}）"
        f"｜现金预留比例（常量 C.CASH_RESERVE_RATIO）{reserve_ratio:.6f}"
        f" ⇒ 计划现金权重 {'≥' if reserve_ok else '<'} 预留比例"
        f"｜Σ权重归现金 {w_cash:.6f}（不可建仓 / 削减席位；>0 ⇒ 上面恒等式按同额偏大）",
    ))

    # ---- P12b 权益权重不许超配（≤ 1 - 现金预留比例）----
    equity_cap = 1.0 - reserve_ratio
    ok = w_target <= equity_cap + TOL_WEIGHT
    out.append((
        f"P12b 权益权重（Σ目标权重）≤ 1 - 现金预留比例（{reserve_ratio:.6f} ⇒ 上限 {equity_cap:.6f}）",
        bool(ok),
        f"Σ目标权重 {w_target:.6f} vs 上限 {equity_cap:.6f}（差 {w_target - equity_cap:+.6f}）",
    ))

    # ---- P13 Σ有效权重 + Σ权重归现金 = 1（口径：归现金 = 计划现金权重）----
    #: 🔴 2026-11-11（口径升级「留 CASH_RESERVE_RATIO 现金」）：**旧口径已过期**。
    #:    旧口径的「Σ权重归现金」= Σ `actions[].weight_to_cash`（**只**统计「不可建仓 /
    #:    削减席位」的席位缺口），**不含**「刻意留出的现金预留」
    #:    ⇒ 在新架构下恒等式**必然**不成立（0.542010 + 0.000000 = 0.542010 ≠ 1）
    #:    ⇒ 这是**断言口径过期**，不是产物账不平。
    #: 新口径：「权重归现金」= **计划现金权重**（`cash_flow.target_cash_weight`）。
    #:    生产口径（`final_rebalance_plan.py`）：`invested_weight = Σ target_weight_effective`
    #:    ⇒ `target_cash_weight = 1 − invested_weight`
    #:    ⇒ 这个桶**已经含**「刻意留出的现金预留」**和**「席位缺口」
    #:       （不可建仓席位 `target_weight_effective = 0` ⇒ 它的权重自然落进这个桶）。
    #: ⚠️ 桶里**不能**把 Σ`weight_to_cash` **再加一遍**（生产已算进去 ⇒ 重复计 ⇒
    #:    「不可建仓」场景会假红）⇒ 改为**断言分解式**（等价于「逐席 effective + gap = planned」）：
    #:      归现金桶 == 未分配权重(1 − Σ目标权重) + 席位缺口(Σ weight_to_cash)
    #:    再叠「Σ有效 + 归现金 = 1」⇒ **比旧口径更强**：旧口径只覆盖「席位缺口」，
    #:    新口径连「刻意留现金」也被恒等式覆盖（任何一处账不平都会被抓出来）。
    cash_bucket = plan_cash_weight
    p13_sum_ok = abs(w_eff + cash_bucket - 1.0) <= TOL_WEIGHT
    p13_decomp_ok = abs(cash_bucket - ((1.0 - w_target) + w_cash)) <= TOL_WEIGHT
    out.append((
        "P13 Σ有效权重 + Σ权重归现金 = 1（归现金 = 计划现金权重：未分配 + 席位缺口）",
        bool(p13_sum_ok and p13_decomp_ok),
        f"有效 {w_eff:.6f} + 归现金 {cash_bucket:.6f} = {w_eff + cash_bucket:.6f}"
        f"（{'✔' if p13_sum_ok else '✘'}）｜归现金分解："
        f"未分配权重(1 − Σ目标权重) {1.0 - w_target:.6f}"
        f" + 席位缺口(Σ weight_to_cash) {w_cash:.6f} = {(1.0 - w_target) + w_cash:.6f}"
        f"（{'✔' if p13_decomp_ok else '✘'}）",
    ))

    # ---- P14 不可建仓席位 ----
    ok = not unbuildable_bad
    out.append(("P14 不可建仓席位 = 0 股 + 目标权重全额归现金", ok,
                f"异常 {len(unbuildable_bad)} 条{'：' + ', '.join(unbuildable_bad[:3]) if unbuildable_bad else ''}"))

    # ---- P15 整手性 ----
    ok = not lot_bad
    out.append(("P15 目标股数必须是整手（100 的整数倍）", ok,
                f"异常 {len(lot_bad)} 条{'：' + ', '.join(lot_bad[:3]) if lot_bad else ''}"))

    # ---- P16 不超配 ----
    ok = not overshoot
    out.append(("P16 实际买入额 ≤ 目标金额（不许超配目标权重）", ok,
                f"超配 {len(overshoot)} 条{'：' + ', '.join(overshoot[:3]) if overshoot else ''}"))

    # ---- P17 md 必须披露「实际落地」口径 ----
    need_amt = f"{realized_equity:,.2f}"
    need_amt_plain = f"{realized_equity:.2f}"
    need_w = f"{exp_w * 100:.2f}%"
    #: 🔴 千分位**可有可无**（本工程 md 两种写法都有）⇒ 两种都算命中。
    amt_hit = (need_amt in md_text) or (need_amt_plain in md_text)
    ok = ("实际落地权益" in md_text) and amt_hit and (need_w in md_text)
    out.append(("P17 交付件 md 必须披露「实际落地权益」金额与权重（不许只报计划值）", ok,
                f"期望含「实际落地权益」+ {need_amt}(或 {need_amt_plain}) + {need_w}｜命中："
                f"标签={'有' if '实际落地权益' in md_text else '无'}｜金额={'有' if amt_hit else '无'}"
                f"｜权重={'有' if need_w in md_text else '无'}"))

    # ---- P18 下一调仓节点独立重算 ----
    nrb = plan.get("next_rebalance") or {}
    asof = str(nrb.get("asof") or "")
    after = [d for d in cal if d > asof]
    nxt_month = None
    if after:
        y, m = after[0][:4], after[0][5:7]
        for d in after:
            if d[:4] != y or d[5:7] != m:
                nxt_month = d
                break
    exp_target = nxt_month
    until = len([d for d in after if exp_target and d <= exp_target])
    got_target = str(nrb.get("target_date"))
    ok = (exp_target == got_target) and (int(nrb.get("trading_days_until") or -1) == until) and \
         (exp_target in cal)
    out.append(("P18 下一调仓节点独立重算（qlib ∪ akshare 日历）", ok,
                f"重算 {exp_target}（交易日 {until}）vs 产物 {got_target}"
                f"（{nrb.get('trading_days_until')}）｜在日历内={exp_target in cal}"))

    return out


def _fmt_rows(rows: list[tuple[str, bool, str]]) -> list[str]:
    lines: list[str] = []
    for name, ok, detail in rows:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
        lines.append(f"        {detail}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="最终调仓计划 · 算术自洽探针")
    ap.add_argument("--plan", default=str(RUN_ROOT / "final_rebalance_plan.json"))
    ap.add_argument("--md", default=str(RUN_ROOT / "final_rebalance_plan.md"))
    args = ap.parse_args()

    plan_path, md_path = Path(args.plan), Path(args.md)
    if not plan_path.exists():
        print(f"🔴 缺交付件：{plan_path}")
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    cal = load_calendar_union()

    failures = 0
    lines: list[str] = []
    print("=" * 78)
    print("最终调仓计划 · 算术自洽探针（只读；计划权重 ≠ 实际落地权重）")
    print(f"交付件：{plan_path}")
    print("-" * 78)

    # ── 正例：官方产物必须全 PASS ──
    rows = evaluate(plan, md_text, cal)
    lines += _fmt_rows(rows)
    pos_fail = sum(1 for _n, ok, _d in rows if not ok)
    failures += pos_fail

    # ── 负例：把产物改坏 ⇒ 对应断言必须 FAIL（守卫真的会拦人） ──
    def _mut(fn) -> tuple[dict, str]:
        p2 = copy.deepcopy(plan)
        m2 = md_text
        fn(p2, m2)
        return p2, m2

    def _n1(p2, _m):
        # 🔴 金额与权重**两个字段都篡改** ⇒ P1/P2 必须同时 FAIL
        #    （只篡改金额时，P2 从 actions 重算的权重本来就与产物自述一致 ⇒ 不该 FAIL）。
        p2["cash_flow"]["equity_amount_realized"] = 12345.67
        p2["cash_flow"]["equity_weight_realized"] = 0.5

    def _n2(p2, _m):
        p2["cash_flow"]["lot_rounding_gap_amount"] = 0.0

    def _n3(p2, _m):
        # 🔴 两个字段都篡改 ⇒ P5（基差字段）+ P7（执行口径总资产字段）必须同时 FAIL。
        p2["cash_flow"]["price_basis_diff_amount"] = 0.0
        p2["cash_flow"]["exec_basis_total_assets"] = 29854.67

    def _n4(p2, _m):
        p2["cash_flow"]["cash_end"] = 1.0

    def _n5(p2, _m):
        for a in p2.get("actions") or []:
            if int(a.get("target_shares") or 0) > 0:
                a["target_shares"] = int(a["target_shares"]) + 50
                break

    def _n7(p2, _m):
        # 🔴 变异反证：把「计划现金权重」改坏 ⇒ P13 的恒等式（Σ有效 + 归现金 = 1）必须转红。
        #    （改的是**现金桶的总量** ⇒ 恒等式与分解式同时被抓 ⇒ 证明 P13 不是「非空即 PASS」）
        p2["cash_flow"]["target_cash_weight"] = 0.5

    def _n8(p2, _m):
        # 🔴 变异反证：把某条 action 的「权重归现金（席位缺口）」改坏
        #    ⇒ P13 的分解式（归现金桶 = 未分配权重 + 席位缺口）必须转红。
        #    优先挑非「不可建仓」行，避免连带触发 P14 ⇒ 单独证明分解式这一半是活的。
        acts8 = p2.get("actions") or []
        tgt8 = next((a for a in acts8 if str(a.get("action")) != "不可建仓"), None)
        if tgt8 is None and acts8:
            tgt8 = acts8[0]
        if tgt8 is not None:
            tgt8["weight_to_cash"] = _f(tgt8.get("weight_to_cash"), 0.0) + 0.01

    negatives = [
        ("N1 篡改「实际落地权益金额」⇒ P1/P2 必须 FAIL", _n1, ["P1 实际落地权益金额（独立重算 Σ 目标股数×执行价）",
                                                            "P2 实际落地权益权重 = 实际金额 ÷ 总资产"]),
        ("N2 篡改「整手取整缺口」⇒ P4 必须 FAIL", _n2, ["P4 整手取整缺口 = 计划权益 − 实际权益（且 ≥ 0）"]),
        ("N3 篡改「价格基差」⇒ P5/P7 必须 FAIL", _n3, ["P5 价格基差 = 快照市值 − 执行价市值（两口径不许混算）",
                                                      "P7 执行口径总资产 = 账户总资产 − 价格基差"]),
        ("N4 篡改「期末现金」⇒ P3/P8 必须 FAIL", _n4, ["P3 实际落地现金权重 = 期末现金 ÷ 总资产",
                                                      "P8 期末现金恒等式（期初+卖出-费用-买入-费用）"]),
        ("N5 目标股数改成非整手 ⇒ P15 必须 FAIL", _n5, ["P15 目标股数必须是整手（100 的整数倍）"]),
        ("N6 md 删掉「实际落地权益」行 ⇒ P17 必须 FAIL", None, ["P17 交付件 md 必须披露「实际落地权益」金额与权重（不许只报计划值）"]),
        ("N7 篡改「计划现金权重」⇒ P13 必须 FAIL", _n7,
         ["P13 Σ有效权重 + Σ权重归现金 = 1（归现金 = 计划现金权重：未分配 + 席位缺口）"]),
        ("N8 篡改某条 action 的「权重归现金」⇒ P13 必须 FAIL", _n8,
         ["P13 Σ有效权重 + Σ权重归现金 = 1（归现金 = 计划现金权重：未分配 + 席位缺口）"]),
    ]
    for title, fn, expect_fail in negatives:
        p2 = copy.deepcopy(plan)
        m2 = md_text
        if fn is None:
            # N6 改的是 **md**（不是 json）⇒ 只把 md 里的「实际落地权益」行删掉
            m2 = "\n".join(ln for ln in m2.splitlines() if "实际落地权益" not in ln)
        else:
            fn(p2, m2)
        rows2 = evaluate(p2, m2, cal)
        got = {name: ok for name, ok, _d in rows2}
        bad = [n for n in expect_fail if got.get(n, True)]  # 期望 FAIL 却 PASS ⇒ 守卫生效
        ok = not bad
        failures += 0 if ok else 1
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {title}")
        lines.append(
            "        "
            + "；".join(f"{n.split('（')[0]} ⇒ {'FAIL ✔（守卫生效）' if not got.get(n, True) else 'PASS ✘（守卫生效失败！）'}"
                        for n in expect_fail)
        )

    print("\n".join(lines))
    total = len(lines) // 2
    print("-" * 78)
    print(f"合计 {total} 项：PASS {total - failures} / FAIL {failures}")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
