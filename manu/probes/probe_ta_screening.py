#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🛡️ **TA 合规筛选（否决 / 顺延）**闸门探针。

【为什么要有这个探针】
TradingAgents（TA）承担**合规筛选**：审查 → **一票否决** → **顺延**。
这条链路横跨三个 owner，必须逐条钉死，否则会静默失真：

  ① `overlap.py`：候选池两两重合度（`rule_conflict` = 规则层现役判据；
     `screen_flag` = 更宽口径的**预警**，只提示、不改判）；
  ② `llm_final.extract_rejections`：从决策原文里**保守地**抽出「点名否决某只标的」；
  ③ `final_rebalance_plan.apply_ta_screening`：剔除被否决者 + 顺延补位
     （**不是卖出**；顺延者必须**不同主题**、不与已入选集合构成规则层冲突对）。

🔴 **本探针不依赖网络、不依赖服务器产物** ⇒ 合成夹具自给自足
（成分权重、日线、候选池、决策原文全部在本地造）。

【断言清单】
A1  合成夹具：两只 ETF 成分完全重合 ⇒ `weight_overlap` ≈ 1.0 且 `rule_conflict=True`
A2  `extract_rejections`：剔除句**命中**；否定式 / 范围级 / 「否决权」**不命中**；
    同句多代码**都**命中
A3  顺延结果必须与「剔除后仍入选的集合」**不构成 rule_conflict** 且**不同族**
A4  `substituted_for` 必须指向被否决代码；**没有**顺延时 `no_substitute_reason` 必须非空
A5  **变异负例**：关掉「顺延时跳过同族」⇒ 顺延结果必须违反同族约束（否则断言没牙）
A6  剔除**不是**「权重 0」（被否决者不在 action 列表里）；顺延者权重 = 被否决者权重
A7  端到端：`build_plan` + `render_markdown` ⇒ md 必须含「🛡️ TA 合规筛选（否决 / 顺延）」章节
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import final_rebalance_plan as F  # noqa: E402
import llm_final  # noqa: E402
import overlap as OV  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """打一条断言结果。

    🔴 必须带 `[PASS]` / `[FAIL]` 字面量（`run_gates.sh` 靠 `grep -c '\\[PASS\\]'` 计数）。
    """
    (PASS if ok else FAIL).append(name)
    mark = "[PASS]" if ok else "[FAIL]"
    emoji = "✅" if ok else "❌"
    print(f"  {mark} {emoji} {name}" + (f" —— {detail}" if detail else ""))


# ---------------------------------------------------------------- 合成夹具

#: asof（信号日）
_ASOF = "2026-11-03"

#: 成分权重（合成）：
#: - 512800 / 512890 成分**完全相同** ⇒ 权重重叠 = 1.0 ⇒ 规则层冲突对；
#: - 512880 / 510300 成分**完全相同** ⇒ 构成冲突对；
#: - 512000 成分独立 ⇒ 谁也不冲突。
_HOLDINGS = pd.DataFrame([
    {"etf_code": "512800", "stock_code": "600000", "weight_pct": 50.0},
    {"etf_code": "512800", "stock_code": "600001", "weight_pct": 50.0},
    {"etf_code": "512890", "stock_code": "600000", "weight_pct": 50.0},
    {"etf_code": "512890", "stock_code": "600001", "weight_pct": 50.0},
    {"etf_code": "512880", "stock_code": "600003", "weight_pct": 60.0},
    {"etf_code": "512880", "stock_code": "600004", "weight_pct": 40.0},
    {"etf_code": "510300", "stock_code": "600003", "weight_pct": 60.0},
    {"etf_code": "510300", "stock_code": "600004", "weight_pct": 40.0},
    {"etf_code": "512000", "stock_code": "600005", "weight_pct": 100.0},
])

