"""test_pool_whitelist.py —— **池内白名单** 与 **官方名优先** 的钉死测试。

【真机事故背景（2026-11-11 全量跑批实测）】
1. **池外垃圾代码进了否决名单**：TradingAgents 的权威裁决段落里把
   `516620`（影视ETF国泰）**笔误**写成 `566620`（**不在** ETF 池内）
   ⇒ 规则层「照单全收」⇒ 交付件的 `ta_screening.rejected` 里出现一只
   **根本不存在的标的** ⇒ 既污染产物，又让产物级闸门（A2）判成 `na`（假绿防护触发）。
2. **名称被抽成垃圾词**：`159547` 的 name 被抽成 `重叠`
   （原文「…与 512890/159547 **重叠** 0.974/0.978…」）
   ⇒ 交付件里出现一只**没有名字的基金**。

【钉死的纪律】
* **池外代码** ⇒ 不进 `ta_screening.rejected`，但**必须**进
  `ta_screening.rejected_out_of_universe` 如实披露（不许静默丢弃）；
* **白名单不可用**（`--universe` 缺失）⇒ **一个都不剔除**（fail-closed）
  ⇒ 绝不能因为白名单缺失而放过真否决；
* **官方名优先** ⇒ `--universe` 的 `name` 列盖住 LLM 抽出来的名字；
* **池内真否决行为一字不变**。
"""
from __future__ import annotations

import sys
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(RESEARCH_DIR))

import final_rebalance_plan as F  # noqa: E402


# =============================================================================
# 合成数据
# =============================================================================
#: 池内代码（真池：1664 个，这里只留相关的几个）
_POOL = {"516620", "159207", "563020", "512890", "159547", "512170", "512800"}

#: 官方名（`--universe` 的 name 列）
_OFFICIAL = {
    "516620": "影视ETF国泰",
    "159207": "高股息ETF广发",
    "563020": "红利低波ETF易方达",
    "512890": "红利低波ETF华泰柏瑞",
    "159547": "红利低波ETF华夏",
    "512170": "医疗ETF华宝",
    "512800": "银行ETF华宝",
}


def _rejections() -> list[dict]:
    """LLM 权威节解析出来的否决名单（含**笔误的池外代码** 566620）。"""
    return [
        {"code": "516620", "name": "影视ETF国泰", "reason": "E 为负",
         "source_section": "风险裁决"},
        {"code": "159207", "name": "高股息ETF广发", "reason": "最终分最弱",
         "source_section": "风险裁决"},
        {"code": "563020", "name": "红利低波ETF易方达", "reason": "拥挤 86% 分位",
         "source_section": "风险裁决"},
        {"code": "566620", "name": "", "reason": "566620（E=-1.2162…）均否决",
         "source_section": "风险裁决"},
        {"code": "512890", "name": "红利低波ETF华泰柏瑞", "reason": "同族重复",
         "source_section": "风险裁决"},
        {"code": "159547", "name": "重叠", "reason": "高度同质",
         "source_section": "风险裁决"},
    ]


