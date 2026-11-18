"""🔴 2026-11-17 新增：**观点立场（熊派）纯主语否决**与**连坐误伤**的回归测试。

【真机事故（交付件 `snapshots/20261117_221846`）】
研究经理结论章 / 辩论裁决章（**裁判自己的话**，驳回熊派）里写：

    「1. 熊派否决512800的依据是"与515450/159207/159263的120日相关性0.835—0.848"。」

⇒ 旧口径把「熊派否决512800」当成**权威裁决** ⇒ 512800 进 `ta_screening.rejected`
⇒ **同一子句**里的 515450 / 159207 / 159263（只是相关性**对照对象**）被**连坐**
   ⇒ 而 TA 明确要买这三只（最终交易决定章：515450 买、159207 买、159263 条件性买）
⇒ 闸门 G-TA1「否决名单里不许有买侧标的」红灯；A20③「最终裁决可回放」红灯。

【根因（三处，各自都有变异反证）】
① **阵营称呼的词表缺口**：真机写「**熊派** / 多派」，而 `_META_VETO_RE` 的角色表
   只有「熊方 / 牛方」⇒ 第三方立场元指代层**认不出主语** ⇒ 否决动词被当权威裁决。
   ⇒ 修法：`_META_VETO_RE` 补齐「派」系阵营词。
② **动作词表缺口**：「实际部署因否决515650、**削减159207/159263**而落在…」
   ⇒ 「削减」不在 `_ITEM_ACTION_WORDS` ⇒ 该并列项被当成「没有自带动作」
   ⇒ **前一项的否决动词跨项泄漏** ⇒ 159207/159263 被连坐。
   ⇒ 修法：`_ITEM_ACTION_WORDS` 补齐「削减/降权/降配/减配」。
③ **表格路径没有假设护栏**：`| 515020 / 516310 / 512820 | 不作为替代 | 0 |
   …**即便512800被否决**也不做机械替换… |` ⇒ 表格行把 512800 拖进否决名单。
   ⇒ 修法：表格路径用 `_hypothetical_codes` 剔除**让步/假设结构**里的代码。

【分层纪律】`_META_VETO_RE`（第三方立场元指代）与 `_VIEWPOINT_SUBJECT_VETO_RE`
（观点角色**纯主语**）**各司其职**、**不得重叠**
（否则既有变异反证 `test_meta_veto_mutation_guard_is_load_bearing` 会空转）。
"""

from __future__ import annotations

import re

import llm_final as LLF


def _codes(text: str) -> list[str]:
    return [str(r.get("code")) for r in LLF.extract_rejections(text)]


#: 真机原文（研究经理结论章 / 辩论裁决章 · 裁判在**驳回**熊派）
_SNIPPET_BEAR_PURE_SUBJECT = (
    "1. 熊派否决512800的依据是"
    "“与515450/159207/159263的120日相关性0.835—0.848”"
)

#: 真机原文（同一章 · 裁判的落点句）：**前一项的否决动词跨项泄漏**
_SNIPPET_REDUCE_LEAK = (
    "**落点：** 对512800的方向是**减**（超配22.54% vs 机械16%），但减到约12%而非清零；"
    "实际部署因否决515650、削减159207/159263而落在**30—35%权益、65—70%现金**；"
)

#: 真机原文（同一章 · 表格行）：**即便…被否决** ⇒ 假设结构
_SNIPPET_ROW_HYPOTHETICAL = (
    "| 515020 / 516310 / 512820 | **不作为替代** | 0 | "
    "与512800成分重叠0.983—0.991、相关性0.980—0.990，属同一银行篮子复制；"
    "即便512800被否决也不做机械替换（515020/516310已被规则层冲突对与拥挤度排除） |"
)

#: 观点角色**纯主语**（不带提议动词、不带「的」）⇒ 第三方立场
_SNIPPET_VIEWPOINT_SUBJECT = (
    "激进派否决512800，理由是银行簇集中度偏高。\n"
)

#: 同句**采纳**了第三方立场 ⇒ 权威接受了 ⇒ 仍算否决
_SNIPPET_ADOPTED = (
    "熊派否决512800，依据是相关性0.85；委员会审议后予以采纳该提议。\n"
)

#: 主语含**权威角色** ⇒ 权威自己的裁决 ⇒ 仍算否决
_SNIPPET_AUTHORITY_AGENT = (
    "563020三位分析师与研究经理一致否决（最终分0.3818远低于515450、拥挤分位95%）。\n"
)


# ---------------------------------------------------------------- ① 阵营词表缺口

def test_真机_熊派纯主语否决不是权威否决() -> None:
    """真机原文 ⇒ 512800 / 515450 / 159207 / 159263 **都不许**进否决名单。"""
    codes = _codes(_SNIPPET_BEAR_PURE_SUBJECT)
    for code in ("512800", "515450", "159207", "159263"):
        assert code not in codes, f"{code} 被误判成权威否决（实测 {codes}）"


