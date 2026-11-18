"""🔴 2026-11-17 新增：**权威段引用「第三方（观点角色）提出的否决」**的回归测试。

【真机事故（交付件 `snapshots/20261117_210415`）】
辩论裁决章（**裁判自己的话**）里写：

    「空头提出否决 512800 的宏观风险没有新增事件证据支持，
      不能把"外部数据缺失"当作已经发生黑天鹅。」

⇒ 裁判是在**驳回**空头的提议；旧口径把子句里的「否决 512800」当成**权威否决**
⇒ 512800 进 `ta_screening.rejected`（source_section=辩论裁决）
⇒ 交付层把 512800 冻结成「维持 7,700 股」的席位，并把空出来的槽位顺延给
   `512690`（**原文明写要清仓**）⇒ **双错**。

【修法】`llm_final._third_party_veto_positions`：否决动词的**主语是观点角色**
且带**提议动词**（提出/建议/…）⇒ 该否决属于**第三方提议** ⇒ 不算本轮否决
（除非同句出现「采纳/接受/同意…」等**采纳**措辞）。

⚠️ 与既有护栏的分工：不含提议动词的形态（「熊方的否决」）仍由 `_META_VETO_RE` 负责
⇒ 两层护栏各司其职（既有变异反证测试 `test_meta_veto_mutation_guard_is_load_bearing`
   必须继续成立）。
"""

from __future__ import annotations

import re

import llm_final as LLF


def _codes(text: str) -> list[str]:
    return [str(r.get("code")) for r in LLF.extract_rejections(text)]


#: 真机原文（辩论裁决章）
_SNIPPET_THIRD_PARTY = (
    "**裁决：空头在组合风险层胜出，多头在 512800 个券去留层略胜。**\n"
    "1. **512800 应减仓，但不构成清仓级证据。**\n"
    "   - 不否决 512800 的原因是：其最终分 **1.3372** 在入选组合中排名第一。\n"
    "2. **不采纳对 512800 的一票否决，但必须执行更严格的组合上限。**\n"
    "   - 空头提出否决 512800 的宏观风险没有新增事件证据支持，"
    "不能把“外部数据缺失”当作已经发生黑天鹅。\n"
)

#: 同句**采纳**了第三方的提议 ⇒ 仍算否决
_SNIPPET_ADOPTED = (
    "空头提出否决 512690 的宏观风险没有新增事件证据支持，"
    "委员会审议后予以采纳该提议。\n"
)


def test_第三方提出的否决不是权威否决() -> None:
    """真机原文 ⇒ 512800 **不许**进否决名单。"""
    codes = _codes(_SNIPPET_THIRD_PARTY)
    assert "512800" not in codes, (
        f"裁判在驳回空头的提议 ⇒ 512800 不是被否决（实测 {codes}）"
    )


def test_同句采纳第三方提议时仍是否决() -> None:
    """「提出否决」+ 同句「采纳」⇒ 权威接受了那个提议 ⇒ 仍算否决。"""
    codes = _codes(_SNIPPET_ADOPTED)
    assert "512690" in codes, f"被采纳的第三方否决必须判为否决（实测 {codes}）"


def test_否定式采纳不豁免第三方护栏() -> None:
    """「**不**采纳」⇒ 不是采纳 ⇒ 第三方护栏**照常生效**。"""
    text = (
        "空头提出否决 512800 的宏观风险没有新增事件证据支持，"
        "本轮不采纳该提议。\n"
    )
    codes = _codes(text)
    assert "512800" not in codes, f"不采纳 ⇒ 第三方提议不成立（实测 {codes}）"


def test_变异反证_第三方护栏清空后必须变红() -> None:
    """把 `_THIRD_PARTY_VETO_RE` 清空 ⇒ 上一条必须变红（证明这层护栏真在承重）。"""
    saved = LLF._THIRD_PARTY_VETO_RE
    try:
        LLF._THIRD_PARTY_VETO_RE = re.compile(r"(?!x)x")
        codes = _codes(_SNIPPET_THIRD_PARTY)
        assert "512800" in codes, "变异后应当误伤 512800（证明护栏承重）"
    finally:
        LLF._THIRD_PARTY_VETO_RE = saved


def test_真否决不受第三方护栏影响() -> None:
    """权威**自己**的否决（没有观点角色主语）必须照旧判否决。"""
    text = "本轮正式一票否决 512170，其拥挤度分位已达 99%。\n"
    codes = _codes(text)
    assert "512170" in codes, f"权威自己的否决必须判否决（实测 {codes}）"


def test_元指代形态仍由既有护栏负责() -> None:
    """「熊方的否决」这种**元指代**形态不归本护栏管（由 `_META_VETO_RE` 负责）。"""
    text = "决定最终落在 512800 的是三条：第一，熊方的否决拿不出更优替代\n"
    codes = _codes(text)
    assert "512800" not in codes, f"元指代形态不得误伤 512800（实测 {codes}）"
