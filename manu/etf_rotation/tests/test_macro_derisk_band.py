#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-18（用户目标文档 §三.2「宏观降仓」）：**压到 25%~35% 权益上限**的钉子。

【钉的是什么】
「宏观降仓（比如压到 25%~35% 权益上限）」= 宏观避险时，组合**权益仓位上限**
落在 **25%~35%**（其余是现金）⇒ **不是**「降仓 25~35 个百分点」。

① **没有** TA 压降建议 ⇒ 生效上限**完全不变**（= 规则层硬上限 0.80）⇒ 产物逐字节不变；
② **有** TA 压降建议 ⇒ 生效上限 = min(硬上限, TA 建议, 带内上沿 0.35)：
   TA=36% ⇒ **0.35**｜TA=50% ⇒ **0.35**｜TA=20% ⇒ **0.20**（TA 比带**更严**
   ⇒ **保留 TA 的值**，**绝不**抬高回 0.25）｜TA=0% ⇒ **0.00**；
③ 🔴 TA 建议 ≥ 硬上限（80%）⇒ 规则层**已忽略**（`at_or_above_base_cap`）⇒ **不压降** ⇒ 0.80；
④ 🔴 **绝不抬高**：带只往下压 ⇒ 生效上限 ≤ 旧口径取值；
⑤ 披露：`ta_screening.equity_cap_ta_recommendation` 必须带
   `macro_band_min` / `macro_band_max` / `macro_band_applied` / `macro_band_reason`
   ⇒ md 里也要**看得见**这条带。

【夹具】
合成夹具（report.json / decision.md / 行情 / 账户）**照抄** `test_ta_equity_cap.py`
的构造方式（同目录、同风格）⇒ 只换决策件里的**权益目标口径**数字。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402
import final_rebalance_plan as F  # noqa: E402
import llm_final  # noqa: E402

#: 🔴 单一口径：**权益上限带**的事实源 = `config.MACRO_DERISK_EQUITY_CAP_MIN/MAX`
#:    （不许在测试里写第二份字面量当事实源 ⇒ 只允许**断言**这两个值）。
#:    ⚠️ 本仓库有**两个**同名 config.py（`manu/etf_rotation/config.py` 与它的根级
#:    **逐字节副本** `manu/config.py`）⇒ 断言拿到的是 ETF 线那一份 ⇒ 防常量从别处漂移。
_CONFIG_OK = ("manu/etf_rotation/config.py", "manu/config.py")
assert any(str(C.__file__).replace("\\", "/").endswith(p) for p in _CONFIG_OK), C.__file__

_ASOF = "2026-11-03"
_NAMES = {"512800": "银行ETF华宝", "510300": "沪深300ETF"}

#: 规则层动作（合成）：两席各 40% ⇒ 合计 80% = 1 − 现金预留比例
_ACTIONS = [
    {"code": "512800", "name": "银行ETF华宝", "action": "买入",
     "target_weight": 0.4, "current_weight": 0.0},
    {"code": "510300", "name": "沪深300ETF", "action": "买入",
     "target_weight": 0.4, "current_weight": 0.0},
]

_HOLDINGS = pd.DataFrame([
    {"etf_code": "512800", "stock_code": "600000", "weight_pct": 100.0},
    {"etf_code": "510300", "stock_code": "600003", "weight_pct": 100.0},
])


def _synthetic_prices() -> pd.DataFrame:
    """合成日线（两只 ETF 各自随机 ⇒ 不构成冲突对）。"""
    dates = pd.bdate_range("2025-01-01", periods=260)
    rng = np.random.default_rng(3)
    rows: list[dict] = []
    for code in ("512800", "510300"):
        rets = rng.normal(0.0, 0.01, len(dates))
        px = 1.0 * np.cumprod(1 + rets)
        for d, p in zip(dates, px):
            rows.append({"date": d.strftime("%Y-%m-%d"), "code": code,
                         "open": p, "high": p, "low": p, "close": p,
                         "volume": 1e6, "amount": 1e8})
    return pd.DataFrame(rows)


