#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-18（用户目标文档 §二.2）：**Final Theme Guard**（最终清单同主题只留 1 只）。

【钉的是什么】
风控规则 `MAX_PER_THEME = 1` 本已挡在候选池层，但
「TA 否决 → 孤儿维持 → 顺延补位」这条拼接链**没有再校验最终清单**
⇒ 实测（2026-11-18 V1 臂）：最终同时持有 `512800`（银行 23.43%）+
`515020`（银行 15.69%）⇒ **单一银行板块 39.1%**。

【本文件钉三条】
① 端到端：孤儿维持行（真实持仓）占着某族 ⇒ **顺延补位不得再进同一族**；
② 守卫本体：同主题的**新买行**必须被拦下（目标权重归 0），
   **真实持仓优先**（不许市值凭空蒸发），同主题多只真实持仓 ⇒ 如实披露；
③ 不许替用户做卖出决策（真实持仓两只同族 ⇒ 两只都留 + 披露集中度）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402
import final_rebalance_plan as F  # noqa: E402

_ASOF = "2026-11-18"

#: 规则层动作（合成）：3 席各 16%（512800 已持有 ⇒ current_weight 非 0）
_ACTIONS = [
    {"code": "512800", "name": "银行ETF华宝", "action": "买入",
     "target_weight": 0.16, "current_weight": 0.2343},
    {"code": "515450", "name": "红利低波ETF", "action": "买入",
     "target_weight": 0.16, "current_weight": 0.0},
    {"code": "159207", "name": "半导体ETF", "action": "买入",
     "target_weight": 0.16, "current_weight": 0.0},
]

#: 候选池：3 只已入选 + 2 只未入选（515020 银行 / 512890 证券）
_CANDIDATES = [
    {"code": "512800", "name": "银行ETF华宝", "final_score": 1.90,
     "theme_id": "银行", "selected": True, "above_ma60": True,
     "entry_ma_ok": True, "broken": False},
    {"code": "515450", "name": "红利低波ETF", "final_score": 1.80,
     "theme_id": "红利低波", "selected": True, "above_ma60": True,
     "entry_ma_ok": True, "broken": False},
    {"code": "159207", "name": "半导体ETF", "final_score": 1.70,
     "theme_id": "半导体", "selected": True, "above_ma60": True,
     "entry_ma_ok": True, "broken": False},
    # 未入选：515020 的最终分**最高** ⇒ 旧口径会先挑它（银行）⇒ 复现 39.1% 缺陷
    {"code": "515020", "name": "银行ETF易方达", "final_score": 1.95,
     "theme_id": "银行", "selected": False, "above_ma60": True,
     "entry_ma_ok": True, "broken": False},
    {"code": "512890", "name": "证券ETF", "final_score": 1.80,
     "theme_id": "证券", "selected": False, "above_ma60": True,
     "entry_ma_ok": True, "broken": False},
]

#: TA 一票否决：512800（**在持仓** ⇒ 孤儿维持行）+ 159207（不在持仓 ⇒ 顺延补位）
_REJECTIONS = [
    {"code": "512800", "reason": "银行板块拥挤", "source_section": "最终交易决定"},
    {"code": "159207", "reason": "行业景气度不足", "source_section": "最终交易决定"},
]

_NAMES = {c["code"]: c["name"] for c in _CANDIDATES}
_UNIVERSE = set(_NAMES)


def _screening(monkeypatch, actions=None, candidates=None, rejections=None,
               held=("512800",), current_weights=None, max_per_theme=None):
    """跑一次 `apply_ta_screening`（重合度算不出来 ⇒ 视作「无冲突对」，不编数据）。

    🔴 2026-11-25（V2 冻结件接缝）：``max_per_theme`` = **本轮生效**的同族上限
    （``None`` ⇒ 回落 ``config.MAX_PER_THEME`` ⇒ 行为与今天逐字相同；
    **0 ＝ 不限** ⇒ 同族可占第二槽）。
    """
    monkeypatch.setattr(F.overlap_mod, "compute_overlap_pairs",
                        lambda **kw: [])
    extra = {} if max_per_theme is None else {"max_per_theme": max_per_theme}
    return F.apply_ta_screening(
        actions=actions if actions is not None else _ACTIONS,
        candidates=candidates if candidates is not None else _CANDIDATES,
        rejections=rejections if rejections is not None else _REJECTIONS,
        held_codes=list(held),
        current_weights=current_weights if current_weights is not None
        else {"512800": 0.2343},
        holdings=object(),          # 非 None ⇒ 走「重合度数据可用」分支
        prices=None,
        asof=_ASOF,
        names=_NAMES,
        equity_cap=0.8,
        universe_codes=set(_UNIVERSE),
        official_names=_NAMES,
        **extra,
    )