#: 名称（名称族判定用）
_NAMES = {
    "512800": "银行ETF华宝",
    "512890": "银行ETF华夏",
    "512880": "证券ETF国泰",
    "510300": "沪深300ETF",
    "512000": "券商ETF华夏",
}

#: 候选池（合成）
_CANDIDATES = [
    {"code": "512800", "name": "银行ETF华宝", "final_score": 1.8, "theme_id": "银行",
     "selected": True, "above_ma60": True, "entry_ma_ok": True, "broken": False},
    {"code": "510300", "name": "沪深300ETF", "final_score": 1.7, "theme_id": "510300",
     "selected": True, "above_ma60": True, "entry_ma_ok": True, "broken": False},
    {"code": "512890", "name": "银行ETF华夏", "final_score": 1.5, "theme_id": "银行",
     "selected": False, "above_ma60": True, "entry_ma_ok": True, "broken": False},
    {"code": "512880", "name": "证券ETF国泰", "final_score": 1.3, "theme_id": "券商",
     "selected": False, "above_ma60": True, "entry_ma_ok": True, "broken": False},
    {"code": "512000", "name": "券商ETF华夏", "final_score": 1.2, "theme_id": "券商",
     "selected": False, "above_ma60": True, "entry_ma_ok": True, "broken": False},
]

#: 规则层动作（合成）：512800 / 510300 各 20%
_ACTIONS = [
    {"code": "512800", "name": "银行ETF华宝", "action": "买入",
     "target_weight": 0.2, "current_weight": 0.0},
    {"code": "510300", "name": "沪深300ETF", "action": "买入",
     "target_weight": 0.2, "current_weight": 0.0},
]

#: TA 的否决（合成）
_REJECTIONS = [{
    "code": "512800", "name": "银行ETF华宝",
    "reason": "本轮建议剔除 512800 银行ETF华泰柏瑞",
    "source_section": "六、最终交易决定",
}]


def _synthetic_prices() -> pd.DataFrame:
    """合成日线（512800 与 512890 完全同涨 ⇒ 相关性 ≈ 1.0）。"""
    dates = pd.bdate_range("2025-01-01", periods=260)
    rng = np.random.default_rng(7)
    rows: list[dict] = []
    base = _synthetic_prices._base  # type: ignore[attr-defined]
    for code, factor in (("512800", 1.0), ("512890", 1.0), ("512880", 1.0),
                         ("510300", 1.0), ("512000", 1.0)):
        rets = rng.normal(0.0, 0.01, len(dates))
        if code in ("512800", "512890"):
            rets = base
        px = 1.0 * np.cumprod(1 + rets)
        for d, p in zip(dates, px):
            rows.append({"date": d.strftime("%Y-%m-%d"), "code": code,
                         "open": p, "high": p, "low": p, "close": p,
                         "volume": 1e6, "amount": 1e8})
    return pd.DataFrame(rows)


_synthetic_prices._base = np.random.default_rng(11).normal(0.0, 0.01, 260)  # type: ignore[attr-defined]


def _screen(**kwargs) -> dict:
    """跑一次合规筛选（夹具自带）。"""
    payload = dict(
        actions=_ACTIONS,
        candidates=_CANDIDATES,
        rejections=_REJECTIONS,
        held_codes=["512800"],
        current_weights={"512800": 0.2},
        holdings=_HOLDINGS,
        prices=_synthetic_prices(),
        asof=_ASOF,
        names=_NAMES,
    )
    payload.update(kwargs)
    return F.apply_ta_screening(**payload)


# ---------------------------------------------------------------- A 类