def _plan(tmp_path: Path, decision_md: str) -> dict:
    """用合成夹具跑一次 `build_plan`（照抄 `test_ta_equity_cap.py` 的写法）。"""
    tmp = Path(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)
    prices = _synthetic_prices()
    prices.to_csv(tmp / "etf_prices.csv", index=False)
    pd.DataFrame([{"code": c, "name": n, "last_price": 1.0,
                   "last_price_date": _ASOF} for c, n in _NAMES.items()]
                 ).to_csv(tmp / "etf_universe.csv", index=False)
    pd.DataFrame([{"code": "512800", "name": "银行ETF华宝",
                   "shares": 0, "price": 1.0, "market_value": 0.0}]
                 ).to_csv(tmp / "account.csv", index=False)
    (tmp / "decision.md").write_text(decision_md, encoding="utf-8")
    report = {
        "meta": {"asof": _ASOF,
                 "params": {"commission_bp": 1.5, "slippage_bp": 5.0,
                            "target_n_holdings": 2}},
        "current_holdings_diagnosis": {
            "total_assets": 30000.0, "cash": 30000.0, "available": True,
            "holdings": [],
        },
        "next_rebalance": {"target_date": "2026-12-01", "signal_date": _ASOF,
                           "execution_date": "2026-12-02", "trading_days_until": 20,
                           "rule": "月度首个交易日", "execution_lag_days": 1},
        "recommendation": {"actions": _ACTIONS,
                           "hold_buffer": 0,
                           "weights_used": {"alpha": 0.6, "momentum": 0.4}},
        "candidates": [
            {"code": "512800", "name": "银行ETF华宝", "final_score": 1.8,
             "theme_id": "银行", "selected": True, "above_ma60": True,
             "entry_ma_ok": True, "broken": False},
            {"code": "510300", "name": "沪深300ETF", "final_score": 1.7,
             "theme_id": "沪深300", "selected": True, "above_ma60": True,
             "entry_ma_ok": True, "broken": False},
        ],
        "risk_disclosure": [],
    }
    (tmp / "report.json").write_text(json.dumps(report, ensure_ascii=False),
                                     encoding="utf-8")
    args = argparse.Namespace(
        report=str(tmp / "report.json"), decision=str(tmp / "decision.md"),
        prices=str(tmp / "etf_prices.csv"), universe=str(tmp / "etf_universe.csv"),
        account=str(tmp / "account.csv"), out_dir=str(tmp),
    )
    original_loader = F.overlap_mod.load_overlap_inputs
    F.overlap_mod.load_overlap_inputs = lambda **kw: (_HOLDINGS, _synthetic_prices())
    try:
        return F.build_plan(args)
    finally:
        F.overlap_mod.load_overlap_inputs = original_loader


def _decision_reduce(pct: float) -> str:
    """TA 决策件：**目标动词**口径的组合权益上限建议（⇒ `equity_target_pct`）。"""
    return (
        "## 六、最终交易决定\n\n"
        f"- 组合层权益由 48.02% 适度降至约 **{pct:g}%**，现金提高至约 **{100 - pct:g}%**。\n"
    )


#: 只有**陈述句**（没有目标动词）⇒ 不是建议 ⇒ 上限完全不变
_DECISION_STATEMENT = (
    "## 六、最终交易决定\n\n"
    "- 当前组合层权益敞口约 **56%**，维持现状。\n"
)


def _sum_target_weight(plan: dict) -> float:
    return sum(float(p.get("target_weight") or 0.0) for p in plan.get("actions") or [])


def _effective_cap(plan: dict) -> float:
    return float((plan.get("ta_screening") or {}).get("equity_cap_effective") or 0.0)


# ───────────────────────── ① 带函数（纯函数） ─────────────────────────
def test_带常量_事实源写死():
    """🔴 带的上/下沿与开关 = `config` 里的**单一事实源**（不许在别处写死）。"""
    assert C.MACRO_DERISK_EQUITY_CAP_MIN == 0.25, C.MACRO_DERISK_EQUITY_CAP_MIN
    assert C.MACRO_DERISK_EQUITY_CAP_MAX == 0.35, C.MACRO_DERISK_EQUITY_CAP_MAX
    assert C.MACRO_DERISK_BAND_ENABLED is True, C.MACRO_DERISK_BAND_ENABLED