# ───────────────────────── ① 端到端：同族不得重复 ─────────────────────────
def test_端到端_孤儿维持行占族_顺延补位不得同族(monkeypatch):
    res = _screening(monkeypatch)
    scr = res["ta_screening"]
    final_rows = list(res["actions"]) + list(scr.get("orphaned_holding_rows") or [])
    themes = {}
    for r in final_rows:
        tw = float(r.get("target_weight") or 0.0)
        if tw <= 0:
            continue
        code = str(r.get("code") or "").zfill(6)
        theme = next((c["theme_id"] for c in _CANDIDATES if c["code"] == code), code)
        themes.setdefault(theme, []).append(code)
    for theme, codes in themes.items():
        assert len(codes) == 1, f"主题「{theme}」最终清单出现多只：{codes}"
    # 🔴 旧口径会把 515020（银行）当顺延补位 ⇒ 银行两只（512800 + 515020）
    assert "515020" not in [str(r.get("code")) for r in res["actions"]], res["actions"]
    # 顺延补位确实发生了（补的是**不同族**的 512890）
    assert "512890" in [str(r.get("code")) for r in res["actions"]], res["actions"]


def test_端到端_披露里有守卫字段(monkeypatch):
    res = _screening(monkeypatch)
    guard = res["ta_screening"].get("theme_guard")
    assert isinstance(guard, dict), res["ta_screening"].keys()
    #: 🔴 2026-11-25（V2 接缝）：守卫披露的必须是**生效值**
    #:    （不传 ⇒ 回落 `config.MAX_PER_THEME`）
    assert int(guard["max_per_theme"]) == int(C.MAX_PER_THEME), guard
    assert "theme_map" in guard and "blocked" in guard, guard


# ───────────── ② 生效同族上限（V2 冻结件接缝 · 2026-11-25）─────────────
def test_生效值等于传入值(monkeypatch):
    """🔴 守卫披露的 `max_per_theme` ＝ **本轮生效值**（＝ 传进来的值）。"""
    res = _screening(monkeypatch, max_per_theme=3)
    guard = res["ta_screening"]["theme_guard"]
    assert int(guard["max_per_theme"]) == 3, guard
    assert int(res["ta_screening"]["max_per_theme_effective"]) == 3, res["ta_screening"]


def test_缺省回落config(monkeypatch):
    """🔴 不传 `max_per_theme` ⇒ 回落 `config.MAX_PER_THEME`（**旧行为逐字不变**）。"""
    res = _screening(monkeypatch)
    guard = res["ta_screening"]["theme_guard"]
    assert int(guard["max_per_theme"]) == int(C.MAX_PER_THEME), guard
    assert int(res["ta_screening"]["max_per_theme_effective"]) == int(C.MAX_PER_THEME)


def test_生效值0时同族可占第二槽_但顺延替换纪律不变(monkeypatch):
    """🔴 生效值 **0 ＝ 不限** ⇒ **最终清单**的同族配额不拦（守卫不拦）；
    但**顺延的同族替换纪律不变**（顺延者仍须与已入选集合**及被否决者**不同族）。

    🔴 2026-11-25 审计修正：本条原先断言「上限 0 时允许顺延同族」——**那条是错的**。
    「同族上限=0」只放开**同族配额**（最终清单 + 顺延的同族计数闸门）；
    顺延的**替换纪律**（否则「否决一只银行 ETF、换来另一只银行 ETF」⇒ 否决形同虚设）
    与同族上限**无关**，由三处钉死：① `apply_ta_screening` docstring；
    ② 探针 A3「顺延结果必须与已入选集合…不同族」＋ A5 变异负例；
    ③ 提示词 3.5 小节（`max_per_theme == 0` 分支**仍**写
    「顺延者必须与已入选集合**不同族**」⇒ `test_ta_context_duties.py` 钉死）。
    ⇒ 代码必须与之一致（否则「提示词说一套、代码做另一套」）。
    """
    res = _screening(monkeypatch, max_per_theme=0)
    guard = res["ta_screening"]["theme_guard"]
    assert int(guard["max_per_theme"]) == 0, guard
    assert guard["blocked"] == [], guard
    codes = [str(r.get("code")) for r in res["actions"]]
    #: 🔴 顺延仍**必须**不同族 ⇒ 515020（银行，与被否决的 512800 同族）**不许**补位
    assert "515020" not in codes, res["actions"]
    #: 补位的是**不同族**的 512890（证券）
    assert "512890" in codes, res["actions"]