def _run_a_class() -> None:
    print("── A1 · 重合度筛查（合成夹具）──")
    pairs = OV.compute_overlap_pairs(
        holdings=_HOLDINGS, prices=_synthetic_prices(),
        codes=["512800", "512890", "510300", "512000"],
        asof=_ASOF, names=_NAMES,
    )
    by_pair = {(p["a"], p["b"]): p for p in pairs}
    pair = by_pair.get(("512800", "512890"))
    ok = pair is not None and abs(float(pair["weight_overlap"]) - 1.0) < 1e-6
    check("A1a 成分完全重合 ⇒ weight_overlap ≈ 1.0",
          ok, f"实测 {None if pair is None else pair['weight_overlap']}")
    ok = pair is not None and bool(pair["rule_conflict"])
    check("A1b 成分完全重合 ⇒ rule_conflict = True（规则层现役判据）",
          ok, f"实测 {None if pair is None else pair['rule_conflict']}")
    ok = pair is not None and float(pair["corr120"]) > 0.95
    check("A1c 同涨 ⇒ corr120 > 0.95", ok,
          f"实测 {None if pair is None else pair['corr120']}")
    ok = pair is not None and bool(pair["screen_flag"]) and bool(pair["same_theme"])
    check("A1d 同族 ⇒ screen_flag = True（更宽口径的预警）", ok,
          f"实测 screen_flag={None if pair is None else pair['screen_flag']}"
          f"｜same_theme={None if pair is None else pair['same_theme']}")
    # 独立成分 ⇒ 不冲突
    other = by_pair.get(("512800", "512000"))
    ok = other is not None and not bool(other["rule_conflict"])
    check("A1e 成分独立 ⇒ rule_conflict = False", ok,
          f"实测 {None if other is None else other['rule_conflict']}")


def _run_b_class() -> None:
    print("── A2 · 否决解析（合成原文）──")
    md_hit = ("## 六、最终交易决定\n\n本轮建议剔除 512800 银行ETF华泰柏瑞，其余维持不变。\n")
    md_neg = "## 六、最终交易决定\n\n512800 继续持有，不否决该标的。\n"
    md_scope = "## 六、最终交易决定\n\n本轮所有红利类标的全部不考虑。\n"
    md_veto = ("## 六、最终交易决定\n\nTA 拥有一票否决权，可在必要时否决 512800 等任一标的。\n")
    md_multi = "## 六、最终交易决定\n\n剔除 512800 与 512890，两只均不建仓。\n"

    codes = [r["code"] for r in llm_final.extract_rejections(md_hit)]
    check("A2a 剔除句 ⇒ 命中", codes == ["512800"], f"实测 {codes}")
    codes = [r["code"] for r in llm_final.extract_rejections(md_neg)]
    check("A2b 否定式（「不否决」）⇒ 不命中", codes == [], f"实测 {codes}")
    codes = [r["code"] for r in llm_final.extract_rejections(md_scope)]
    check("A2c 范围级表述 ⇒ 不命中", codes == [], f"实测 {codes}")
    codes = [r["code"] for r in llm_final.extract_rejections(md_veto)]
    check("A2d 「一票否决权」措辞 ⇒ 不命中", codes == [], f"实测 {codes}")
    codes = [r["code"] for r in llm_final.extract_rejections(md_multi)]
    check("A2e 同句多代码 ⇒ 都命中", codes == ["512800", "512890"], f"实测 {codes}")
    rows = llm_final.extract_rejections(md_hit)
    ok = bool(rows) and bool(rows[0].get("reason")) and bool(rows[0].get("source_section"))
    check("A2f 命中项必须带原文证据句与出处章节", ok,
          f"实测 reason={bool(rows) and bool(rows[0].get('reason'))}"
          f"｜section={bool(rows) and rows[0].get('source_section')}")


