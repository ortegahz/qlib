"""🔴 2026-11-17：执行卡**不得改写**研究链股数决策的钉死测试。

【事故（真机交付件 `execution_card_20261116.json` / `report_20261116.md`）】
研究链（`final_rebalance_plan` / `node_reprice`）对 `512800` 的结论是：

* 入选本期组合，目标权重 16%，**目标股数 7700 股**；
* 存量微调 2292.5 元 < 2800 元门槛 ⇒ **本轮不调仓**
  （`action="持有"`、`delta_shares=0`、披露在 `min_amount_gate.skipped`）。

执行卡却按 `target_amount`（7060.90 元 = 7700 股 × 0.917）**重新推导**股数，
再无脑套「单只金额上限 6007.11 元（总资产 × 20%）」⇒ 目标被砍到 6500 股
⇒ **凭空生成 1200 股 / 1100.40 元的卖单**
（该笔金额本身也 < 2800 元存量微调门槛 ⇒ 双重违规），
⇒ 执行卡与计划件口径不一致（期末现金 19684.88 vs 计划件 18593.07）。

【钉死的纪律】
* 动作行里有**明确的 `target_shares`** ⇒ 执行卡以它为准（只做整手取整，不得改写）；
* 「单只金额上限」**只约束建仓侧** ⇒ 执行层永远不凭空下减仓单；
* 减仓行（目标股数 < 当前股数）照研究链执行，不被上限改写；
* 封顶后的目标**不得低于现仓** ⇒ 最多「本轮不建仓」，绝不反向变成减仓；
* 观察态（`live_trade=False`）依旧带挂单禁用标记（不许因为修 bug 破坏既有闸门）。
"""

from __future__ import annotations

import sys
from pathlib import Path

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import agents_rebalance  # noqa: E402

#: 真机口径（2026-11-16 信号日）：账户总资产 / 现金 / 持仓
ACCOUNT = {
    "total_assets": 30035.57,
    "cash": 23367.37,
    "market_value": 6668.20,
    "cash_weight": 0.778,
    "positions": [
        {"code": "512800", "name": "银行ETF华宝", "shares": 7700, "market_value": 6668.20},
    ],
}

#: 单只金额上限 = 总资产 × 20%（与真机一致 ⇒ 会触发旧缺陷）
MAX_AMOUNT = 6007.11
LOT = 100


def _hold_row() -> dict:
    """研究链对 `512800` 的结论：**维持 7700 股**（存量微调被门槛拦下 ⇒ 本轮不调仓）。"""
    return {
        "code": "512800", "name": "银行ETF华宝", "action": "持有",
        "current_shares": 7700, "price": 0.917, "price_date": "2026-11-16",
        "target_shares": 7700, "delta_shares": 0,
        "target_weight": 0.16, "target_amount": 7060.90,
        "reason": "存量微调 2292.5 元 < 2800 元门槛 ⇒ 本轮不调仓",
    }


def _buy_row() -> dict:
    """研究链对 `515450` 的结论：**买入 3200 股 @ 1.491**。"""
    return {
        "code": "515450", "name": "红利低波50ETF南方", "action": "买入",
        "current_shares": 0, "price": 1.491, "price_date": "2026-11-16",
        "target_shares": 3200, "delta_shares": 3200,
        "target_weight": 0.16, "target_amount": 4805.6912,
        "reason": "入选本期组合",
    }


def _card(actions: list[dict], **kwargs) -> dict:
    params = {
        "actions": actions,
        "account": ACCOUNT,
        "asof": "2026-11-16",
        "execution_date": "2026-11-17",
        "max_amount": MAX_AMOUNT,
        "lot_size": LOT,
        "cash_buffer_min": 400.0,
        "cash_buffer_max": 400.0,
        "live_trade": False,
        "node_reprice_ok": True,
        "node_reprice_error": "",
    }
    params.update(kwargs)
    return agents_rebalance.build_execution_card(**params)


def _order(card: dict, code: str) -> dict:
    for item in card.get("orders") or []:
        if str(item.get("code")) == code:
            return item
    raise AssertionError(f"卡上没有 {code} 的订单行")


