"""🔴 2026-11-17 新增：探针 A20⑥「**裸卖出只是默认推断**」护栏回归（章级豁免）。

事故来源（2026-11-17 19:06 真机 full mode 快照 `snapshots/20261117_190638`，
`probe_llm_parser` A20⑥ **红灯**）：

    [FAIL] A20⑥ 决策 md 里写明「清仓/不买」的代码 ⇒ 计划里必须零目标 + 零方向（+ 部分卖出≠清仓）
            最终裁决章节：表格方向列 0 个代码｜散文子句 1 个代码｜合计 1 个：['512800']
            违例 ['512800: 原文写明零方向，但计划目标=None（必须 0.0）']

⇒ **产品侧是对的**：`final_rebalance_plan.json` 里 512800 = `direction=减`、`target_pct=None`
   （真机原文明写「部分卖出、保留条件性核心仓」「卖出/减仓 `512800`」）⇒ 减仓目标未定。
⇒ 红的是**探针口径**：真机最终裁决章的**投研论证段**里另有一句

       「卖出 `512800` 后换入这些产品，只是同质替换，并不能实质降低银行主题风险。」

   ⇒ 旧口径把**每一处**裸「卖出」都算成**零方向指令** ⇒ 512800 被判「清仓」
   ⇒ 与计划（减 / 目标未定）自相矛盾 ⇒ **假红**。
⇒ 根因与生产解析器 `llm_final` 2026-11-11 的护栏**同源但不同宽**：
   解析器明写「**裸『卖出』只是默认推断**」——同一句里若该标的被明确说成「减仓 / 部分卖出」
   ⇒ 裸卖出**不生效**；探针却按**子句/句**切完就**并集**所有裸卖出 ⇒ 口径漂移。
   ⇒ 本轮修法：把这条护栏的作用域从**句**扩到**章**（`_md_zero_direction_codes`），
   **明写「清仓 / 全部卖出 / 不买 / 不建仓」的不受此豁免** ⇒ 真清仓照旧判零方向。

本文件钉死（自带夹具，**不依赖**服务器产物 ⇒ 原句内联）：

  ① **真夹具**：真机原句（Executive Summary + 投研论证段）⇒ 512800 **不是**零方向。
  ② **真·清仓照旧**：同一份原句里把「卖出」改成「清仓」⇒ 512800 **必须**进零方向集合
     （护栏不许把探针改废）。
  ③ **清仓优先于章级减仓证据**：原文明写「清仓 `512800`」且同章另有「部分卖出」证据
     ⇒ 512800 **仍必须**进零方向集合（⇒ 断言强度**没有**被降）。
  ④ **否定式清仓**（「也不一次性清仓」）与**条件句**（「若…应卖出更多」）⇒ 不是零方向
     （既有护栏，本轮**未动** ⇒ 钉住它们不被顺手改坏）。
  ⑤ **部分卖出护栏仍在**：「卖出超配部分、保留16%核心」⇒ 不是零方向。
  ⑥ **变异反证**：把章级豁免的两个承重点分别拆掉 ⇒ ① 的断言**必须转红**（证明护栏承重）；
     把真阴性文本改一个字（「卖出」→「清仓」）⇒ 必须命中零方向（证明护栏**精确**）。
  ⑦ **豁免 ≠ 放过**：被豁免出零方向集合的代码，**仍**被 A20⑥ 的另一半
     （「原文写的是部分卖出 ⇒ 计划方向必须是减」）钉住 ⇒ 计划方向=「卖」照样红
     （2026-11-13 真机 `snapshots/20261113_092926` 实证）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETF_DIR))

_PLP_PATH = _ETF_DIR / "probes" / "probe_llm_parser.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLP = _load("_plp_under_test", _PLP_PATH)
LLF = PLP.LF  # noqa: N815 —— 探针自己 import 的解析器（能力侧夹具用同一个）

#: 真机最终裁决章（`snapshots/20261117_190638`）的**原句**（内联 ⇒ 不依赖服务器产物）：
#:    · Executive Summary：明写「部分卖出、保留条件性核心仓」+「卖出/减仓 `512800`」；
#:    · 投研论证段：另有一句裸「卖出 `512800` 后换入这些产品…」（**红的那一句**）。
REAL_FINAL_SECTION = (
    "**Rating**: Underweight\n\n"
    "**Executive Summary**\n\n"
    "对 `512800` 执行**部分卖出、保留条件性核心仓**，不追涨加仓，也不一次性清仓。"
    "建议先卖出约 **4,700 份**，保留约 **3,000 份**。\n\n"
    "交易指令应明确写成“**卖出/减仓 `512800`**”，避免被理解为全额退出。\n\n"
    "**Investment Thesis**\n\n"
    "### 2. 全额清空的证据尚未压倒性，因而保留小核心\n\n"
    "- 515020、516310、512820与 `512800` 的成分重叠约0.986—0.991。"
    "卖出 `512800` 后换入这些产品，只是同质替换，并不能实质降低银行主题风险。\n"
)


def _md(body: str) -> str:
    """拼一份带「最终交易决定」章节的决策 md（探针与解析器都靠这个标题定位章节）。"""
    return f"## 六、最终交易决定（原始输出）\n\n{body}\n"


def _zero(body: str) -> set[str]:
    """决策 md 正文 ⇒ 零方向代码集合。"""
    codes, _note = PLP._md_zero_direction_codes(_md(body))
    return codes


# --------------------------------------------------------------------- ① 真夹具
def test_real_thesis_sentence_bare_sell_is_not_zero_direction() -> None:
    """① 真机原句：512800 是**减仓**（部分卖出 + 保留核心仓）⇒ **不是**零方向。"""
    codes = _zero(REAL_FINAL_SECTION)
    assert "512800" not in codes, f"512800 又被误判成零方向：{sorted(codes)}"


def test_real_thesis_sentence_matches_production_parser() -> None:
    """① 同口径：生产解析器对同一份原文给的是「减 / 目标未定」⇒ 探针不许判它清仓。"""
    targets = LLF.extract_final_decision(_md(REAL_FINAL_SECTION)).get("targets") or {}
    slot = targets.get("512800") or {}
    assert slot.get("direction") == "减", f"解析器方向变了：{slot}"
    assert slot.get("target_pct") is None, f"解析器目标变了：{slot}"
    codes = _zero(REAL_FINAL_SECTION)
    assert "512800" not in codes, f"探针与解析器口径漂移：{sorted(codes)}"


# --------------------------------------------------------------------- ② 真·清仓照旧
def test_one_word_change_to_clear_out_must_be_flagged() -> None:
    """② 把真阴性文本里的「卖出」改成一个字的「清仓」⇒ **必须**命中零方向集合。

    🔴 这是护栏**精确性**的证明：豁免只挡「裸卖出」，不挡明写清仓。
    """
    body = REAL_FINAL_SECTION.replace("卖出 `512800` 后换入这些产品",
                                     "清仓 `512800` 后换入这些产品")
    codes = _zero(body)
    assert "512800" in codes, f"真·清仓被放过（护栏过宽）：{sorted(codes)}"


def test_explicit_full_sell_is_still_zero_direction() -> None:
    """② 明写「全部卖出 / 清仓」⇒ 永远进零方向集合（护栏不许把探针改废）。"""
    body = ("全部卖出 `512800`，退出本轮组合。\n"
            "对 `159929` 执行**部分卖出、保留条件性核心仓**。\n")
    codes = _zero(body)
    assert "512800" in codes, f"真·清仓漏判：{sorted(codes)}"


# --------------------------------------------------------------------- ③ 清仓优先
def test_clear_out_wins_over_chapter_partial_evidence() -> None:
    """③ 同章既有「部分卖出」证据、又明写「清仓」⇒ **仍**判零方向（断言强度未降）。"""
    body = ("对 `512800` 执行**部分卖出、保留条件性核心仓**。\n"
            "另：本轮决定清仓 `512800`，不再保留核心仓。\n")
    codes = _zero(body)
    assert "512800" in codes, f"真·清仓被「减仓证据」放过（断言被削弱）：{sorted(codes)}"


def test_nobuy_is_still_zero_direction() -> None:
    """③ 「不买」也是零方向 ⇒ 不受章级豁免影响。"""
    body = ("对 `515450` 执行**部分卖出**；`159263` 不买，不建仓。\n")
    codes = _zero(body)
    assert "159263" in codes, f"真·不买漏判：{sorted(codes)}"


# --------------------------------------------------------------------- ④ 否定 / 条件
def test_negated_clear_out_is_not_zero_direction() -> None:
    """④ 「也不一次性清仓」⇒ 否定作用域 ⇒ **不是**零方向（既有护栏，本轮未动）。"""
    body = "对 `512800` 执行**部分卖出、保留条件性核心仓**，也不一次性清仓。\n"
    codes = _zero(body)
    assert "512800" not in codes, f"否定式清仓被误判成零方向：{sorted(codes)}"


def test_conditional_sell_more_is_not_zero_direction() -> None:
    """④ 「若实时价格…应卖出更多」⇒ 条件句 ⇒ **不是**零方向（既有护栏，本轮未动）。"""
    body = ("对 `512800` 执行**部分卖出、保留条件性核心仓**。\n"
            "若实时价格因上涨而使 `512800` 权重进一步超过9%，应卖出更多。\n")
    codes = _zero(body)
    assert "512800" not in codes, f"条件句被误判成零方向：{sorted(codes)}"


# --------------------------------------------------------------------- ⑤ 部分卖出
def test_partial_sell_with_keep_core_is_not_zero_direction() -> None:
    """⑤ 「卖出超配部分、保留16%核心」⇒ 部分卖出 ⇒ **不是**零方向（既有护栏仍在）。"""
    body = "`512800`卖出超配部分、保留16%核心。\n"
    codes = _zero(body)
    assert "512800" not in codes, f"部分卖出被误判成零方向：{sorted(codes)}"


def test_bare_sell_without_partial_evidence_is_zero_direction() -> None:
    """⑤ 反过来：只有裸「卖出」、**没有**任何减仓证据 ⇒ **必须**判零方向（不许放宽）。"""
    body = "本轮卖出 `512800`，退出本轮组合。\n"
    codes = _zero(body)
    assert "512800" in codes, f"裸卖出漏判（断言被削弱）：{sorted(codes)}"


# --------------------------------------------------------------------- ⑥ 变异反证
def test_guard_is_load_bearing_without_sentence_partial_codes(monkeypatch) -> None:
    """⑥ 拆掉「章级减仓/部分卖出证据」的采集 ⇒ ① 的断言**必须转红**（护栏承重）。"""
    monkeypatch.setattr(PLP, "_md_sentence_partial_sell_codes", lambda sentence: set())
    codes = _zero(REAL_FINAL_SECTION)
    assert "512800" in codes, "拆掉章级证据后 512800 仍未进零方向集合 ⇒ 夹具不敏感"


def test_guard_is_load_bearing_without_partial_sell_marker(monkeypatch) -> None:
    """⑥ 拆掉生产常量里的**全部**「减仓 / 部分卖出」标记 ⇒ ① 的断言**必须转红**（护栏承重）。

    🔴 真机原句里**同时**有「部分卖出」与「卖出/减仓 `512800`」两处证据
       ⇒ 只摘掉「部分卖出」一个词不够（另一处证据还在）⇒ 这里把标记表**清空**。
    """
    monkeypatch.setattr(PLP, "_MD_PARTIAL_SELL_MARKERS", ())
    codes = _zero(REAL_FINAL_SECTION)
    assert "512800" in codes, "拆掉「部分卖出」标记后 512800 仍未进零方向集合 ⇒ 夹具不敏感"


def test_guard_does_not_shield_other_codes(monkeypatch) -> None:
    """⑥ 护栏**只**按代码豁免：另一只代码的裸卖出**仍**判零方向（不许整章豁免）。"""
    body = ("对 `512800` 执行**部分卖出、保留条件性核心仓**。\n"
            "卖出 `159929`，退出本轮组合。\n")
    codes = _zero(body)
    assert "512800" not in codes, f"512800 被连坐：{sorted(codes)}"
    assert "159929" in codes, f"159929 被放过（豁免外溢）：{sorted(codes)}"


# ------------------------------------------------- ⑦ 豁免 ≠ 放过（另一半断言）
def test_exempted_code_is_still_pinned_by_partial_sell_direction_rule() -> None:
    """⑦ 被豁免出零方向集合的代码，**仍**被「部分卖出 ⇒ 方向必须是减」这条钉住。

    🔴 2026-11-13 真机 `snapshots/20261113_092926` 实证：计划把
       「执行部分卖出而非清仓」写成「卖 / 0%」⇒ 必须抓红
       ⇒ **豁免（零方向集合）≠ 放过（这条仍然红）**。
    """
    section = PLP.LF.extract_section(_md(REAL_FINAL_SECTION))
    bad = PLP._md_partial_sell_direction_violations(section, {"512800": {"direction": "卖"}})
    assert bad, "计划方向='卖'（清仓）却没被抓 ⇒ 豁免变成放过 ⇒ 断言被削弱"
    assert any("512800" in item for item in bad), f"违例没点名 512800：{bad}"


def test_partial_sell_direction_ok_when_plan_says_reduce() -> None:
    """⑦ 计划方向=「减」⇒ 这条不红（真机本轮的正解：512800 = 减 / 目标未定）。"""
    section = PLP.LF.extract_section(_md(REAL_FINAL_SECTION))
    assert PLP._md_partial_sell_direction_violations(
        section, {"512800": {"direction": "减"}}) == [], "计划方向=减 却仍报违例"
