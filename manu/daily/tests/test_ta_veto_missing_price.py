"""test_ta_veto_missing_price.py —— 孤儿维持行**缺价**时市值不得蒸发（回归断言）。

🔴 2026-11-11 审计缺陷 A·**缺价角落**（真机跑批暴露）：
   「被 TA 一票否决但仍在持仓」的**维持行**，若该标的**缺价**（动作件与 prices 里都没有），
   旧顺序把**缺价分支**排在 ``ta_rejected`` 分支**之前** ⇒ 该行落进缺价分支
   ⇒ ``target_shares=None``；
   ⇒ 随后的 ``positions`` 循环**先** ``covered.add(code)`` **再** ``if shares is None: continue``
   ⇒ 该代码**被标记 covered 却不进 positions**；
   ⇒ 「账户持仓兜底」循环又因 ``code in covered`` 跳过它
   ⇒ 它的市值在 ``post_trade.equity_amount`` 里**彻底消失**
   ⇒ 每日链的「权益 ≤ 80%」口径随之失真。

⇒ 修法（``daily/agents_rebalance.py::build_execution_card``）：``ta_rejected`` 分支**提前**，
   缺价时用**账户快照市值**兜底 ⇒ 该行仍是「维持、delta=0、不下单」，且市值**计入权益**。

【本文件覆盖】合成夹具（账户持有 + TA 否决 + 缺价）⇒
   ① ``post_trade.positions`` 里**有**它；② ``equity_amount`` **含**它的市值；
   ③ 动作 = 「维持」、``delta_shares == 0``；④ **无任何买卖挂单**。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DAILY = _HERE.parent
if str(_DAILY) not in sys.path:
    sys.path.insert(0, str(_DAILY))

import agents_rebalance as AR  # noqa: E402


#: 账户持有 512800（1000 股 × 0.874 元 = 874 元市值），但**该标的无行情**（缺价）
_ACCOUNT = {
    "total_assets": 30035.57,
    "cash": 23367.37,
    "market_value": 874.0,
    "positions": [
        {"code": "512800", "name": "银行ETF华宝", "shares": 1000,
         "price": 0.874, "market_value": 874.0},
    ],
}

#: 该标的被 TA 一票否决（维持）⇒ 语义 = 持有不动仓、**既不买也不卖**
_ACTIONS = [
    {
        "code": "512800", "name": "银行ETF华宝", "current_shares": 1000,
        "price": None, "ta_rejected": True, "reason": "TA 一票否决 ⇒ 维持",
    },
]


def _card():
    return AR.build_execution_card(
        actions=_ACTIONS, account=_ACCOUNT, asof="2026-12-01",
        execution_date="2026-12-02", live_trade=False, prices=None,
    )


def test_ta_rejected_missing_price_keeps_position_and_equity():
    """🔴 缺价 + TA 否决 ⇒ 维持行**必须**留在 positions 里、市值**必须**计入权益。"""
    card = _card()
    order = next(o for o in card["orders"] if o["code"] == "512800")

    # ③ 动作语义：维持、delta=0、目标股数 = 当前股数
    assert order["action"] == "维持"
    assert order["delta_shares"] == 0
    assert order["target_shares"] == 1000
    assert order["target_amount"] == 874.0, "缺价时必须用账户快照市值兜底"

    # ① positions 里**有**它（不许被 covered 吞掉）
    post = card["post_trade"]
    codes = [p["code"] for p in post["positions"]]
    assert "512800" in codes, f"维持行在 positions 里消失了（缺陷 A·缺价角落）：{codes}"

    # ② equity_amount **含**它的市值（不许蒸发）
    pos = next(p for p in post["positions"] if p["code"] == "512800")
    assert pos["shares"] == 1000
    assert pos["market_value"] == 874.0
    assert post["equity_amount"] >= 874.0, (
        f"equity_amount={post['equity_amount']} 未含维持行市值 874.0 ⇒ 市值蒸发"
    )

    # ④ 无任何买卖挂单（TA 一票否决 ⇒ 不许下单）
    assert not [o for o in card["orders"] if o.get("action") in ("买入", "卖出")]
    assert card["cash_flow"]["sell_proceeds"] == 0
    assert card["cash_flow"]["buy_amount"] == 0


def test_ta_rejected_missing_price_does_not_count_as_unpriceable():
    """缺价的维持行**不许**被标成「无法定价（不执行）」（那会让市值蒸发）。"""
    card = _card()
    actions = {o["code"]: o["action"] for o in card["orders"]}
    assert actions.get("512800") != "无法定价（不执行）"
