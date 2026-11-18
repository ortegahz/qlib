"""🔴 TA「一票否决」**误伤**回归（2026-11-11 真机实跑事故）。

事故：服务器 `snapshots/20261111_100742` 的交付件里，TA 在最终裁决表**明确写**
「买 515450 16%」，但 `ta_screening.rejected` 里却有 **515450**
⇒ 该席位被顺延成 512890 / 516620 / 512010，**TA 要的红利低波 515450 消失**。
同时 **512800**（TA 明写「保留 512800 的 16% 进攻核心」）也被误判成否决。

三处根因（本文件逐条钉死，并各配一个**变异负例**证明护栏是**承重**的）：

  ① **比较 / 工具引用**被当成裁决对象
     「与 515450 相关性 0.917」「红利敞口只用 515450 表达足够，不建仓」
     ⇒ 旧口径把 515450 判成否决。
     修法：`_MD_REF_COMPARE_RE` / `_MD_REF_TOOL_RE` + `_reference_codes()`
           ⇒ 引用代码不作否决主体，且**不跨子句继承**。
  ② **反向语义护栏词表不全**
     「降低无效权益，**而不是放弃**有效权益」⇒ 旧口径只认「而不是放弃」？
     不，旧口径的否定护栏压根不认「而不是 / 而非」⇒ 「放弃」被当否决动词
     ⇒ 512800 被误伤。
  ③ **弱否决动词跨子句继承**
     「此时清仓等于**放弃**唯一有效趋势暴露」⇒ 「放弃」是弱动词，
     且代码 515450 是从上一子句**继承**来的 ⇒ 不该判否决。

本文件**自带夹具**（今日决策 md 的原文节选），不依赖服务器产物、不联网。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_misjudge_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> list[str]:
    return [r["code"] for r in LLF.extract_rejections(text)]


# ---------------------------------------------------------------- 今日真实 md 节选

#: ① 「563020 的题设有事实错误要纠正…」段 —— 误伤 515450 的**根因现场**。
_SNIPPET_REF = (
    "563020 的题设有事实错误要纠正：表内 E 实际是 **+0.1074**（不是负数），"
    "mom120 +5.45%、最终分 0.4116，而且它在候选清单里是 ✘（未通过同族去重）。"
    "但结论不变：与 515450 相关性 0.917、红利敞口只用 515450 表达足够，不建仓、更不给 20%。\n"
)

#: ② 最终调仓表里的**买 515450** 行 —— 与 ① 自相矛盾 ⇒ 交付件必须以 ② 为准。
_SNIPPET_TABLE_BUY_515450 = (
    "| 买 | 515450 | 红利低波50ETF南方 | 16% | ~4,777 | 100 份整数倍 | 付 ~4,776.75 |\n"
)

#: ③ 「而不是放弃有效权益：保留 512800…」段 —— 误伤 512800 的根因现场。
_SNIPPET_KEEP_512800 = (
    "我的选择因此是降低无效权益，而不是放弃有效权益：保留512800的16%进攻核心，"
    "买入515450，适度买入515650，组合权益维持在不高于当前48.02%的水平，"
    "剩余资金保持为下一次高赔率机会的弹药。\n"
)

#: ④ 「此时清仓等于放弃唯一有效趋势暴露」段 —— 弱动词 + 跨子句继承 ⇒ 误伤 515450。
_SNIPPET_WEAK_ABANDON = (
    "二、方向确定——减仓不是清仓（三位分析员与研究经理一致）：512800 是本轮已入选标的中"
    "信号最强者（最终分 1.1594、E 0.4177、mom120 +12.88%、站上 MA60、coverage 1.00），"
    "快照偏差测算显示现实档下换主题余量 -0.4692、换产品余量 -0.0805 均 ≤0，"
    "且与池内次优 515450 的最终分差达 0.5018，银行席位换不掉；"
    "此时清仓等于放弃唯一有效趋势暴露。\n"
)

#: ⑤ 方向汇总 —— **真否决**必须仍然命中（护栏没把真否决也放过）。
_SNIPPET_REAL_REJECTS = (
    "五、方向性判断汇总（512800 为本轮主结论）：512800 **减**（22.54% → 16%，规则槽位）；"
    "159929 **减**（11.17% → 6%）；513330 **卖**；513770 **卖**；512690 **卖**；"
    "515450 **买**（16%，非银行中最强，E 0.3353、mom120 +5.72%）；"
    "515650 **买**（12%，E 0.2004、mom120 +4.69%）；"
    "159207 **小买**（4%，最终分 -0.1155、E 0.032，仅作象征仓）；"
    "512170 **不买**；563020 **不买**；现金 **46%**。\n"
)

#: ⑥ 「不建仓159207、512170、563020」—— 点名式真否决。
_SNIPPET_NAMED_VETO = "- **不建仓159207、512170、563020。**\n"

#: ⑦ 「暂不建仓」—— 弱动词 + **同子句点名** ⇒ 仍须判否决。
_SNIPPET_WEAK_SAME_CLAUSE = (
    "159207、512170和563020暂不建仓。\n"
)

#: ⑧ 「不买511990」—— 防守槽位全部留现金。
_SNIPPET_511990 = (
    "防守资产方面，我支持全部留现金，不买511990。"
    "其一手约10,000元，占总资产约33.5%，既无法装入20%的防守预算，"
    "也会形成过大的单只工具集中。\n"
)

_MD_TODAY = (
    _SNIPPET_REF
    + _SNIPPET_TABLE_BUY_515450
    + _SNIPPET_KEEP_512800
    + _SNIPPET_WEAK_ABANDON
    + _SNIPPET_REAL_REJECTS
    + _SNIPPET_NAMED_VETO
    + _SNIPPET_WEAK_SAME_CLAUSE
    + _SNIPPET_511990
)


# ---------------------------------------------------------------- 主断言

def test_ta_wanted_codes_are_not_vetoed():
    """🔴 TA 明写要买 / 要留的代码，**绝不许**进否决名单。"""
    codes = set(_codes(_MD_TODAY))
    assert "515450" not in codes, "515450 在最终裁决表里是「买 16%」⇒ 不得被误判成否决"
    assert "512800" not in codes, "512800 明写「保留 16% 进攻核心」⇒ 不得被误判成否决"


def test_real_vetoes_still_vetoed():
    """🔴 护栏**不许**把真否决也放过（宁漏勿误 ≠ 全放过）。"""
    codes = set(_codes(_MD_TODAY))
    for code in ("512170", "563020", "159207", "511990"):
        assert code in codes, f"{code} 是 TA 点名否决的 ⇒ 必须仍然命中"


def test_table_buy_row_is_not_veto():
    """表格行「买 515450 … 16%」⇒ 目标权重 > 0 ⇒ 不是否决。"""
    assert "515450" not in _codes(_SNIPPET_TABLE_BUY_515450)


def test_weak_verb_same_clause_still_vetoes():
    """弱动词「暂不建仓」但**同子句点名** ⇒ 仍判否决（不能一刀切放过弱动词）。"""
    assert "563020" in _codes(_SNIPPET_WEAK_SAME_CLAUSE)


# ---------------------------------------------------------------- 变异负例（护栏承重证明）

def _restore(llf, saved):
    for key, value in saved.items():
        setattr(llf, key, value)


def test_mutation_ref_guard_is_load_bearing():
    """变异：关掉「比较 / 工具引用」护栏 ⇒ 515450 立刻被误伤 ⇒ 证明该护栏承重。"""
    saved = {
        "_MD_REF_COMPARE_RE": LLF._MD_REF_COMPARE_RE,
        "_MD_REF_TOOL_RE": LLF._MD_REF_TOOL_RE,
    }
    try:
        LLF._MD_REF_COMPARE_RE = re.compile(r"(?!x)x")
        LLF._MD_REF_TOOL_RE = re.compile(r"(?!x)x")
        codes = set(_codes(_SNIPPET_REF))
        assert "515450" in codes, "护栏关掉后 515450 必须重新被误伤（否则本变异无效）"
    finally:
        _restore(LLF, saved)


def test_mutation_negation_guard_is_load_bearing():
    """变异：否定护栏的**尾动词表**去掉「放弃」⇒ 512800 立刻被误伤 ⇒ 证明该护栏承重。

    🔴 变异点说明（第一版踩过的坑）：原以为要去掉「而不是 / 而非」这两个限定词，
       实际**无效**——「而不是」本身含「不是」，`不是` 早已在护栏的否定词闭集里
       ⇒ 去掉限定词后依然命中 ⇒ 变异「测不出东西」。
       **真正承重**的是尾动词表里的 **「放弃」**：没有它，「而不是放弃有效权益」
       这句就护不住 512800（否定词与「放弃」之间隔了「有效权益：保留512800…」）。
    """
    saved = {"_REJECT_NEGATION_RE": LLF._REJECT_NEGATION_RE}
    try:
        pattern = LLF._REJECT_NEGATION_RE.pattern.replace("|放弃|", "|")
        assert pattern != LLF._REJECT_NEGATION_RE.pattern, "否定护栏词表形态变了 ⇒ 变异无效"
        LLF._REJECT_NEGATION_RE = re.compile(pattern)
        codes = set(_codes(_SNIPPET_KEEP_512800))
        assert "512800" in codes, "护栏关掉后 512800 必须重新被误伤（否则本变异无效）"
    finally:
        _restore(LLF, saved)


def test_mutation_weak_verb_guard_is_load_bearing():
    """变异：清空弱动词表 ⇒ 跨子句继承的 515450 立刻被误伤 ⇒ 证明该护栏承重。"""
    saved = {"_REJECT_VERBS_WEAK": LLF._REJECT_VERBS_WEAK}
    try:
        LLF._REJECT_VERBS_WEAK = ()
        codes = set(_codes(_SNIPPET_WEAK_ABANDON))
        assert "515450" in codes, "护栏关掉后 515450 必须重新被误伤（否则本变异无效）"
    finally:
        _restore(LLF, saved)


def test_mutation_ref_guard_does_not_break_real_veto():
    """变异护栏**不许**误伤真否决：关掉引用护栏后 563020 仍须命中（来自别处）。"""
    saved = {
        "_MD_REF_COMPARE_RE": LLF._MD_REF_COMPARE_RE,
        "_MD_REF_TOOL_RE": LLF._MD_REF_TOOL_RE,
    }
    try:
        LLF._MD_REF_COMPARE_RE = re.compile(r"(?!x)x")
        LLF._MD_REF_TOOL_RE = re.compile(r"(?!x)x")
        codes = set(_codes(_MD_TODAY))
        assert "512170" in codes
        assert "563020" in codes
    finally:
        _restore(LLF, saved)