def test_带表_生效上限数值写死():
    """🔴 断言**写死数值**（不许只断言「没抛错」）。

    （TA 建议, 期望生效上限, 期望 reason, 期望 applied）
    """
    cases = [
        (None, 0.80, "no_ta_advice", False),       # 没有 TA 建议 ⇒ 完全不变
        (0.80, 0.80, "no_ta_advice", False),       # ≥ 硬上限 ⇒ 规则层已忽略 ⇒ 不压降
        (0.50, 0.35, "above_band_max", True),      # 高于带内上沿 ⇒ 压到上沿
        (0.36, 0.35, "above_band_max", True),      # 同上（36% ⇒ 0.35）
        (0.20, 0.20, "below_band_min_kept", True),  # TA 更严 ⇒ 保留 TA 的值
        (0.00, 0.00, "below_band_min_kept", True),  # 全现金
    ]
    for ta_pct, want_eff, want_reason, want_applied in cases:
        eff, info = F._apply_macro_derisk_band(ta_pct, F._EQUITY_CAP)
        assert abs(float(eff) - want_eff) < 1e-12, (ta_pct, eff, want_eff, info)
        assert info["reason"] == want_reason, (ta_pct, info)
        assert bool(info["applied"]) is want_applied, (ta_pct, info)


def test_绝不抬高_生效上限不高于旧口径():
    """🔴 带只往下压 ⇒ 任何情况下生效上限 ≤ 旧口径 min(硬上限, TA 建议)。"""
    for ta_pct in (None, 0.0, 0.2, 0.25, 0.35, 0.36, 0.5, 0.8):
        eff, _info = F._apply_macro_derisk_band(ta_pct, F._EQUITY_CAP)
        old = F._EQUITY_CAP if ta_pct is None else min(F._EQUITY_CAP, float(ta_pct))
        assert float(eff) <= old + 1e-12, (ta_pct, eff, old)


def test_带下沿只做披露_不许把TA更严的值抬回带内():
    """🔴 TA=20% ⇒ 0.20（**绝不**抬回带内下沿 0.25）。"""
    eff, info = F._apply_macro_derisk_band(0.20, F._EQUITY_CAP)
    assert abs(float(eff) - 0.20) < 1e-12, info
    assert abs(float(info["band_min"]) - 0.25) < 1e-12, info


# ───────────────────────── ② 端到端（build_plan） ─────────────────────────
def test_端到端_无TA建议_上限完全不变(tmp_path):
    """🔴 没有 TA 压降建议 ⇒ 生效上限**完全不变**（0.80）⇒ 产物逐字节不变。"""
    plan = _plan(tmp_path, _DECISION_STATEMENT)
    scr = plan.get("ta_screening") or {}
    assert abs(_effective_cap(plan) - 0.80) < 1e-9, scr
    assert abs(_sum_target_weight(plan) - 0.80) < 1e-9, plan.get("actions")
    rec = scr.get("equity_cap_ta_recommendation") or {}
    assert rec.get("macro_band_applied") is False, rec
    assert rec.get("macro_band_reason") == "no_ta_advice", rec
    md = F.render_markdown(plan)
    assert "宏观降仓" not in md, md[:4000]


def test_端到端_TA建议36_压进带内上沿35(tmp_path):
    plan = _plan(tmp_path, _decision_reduce(36))
    scr = plan.get("ta_screening") or {}
    assert abs(_effective_cap(plan) - 0.35) < 1e-9, scr
    total = _sum_target_weight(plan)
    assert total <= 0.35 + 1e-9, f"计划权益合计 {total} 超过生效上限 0.35"
    assert abs(total - 0.35) < 1e-9, f"计划权益合计 {total}，应恰为 0.35"
    # 🔴 `equity_cap` 恒为 0.8（验收脚本按它断言）
    assert abs(float(scr.get("equity_cap") or 0) - 0.8) < 1e-9, scr


def test_端到端_TA建议50_压进带内上沿35(tmp_path):
    plan = _plan(tmp_path, _decision_reduce(50))
    assert abs(_effective_cap(plan) - 0.35) < 1e-9, plan.get("ta_screening")
    assert abs(_sum_target_weight(plan) - 0.35) < 1e-9, plan.get("actions")


