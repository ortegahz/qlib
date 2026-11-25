"""🔴 2026-11-25：**研究链自判「整手买不起」** ⇒ 归「**不可建仓**」（碎屑归现金）。

【根因】计划件（``manu/etf_rotation/final_rebalance_plan.py``）对「目标金额 < 一手金额」
的席位**自己**判定：``action="不可建仓"`` / ``target_shares=0`` / 权重归现金 /
``lot_note`` 写明原因（并在计划件自己的 ``unbuildable`` 列表里单列）。
但执行卡的 ``build_execution_card`` **从不读** ``act["action"]`` ⇒ 只按 ``delta_shares``
把这一行重写成「持有」⇒ 该行被印进《**无动作（维持）**》表
⇒ 读起来像「维持原仓」，实际是「本轮**整手都买不起**」⇒ **口径说谎**
（与已修的「最低佣金门槛作废」那一条同一类缺陷）。

【钉死的纪律】
1. 命中「**计划件标签=不可建仓 + 0 股 + 目标股数=0 + 有 lot_note**」⇒ **不进 ``orders``**、
   进 ``cash_flow.unbuildable_slots``、``action="不可建仓"``；
2. **披露不许丢**：``lot_note`` / ``one_lot_amount`` / ``max_amount`` 都在，
   ``reason`` 里也带 ``lot_note`` 原文；
3. **现金 / 权益 / 目标权重零变化**：被归位的席位**留现金**（不许凭空消失，也不许被买）；
4. ``current_shares > 0`` 的真实持仓行**不得**被误分类（否则持仓会从卡上消失）；
5. **回归**：分支 A（单只金额上限）与分支 B（最低佣金门槛）**仍各走各的路径**，互不覆盖。
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


def _lot_unbuildable() -> dict:
    """计划件**自己**判定的「整手买不起」行（``final_rebalance_plan.py`` 的 ``lot_note`` 口径）。

    🔴 形态：目标金额 1,521 元 < 一手（100 份 × 20.00 = 2,000 元）⇒ 整手口径下
       **本轮连一手都买不起** ⇒ 计划件把 ``action`` 改成「不可建仓」、
       ``target_shares`` 改成 0、权重归现金，并留下 ``lot_note``。
    🔴 一手金额（2,000 元）**未超**单只金额上限 ⇒ 分支 A 不命中 ⇒ 只可能由分支 C 归位。
    """
    return {
        "code": "159001", "name": "某沪深300ETF", "current_shares": 0,
        "price": 20.00, "price_date": "2026-11-13",
        "target_shares": 0, "delta_shares": 0, "target_amount": 1521.00,
        "est_amount": 0.0, "est_fee": 0.0, "target_weight": 0.05,
        "action": "不可建仓",
        "lot_note": (
            "目标金额 1521 元 < 一手(100 份 × 20.000 = 2000 元)"
            " ⇒ 整手口径下本轮无法按目标权重建仓"
        ),
        "reason": "入选本期组合（目标权重 5%）",
    }


def _lot_unbuildable_but_held() -> dict:
    """同类标签但**有真实持仓**的行 ⇒ **不得**被误分类（否则持仓会从卡上消失）。"""
    row = _lot_unbuildable()
    row.update({
        "current_shares": 500, "target_shares": 500, "target_amount": 10000.00,
        "delta_shares": 0, "action": "不可建仓",
    })
    return row


def _one_lot_over_cap() -> dict:
    """分支 A：一手金额 > **单只金额上限** ⇒ 「不可建仓」（碎屑归现金）。"""
    return {
        "code": "511660", "name": "货币ETF", "current_shares": 0,
        "price": 100.00, "price_date": "2026-11-13",
        "target_shares": 0, "delta_shares": 0, "target_amount": 5000.00,
        "est_amount": 0.0, "est_fee": 0.0,
        "action": "买入", "lot_note": "",
        "reason": "入选本期组合",
    }


def _min_amount_skipped_new_open() -> dict:
    """分支 B：被**最低佣金门槛**作废的新开仓（0 股）⇒ 「不可建仓」。"""
    return {
        "code": "515450", "name": "红利低波50ETF南方", "current_shares": 0,
        "price": 1.4610, "price_date": "2026-11-13",
        "target_shares": 0, "delta_shares": 0, "est_amount": 0.0, "est_fee": 0.0,
        "target_amount": 0.0, "target_weight": 0.195,
        "action": "不建仓",
        "min_amount_skipped": True, "min_amount_kind": "新开仓",
        "min_amount_threshold": 3800.0, "min_amount_skipped_amount": 584.40,
        "lot_note": "",
        "reason": "入选本期组合；🔴 金额 584 元 < 新开仓门槛 3800 元 ⇒ 本轮不调仓",
    }


def test_整手买不起归不可建仓() -> None:
    """计划件标签=不可建仓 + 0 股 + 目标股数=0 + 有 lot_note ⇒ 归「不可建仓」、**不进下单清单**。"""
    card = _card([_held_row(), _lot_unbuildable()])
    slots = card["cash_flow"]["unbuildable_slots"]
    assert slots, "计划件自判「整手买不起」的行必须计入 unbuildable_slots"
    slot = slots[0]
    assert slot["code"] == "159001"
    assert slot["action"] == "不可建仓"
    assert slot["target_shares"] == 0
    assert slot["current_shares"] == 0
    assert "159001" not in [o["code"] for o in card["orders"]], card["orders"]


def test_现金与权益零变化() -> None:
    """被归位的席位**留现金** ⇒ 现金 / 权益 / 目标权重与「没有这一行」时**完全一致**。"""
    base = _card([_held_row()])
    with_row = _card([_held_row(), _lot_unbuildable()])
    assert with_row["cash_flow"]["cash_end"] == base["cash_flow"]["cash_end"]
    assert with_row["cash_flow"]["buy_amount"] == base["cash_flow"]["buy_amount"]
    assert with_row["cash_flow"]["sell_proceeds"] == base["cash_flow"]["sell_proceeds"]
    assert with_row["post_trade"]["equity_amount"] == base["post_trade"]["equity_amount"]
    assert with_row["post_trade"]["equity_weight"] == base["post_trade"]["equity_weight"]
    assert with_row["post_trade"]["cash"] == base["post_trade"]["cash"]
    #: 目标权重零变化 ⇒ 两边的**有仓位**行必须一模一样（该席位是 0 股 ⇒ 不占仓位）
    _held = lambda card: sorted(  # noqa: E731
        (p["code"], p["shares"], p["market_value"]) for p in card["post_trade"]["positions"]
        if (p["shares"] or 0) > 0
    )
    assert _held(with_row) == _held(base)


def test_有持仓的同类行不被误分类() -> None:
    """🔴 ``current_shares > 0`` 的真实持仓行 ⇒ **不得**挪进 unbuildable（不许从卡上消失）。"""
    card = _card([_lot_unbuildable_but_held()])
    assert not card["cash_flow"]["unbuildable_slots"], card["cash_flow"]["unbuildable_slots"]
    order = card["orders"][0]
    assert order["code"] == "159001"
    assert order["action"] == "持有"
    assert order["current_shares"] == 500
    assert order["delta_shares"] == 0


def test_披露不许丢() -> None:
    """``lot_note`` / ``one_lot_amount`` / ``max_amount`` 一个都不许丢，``reason`` 也要带原文。"""
    row = _lot_unbuildable()
    card = _card([_held_row(), row])
    slot = card["cash_flow"]["unbuildable_slots"][0]
    assert slot["lot_note"] == row["lot_note"]
    assert slot["one_lot_amount"] == 2000.0
    assert slot["max_amount"] is not None
    assert row["lot_note"] in slot["reason"]
    assert "买不进" in slot["reason"]


def test_分支A与分支B仍各走各的路径() -> None:
    """🔴 回归：分支 A（单只金额上限）/ 分支 B（最低佣金门槛）/ 分支 C 互不覆盖。"""
    card = _card([_held_row(), _one_lot_over_cap(), _min_amount_skipped_new_open(), _lot_unbuildable()])
    slots = card["cash_flow"]["unbuildable_slots"]
    by_code = {s["code"]: s for s in slots}
    assert set(by_code) == {"511660", "515450", "159001"}, by_code
    #: 分支 A：一手金额超单只上限 ⇒ 说的是「上限」，**不是**门槛、也不是 lot_note
    cap_slot = by_code["511660"]
    assert "单只上限" in cap_slot["reason"]
    assert cap_slot["one_lot_amount"] == 10000.0
    assert "min_amount_kind" not in cap_slot
    #: 分支 B：最低佣金门槛作废 ⇒ 带门槛三件套（阈值 / 被作废金额 / 档位）
    min_slot = by_code["515450"]
    assert min_slot["min_amount_kind"] == "新开仓"
    assert min_slot["min_amount_threshold"] == 3800.0
    assert min_slot["min_amount_skipped_amount"] == 584.40
    #: 分支 C：计划件自判「整手买不起」⇒ 带 lot_note
    lot_slot = by_code["159001"]
    assert lot_slot["lot_note"]
    assert "min_amount_kind" not in lot_slot
    #: 三条都**不许**出现在下单清单里
    assert not ({"511660", "515450", "159001"} & {o["code"] for o in card["orders"]}), card["orders"]
