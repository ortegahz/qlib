"""🔴 2026-11-17 新增：**裸「卖出」的豁免必须做到「章级」**（与交付面探针同口径）。

【真机事故（交付件 `snapshots/20261117_210415`）】
最终交易决定章（Trader 的原始输出）里写：

    1. **减仓方向由超配事实和风险证据共同决定。**
       三位风险分析员都同意 512800 应当卖出，分歧仅在于卖出后是否清零。
    2. **保留标准参与权优于直接清零。**
       激进派与中立派关于"减仓而不清仓"的论证更有说服气：512800 最终分 1.3372…

⇒ 旧口径只在**同一句**里找「减仓/部分卖出」证据 ⇒ 第一句（**章内别处**才说「减仓
   而不清仓」）把 512800 判成「卖 + 0%」（= 清仓）⇒ 与同章原文自相矛盾
⇒ 交付面探针 `probe_llm_parser` 的 A20⑥ 判红（且交付件把「减至 16%」写成「清零」）。

【修法】`llm_final.extract_final_decision`：把「减仓/部分卖出」证据汇总到**章级**
⇒ 命中该代码时**裸卖出不生效**（明写「清仓/全部卖出/不买/不建仓」**不受**此豁免影响）。
"""

from __future__ import annotations

import llm_final as LLF


_SNIPPET_CHAPTER = (
    "## 六、最终交易决定\n"
    "**Executive Summary**:\n"
    "对 512800 执行 **Sell**，但明确限定为**卖出超配部分，而非清仓**。\n"
    "\n"
    "**Investment Thesis**:\n"
    "1. **减仓方向由超配事实和风险证据共同决定。**\n"
    "   三位风险分析员都同意 512800 应当卖出，分歧仅在于卖出后是否清零。\n"
    "2. **保留标准参与权优于直接清零。**\n"
    "   激进派与中立派关于“减仓而不清仓”的论证更有说服气：512800 最终分 1.3372。\n"
)

_SNIPPET_TRUE_CLEAR = (
    "## 六、最终交易决定\n"
    "对 512170 执行 **全部卖出**，本轮一票否决该标的。\n"
)


def _slot(code: str, text: str) -> dict:
    out = LLF.extract_final_decision(text)
    return out["targets"].get(code, {})


def test_章级部分卖出证据使裸卖出不生效() -> None:
    """章内别处写了「减仓而不清仓」⇒ 512800 方向必须是「减」，**不许**是「卖 + 0%」。"""
    slot = _slot("512800", _SNIPPET_CHAPTER)
    assert slot.get("direction") == "减", (
        f"方向必须是「减」（实测 {slot.get('direction')!r}，证据：{slot.get('evidence')!r}）"
    )
    assert slot.get("target_pct") != 0.0, "不许把部分卖出写成清仓 0%"


def test_明写清仓不受章级豁免影响() -> None:
    """明写「全部卖出」⇒ 仍然是「卖 + 0%」（豁免**不许**把真清仓放过）。"""
    slot = _slot("512170", _SNIPPET_TRUE_CLEAR)
    assert slot.get("direction") == "卖", f"明写清仓必须是卖（实测 {slot.get('direction')!r}）"
    assert slot.get("target_pct") == 0.0, "明写清仓的目标必须是 0%"


def test_变异反证_章级豁免失效后必须变红() -> None:
    """把章级汇总打掉（回到句级）⇒ 上一条必须变红（证明这层护栏真在承重）。"""
    saved = LLF._sentence_partial_sell_codes
    try:
        # 整个「部分卖出」证据源被关掉 ⇒ 句级 + 章级豁免**同时**失效
        LLF._sentence_partial_sell_codes = lambda sentence: set()
        slot = _slot("512800", _SNIPPET_CHAPTER)
        assert slot.get("direction") == "卖", (
            "变异后应当回到「卖」（证明章级豁免承重）"
        )
    finally:
        LLF._sentence_partial_sell_codes = saved