def _run_c_class() -> None:
    print("── A3/A4/A6 · 顺延结果（合成夹具）──")
    out = _screen()
    scr = out["ta_screening"]
    subs = scr.get("substituted") or []
    check("A4a 顺延记录里 substituted_for 必须指向被否决代码",
          bool(subs) and all(s.get("substituted_for") == "512800" for s in subs),
          f"实测 {[s.get('substituted_for') for s in subs]}")
    check("A4b 被否决代码必须进 rejected 名单（带理由与出处章节）",
          [r.get("code") for r in scr.get("rejected") or []] == ["512800"]
          and bool((scr.get("rejected") or [{}])[0].get("reason"))
          and bool((scr.get("rejected") or [{}])[0].get("source_section")),
          f"实测 {[r.get('code') for r in scr.get('rejected') or []]}")

    # 顺延者必须与「剔除后仍入选的集合」不冲突、不同族
    codes_left = [str(a["code"]) for a in out["actions"]]
    sel = [c for c in codes_left if c != "512000"]
    banned_themes = {"银行", "510300"}
    pairs = OV.compute_overlap_pairs(
        holdings=_HOLDINGS, prices=_synthetic_prices(),
        codes=sorted(set(sel) | {"512000"}), asof=_ASOF, names=_NAMES)
    conflict = {(p["a"], p["b"]) for p in pairs if p["rule_conflict"]}
    conflict |= {(p["b"], p["a"]) for p in pairs if p["rule_conflict"]}
    bad = [s for s in subs
           if any((s["code"], x) in conflict for x in sel)
           or s.get("theme") in banned_themes]
    check("A3 顺延结果必须与已入选集合不冲突（无 rule_conflict）且不同族",
          bool(subs) and not bad,
          f"顺延={[s.get('code') for s in subs]}｜已入选={sel}｜违例={bad}")

    # 剔除 ≠ 权重 0：被否决者不在 action 列表里
    ok = "512800" not in codes_left
    check("A6a 剔除 ≠ 「权重 0」⇒ 被否决者不得留在 action 列表里", ok,
          f"实测 action 列表={codes_left}")
    tw = {str(a["code"]): a.get("target_weight") for a in out["actions"]}
    ok = bool(subs) and abs(float(tw.get("512000", -1)) - 0.2) < 1e-9
    check("A6b 顺延者目标权重 = 被否决者目标权重（等权口径）", ok,
          f"实测 顺延={tw.get('512000')}｜被否决者=0.2")
    check("A6c held_not_touched 必须为 True（被否决者在持仓里 ⇒ 不动持仓）",
          bool(scr.get("held_not_touched")), f"实测 {scr.get('held_not_touched')}")

    # 没有顺延时：no_substitute_reason 必须非空
    same_theme_only = [c for c in _CANDIDATES if c["code"] != "512000"
                       and c["code"] != "510300" and c["code"] != "512880"]
    out2 = _screen(candidates=same_theme_only)
    scr2 = out2["ta_screening"]
    check("A4c 没有顺延者 ⇒ no_substitute_reason 必须非空",
          not (scr2.get("substituted") or []) and bool(scr2.get("no_substitute_reason")),
          f"实测 substituted={scr2.get('substituted')}"
          f"｜reason={scr2.get('no_substitute_reason')}")


