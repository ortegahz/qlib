"""🔴 小资金兜底（2026-11-19）的**钉子测试**。

【用户拍板的需求】
「**保留 3 万元账户规模**，让 Agent 在阶段 5 增加一条小资金兜底规则：
  当**剩余权益额度不足以均分跨过 3,800 元**时，允许将**单笔建仓下限放宽至 1,500 元**，
  并**集中额度优先买入排名第 1 的顺延标的**。」

【为什么要有这一条】
3 万本金 + 生效权益上限 30%~35% ⇒ 扣掉「维持行」占额后，**剩余额度不足以均分跨过
3,800 元** ⇒ 门槛把**所有**新开仓全拦下 ⇒ **数学死锁**（以后再也买不进新 ETF）。
⇒ 修法**不动 TA 的宏观风控判断、不动常规门槛**，只在**门槛层**加一条兜底：
  ① 额度不足均分跨过门槛 ⇒ 本轮「新开仓」单笔下限**放宽到 1,500 元**；
  ② 额度**集中**给候选 #1（`final_score` 最高者）⇒ 只买一只，不买四只碎单。

【本文件钉死的行为】
  ① **触发**：均分额 < 常规「新开仓」门槛 且 集中落地金额 ≥ 1,500 元；
  ② **不触发**（三种）：额度足够均分 / 额度 < 1,500 / 整手落地不足 1,500；
  ③ **集中给 #1**（`final_score` 最高者；同分保持原序）；
  ④ **其余候选仍被拦下**（门槛层放宽后 1,500 元档）⇒ 落地权益不破生效上限；
  ⑤ **TA 维持行占额先扣除**（额度口径）⇒ 不会因为维持行而多买；
  ⑥ **只降不升**（放宽下限永远 ≤ 常规门槛）；
  ⑦ **开关可关**（`--no-small-account-fallback`）⇒ 行为与旧版一致；
  ⑧ **阶段 6 沿用阶段 5 的集中标的**（`preferred_code`）⇒ 两阶段执行卡不打架。

【夹具】全部自造（纯 dict）⇒ **不依赖服务器路径、不读 canonical 产物**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
#: 🔴 本文件是**镜像对**（`manu/tests/` ↔ `manu/etf_rotation/tests/` ⇒ 逐字一致）
#:    ⇒ 可能被**镜像侧**（`manu/tests/`）收集 ⇒ 仓库根要认得**两种**位置。
_ROOT = _HERE.parents[2] if _HERE.parent.parent.name == "etf_rotation" else _HERE.parents[1]
_ETF_DIR = _ROOT / "etf_rotation"
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

#: 🔴 **按文件路径加载活链** `manu/etf_rotation/min_trade_gate.py`
#:    （`manu/` 与 `manu/etf_rotation/` 同名模块并存 ⇒ 裸 import 命中哪份看 sys.path 顺序）。
_spec = importlib.util.spec_from_file_location(
    "etf_rotation_min_trade_gate", _ETF_DIR / "min_trade_gate.py")
gate_mod = importlib.util.module_from_spec(_spec)
sys.modules["etf_rotation_min_trade_gate"] = gate_mod
_spec.loader.exec_module(gate_mod)

TOTAL = 30405.17
CAP = 0.35
HOLD_AMOUNT = 7037.80          # 维持行 512800：7,700 股 × 0.914 元


def _hold_row(code: str = "512800", amount: float = HOLD_AMOUNT) -> dict:
    """TA 一票否决但**仍在持仓**的「维持」行（占额 ⇒ 先从额度里扣）。"""
    return {
        "code": code, "name": "维持行", "ta_rejected": True, "held_not_touched": True,
        "current_shares": 7700, "price": 0.914, "delta_shares": 0, "est_amount": 0.0,
        "target_amount": amount, "target_weight": amount / TOTAL,
        "reason": "TA 一票否决 ⇒ 维持现仓",
    }


def _cand(code: str, score: float, price: float, amount: float) -> dict:
    """新开仓候选行（当前 0 股 ⇒ 目标股数 > 0）。"""
    shares = int(amount / price)
    return {
        "code": code, "name": f"候选{code}", "final_score": score, "price": price,
        "current_shares": 0, "target_shares": shares, "delta_shares": shares,
        "est_amount": amount, "est_fee": 0.0, "target_amount": amount,
        "target_weight": amount / TOTAL, "reason": "候选", "ta_rejected": False,
    }


def _four_candidates() -> list[dict]:
    """四个等分候选（每个 ≈ 769 元 ⇒ 均分远低于 3,800 元 ⇒ 触发集中）。"""
    return [
        _cand("515450", 0.8260, 1.507, 769.0),
        _cand("159263", 0.0484, 1.100, 754.0),
        _cand("159207", -0.1881, 0.900, 736.0),
        _cand("515170", -0.4760, 1.000, 729.0),
    ]


def _run(plans, *, cap=CAP, total=TOTAL, **kwargs):
    return gate_mod.apply_small_account_fallback(
        plans, total_assets=total, equity_cap=cap, **kwargs)


def test_额度不足均分跨过门槛时触发并集中买排名1() -> None:
    """🔴 核心：均分额 < 3,800 ⇒ 触发；额度**集中**给 `final_score` 最高的 #1。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = _run(plans, enabled=True, commission_rate=0.00015)

    assert fb["triggered"] is True
    assert fb["normal_new_limit"] == 3800.0
    assert fb["relaxed_new_limit"] == 1500.0
    assert fb["candidate_count"] == 4
    # 额度 = 上限空间 − 维持行占额
    assert fb["allowance_amount"] == pytest.approx(CAP * TOTAL - HOLD_AMOUNT, abs=0.01)
    assert fb["per_candidate_amount"] < fb["normal_new_limit"]
    # 选中的是 #1（final_score 最高）
    assert fb["selected"]["code"] == "515450"
    assert fb["selected"]["final_score"] == 0.8260
    # 集中金额 = 整手落地金额（向下取整到 100 股）
    assert fb["selected"]["target_shares"] % 100 == 0
    assert fb["concentrated_amount"] >= 1500.0
    assert fb["concentrated_amount"] <= fb["allowance_amount"]


