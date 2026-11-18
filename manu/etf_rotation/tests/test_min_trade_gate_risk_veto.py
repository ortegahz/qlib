"""🔴 2026-11-17（夜班 · 真机闸门 P12 红灯）**风险硬约束优先于最低佣金门槛**的回归测试。

【真机事故（快照 `20261117_190638`）】
    512800：目标 16% / 实际超配 22.5%（执行价口径 23.5%）
    ⇒ 减仓 **2,290 元** < 存量微调门槛 **2,800 元**
    ⇒ 旧口径**拦下**这笔减仓（省 5 元最低佣金）
    ⇒ 计划权益权重 **87.48%** 破生效上限 **80%**、计划现金权重 **12.52%** 破 **20%** 现金预留
    ⇒ 闸门 `probe_plan_arithmetic` 的 **P12 转红**（真 bug，不是断言写错）。

【本测试锁住的口径】
    ① 「省 5 元最低佣金」**不得**覆盖「权益上限 / 现金预留」这条风控硬约束；
    ② 只对**减仓**生效（拦下买入只会让现金更多 ⇒ 永远不破现金预留 ⇒ 行为不变）；
    ③ 否决必须**如实披露**（`risk_overrides`），不许静默；
    ④ **变异反证**：护栏任一环被改坏 ⇒ 本文件必须转红（不许把断言改松）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import min_trade_gate as gate_mod  # noqa: E402


#: 真机口径：总资产 30,035.57 元、5 席等权 16%（权益上限 80%）、存量微调门槛 2,800 元
TOTAL_ASSETS = 30_035.57
CAP = 0.80


def _row(code: str, *, current: int, target: int, price: float, amount: float | None = None):
    """造一行（**字段名与生产一致**：`est_amount` 为**带符号**的成交额）。"""
    row = {
        "code": code,
        "name": f"标的{code}",
        "current_shares": current,
        "target_shares": target,
        "delta_shares": target - current,
        "price": price,
        "est_amount": (amount if amount is not None else (target - current) * price),
        "est_fee": 0.0,
    }
    return row


def _real_case_plans() -> list[dict]:
    """复刻真机组合：512800 超配待减仓 + 4 席新开仓（合计权益正好打满 80%）。"""
    plans = [
        _row("512800", current=7_700, target=5_200, price=0.916),   # 减仓 2,500 股 = 2,290 元
    ]
    for code in ("159207", "159263", "515450", "515650"):
        plans.append(_row(code, current=0, target=4_816, price=1.0))  # 4,816 元 ≥ 3,800 ⇒ 放行
    return plans


def _apply(plans, *, cap=CAP, thresholds=None):
    return gate_mod.apply_min_trade_threshold(
        plans,
        total_assets=TOTAL_ASSETS,
        commission_rate=6.5e-4,
        thresholds=thresholds or {"清仓止损": 0.0, "新开仓": 3_800.0, "存量微调": 2_800.0},
        enabled=True,
        price_basis="2026-11-17 收盘价（信号日价）",
        equity_cap=cap,
    )


# ───────────────────────────── ① 否决生效 ─────────────────────────────
def test_减仓被拦会破权益上限_门槛不得作废这一笔():
    """🔴 核心口径：拦下这笔减仓 ⇒ 计划权益 87.6% > 上限 80% ⇒ **不许拦**。"""
    plans = _real_case_plans()
    disclosure = _apply(plans)

    row = next(p for p in plans if p["code"] == "512800")
    assert row["delta_shares"] == -2_500, row          # 减仓**照常执行**
    assert row.get("min_amount_skipped") is None, row  # 门槛**没有**作废它
    assert row["target_shares"] == 5_200, row

    assert disclosure["risk_overrides_count"] == 1, disclosure
    ov = disclosure["risk_overrides"][0]
    assert ov["code"] == "512800", ov
    assert ov["kind"] == "存量微调", ov
    assert ov["amount"] == pytest.approx(2_290.0, abs=0.01), ov
    assert ov["equity_cap"] == pytest.approx(CAP, abs=1e-6), ov
    assert ov["equity_weight_if_executed"] <= CAP + 1e-6, ov
    assert ov["equity_weight_if_blocked"] > CAP + 1e-6, ov
    assert ov["equity_weight_if_blocked"] == pytest.approx(0.87615, abs=1e-4), ov
    assert "风险硬约束优先" in ov["reason"], ov
    # 门槛省下的佣金里**不含**这笔（它没有被拦）
    assert disclosure["min_commission_saved"] == pytest.approx(0.0, abs=1e-9), disclosure


def test_否决必须如实披露_不许静默():
    """🔴 否决记录必须出现在披露结构与 notes 里（用户看得见）。"""
    disclosure = _apply(_real_case_plans())
    assert disclosure["equity_cap_used"] == pytest.approx(CAP, abs=1e-6), disclosure
    assert any("风险硬约束优先于最低佣金门槛" in n for n in disclosure["notes"]), disclosure["notes"]
    assert any("512800" in n for n in disclosure["notes"]), disclosure["notes"]


# ───────────────────── ② 不该否决的场景（旧行为必须不变）─────────────────────
def test_拦下不会破上限_仍照旧拦下():
    """🔴 上限宽松时 ⇒ 门槛照旧作废这笔减仓（旧行为不变）。"""
    plans = _real_case_plans()
    disclosure = _apply(plans, cap=0.95)

    row = next(p for p in plans if p["code"] == "512800")
    assert row["delta_shares"] == 0, row                # 被拦下
    assert row.get("min_amount_skipped") is True, row
    assert row["min_amount_kind"] == "存量微调", row
    assert disclosure["risk_overrides_count"] == 0, disclosure
    assert disclosure["min_commission_saved"] == pytest.approx(5.0, abs=0.01), disclosure


def test_组合本来就很轻_仍照旧拦下():
    """🔴 组合权益远低于上限 ⇒ 拦下减仓不会破上限 ⇒ 照旧拦。"""
    plans = [
        _row("512800", current=7_700, target=5_200, price=0.916),
        _row("159207", current=0, target=1_000, price=1.0),
    ]
    disclosure = _apply(plans)
    row = next(p for p in plans if p["code"] == "512800")
    assert row["delta_shares"] == 0, row
    assert disclosure["risk_overrides_count"] == 0, disclosure


def test_买入永远不被风险否决():
    """🔴 风险否决**只管减仓**：买入低于新开仓门槛 ⇒ 照旧拦下（不许因为上限而放行）。"""
    plans = [
        _row("159207", current=0, target=100, price=1.0),   # 100 元 < 3,800 ⇒ 新开仓档
        _row("159263", current=0, target=4_816, price=1.0),
        _row("515450", current=0, target=4_816, price=1.0),
        _row("515650", current=0, target=4_816, price=1.0),
    ]
    disclosure = _apply(plans)
    row = next(p for p in plans if p["code"] == "159207")
    assert row["delta_shares"] == 0, row
    assert row.get("min_amount_kind") == "新开仓", row
    assert disclosure["risk_overrides_count"] == 0, disclosure


def test_没给上限_旧行为不变():
    """🔴 不传 `equity_cap` ⇒ **完全保持旧行为**（其它调用方零影响）。"""
    plans = _real_case_plans()
    disclosure = gate_mod.apply_min_trade_threshold(
        plans,
        total_assets=TOTAL_ASSETS,
        commission_rate=6.5e-4,
        thresholds={"清仓止损": 0.0, "新开仓": 3_800.0, "存量微调": 2_800.0},
        enabled=True,
    )
    row = next(p for p in plans if p["code"] == "512800")
    assert row["delta_shares"] == 0, row
    assert disclosure["risk_overrides_count"] == 0, disclosure
    assert disclosure["equity_cap_used"] is None, disclosure


# ───────────────────────── ③ 变异反证（护栏承重）─────────────────────────
def test_变异_把否决判定改坏_必须转红(monkeypatch):
    """🔴 变异反证：把 `risk_veto_for_cap` 改坏（永远返回 None）⇒ 减仓又被拦下。

    ⇒ 证明「① 减仓被拦会破权益上限_门槛不得作废这一笔」这条断言**真的在承重**
    （不是恒真断言）。
    """
    plans = _real_case_plans()
    monkeypatch.setattr(gate_mod, "risk_veto_for_cap", lambda *a, **k: None)
    disclosure = _apply(plans)
    row = next(p for p in plans if p["code"] == "512800")
    assert row["delta_shares"] == 0, "变异后减仓被拦下 ⇒ 说明护栏确实在承重"
    assert disclosure["risk_overrides_count"] == 0


def test_变异_把上限判据改成反向_必须转红(monkeypatch):
    """🔴 变异反证：把判据改成「拦下后不超上限才否决」（反向）⇒ 真机上不该否决却否决了。"""
    plans = _real_case_plans()
    orig = gate_mod.risk_veto_for_cap

    def _broken(row, *, kind, amount, threshold, intended, current, total_assets, equity_cap, **kw):
        got = orig(row, kind=kind, amount=amount, threshold=threshold, intended=intended,
                   current=current, total_assets=total_assets, equity_cap=equity_cap, **kw)
        if got is None:
            return None
        broken = dict(got)
        broken["equity_weight_if_blocked"] = 0.01      # 反向 ⇒ 不满足「破上限」条件
        return None

    monkeypatch.setattr(gate_mod, "risk_veto_for_cap", _broken)
    disclosure = _apply(plans)
    row = next(p for p in plans if p["code"] == "512800")
    assert row["delta_shares"] == 0, "变异后减仓被拦下 ⇒ 说明判据确实在承重"
    assert disclosure["risk_overrides_count"] == 0