def _run_d_class() -> None:
    print("── A5 · 变异负例（把「顺延时跳过同族」关掉）──")
    original = F._SKIP_SAME_THEME_ON_SUBSTITUTE
    try:
        F._SKIP_SAME_THEME_ON_SUBSTITUTE = False
        mutant = _screen()
    finally:
        F._SKIP_SAME_THEME_ON_SUBSTITUTE = original
    subs = mutant["ta_screening"].get("substituted") or []
    viol = [s for s in subs if s.get("theme") in {"银行", "510300"}]
    check("A5 变异负例：关掉「跳过同族」⇒ 顺延结果必须违反同族约束（否则断言没牙）",
          bool(viol), f"变异体顺延={[s.get('code') for s in subs]}｜违例={viol}")
    # 复原后必须重新满足约束
    ok_subs = _screen()["ta_screening"].get("substituted") or []
    ok = bool(ok_subs) and not [s for s in ok_subs if s.get("theme") in {"银行", "510300"}]
    check("A5b 复原后顺延结果必须重新满足同族约束", ok,
          f"实测顺延={[s.get('code') for s in ok_subs]}")

    # 第二个变异：关掉「顺延时跳过规则层冲突对」⇒ 顺延结果必须与已入选集合冲突
    original_c = F._SKIP_CONFLICT_ON_SUBSTITUTE
    try:
        F._SKIP_CONFLICT_ON_SUBSTITUTE = False
        mutant_c = _screen()
    finally:
        F._SKIP_CONFLICT_ON_SUBSTITUTE = original_c
    subs_c = mutant_c["ta_screening"].get("substituted") or []
    left = [str(a["code"]) for a in mutant_c["actions"] if str(a["code"]) != "512000"]
    pairs_c = OV.compute_overlap_pairs(
        holdings=_HOLDINGS, prices=_synthetic_prices(),
        codes=sorted(set(left) | {"512000", "512880"}), asof=_ASOF, names=_NAMES)
    conf = {(p["a"], p["b"]) for p in pairs_c if p["rule_conflict"]}
    conf |= {(p["b"], p["a"]) for p in pairs_c if p["rule_conflict"]}
    viol_c = [s for s in subs_c
              if any((s["code"], x) in conf for x in left)]
    check("A5c 变异负例：关掉「跳过规则层冲突对」⇒ 顺延结果必须与已入选集合冲突（否则断言没牙）",
          bool(viol_c), f"变异体顺延={[s.get('code') for s in subs_c]}｜违例={viol_c}")


def _run_e_class() -> None:
    print("── A7 · 端到端（build_plan + render_markdown）──")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prices = _synthetic_prices()
        prices.to_csv(tmp / "etf_prices.csv", index=False)
        pd.DataFrame([{"code": c, "name": n, "last_price": 1.0,
                       "last_price_date": _ASOF} for c, n in _NAMES.items()]
                     ).to_csv(tmp / "etf_universe.csv", index=False)
        pd.DataFrame([{"code": "512800", "name": "银行ETF华宝", "shares": 1000,
                       "price": 1.0, "market_value": 1000.0}]) \
            .to_csv(tmp / "account.csv", index=False)
        (tmp / "decision.md").write_text(
            "## 六、最终交易决定\n\n本轮建议剔除 512800 银行ETF华泰柏瑞，其余维持不变。\n",
            encoding="utf-8")
        report = {
            "meta": {"asof": _ASOF,
                     "params": {"commission_bp": 1.5, "slippage_bp": 5.0,
                                "target_n_holdings": 2}},
            "current_holdings_diagnosis": {
                "total_assets": 30000.0, "cash": 20000.0, "available": True,
                "holdings": [{"code": "512800", "name": "银行ETF华宝", "shares": 1000,
                              "price": 1.0, "market_value": 1000.0, "pnl_pct": 0.01,
                              "current_weight": 1000.0 / 30000.0}]},
            "next_rebalance": {"target_date": "2026-12-01", "signal_date": _ASOF,
                               "execution_date": "2026-12-02", "trading_days_until": 20,
                               "rule": "月度首个交易日", "execution_lag_days": 1},
            "recommendation": {"actions": _ACTIONS, "hold_buffer": 0,
                               "weights_used": {"alpha": 0.6, "momentum": 0.4}},
            "candidates": _CANDIDATES,
            "risk_disclosure": [],
        }
        (tmp / "report.json").write_text(json.dumps(report, ensure_ascii=False),
                                         encoding="utf-8")
        args = argparse.Namespace(
            report=str(tmp / "report.json"), decision=str(tmp / "decision.md"),
            prices=str(tmp / "etf_prices.csv"), universe=str(tmp / "etf_universe.csv"),
            account=str(tmp / "account.csv"), out_dir=str(tmp),
        )
        # 🔴 成分权重在服务器路径上（本地没有）⇒ 探针用**合成夹具**顶上（只读、不写盘）
        original_loader = F.overlap_mod.load_overlap_inputs
        F.overlap_mod.load_overlap_inputs = lambda **kw: (_HOLDINGS, _synthetic_prices())
        try:
            plan = F.build_plan(args)
        finally:
            F.overlap_mod.load_overlap_inputs = original_loader
        md = F.render_markdown(plan)
        scr = plan.get("ta_screening") or {}
        check("A7a 计划产物必须带 ta_screening 字段",
              bool(scr) and bool(scr.get("enabled")),
              f"实测 enabled={scr.get('enabled')}")
        check("A7b 端到端：否决名单 = 512800",
              [r.get("code") for r in scr.get("rejected") or []] == ["512800"],
              f"实测 {[r.get('code') for r in scr.get('rejected') or []]}")
        check("A7c 端到端：顺延补位者 = 512000（不同主题、不冲突）",
              [s.get("code") for s in scr.get("substituted") or []] == ["512000"],
              f"实测 {[s.get('code') for s in scr.get('substituted') or []]}")
        ok = "## 二之四、🛡️ TA 合规筛选（否决 / 顺延）" in md
        check("A7d md 必须含「🛡️ TA 合规筛选（否决 / 顺延）」章节", ok,
              "章节缺失" if not ok else "章节在位")
        ok = ("512000" in md) and ("剔除" in md or "否决" in md)
        check("A7e md 章节必须列出否决名单与顺延记录", ok,
              "md 已同时出现顺延者 512000 与否决信息" if ok
              else "md 未同时出现否决与顺延信息")


