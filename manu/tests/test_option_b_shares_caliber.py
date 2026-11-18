"""🔴 2026-11-11 新增：**选项 B 的落地股数口径**（目标权重优先）必须被钉住。

【背景（服务器实测红灯）】
    ``probe_decision_scenarios`` 的 P6（独立重算）红灯：
    「512800=B 现金差 -87.942800；权益差 88.000000；总资产差 0.057200」
    ⇒ 独立重算给出 3,400 股，生成器给出 3,300 股（差 100 股 = 88 元）。

【根因】
    2026-11-03 那一轮决策 md 的最终裁决里**同时**有两种写法：
      · 「将当前22.54%的仓位降至**基准约10%、硬上限10%**」⇒ **目标权重 10%**；
      · 「按2026-11-03**参考价**0.874元测算，`512800`保留3,400份」⇒ **参考价下的示例股数**。
    旧口径（「方向=减 且原文给了保留股数 ⇒ 用股数」）在**执行价**下落地：
      3,400 × 0.88 = 2,992 元 = 总资产的 **10.02%** ⇒ **突破原文自己写的硬上限 10%**；
    而按**目标权重 10%** 向下取整到整手 = 3,300 份 = 2,904 元 = **9.73%** ⇒ 不破上限。
    ⇒ **目标权重是 LLM 的主口径，股数只是参考价下的示例** ⇒ 目标权重优先
    （与 ``make_decision_options`` / ``make_decision_scenarios`` 同口径）。

【本文件锁住的行为】
    ① 有 ``target_pct`` ⇒ 按目标权重向下取整到整手（**不用** ``shares_keep``）；
    ② 没有 ``target_pct``、方向=减 ⇒ 才用原文 ``shares_keep``；
    ③ 零方向（卖 / 不买 / 不建仓 / 清仓）⇒ 0 股（**即使**有 target_pct）；
    ④ 算出来的股数买不起一手 ⇒ 0 股。

【夹具】全部自造（不依赖服务器路径、不读真实交付件）。
"""

from __future__ import annotations

import pytest

from manu.etf_rotation.probes import probe_decision_scenarios as P  # type: ignore  # noqa: E402

TOTAL_ASSETS = 29854.67
PRICE = 0.88


def _plan(target: dict) -> dict:
    return {
        "meta": {"total_assets": TOTAL_ASSETS},
        "llm_final_decision": {"targets": {"512800": target}},
    }


def _action() -> dict:
    return {"code": "512800", "price": PRICE}


def test_target_pct_wins_over_reference_price_shares_keep():
    """① 有目标权重 ⇒ 按目标权重整手取整（3,300 股），**不是** 3,400 股。"""
    plan = _plan({"direction": "减", "target_pct": 10.0, "shares_keep": 3400})
    got = P.option_b_shares(plan, _action())
    assert got == 3300, f"目标权重口径应给 3,300 股，实际 {got}"
    # 破上限的反证：3,400 股在执行价下是 10.02% ⇒ 原文的「硬上限 10%」会被突破
    assert 3400 * PRICE / TOTAL_ASSETS * 100.0 > 10.0
    assert got * PRICE / TOTAL_ASSETS * 100.0 <= 10.0


def test_shares_keep_used_only_when_target_pct_missing():
    """② 没有目标权重、方向=减 ⇒ 才回退到原文保留股数。"""
    plan = _plan({"direction": "减", "target_pct": None, "shares_keep": 3400})
    assert P.option_b_shares(plan, _action()) == 3400


def test_zero_direction_is_zero_shares_even_with_target_pct():
    """③ 零方向 ⇒ 0 股（不受目标权重影响）。"""
    for direction in ("卖", "不买", "不建仓", "清仓"):
        plan = _plan({"direction": direction, "target_pct": 10.0})
        assert P.option_b_shares(plan, _action()) == 0, direction


def test_unaffordable_lot_becomes_zero():
    """④ 算出来的股数买不起一手 ⇒ 0 股。"""
    plan = _plan({"direction": "买", "target_pct": 0.001})
    assert P.option_b_shares(plan, _action()) == 0


def test_missing_everything_raises():
    """既无方向也无目标权重 ⇒ 报错（不许默认、不许静默跳过）。"""
    plan = _plan({"direction": None, "target_pct": None})
    with pytest.raises(ValueError):
        P.option_b_shares(plan, _action())
