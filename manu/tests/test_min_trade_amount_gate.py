"""🔴 方案一 · 最小调仓门槛的**钉子测试**（2026-11-13）。

【方案一是什么】
按**交易性质**把「最小调仓金额」拆成**三档**（而不是单一金额一刀切）：
  ① **清仓止损**（卖到 0 股）⇒ 门槛 **0 元** ⇒ 风控优先于手续费，**无条件执行**；
  ② **新开仓**（当前 0 股 → 买）⇒ 门槛 **3,800 元**；
  ③ **存量微调**（加仓 / 减仓）⇒ 门槛 **2,800 元**。

【为什么必须带「死锁」检测】
3 万本金、5 只等权 ⇒ 单只标准席 ≈ 30,035 × 16% ≈ **4,805 元**
⇒ 门槛若硬设 5,000 元 ⇒ 4,805 < 5,000 ⇒ **所有「新开仓」被全部拦死**
⇒ 以后**再也买不进任何新 ETF**（数学死锁，不是风控）
⇒ 本轮「所有新开仓都被拦」时必须**显式报警**（`deadlock_risk=True`）。

【本文件钉死的行为】
  ① 三档门槛各自生效；清仓止损 0 元门槛**无条件放行**；
  ② 被拦的**行不删**（仍留在 actions 里），但 delta / 金额 / 费用归零
     ⇒ **现金流不受影响**（既无卖出回款，也无买入支出）；
  ③ 被拦行的 `target_weight_effective` 按**当前市值**重算、`weight_to_cash` 记差额
     ⇒ 目标权重与期末现金**自洽**；
  ④ 披露如实：被拦明细 / 折合 bp / 省下的最低佣金 / 死锁风险；
  ⑤ 开关与 CLI 覆盖（`--no-min-amount-gate` 与三档阈值覆盖）都生效。

【夹具】全部自造（纯 dict）⇒ **不依赖服务器路径、不读 canonical 产物**。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

_ETF_DIR = Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import min_trade_gate as gate_mod  # noqa: E402

import config as C  # noqa: E402

#: 总资产（3 万本金口径，与方案一的死锁推导一致）
TOTAL_ASSETS = 30035.0


# =============================================================================
# 夹具：纯 dict 动作行（字段名与 `final_rebalance_plan.py` 的 actions 对齐）
# =============================================================================
def _base_row(**overrides) -> dict:
    """一条**最小**动作行。"""
    row = {
        "code": "512800",
        "name": "银行ETF",
        "current_shares": 0,
        "price": 1.0,
        "target_weight": 0.0,
        "target_amount": 0.0,
        "target_shares": 0,
        "delta_shares": 0,
        "est_amount": 0.0,
        "est_fee": 0.0,
        "action": "买入",
        "reason": "",
        "ta_rejected": False,
        "target_weight_effective": None,
        "weight_to_cash": None,
    }
    row.update(overrides)
    return row


def _new_open_row(
    *,
    code: str = "512800",
    price: float = 1.0,
    shares: int = 3000,
    amount: float | None = None,
    target_weight: float = 0.10,
    reason: str = "技术面买入信号",
) -> dict:
    """**新开仓**行（当前 0 股 ⇒ `delta_shares > 0` ⇒ 适用「新开仓」档）。"""
    amount = float(shares * price) if amount is None else float(amount)
    return _base_row(
        code=code,
        current_shares=0,
        price=price,
        target_weight=target_weight,
        target_amount=shares * price,
        target_shares=shares,
        delta_shares=shares,
        est_amount=amount,
        action="买入",
        reason=reason,
    )


def _adjust_row(
    *,
    code: str = "512800",
    price: float = 0.88,
    current_shares: int = 6700,
    target_shares: int = 4378,
    amount: float | None = None,
    target_weight: float = 0.14,
    reason: str = "减仓",
) -> dict:
    """**存量微调**行（当前有仓 ⇒ 适用「存量微调」档）。"""
    delta = target_shares - current_shares
    amount = float(delta * price) if amount is None else float(amount)
    return _base_row(
        code=code,
        current_shares=current_shares,
        price=price,
        target_weight=target_weight,
        target_amount=target_shares * price,
        target_shares=target_shares,
        delta_shares=delta,
        est_amount=amount,
        action="卖出" if delta < 0 else "买入",
        reason=reason,
    )


def _liquidate_row(
    *,
    code: str = "159915",
    price: float = 0.60,
    current_shares: int = 500,
    amount: float = -300.0,
    reason: str = "跌破 MA60 清仓",
) -> dict:
    """**清仓止损**行（卖到 0 股 ⇒ `target_shares == 0` ⇒ 适用 0 元门槛档）。"""
    return _base_row(
        code=code,
        current_shares=current_shares,
        price=price,
        target_weight=0.0,
        target_amount=0.0,
        target_shares=0,
        delta_shares=-current_shares,
        est_amount=amount,
        action="卖出",
        reason=reason,
    )


def _flat(text: str) -> str:
    """去掉千分位逗号 ⇒ 「3,000」与「3000」都能被同一条断言命中。"""
    return str(text).replace(",", "").replace(" ", "")


# =============================================================================
# ① 清仓止损：0 元门槛 ⇒ 无条件放行
# =============================================================================
def test_liquidate_stop_loss_is_never_blocked_by_the_zero_threshold() -> None:
    """① 清仓止损（卖到 0 股、金额仅 300 元）⇒ 门槛 0 元 ⇒ **无条件放行**。"""
    row = _liquidate_row(current_shares=500, price=0.60, amount=-300.0)
    snapshot = dict(row)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["thresholds"]["清仓止损"] == 0.0, "清仓档门槛必须是 0 元"
    assert disclosure["considered_actions"] == 1, "清仓行必须**参与**门槛判定（不是被忽略）"
    assert disclosure["skipped_count"] == 0
    assert disclosure["skipped"] == []
    assert row == snapshot, "放行的行不得被就地改写"
    assert row["action"] == "卖出"
    assert row["delta_shares"] == -500
    assert row["est_amount"] == -300.0
    assert "min_amount_skipped" not in row


# =============================================================================
# ② 新开仓：3,800 元门槛
# =============================================================================
def test_new_open_4805_meets_the_3800_threshold_and_passes() -> None:
    """② 新开仓 4,805 元（3 万本金 5 只等权的标准席）⇒ 达标**放行**。"""
    row = _new_open_row(code="512800", price=0.961, shares=5000)
    assert row["est_amount"] == pytest.approx(4805.0)
    snapshot = dict(row)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["considered_actions"] == 1
    assert disclosure["skipped_count"] == 0
    assert disclosure["new_open_total"] == 1
    assert disclosure["new_open_skipped"] == 0
    assert row == snapshot, "达标的行不得被就地改写"
    assert row["delta_shares"] == 5000
    assert row["est_amount"] == pytest.approx(4805.0)
    assert row["action"] == "买入"


def test_new_open_3000_is_blocked_by_the_3800_threshold() -> None:
    """② 新开仓 3,000 元 ⇒ **被拦**：delta 归零、动作改「不建仓」、金额费用归零。"""
    row = _new_open_row(code="512800", price=1.0, shares=3000)
    assert row["est_amount"] == pytest.approx(3000.0)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["skipped_count"] == 1
    skipped = disclosure["skipped"][0]
    assert skipped["code"] == "512800"
    assert skipped["kind"] == "新开仓"
    assert skipped["amount"] == pytest.approx(3000.0)
    assert skipped["threshold"] == pytest.approx(3800.0)

    # 行被就地作废（但**不删行**）
    assert row["delta_shares"] == 0
    assert row["target_shares"] == 0
    assert row["est_amount"] == 0.0
    assert row["est_fee"] == 0.0
    assert row["action"] == "不建仓"
    assert row["min_amount_skipped"] is True
    assert row["min_amount_kind"] == "新开仓"
    assert row["min_amount_threshold"] == pytest.approx(3800.0)
    assert row["min_amount_skipped_amount"] == pytest.approx(3000.0)

    # reason 必须说清：金额 / 档位 / 门槛 / 折合 bp
    reason = str(row["reason"])
    assert "3000" in _flat(reason), f"reason 必须含被拦金额，实得 {reason!r}"
    assert "新开仓" in reason, f"reason 必须含档位，实得 {reason!r}"
    assert "3800" in _flat(reason), f"reason 必须含门槛，实得 {reason!r}"
    assert "16.7" in reason and "bp" in reason, f"reason 必须含折合 bp，实得 {reason!r}"


# =============================================================================
# ③ 存量微调：2,800 元门槛（含真实案例）
# =============================================================================
def test_adjust_real_case_512800_2044_is_blocked_by_the_2800_threshold() -> None:
    """③ 真实案例：512800 减持 2,044.70 元 ⇒ 低于 2,800 ⇒ **被拦**。

    🔴 门槛判定只认计划件里的 `est_amount`（权威口径），**不重算** `delta_shares × price`
    ⇒ 断言直接钉住「判定金额 = 2,044.70」。

    🔴 数字取自**真机产物**（asof=2026-11-12 的计划件 `final_rebalance_plan.json`）：
       512800 持有 **7,700** 股 @ **0.889** ⇒ 目标 5,400 股 ⇒ 减持 2,300 股
       ⇒ 金额 2,300 × 0.889 = **2,044.70 元**（= 用户文档里点名的那个案例）。
    """
    row = _base_row(
        code="512800",
        name="银行ETF",
        current_shares=7700,
        price=0.889,
        target_weight=0.16,
        target_amount=5400 * 0.889,
        target_shares=5400,
        delta_shares=-2300,
        est_amount=-2044.70,
        est_fee=0.0,
        action="卖出",
        reason="跌破 MA60 减仓",
        ta_rejected=False,
    )
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["skipped_count"] == 1
    skipped = disclosure["skipped"][0]
    assert skipped["kind"] == "存量微调"
    assert skipped["amount"] == pytest.approx(2044.70)
    assert skipped["threshold"] == pytest.approx(2800.0)
    assert skipped["effective_bp"] == round(5.0 / 2044.70 * 1e4, 1)

    assert row["delta_shares"] == 0
    assert row["target_shares"] == 7700, "被拦 ⇒ 目标股数回退到**当前持仓**"
    assert row["est_amount"] == 0.0
    assert row["est_fee"] == 0.0
    assert row["action"] == "持有"
    assert row["min_amount_kind"] == "存量微调"
    assert row["min_amount_skipped_amount"] == pytest.approx(2044.70)
    assert row["target_amount"] == pytest.approx(7700 * 0.889)

    reason = str(row["reason"])
    #: reason 里的金额按**整元**渲染（生产口径 `f"{amount:.0f}"`）⇒ 2,044.70 ⇒ "2045"
    assert f"{2044.70:.0f}" in _flat(reason), f"reason 必须含被拦金额，实得 {reason!r}"
    assert "存量微调" in reason, f"reason 必须含档位，实得 {reason!r}"
    assert "2800" in _flat(reason), f"reason 必须含门槛，实得 {reason!r}"


def test_adjust_3000_meets_the_2800_threshold_and_passes() -> None:
    """③ 存量微调 3,000 元 ⇒ 达标**放行**（同一档里 2,044 拦、3,000 放）。"""
    row = _adjust_row(code="512800", price=0.88, current_shares=6700, target_shares=3291)
    row["est_amount"] = -3000.0
    snapshot = dict(row)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["considered_actions"] == 1
    assert disclosure["skipped_count"] == 0
    assert row == snapshot, "达标的行不得被就地改写"
    assert row["delta_shares"] == -3409
    assert row["est_amount"] == pytest.approx(-3000.0)


# =============================================================================
# ④ 死锁检测
# =============================================================================
def test_deadlock_risk_is_true_when_every_new_open_is_blocked() -> None:
    """④ 一轮里**只有**新开仓且金额都 < 3,800 ⇒ **数学死锁** ⇒ 显式报警。"""
    plans = [
        _new_open_row(code="512800", price=1.0, shares=3000),
        _new_open_row(code="513100", price=1.0, shares=2000),
    ]
    assert all(p["est_amount"] < 3800.0 for p in plans)

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["new_open_total"] == 2
    assert disclosure["new_open_skipped"] == 2
    assert disclosure["new_open_total"] == disclosure["new_open_skipped"]
    assert disclosure["deadlock_risk"] is True
    assert disclosure["skipped_count"] == 2
    assert any("死锁" in note for note in disclosure["notes"]), "披露里必须写明死锁"


def test_deadlock_risk_is_false_when_a_new_open_meets_the_threshold() -> None:
    """④ 换成一笔达标的新开仓 ⇒ **不再死锁**（`deadlock_risk is False`）。"""
    plans = [_new_open_row(code="512800", price=0.961, shares=5000)]
    assert plans[0]["est_amount"] == pytest.approx(4805.0)

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["new_open_total"] == 1
    assert disclosure["new_open_skipped"] == 0
    assert disclosure["deadlock_risk"] is False
    assert disclosure["skipped_count"] == 0
    assert not any("死锁" in note for note in disclosure["notes"])


# =============================================================================
# ⑤ 被拦行仍在 actions 里，且现金流不受影响
# =============================================================================
def test_blocked_rows_stay_in_actions_and_cash_flow_is_untouched() -> None:
    """⑤ 被拦行**不删** ⇒ 仍在 actions 里；金额/费用归零 ⇒ 现金流不受影响。"""
    blocked_new = _new_open_row(code="512800", price=1.0, shares=3000)
    blocked_adj = _adjust_row(code="513100", price=0.88, current_shares=6700, target_shares=4378)
    blocked_adj["est_amount"] = -2044.16
    plans = [blocked_new, blocked_adj]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    assert disclosure["skipped_count"] == 2
    assert len(plans) == 2, "被拦的行**不许删**（删了 ⇒ 市值凭空蒸发 ⇒ 交付件自相矛盾）"
    assert [p["code"] for p in plans] == ["512800", "513100"]
    assert {p["min_amount_kind"] for p in plans} == {"新开仓", "存量微调"}

    for row in plans:
        assert row["est_amount"] == 0.0, "被拦 ⇒ 成交额归零"
        assert row["est_fee"] == 0.0, "被拦 ⇒ 费用归零"

    # 现金流：既无卖出回款，也无买入支出
    cash_in = sum(abs(p["est_amount"]) for p in plans if p["est_amount"] < 0)
    cash_out = sum(abs(p["est_amount"]) for p in plans if p["est_amount"] > 0)
    assert cash_in == 0.0
    assert cash_out == 0.0
    assert sum(p["est_amount"] for p in plans) == 0.0

    # 目标权重自洽：effective = 当前市值 / 总资产，差额记进 weight_to_cash
    for row in plans:
        expected = row["current_shares"] * row["price"] / TOTAL_ASSETS
        assert row["target_weight_effective"] == pytest.approx(expected)
        # 🔴 2026-11-13 夜实测修：差额是**有符号**的（target_weight − effective），
        #    **不许截断成非负** —— 旧口径 `max(0.0, ...)` 把「超配没减仓」的**负**差额抹成 0
        #    ⇒ Σ`weight_to_cash` ≠ 席位缺口 ⇒「Σ有效权重 + Σ权重归现金 = 1」恒等式崩
        #    ⇒ 计划件自检 FATAL ⇒ 流水线 exit 1（产物被判「不可交付」）。
        assert row["weight_to_cash"] == pytest.approx(
            row["target_weight"] - row["target_weight_effective"]
        )
    assert plans[0]["target_weight_effective"] == pytest.approx(0.0)
    assert plans[0]["weight_to_cash"] == pytest.approx(plans[0]["target_weight"])
    assert plans[1]["target_weight_effective"] == pytest.approx(6700 * 0.88 / TOTAL_ASSETS)
    # 🔴 「超配没减仓」（被拦的减仓 ⇒ 仍持原股数 ⇒ 实际权重高于目标权重）
    #    ⇒ 差额为**负** ⇒ 现金相应比目标少 ⇒ 不许被抹成 0。
    assert plans[1]["weight_to_cash"] < 0.0
    assert plans[1]["weight_to_cash"] == pytest.approx(
        plans[1]["target_weight"] - plans[1]["target_weight_effective"]
    )


# =============================================================================
# ⑥ 分类函数：三分支 + delta == 0
# =============================================================================
def test_classify_min_amount_kind_covers_three_branches_and_zero_delta() -> None:
    """⑥ 分类只看三个事实（当前股数 / 目标股数 / 变动方向），**不看金额**。"""
    assert gate_mod.MIN_AMOUNT_KINDS == ("清仓止损", "新开仓", "存量微调")

    liquidate = {"current_shares": 500, "target_shares": 0, "delta_shares": -500}
    new_open = {"current_shares": 0, "target_shares": 3000, "delta_shares": 3000}
    adjust = {"current_shares": 6700, "target_shares": 4378, "delta_shares": -2322}
    no_trade = {"current_shares": 100, "target_shares": 100, "delta_shares": 0}

    assert gate_mod.classify_min_amount_kind(liquidate) == "清仓止损"
    assert gate_mod.classify_min_amount_kind(new_open) == "新开仓"
    assert gate_mod.classify_min_amount_kind(adjust) == "存量微调"
    assert gate_mod.classify_min_amount_kind(no_trade) == ""

    # 金额不影响分类（门槛判定在分类之后）
    assert gate_mod.classify_min_amount_kind({**adjust, "est_amount": 0.01}) == "存量微调"
    assert gate_mod.classify_min_amount_kind({**new_open, "est_amount": 99999.0}) == "新开仓"


# =============================================================================
# ⑦ 开关与 CLI 覆盖
# =============================================================================
def test_gate_disabled_blocks_nothing() -> None:
    """⑦ `enabled=False` ⇒ **一行都不拦**（对比 / 回退用）。"""
    row = _new_open_row(code="512800", price=1.0, shares=3000)
    snapshot = dict(row)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(
        plans, total_assets=TOTAL_ASSETS, enabled=False
    )

    assert disclosure["enabled"] is False
    assert disclosure["skipped_count"] == 0
    assert disclosure["skipped"] == []
    assert disclosure["considered_actions"] == 0
    assert row == snapshot, "门槛关掉时行不得被就地改写"


def test_threshold_override_lets_a_3000_new_open_pass() -> None:
    """⑦ 阈值覆盖 `{"新开仓": 1000.0}` ⇒ 3,000 元的新开仓**放行**。"""
    row = _new_open_row(code="512800", price=1.0, shares=3000)
    snapshot = dict(row)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(
        plans, total_assets=TOTAL_ASSETS, thresholds={"新开仓": 1000.0}
    )

    assert disclosure["thresholds"]["新开仓"] == pytest.approx(1000.0)
    assert disclosure["skipped_count"] == 0
    assert row == snapshot
    # 没被覆盖的两档仍走 config 默认
    assert disclosure["thresholds"]["清仓止损"] == pytest.approx(C.MIN_TRADE_AMOUNT_LIQUIDATE)
    assert disclosure["thresholds"]["存量微调"] == pytest.approx(C.MIN_TRADE_AMOUNT_ADJUST)


def test_gate_enabled_honours_the_cli_switch() -> None:
    """⑦ `gate_enabled(args)` 认 `--no-min-amount-gate`。"""
    assert gate_mod.gate_enabled() is True
    assert gate_mod.gate_enabled(None) is True
    assert gate_mod.gate_enabled(argparse.Namespace(no_min_amount_gate=True)) is False
    assert gate_mod.gate_enabled(argparse.Namespace(no_min_amount_gate=False)) is True


def test_thresholds_from_args_reads_the_three_cli_overrides() -> None:
    """⑦ `thresholds_from_args` 读三个 CLI 覆盖项（没给 ⇒ None）。"""
    assert gate_mod.thresholds_from_args(None) is None
    assert gate_mod.thresholds_from_args(argparse.Namespace()) is None

    args = argparse.Namespace(
        min_amount_liquidate=0.0, min_amount_new=1000.0, min_amount_adjust=500.0
    )
    overrides = gate_mod.thresholds_from_args(args)
    assert overrides == {"清仓止损": 0.0, "新开仓": 1000.0, "存量微调": 500.0}
    assert gate_mod.resolve_thresholds(overrides) == {
        "清仓止损": 0.0,
        "新开仓": 1000.0,
        "存量微调": 500.0,
    }


def test_resolve_thresholds_defaults_to_the_config_caliber() -> None:
    """⑦ 默认三档门槛 = config 口径（单一事实源，不许在脚本里另写一份）。"""
    assert gate_mod.C is C, "模块与测试必须读**同一份** config"
    assert C.MIN_TRADE_AMOUNT_LIQUIDATE == 0.0
    assert C.MIN_TRADE_AMOUNT_NEW == 3800.0
    assert C.MIN_TRADE_AMOUNT_ADJUST == 2800.0
    assert C.MIN_AMOUNT_GATE_ENABLED is True
    assert C.ETF_MIN_COMMISSION_STRICT == 5.0
    assert gate_mod.resolve_thresholds() == {
        "清仓止损": 0.0,
        "新开仓": 3800.0,
        "存量微调": 2800.0,
    }


# =============================================================================
# ⑧ 折合 bp 与省下的最低佣金
# =============================================================================
def test_effective_bp_and_saved_min_commission_for_a_3000_block() -> None:
    """⑧ 3,000 元 ⇒ 折合 **16.7 bp**（= 5 / 3,000 × 1e4），省下最低佣金 **5.0 元**。"""
    row = _new_open_row(code="512800", price=1.0, shares=3000)
    plans = [row]

    disclosure = gate_mod.apply_min_trade_threshold(plans, total_assets=TOTAL_ASSETS)

    skipped = disclosure["skipped"][0]
    assert skipped["effective_bp"] == round(5.0 / 3000 * 1e4, 1)
    assert skipped["effective_bp"] == 16.7
    assert skipped["min_commission_saved"] == 5.0
    assert disclosure["min_commission_strict"] == 5.0
    assert disclosure["min_commission_saved"] == 5.0


# =============================================================================
# ⑨ 同一行被作废两次 ⇒ 披露句**只出现一次**（2026-11-16 交付件复核发现的回归）
# =============================================================================
def test_block_reason_is_idempotent_when_same_row_is_blocked_twice() -> None:
    """⑨ 阶段 5（信号日价）与阶段 6（节点日价）**可能作废同一行**。

    旧实现无条件 `row["reason"] += suffix` ⇒ 两次金额口径一致时
    **同一句披露在交付件表格里出现两遍**（读起来像重复报错）。
    ⇒ 修法：幂等追加（句子里已有这句就不再追加）；**只影响文案**，
    金额/费用归零、现金自洽、门槛计数本来就不受影响。
    """
    row = _new_open_row(code="515450", price=1.466, shares=1000)

    first = gate_mod.block_row(
        row,
        kind="新开仓",
        amount=1466.0,
        threshold=3800.0,
        commission_rate=0.00015,
        min_commission=5.0,
        total_assets=TOTAL_ASSETS,
    )
    second = gate_mod.block_row(
        row,
        kind="新开仓",
        amount=1466.0,
        threshold=3800.0,
        commission_rate=0.00015,
        min_commission=5.0,
        total_assets=TOTAL_ASSETS,
    )

    reason = str(row.get("reason") or "")
    assert reason.count("门槛 3800") == 1, "披露句只许出现一次"
    assert reason.count("本轮不调仓") == 1
    assert first["already_disclosed"] is False
    assert second["already_disclosed"] is True
    #: 作废语义不受影响（第二次调用仍必须把行归零）
    assert row["delta_shares"] == 0
    assert row["est_amount"] == 0.0
    assert row["est_fee"] == 0.0
    assert row["action"] == "不建仓"
