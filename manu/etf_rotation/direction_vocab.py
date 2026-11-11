#!/usr/bin/env python
"""LLM 方向词表 —— **解析器与语义守卫共用的单一事实源**。

🔴 2026-11-11（critic 生产代码审计 · 高危）修：**两份词表分叉**。
   · 解析器 `final_rebalance_plan._ZERO_DIR_WORDS` = {卖, 卖出, 卖掉, 清仓, 清掉,
     清零, 不买, 不建仓, 退出}
   · 守卫 `verify_artifacts.LLM_DIRECTION_ZERO` = {不买, 清仓, 清掉, 清, 卖出, 卖,
     退出, Sell}
   ⇒ 解析器 `_classify_dir_cell` **能吐出**「清」「Sell」，二者**不在**解析器的
     ZERO 档 ⇒ 同类「清掉+8%」不会被强制归零，只会被守卫**拦死**（响亮但未修净）；
   ⇒ 反向：解析器词表里的「卖掉/清零/不建仓」在解析器侧是**死条目**
     （`_LLM_DIR_CELL` 永远吐不出这三个词）⇒ 词表名义上有、实际不生效。
   ⇒ 现在两边都从**本模块**取词表 ⇒ 单一事实源，词表分叉从根上消失。

口径来源（同一套语义，三处实现共用同一批词）：
   · `llm_final._ACTION_RULES` ① 清仓类（全部卖出/卖出全部/清仓/清掉/清零）⇒ 目标 0%
   · `llm_final._ACTION_RULES` ② 带数量的卖出 ⇒ **减**（部分卖出，不是清仓）
   · `llm_final._ACTION_RULES` ④ 裸「卖出/卖掉」 ⇒ 目标 0%
   · `llm_final._ACTION_RULES` ⑥ 「不建仓」「不买」 ⇒ 目标 0%
"""

from __future__ import annotations

#: 方向 ∈ ZERO ⇒ 目标权重**必然是 0%**（清仓 / 不买 / 不建仓 / 退出）
ZERO = frozenset({
    "卖", "卖出", "卖掉", "清仓", "清掉", "清零", "清",
    "不买", "不建仓", "退出", "Sell",
})

#: 方向 ∈ POS ⇒ 目标权重必须 > 0%
POS = frozenset({
    "买", "买入", "加仓", "增持", "建仓", "Buy", "Overweight",
})

#: 方向 ∈ NEUTRAL ⇒ **不设约束**（持有 / 保留 / 维持 / 不卖）
NEUTRAL = frozenset({
    "不卖", "持有", "保留", "维持", "持", "留", "Hold", "Neutral", "Keep",
})

#: 方向 ∈ REDUCE ⇒ 目标权重必须 < 当前权重（**当前权重可比时**才成立）
REDUCE = frozenset({
    "减持", "减仓", "减", "Reduce", "Underweight",
})

#: 方向 ∈ INCREASE ⇒ 目标权重必须 > 当前权重（**当前权重可比时**才成立）
INCREASE = frozenset({
    "增", "加", "Increase", "Add",
})

#: 全部已分类的方向词（供「未分类」判定用）
ALL = ZERO | POS | NEUTRAL | REDUCE | INCREASE
