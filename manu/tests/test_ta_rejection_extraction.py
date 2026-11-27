"""🔴 TA「一票否决抽取」：最终裁决句 / 讨论句 / 邻居标的 三重隔离（G4 · 2026-11-26 真缺陷）。

事故来源（2026-11-26 全量跑批闸门红 · 服务器交付件
`snapshots/20261126_171821/tradingagents_decision.md` 与同目录 `final_rebalance_plan.json`）：

    `ta_screening.rejected` 抽出 **5** 只，其中 **2** 只是**误判**：

    | 标的 | 管道口径 | TA 真实裁决 | 误判句式 |
    |---|---|---|---|
    | `512800` | 否决 | 放行（真实裁决在「最终交易决定」段，本轮是**减仓**） | 「这场辩论真正在吵三件事：**512800 个股要不要减**、**要不要否决**消费重复腿、权益总敞口该多大」——「要不要否决」是**讨论句**，不是裁决句 |
    | `159928` | 否决 | 放行（**买入**） | 「1. **否决 515170**：与 **159928** 成分重叠 0.645、120 日相关 0.938……」——**否决动词属于 515170**，`159928` 只是被提到的**邻居标的** |

    ⇒ 真正该否决的只有 **515170 / 512010 / 563020**（3 只）。

根因（`manu/etf_rotation/llm_final.py`）
    ① **讨论句被当成裁决句**：「要不要否决」的「否决」动词被绑到本行**唯一**的代码
       `512800` 头上 ⇒ 减仓指令被写成「一票否决」；
    ② **邻居标的被连坐**：「否决 515170：与 159928 成分重叠」里，旧口径按
       「同一并列项内 40 字窗口」⇒ `515170` 与 `159928` **一起**被判成否决。

修法（用户拍板口径）
    ① **只认最终裁决句**：行内必须有**明确指向某只标的**的否决动词（与代码**直接绑定**）；
    ② **讨论句不算**：「要不要否决 / 是否否决 / 可以否决吗」等疑问语气 ⇒ 动词作废；
    ③ **按标的代码分块隔离**：同句里作为邻居/对比对象出现的其它代码**不得连坐**；
    ④ **fail-closed**：判不准时宁可不判（少判比误杀安全），但**不许**放过明确否决句。

本文件钉死（自带夹具 `fixtures/ta_decision_real_20261126.json`
＝ 真机件**权威裁决段**裁剪版，不联网、不依赖服务器产物）：

    ① 真机件 ⇒ 否决集合**恰好** `{515170, 512010, 563020}`；
    ② `512800` / `159928` **各自**不得出现在否决集合里；
    ③ 三只**真否决**必须仍在名单里（收紧不许漏判）；
    ④ **变异反证**：把明确否决句改成「要不要否决 X」⇒ X **不再**被否（收紧真在起作用）；
    ⑤ **变异反证**：关掉「讨论语气」护栏 ⇒ 变体里的 X 重新被误伤（证明该护栏承重）；
    ⑥ **变异反证**：关掉「直接绑定」护栏 ⇒ `159928` 重新被连坐（证明该护栏承重）。
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
_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "ta_decision_real_20261126.json"
)


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_reject_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> set[str]:
    """散文路径 ⇒ 抽出的否决代码集合。"""
    return {r["code"] for r in LLF.extract_rejections(text)}


def _fixture_rows() -> list[dict]:
    """生产入口（只喂权威裁决段）⇒ 否决条目列表。"""
    return LLF.extract_authority_rejections(str(_FIXTURE_PATH))


def _fixture_codes() -> set[str]:
    return {r["code"] for r in _fixture_rows()}


# ---------------------------------------------------------------- 真机原文（2026-11-26）

#: 误判①：**讨论句**（辩论裁决段原文一字不改）。
#: 「要不要否决消费重复腿」是这场辩论在吵什么，**不是**裁决 ⇒ `512800` 不得被否。
_SNIPPET_DISCUSSION = (
    "这场辩论真正在吵三件事：512800 个股要不要减、要不要否决消费重复腿、权益总敞口该多大。"
    "前两件没有实质分歧，第三件分歧很大。我按「谁赢得多」逐层判。"
)

#: 误判②：**邻居标的连坐**（辩论裁决段原文一字不改）。
#: 「否决」的宾语只有 `515170`；`159928` 只是被提到的**邻居/对比对象** ⇒ 不得被否。
_SNIPPET_NEIGHBOR = (
    "1. **否决 515170**：与 159928 成分重叠 0.645、120 日相关 0.938，"
    "是「已入选对」里预警最强的一对；同时自身 mom120 仅 +0.21%、最终分 0.9539 是四选里最低。"
    "消费主题 40% 的重复暴露不值得付这个价。"
)

#: 误判②的同族形态：**监控邻居**（辩论裁决段原文一字不改）。
#: 「>1.5x ⇒ 否决首选」没点名任何标的 ⇒ 后面的 `159928`（另一张监控表）不得被连坐。
_SNIPPET_MONITOR = (
    "- 下一个调仓节点前，监控两张表：515450 放量倍数（现 1.29x，>1.5x ⇒ 否决首选）、"
    "159928 放量倍数（现 1.36x）、以及外部数据源是否恢复（恢复前不突破 60% 权益上限）。"
)

#: **真裁决**（最终交易决定段原文一字不改）⇒ 三只都**必须**被否。
_SNIPPET_VERDICT = "- **515170正式否决；512010、563020不建仓；防守槽全部留现金**"


# ---------------------------------------------------------------- 真机件回归

def test_真机件_否决集合恰好三只():
    """🔴 2026-11-26 真机件 ⇒ 否决集合必须**恰好** `{515170, 512010, 563020}`。"""
    codes = _fixture_codes()
    assert codes == {"515170", "512010", "563020"}, sorted(codes)


def test_真机件_512800不得被否():
    """🔴 误判①：`512800` 真实裁决是**减仓**（不是一票否决）⇒ 不得进否决名单。"""
    codes = _fixture_codes()
    assert "512800" not in codes, f"512800 被误判成否决（实测 {sorted(codes)}）"


def test_真机件_159928不得被否():
    """🔴 误判②：`159928` 真实裁决是**买入** ⇒ 不得进否决名单。"""
    codes = _fixture_codes()
    assert "159928" not in codes, f"159928 被误判成否决（实测 {sorted(codes)}）"


def test_真机件_三只真否决必须仍在名单():
    """🔴 收紧不许漏判：`515170 / 512010 / 563020` 必须**仍**被否（少判可以、漏判不行）。"""
    codes = _fixture_codes()
    for code in ("515170", "512010", "563020"):
        assert code in codes, f"{code} 是本轮真否决，却漏判了（实测 {sorted(codes)}）"


def test_真机件_出处章节必须仍是权威裁决段():
    """🔴 否决的**出处章节**仍须落在权威裁决段（研究经理/辩论裁决/风险裁决/Trader/最终交易决定）。"""
    allowed = {title for title, _paths in LLF.REJECT_SOURCE_SECTIONS}
    rows = _fixture_rows()
    assert rows, "真机件必须抽出否决条目"
    for row in rows:
        assert row["source_section"] in allowed, (
            f"{row['code']} 的出处章节不在权威段内：{row['source_section']}"
        )


# ---------------------------------------------------------------- 单元级：句式隔离

def test_讨论句_不得抽出否决():
    """🔴 「要不要否决消费重复腿」是讨论句 ⇒ `512800`（本行唯一代码）不得被否。"""
    assert _codes(_SNIPPET_DISCUSSION) == set(), sorted(_codes(_SNIPPET_DISCUSSION))


def test_邻居标的_不得连坐():
    """🔴 「否决 515170：与 159928 成分重叠」⇒ 只有 `515170` 被否。"""
    assert _codes(_SNIPPET_NEIGHBOR) == {"515170"}, sorted(_codes(_SNIPPET_NEIGHBOR))


def test_监控邻居_不得连坐():
    """🔴 「>1.5x ⇒ 否决首选」没点名标的 ⇒ 监控邻居 `159928` 不得被连坐。"""
    assert _codes(_SNIPPET_MONITOR) == set(), sorted(_codes(_SNIPPET_MONITOR))


def test_真裁决句_三只都否():
    """🔴 明确否决句 ⇒ `515170 / 512010 / 563020` 三只都否（不许被收紧误伤）。"""
    assert _codes(_SNIPPET_VERDICT) == {"515170", "512010", "563020"}, sorted(
        _codes(_SNIPPET_VERDICT)
    )


def test_直接绑定_只认否决动词绑住的那只代码():
    """🔴 行内「直接绑定」判定：否决动词只绑 `515170`，`159928` 不在其列。"""
    clause = "1. **否决 515170**：与 159928 成分重叠 0.645、120 日相关 0.938"
    vpos = clause.index("否决")
    bound = LLF._reject_verb_bound_codes(clause, vpos, len("否决"))
    assert bound == {"515170"}, bound


def test_点名条目_讨论语气不算裁决():
    """🔴 同族：点名条目的「方向『是否否决』」是**讨论** ⇒ 不得判成否决。"""
    text = "1) 512800（银行ETF华宝）：方向「是否否决」，维持 20%。"
    assert _codes(text) == set(), sorted(_codes(text))


def test_点名条目_真否决仍算否决():
    """🔴 同族收口：点名条目的「正式一票否决」**仍**是否决（不许被收紧误伤）。"""
    text = "4) 516620（影视ETF国泰）：**正式一票否决，不买**（E -0.3617）。"
    assert _codes(text) == {"516620"}, sorted(_codes(text))


def test_表格动作格_讨论语气不算裁决():
    """🔴 同族：表格动作格写「是否否决」⇒ 不是裁决。"""
    text = "| 512800 | 银行ETF | 是否否决 | 维持 20% |"
    assert _codes(text) == set(), sorted(_codes(text))


def test_讨论语气_疑问标记被识别():
    """🔴 「要不要否决 / 是否否决 / 可以否决吗」三句都必须被识别成**讨论**。"""
    for marker in ("要不要否决", "是否否决", "可以否决吗"):
        clause = f"这场辩论在吵：{marker}消费重复腿。"
        verbs = LLF._reject_verb_hits(clause)
        assert verbs, f"{marker} 应当命中否决动词（供讨论护栏作废）"
        assert LLF._discussion_verb_positions(clause, verbs) == {pos for pos, _w in verbs}, (
            f"{marker} 未被识别成讨论语气"
        )


# ---------------------------------------------------------------- 变异反证（收紧真在起作用）

def test_变异_明确否决句改成讨论句则该标的不再被否():
    """🔴 用户口径的变异反证：「否决 515170」⇒「要不要否决 515170」⇒ 515170 **不再**被否。

    ⇒ 同时证明「讨论语气」护栏**承重**（把它关掉 ⇒ 515450 又被误伤）。
    """
    mutated = _SNIPPET_NEIGHBOR.replace("**否决 515170**", "**要不要否决 515170**")
    assert "**要不要否决 515170**" in mutated, "变异必须真的改到句式"
    assert _codes(mutated) == set(), sorted(_codes(mutated))

    saved = LLF._REJECT_DISCUSSION_TAIL_RE
    try:
        LLF._REJECT_DISCUSSION_TAIL_RE = re.compile(r"(?!x)x")
        codes = _codes(mutated)
        assert "515170" in codes, "变异后应当重新误伤 515170（否则本变异无效）"
    finally:
        LLF._REJECT_DISCUSSION_TAIL_RE = saved


def test_变异_关掉直接绑定护栏则邻居标的重新被连坐():
    """🔴 变异反证：关掉「直接绑定」⇒ `159928` 重新被连坐（证明该护栏承重）。"""
    saved = LLF._reject_verb_bound_codes
    try:
        LLF._reject_verb_bound_codes = lambda clause, vpos, vlen: set()
        codes = _codes(_SNIPPET_NEIGHBOR)
        assert "159928" in codes, "变异后 159928 必须重新被误伤（否则本变异无效）"
        assert "159928" in _fixture_codes(), "真机件也必须重新误伤 159928（否则本变异无效）"
    finally:
        LLF._reject_verb_bound_codes = saved
    assert "159928" not in _codes(_SNIPPET_NEIGHBOR)
    assert "159928" not in _fixture_codes()


def test_判不准时宁可不判():
    """🔴 fail-closed：句子里**没有任何代码** ⇒ 不得凭空造出否决。"""
    text = "本轮对同族重复暴露保留一票否决权，必要时再启用。"
    assert _codes(text) == set(), sorted(_codes(text))


def test_前驱绑定_代码在动词之后也必须命中():
    """🔴 「`515170`正式否决」⇒ 动词在代码**之后** ⇒ 前驱绑定必须命中（含副词修饰）。"""
    text = "515170 正式否决（食品饮料ETF华夏）。"
    assert _codes(text) == {"515170"}, sorted(_codes(text))


def test_枚举前驱_并列代码整条都否():
    """🔴 「`512010`、`563020`不建仓」⇒ 枚举链上的两只都否（收紧不许漏判）。"""
    text = "- 512010、563020不建仓；防守槽全部留现金。"
    assert _codes(text) == {"512010", "563020"}, sorted(_codes(text))