def test_hold_row_is_not_turned_into_a_sell() -> None:
    """研究链说「维持」⇒ 卡上**不得**出现减仓单（旧口径会砍到 6500 股 ⇒ 卖 1200 股）。"""
    card = _card([_hold_row(), _buy_row()])
    order = _order(card, "512800")
    assert order["delta_shares"] == 0, f"512800 被凭空改成减仓：{order}"
    assert order["target_shares"] == 7700
    assert order["action"] == "持有"
    assert order["est_amount"] == 0.0
    assert order["capped"] is False


def test_no_negative_delta_against_the_chain() -> None:
    """**全卡**不得出现研究链没要求的减仓单（delta < 0）。"""
    card = _card([_hold_row(), _buy_row()])
    for order in card.get("orders") or []:
        assert (order.get("delta_shares") or 0) >= 0, f"凭空生成了减仓单：{order}"


def test_cash_flow_has_no_sell_side() -> None:
    """卖出侧必须为空 ⇒ 期末现金 = 期初现金 − 买入 − 费用 − 滑点。"""
    card = _card([_hold_row(), _buy_row()])
    flow = card["cash_flow"]
    assert flow["sell_proceeds"] == 0.0
    assert flow["sell_fees"] == 0.0
    assert flow["buy_amount"] == 4771.20
    assert flow["cash_end"] == 23367.37 - 4771.20 - flow["buy_fees"] - flow["slippage"]
    # 与计划件口径（18593.07）同量级（差额 = 执行层的 5 元最低佣金 + 滑点）
    assert 18585.0 < flow["cash_end"] < 18600.0


def test_explicit_target_wins_over_amount_cap() -> None:
    """有明确 `target_shares` ⇒ 上限**不得**改写它（即使目标金额 > 上限）。"""
    card = _card([_hold_row(), _buy_row()])
    order = _order(card, "512800")
    assert order["target_amount"] == 7700 * 0.917
    assert order["target_amount"] > MAX_AMOUNT  # 夹具前提：目标金额确实超上限


def test_build_side_is_still_capped() -> None:
    """**建仓侧**上限仍然生效（回归保护：不能为了修 bug 把上限放飞）。"""
    row = {
        "code": "510880", "name": "红利ETF", "action": "买入",
        "current_shares": 0, "price": 1.0, "price_date": "2026-11-16",
        "target_shares": 9000, "delta_shares": 9000,
        "target_amount": 9000.0,
        "reason": "入选本期组合",
    }
    card = _card([row])
    order = _order(card, "510880")
    assert order["capped"] is True
    assert order["target_shares"] * order["price"] <= MAX_AMOUNT + 1e-9
    assert (order.get("delta_shares") or 0) > 0, "建仓被封顶后**不得**反向变成减仓"


def test_cap_never_forces_a_sell_on_existing_holding() -> None:
    """现仓已超上限 ⇒ 封顶后**维持现仓**（delta = 0），绝不生成减仓单。"""
    row = {
        "code": "512800", "name": "银行ETF华宝", "action": "买入",
        "current_shares": 7000, "price": 1.0, "price_date": "2026-11-16",
        "target_shares": 9000, "delta_shares": 2000,
        "target_amount": 9000.0,
        "reason": "夹具",
    }
    card = _card([row])
    order = _order(card, "512800")
    assert order["capped"] is True
    assert (order.get("delta_shares") or 0) == 0, f"封顶不应变成减仓：{order}"
    assert order["target_shares"] == 7000


def test_sell_row_is_honored() -> None:
    """研究链真的要减仓 ⇒ 照单执行（不被上限改写、不被吞掉）。"""
    row = {
        "code": "512800", "name": "银行ETF华宝", "action": "卖出",
        "current_shares": 7700, "price": 0.917, "price_date": "2026-11-16",
        "target_shares": 5500, "delta_shares": -2200,
        "target_amount": 5043.50,
        "reason": "夹具：TA 要求减仓",
    }
    card = _card([row])
    order = _order(card, "512800")
    assert order["delta_shares"] == -2200
    assert order["target_shares"] == 5500
    assert order["action"] == "卖出"


def test_observation_card_still_disabled() -> None:
    """修 bug 不许破坏「观察态挂单禁用」闸门。"""
    card = _card([_hold_row(), _buy_row()])
    assert card.get("order_enabled") is False
    assert card.get("disabled") is True
