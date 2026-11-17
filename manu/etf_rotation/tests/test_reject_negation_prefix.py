r"""🔴 2026-11-16：Stage 4 **否定前缀误判** + **否决来源章节越界**的钉死测试。

【事故（真机交付件 `snapshots/20261113_202504`）】
Trader 交易计划里写「因此**不正式否决** `512800`，但将其降至16%」
⇒ 旧口径的否定护栏只认「不否决」「未否决」「并非否决」这类**紧贴**写法
⇒ 「不**正式**否决」**逃过**护栏 ⇒ 512800 被当成**一票否决**
⇒ 交付层把它变成「维持席位」（`orphaned_holdings`）⇒ 该顺延补位的 515450 消失
⇒ 权益上限 80%→32% 时只剩 584.4 元 ⇒ 又被 3800 元新开仓门槛拦下 ⇒ 全链路错。

【修法（三条独立防线）】
① **否定前缀过滤**（`llm_final._REJECT_NEG_PREFIX_RE`）：
   「否定前缀 + 修饰词**白名单** + **裸**否决动词」**贴脸**过滤
   ⇒ 「不满足条件即剔除」这类**真否决**不受伤（修饰词白名单不含任何动词）。
   🔴 2026-11-16 独立审查 I1：命中作废用**区间重叠**判定，不是「起点相等」
   ⇒ 「**不**考虑**剔除**」这种**复合**动词也护得住。
② **强调句是真否决**（`llm_final._REJECT_EMPHASIS_RE`）：
   「**不得不否决** / 未尝不否决 / 无不否决」= 必须否掉
   ⇒ 不得被 `_REJECT_NEGATIONS` 的**子串**匹配吞掉（独立审查 I2）。
③ **否决来源：裁决段全集，但「Trader 交易计划」只采信表格行**
   （`llm_final.REJECT_SOURCE_SECTIONS` / `REJECT_SOURCE_TABLE_ONLY_SECTIONS`）：
   * **散文**（执行计划的自然语言叙述，事故句就在里面）⇒ **丢弃**；
   * **表格行**（结构化裁决「| 563020 | 不买，正式否决 |」）⇒ **采信**
     ⇒ `('Trader', 30)` 权威层级仍可达 ⇒ 不制造同型漏判（独立审查 B1）。

【为什么 ③ 不是「只认最终交易决定一段」】
真机 20261110 决策件里「最终交易决定」只列了 **4** 只否决，另外 **9** 只真否决
写在「研究经理 / 辩论裁决」段 ⇒ 硬收敛到一段会**漏判 9 只**
（探针 A1「恰好 13 只」当场变红）⇒ 保留**裁决段全集**。

【钉死的纪律】
* 四种否定前缀写法（不否决 / 不正式否决 / 未否决 / 并非否决）都**不得**被判否决；
* 真否决写法（正式一票否决 / 不满足条件即剔除 / 不得不否决）**必须**仍被否；
* 「Trader 交易计划」段里的**散文**否决**不得**进名单、**表格行**否决**必须**进名单；
* 真机 20261113 决策件 ⇒ 恰好 13 只、**512800 不在名单里**。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ETF_DIR = Path(__file__).resolve().parents[1]
if str(ETF_DIR) not in sys.path:
    sys.path.insert(0, str(ETF_DIR))

import llm_final as L  # noqa: E402

#: 真机 20261113 决策件里 TA **真正**一票否决的 13 只
#: （= 最终交易决定表的「不买 / 正式否决 / 不买」行，逐只核对过）。
EXPECTED_13: frozenset[str] = frozenset({
    "515650", "516620", "159207", "563020",
    "515020", "516310", "512820",
    "512890", "159547", "515100", "159307", "159928", "512010",
})

#: TA 明确「买 / 减」的标的 ⇒ **绝不能**被当成否决。
PROTECTED: frozenset[str] = frozenset({"512800", "515450"})

FIXTURE_20261113 = ETF_DIR / "tests" / "fixtures" / "ta_decision_real_20261113.md"
FIXTURE_20261110 = ETF_DIR / "tests" / "fixtures" / "ta_decision_real_20261110.json"


def _codes(rows: list[dict]) -> list[str]:
    return [str(r.get("code")) for r in rows]


# ─────────────────── ① 否定前缀：不得判否决 ───────────────────
@pytest.mark.parametrize("sentence", [
    "因此不正式否决 `512800`，但将其降至16%。",
    "不否决 `512800`。",
    "未否决 `512800`。",
    "并非否决 `512800`。",
    "不再否决 `512800`。",
    "未正式否决 `512800`。",
    "不予正式否决 `512800`。",
    "不正式否决 `512800`，改按 16% 持有。",
])
def test_否定前缀写法_不得判否决(sentence: str) -> None:
    """否定前缀 + 裸否决动词 ⇒ 那是「澄清**不**否决」，不是否决。"""
    assert "512800" not in _codes(L.extract_rejections(sentence)), sentence


# ─────────────────── ② 变异反证：新护栏是 load-bearing 的 ───────────────────
def test_变异负例_清空否定前缀护栏后必须转红() -> None:
    """把**两道**否定护栏都清空 ⇒ 「不正式否决」必须变红 ⇒ 证明断言**有牙**。

    🔴 两道护栏：① 旧护栏 `_REJECT_NEGATIONS`（字面量，含新增的「不正式否决」）
    ② 新护栏 `_REJECT_NEG_PREFIX_RE`（贴脸正则）⇒ **两道一起**清空才逼得红。
    """
    original = L._REJECT_NEGATIONS
    original_prefix = L._REJECT_NEG_PREFIX_RE
    try:
        L._REJECT_NEGATIONS = ()
        L._REJECT_NEG_PREFIX_RE = re.compile(r"(?!x)x")
        hit = _codes(L.extract_rejections("因此不正式否决 `512800`，但将其降至16%。"))
        assert "512800" in hit, f"变异后仍未命中 ⇒ 没有牙：{hit}"
    finally:
        L._REJECT_NEGATIONS = original
        L._REJECT_NEG_PREFIX_RE = original_prefix
    assert "512800" not in _codes(
        L.extract_rejections("因此不正式否决 `512800`，但将其降至16%。"))


# ─────────────────── ③ 真否决：不许被过度豁免 ───────────────────
@pytest.mark.parametrize("text,code", [
    ("正式一票否决 `516620`，不买。", "516620"),
    ("| `516620` | **不买，正式否决** | 拥挤度 100% |", "516620"),
    ("不满足条件即剔除 `512170`。", "512170"),
    ("对 `512820` 一票否决，不买。", "512820"),
    ("未通过复核即剔除 `512170`。", "512170"),
    ("不得不否决 `516620`。", "516620"),
    ("未尝不否决 `516620`。", "516620"),
    ("无不否决 `516620`。", "516620"),
])
def test_真否决写法_仍被判否决(text: str, code: str) -> None:
    """否定前缀护栏**只**作废「贴脸」的那一个动词 ⇒ 真否决必须原样命中。"""
    assert code in _codes(L.extract_rejections(text)), text


def test_同句里_否定与真否决要分清() -> None:
    """「不否决 A，正式否决 B」⇒ 只否 B。"""
    codes = _codes(L.extract_rejections("不否决 `512800`，正式否决 `516620`。"))
    assert "512800" not in codes, codes
    assert "516620" in codes, codes


# ─────────────────── ④ 否决来源章节：禁止 Trader 交易计划散文 ───────────────────
def test_Trader交易计划段_散文不得作为否决来源(tmp_path: Path) -> None:
    """🔴 事故形态：Trader 段的**散文**里的否决词**不得**进名单。

    事故原文：「因此**不正式否决** `512800`，但将其降至16%」⇒ 512800 被误判成一票否决。
    """
    md = (
        "## 五、Trader 交易计划\n\n"
        "散文：因此不正式否决 `512800`，但将其降至16%。\n\n"
        "| `512800` | **减** | 减至16%标准槽位 |\n\n"
        "## 六、最终交易决定（原始输出）\n\n"
        "维持 `512800` 减至 16%，其余不变。\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    codes = _codes(L.extract_authority_rejections(str(md_path)))
    assert "512800" not in codes, codes


def test_Trader交易计划段_表格行必须被采信(tmp_path: Path) -> None:
    """🔴 独立审查 B1：Trader 段的**结构化裁决表**是真否决 ⇒ **必须**进名单。

    整段砍掉 Trader ⇒ `('Trader', 30)` 权威层级永不可达 ⇒ 只写在 Trader 段的
    真否决会**漏判**（与本次事故同型、方向相反）⇒ 折中：**只采信表格行**。
    """
    md = (
        "## 五、Trader 交易计划\n\n"
        "散文段落，随便写点什么。\n\n"
        "| 标的 | 方向 | 主要理由 |\n"
        "|---|---|---|\n"
        "| `516620` | **不买，正式否决** | 拥挤度 100% |\n\n"
        "## 六、最终交易决定（原始输出）\n\n"
        "维持 `512800` 减至 16%，其余不变。\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    rows = L.extract_authority_rejections(str(md_path))
    assert "516620" in _codes(rows), rows


def test_Trader否决的出处层级是30(tmp_path: Path) -> None:
    """Trader 段的否决 ⇒ 出处层级 **30**（观点 0 < 研究经理 10 < 裁决 20 < Trader 30）。"""
    md = (
        "## 五、Trader 交易计划\n\n"
        "| `516620` | **不买，正式否决** | 拥挤度 100% |\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    rows = L.extract_authority_rejections(str(md_path))
    assert rows, rows
    for row in rows:
        assert L._authority_rank(row.get("source_section")) == 30, row


def test_最终交易决定段_仍是否决来源(tmp_path: Path) -> None:
    """「最终交易决定」是**终端裁决** ⇒ 它的否决必须进名单（不许收敛过头）。"""
    md = (
        "## 六、最终交易决定（原始输出）\n\n"
        "| `516620` | **不买，正式否决** | 拥挤度 100% |\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    codes = _codes(L.extract_authority_rejections(str(md_path)))
    assert "516620" in codes, codes


def test_来源章节常量_裁决段全集且Trader只采信表格() -> None:
    """单一事实源断言：来源 = **裁决段全集**；「Trader」= **只采信表格行**。"""
    titles = [title for title, _ in L.REJECT_SOURCE_SECTIONS]
    assert "最终交易决定" in titles, titles
    assert "研究经理" in titles, titles
    assert "Trader 交易计划" in titles, titles
    assert "Trader 交易计划" in L.REJECT_SOURCE_TABLE_ONLY_SECTIONS
    assert "最终交易决定" not in L.REJECT_SOURCE_TABLE_ONLY_SECTIONS
    assert "Trader" in L.REJECT_SOURCE_MD_MARKERS, L.REJECT_SOURCE_MD_MARKERS
    assert "最终交易决定" in L.REJECT_SOURCE_MD_MARKERS, L.REJECT_SOURCE_MD_MARKERS
    assert "Trader" in L.REJECT_SOURCE_TABLE_ONLY_MD_MARKERS


# ─────────────────── ⑤ 真机夹具：20261113 恰好 13 只、零误伤 ───────────────────
def test_真机20261113_恰好13只_零误伤() -> None:
    rows = L.extract_authority_rejections(str(FIXTURE_20261113))
    codes = set(_codes(rows))
    assert codes == set(EXPECTED_13), (sorted(codes), sorted(set(EXPECTED_13) ^ codes))
    assert not (codes & PROTECTED), sorted(codes & PROTECTED)


def test_真机20261113_512800不得被否() -> None:
    """事故标的：512800 **不得**出现在否决名单里。"""
    codes = _codes(L.extract_authority_rejections(str(FIXTURE_20261113)))
    assert "512800" not in codes, codes


def test_真机20261113_否决出处章节必须是最终交易决定() -> None:
    """否决出处章节取**权威层级最高**的那一处 ⇒ 13 只都该记在「最终交易决定」。"""
    rows = L.extract_authority_rejections(str(FIXTURE_20261113))
    for row in rows:
        assert "最终交易决定" in str(row.get("source_section") or ""), row


def test_老夹具20261110_仍恰好13只() -> None:
    """变异反证：**不许**收敛过头（真机 20261110 的 13 只分散在多个裁决段）。"""
    codes = set(_codes(L.extract_authority_rejections(str(FIXTURE_20261110))))
    assert len(codes) == 13, sorted(codes)
    assert not (codes & PROTECTED), sorted(codes & PROTECTED)
