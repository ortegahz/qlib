#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🛡️ **一票否决的归属**：驳回否决的元结构 / 名词用法 / 权威层级。

【钉的是什么】（2026-11-13 全量跑批 `snapshots/20261113_092926` · G-TA1 红灯实证）
真机研究经理结论段**逐字**原句：

    第二，拥挤度是风险提示而不是选股依据，只可作否决的补充理由——
    正因如此我拒绝熊方对 515650 的否决：515650 的 E=+0.3401 是全部非银行候选里最高的，
    最终分 -0.0852 也远好于顺延替代品（医药类 512170 -1.3950、512010 -1.4778，
    且两者互为同族只能选一只），否决它等于用 -1.4 分的资产换 -0.0852 分的资产，是自伤

三处语义坑（旧口径全踩）：

① **驳回否决的元结构**：「我拒绝熊方对 515650 的否决」
   ⇒ 否决动词的宾语是「**某方的否决**」而不是「**某只标的**」
   ⇒ 说话人（研究经理）**驳回**了否决、并**买入** 515650
   ⇒ 旧口径把 515650 写进「TA 一票否决」名单 ⇒ 席位错位。
② **否决的名词用法**：「只可作**否决的**补充理由」
   ⇒ 这里的「否决」是「作……的理由」的**宾语名词**，不是裁决动词。
③ **否决出处的权威层级**：同一只标的在「研究经理(10) / 辩论裁决(20) / 风险裁决(20) /
   最终交易决定(40)」多段都被否决 ⇒ **约束力最强**的那一处才是生效的裁决
   ⇒ 交付件必须记**最高层级**的出处，否则闸门拿「否决层级 vs 买侧层级」判不出真矛盾。

【口径铁律】
* 驳回否决 / 名词用法 ⇒ **不**产出否决；
* **真**点名否决（一票否决 / 维持否决 / 正式否决）⇒ 必须仍然被抓到（不许漏放）；
* 出处章节 ⇒ 取**权威层级最高**的那一处；
* 每条护栏都配**运行时变异反证**：把护栏改废 ⇒ 对应断言**必须转红**
  （= 证明「若无此护栏，真机件确实会误判」，而不是「断言本来就不成立」）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_final as L  # noqa: E402

#: 🔴 真机原句（研究经理结论段，**逐字**取自 `snapshots/20261113_092926`）
REAL_SCORE_LINE = (
    "第二，拥挤度是风险提示而不是选股依据，只可作否决的补充理由——"
    "正因如此我拒绝熊方对 515650 的否决：515650 的 E=+0.3401 是全部非银行候选里最高的，"
    "最终分 -0.0852 也远好于顺延替代品（医药类 512170 -1.3950、512010 -1.4778，"
    "且两者互为同族只能选一只），否决它等于用 -1.4 分的资产换 -0.0852 分的资产，是自伤"
)

#: 🔴 真机**风险裁决 / 最终交易决定**段原句（对 515650 的**真**否决）
REAL_FINAL_LINE = (
    "515650（消费50ETF富国）：按保守派与中性派的多数意见维持否决"
    "——最终分 -0.0852、放量 1.81 / 分位 93% 已触发拥挤补充否决条件，不另设 16% 卫星仓。"
)


def _codes(text: str) -> set[str]:
    return {r["code"] for r in L.extract_rejections(text)}


def _sections(text: str) -> dict[str, str]:
    return {r["code"]: r["source_section"] for r in L.extract_rejections(text)}


# ---------------------------------------------------------------- ① 驳回否决
def test_真机原句_驳回否决不得产出对515650的否决() -> None:
    """🔴 主用例：研究经理**驳回**熊方对 515650 的否决 ⇒ 515650 不是被否决的标的。"""
    assert "515650" not in _codes(REAL_SCORE_LINE), f"🔴 驳回否决被误判成否决：{_codes(REAL_SCORE_LINE)}"


def test_驳回否决护栏_必须逐条命中真机原句() -> None:
    """🔴 **逐条承重**：真机原句里的「拒绝…对 515650 的否决」必须被两条护栏各自命中。"""
    refused = [m.group(0) for m in L._REFUSED_VETO_RE.finditer(REAL_SCORE_LINE)]
    meta = [m.group(0) for m in L._META_VETO_RE.finditer(REAL_SCORE_LINE)]
    assert refused and all("515650" in s for s in refused), f"🔴 驳回护栏没命中：{refused}"
    assert meta and all("515650" in s for s in meta), f"🔴 元指代护栏没命中：{meta}"


def test_变异_废掉驳回否决护栏_515650必须重新被误判(monkeypatch) -> None:
    """🔴 **变异反证**：把 `_REFUSED_VETO_RE` 改废（= 修复前的状态）
    ⇒ 515650 必须重新被误判 ⇒ 证明该护栏承重。
    """
    monkeypatch.setattr(L, "_REFUSED_VETO_RE", re.compile(r"(?!x)x"))
    assert "515650" in _codes(REAL_SCORE_LINE), "变异后必须重新误判 515650（否则本用例是假通过）"


def test_变异_同时废掉驳回与元指代两条护栏_515650必须重新被误判(monkeypatch) -> None:
    """🔴 **变异反证**：把 `_REFUSED_VETO_RE` 与 `_META_VETO_RE` 一起改回**修复前**的口径
    ⇒ 515650 必须重新被误判 ⇒ 证明这两条护栏**合起来**承重
    （单废一条时另一条会兜住 ⇒ 必须一起废才能复原事故现场）。
    """
    monkeypatch.setattr(L, "_REFUSED_VETO_RE", re.compile(r"(?!x)x"))
    monkeypatch.setattr(
        L, "_META_VETO_RE",
        re.compile(r"(?:熊方|牛方|空方|多头|空头|对方|对手方|多方|空方)\s*的?\s*(?:一票\s*)?否决"),
    )
    assert "515650" in _codes(REAL_SCORE_LINE), "变异后必须重新误判 515650（否则本用例是假通过）"


