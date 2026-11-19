#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-18（用户目标文档 §三.1）：**TA 喊 Sell 却没给数字上限 ⇒ 默认兜底上限**。

【钉的是什么】
V1 臂实测：TA 宏观态度 = `Sell`，但**没吐出百分比上限**
⇒ 旧口径判 `not_a_target` ⇒ `pct=None` ⇒ **完全不压降**
⇒ 顺延补位把权益从 23.43% 拉到 **70.53%**（与防守初衷完全相反）。

【本文件钉三条】
① TA **看空**（评级/动作）+ **解析不出数字** ⇒ 生效上限 = 兜底上限
   （单一事实源 = `config.MACRO_DERISK_EQUITY_CAP_MAX`，默认 35%），reason = `bearish_fallback_cap`；
② **没有** TA 决策 / 看多 / 中性 ⇒ **不套兜底**（保持旧行为，不许凭关键词压降）；
③ 端到端：兜底上限真的作用到计划权益合计（≤ 35%）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402
import final_rebalance_plan as F  # noqa: E402
import llm_final  # noqa: E402

_ASOF = "2026-11-03"
_NAMES = {"512800": "银行ETF华宝", "510300": "沪深300ETF"}
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
    tmp = Path(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)
    _synthetic_prices().to_csv(tmp / "etf_prices.csv", index=False)
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
    original = F.overlap_mod.load_overlap_inputs
    F.overlap_mod.load_overlap_inputs = lambda **kw: (_HOLDINGS, _synthetic_prices())
    try:
        return F.build_plan(args)
    finally:
        F.overlap_mod.load_overlap_inputs = original


#: TA 喊 Sell（**有**评级、**没有**数字上限）⇒ 兜底
_DECISION_SELL_NO_PCT = (
    "## 六、最终交易决定\n\n"
    "**Rating**: Sell\n\n"
    "**Action**: Sell\n\n"
    "- 组合层权益敞口应当显著降低，其余留现金。\n"
)


# ───────────────────────── ① 纯函数 ─────────────────────────
def test_看空且无数字上限_套兜底上限():
    for rating in ("Sell", "Underweight", "卖出"):
        out = F._resolve_ta_equity_cap(
            {"available": True, "post_trade": {}, "rating": rating})
        assert out["reason"] == "bearish_fallback_cap", (rating, out)
        assert abs(float(out["pct"]) - float(C.MACRO_DERISK_EQUITY_CAP_MAX)) < 1e-12, out
        assert out["evidence"], out


def test_看空但给了数字_走正常口径不套兜底():
    out = F._resolve_ta_equity_cap(
        {"available": True, "post_trade": {"equity_target_pct": 30.0},
         "rating": "Sell"})
    assert out["reason"] == "accepted", out
    assert abs(float(out["pct"]) - 0.30) < 1e-12, out


def test_看多或中性_不套兜底():
    for rating in ("Buy", "Overweight", "Hold", "Neutral"):
        out = F._resolve_ta_equity_cap(
            {"available": True, "post_trade": {}, "rating": rating})
        assert out["reason"] == "not_a_target", (rating, out)
        assert out["pct"] is None, (rating, out)


def test_没有TA决策_不套兜底():
    out = F._resolve_ta_equity_cap({"available": False, "post_trade": {}})
    assert out["reason"] == "no_ta_advice", out
    assert out["pct"] is None, out


def test_决策件Action行看空_也套兜底(tmp_path):
    f = Path(tmp_path) / "decision.md"
    f.write_text(_DECISION_SELL_NO_PCT, encoding="utf-8")
    out = F._resolve_ta_equity_cap(
        {"available": True, "post_trade": {}, "source_file": str(f)})
    assert out["reason"] == "bearish_fallback_cap", out
    assert abs(float(out["pct"]) - float(C.MACRO_DERISK_EQUITY_CAP_MAX)) < 1e-12, out


def test_真实决策件_抽到评级且无数字上限():
    raw = llm_final.extract_final_decision(_DECISION_SELL_NO_PCT)
    assert raw.get("available"), raw
    assert (raw.get("rating") or "").strip().lower() == "sell", raw
    out = F._resolve_ta_equity_cap(raw)
    assert out["reason"] == "bearish_fallback_cap", out


# ───────────────────────── ② 端到端 ─────────────────────────
def test_端到端_看空无数字上限_权益被压到兜底上限(tmp_path):
    plan = _plan(tmp_path, _DECISION_SELL_NO_PCT)
    scr = plan.get("ta_screening") or {}
    rec = scr.get("equity_cap_ta_recommendation") or {}
    assert rec.get("reason") == "bearish_fallback_cap", rec
    cap = float(C.MACRO_DERISK_EQUITY_CAP_MAX)
    assert abs(float(scr.get("equity_cap_effective") or 0.0) - cap) < 1e-9, scr
    total = sum(float(p.get("target_weight") or 0.0) for p in plan.get("actions") or [])
    assert total <= cap + 1e-9, f"计划权益合计 {total} 超过兜底上限 {cap}"
    assert abs(total - cap) < 1e-9, f"计划权益合计 {total}，应恰好落在 {cap}"
