#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-11（Beta 避险落地）：**TA 建议压降组合权益上限**的规则层落地钉子。

【钉的是什么】
文档「三.3 极端行情下的仓位建议」：TA 可以建议**压降组合权益上限**（如 60%），
其余资金**留现金** ⇒ 规则层必须**采纳**这个建议（否则 TA 的建议只是「披露」，不是「建议」）。

① **只认目标动词**：`llm_final.post_trade.equity_target_pct`（「降至/定为/控制在…」）
   ⇒ 陈述句（「组合层权益约 56%」）**不算建议**；
② **只许压降、不许抬高**：建议 ≥ 规则层硬上限（1 − 现金预留比例 = 80%）⇒ **忽略**；
③ 解析不出 ⇒ **完全保持原行为**（产物逐字节不变）；
④ 席位目标权重**等比缩放**到「生效上限 − 维持行占额」，差额**留现金**；
⑤ 「维持」行（被 TA 否决但仍在持仓）**权重不动**；
⑥ `ta_screening.equity_cap` **恒为 0.8**（验收脚本按它断言，不许被 TA 改写）。
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

import final_rebalance_plan as F  # noqa: E402
import llm_final  # noqa: E402

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


def _plan(tmp_path: Path, decision_md: str, *, account_rows=None,
          holdings_rows=None, actions=None, candidates=None,
          extra_codes=()) -> dict:
    """用合成夹具跑一次 `build_plan`。"""
    tmp = Path(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)
    prices = _synthetic_prices()
    for code in extra_codes:
        sub = prices[prices["code"] == "512800"].copy()
        sub["code"] = code
        sub["close"] = sub["close"] * 1.37
        prices = pd.concat([prices, sub], ignore_index=True)
    prices.to_csv(tmp / "etf_prices.csv", index=False)
    _univ = dict(_NAMES)
    for code in extra_codes:
        _univ[code] = f"顺延测试{code}"
    pd.DataFrame([{"code": c, "name": n, "last_price": 1.0,
                   "last_price_date": _ASOF} for c, n in _univ.items()]
                 ).to_csv(tmp / "etf_universe.csv", index=False)
    pd.DataFrame(account_rows or [{"code": "512800", "name": "银行ETF华宝",
                                   "shares": 0, "price": 1.0, "market_value": 0.0}]
                 ).to_csv(tmp / "account.csv", index=False)
    (tmp / "decision.md").write_text(decision_md, encoding="utf-8")
    report = {
        "meta": {"asof": _ASOF,
                 "params": {"commission_bp": 1.5, "slippage_bp": 5.0,
                            "target_n_holdings": 2}},
        "current_holdings_diagnosis": {
            "total_assets": 30000.0, "cash": 30000.0, "available": True,
            "holdings": holdings_rows or [],
        },
        "next_rebalance": {"target_date": "2026-12-01", "signal_date": _ASOF,
                           "execution_date": "2026-12-02", "trading_days_until": 20,
                           "rule": "月度首个交易日", "execution_lag_days": 1},
        "recommendation": {"actions": (actions if actions is not None else _ACTIONS),
                           "hold_buffer": 0,
                           "weights_used": {"alpha": 0.6, "momentum": 0.4}},
        "candidates": (candidates if candidates is not None else [
            {"code": "512800", "name": "银行ETF华宝", "final_score": 1.8,
             "theme_id": "银行", "selected": True, "above_ma60": True,
             "entry_ma_ok": True, "broken": False},
            {"code": "510300", "name": "沪深300ETF", "final_score": 1.7,
             "theme_id": "沪深300", "selected": True, "above_ma60": True,
             "entry_ma_ok": True, "broken": False},
        ]),
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


#: TA 建议：组合权益**降至** 36%（目标动词口径）
_DECISION_REDUCE = (
    "## 六、最终交易决定\n\n"
    "- 组合层权益由 48.02% 适度降至约 **36%**，现金提高至约 **64%**。\n"
)
#: 只有**陈述句**（没有目标动词）⇒ 不算建议
_DECISION_STATEMENT = (
    "## 六、最终交易决定\n\n"
    "- 当前组合层权益敞口约 **56%**，维持现状。\n"
)
#: TA 建议**高于**规则层硬上限 ⇒ 忽略（只许压降）
_DECISION_ABOVE = (
    "## 六、最终交易决定\n\n"
    "- 组合层权益敞口定为 **90%**。\n"
)


def _sum_target_weight(plan: dict) -> float:
    return sum(float(p.get("target_weight") or 0.0) for p in plan.get("actions") or [])


# ───────────────────────── ① 解析口径（纯函数） ─────────────────────────
def test_解析_目标动词_才被当成建议():
    raw = llm_final.extract_final_decision(_DECISION_REDUCE)
    assert raw.get("available"), raw
    assert abs(float(raw["post_trade"]["equity_target_pct"]) - 36.0) < 1e-9, raw
    out = F._resolve_ta_equity_cap(raw)
    assert out["reason"] == "accepted", out
    assert abs(float(out["pct"]) - 0.36) < 1e-9, out
    assert out["evidence"], out


def test_解析_陈述句_不算建议():
    raw = llm_final.extract_final_decision(_DECISION_STATEMENT)
    assert raw.get("available"), raw
    out = F._resolve_ta_equity_cap(raw)
    assert out["reason"] == "not_a_target", out
    assert out["pct"] is None, out


def test_解析_无决策文件_完全保持原行为():
    out = F._resolve_ta_equity_cap({"available": False, "post_trade": {}})
    assert out["reason"] == "no_ta_advice", out
    assert out["pct"] is None, out


def test_解析_高于硬上限_忽略():
    raw = llm_final.extract_final_decision(_DECISION_ABOVE)
    out = F._resolve_ta_equity_cap(raw)
    assert out["reason"] == "at_or_above_base_cap", out
    assert float(out["pct"]) >= F._EQUITY_CAP, out


# ───────────────────────── ② 端到端（build_plan） ─────────────────────────
def test_端到端_TA建议36_权益被压降(tmp_path):
    plan = _plan(tmp_path, _DECISION_REDUCE)
    scr = plan.get("ta_screening") or {}
    assert abs(float(scr.get("equity_cap_effective") or 0) - 0.36) < 1e-9, scr
    total = _sum_target_weight(plan)
    assert total <= 0.36 + 1e-9, f"计划权益合计 {total} 超过生效上限 0.36"
    assert abs(total - 0.36) < 1e-9, f"计划权益合计 {total}，应恰好落在 0.36"
    # 🔴 `equity_cap` 恒为 0.8（验收脚本按它断言）
    assert abs(float(scr.get("equity_cap") or 0) - 0.8) < 1e-9, scr
    rec = scr.get("equity_cap_ta_recommendation") or {}
    assert rec.get("reason") == "accepted", rec
    assert rec.get("evidence"), rec


def test_端到端_无建议_完全保持原行为(tmp_path):
    plan = _plan(tmp_path, _DECISION_STATEMENT)
    scr = plan.get("ta_screening") or {}
    assert abs(float(scr.get("equity_cap_effective") or 0) - 0.8) < 1e-9, scr
    assert abs(_sum_target_weight(plan) - 0.8) < 1e-9, plan.get("actions")


def test_端到端_建议高于硬上限_忽略且不压降(tmp_path):
    plan = _plan(tmp_path, _DECISION_ABOVE)
    scr = plan.get("ta_screening") or {}
    assert abs(float(scr.get("equity_cap_effective") or 0) - 0.8) < 1e-9, scr
    assert abs(_sum_target_weight(plan) - 0.8) < 1e-9, plan.get("actions")
    rec = scr.get("equity_cap_ta_recommendation") or {}
    assert rec.get("reason") == "at_or_above_base_cap", rec


def test_端到端_维持行权重不动(tmp_path):
    """被 TA 否决但**仍在持仓**的代码 ⇒ 「维持」行目标权重 = 当前权重（缩放不许碰它）。"""
    account = [{"code": "512800", "name": "银行ETF华宝", "shares": 6000,
                "price": 1.0, "market_value": 6000.0}]
    holdings_rows = [{"code": "512800", "name": "银行ETF华宝", "shares": 6000,
                      "price": 1.0, "market_value": 6000.0, "pnl_pct": 0.01,
                      "current_weight": 6000.0 / 30000.0}]
    decision = (
        "## 六、最终交易决定\n\n"
        "- 剔除 512800 银行ETF华宝。\n"
        "- 组合层权益由 48.02% 适度降至约 **36%**，现金提高至约 **64%**。\n"
    )
    plan = _plan(tmp_path, decision, account_rows=account, holdings_rows=holdings_rows)
    rows = {str(p["code"]): p for p in plan.get("actions") or []}
    hold_row = rows.get("512800")
    assert hold_row is not None, rows
    assert hold_row.get("ta_rejected") is True, hold_row
    assert abs(float(hold_row["target_weight"]) - 6000.0 / 30000.0) < 1e-9, hold_row
    total = _sum_target_weight(plan)
    assert total <= 0.36 + 1e-9, f"计划权益合计 {total} 超过生效上限 0.36"


def test_端到端_md必须披露Beta避险(tmp_path):
    plan = _plan(tmp_path, _DECISION_REDUCE)
    md = F.render_markdown(plan)
    assert "Beta 避险" in md, md[:4000]
    assert "36.0%" in md, md[:4000]


# ───────────────────────── ③ 变异负例（断言必须有牙） ─────────────────────────
def test_变异负例_关掉建议解析_权益不再被压降(tmp_path, monkeypatch):
    """把 `_resolve_ta_equity_cap` 打成「无建议」⇒ 权益**必须**回到 80%
    ⇒ 证明上面那些断言真的在检验「采纳 TA 建议」这件事。"""
    plan_ok = _plan(tmp_path / "ok", _DECISION_REDUCE)
    assert _sum_target_weight(plan_ok) <= 0.36 + 1e-9
    monkeypatch.setattr(
        F, "_resolve_ta_equity_cap",
        lambda raw: {"pct": None, "evidence": "", "reason": "no_ta_advice"},
    )
    plan_mut = _plan(tmp_path / "mut", _DECISION_REDUCE)
    assert abs(_sum_target_weight(plan_mut) - 0.8) < 1e-9, plan_mut.get("plans")


# ───────────────────────── ④ 审计补钉（2026-11-11 独立审查） ─────────────────────────
def test_解析_预测句不算目标_不许把想加仓读成要减仓():
    """🔴 「回升至 56%」「回到 56%」是**预测/陈述**，**不是目标**。
    旧实现（裸介词「到/至」）会把它当成建议 ⇒ 规则层把权益从 80% **静默砍到 56%**。"""
    for sentence in (
        "- 极端反弹情形下，组合层权益将回升至 **56%**。",
        "- 组合层权益回到 **56%** 的初始水平即可。",
        "- 流动性改善后，组合层权益可回到 **56%**。",
    ):
        raw = llm_final.extract_final_decision(
            "## 六、最终交易决定\n\n" + sentence + "\n")
        assert raw.get("available"), raw
        out = F._resolve_ta_equity_cap(raw)
        assert out["pct"] is None, f"{sentence} ⇒ {out}"
        assert out["reason"] == "not_a_target", f"{sentence} ⇒ {out}"


def test_解析_反向动词不算目标():
    """「提高到 56%」是**加仓** ⇒ **不许**被读成「压降到 56%」（那会把仓位砍掉）。"""
    raw = llm_final.extract_final_decision(
        "## 六、最终交易决定\n\n- 组合层权益提高到 **56%**。\n")
    out = F._resolve_ta_equity_cap(raw)
    assert out["pct"] is None, out


def test_解析_上限类写法_能抽到():
    """「上限为 / 不超过」是**目标口径** ⇒ 必须抽得到（否则 TA 说了也不落地）。"""
    for sentence, expect in (
        ("- 组合层权益上限为 **40%**。", 40.0),
        ("- 组合层权益不超过 **40%**。", 40.0),
    ):
        raw = llm_final.extract_final_decision(
            "## 六、最终交易决定\n\n" + sentence + "\n")
        out = F._resolve_ta_equity_cap(raw)
        assert out["reason"] == "accepted", f"{sentence} ⇒ {out}"
        assert abs(float(out["pct"]) * 100 - expect) < 1e-6, f"{sentence} ⇒ {out}"


def test_解析_边界输入_一律不崩():
    """🔴 崩溃安全：None / 空串 / 非数字 / 越界 / 小数形式 ⇒ **安全返回**。"""
    cases = [
        {"available": True, "post_trade": {"equity_target_pct": None}},
        {"available": True, "post_trade": {"equity_target_pct": ""}},
        {"available": True, "post_trade": {"equity_target_pct": "abc"}},
        {"available": True, "post_trade": {"equity_target_pct": -1}},
        {"available": True, "post_trade": {"equity_target_pct": 100.1}},
        {"available": True, "post_trade": {"equity_target_pct": True}},
        {"available": True, "post_trade": {}},
        {"available": True, "post_trade": None},
        {"available": True},
        {},
        None,
    ]
    for raw in cases:
        out = F._resolve_ta_equity_cap(raw)
        assert out["reason"] in F._TA_CAP_REASONS, (raw, out)
        assert out["pct"] is None or 0.0 <= float(out["pct"]) <= 1.0, (raw, out)


def test_解析_小数形式判越界_不许静默压降100倍():
    """🔴 输入单位 = **百分点**；`0.36`（小数形式）无法与「0.36%」区分 ⇒ 判越界。"""
    out = F._resolve_ta_equity_cap(
        {"available": True, "post_trade": {"equity_target_pct": 0.36}})
    assert out["reason"] == "out_of_range", out
    assert out["pct"] is None, out


def test_端到端_顺延补位继承缩放后权重(tmp_path):
    """🔴 **最关键的一条**：席位等比缩放必须发生在 TA 筛选**之前**。

    ⇒ 顺延补位者拿到的是**缩放后**的权重（0.18），**不是**未缩放的 0.4；
    ⇒ 否则 Σtarget_weight = 0.4 + 0.18 = 0.58 > 生效上限 0.36（权益自相矛盾）。
    """
    actions = [
        {"code": "512800", "name": "银行ETF华宝", "action": "买入",
         "target_weight": 0.4, "current_weight": 0.0},
        {"code": "510300", "name": "沪深300ETF", "action": "买入",
         "target_weight": 0.4, "current_weight": 0.0},
    ]
    candidates = [
        {"code": "512800", "name": "银行ETF华宝", "final_score": 1.8,
         "theme_id": "银行", "selected": True, "above_ma60": True,
         "entry_ma_ok": True, "broken": False},
        {"code": "510300", "name": "沪深300ETF", "final_score": 1.7,
         "theme_id": "沪深300", "selected": True, "above_ma60": True,
         "entry_ma_ok": True, "broken": False},
        {"code": "159915", "name": "顺延测试159915", "final_score": 1.5,
         "theme_id": "顺延测试", "selected": False, "above_ma60": True,
         "entry_ma_ok": True, "broken": False},
    ]
    decision = (
        "## 六、最终交易决定\n\n"
        "- 剔除 510300 沪深300ETF。\n"
        "- 组合层权益由 48.02% 适度降至约 **36%**，现金提高至约 **64%**。\n"
    )
    plan = _plan(tmp_path, decision, actions=actions, candidates=candidates,
                 extra_codes=("159915",))
    rows = {str(p["code"]): p for p in plan.get("actions") or []}
    assert "159915" in rows, f"顺延补位未发生：{rows}"
    sub_w = float(rows["159915"]["target_weight"])
    assert abs(sub_w - 0.18) < 1e-9, f"补位者权重 {sub_w}，应为缩放后的 0.18"
    total = _sum_target_weight(plan)
    assert abs(total - 0.36) < 1e-9, f"Σtarget_weight={total}，应恰为 0.36"
    scr = plan.get("ta_screening") or {}
    assert abs(float(scr.get("equity_cap_effective") or 0) - 0.36) < 1e-9, scr


def test_变异负例_缩放挪到筛选之后_补位权重必须转红(tmp_path, monkeypatch):
    """🔴 断言必须有牙：把「缩放」挪到 TA 筛选之后 ⇒ 补位者权重必须变（不再 0.18）。"""
    actions = [
        {"code": "512800", "name": "银行ETF华宝", "action": "买入",
         "target_weight": 0.4, "current_weight": 0.0},
        {"code": "510300", "name": "沪深300ETF", "action": "买入",
         "target_weight": 0.4, "current_weight": 0.0},
    ]
    candidates = [
        {"code": "512800", "name": "银行ETF华宝", "final_score": 1.8,
         "theme_id": "银行", "selected": True, "above_ma60": True,
         "entry_ma_ok": True, "broken": False},
        {"code": "510300", "name": "沪深300ETF", "final_score": 1.7,
         "theme_id": "沪深300", "selected": True, "above_ma60": True,
         "entry_ma_ok": True, "broken": False},
        {"code": "159915", "name": "顺延测试159915", "final_score": 1.5,
         "theme_id": "顺延测试", "selected": False, "above_ma60": True,
         "entry_ma_ok": True, "broken": False},
    ]
    decision = (
        "## 六、最终交易决定\n\n"
        "- 剔除 510300 沪深300ETF。\n"
        "- 组合层权益由 48.02% 适度降至约 **36%**，现金提高至约 **64%**。\n"
    )
    plan = _plan(tmp_path, decision, actions=actions, candidates=candidates,
                 extra_codes=("159915",))
    rows = {str(p["code"]): p for p in plan.get("actions") or []}
    sub_w = float(rows["159915"]["target_weight"])

    _real = F.apply_ta_screening

    def _mut(**kwargs):
        """模拟「先筛选、后缩放」⇒ 补位者拿到未缩放的 0.4。"""
        actions_in = kwargs.get("actions") or []
        kwargs["actions"] = [
            {**a, "target_weight": 0.4} for a in actions_in]
        return _real(**kwargs)

    monkeypatch.setattr(F, "apply_ta_screening", _mut)
    plan_mut = _plan(tmp_path / "mut", decision, actions=actions,
                     candidates=candidates, extra_codes=("159915",))
    rows_mut = {str(p["code"]): p for p in plan_mut.get("actions") or []}
    if "159915" in rows_mut:
        sub_w_mut = float(rows_mut["159915"]["target_weight"])
    else:
        # 🔴 「先筛选、后缩放」的**真实后果**：补位会把权益顶到 0.8 > 生效上限 0.36
        #    ⇒ 补位被上限闸门挡下 ⇒ **席位空缺**（顺延彻底失效）。
        sub_w_mut = None
    assert sub_w_mut != sub_w, (
        f"变异体补位权重 {sub_w_mut} 与正确值 {sub_w} 相同 ⇒ 断言没有牙")
    assert sub_w_mut is None or abs(sub_w_mut - 0.4) < 1e-9, (
        f"变异体补位权重 {sub_w_mut}，应为 None（补位被上限挡下）或 0.4（未缩放）")
    assert abs(_sum_target_weight(plan_mut) - 0.36) > 1e-9, (
        "变异体的 Σtarget_weight 竟仍等于 0.36 ⇒ 断言没有牙")