def _run_f_class() -> None:
    """F · 🔴 2026-11-11（Beta 避险落地）：**TA 建议压降组合权益上限**被规则层采纳。"""
    print("── F1/F2 · Beta 避险（TA 建议 ⇒ 权益上限压降）──")

    def _plan_with(decision_text: str, tag: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            prices = _synthetic_prices()
            prices.to_csv(tmp / "etf_prices.csv", index=False)
            pd.DataFrame([{"code": c, "name": n, "last_price": 1.0,
                           "last_price_date": _ASOF} for c, n in _NAMES.items()]
                         ).to_csv(tmp / "etf_universe.csv", index=False)
            pd.DataFrame([{"code": "512800", "name": "银行ETF华宝", "shares": 1000,
                           "price": 1.0, "market_value": 1000.0}]) \
                .to_csv(tmp / "account.csv", index=False)
            (tmp / "decision.md").write_text(decision_text, encoding="utf-8")
            report = {
                "meta": {"asof": _ASOF,
                         "params": {"commission_bp": 1.5, "slippage_bp": 5.0,
                                    "target_n_holdings": 2}},
                "current_holdings_diagnosis": {
                    "total_assets": 30000.0, "cash": 20000.0, "available": True,
                    "holdings": [{"code": "512800", "name": "银行ETF华宝", "shares": 1000,
                                  "price": 1.0, "market_value": 1000.0, "pnl_pct": 0.01,
                                  "current_weight": 1000.0 / 30000.0}]},
                "next_rebalance": {"target_date": "2026-12-01", "signal_date": _ASOF,
                                   "execution_date": "2026-12-02", "trading_days_until": 20,
                                   "rule": "月度首个交易日", "execution_lag_days": 1},
                "recommendation": {"actions": _ACTIONS, "hold_buffer": 0,
                                   "weights_used": {"alpha": 0.6, "momentum": 0.4}},
                "candidates": _CANDIDATES,
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
                plan = F.build_plan(args)
            finally:
                F.overlap_mod.load_overlap_inputs = original_loader
            return plan

    def _sum_w(plan: dict) -> float:
        return sum(float(r.get("target_weight") or 0.0)
                   for r in (plan.get("actions") or []))

    #: TA 建议：组合权益**降至** 20%（目标动词口径 ⇒ 规则层必须采纳）
    _dec_reduce = ("## 六、最终交易决定\n\n"
                   "- 本轮建议剔除 512800 银行ETF华泰柏瑞。\n"
                   "- 组合层权益由 48.02% 适度降至约 **20%**，现金提高至约 **80%**。\n")
    plan = _plan_with(_dec_reduce, "reduce")
    scr = plan.get("ta_screening") or {}
    eff = float(scr.get("equity_cap_effective") or 0.0)
    total = _sum_w(plan)
    check("F1a TA 建议压降 ⇒ 生效权益上限 = 建议值（20%）",
          abs(eff - 0.20) < 1e-9, f"实测 equity_cap_effective={eff}")
    check("F1b TA 建议压降 ⇒ 计划权益合计 ≤ 生效上限",
          total <= eff + 1e-9, f"实测计划权益合计={total:.4f}｜上限={eff:.4f}")
    check("F1c `equity_cap` 必须恒为规则层硬上限 0.8（验收脚本按它断言）",
          abs(float(scr.get("equity_cap") or 0.0) - 0.8) < 1e-9,
          f"实测 equity_cap={scr.get('equity_cap')}")
    rec = scr.get("equity_cap_ta_recommendation") or {}
    check("F1d 有 TA 建议 ⇒ 必须披露**原文证据句**",
          bool(rec.get("evidence")) and rec.get("reason") == "accepted",
          f"实测 reason={rec.get('reason')}｜evidence={str(rec.get('evidence'))[:80]}")
    md = F.render_markdown(plan)
    check("F1e md 必须披露「Beta 避险」段",
          "Beta 避险" in md and "20.0%" in md,
          "章节/数字在位" if ("Beta 避险" in md and "20.0%" in md)
          else "章节/数字缺失")

    #: 无建议（只有陈述句）⇒ 完全保持原行为
    _dec_none = ("## 六、最终交易决定\n\n"
                 "- 本轮建议剔除 512800 银行ETF华泰柏瑞。\n"
                 "- 当前组合层权益敞口约 **56%**，维持现状。\n")
    plan_none = _plan_with(_dec_none, "none")
    scr_none = plan_none.get("ta_screening") or {}
    check("F2a 无目标口径建议 ⇒ 生效上限 = 规则层硬上限 0.8",
          abs(float(scr_none.get("equity_cap_effective") or 0.0) - 0.8) < 1e-9,
          f"实测 {scr_none.get('equity_cap_effective')}")
    check("F2b 无目标口径建议 ⇒ 权益**不被压降**（完全保持原行为）",
          _sum_w(plan_none) > _sum_w(plan) + 1e-9,
          f"实测无建议={_sum_w(plan_none):.4f}｜有建议={_sum_w(plan):.4f}")

    #: 🔴 变异负例：把建议解析打成「无建议」⇒ 权益**必须**不再被压降
    original_resolver = F._resolve_ta_equity_cap
    try:
        F._resolve_ta_equity_cap = lambda raw: {"pct": None, "evidence": "",
                                                 "reason": "no_ta_advice"}
        plan_mut = _plan_with(_dec_reduce, "mutant")
    finally:
        F._resolve_ta_equity_cap = original_resolver
    check("F3 变异负例：关掉建议解析 ⇒ 权益不再被压降（否则上面断言没牙）",
          _sum_w(plan_mut) > _sum_w(plan) + 1e-9,
          f"实测变异体={_sum_w(plan_mut):.4f}｜正常体={_sum_w(plan):.4f}")


def main() -> int:
    """跑全部断言并汇总（退出码 0 = 全过，1 = 有 FAIL）。"""
    print("=== 🛡️ TA 合规筛选（否决 / 顺延）· 闸门探针 ===")
    _run_a_class()
    _run_b_class()
    _run_c_class()
    _run_d_class()
    _run_e_class()
    _run_f_class()
    print(f"\n=== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    if FAIL:
        print("FAIL：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