def test_变异反证_元指代层清空后必须变红() -> None:
    """把 `_META_VETO_RE` 清空 ⇒ 上一条必须变红（证明这层护栏真在承重）。"""
    saved = LLF._META_VETO_RE
    try:
        LLF._META_VETO_RE = re.compile(r"(?!x)x")
        codes = _codes(_SNIPPET_BEAR_PURE_SUBJECT)
        assert "512800" in codes, f"变异后应当误伤（证明护栏承重），实测 {codes}"
    finally:
        LLF._META_VETO_RE = saved


def test_同句采纳第三方立场时仍算否决() -> None:
    """「熊派否决512800……予以采纳」⇒ 权威接受了 ⇒ 仍算否决。"""
    codes = _codes(_SNIPPET_ADOPTED)
    assert "512800" in codes, f"被采纳的第三方否决必须判为否决（实测 {codes}）"


def test_主语含权威角色时仍算否决() -> None:
    """「三位分析师与**研究经理**一致否决563020」⇒ 权威自己的裁决 ⇒ 仍算否决。"""
    codes = _codes(_SNIPPET_AUTHORITY_AGENT)
    assert "563020" in codes, f"权威一致否决必须判为否决（实测 {codes}）"


# ---------------------------------------------------------------- ② 跨项泄漏

def test_削减动作不被前一项的否决动词连坐() -> None:
    """「否决515650、削减159207/159263」⇒ 只否 515650，**不连坐**另两只。"""
    codes = _codes(_SNIPPET_REDUCE_LEAK)
    assert "515650" in codes, f"515650 是真否决（实测 {codes}）"
    for code in ("159207", "159263"):
        assert code not in codes, f"{code} 被前一项的否决动词连坐（实测 {codes}）"


def test_变异反证_动作词表去掉削减后必须变红() -> None:
    """把「削减」从 `_ITEM_ACTION_WORDS` 去掉 ⇒ 上一条必须变红。"""
    saved = LLF._ITEM_ACTION_WORDS
    try:
        LLF._ITEM_ACTION_WORDS = tuple(
            w for w in saved if w not in ("削减", "降权", "降配", "减配"))
        codes = _codes(_SNIPPET_REDUCE_LEAK)
        assert "159207" in codes and "159263" in codes, (
            f"变异后应当连坐（证明动作词表承重），实测 {codes}"
        )
    finally:
        LLF._ITEM_ACTION_WORDS = saved


# ---------------------------------------------------------------- ③ 表格里的假设式

def test_表格行里的假设式否决不算否决() -> None:
    """「即便512800被否决也不做机械替换」⇒ 512800 不是被否决（但那三只仍被排除）。"""
    codes = _codes(_SNIPPET_ROW_HYPOTHETICAL)
    assert "512800" not in codes, f"假设结构里的 512800 被误判成否决（实测 {codes}）"
    for code in ("515020", "516310", "512820"):
        assert code in codes, f"{code} 仍是真排除项（实测 {codes}）"


def test_变异反证_让步词表清空后必须变红() -> None:
    """把 `_HYPOTHETICAL_MARKERS` 清空 ⇒ 上一条必须变红（证明表格护栏承重）。"""
    saved = LLF._HYPOTHETICAL_MARKERS
    try:
        LLF._HYPOTHETICAL_MARKERS = ()
        codes = _codes(_SNIPPET_ROW_HYPOTHETICAL)
        assert "512800" in codes, f"变异后应当误伤（证明护栏承重），实测 {codes}"
    finally:
        LLF._HYPOTHETICAL_MARKERS = saved


# ---------------------------------------------------------------- 纯主语层（各司其职）

def test_观点角色纯主语否决不是权威否决() -> None:
    """「激进派否决512800」⇒ 观点立场 ⇒ 不是权威裁决。"""
    codes = _codes(_SNIPPET_VIEWPOINT_SUBJECT)
    assert "512800" not in codes, f"观点立场的否决被当权威裁决（实测 {codes}）"


def test_变异反证_纯主语层清空后必须变红() -> None:
    """把 `_VIEWPOINT_SUBJECT_VETO_RE` 清空 ⇒ 上一条必须变红。"""
    saved = LLF._VIEWPOINT_SUBJECT_VETO_RE
    try:
        LLF._VIEWPOINT_SUBJECT_VETO_RE = re.compile(r"(?!x)x")
        codes = _codes(_SNIPPET_VIEWPOINT_SUBJECT)
        assert "512800" in codes, f"变异后应当误伤（证明护栏承重），实测 {codes}"
    finally:
        LLF._VIEWPOINT_SUBJECT_VETO_RE = saved


def test_纯主语层与元指代层不重叠() -> None:
    """两层**不得重叠**（否则既有变异反证会空转）。

    `_META_VETO_RE` 覆盖「X方的否决」（元指代），`_VIEWPOINT_SUBJECT_VETO_RE`
    只覆盖「X派否决CODE」（纯主语）⇒ 「熊方的否决」**不得**被纯主语层接管。
    """
    meta = LLF._META_VETO_RE
    subject = LLF._VIEWPOINT_SUBJECT_VETO_RE
    snippet = "熊方的否决拿不出更优替代——515020 与 512800 成分重叠 0.99"
    assert meta.search(snippet), "「熊方的否决」必须由元指代层接管"
    assert not subject.search(snippet), "「熊方的否决」不得被纯主语层接管（两层重叠）"