def test_守卫本体_生效值0时同族两只都留():
    """🔴 `_final_theme_guard` 本体：生效值 0 ⇒ 同族两只新买**都留**。"""
    kept = [
        {"code": "515020", "name": "银行ETF易方达", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
        {"code": "512800", "name": "银行ETF华宝", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
    ]
    out = F._final_theme_guard(
        kept, [], theme_of=_themes({"515020": "银行", "512800": "银行"}),
        max_per_theme=0,
        score_of=lambda c: {"515020": 1.95, "512800": 1.90}.get(c, 0.0))
    assert out["blocked"] == [], out
    assert float(kept[0]["target_weight"]) == 0.16, kept
    assert float(kept[1]["target_weight"]) == 0.16, kept


def test_守卫本体_缺省回落config():
    """🔴 `_final_theme_guard` 不传 `max_per_theme` ⇒ 回落 `config.MAX_PER_THEME`。"""
    kept = [
        {"code": "515020", "name": "银行ETF易方达", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
        {"code": "512800", "name": "银行ETF华宝", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
    ]
    out = F._final_theme_guard(
        kept, [], theme_of=_themes({"515020": "银行", "512800": "银行"}),
        score_of=lambda c: {"515020": 1.95, "512800": 1.90}.get(c, 0.0))
    assert int(out["max_per_theme"]) == int(C.MAX_PER_THEME), out
    assert [b["code"] for b in out["blocked"]] == ["512800"], out


# ───────────────────────── ② 守卫本体 ─────────────────────────
def _themes(mapping: dict):
    return lambda code: mapping.get(str(code).zfill(6), str(code))


def test_守卫本体_同主题新买行被拦下_真实持仓保留():
    kept = [{"code": "515020", "name": "银行ETF易方达", "action": "买入",
             "target_weight": 0.16, "current_weight": 0.0}]
    orphans = [{"code": "512800", "name": "银行ETF华宝",
                "target_weight": 0.2343, "current_weight": 0.2343}]
    out = F._final_theme_guard(
        kept, orphans, theme_of=_themes({"515020": "银行", "512800": "银行"}),
        max_per_theme=1, score_of=lambda c: 1.95)
    assert out["applied"] is True, out
    assert float(kept[0]["target_weight"]) == 0.0, kept
    assert kept[0].get("theme_guard_blocked") is True, kept
    assert [b["code"] for b in out["blocked"]] == ["515020"], out
    assert out["blocked"][0]["kept_codes"] == ["512800"], out
    # 真实持仓的权重**不许**被动（不许市值凭空蒸发）
    assert float(orphans[0]["target_weight"]) == 0.2343, orphans


def test_守卫本体_两只真实持仓同族_都留并披露集中度():
    kept = []
    orphans = [
        {"code": "512800", "name": "银行ETF华宝", "target_weight": 0.20,
         "current_weight": 0.20},
        {"code": "515020", "name": "银行ETF易方达", "target_weight": 0.10,
         "current_weight": 0.10},
    ]
    out = F._final_theme_guard(
        kept, orphans, theme_of=_themes({"512800": "银行", "515020": "银行"}),
        max_per_theme=1, score_of=lambda c: 1.0)
    assert out["blocked"] == [], out
    assert out["concentration_breaches"], out
    assert float(orphans[0]["target_weight"]) == 0.20, orphans
    assert float(orphans[1]["target_weight"]) == 0.10, orphans


def test_守卫本体_两只新买同族_留最终分高的():
    kept = [
        {"code": "515020", "name": "银行ETF易方达", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
        {"code": "512800", "name": "银行ETF华宝", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
    ]
    out = F._final_theme_guard(
        kept, [], theme_of=_themes({"515020": "银行", "512800": "银行"}),
        max_per_theme=1,
        score_of=lambda c: {"515020": 1.95, "512800": 1.90}.get(c, 0.0))
    assert float(kept[0]["target_weight"]) == 0.16, kept
    assert float(kept[1]["target_weight"]) == 0.0, kept
    assert [b["code"] for b in out["blocked"]] == ["512800"], out