def test_端到端_TA建议20_保留TA更严的值(tmp_path):
    """🔴 TA 比带**更严** ⇒ 保留 0.20（**绝不**抬回 0.25）。"""
    plan = _plan(tmp_path, _decision_reduce(20))
    assert abs(_effective_cap(plan) - 0.20) < 1e-9, plan.get("ta_screening")
    assert abs(_sum_target_weight(plan) - 0.20) < 1e-9, plan.get("actions")
    rec = (plan.get("ta_screening") or {}).get("equity_cap_ta_recommendation") or {}
    assert rec.get("macro_band_reason") == "below_band_min_kept", rec


def test_端到端_TA建议80_不低于硬上限_不压降(tmp_path):
    """🔴 TA=80% ⇒ 规则层已忽略（`at_or_above_base_cap`）⇒ 上限不变（0.80）。"""
    plan = _plan(tmp_path, _decision_reduce(80))
    scr = plan.get("ta_screening") or {}
    assert abs(_effective_cap(plan) - 0.80) < 1e-9, scr
    assert abs(_sum_target_weight(plan) - 0.80) < 1e-9, plan.get("actions")
    rec = scr.get("equity_cap_ta_recommendation") or {}
    assert rec.get("reason") == "at_or_above_base_cap", rec
    assert rec.get("macro_band_applied") is False, rec


def test_披露块必须含带字段(tmp_path):
    plan = _plan(tmp_path, _decision_reduce(36))
    rec = (plan.get("ta_screening") or {}).get("equity_cap_ta_recommendation") or {}
    assert abs(float(rec.get("macro_band_min")) - 0.25) < 1e-12, rec
    assert abs(float(rec.get("macro_band_max")) - 0.35) < 1e-12, rec
    assert rec.get("macro_band_applied") is True, rec
    assert rec.get("macro_band_reason") == "above_band_max", rec
    # 🔴 TA 说的**原值**不许被带改写（旧枚举 `accepted` 也不许破坏）
    assert abs(float(rec.get("pct")) - 0.36) < 1e-9, rec
    assert rec.get("reason") == "accepted", rec
    assert abs(float(rec.get("effective_cap")) - 0.35) < 1e-9, rec


def test_md必须披露带(tmp_path):
    plan = _plan(tmp_path, _decision_reduce(36))
    md = F.render_markdown(plan)
    assert "宏观降仓" in md, md[:4000]
    assert "25.0%~35.0%" in md, md[:4000]


# ───────────────────────── ③ 变异负例（断言必须有牙） ─────────────────────────
def test_变异反例_把带上沿monkeypatch成硬上限_35断言必须转红(tmp_path, monkeypatch):
    """🔴 断言必须有牙：把 `MACRO_DERISK_EQUITY_CAP_MAX` 打成 0.80（= 等于关掉带）
    ⇒ 生效上限**必须**从 0.35 变回 0.36 ⇒ 上面那些 0.35 断言**转红**。"""
    plan_ok = _plan(tmp_path / "ok", _decision_reduce(36))
    assert abs(_effective_cap(plan_ok) - 0.35) < 1e-9, plan_ok.get("ta_screening")
    monkeypatch.setattr(C, "MACRO_DERISK_EQUITY_CAP_MAX", 0.80)
    plan_mut = _plan(tmp_path / "mut", _decision_reduce(36))
    eff_mut = _effective_cap(plan_mut)
    assert abs(eff_mut - 0.35) > 1e-9, (
        f"变异体的生效上限 {eff_mut} 竟仍是 0.35 ⇒ 断言没有牙")
    assert abs(eff_mut - 0.36) < 1e-9, eff_mut


def test_变异负例_关掉带开关_回到旧口径(tmp_path, monkeypatch):
    """🔴 关掉带开关（仅供审计切回旧口径）⇒ 生效上限回到 TA 的原值 0.36。"""
    monkeypatch.setattr(C, "MACRO_DERISK_BAND_ENABLED", False)
    plan = _plan(tmp_path, _decision_reduce(36))
    assert abs(_effective_cap(plan) - 0.36) < 1e-9, plan.get("ta_screening")
    rec = (plan.get("ta_screening") or {}).get("equity_cap_ta_recommendation") or {}
    assert rec.get("macro_band_applied") is False, rec
    assert rec.get("macro_band_reason") == "band_disabled", rec