def test_其余候选仍被门槛拦下且落地权益不破生效上限() -> None:
    """🔴 集中只能买一只 ⇒ 其余候选照旧被拦（放宽后 1,500 元档）⇒ 权益不破上限。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = _run(plans, enabled=True, commission_rate=0.00015)
    thr = gate_mod.resolve_thresholds(None)
    thr["新开仓"] = fb["relaxed_new_limit"]
    gate = gate_mod.apply_min_trade_threshold(
        plans, total_assets=TOTAL, commission_rate=0.00015,
        thresholds=thr, enabled=True, equity_cap=CAP)

    skipped = {row["code"] for row in gate["skipped"]}
    assert skipped == {"159263", "159207", "515170"}
    assert "515450" not in skipped
    # 死锁报警**不亮**（有一只真的建仓了）
    assert gate["deadlock_risk"] is False
    # 落地权益 ≤ 生效上限
    equity = sum(float(p.get("target_amount") or 0) for p in plans
                 if p.get("target_amount") is not None)
    assert equity <= CAP * TOTAL + 0.01


def test_额度足够均分跨过门槛时不触发() -> None:
    """🔴 额度充足 ⇒ 行为与旧版一致（不集中、不改门槛）。

    🔴 额度 = 上限空间 − 维持行占额 ⇒ 生效上限 40% 时额度 5,124.27 元 ≥ 3,800 元。
    """
    plans = [_hold_row(), _cand("515450", 0.826, 1.507, 4200.0)]
    fb = _run(plans, cap=0.40, enabled=True)
    assert fb["triggered"] is False
    assert fb["selected"] is None
    assert fb["relaxed_new_limit"] == 1500.0


def test_额度低于放宽下限时不触发() -> None:
    """🔴 剩余额度 < 1,500 元 ⇒ 宁可空仓也不建碎单。

    🔴 额度 = 0.28 × 30,405.17 − 7,037.80 = 1,475.65 元 < 1,500 元。
    """
    plans = [_hold_row(), _cand("515450", 0.826, 1.507, 769.0)]
    fb = _run(plans, cap=0.28, enabled=True)
    assert fb["triggered"] is False
    assert fb["allowance_amount"] < 1500.0


def test_额度买不起一手时不触发() -> None:
    """🔴 #1 自身金额够不到门槛 ⇒ 要集中 ⇒ 但**买不起一手** ⇒ 不触发（不建碎单）。"""
    plans = [_hold_row(), _cand("515450", 0.826, 40.0, 769.0)]
    fb = _run(plans, cap=0.352, enabled=True)
    assert fb["triggered"] is False
    assert fb["concentrated_amount"] == 0.0


def test_只降不升_放宽下限永不超过常规门槛() -> None:
    """🔴 常规门槛被 CLI 调到 1,000 元 ⇒ 放宽下限 = 1,000（不许抬高）。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = _run(plans, enabled=True, thresholds={"新开仓": 1000.0})
    assert fb["relaxed_new_limit"] == 1000.0
    assert fb["relaxed_new_limit"] <= fb["normal_new_limit"]


def test_同分时保持原序() -> None:
    """🔴 `final_score` 同分 ⇒ 取**原序**靠前的那只（不编排名）。"""
    plans = [_hold_row(), _cand("515450", 0.826, 1.507, 769.0), _cand("159263", 0.826, 1.100, 754.0)]
    fb = _run(plans, enabled=True)
    assert fb["triggered"] is True
    assert fb["selected"]["code"] == "515450"


def test_维持行占额先扣除() -> None:
    """🔴 额度口径 = 上限空间 − **维持行**占额 ⇒ 维持行不会让兜底多买。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = _run(plans, enabled=True)
    assert fb["allowance_amount"] == pytest.approx(CAP * TOTAL - HOLD_AMOUNT, abs=0.01)
    # 若不扣维持行，额度会是 0.35 × 30,405.17 = 10,641.81 ⇒ 均分 2,660 元仍 < 3,800
    # ⇒ 也会触发，但**集中金额**必须按扣掉维持行后的额度算（3,604.01 上限）。
    assert fb["allowance_amount"] < CAP * TOTAL
    assert fb["concentrated_amount"] <= CAP * TOTAL - HOLD_AMOUNT