# ---------------------------------------------------------------- ② 名词用法
def test_真机原句_作否决的补充理由里的否决是名词() -> None:
    """🔴 主用例：「只可作**否决的**补充理由」⇒ 这个「否决」不是裁决动词。"""
    assert any(m.group(0).endswith("否决") for m in L._REJECT_NOUN_RE.finditer(REAL_SCORE_LINE))


def test_变异_废掉名词用法护栏_515650必须重新被误判(monkeypatch) -> None:
    """🔴 **变异反证**：把 `_REJECT_NOUN_RE` 改废 ⇒ 515650 必须重新被误判。"""
    monkeypatch.setattr(L, "_REJECT_NOUN_RE", re.compile(r"(?!x)x"))
    assert "515650" in _codes(REAL_SCORE_LINE), "变异后必须重新误判 515650（否则本用例是假通过）"


# ---------------------------------------------------------------- ③ 成对负例（不许漏放）
def test_真机最终裁决段_515650必须仍被否决() -> None:
    """🔴 **成对负例**：真机最终裁决段对 515650 的**真否决**必须仍然被抓到。"""
    assert "515650" in _codes(REAL_FINAL_LINE), f"🔴 真否决漏放：{_codes(REAL_FINAL_LINE)}"


def test_点名否决的其它写法_不得因修护栏而漏放() -> None:
    """🔴 **成对负例**：各种真否决写法必须仍然被抓到。"""
    for text in (
        "- 515650（消费50ETF富国）：否决",
        "515650 正式一票否决，移出顺延池",
        "516620（影视ETF国泰）：全票否决——E=-0.6798",
        "159207（高股息ETF广发）：维持否决",
        "563020（红利低波ETF易方达）：全票否决",
    ):
        codes = _codes(text)
        expected = re.search(r"[1-9]\d{5}", text).group(0)
        assert expected in codes, f"🔴 点名否决漏放（{text}）：{codes}"


def test_拒绝与真否决并存时_真否决仍要被抓到() -> None:
    """🔴 **成对负例**：「拒绝……的否决」不得把**同句里别的**真否决一起吞掉。"""
    codes = _codes("我拒绝熊方对 515650 的否决；正式否决 512170，不建仓")
    assert "512170" in codes, f"🔴 真否决漏放：{codes}"
    assert "515650" not in codes, f"🔴 驳回否决被误判：{codes}"


# ---------------------------------------------------------------- ④ 权威层级归属
def test_否决出处_取权威层级最高的那一处() -> None:
    """🔴 主用例：同一只标的在多段被否决 ⇒ 记**约束力最强**的那一段。"""
    text = (
        "## 研究经理\n- 515650（消费50ETF富国）：否决。\n\n"
        "## 风险裁决\n515650（消费50ETF富国）：维持否决——最终分 -0.0852。\n\n"
        "## 最终交易决定\n515650（消费50ETF富国）：按保守派与中性派的多数意见维持否决。\n"
    )
    assert _sections(text).get("515650") == "最终交易决定", f"🔴 出处归属错：{_sections(text)}"


def test_否决出处_只有低层级时_保持原样() -> None:
    """🔴 **成对负例**：只有研究经理段否决时，出处**不得**被改写。"""
    text = "## 研究经理\n- 512170（医疗ETF华宝）：否决。\n"
    assert _sections(text).get("512170") == "研究经理", f"🔴 出处归属错：{_sections(text)}"


def test_否决名单_升级归属后一只都不许丢() -> None:
    """🔴 **不许漏放**：归属升级只许换出处，**不许**把哪一只从名单里弄丢。"""
    text = (
        "## 研究经理\n- 512170（医疗ETF华宝）：否决。\n- 512170：不建仓。\n\n"
        "## 最终交易决定\n512170（医疗ETF华宝）：正式一票否决。\n"
    )
    assert "512170" in _codes(text), f"🔴 否决漏放：{_codes(text)}"


# ---------------------------------------------------------------- ⑤ 层级表漂移守卫
def test_权威层级表_必须与探针逐字一致() -> None:
    """🔴 **单一事实源漂移守卫**：生产解析器与闸门探针的层级表必须完全一致。

    ⇒ 否则「交付件记的出处层级」与「闸门判的层级」会用两把尺子 ⇒ 闸门结论不可信。
    """
    from probes import probe_verify_guards as G  # noqa: PLC0415

    assert L._AUTHORITY_RANK_MARKERS == G._AUTHORITY_RANKS, (
        "🔴 权威层级表漂移：生产 "
        f"{L._AUTHORITY_RANK_MARKERS} ≠ 探针 {G._AUTHORITY_RANKS}"
    )


def test_层级表_真机章节标题必须落到预期层级() -> None:
    """🔴 真机 md 的章节标题 ⇒ 预期层级（研究经理 10 / 辩论·风险裁决 20 / Trader 30 / 最终 40）。"""
    assert L._authority_rank("二、研究经理结论（Research Manager）") == 10
    assert L._authority_rank("辩论裁决") == 20
    assert L._authority_rank("风险裁决") == 20
    assert L._authority_rank("五、Trader 交易计划") == 30
    assert L._authority_rank("六、最终交易决定（原始输出）") == 40
    assert L._authority_rank("三、多空辩论要点") == 0
