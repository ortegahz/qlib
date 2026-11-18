"""🔴 2026-11-10 新增：最终裁决的**否定作用域** + **千分位** + **子句切分**回归。

事故来源（2026-11-10 上午服务器闸门复跑，`probe_llm_parser` A20⑥ 红灯）：

  LLM 在「最终交易决定」章节里写：

      采纳本轮**战术性卖出、降低权益敞口**，但不把 `512800` 一次清仓。
      - `512800`：按参考价 **0.874** 卖出约 **2,600份**，保留约 **5,100份**。
      预计其他四只卖出回笼约 **7,606元**，`512800`减持回笼约 **2,272.40元**。
      这里的“Sell”指本轮执行净卖出和显著降险，并非在当前证据下将 `512800` 清零。

  三个缺陷叠在一起，把 512800 的真实裁决（「减至约 15%」）整条覆盖成了「清仓 0%」：

    ① **否定作用域太窄**：`_negated()` 只看动作词**前 4 个字**（这里是「一次」），
       否定词「不」落在窗口外 ⇒ 「清仓」被当成**清仓指令** ⇒ 方向 = 卖、目标 = 0%。
    ② **千分位被腰斩**：探针 `_md_clauses()` 连**半角逗号**一起切 ⇒ 「卖出约 **2,600份**」
       被切成「卖出约 **2」＋「600份**…」⇒ 「部分卖出」正则匹配不上 ⇒ 又落回裸「卖出」。
    ③ **markdown 强调挡住数量正则**：「卖出约 **2,600份**」里动词与数字之间夹着 `**`
       ⇒ 产品侧 `_ACTION_RULES` ② 也匹配不上。

  本文件钉死四件事（自带夹具，不依赖服务器产物）：

    ① **否定是子句级作用域**：否定标记与动作词之间没有转折词/别的代码 ⇒ 动作被否定；
       有转折词（「但」「却」「而是」…）⇒ 否定**管不到**后面的动作。
    ② **千分位不许被切**：子句切分只切全角「，」⇒ 「2,600份」完整。
    ③ **带数量的卖出 = 部分卖出（减）**，哪怕中间夹着 markdown 强调标记。
    ④ **变异反证**：把三处修复分别改回旧口径 ⇒ 对应断言必须**转红**（证明本测试有牙）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"
_PLP_PATH = _ETF_DIR / "probes" / "probe_llm_parser.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load("_llf_under_test", _LLF_PATH)
PLP = _load("_plp_under_test", _PLP_PATH)

#: 真实事故件里「最终交易决定」章节的散文片段（截取，改动最小）
_MD_SNIPPET = (
    "## 六、最终交易决定\n\n"
    "采纳本轮**战术性卖出、降低权益敞口**，但不把 `512800` 一次清仓。\n\n"
    "- `512800`：按参考价 **0.874** 卖出约 **2,600份**，保留约 **5,100份**。\n\n"
    "预计其他四只卖出回笼约 **7,606元**，`512800`减持回笼约 **2,272.40元**。\n\n"
    "这里的“Sell”指本轮执行净卖出和显著降险，并非在当前证据下将 `512800` 清零。\n\n"
    "`159929`、`513330`：全部卖出，退出本轮组合。\n"
)


# --------------------------------------------------------------------------- ① 否定作用域
def test_negation_scope_is_clause_level() -> None:
    """① 「但不把 512800 一次清仓」⇒ 清仓被否定；「并非…清零」同理。"""
    assert LLF._negated("但不把 `512800` 一次清仓", "但不把 `512800` 一次清仓".index("清仓")) is True
    assert LLF._negated("并非在当前证据下将 `512800` 清零",
                        "并非在当前证据下将 `512800` 清零".index("清零")) is True
    # 真清仓**不许**被误判成否定
    assert LLF._negated("`159929`、`513330`：全部卖出", 0) is False


def test_turnaround_word_breaks_negation_scope() -> None:
    """① 转折词把否定作用域切断 ⇒「不清仓但要减仓」的「减仓」不被否定。"""
    text = "512800 不清仓但要减仓"
    assert LLF._negated(text, text.index("清仓")) is True
    assert LLF._negated(text, text.index("减仓")) is False


def test_action_word_with_own_negation_is_not_negated() -> None:
    """① 动作词自带否定（「不买」「不建仓」）⇒ 本身就是零方向指令。"""
    text = "515450 不买入"
    assert LLF._negated(text, text.index("不买")) is False


# --------------------------------------------------------------------------- ② 千分位
def test_clause_split_keeps_thousands_separator() -> None:
    """② 子句切分只切全角「，」⇒「2,600份」不许被腰斩。"""
    text = "- `512800`：按参考价 **0.874** 卖出约 **2,600份**，保留约 **5,100份**"
    clauses = PLP._md_clauses(text)
    assert len(clauses) == 2, f"子句切分数量不对：{clauses}"
    assert "2,600份" in clauses[0], f"千分位被腰斩：{clauses[0]!r}"
    assert "5,100份" in clauses[1]


def test_sell_with_quantity_is_partial_sell_even_with_markdown() -> None:
    """③ 「卖出约 **2,600份**」= 部分卖出（减），不是清仓。"""
    text = "- `512800`：按参考价 **0.874** 卖出约 **2,600份**"
    assert PLP._md_has_partial_action(text) is True
    assert PLP._md_clause_zero_word(text) is None


# --------------------------------------------------------------------------- ③ 端到端
def test_real_snippet_512800_is_reduce_not_liquidate() -> None:
    """①+③ 合起来：真实片段里 512800 必须是「减」，且**不是**零方向。"""
    res = LLF.extract_final_decision(_MD_SNIPPET)
    slot = (res.get("targets") or {}).get("512800") or {}
    assert slot.get("direction") == "减", f"512800 方向错：{slot}"
    assert not slot.get("target_pct"), f"512800 目标权重不该是 0：{slot}"
    zero_codes, _note = PLP._md_zero_direction_codes(_MD_SNIPPET)
    assert "512800" not in zero_codes, f"512800 被误判成零方向：{sorted(zero_codes)}"
    # 真清仓的那只仍必须在零方向集合里（不许把探针改废）
    # ℹ 已知口径（**不是**本轮引入）：`_md_clause_codes` 的「连写代码」判定只认
    #    「、/空格/与/和/及」等连接词，**不认反引号** ⇒ 散文里写成
    #    「`159929`、`513330`：全部卖出」时，**只有第一个**代码被归到该子句。
    #    ⇒ 这里只断言第一个（`159929`）；扩大口径是另一件事（会改变零方向集合的大小），
    #    不在本轮「修 A20⑥ 假红」的范围里。
    assert "159929" in zero_codes, f"真清仓漏判：{sorted(zero_codes)}"


def test_evidence_is_recorded_for_direction_only_rules() -> None:
    """③ 「只给方向、不给目标权重」的档也必须记下证据句（可回放不断链）。"""
    res = LLF.extract_final_decision(_MD_SNIPPET)
    slot = (res.get("targets") or {}).get("512800") or {}
    assert slot.get("evidence"), "证据句为空 ⇒ 交付件「可回放」断链"
    assert slot["evidence"] in _MD_SNIPPET


# --------------------------------------------------------------------------- ④ 变异反证
def test_mutants_are_caught() -> None:
    """④ 把三处修复分别改回旧口径 ⇒ 对应断言必须转红（本测试有牙）。"""

    def _old_negated(text: str, pos: int, matched: str = "") -> bool:
        """修复前的口径：只看动作词**前 4 个字**。"""
        window = text[max(0, pos - 4):pos]
        return any(marker in window for marker in LLF._NEG_MARKERS)

    text = "但不把 `512800` 一次清仓"
    assert _old_negated(text, text.index("清仓")) is False, "旧口径没复现事故 ⇒ 夹具不敏感"
    assert LLF._negated(text, text.index("清仓")) is True

    def _old_clauses(sentence: str) -> list[str]:
        """修复前的口径：连**半角逗号**一起切 ⇒ 千分位被腰斩。"""
        import re
        return [c for c in re.split(r"[，,]", sentence or "") if c.strip()]

    raw = "- `512800`：按参考价 **0.874** 卖出约 **2,600份**，保留约 **5,100份**"
    assert "2,600份" not in _old_clauses(raw)[0], "旧口径没复现腰斩 ⇒ 夹具不敏感"
    assert "2,600份" in PLP._md_clauses(raw)[0]

    def _old_has_partial_action(clause: str) -> bool:
        """修复前的口径：数量正则不容纳 markdown 强调标记 ⇒ 匹配不上「卖出约 **2,600份**」。"""
        import re
        return bool(re.search(r"(?:卖出|卖掉|减掉)\s*(?:约)?\s*[\d,]+(?:\.\d+)?\s*(?:股|份)",
                              clause or ""))

    qty = "- `512800`：按参考价 **0.874** 卖出约 **2,600份**"
    assert _old_has_partial_action(qty) is False, "旧口径没复现 ⇒ 夹具不敏感"
    assert PLP._md_has_partial_action(qty) is True