def _actions() -> list[dict]:
    return [
        {"code": "512800", "name": "银行ETF华宝", "action": "卖出",
         "target_weight": 0.16, "current_weight": 0.222},
        {"code": "515450", "name": "红利低波50ETF南方", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
        {"code": "159207", "name": "高股息ETF广发", "action": "买入",
         "target_weight": 0.16, "current_weight": 0.0},
    ]


def _candidates() -> list[dict]:
    """顺延池（`report["candidates"]`）。"""
    return [
        {"code": "512170", "name": "医疗ETF华宝", "final_score": -0.947,
         "selected": False, "broken": False, "entry_ma_ok": True,
         "above_ma60": True, "coverage": 0.8, "theme_id": "医药"},
        {"code": "515650", "name": "消费50ETF富国", "final_score": 0.2,
         "selected": True, "broken": False, "entry_ma_ok": True,
         "above_ma60": True, "coverage": 0.8, "theme_id": "消费"},
    ]


# =============================================================================
# 单元：_pool_whitelist
# =============================================================================
def test_pool_whitelist_prefers_universe_csv():
    """事实源优先级：① `--universe` ⇒ ② 报告侧代码集合 ⇒ ③ 不可用（fail-closed）。"""
    universe = {c: {"name": _OFFICIAL.get(c, "")} for c in _POOL}
    codes, source = F._pool_whitelist({"candidates": _candidates()}, universe)
    assert codes is not None
    assert codes == _POOL
    assert "--universe" in source


def test_pool_whitelist_falls_back_to_report_side_codes():
    """`--universe` 不可用 ⇒ 回退报告侧代码集合（宁可多收、不漏收）。"""
    codes, source = F._pool_whitelist({"candidates": _candidates()}, {})
    assert codes is not None
    assert "512170" in codes and "515650" in codes
    assert "报告侧" in source


def test_pool_whitelist_unavailable_is_none():
    """两者都取不到 ⇒ None（fail-closed ⇒ 后续一个都不剔除）。"""
    codes, source = F._pool_whitelist({}, {})
    assert codes is None
    assert "不可用" in source


# =============================================================================
# 单元：_split_rejections_by_pool
# =============================================================================
def test_split_rejections_removes_pool_outside_code():
    """池外代码 ⇒ 剔除 + 如实披露；池内真否决**一个不少**。"""
    kept, out = F._split_rejections_by_pool(_rejections(), set(_POOL))
    kept_codes = {r["code"] for r in kept}
    assert "566620" not in kept_codes, "池外代码不得进否决名单"
    assert kept_codes == {"516620", "159207", "563020", "512890", "159547"}
    assert [r["code"] for r in out] == ["566620"]
    assert "池外" in out[0]["note"]


def test_split_rejections_is_fail_closed_without_whitelist():
    """白名单不可用 ⇒ **一个都不剔除**（不许因为白名单缺失而放过真否决）。"""
    kept, out = F._split_rejections_by_pool(_rejections(), None)
    assert len(kept) == 6
    assert out == []


# =============================================================================
# 单元：_official_name
# =============================================================================
def test_official_name_wins_over_garbage_name():
    """官方名存在 ⇒ 用官方名（垃圾名「重叠」不许盖住它）。"""
    assert F._official_name("159547", _OFFICIAL, "重叠") == "红利低波ETF华夏"


def test_official_name_falls_back_when_not_in_official():
    """官方名没有 ⇒ 才用来源名；都没有 ⇒ 空串。"""
    assert F._official_name("999999", _OFFICIAL, "某名字") == "某名字"
    assert F._official_name("999999", _OFFICIAL, "") == ""


# =============================================================================
# 集成：apply_ta_screening
# =============================================================================
def _screen(**kw):
    params = dict(
        actions=_actions(),
        candidates=_candidates(),
        rejections=_rejections(),
        held_codes=["512800"],
        current_weights={"512800": 0.222},
        asof="2026-11-11",
        universe_codes=set(_POOL),
        official_names=_OFFICIAL,
    )
    params.update(kw)
    return F.apply_ta_screening(**params)


def test_screening_excludes_pool_outside_code_and_discloses():
    """端到端：池外代码**不进** rejected，但**进** rejected_out_of_universe。"""
    out = _screen()
    ts = out["ta_screening"]
    rejected_codes = [r["code"] for r in ts["rejected"]]
    assert "566620" not in rejected_codes
    assert set(rejected_codes) == {"516620", "159207", "563020", "512890", "159547"}
    assert [r["code"] for r in ts["rejected_out_of_universe"]] == ["566620"]


def test_screening_names_use_official_names():
    """否决名单里的名称必须是**官方名**（159547 ⇒ 红利低波ETF华夏，不是「重叠」）。"""
    out = _screen()
    names = {r["code"]: r["name"] for r in out["ta_screening"]["rejected"]}
    assert names["159547"] == "红利低波ETF华夏"
    assert "重叠" not in names.values()


def test_screening_keeps_real_pool_rejections_unchanged():
    """池内真否决**行为不变**：被否者被剔除；顺延**照旧**按既有口径走。

    ⚠️ 本用例**没有**成分权重表（holdings=None）⇒ 规则层的「冲突对核验」不可得
    ⇒ 按既有 fail-safe 口径**不强行补位**（宁缺毋滥）⇒ `substituted` 为空，
    且 `no_substitute_reason` 必须写明原因。
    （顺延链路的**端到端**验证放在真实快照 A/B 上：2026-11-11 那轮
    成分权重可得 ⇒ 159207 被否 ⇒ 顺延补位 512170 医疗ETF华宝。）
    """
    out = _screen()
    ts = out["ta_screening"]
    assert "159207" not in [a.get("code") for a in out["actions"]], "被否者不得留在动作表里"
    assert "159207" in [r["code"] for r in ts["rejected"]], "池内真否决必须保留"
    if not ts["substituted"]:
        assert ts["no_substitute_reason"], "不补位必须写明原因"
        assert "成分权重数据不可得" in ts["no_substitute_reason"]


def test_screening_without_whitelist_keeps_everything():
    """白名单不可用 ⇒ 池外代码**保留**在 rejected（fail-closed）。"""
    out = _screen(universe_codes=None, official_names=None)
    rejected_codes = [r["code"] for r in out["ta_screening"]["rejected"]]
    assert "566620" in rejected_codes
    assert out["ta_screening"]["rejected_out_of_universe"] == []


def test_screening_backward_compatible_without_new_params():
    """不传新参数 ⇒ 行为与旧版一致（向后兼容）。"""
    out = _screen(universe_codes=None, official_names=None)
    assert len(out["ta_screening"]["rejected"]) == 6