def test_开关关闭时不触发() -> None:
    """🔴 `--no-small-account-fallback` ⇒ 完全不改变既有行为。"""
    plans = [_hold_row(), *_four_candidates()]
    before = {p["code"]: p.get("target_amount") for p in plans}
    fb = _run(plans, enabled=False)
    assert fb["triggered"] is False
    assert fb["enabled"] is False
    after = {p["code"]: p.get("target_amount") for p in plans}
    assert before == after


def test_阶段6沿用阶段5的集中标的() -> None:
    """🔴 `preferred_code` ⇒ 阶段 6 选**同一只**（两阶段执行卡不打架）。"""
    plans = [_hold_row(), _cand("515450", 0.826, 1.507, 769.0), _cand("159263", 0.048, 1.100, 754.0)]
    fb = _run(plans, enabled=True, preferred_code="159263")
    assert fb["triggered"] is True
    assert fb["selected"]["code"] == "159263"
    assert fb["selected"]["carried_from_plan"] is True


def test_没有新开仓候选时不触发() -> None:
    """🔴 没有「新开仓」候选 ⇒ 不触发（不编数字）。"""
    plans = [_hold_row()]
    fb = _run(plans, enabled=True)
    assert fb["triggered"] is False
    assert fb["candidate_count"] == 0


def test_缺生效权益上限时不触发() -> None:
    """🔴 缺生效上限 ⇒ 不触发（宁可不买也不编上限）。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = gate_mod.apply_small_account_fallback(
        plans, total_assets=TOTAL, equity_cap=None, enabled=True)
    assert fb["triggered"] is False


def test_候选1自身金额已达标时只放宽不集中() -> None:
    """🔴 2026-11-19 真机复盘修：#1 自身金额 ≥ 放宽下限 ⇒ **不集中**（保持等权结构）。

    🔴 合成夹具（2 席 × 3,000 元、常规门槛 3,800 元）：旧实现把 6,000 元全压给 #1
    ⇒ 计划权益合计 29.95% ≠ 生效上限 20%（**结构被改坏**）⇒ 现改为「只放宽、不集中」。
    """
    plans = [_cand("512800", 1.8, 1.0, 3000.0), _cand("510300", 1.2, 1.0, 3000.0)]
    _before = [float(p.get("target_weight") or 0) for p in plans]
    fb = _run(plans, cap=0.20, enabled=True)
    assert fb["triggered"] is True
    assert fb["concentrated"] is False
    assert fb["selected"] is None
    assert fb["relaxed_new_limit"] == 1500.0
    # 目标权重**原封不动** ⇒ 等权结构不变
    assert [float(p.get("target_weight") or 0) for p in plans] == _before


def test_集中后可达权重必须同步() -> None:
    """🔴 `target_weight_effective` 是「计划权益权重」的取数口 ⇒ 集中后必须同步。

    🔴 真机证据（修前）：计划权益 24.76% vs 实际落地 34.97% ⇒ 交付件自相矛盾。
    """
    plans = [_hold_row(), *_four_candidates()]
    for _p in plans:
        _p["target_weight_effective"] = float(_p.get("target_weight") or 0.0)
        _p["weight_to_cash"] = 0.0
    fb = _run(plans, enabled=True, commission_rate=0.00015)
    assert fb["triggered"] is True
    assert fb["concentrated"] is True
    _sel = next(p for p in plans if p["code"] == fb["selected"]["code"])
    assert float(_sel["target_weight_effective"]) == pytest.approx(
        float(_sel["target_weight"]), abs=1e-9)
    assert float(_sel["weight_to_cash"]) == 0.0
    #: 计划权益合计 = Σ 可达权重 ⇒ 与「生效上限」口径一致
    _invested = sum(float(p.get("target_weight_effective") or 0.0) for p in plans)
    assert _invested <= CAP + 1e-9


def test_集中后其余候选目标权重归零() -> None:
    """🔴 其余候选的席位预算已被集中给 #1 ⇒ 计划目标权重归零（不许重复计）。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = _run(plans, enabled=True)
    assert fb["concentrated"] is True
    for _p in plans[1:]:
        if _p["code"] == fb["selected"]["code"]:
            continue
        assert float(_p.get("target_weight") or 0.0) == 0.0


def test_集中金额不得超过单席上限() -> None:
    """🔴 `max_single_amount` ⇒ 集中额度封顶（免得集中变成新的超配）。"""
    plans = [_hold_row(), *_four_candidates()]
    fb = _run(plans, enabled=True, max_single_amount=2000.0)
    assert fb["triggered"] is True
    assert fb["concentrated_amount"] <= 2000.0
