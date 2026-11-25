"""🔴 2026-11-25：「被最低佣金门槛作废的新开仓」⇒ 归「**不可建仓**」（碎屑归现金）。

【根因】执行卡的 ``build_execution_card`` **从不读**研究链写在动作行上的 ``action``
（``min_trade_gate.py`` 已正确标「不建仓」）⇒ 只按 ``delta_shares`` 把这一行重写成
「持有」⇒ 被门槛作废的席位被印进《无动作（维持）》表
⇒ 读起来像「维持原仓」，实际是「本轮根本买不进」⇒ **口径说谎**。

【钉死的纪律】
1. 命中「**0 股 + 门槛作废 + 新开仓**」⇒ **不进 ``orders``**、
   进 ``cash_flow.unbuildable_slots``、``action="不可建仓"``；
2. **披露不许丢**：门槛阈值 / 被作废金额 / 门槛档位 / 原有 ``lot_note`` 都得在；
3. **现金 / 权益零变化**：被作废席位的预算**留现金**（不许凭空消失，也不许被买）；
4. ``current_shares > 0`` 的「持有」行（门槛作废但**有持仓** ⇒ 维持）
   **不得**被误分类成「不可建仓」。
"""

from __future__ import annotations

import sys
from pathlib import Path

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import agents_rebalance as AR  # noqa: E402

_ACCOUNT = {"total_assets": 30035.57, "cash": 23367.37}


def _card(actions: list[dict]) -> dict:
    return AR.build_execution_card(
        actions=actions, account=_ACCOUNT, asof="2026-11-13", execution_date="2026-12-01",
        live_trade=True,
    )


def _held_row() -> dict:
    """一只**真持仓**的「维持」行（口径取自 2026-11-13 真机交付件）。"""
    return {
        "code": "512800", "name": "银行ETF华宝", "current_shares": 7700,
        "price": 0.8970, "price_date": "2026-11-13",
        "target_shares": 7700, "delta_shares": 0, "target_amount": 6906.90,
        "est_amount": 0.0, "est_fee": 0.0, "action": "持有",
        "reason": "维持",
    }


def _skipped_new_open() -> dict:
    """研究链（``min_trade_gate.py``）**作废后**的「新开仓」行动行。

    🔴 作废会把 ``target_shares`` 回退到 ``current_shares``（都是 0）、
    ``delta_shares`` 归零、``est_amount`` 归零、``action`` 标成「不建仓」，
    并留下四个披露字段（阈值 / 被作废金额 / 档位 / 折合 bp 在 ``reason`` 里）。
    """
    return {
        "code": "515450", "name": "红利低波50ETF南方", "current_shares": 0,
        "price": 1.4610, "price_date": "2026-11-13",
        "target_shares": 0, "delta_shares": 0, "est_amount": 0.0, "est_fee": 0.0,
        "target_amount": 0.0, "target_weight": 0.195,
        "action": "不建仓",
        "min_amount_skipped": True, "min_amount_kind": "新开仓",
        "min_amount_threshold": 3800.0, "min_amount_skipped_amount": 584.40,
        "lot_note": "",
        "reason": (
            "入选本期组合；🔴 **金额 584 元 < 新开仓门槛 3800 元**"
            "（按 5 元最低佣金折合 87.6 bp） ⇒ **本轮不调仓**"
        ),
    }


def _skipped_adjust() -> dict:
    """门槛作废但**有持仓**的行（存量微调）⇒ 属于「维持」，**不是**「不可建仓」。"""
    return {
        "code": "512800", "name": "银行ETF华宝", "current_shares": 7700,
        "price": 0.8970, "price_date": "2026-11-13",
        "target_shares": 7700, "delta_shares": 0, "est_amount": 0.0, "est_fee": 0.0,
        "target_amount": 6906.90, "action": "持有",
        "min_amount_skipped": True, "min_amount_kind": "存量微调",
        "min_amount_threshold": 2800.0, "min_amount_skipped_amount": 2292.50,
        "lot_note": "",
        "reason": "🔴 金额 2292 元 < 存量微调门槛 2800 元 ⇒ 本轮不调仓",
    }


def test_门槛作废的新开仓归不可建仓() -> None:
    """0 股 + 门槛作废 + 新开仓 ⇒ **不可建仓**、**不进下单清单**。"""
    card = _card([_held_row(), _skipped_new_open()])
    slots = card["cash_flow"]["unbuildable_slots"]
    assert slots, "被门槛作废的新开仓必须计入 unbuildable_slots"
    slot = slots[0]
    assert slot["code"] == "515450"
    assert slot["action"] == "不可建仓"
    assert slot["target_shares"] == 0
    assert "515450" not in [o["code"] for o in card["orders"]], card["orders"]
    assert "515450" not in [o["code"] for o in card["orders"] if o["action"] == "持有"]


def test_披露不许丢() -> None:
    """门槛阈值 / 被作废金额 / 门槛档位 / 原有 ``lot_note`` 一个都不许丢。"""
    row = _skipped_new_open()
    row["lot_note"] = "目标金额 584 元 < 一手(100 份 × 1.461 = 146 元) ⇒ 整手口径下本轮无法按目标权重建仓"
    card = _card([_held_row(), row])
    slot = card["cash_flow"]["unbuildable_slots"][0]
    assert slot["min_amount_threshold"] == 3800.0
    assert slot["min_amount_skipped_amount"] == 584.40
    assert slot["min_amount_kind"] == "新开仓"
    assert slot["lot_note"] == row["lot_note"]
    #: 披露还得**渲染得出来**（报告的「不可建仓」表只印 `reason` 这一列）
    assert "3800" in slot["reason"], slot["reason"]
    assert "584.40" in slot["reason"], slot["reason"]
    assert "新开仓" in slot["reason"], slot["reason"]


def test_有持仓的门槛作废行仍是维持() -> None:
    """🔴 门槛作废但**有持仓** ⇒ 属于「维持」，**不得**被误分类成「不可建仓」。"""
    card = _card([_skipped_adjust()])
    assert not card["cash_flow"]["unbuildable_slots"], card["cash_flow"]["unbuildable_slots"]
    order = card["orders"][0]
    assert order["code"] == "512800"
    assert order["action"] == "持有"
    assert order["current_shares"] == 7700
    assert order["delta_shares"] == 0


def test_现金与权益零变化() -> None:
    """被作废席位的预算**留现金** ⇒ 现金 / 权益 / 权重合计与「没有这一行」时**完全一致**。"""
    base = _card([_held_row()])
    with_skip = _card([_held_row(), _skipped_new_open()])
    assert with_skip["cash_flow"]["cash_end"] == base["cash_flow"]["cash_end"]
    assert with_skip["cash_flow"]["buy_amount"] == base["cash_flow"]["buy_amount"]
    assert with_skip["post_trade"]["equity_amount"] == base["post_trade"]["equity_amount"]
    assert with_skip["post_trade"]["equity_weight"] == base["post_trade"]["equity_weight"]
    assert with_skip["post_trade"]["cash"] == base["post_trade"]["cash"]
