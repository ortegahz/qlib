#!/usr/bin/env python
"""从 TradingAgents 决策里抽「最终交易决定」（Trader 最终裁决）——结构化。

🔴 **为什么必须单独抽这一层（M-P，2026-11-05 04:3x 实测）**
------------------------------------------------------------------
`final_rebalance_plan.py` 的 LLM 层（`llm_targets` / `llm_conflicts`）**只从表格取数**
⇒ 而 Trader 的**最终裁决写在「六、最终交易决定」章节的散文里**（该章节**没有表格**）
⇒ `_LLM_SECTIONS` 里虽然登记了 `("final", ("最终交易决定", ...))`，但**没有表格行** ⇒
**它对 LLM 层零贡献** ⇒ 交付件里的「LLM 目标」永远是**多头表/空头表**的数。

实测（官方决策 md `58ae42c3…`）：最终裁决与两张表**三处都不一样**：

| 标的 | 多头表 | 空头表 | **最终裁决（原文）** |
|:--|--:|--:|:--|
| 512800 | 20% | 10% | **12%**（保留约 4,100 股、卖出约 3,600 股；硬上限 15%、下限 0） |
| 511880 | 33.5% | 0%（不买） | **买 1 手**（≈10,000 元 / 33.5%；溢价>0.3% 则全额留现金） |
| 563020 | 10% | 0%（不买） | **不建仓**（0%） |

⇒ 冲突表按「与规则层**差得最远**」取行 ⇒ 报出来的是**空头的 10%**和**空头的「不买」**
⇒ **用户读到的「LLM 结论」与 Trader 的最终裁决不一致**（511880 上甚至是相反结论）⇒
**必须把最终裁决提升为交付件的一等公民**。

🔴 **口径（铁律）**
------------------------------------------------------------------
1. **只抽原文明确写出的数字**；抽不出 ⇒ `target_pct=None` + 说明，**绝不编造**；
2. 每条抽取都带**原文证据句**（`evidence`）⇒ 可逐条回放校验；
3. **散文**只认「**最终交易决定**」章节 ⇒ 不把多头/空头/研究经理的正文混进来；
   （**结构化决策表**是例外：裁决区＝研究经理结论 / Trader 交易计划 / 最终交易决定，
   只认**目标列**、**只补空** ⇒ 观点表**没有裁决权**）
4. **动作类词**（卖出/不建仓/不买/买 N 手）按**子句**判定 ⇒ 一个子句里多个代码一起成立
   （如「其余持仓 159929、513330、513770、512690 全部卖出」）；
5. **权重类数字**（目标权重降至 X% / 保留 N 股 / 硬上限 / 下限 / 止损）按**句**判定，
   且只归属**该句里离它最近的、在它前面的那个代码** ⇒ 避免「把一个代码的数安到别人头上」；
6. 只有**明确带权重名词**（目标权重/权重/仓位/敞口/权益）才认「降至 X%」⇒
   「降至 0%~10%」这种**条件句**（没有权重名词）**不抽** ⇒ 不会污染主裁决。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: 6 位代码（ETF / 股票）
#: 🔴 2026-11-05 10:3x（M-Q 修）：**首位不能是 0**。
#:    实测：新决策里「最终分差仅 0.006126 / 0.019181」⇒ 旧正则把 "006126" 当成
#:    一个 6 位代码 ⇒ `unparsed_codes` 里冒出根本不存在的标的 "006126"。
#:    A 股/ETF 代码首位恒为 1~6 ⇒ 加 `(?<![\d.])[1-9]\d{5}(?!\d)`。
_CODE_RE = re.compile(r"(?<![\d.])([1-9]\d{5})(?!\d)")

#: 「最终交易决定」章节的标题关键词（小写匹配）
_FINAL_HEADINGS = ("最终交易决定", "最终交易决策", "final trade decision",
                   "final_trade_decision", "最终裁决")

#: 评级行：**Rating**: Underweight
#: 🔴（2026-11-09 独立审计实证）：**粗体可有可无** ——
#:    实测 2026-11-09 这一轮 LLM 写的是**裸**「Rating: Underweight」（无粗体）
#:    ⇒ 旧正则要求 `**Rating**` ⇒ 评级**整条漏抽**（计划里 rating=null）
#:    ⇒ 交付件少了一个权威字段。
#: 🔴 2026-11-09 补 **左词边界**：旧正则没有左边界 ⇒ md 里若出现
#:    「RiskRating:」「CreditRating:」这类**更长词**里的 Rating 子串，
#:    `search` 会从**全文第一个匹配处**抽出**错误的值**
#:    ⇒ 评级被污染（把风险评级 / 信用评级当成 Trader 的最终评级）。
#:    ⇒ 修法：`Rating` 前加 `(?<![A-Za-z])` ⇒ 裸写 `Rating:` 与 `**Rating**:` 都认，
#:      而 `RiskRating:` / `CreditRating:` 抽不到（它们是**别的词**，不是评级行）。
_RATING_RE = re.compile(r"\*{0,2}(?<![A-Za-z])Rating\*{0,2}\s*[:：]\s*(?P<v>[^\n*]{1,40})")

#: 🔴（2026-11-09 独立审计实证）修：「**已触发**」是**既成事实**，不是条件。
#:    实测事故：LLM 最终表写
#:      「| **159929** | **卖出** | 0% | 已触发持仓MA60强制退出，继续持有会使已有浮盈重新暴露 |」
#:    ⇒ `_is_conditional` 看首子句里有「**触发**」⇒ 判成条件句 ⇒ **整行被跳过**
#:    ⇒ 159929 / 513330 / 513770 三条**真裁决**（清仓）整条漏抽，落进 unparsed_codes
#:    ⇒ 交付件的「最终裁决层」少了三条清仓指令。
#:    ⇒ 修法：判条件前先抹掉「已/已经/均/皆/都 + 触发」这类**既成事实**；
#:      「触发后 / 触发则 / 触发时 / 触发条件」仍按条件句处理。
_FACT_TRIGGER_RE = re.compile(r"(?:已|已经|均|皆|都|全部)?触发(?![后则时条])")

#: Executive Summary（多行）—— 取到空行为止
#: 🔴 2026-11-05 10:0x（M-Q 修）：**冒号是可选的**。
#:    实测 09:22 那份决策 md 写的是「**Executive Summary**\n\n最终决策是：…」
#:    ⇒ 旧正则要求 `[:：]` ⇒ 匹配失败 ⇒ 交付件里「原文摘要」显示「（无）」
#:    ⇒ 用户看不到 Trader 自己的结论原话（而这正是「可回放」最该给的东西）。
_SUMMARY_RE = re.compile(r"\*\*Executive Summary\*\*\s*(?:[:：]\s*)?(?P<v>.+?)(?=\n\s*\n|\Z)",
                          re.S)

#: 「目标权重/权重/仓位/目标/风险预算 … 降至/降到 X%」——**单只代码层**的名词。
#:
#: 🔴 2026-11-05 10:3x（M-Q 修）：**组合层名词必须拆出去**。
#:    实测：新决策 Executive Summary 写「511880/511660/511990 三只防守 ETF 与
#:    563020 均不买入，**权益敞口由 48.02% 降至约 15%**」
#:    ⇒ 旧正则把「权益敞口」当单只代码的权重名词 ⇒ 最近码规则把 **15% 记到
#:    563020 头上**（563020 的真实裁决是 **0% 不买**）⇒ 交付件里出现
#:    「563020 目标 15%」的幽灵数字。
#:    ⇒ **「权益敞口/总权益/敞口/权益」一律走组合层字段，永不归给单只代码**。
_CODE_PCT_NOUNS = r"目标权重|权重|仓位|目标|风险预算"
#: 组合层（**不是**单只代码）权重名词
_PORTFOLIO_PCT_NOUNS = r"权益敞口|总权益|敞口|权益"

#: 「目标权重/权重/仓位/目标/风险预算 … 降至/降到 X%」——**必须带权重名词**
#: 「动词 → 数字」之间允许的**计量对象**（**闭集**，不是模糊窗口）。
#: 🔴 2026-11-05 10:4x（M-Q 修）：实测「**目标权重降至总资产的约 8%**」
#:    ⇒ 旧正则要求数字**紧跟**动词（中间只允许空白/约/强调号）⇒ 抽不出 8%
#:    ⇒ 512800 的目标退化成后面那句「权重约 7.90%」⇒ 与原文明写的「约 8%」差 0.1pp。
#:    ⇒ 允许「的 + 计量对象（总资产/本金/…）」这一**封闭**结构，不放开任意字符窗口。
_MEASURE_NOUNS = (r"总资产|账户资产|总市值|市值|净值|本金|总权益|权益|总仓位|仓位")

#: 「动词 → 数字」之间允许的**基准限定词**（**闭集**，不是模糊窗口）。
#: 🔴 2026-11-11 实测（2026-11-03 那一轮决策 md 实跑）：Trader 最终裁决写
#:    「将当前22.54%的仓位降至**基准约10%、硬上限10%**的战术观察仓」
#:    ⇒ 旧口径抽不出目标权重（`降至` 与 `10%` 之间夹着「**基准约」，
#:      而「基准」不在 `_MEASURE_NOUNS` 闭集里）⇒ `target_pct=None`
#:    ⇒ `make_decision_options` 把 512800 判成「最终裁决抽不出数字」⇒
#:      **真实分歧（规则层 20% vs LLM 10%）被静默丢掉** ⇒ 「待拍板分歧 0 项」
#:      ⇒ `make_decision_scenarios` 直接抛错 ⇒ 重建链转红。
#:    ⇒ 现在允许「基准/中枢/参考值」这三个**闭集**限定词（依旧不是任意字符窗口）。
_PCT_QUALIFIER_NOUNS = r"基准|中枢|参考值"

_TARGET_PCT_RE = re.compile(
    r"(?:" + _CODE_PCT_NOUNS + r")"
    r"[^，。；]{0,14}?"
    r"(?:降至|下调至|下调到|调至|调到|调整至|调整到|压缩至|压缩到|"
    r"定为|设为|定在|定到|控制在|压到|限定在|降|到|至)"
    r"\s*(?:的\s*)?(?:" + _MEASURE_NOUNS + r")?(?:的\s*)?"
    r"[\*（(]{0,3}\s*"
    r"(?:" + _PCT_QUALIFIER_NOUNS + r")?[\*（(]{0,3}\s*"
    r"(?:约|大约|约等于|≈)?\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%")

#: 组合层「权益敞口 … 降至 X%」——**不进单只代码槽位**
_PORTFOLIO_PCT_RE = re.compile(
    r"(?:" + _PORTFOLIO_PCT_NOUNS + r")"
    r"[^，。；]{0,14}?"
    r"(?:降至|下调至|下调到|调至|调到|调整至|调整到|压缩至|压缩到|"
    r"定为|设为|定在|定到|控制在|压到|限定在|降|到|至)"
    r"\s*(?:的\s*)?(?:" + _MEASURE_NOUNS + r")?(?:的\s*)?"
    r"(?:约|大约|约等于|≈)?\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%")

#: 🔴 2026-11-11（审计缺陷 B）：`equity_target_pct` 的**严格目标动词表**。
#:
#: ⚠️ **不许**复用 `_PORTFOLIO_PCT_RE`（那是 `equity_pct` 的宽松口径，**既有金标一个字都不许动**）：
#:    它的动词表末尾是**裸介词** `降|到|至` ⇒ 「**回升至 56%**」「**回到 56%**」这类
#:    **预测/陈述**句会被当成**目标** ⇒ 规则层据此把权益从 80% **静默砍到 56%**（24pp 仓位）。
#:    ⇒ 这里单列一条**只认成词目标动词**的正则：裸「到 / 至」**不算目标**。
#: 🔴 **反向动词一律不算目标**：「提高到 / 上调至 / 回升至 / 回到 / 升至」是**加仓或预测**
#:    ⇒ 解析不出 ⇒ **不压降**（宁可不压降，也**不许**把「想加仓」读成「要减仓」）。
_PORTFOLIO_TARGET_PCT_RE = re.compile(
    r"(?:" + _PORTFOLIO_PCT_NOUNS + r")"
    r"[^，。；]{0,14}?"
    r"(?:降至|下调至|下调到|调至|调到|调整至|调整到|压缩至|压缩到|"
    r"压降至|压降到|压到|降到|定为|设为|定在|定到|控制在|限定在|"
    r"不超过|不高于|上限为|最高)"
    r"\s*(?:的\s*)?(?:" + _MEASURE_NOUNS + r")?(?:的\s*)?"
    r"(?:约|大约|约等于|≈)?\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%"
)

#: 「目标约 15%」——**必须带「约/大约/≈」**。
#:
#: 🔴 2026-11-05 09:3x 实测（M-Q 修）：Trader 写「现阶段风险预算**定为约** 15%」
#:    （**名词+动词**齐全，但「风险预算/定为」都不在旧表里 ⇒ 抽不出）
#:    旧实现转而抓到了**条件句**里的「将仓位进一步降至约10%」⇒ 计划写成 10%、
#:    真实裁决是 15% ⇒ **方向错**。
#:    ⇒ 补一条**松**规则：只认「权重名词 + 约 + 数字%」。
#:    🔴 **「约」是硬要求**：否则「明显低于机械目标 20%」会把 20% 误当成目标权重。
_TARGET_PCT_LOOSE_RE = re.compile(
    r"(?:" + _CODE_PCT_NOUNS + r")"
    r"\s*(?:约|大约|约等于|≈)\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%")

#: 组合层松匹配：「权益敞口约 X%」
_PORTFOLIO_PCT_LOOSE_RE = re.compile(
    r"(?:" + _PORTFOLIO_PCT_NOUNS + r")"
    r"\s*(?:约|大约|约等于|≈)\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%")

#: 🔴 2026-11-13（真实交付件 `snapshots/20261112_181544` 实证）：**代码锚定**的紧致写法。
#:
#: Trader 的最终裁决常把整套方案压成**一句**「代码 + 目标设定词/档位 + 数字%」：
#:     「本轮最终采用**"`512800`减至20%、`515450`标准档20%、否决`515650`、
#:       其余弱仓退出、权益上限40%、约60%现金"**的方案」
#: ⇒ 旧口径**只认「权重名词（目标权重/权重/仓位/目标/风险预算）+ 动词 + 数字%」**
#:    ⇒ 这两只都**抽不出** ⇒ `target_pct=None` ⇒ `unparsed_codes` 里挂 `512800`/`515450`
#:    ⇒ `make_decision_scenarios` 硬失败（「不许用默认值蒙混」）⇒ 重建链转红
#:    （2026-11-12 夜跑实测：`llm_final_decision.targets[515450]缺少必需字段 target_pct`）。
#:
#: 🔴 **口径纪律**（**不许**放宽成模糊窗口）：
#:    ① **代码必须紧邻**（中间只允许 markdown 记号与空白）⇒ 数字不会安到别的标的头上；
#:    ② 中间的词**只认闭集**（目标设定动词 / 档位词）且**至少要有一个**
#:       ⇒ 「512800当前权重7.90%」这类**当前状态**数字**抽不出**
#:       （宁可抽不出，也**不许**把现值当目标权重）；
#:    ③ **裸「到/至」不收** ⇒ 「512800涨到20%」不会被当成目标；
#:    ④ **只补空**：`_set` 先到先得 ⇒ 严格规则已抽到的值**不被本规则覆盖**。
_TERSE_TARGET_VERBS = (
    r"减至|减到|降至|降到|下调至|下调到|调至|调到|调整至|调整到|"
    r"压缩至|压缩到|压至|压到|定为|设为|定在|定到|控制在|限定在|"
    r"只给|仅给|配置|给"
)
#: 档位词（LLM 自创的仓位档说法：**不是**权重名词，但后面跟的数字就是目标权重）
_TERSE_TARGET_BANDS = r"标准档位|标准档|标准槽位|标准仓位|档位|档|槽位"
#: 🔴 2026-11-13（独立审查 A-1）：**否决/备选语境**词表。
#:    实测：「`515650`调至20%的备选方案被否决。」⇒ 若不设防 ⇒ 会被发一个**目标 20%**
#:    ⇒ 交付件里「LLM 最终裁决目标权重 20%」对一只**被一票否决**的标的是**错的**。
_TERSE_REJECT_MARKERS: tuple[str, ...] = (
    "否决", "放弃", "不采用", "未采纳", "不考虑", "剔除", "排除", "移除",
    "备选", "原方案", "不作为", "不予", "搁置",
)


def _has_reject_marker(text: str) -> bool:
    """这段文字里有没有**否决/备选**语境（粗判，仅作辅助）。"""
    return any(marker in (text or "") for marker in _TERSE_REJECT_MARKERS)


def _reject_bound_codes(sentence: str) -> set[str]:
    """句子里**被否决/备选语境管到**的代码集合。

    🔴 2026-11-13（独立审查 A-1 + 实测修）：**同一个否决词可能管的是别的标的**。
       实测真实原文：「`512800`减至20%、`515450`标准档20%、**否决**`515650`、…」
       ⇒ 「否决」紧跟在 515450 后面，但它**管的是下一只 515650**
       ⇒ 若按「邻近即命中」判 ⇒ **515450 被误伤**（target_pct 抽不出 ⇒ 又回到硬失败）。
       ⇒ 口径：**逐个否决词判定它绑定的是哪只**——
          ① 后面紧跟代码（「否决`515650`」）⇒ **绑后面那只**；
          ② 后面没有代码 ⇒ **绑前面最近那只**（「`515650`调至20%的备选方案被否决」）。
    """
    bound: set[str] = set()
    for marker in _TERSE_REJECT_MARKERS:
        start = 0
        while True:
            pos = sentence.find(marker, start)
            if pos < 0:
                break
            start = pos + len(marker)
            tail = sentence[pos + len(marker): pos + len(marker) + 8]
            m = _CODE_RE.search(tail)
            if m is not None:
                bound.add(m.group(1))
                continue
            head_codes = [mm.group(1) for mm in _CODE_RE.finditer(sentence[:pos])]
            if head_codes:
                bound.add(head_codes[-1])
    return bound


_TERSE_TARGET_RE = re.compile(
    r"(?P<code>(?<![\d.])[1-9]\d{5}(?!\d))"
    r"[\*`）)）:：|\s]{0,4}"
    r"(?:" + _TERSE_TARGET_VERBS + r"|" + _TERSE_TARGET_BANDS + r")"
    r"[\*`）)）:：|\s]{0,4}"
    r"(?:" + _TERSE_TARGET_VERBS + r"|" + _TERSE_TARGET_BANDS + r")?"
    r"[\*`）)）:：|\s]{0,4}"
    r"(?:约|大约|约等于|≈)?\s*"
    r"[\*（(]{0,3}\s*(?P<pct>\d+(?:\.\d+)?)\s*%")

#: 🔴 2026-11-05 09:3x（M-Q 修）：**条件句**里的数字**不是**最终裁决。
#:    实测：「若512800的E转负但mom120仍为正且未有效跌破MA60，将仓位进一步降至约10%」
#:    ⇒ 这句是**未来触发条件**，却被当成主裁决抽成了 10%。
#:    ⇒ 第 1/2 遍都跳过条件句（句首词 + 首个子句内出现条件词都算）。
_CONDITIONAL_WORDS = ("若", "如果", "假如", "倘若", "一旦", "除非", "只有当", "只有")
#: 「条件买入/条件建仓」是**动作**（不是条件句）⇒ 判条件前先把它抹掉
_COND_BUY_SPAN = re.compile(r"有条件买入|条件买入|条件建仓|有条件建仓")

#: 🔴 2026-11-11（真误伤 · G-TA1 闸门实测）：**让步/假设连词**引导的否决动词
#:    **不是本轮裁决**。
#:    实测（研究链 canonical 根 · 2026-11-11 14:54）：LLM 原文
#:      `- 即使否决 \`515450\`，它也不能作为顺延者：两者同属红利族，而顺延必须选择不同主题资产`
#:    ⇒ 「否决 515450」被当成**真否决** ⇒ 规则层真实入选（最终分 0.8672、动量 +6.31%、
#:      站上 MA60、未拥挤）、TA 权威方向表写「买（条件性）」的 **515450 被踢出计划**
#:      ⇒ 顺延位只能给**更弱**的 563020（最终分 −0.4683）⇒ **误伤**。
#:    ⇒ 判定口径：让步词与否决动词之间**必须紧邻**（只允许空白/强调标记 + 可选一个
#:      6 位代码 + 可选被动字）⇒
#:        「即使否决 515450」/「即使 515450 被否决」 ⇒ **假设**（不判否决）；
#:        「即使拥挤度 92% 也不买 515650」          ⇒ 中间夹了内容 ⇒ **真否决**（不豁免）。
#:    🔴 口径**不做成整句豁免**（那会连「即使…也不买 X」的真否决一起放过）。
_HYPOTHETICAL_MARKERS: tuple[str, ...] = (
    "即使", "即便", "就算", "哪怕", "纵使", "假设", "假定",
)
#: 让步词与（可选的）代码/被动字之间允许的「无内容」间隔
_HYPOTHETICAL_GAP_RE = re.compile(r"[\s*`]*(\d{6})?[\s*`]*(?:被|遭|遭到|受到)?")

#: 「保留约 4,100 股」/「减至约 5,100 份」
#: 🔴 2026-11-05 10:3x（M-Q 修）：**「减至 N 份」也是「保留 N 份」**。
#:    实测：新决策写「512800：减至约 5,100 份（卖出约 2,600 份…），权重降至约 14.9%」
#:    ⇒ 旧实现只认「保留/留/持有」⇒ `shares_keep` 抽不出 ⇒ 计划里没法复算份数。
_KEEP_SHARES_RE = re.compile(
    r"(?:保留|留|持有|减至|减到|降至|降为|下调至|下调到)\s*(?:约)?\s*([\d,]+)\s*(?:股|份)")
#: 「卖出约 3,600 股」/「卖出2,600份」
_SELL_SHARES_RE = re.compile(r"(?:卖出|卖掉|减掉)\s*(?:约)?\s*([\d,]+)\s*(?:股|份)")
#: 「硬上限 15%」
_CAP_RE = re.compile(r"硬上限\s*(?:约)?\s*(\d+(?:\.\d+)?)\s*%?")
#: 「下限 0」
_FLOOR_RE = re.compile(r"下限\s*(?:约)?\s*(\d+(?:\.\d+)?)\s*%?")
#: 「价格硬止损 0.800」
_STOP_RE = re.compile(r"止损\s*(?:价)?\s*(\d+(?:\.\d+)?)")
#: 「买 1 手」/「买入一手」/「买两手」
_BUY_LOTS_RE = re.compile(r"买\s*(?:入)?\s*(\d+|[一二两三四五六七八九十])\s*手")
#: 「最多一手」/「至多 1 手」/「不超过一手」—— **上界**语义（≤ N 手）
_MAX_LOTS_RE = re.compile(
    r"(?:最多|至多|不超过|不超)\s*(\d+|[一二两三四五六七八九十])\s*手")
#: 「511880 ≈10,000 元（33.5%）」—— 调仓后权重/金额
_POST_AMOUNT_RE = re.compile(r"≈\s*([\d,]+(?:\.\d+)?)\s*元\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）")
#: 「511880 约占 33.5%」—— 调仓后**权重**（代码写在前面 ⇒ 直接归属，不靠就近猜）
_POST_PCT_RE = re.compile(r"(?P<code>\d{6})\s*(?:调仓后)?约占\s*(?P<pct>\d+(?:\.\d+)?)\s*%")
#: 「现金 ≈16,271 元（54.5%）」—— 现金（代码位是「现金」两个字，不是 6 位数字）
_POST_CASH_RE = re.compile(r"现金\s*≈\s*([\d,]+(?:\.\d+)?)\s*元\s*（\s*(\d+(?:\.\d+)?)\s*%\s*）")
#: 「现金：约 25,397 元、占比约 85%」（**金额在前、占比在后**）
_POST_CASH_COLON_RE = re.compile(
    r"现金\s*[:：]?\s*(?:约|≈)?\s*([\d,]+(?:\.\d+)?)\s*元"
    r"[^，。；]{0,8}?(?:占比|占)?\s*(?:约)?\s*(\d+(?:\.\d+)?)\s*%")
#: 「现金升至约 85%（约 25,397 元）」（**占比在前、金额在后**）
_POST_CASH_PCT_RE = re.compile(
    r"现金[^，。；]{0,10}?约\s*(\d+(?:\.\d+)?)\s*%"
    r"[^，。；]{0,4}?[（(]\s*(?:约)?\s*([\d,]+(?:\.\d+)?)\s*元")

#: 否定词（出现在动作词**前面** ⇒ 该动作被否定，不算数）
#:
#: 🔴 2026-11-05 10:3x（M-Q 修）：实测「**这不是清零** 512800」
#:    ⇒ 旧/新规则都会把「清零」当成清仓 ⇒ 512800 方向变成「卖/0%」⇒ 真实裁决
#:    （减仓至约 15%）被覆盖 ⇒ **必须做否定式保护**。
_NEG_MARKERS = ("不是", "非", "不", "无需", "避免", "杜绝", "不予", "排除", "拒绝", "免于")

#: 🔴 否定**作用域**的断点词：出现这些词 ⇒ 之前的否定**管不到**后面的动作词。
#:   「不卖却要清仓」⇒ 否定管不到「清仓」；「不是清仓而是减仓」⇒ 管得到「清仓」、管不到「减仓」。
_SCOPE_BREAKERS = ("但", "不过", "然而", "却", "反而", "而是", "改为", "转而")

#: md 的强调/代码标记（LLM 常写「卖出约 **2,600份**」⇒ 动词与数量之间夹着 `**`）。
#:    动作词与数量之间允许夹这些标记 ⇒ 否则「部分卖出」会被漏判成「清仓」。
_MD_NOISE = r"[\s*`_~]*"


def _negated(text: str, pos: int, matched: str = "") -> bool:
    """`pos` 处的动作词是否被否定（「不清零」「不是清零」「避免清仓」…）。

    🔴 2026-11-10 实测事故（通宵实跑 A20⑥ 红灯）：LLM 写
       「采纳本轮**战术性卖出、降低权益敞口**，但不把 `512800` 一次清仓」
       ⇒ 旧口径只看动作词**前 4 个字**（这里是「一次」）⇒ 否定词「不」落在窗口外
       ⇒ 「清仓」被当成**清仓指令** ⇒ 512800 方向变「卖」、目标权重被写成 0%
       ⇒ 真实裁决（减至约 15%）被整条覆盖。
       ⇒ 改成**子句级否定作用域**：
          ① 动作词**之前**必须有否定标记（取**最近**的那个）；没有 ⇒ 不算被否定；
          ② 否定标记与动作词之间若有**转折/并列词**（但/不过/却/而是…）⇒ 否定**管不到**；
          ③ 否定标记与动作词之间若出现**别的代码**、且子句里还有**否定标记之前**的代码
             ⇒ 否定只绑定它后面那个标的 ⇒ 管不到本动作词；
          ④ 动作词**自带否定**（「不买」「不建仓」）⇒ 它本身就是零方向指令，不算被否定。
    """
    if matched and any(matched.startswith(m) for m in ("不", "非")):
        return False
    scope = text[:pos]
    last = -1
    for marker in _NEG_MARKERS:
        idx = scope.rfind(marker)
        if idx > last:
            last = idx
    if last < 0:
        return False
    if any(b in scope[last:] for b in _SCOPE_BREAKERS):
        return False
    codes = list(_CODE_RE.finditer(scope))
    if any(c.start() > last for c in codes) and any(c.start() < last for c in codes):
        return False
    return True


#: 子句里的动作词 → (方向, 目标权重, 说明)
#:
#: 🔴 2026-11-11（真机跑批 · A20⑥ 红灯）**句级「部分卖出」标记**。
#:    实测事故（真实交付件 `20261111_122825` 的最终交易决定段）：
#:       「最终决策是：**卖出（Sell）512800，但属于减仓而非清仓式看空。**」
#:    ⇒ 子句按「，」切 ⇒ 裸「卖出」规则（第 ④ 档）**先命中**
#:    ⇒ 512800 方向='卖'、目标权重=**0%**（＝清仓）
#:    ⇒ 而同一份交付件的动作表写「卖出 3,600 份、保留约 12% 核心仓」⇒ **交付件自相矛盾**
#:    ⇒ A20⑥ 判红（原文写的是**部分卖出** ⇒ 计划方向必须是「减」，不是「卖」）。
#:    ⇒ 修法：**裸「卖出」只是默认推断**（默认整仓卖出）；
#:      同一句里若该标的被**明确**说成「减仓 / 部分卖出」⇒ 裸卖出**不生效**
#:      ⇒ 交给更明确的表述裁决（第 ② 档「卖出约 N 份」/ 第 ③ 档「明确减仓」）。
_PARTIAL_SELL_MARKERS: tuple[str, ...] = ("减仓", "减持", "减掉")

#: 「减仓」标记后**多远**以内出现代码 ⇒ 认为该标记**点名**那只标的。
_PARTIAL_SELL_NAMED_TAIL = 12


def _sentence_partial_sell_codes(sentence: str) -> set[str]:
    """句子里被**明确**说成「减仓 / 部分卖出」的代码集合。

    🔴 口径（宁保守勿误伤）：
      · 标记后 ≤ `_PARTIAL_SELL_NAMED_TAIL` 字内出现代码 ⇒ 该标记**点名**那只标的；
      · 标记后**没有**任何代码 ⇒ 该标记是**泛指**表述 ⇒ 句内**所有**代码都算「减仓」；
      · 标记后**有**代码但**不是这只** ⇒ 该标记**不**作用于它。
    """
    out: set[str] = set()
    codes = [m.group(1) for m in _CODE_RE.finditer(sentence)]
    for marker in _PARTIAL_SELL_MARKERS:
        for mm in re.finditer(re.escape(marker), sentence):
            named = _CODE_RE.search(sentence[mm.end():mm.end() + _PARTIAL_SELL_NAMED_TAIL])
            if named:
                out.add(named.group(1))
            else:
                out.update(codes)
    return out


#: 🔴 2026-11-05 10:3x（M-Q 修）：**动作规则必须分层、且顺序敏感**。
#:    实测事故：旧规则 `全部卖出|卖出` 把
#:    「512800：减至约 5,100 份（**卖出约 2,600 份**…），权重降至约 14.9%」
#:    里的**部分卖出**当成了**清仓** ⇒ 512800 目标权重写成 0%（真实裁决 ≈14.9%）
#:    ⇒ 交付件的「最终裁决」完全错位。
#:    现在的分层（**先匹配先生效**）：
#:      ① 清仓类（全部卖出 / 卖出全部 / 清仓 / 清掉 / 清零）⇒ 卖 + 0%
#:      ② **带数量的**卖出 ⇒ **减**（部分卖出，**不是**清仓）
#:      ③ 明确减仓（减至/减仓/减持…）⇒ 减
#:      ④ 裸「卖出」⇒ 卖 + 0%
_ACTION_RULES: tuple[tuple[re.Pattern, str, float, str], ...] = (
    # ① 清仓类（**先判**）：「全部卖出 2,500 份」= 整仓清空 ⇒ 目标权重 0%
    #    「不清零 / 不是清零」由 `_negated()` 排除。
    (re.compile(r"(?:全部卖出|卖出全部|清仓|清掉|清零)"), "卖", 0.0, "原文写明清仓"),
    # ② 带数量的卖出 ⇒ **部分卖出**（减仓），目标权重留给「权重/目标」类规则
    #    🔴 2026-11-09 实测事故（通宵实跑 A20⑥ 红灯）：LLM 写
    #       「512800减持约2,600份，保留约5,100份」⇒ 旧口径**不认「减持/减仓 + 数量」**
    #       ⇒ 走不到本规则 ⇒ 方向被后面的裸「卖出」规则抢走 ⇒ 512800 目标写成 0%。
    #    🔴 2026-11-10 实测事故：LLM 写「卖出约 **2,600份**」⇒ 动词与数字之间夹着
    #       markdown 强调标记 `**` ⇒ 旧正则 `\s*` 吃不掉 ⇒ 本规则又漏判
    #       ⇒ 方向被裸「卖出」规则抢走 ⇒ 「减仓」又被写成「清仓/0%」。
    #       ⇒ 修法：动词与数量之间允许夹 **强调/代码标记**（`_MD_NOISE`）。
    (re.compile(r"(?:卖出|卖掉|减掉|减持|减仓)" + _MD_NOISE + r"(?:约)?" + _MD_NOISE
                + r"[\d,]+(?:\.\d+)?" + _MD_NOISE + r"(?:股|份)"),
     "减", None, "原文写明卖出/减持 N 份（部分卖出，非清仓）"),
    # ③ 明确减仓
    (re.compile(r"减至|减到|减仓|减持|压缩至|压到|下调至|下调到"), "减", None, "原文写明减仓"),
    # ④ 裸「卖出」⇒ 整仓卖出（0%）
    #    🔴 2026-11-09 实测事故：LLM 写「卖出512800**预计回笼**约2,272.40元」
    #       ⇒ 这是**收益预估句**（描述减仓回笼多少现金），**不是卖出指令**
    #       ⇒ 旧口径裸「卖出」命中 ⇒ 512800 被当成「卖 + 目标 0%」（真实裁决「减至约15%」）。
    #       ⇒ 修法：若「卖出」后面同一子句里出现**回笼/回款/折现/变现**这类**收益预估词**
    #          ⇒ 本规则**不生效**（宁可留 None，也不把预估句当指令）。
    #       ⚠️ 「全部卖出/清仓」由规则①先判，不受此护栏影响（真清仓仍判 0%）。
    (re.compile(r"(?:卖出|卖掉)(?![^。；\n]{0,40}?(?:回笼|回款|折现|变现))"),
     "卖", 0.0, "原文写明卖出"),
    (re.compile(r"不建仓"), "不建仓", 0.0, "原文写明不建仓"),
    (re.compile(r"不买"), "不买", 0.0, "原文写明不买"),
    # 🔴 2026-11-05 09:3x（M-Q 修）：「条件买入」是**带条件的建仓**（不是不买，也不是
    #    无条件买）⇒ 单独一档；**没有目标权重**（目标权重要靠「调仓后权重」读出）。
    #    实测漏抽后果：511880 整条裁决掉进 `unparsed_codes` ⇒ 计划里显示 0%/不建仓
    #    ⇒ 漏掉一个 ≈33.5% 的条件仓位。
    (re.compile(r"条件买入|有条件买入|条件建仓"), "条件买入", None, "原文写明条件买入"),
    (re.compile(r"买\s*(?:入)?\s*(?:\d+|[一二两三四五六七八九十])\s*手"), "买", None,
     "原文写明买 N 手"),
    # 🔴 「减仓」只给**方向**，**不给目标权重**：「减仓不清仓」不是清零 ⇒ 目标权重
    #    必须由「目标权重降至 X%」那条规则单独抽 ⇒ 抽不出就留 None（不猜）。
    (re.compile(r"减仓"), "减", None, "原文写明减仓"),
)

#: 🔴 2026-11-05 10:4x（M-Q 修）：**主体范围词**。
#:    实测事故：新决策写「…不支持把**银行主题权重**扩到 20%」
#:    ⇒ 「权重扩到 20%」离最近的代码 512820 只有十几个字 ⇒ 最近码规则把 **20%**
#:    记到 512820 头上 ⇒ 一个**根本没被选中**的银行 ETF 拿到了 20% 目标权重。
#:    ⇒ 若「代码」与「权重名词」之间出现**范围词**（主题/组合/全仓/…）⇒
#:    这个数是**范围级**的 ⇒ **不许归给任何单只代码**。
_SCOPE_MARKERS = ("主题", "组合", "全仓", "整体", "板块", "行业", "赛道",
                   "全市场", "层面", "总仓位", "总敞口", "全账户")


def _is_scope_level(sentence: str, code_pos: int, match_pos: int) -> bool:
    """`match_pos` 处的权重数字是否**范围级**（主题/组合级）而非某只代码的。

    判据：代码与数字之间出现**范围词** ⇒ 数字的主语不是那只代码。
    """
    gap = sentence[code_pos:match_pos]
    return any(marker in gap for marker in _SCOPE_MARKERS)


#: 证据句最长保留字数
_EVIDENCE_MAX = 240


def _norm_pct(value: float) -> float:
    """百分比归一到「一位小数」，避免 12 与 12.0 两种写法在比较时不等。"""
    return round(float(value), 1)


def _to_int(text: str) -> int:
    """把「4,100」这类带千分位的数字串转成整数。"""
    return int(str(text).replace(",", "").replace("，", ""))


#: 中文数字 → 阿拉伯数字（只覆盖「手数」会用到的一~十）
_CN_NUM: dict[str, int] = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _to_lots(text: str) -> int:
    """「1」/「一」/「两」⇒ 手数。"""
    raw = str(text).strip().replace(",", "")
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw, 1)


#: 🔴 2026-11-13（真缺陷 · **真否决被放过**）：「**不满足**持有**条件**即剔除512800」
#:    ⇒ 那句里的「条件」是「不满足」的**宾语**（条件**没达标**＝既成事实），
#:    **不是**条件句的**触发器** ⇒ 旧口径把整句当条件句 ⇒ **豁免否决判定**
#:    ⇒ **真否决**（剔除）被放过 ⇒ 交付层让本该出局的标的继续留在组合里。
#:    ⚠️ 词表口径：只认「**不满足/未满足/未达到/不符合/达不到/不达标/不成立** …」
#:       这类**明确的「未达标」**表述；「不满足…的**必要性**」**不在**此列
#:       （那是**否定剔除本身**，由 `_REJECT_NEG_NECESSITY_RE` 单独处理）。
_COND_FAIL_RE = re.compile(
    r"(?:不满足|未满足|未达到|不符合|达不到|不达标|不成立)"
    r"[^，,；;：:\n]{0,12}?"
    r"(?:条件|标准|门槛|要求|阈值)"
)

#: 「**不满足/未达到/…**」**之前**出现这些词 ⇒ 这是**前瞻条件**（不是既成事实）
#:    ⇒ 「512800**若**不满足持有条件，则剔除512800。」**不许**判成否决。
_COND_FAIL_HYPOTHETICAL_WORDS: tuple[str, ...] = (
    "若", "如果", "假如", "倘若", "一旦", "除非", "只有当", "只有在",
)

#: 「**条件没达标 ⇒ 直接出局**」的**剔除类**动词（**只有**这些才允许改判）。
_REJECT_DEF_VERBS: tuple[str, ...] = (
    "剔除", "排除", "否决", "移除", "回避", "踢出", "去掉",
)


def _is_definitive_rejection(sentence: str) -> bool:
    """这句是不是「**条件没达标 ⇒ 直接剔除**」的**既成否决**（⇒ 不按条件句豁免）。

    🔴 2026-11-13：`_is_conditional` 见到首子句里的「条件」就判条件句
       ⇒ 「512800不满足持有条件即剔除512800。」被**整句豁免** ⇒ 真否决被放过。

    ⚠️ 三重护栏（避免把**真条件句**误改判）：
       ① 必须真的出现「**未达标**」的表述（`_COND_FAIL_RE`）；
       ② 「条件」后面若跟「**时/则/便/将**」⇒ 那是**时间条件**
          （「不满足条件**时**目标权重降至10%」）⇒ 仍按条件句处理；
       ③ 必须带**剔除类**动词（否则只是「条件没达标」，不是否决）。
    """
    text = sentence or ""
    m = _COND_FAIL_RE.search(text)
    if not m:
        return False
    # ② 失败短语**之前**有假设连词 ⇒ 这是**前瞻条件**，不是既成事实
    #    （「512800**若**不满足持有条件，则剔除512800。」⇒ 不许判成否决）
    if any(w in text[:m.start()] for w in _COND_FAIL_HYPOTHETICAL_WORDS):
        return False
    # ③ 失败短语**之后**（跳过分隔符）的第一个词是「时/则/便/将/就」
    #    ⇒ **时间/结果条件** ⇒ 仍按条件句处理
    _tail = re.sub(r"^[，,、；;。\s]+", "", text[m.end():])
    if re.match(r"(?:则|便|将|就|时)", _tail):
        return False
    # ④ 剔除动词必须与失败短语**同一子句**（否则主语可能串味）
    _clause = re.split(r"[，,；;。\n]", text)[0]
    return any(v in _clause for v in _REJECT_DEF_VERBS)


def _is_conditional(sentence: str) -> bool:
    """判断一个句子是不是**条件句**（条件句里的数字**不是**最终裁决）。

    🔴 2026-11-05 09:3x（M-Q 修）实测事故：
       「若512800的E转负但mom120仍为正且未有效跌破MA60，将仓位进一步降至约10%」
       被当成主裁决 ⇒ 512800 目标写成 10%（真实裁决 15%）。

    判定口径（两条都算条件句）：
      ① 去掉 markdown/列表/序号前缀后，**句首**是条件词；
      ② **首个逗号之前**出现了条件词（覆盖「512800若因上涨使权重超过15%」这种
         代码打头的写法）。
    """
    head = str(sentence or "").strip()
    head = re.sub(r"^[\-\*\+>#\s]*", "", head)          # 列表/引用前缀
    head = re.sub(r"^\d+[\.、)\]]\s*", "", head)        # 「1. 」序号
    for word in _CONDITIONAL_WORDS:
        if head.startswith(word):
            return True
    # 🔴 2026-11-13：句首是「**若/如果/…**」的**假设**已在上面返回（优先级最高）。
    #    下面只处理「**既成事实**」的否决（见 `_is_definitive_rejection`）。
    if _is_definitive_rejection(head):
        return False
    # 🔴 2026-11-05 10:3x（M-Q 修）：「降级条件：…⇒ 残仓一次性清零」是**触发器**，
    #    不是本轮裁决 ⇒ 但「条件买入/条件建仓」是**动作** ⇒ 判条件前先抹掉它。
    probe = _COND_BUY_SPAN.sub("", head)
    # 🔴 2026-11-09：「已触发 MA60 强制退出」是**既成事实**，不是条件 ⇒ 先抹掉
    probe = _FACT_TRIGGER_RE.sub("", probe)
    first_clause = re.split(r"[，,；;：:]", probe)[0]
    if any(word in first_clause for word in ("若", "如果", "假如", "倘若", "一旦", "除非",
                                            "条件", "触发")):
        return True
    return False


#: 🔴 2026-11-10：文档级章节的**编号模式**（中文序数 / 阿拉伯数字 ＋ 顿号）。
#:   例：「六、最终交易决定（原始输出）」「七、附录」「3. 复盘」⇒ 命中。
#:   例：「Executive Summary」「Investment Thesis」⇒ **不命中**（LLM 自己写的内嵌小标题）。
_SECTION_NUM_RE = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百千]+|[一二三四五六七八九十百千]+|\d+)\s*[、.．]"
)


def extract_section(text: str) -> str | None:
    """截出「最终交易决定」章节的正文；找不到返回 None。

    🔴 章节层级用 `##` 的井号数判定 ⇒ 正文里出现更高级标题（`#`）即结束；
    同级或更低级标题（`##`/`###`）也结束 ⇒ 不会把后面的章节吃进来。

    🔴 2026-11-10（真实缺陷，服务器实跑暴露）：**LLM 会在最终裁决章节里自己写同级小标题**。
       实测（2026-11-10 10:36 那轮 LLM 原文）：

           ## 六、最终交易决定（原始输出）
           **Rating**: Underweight
           ## Executive Summary          ← 🔴 LLM 自己写的 **H2**
           - `512800`：… 目标权重定在 **15%** …
           ## Investment Thesis          ← 🔴 又一个 **H2**

       ⇒ 旧口径「遇到同级标题就截断」⇒ 章节正文只剩「**Rating**: Underweight」一行
       ⇒ **最终裁决 0 个代码** ⇒ 冲突表为空 ⇒ `divergences` 为空
       ⇒ `make_decision_scenarios.py` 直接抛 RuntimeError ⇒ **基线重建整轮失败**。
    ⇒ 现在：**编号章节**（如「六、…」）的**同级且无编号**标题一律视为 **LLM 内嵌小标题**
       ⇒ 继续往下吃；只有「编号同级标题」或「更高级标题（`#`）」才是下一节。
       起始标题**本身无编号**时 ⇒ **保持旧口径**（同级标题即结束）⇒ 旧夹具行为零变化。
    """
    if not text:
        return None
    lines = text.splitlines()
    start: int | None = None
    level = 0
    numbered_start = False
    for idx, line in enumerate(lines):
        if not line.startswith("#"):
            continue
        body = line.lstrip("#").strip().lower()
        if any(key in body for key in _FINAL_HEADINGS):
            start = idx
            level = len(line) - len(line.lstrip("#"))
            numbered_start = bool(_SECTION_NUM_RE.match(line.lstrip("#").strip()))
            break
    if start is None:
        return None
    body_lines: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            lvl = len(line) - len(line.lstrip("#"))
            if lvl < level:
                break
            if lvl == level:
                # 🔴 编号章节里 LLM 自写的**同级无编号**小标题 ⇒ 属章节内部 ⇒ 继续吃
                if not (numbered_start
                        and not _SECTION_NUM_RE.match(line.lstrip("#").strip())):
                    break
        body_lines.append(line)
    return "\n".join(body_lines)


def _sentence_offsets(text: str) -> list[tuple[int, int, str]]:
    """按「。；」与换行切句 ⇒ 返回 (起点, 终点, 句子) 列表。"""
    out: list[tuple[int, int, str]] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch in "。；" or ch == "\n":
            seg = text[start:idx]
            if seg.strip():
                out.append((start, idx, seg))
            start = idx + 1
    tail = text[start:]
    if tail.strip():
        out.append((start, len(text), tail))
    return out


def _clause_spans(sentence: str, base: int) -> list[tuple[int, int, str]]:
    """按「，」切子句 ⇒ 返回 (绝对起点, 绝对终点, 子句) 列表。"""
    out: list[tuple[int, int, str]] = []
    start = 0
    for idx, ch in enumerate(sentence):
        if ch == "，":
            out.append((base + start, base + idx, sentence[start:idx]))
            start = idx + 1
    out.append((base + start, base + len(sentence), sentence[start:]))
    return [sp for sp in out if sp[2].strip()]


#: ── 结构化决策表（Trader 把整套方案写成 markdown 表）────────────────────────
#:
#: 🔴 2026-11-13（真实交付件 `snapshots/20261113_092926` 实证）：Trader 会把最终方案
#:    写成**表**「标的 | 方向 | 目标 | 执行理由」：
#:
#:        | **512800** | **减** | **16%目标，20%不再是执行目标** | 保留相对最强仓… |
#:        | **515450** | **买（条件性）** | **最高16%** | 仅在复核后仍入选… |
#:        | **515650** | **不买，正式一票否决** | **0%** | E=-0.8598… |
#:
#:    而「最终交易决定」章节是**散文**（数字写在不带代码的句子里：「减至 12% 基础目标」
#:    「最高 8%」）⇒ 旧口径（散文 + 权重名词 + 动词）**一个字都抽不出**
#:    ⇒ `target_pct=None` ⇒ `make_decision_scenarios` 硬失败（「不许用默认值蒙混」）
#:    ⇒ 重建链转红（2026-11-13 09:29 实测：`targets[512800]缺少必需字段 target_pct`）。
#:
#: 🔴 **口径纪律**：
#:    ① **只认权威区**（「最终交易决定」章节 ＋「Trader 交易计划」章节）⇒ 多头/空头的
#:       观点表**没有**裁决权（与既有 `AUTHORITY_SECTIONS` 同源）；
#:    ② **只认目标列**（表头含「目标/权重/仓位/敞口」的那一列）⇒ **理由列里的数字一律不看**
#:       （理由列写着「将 22.54% 的当前超配降回标准槽位」⇒ 那是**现值**，不是目标）；
#:    ③ **只认该列的第一个有效数字%**，并剔除被「不再是/硬上限/至少…」修饰的数
#:       （「16%目标，20%不再是执行目标」⇒ 16%，不是 20%）；
#:    ④ **只补空**（`_set` 先到先得）⇒ 散文抽得到的**一律以散文为准**（口径不被污染）。
_TABLE_TARGET_HEADER_WORDS: tuple[str, ...] = (
    "目标", "权重", "仓位", "敞口", "风险目标",
)
#: 🔴 2026-11-13（独立审查 + 变异探针实证）：**现值列**不许被当成目标列。
#:    旧口径是「表头含『目标/权重/仓位/敞口』就算目标列」⇒「**当前权重**」含「权重」
#:    ⇒ 取到**现值** 22.54% 当目标权重（静默产出错误数字，且来源标签写着「目标列」
#:    ⇒ 回放时完全看不出错）。
_TABLE_CURRENT_HEADER_WORDS: tuple[str, ...] = (
    "当前", "现有", "原始", "原持仓", "原仓位", "现值", "调整前", "原值",
)
#: 🔴 2026-11-13（独立审查 + 变异探针实证）：「**从 20% 降至 10%**」/「**由 22.54% 降至 10%**」
#:    ⇒ 目标是**转变之后**那个数（10%）；前面的 20%/22.54% 是**现值**。
#:    ⚠️ 旧口径把「由/从」放进 `SKIP_BEFORE` ⇒ 两个数**都被**跳过 ⇒ 整格 `None`
#:    ⇒ `target_pct` 缺失 ⇒ `make_decision_scenarios` 硬失败（与本次要修的缺陷同类）。
_TABLE_TRANSITION_RE = re.compile(
    r"(?:降至|减至|降到|调至|下调至|下调到|压缩至|压缩到|调整至|调整到)"
)
#: 目标格里「数字**前面/后面**」只看**紧邻**的那一段（到最近的标点/括号为止）
#:    ⇒ 修旧「8 字滑窗」把**别的分句**的排除词也算了进来（假绿：抽不出就静默 None）。
_TABLE_DELIM_RE = re.compile(r"[，,；;：:、（）()【】\[\]「」『』|｜]")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
_TABLE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
#: 目标格里**不是目标**的数字（出现在数字**前面** ⇒ 跳过）
_TABLE_PCT_SKIP_BEFORE: tuple[str, ...] = (
    "不再是", "不是", "并非", "硬上限", "上限", "至少", "不超过", "不低于",
    "高于", "低于", "现值", "当前",
)
#: 目标格里**不是目标**的数字（出现在数字**后面** ⇒ 跳过）
_TABLE_PCT_SKIP_AFTER: tuple[str, ...] = (
    "不再是", "不是", "并非", "已失效", "作废", "不再执行", "硬上限",
)


def _table_target_pct(cell: str) -> float | None:
    """目标列 ⇒ 目标权重（**抽不出就返回 None**，绝不编造）。

    🔴 2026-11-13（三处口径修正，均由独立审查 + 变异探针实证）：
       ① **转变标记优先**：「从 20% 降至 10%」⇒ **10%**（转变之后那个数）。
       ② **排除词只看紧邻段**（旧口径用 8 字滑窗 ⇒「现值 22.54% ⇒ 目标 16%」里
          16% 的窗口也含「现值」⇒ 整格 `None` ⇒ **静默抽不出**）。
       ③ **前/后排除词都成对**：「硬上限12%，执行目标16%」⇒ 16%（12 被「硬上限」否掉）。
    """
    text = re.sub(r"[*`\s]", "", cell or "")
    if not text:
        return None
    tm = _TABLE_TRANSITION_RE.search(text)
    if tm:
        nxt = _TABLE_PCT_RE.search(text[tm.end():])
        if nxt:
            return _norm_pct(float(nxt.group(1)))
    for m in _TABLE_PCT_RE.finditer(text):
        before = text[max(0, m.start() - 10):m.start()]
        after = text[m.end():m.end() + 10]
        if _table_skip_before(before) or _table_skip_after(after):
            continue
        return _norm_pct(float(m.group(1)))
    return None


def _table_skip_before(before: str) -> bool:
    """目标格里这个数**前面紧邻**的那段是不是「不是目标的数」（⇒ 跳过这个数）。"""
    seg = _TABLE_DELIM_RE.split(before)[-1]
    return any(w in seg for w in _TABLE_PCT_SKIP_BEFORE)


def _table_skip_after(after: str) -> bool:
    """目标格里这个数**后面紧邻**的那段是不是「不是目标的数」（⇒ 跳过这个数）。"""
    seg = _TABLE_DELIM_RE.split(after)[0]
    return any(w in seg for w in _TABLE_PCT_SKIP_AFTER)


def _table_direction(cell: str) -> str | None:
    """方向列 ⇒ 方向（口径与既有 `_ACTION_RULES` 的方向词同源）。

    🔴 2026-11-13（独立审查 + 变异探针实证）：**否定护栏缺失** ⇒
       「不清仓 / 不卖出 / 不减」被当成「卖 / 减」⇒ 方向落进 `direction_vocab.ZERO`
       而目标权重 > 0 ⇒ **违反 ZERO 不变量**（守卫 `LLM_DIRECTION_ZERO` 会拦）。
       ⇒ 现在：**每个动作词都过 `_negated`**；全被否掉 ⇒ 抽不出方向（返回 None，**不编造**）。
    """
    text = re.sub(r"[*`\s]", "", cell or "")
    if not text:
        return None

    def _hit(words: tuple[str, ...]) -> str | None:
        """这些词里第一个**未被否定**的（⇒ 动作成立）。"""
        for word in words:
            for m in re.finditer(re.escape(word), text):
                if not _negated(text, m.start(), m.group(0)):
                    return word
        return None

    if _hit(("否决", "不买", "不建仓", "不配置", "不参与", "不纳入")):
        return "不买"
    if _hit(("减",)):
        return "减"
    if _hit(("清仓", "清掉", "卖出", "卖掉", "退出", "卖")):
        return "卖"
    if _hit(("买入", "买", "建仓", "配置")):
        return "条件买入" if "条件" in text else "买"
    if _hit(("持有", "维持", "观察", "持")):
        return "持"
    return None


#: 🔴 2026-11-13（真实交付件 `snapshots/20261113_092926` 实证）：**裁决区**的章节。
#:    Trader 把**结构化决策表**（「标的 | 方向 | 目标 | 理由」）写在
#:    「研究经理结论」/「Trader 交易计划」章里，而「最终交易决定」章是**散文**
#:    ⇒ 只读散文 ⇒ 表里的目标权重**抽不出来**（`target_pct=None`）
#:    ⇒ `make_decision_scenarios` 硬失败 ⇒ 重建链转红。
#:    ⚠️ **只认这三个章** ⇒ 多头/空头/风险三方的**观点表**没有裁决权
#:       （与既有 `AUTHORITY_SECTIONS` 同源）；「辩论裁决 / 风险裁决」里的表**不采信**
#:       （那是陪审团意见，不是 Trader 的执行目标）。
_DECISION_REGION_KEYWORDS: tuple[str, ...] = (
    "研究经理结论", "研究经理", "Trader 交易计划", "Trader交易计划",
    "交易计划", "最终交易决定", "最终交易决策",
)


def _heading_has_keyword(line: str, keywords: tuple[str, ...]) -> bool:
    """标题行里是否**命中**裁决区关键词（🔴 必须「**词边界**」命中）。

    🔴 2026-11-13（独立审查 NIT）：旧口径是裸 `k in line` ⇒「## 四、风险官意见与**交易计划**调整」
    「## 五、**交易计划**（初稿）」这类**非裁决章**也被扫进裁决区 ⇒ 观点表可能被当成裁决表。
    ⇒ 现在：关键词后面必须**到标题末尾**或紧跟**非文字**字符（括号/书名号/空格…）
    ⇒「研究经理结论（Research Manager）」「Trader 交易计划」「最终交易决定（原始输出）」都命中；
      而「交易计划调整」（关键词后紧跟汉字）**不**命中。
    """
    title = line.lstrip("#").strip()
    for k in keywords:
        pos = title.find(k)
        while pos >= 0:
            nxt = title[pos + len(k):pos + len(k) + 1]
            if not nxt or not re.match(r"[一-鿿A-Za-z0-9]", nxt):
                return True
            pos = title.find(k, pos + 1)
    return False


def _chapters_by_keyword(text: str, keywords: tuple[str, ...]) -> list[str]:
    """按标题关键词取**所有**匹配章节（含子章节）⇒ 返回章节列表。"""
    lines = (text or "").splitlines()
    heads: list[tuple[int, int]] = []   # (行号, 井号层级)
    for idx, line in enumerate(lines):
        if line.startswith("#") and _heading_has_keyword(line, keywords):
            heads.append((idx, len(line) - len(line.lstrip("#"))))
    out: list[str] = []
    for start, level in heads:
        chunk = [lines[start]]
        for line in lines[start + 1:]:
            if line.startswith("#"):
                lv = len(line) - len(line.lstrip("#"))
                if lv <= level:
                    break
            chunk.append(line)
        out.append("\n".join(chunk))
    return out


def _decision_region(text: str) -> str:
    """**最终裁决区** = 研究经理结论 / Trader 交易计划 / 最终交易决定 三章（若有）。

    🔴 为什么要在这些章里找**结构化决策表**：
       Trader 常把最终方案写成表，而「最终交易决定」章是散文
       ⇒ 只读散文 ⇒ 表里的目标权重**抽不出来**（2026-11-13 实测：`targets[512800]`
       缺少 `target_pct` ⇒ `make_decision_scenarios` 硬失败 ⇒ 重建链转红）。
    ⇒ 三章都是**权威裁决**（与 `AUTHORITY_SECTIONS` 同源）⇒ 合起来才是裁决区。
    """
    parts: list[str] = []
    sec = extract_section(text)
    if sec:
        parts.append(sec)
    for chapter in _chapters_by_keyword(text, _DECISION_REGION_KEYWORDS):
        parts.append(chapter)
    return "\n\n".join(parts)


def _authority_target_rows(region: str) -> list[dict]:
    """裁决区里的「标的 | 方向 | 目标 | 理由」表 ⇒ 逐行 [{cells, target_idx, line}]。"""
    rows: list[dict] = []
    seen_lines: set[str] = set()
    lines = (region or "").splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not line.strip().startswith("|"):
            idx += 1
            continue
        header = _table_cells(line)
        sep_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        sep_cells = _table_cells(sep_line) if sep_line.strip().startswith("|") else []
        is_sep = bool(sep_cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in sep_cells)
        if not (is_sep and len(header) >= 3):
            idx += 1
            continue
        target_idx = _table_target_col(header)
        if target_idx is None:
            idx += 1
            continue
        dir_idx = _table_direction_col(header)
        j = idx + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = _table_cells(lines[j])
            if cells:
                _line = " ".join(lines[j].split())
                # 🔴 2026-11-13（独立审查 NIT）：裁决区里最终章被扫了**两遍**
                #    （`extract_section` + `_chapters_by_keyword`）⇒ 同一行会重复出现
                #    ⇒ 按**行文本**去重（结果不变，但不白跑一趟）。
                if _line not in seen_lines:
                    seen_lines.add(_line)
                    rows.append({
                        "cells": cells,
                        "target_idx": target_idx,
                        "dir_idx": dir_idx,
                        "line": _line,
                    })
            j += 1
        idx = j
    return rows


def _table_target_col(header: list[str]) -> int | None:
    """表头 ⇒ **目标列**的列号（抽不出返回 None ⇒ 整张表不采信）。

    🔴 2026-11-13（独立审查 + 变异探针实证）：旧口径是「`range(2, len)` 里取**第一个**含
       「目标/权重/仓位/敞口」的列」⇒ 两个坑：
       ① **现值列被当成目标列**：「当前权重」含「权重」⇒ 取到**现值** 22.54%
          （静默产出错误目标权重，且来源标签写着「目标列」⇒ 回放时看不出错）；
       ② **目标列在 index1** 的常见版式「标的 | 目标权重 | 方向 | 理由」被**整张丢弃**
          ⇒ 又回到 `target_pct=None` 的硬失败。
    ⇒ 现在：① 先**排除现值列**（当前/现有/原始/调整前/现值…）；
       ② **全列**找候选（不再跳过 index1）；
       ③ 多个候选时按「目标/风险目标 > 权重/仓位/敞口」取**最具体**的那个。
    """
    cands: list[int] = []
    for i, h in enumerate(header):
        if any(w in h for w in _TABLE_CURRENT_HEADER_WORDS):
            continue
        if any(w in h for w in _TABLE_TARGET_HEADER_WORDS):
            cands.append(i)
    if not cands:
        return None
    for i in cands:
        if "目标" in header[i]:
            return i
    return cands[0]


def _table_direction_col(header: list[str]) -> int | None:
    """表头 ⇒ **方向列**的列号（没有就 None ⇒ **不猜**）。"""
    for i, h in enumerate(header):
        low = h.lower()
        if any(w in low for w in ("方向", "动作", "action")):
            return i
    return None


def extract_final_decision(text: str) -> dict:
    """把「最终交易决定」章节的散文抽成结构化裁决。

    返回：
        {
          "available": bool,          # 是否找到章节
          "source_section": str|None, # 章节标题原文
          "rating": str|None,         # 最终评级
          "summary": str,             # Executive Summary（截断到 1200 字）
          "targets": {code: {...}},   # 逐代码裁决
          "post_trade": {...},        # 调仓后状态（权益/现金）
          "unparsed_codes": [...],    # 提到但抽不出数字的代码
        }
    """
    result: dict = {
        "available": False,
        "source_section": None,
        "rating": None,
        "summary": "",
        "targets": {},
        "post_trade": {},
        "unparsed_codes": [],
    }
    if not text:
        result["note"] = "决策文本为空"
        return result

    section = extract_section(text)
    if section is None:
        result["note"] = "决策里**没有**「最终交易决定」章节"
        return result
    result["available"] = True

    # 章节标题原文（回放用）
    for line in text.splitlines():
        if line.startswith("#") and any(k in line.lower() for k in _FINAL_HEADINGS):
            result["source_section"] = line.lstrip("#").strip()
            break

    m = _RATING_RE.search(section) or _RATING_RE.search(text)
    if m:
        result["rating"] = m.group("v").strip()

    m = _SUMMARY_RE.search(section)
    if m:
        result["summary"] = m.group("v").strip()[:1200]

    targets: dict[str, dict] = {}

    def _slot(code: str) -> dict:
        return targets.setdefault(code, {
            "target_pct": None,
            "target_pct_source": None,
            "direction": None,
            "lots": None,
            "shares_keep": None,
            "shares_sell": None,
            "cap_pct": None,
            "floor_pct": None,
            "stop_loss": None,
            "post_trade_pct": None,
            "post_trade_amount": None,
            "evidence": "",
            "note": "",
        })

    def _set(code: str, key: str, value, evidence: str, source: str) -> None:
        """写入一个抽取结果（**先到先得** ⇒ 第一次命中即定性，冲突留 note）。

        🔴 2026-11-05 09:4x（M-Q 收尾）修：旧实现里「**值相同**」会**继续往下写**
           ⇒ 后面那条规则（来源标签不同）会把 `target_pct_source` 覆盖掉
           ⇒ 512800 的值明明来自「风险预算定为约 15%」，来源标签却变成「目标约 X%」
           ⇒ **来源标注与实际出处不符** ⇒ 回放时会指错地方。
           ⇒ 现在：**值相同 ⇒ 直接返回**（保持第一次命中的来源与证据句）。
        """
        slot = _slot(code)
        cur = slot.get(key)
        if cur is not None:
            if cur != value:
                slot["note"] = (slot.get("note", "") +
                                f"；另见 {key}={value}（{source}）但已采信 {cur}").strip("；")
            return
        slot[key] = value
        if key == "target_pct":
            slot["target_pct_source"] = source
        if not slot["evidence"]:
            slot["evidence"] = evidence[:_EVIDENCE_MAX]

    # ---- 第 1 遍：子句级「动作词」⇒ 同一子句里的代码**一起**成立 ----
    for s_start, s_end, sentence in _sentence_offsets(section):
        if _is_conditional(sentence):
            continue
        for c_start, c_end, clause in _clause_spans(sentence, s_start):
            codes = [mm.group(1) for mm in _CODE_RE.finditer(clause)]
            if not codes:
                # 现金（不是 6 位代码）单独处理
                cm = _POST_CASH_RE.search(clause)
                if cm:
                    result["post_trade"]["cash_amount"] = float(cm.group(1).replace(",", ""))
                    result["post_trade"]["cash_pct"] = _norm_pct(float(cm.group(2)))
                    result["post_trade"]["evidence"] = clause.strip()[:_EVIDENCE_MAX]
                    continue
                cm = _POST_CASH_COLON_RE.search(clause)
                if cm:
                    result["post_trade"]["cash_amount"] = float(cm.group(1).replace(",", ""))
                    result["post_trade"]["cash_pct"] = _norm_pct(float(cm.group(2)))
                    result["post_trade"]["evidence"] = clause.strip()[:_EVIDENCE_MAX]
                    continue
                cm = _POST_CASH_PCT_RE.search(clause)
                if cm:
                    result["post_trade"]["cash_pct"] = _norm_pct(float(cm.group(1)))
                    result["post_trade"]["cash_amount"] = float(cm.group(2).replace(",", ""))
                    result["post_trade"]["evidence"] = clause.strip()[:_EVIDENCE_MAX]
                continue
            for pattern, direction, target_pct, note in _ACTION_RULES:
                hits = [mm for mm in pattern.finditer(clause)
                        if not _negated(clause, mm.start(), mm.group(0))]
                if not hits:
                    continue
                for code in codes:
                    slot = _slot(code)
                    if slot["direction"] is None:
                        # 🔴 2026-11-11（A20⑥）：**裸「卖出」只是默认推断**。
                        #    同一句里若该标的被**明确**说成「减仓 / 部分卖出」
                        #    （如「卖出（Sell）512800，但属于减仓而非清仓式看空」），
                        #    ⇒ 裸卖出**不生效**（否则会被写成「清仓 0%」），
                        #    ⇒ 交给更明确的表述裁决（第 ② 档「卖出约 N 份」/ 第 ③ 档「减仓」）。
                        if (direction == "卖" and target_pct == 0.0
                                and note == "原文写明卖出"
                                and code in _sentence_partial_sell_codes(sentence)):
                            continue
                        slot["direction"] = direction
                        slot["note"] = note
                        if target_pct is not None:
                            _set(code, "target_pct", target_pct, clause.strip(),
                                 "动作词（%s）" % note)
                        elif not slot.get("evidence"):
                            # 🔴 2026-11-10 修：**只给方向、不给目标权重**的档
                            #    （②带数量的卖出 / ③明确减仓）也必须**记下证据句**
                            #    ⇒ 否则方向对了但 `evidence` 是空的
                            #    ⇒ 交付件的「可回放」断链（探针 A20④ 会红）。
                            slot["evidence"] = clause.strip()[:_EVIDENCE_MAX]
                    if direction in ("买", "条件买入"):
                        lm = _BUY_LOTS_RE.search(clause)
                        if lm:
                            _set(code, "lots", _to_lots(lm.group(1)), clause.strip(),
                                 "原文「买 N 手」")
                        else:
                            mx = _MAX_LOTS_RE.search(clause)
                            if mx:
                                _set(code, "lots", _to_lots(mx.group(1)), clause.strip(),
                                     "原文「最多 N 手」（上界）")
                break

    # ---- 组合层：「权益敞口 … 降至 X%」⇒ **只进 portfolio 字段** ----
    # 🔴 2026-11-05 10:3x（M-Q 修）：组合层数字**永不归给单只代码**
    #    （旧实现把「权益敞口降至 15%」记到了最近的代码 563020 头上）。
    #    本遍**不要求句里有代码** ⇒ 组合层数字常出现在无代码的句子里。
    for s_start, s_end, sentence in _sentence_offsets(section):
        if _is_conditional(sentence):
            continue
        for rx, loose in ((_PORTFOLIO_PCT_RE, False), (_PORTFOLIO_PCT_LOOSE_RE, True)):
            for mm in rx.finditer(sentence):
                val = _norm_pct(float(mm.group(1)))
                cur = result["post_trade"].get("equity_pct")
                if cur is None:
                    result["post_trade"]["equity_pct"] = val
                    result["post_trade"]["equity_pct_evidence"] = sentence.strip()[:_EVIDENCE_MAX]
                elif cur != val:
                    result["post_trade"]["equity_pct_note"] = (
                        f"另见组合层权益 {val}%（{'原文' if loose else '原文'}）但已采信 {cur}%")
                break

    # ---- 组合层：**明确的目标**权益（只认「降至 / 定为 / 控制在 …」这类**目标动词**）----
    # 🔴 2026-11-11（Beta 避险落地，文档「三.3 极端行情下的仓位建议」）：
    #    `equity_pct` 的**宽松口径**（「组合层权益约 56%」）可能只是**陈述当前值**
    #    ⇒ 规则层**不许**拿它当目标去压降权益 ⇒ 这里单列一个**只认目标动词**的字段
    #    `equity_target_pct`（含证据句）⇒ 规则层只认它 ⇒ 口径唯一、来源可回放。
    #    ⚠️ **不动** `equity_pct` / `equity_pct_note`（既有口径与金标不变）。
    for _s_start, _s_end, _sentence in _sentence_offsets(section):
        if _is_conditional(_sentence):
            continue
        # 🔴 2026-11-11（审计缺陷 B）：这里**必须**用**严格目标动词表**
        #    （`_PORTFOLIO_TARGET_PCT_RE`）⇒ 预测/陈述句（「回升至 56%」「回到 56%」）
        #    **不再**被当成建议（否则规则层会把权益静默砍掉 24pp）。
        _mm = _PORTFOLIO_TARGET_PCT_RE.search(_sentence)
        if not _mm:
            continue
        result["post_trade"]["equity_target_pct"] = _norm_pct(float(_mm.group(1)))
        result["post_trade"]["equity_target_pct_evidence"] = _sentence.strip()[:_EVIDENCE_MAX]
        break

    # ---- 第 2 遍：句级「权重类数字」⇒ 归属**最近的、在它前面的代码** ----
    for s_start, s_end, sentence in _sentence_offsets(section):
        if _is_conditional(sentence):
            continue
        code_hits = [(mm.start(), mm.group(1)) for mm in _CODE_RE.finditer(sentence)]
        if not code_hits:
            continue

        def _owner(pos: int) -> str | None:
            """找 `pos` 之前**最近**的代码（距离 ≤ 260 字）；没有则找之后的（≤ 120 字）。

            🔴 **坐标口径**：`pos` 与 `code_hits` 里的位置**都必须是句内相对偏移**
               （正则是在 `sentence` 上匹配的）⇒ 调用方**不许**传绝对偏移。
               （2026-11-05 04:4x 实测：曾把绝对偏移传进来 ⇒ 靠前的句子侥幸命中、
                靠后的句子全部落空 ⇒ 511880 的「调仓后 33.5%」被漏掉。）
            🔴 2026-11-05 10:4x（M-Q 修）：**范围级守卫** —— 代码与数字之间若有
               「主题/组合/全仓…」等范围词 ⇒ 这个数**不是**那只代码的 ⇒ 返回 None。
            """
            before = [c for c in code_hits if c[0] <= pos and pos - c[0] <= 260]
            if before:
                cpos, ccode = before[-1]
                if _is_scope_level(sentence, cpos, pos):
                    return None
                return ccode
            after = [c for c in code_hits if pos < c[0] and c[0] - pos <= 120]
            return after[0][1] if after else None

        for rx, key, cast, source in (
            (_TARGET_PCT_RE, "target_pct", lambda g: _norm_pct(float(g)),
             "原文「权重/风险预算类动词 + X%」"),
            (_TARGET_PCT_LOOSE_RE, "target_pct", lambda g: _norm_pct(float(g)),
             "原文「目标约 X%」"),
            (_KEEP_SHARES_RE, "shares_keep", lambda g: _to_int(g), "原文「保留 N 股」"),
            (_SELL_SHARES_RE, "shares_sell", lambda g: _to_int(g), "原文「卖出 N 股」"),
            (_CAP_RE, "cap_pct", lambda g: _norm_pct(float(g)), "原文「硬上限 X%」"),
            (_FLOOR_RE, "floor_pct", lambda g: _norm_pct(float(g)), "原文「下限 X%」"),
            (_STOP_RE, "stop_loss", lambda g: float(g), "原文「止损价」"),
        ):
            for mm in rx.finditer(sentence):
                owner = _owner(mm.start())
                if owner is None:
                    continue
                _set(owner, key, cast(mm.group(1)), sentence.strip(), source)

        # 调仓后权重：「511880 约占 33.5%」（**代码写在前面** ⇒ 归属明确）
        for mm in _POST_PCT_RE.finditer(sentence):
            _set(mm.group("code"), "post_trade_pct", _norm_pct(float(mm.group("pct"))),
                 sentence.strip(), "原文「调仓后权重」")

        # 调仓后金额/权重：「511880 ≈10,000 元（33.5%）」
        for mm in _POST_AMOUNT_RE.finditer(sentence):
            owner = _owner(mm.start())
            if owner is None:
                continue
            _set(owner, "post_trade_amount", float(mm.group(1).replace(",", "")),
                 sentence.strip(), "原文「调仓后金额」")
            _set(owner, "post_trade_pct", _norm_pct(float(mm.group(2))),
                 sentence.strip(), "原文「调仓后权重」")

    # ---- 第 1.5 遍：**结构化决策表**（Trader 的「标的 | 方向 | 目标 | 理由」表）----
    #    🔴 2026-11-13（真实交付件 `snapshots/20261113_092926` 实证）：
    #       「最终交易决定」章是**散文**（「减至 12% 基础目标」「最高 8%」…）
    #       ⇒ 散文遍抽不出 `target_pct` ⇒ `make_decision_scenarios` 硬失败 ⇒ 重建链转红。
    #    ⇒ 现在额外读**裁决区**里的结构化表：**只认目标列**、**只认第一个有效数字%**、
    #       **只补空** ⇒ 散文抽得到的一律以散文为准（口径不被污染）。
    for _row in _authority_target_rows(_decision_region(text)):
        _cells = _row["cells"]
        # 🔴 2026-11-13（独立审查 + 变异探针实证）：**代码只认「标的列」**。
        #    旧口径在标的列没有代码时**扫全部格** ⇒ 「组合整体 | 减仓 | 10% | 主要是512800…」
        #    ⇒ 512800 拿到**组合级** 10%，且来源标签谎称来自「标的列」。
        #    ⇒ 标的列没有代码 ⇒ 整行跳过（**不回落**到理由列）。
        _codes = [m.group(1) for m in _CODE_RE.finditer(_cells[0])] if _cells else []
        if not _codes:
            continue
        _ti = int(_row["target_idx"])
        if _ti >= len(_cells):
            continue
        _pct = _table_target_pct(_cells[_ti])
        _di = _row.get("dir_idx")
        if _di is None:
            # 无「方向」表头 ⇒ 兼容旧版式「标的 | 方向 | 目标 | 理由」，
            # 但**不得**把**目标列**当成方向列。
            _di = 1 if (1 != _ti and len(_cells) > 1) else None
        _dir = (_table_direction(_cells[int(_di)])
                if _di is not None and int(_di) < len(_cells) else None)
        for _code in _codes:
            if _pct is not None:
                _set(_code, "target_pct", _pct, _row["line"],
                     "最终裁决表（标的/方向/目标列）")
            if _dir:
                _sl = _slot(_code)
                if _sl["direction"] is None:
                    _sl["direction"] = _dir
                    if not _sl.get("evidence"):
                        _sl["evidence"] = _row["line"][:_EVIDENCE_MAX]

    # ---- 第 2 遍之补（**独立一遍**）：**代码锚定**的紧致写法（2026-11-13 实证）----
    #    「`512800`减至20%、`515450`标准档20%」⇒ 旧口径（权重名词 + 动词）抽不出
    #    ⇒ 这两只 `target_pct=None` ⇒ `make_decision_scenarios` 硬失败 ⇒ 重建链转红。
    #
    #    🔴 **必须独立成一遍**（2026-11-13 独立审查实证）：若与严格规则**逐句交错**，
    #    则「先出现的紧致句」会赢过「后出现的严格句」（`_set` 先到先得）
    #    ⇒ 「`512800`减至20%。512800目标权重降至12%。」会取到 **20.0**（宽松口径压过
    #    严格口径）⇒ 与「严格规则优先」的口径**不一致**。
    #    ⇒ 现在：**先把整节严格规则跑完**，再跑这一遍 ⇒ 严格规则**全局**优先。
    for s_start, s_end, sentence in _sentence_offsets(section):
        if _is_conditional(sentence):
            continue
        # 🔴 **坐标口径**：`mm.start()` 是**句内相对偏移**；`_clause_spans` 返回的是
        #    **绝对偏移**（base=s_start）⇒ 二者**必须**先统一到句内相对口径再比较
        #    （2026-11-13 首版混用 ⇒ 否决语境守卫**没生效**，实测「备选方案被否决」
        #    仍被抽成 20.0 ⇒ 独立审查 A-1 差点漏修）。
        rejected_codes = _reject_bound_codes(sentence)
        clause_spans = [(cs - s_start, ce - s_start)
                        for cs, ce, _clause in _clause_spans(sentence, s_start)]
        code_spans = [(mm.start(), mm.end()) for mm in _CODE_RE.finditer(sentence)]
        for mm in _TERSE_TARGET_RE.finditer(sentence):
            c_start, c_end = 0, len(sentence)
            for cs, ce in clause_spans:
                if cs <= mm.start() < ce:
                    c_start, c_end = cs, ce
                    break
            # 🔴 **否决/备选语境守卫**（独立审查 A-1）：被否决/备选/未采纳的标的
            #    **不许**拿到目标权重（否则交付件会给一只被一票否决的标的编出「目标 20%」）。
            #    🔴 判定口径 = **这个否决词绑的是哪只**（`_reject_bound_codes`）
            #    ⇒ 同一个句子里「否决`515650`」**不会**误伤前面的 515450。
            if mm.group("code") in rejected_codes:
                continue
            _set(mm.group("code"), "target_pct", _norm_pct(float(mm.group("pct"))),
                 sentence.strip(), "原文「代码 + 目标设定词/档位 + 数字%」（紧致写法）")

    # ---- 第 3 遍：补齐「买 N 手 ⇒ 目标权重取调仓后权重」 ----
    for code, slot in targets.items():
        if slot["target_pct"] is None and slot["post_trade_pct"] is not None \
                and slot["direction"] in ("买", "条件买入"):
            slot["target_pct"] = slot["post_trade_pct"]
            slot["target_pct_source"] = ("原文「调仓后权重」（买/条件买 N 手 ⇒ 整手金额即权重）")
            slot["note"] = (slot["note"] + "；目标权重由「调仓后权重」读出").strip("；")
        if slot["lots"]:
            slot["shares_hint"] = slot["lots"] * 100

    # 提到了但没抽出任何数字的代码
    mentioned = {mm.group(1) for mm in _CODE_RE.finditer(section)}
    for code in sorted(mentioned):
        slot = targets.get(code)
        if not slot or slot.get("target_pct") is None:
            result["unparsed_codes"].append(code)

    result["targets"] = targets
    return result


#: ── 否决解析（合规筛选：审查 + 一票否决 + 顺延）─────────────────────────────
#:
#: 🔴 **与 `probes/probe_llm_parser.py` 的 `_REJECT_WORDS` 无关**：
#:    那一组词表（「方案/被否/否决/不考虑/未采纳/备选」）是「**清仓词不算本轮裁决**」
#:    的语义（`extract_final_decision` 在用）；本组词表是「**否决某只标的**」的语义
#:    ⇒ **两套词表不许混用**。
#:
#: 🔴 2026-11-11（真实夹具 `tests/fixtures/ta_decision_real_20261110.md` 实证）：
#:    旧口径把 TA **要买**的 `515450`/`515650`、**要减**的 `512800` 全判成否决
#:    ⇒ 计划里 5 席几乎被清空 ⇒ 闸门 8 个阶段转红。
#:    根因：① 切句只按 `。；\n` ⇒ `当前不买；条件买入` 被切成两半（"条件买入"丢失）；
#:          ② 同文件**已有**的 `_is_conditional` 没被复用；
#:          ③ 表格行没按「**整行**」判定（单元格里的「；」把动作切碎了）。
#:    ⇒ 现在：**表格行整行判定 + 条件/买入意图豁免 + 子句级反向护栏 + 词表补齐**。
_REJECT_VERBS: tuple[str, ...] = (
    # ① 原有动词
    "剔除", "排除", "否决", "移除", "不选", "不建议", "不考虑",
    "不建仓", "不买", "不要买", "pass on", "reject",
    # ② 2026-11-11 补齐（真实 TA 用语；缺一个就**漏判**一条真否决）
    "回避", "不配置", "不予配置", "暂缓", "放弃", "不纳入",
    "退出候选", "不参与", "0仓观察", "零仓观察",
    # ③ 2026-11-11 审计补齐（真实中文用法；缺一个就**漏判**一条真否决）
    "拒绝", "不予考虑",
)

#: 「持有 / 卖出」类动作：紧跟在否决动词**后面** ⇒ 该否决词**惰性**。
#:    🔴 实测事故：`最终决策是**减持 512800，但不建议清仓**` ⇒ 「不建议」后面是
#:       「清仓」= 继续持有 ⇒ **不是否决** ⇒ 512800 不该进否决名单。
_REJECT_KEEP_ACTIONS: tuple[str, ...] = (
    "清仓", "清掉", "清零", "卖出", "卖", "减仓", "减持", "换仓", "加仓", "补仓",
)

#: 反向语义护栏（**子句级**）：出现在本子句 ⇒ 该子句不算否决。
#:    🔴 2026-11-11 修：旧口径是**整句**护栏 ⇒ `TA 拥有一票否决权，故剔除 512800。`
#:       里的**真否决**被整句吞掉（漏判）⇒ 现在只中和**它所在的那一句/那一行**。
_REJECT_NEGATIONS: tuple[str, ...] = (
    # 🔴 2026-11-11 修（**假护栏 ⇒ 漏放真否决**）：旧口径含「一票否决」⇒
    #    任何含这四个字的子句都被 `_guarded()` 整条中和
    #    ⇒ 「516620 **正式一票否决**，不买」这种**真否决**被放过
    #    ⇒ 该标的没进否决名单 ⇒ 席位被顺延给本该被否的标的（实测坐实）。
    #    ⇒ 现在**只**保留**条件式**的「若/如…一票否决」（那才是假设句），
    #    「否决权」（能力陈述）仍由本表守住。
    "否决权", "若一票否决", "如一票否决", "一旦一票否决", "假如一票否决", "倘若一票否决",
    "不否决", "未否决", "无需否决", "若否决", "如否决",
    "不予否决", "不得否决", "并非否决", "不是否决", "不构成否决", "不算否决",
    # ③ 2026-11-11 审计补齐：「**不排除** X」= 仍可入选 ⇒ 不是否决
    "不排除",
)

#: 「**禁止性**」反向语义（**中间允许隔字**）：例 `不得被判成否决` / `不可否决`。
#:    🔴 2026-11-11 实测事故（夹具头部说明文字）：
#:       「`512800`（减）、`515450`/`515650`（买）**不得**被判成否决」
#:       ⇒ 旧词表只有连续的「不得否决」，中间隔了「被判成」⇒ 护栏失效
#:       ⇒ **要买/要减的标的被误判成否决**（实测夹具整文件误伤 3 只）。
#:    ⇒ 现在用正则覆盖「否定词 + ≤16 字 + 否决类动词」（16 字够跨过「对 <代码> 的」这种隔字）。
#: 🔴 2026-11-11 修（真误伤）：否定护栏必须覆盖**全部**否决动词，
#:    旧口径只认 `否决|剔除|排除|不选|移除` ⇒ 「**而不是放弃**有效权益」
#:    这类**否定式**没被护栏中和 ⇒ 「放弃」被当成否决动词
#:    ⇒ 交付件把 **512800**（TA 明写「保留 512800 的 16% 进攻核心」）判成**否决** ✗✗
#:    ⇒ 后果：核心持仓被剔除 ⇒ 席位顺延成别的标的。
#:    ⇒ 现在把 `放弃/回避/暂缓/拒绝/不配置/不予配置/不参与/不纳入/不参与` 一并纳入。
_REJECT_NEGATION_RE = re.compile(
    # 🔴 2026-11-11 补「**不能**」：旧词表只有「不可 / 不得」，
    #    ⇒「**不能**仅因模型存在局限就一票否决 `512800`」（研究经理明写**不许否** 512800）
    #    逃过护栏 ⇒ 512800 被误判成否决 ⇒ 计划把 512800 变成「维持」席位、515450 消失。
    r"(?:不得|不可|不应|不能|不至于|无须|无需|不予|并非|不是|不构成|不算|避免|禁止|杜绝|而不是|而非|不作|不做)"
    r"[^，；。｜|\n]{0,16}?"
    r"(?:否决|剔除|排除|不选|移除|放弃|回避|暂缓|拒绝|不配置|不予配置|不参与|不纳入)"
)

#: 「能力陈述」标记：说了「TA **可以**否决」≠ 本轮否决了谁。
#:    例：`TA 拥有一票否决权，可在必要时否决 512800 等任一标的。` ⇒ 不是否决。
_REJECT_CAPABILITY: tuple[str, ...] = (
    "必要时", "任一", "任意", "任何", "视需要", "视情况", "视情形",
    "有权", "酌情", "需要时", "如需",
)

#: 「买入意图」标记：出现 ⇒ 该句 / 该行**不算否决**（是条件买，不是剔除）。
#:    🔴 2026-11-11 修：`当前不买；条件买入` 是**条件买**（TA 仍要它）⇒ 不是否决。
_BUY_INTENT_WORDS: tuple[str, ...] = (
    "条件买入", "条件建仓", "条件满足", "视情况买入", "视情况建仓",
    "视情况配置", "视情形买入", "才可买入", "方可买入", "才可建仓", "方可建仓",
)
#: 「若…则买入」这类**条件买入**（动词前面没固定词，只能用正则）。
_BUY_INTENT_RE = re.compile(
    r"(?:若|如果|假如|倘若|一旦|除非)[^，；。｜|\n]{0,40}?则(?:才|方可)?"
    r"(?:买入|买|建仓|配置|参与)")
#: 买入意图标记**紧跟**某个代码 ⇒ 该意图只保护那个代码（不保护同行/同句其它代码）。
_BUY_INTENT_TAIL = 10

#: 名称抽取的「不像名称」词：状态词 / 仓位词。
_NAME_STOP_WORDS: tuple[str, ...] = (
    "持有", "未持有", "已持有", "空仓", "满仓", "观望", "观察", "配置",
    "调仓", "仓位", "权重", "买入", "卖出", "新建", "加仓", "减仓",
    # 🔴 2026-11-11 修（连词被当成名称）：
    #   「…563020 **以及** 515020/516310/512820 正式一票否决」
    #   ⇒ `_name_near` 的**向左回看**兜底把连词「以及」当成了 515020 的名称
    #   ⇒ 交付件的被否名单里出现「515020 | 以及」这种垃圾名（实测 20261111_161126）。
    #   ⇒ 现在**连词/连接词一律不算名称** ⇒ 取不到就返回空串（渲染层显示「—」，
    #      再由计划层用分析报告的 code→name 表回填 ⇒ 515020 应显示「银行ETF华夏」）。
    "以及", "还有", "另外", "此外", "同时", "其中", "包括", "外加", "另加",
    "连同", "换言之", "并且", "例如", "比如", "也就是说", "除此之外",
    "其余", "其他", "其一", "尤其是", "特别是",
)

#: 「**裸**方向/状态词」：**整格相等**才算「不是名称」。
#:    🔴 2026-11-11 审计补齐：`llm_final` 抽名称时，决策表里**方向/状态那一格**
#:       若写的是裸词（`| 515020 | 否 | …不买 |`、`| 515020 | 维持 | … |`），
#:       ⇒ 该格被当成 ETF 名称 ⇒ 交付件被否名单出现「515020 | 否」这种垃圾名。
#:    ⚠️ **必须是整格相等、不能进 `_NAME_STOP_WORDS` 的子串表**：
#:       子串匹配会把「**有**色金属ETF」「**减**…」等**真名称**一起毙掉
#:       （`_NAME_STOP_WORDS` 是 `w in txt` 的**子串**判定）⇒ 单独用 frozenset。
_NAME_BARE_STATE_WORDS: frozenset[str] = frozenset({
    # 裸是否/有无
    "是", "否", "有", "无",
    # 裸方向动词
    "买", "卖", "持", "留", "增", "加", "减", "清",
    # 裸状态词
    "维持", "保留", "目标",
})

#: 代码与剔除动词之间的最大距离（**字**）——「同一句或同一表格行内」。
_REJECT_WINDOW = 40

#: 理由（原文证据句）的最大长度。
_REJECT_REASON_MAX = 200

#: 百分比（权重）——「目标权重 > 0%」⇒ 该表格行不是否决。
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

#: 🔴 2026-11-13（真实交付件 `snapshots/20261113_092926` 实证）：**范围级**宾语。
#:    「放弃**整个银行主题**偏于反应过度」= 放弃**主题** ⇒ 不是否决某一只代码。
#: 🔴 2026-11-13：被「不选」的**方案名**（动作/立场词）⇒ 「不选 <方案名>」= 不采用那个方案
#:    ⇒ **不是**否决某一只代码（实测：「不选Sell：512800仍具有…」）。
#:    ⚠️ 必须**非空**且**后面不是代码** ⇒ 表格动作格里光秃秃的「不选」**仍判否决**
#:    （`tests/test_llm_rejections.py::test_表格行剔除命中且名称取下一格` 钉死这条）。
#: 🔴 2026-11-13（独立审查 + 变异探针实证）：「不选 <方案名>」里**只有「处置既有仓位」类**
#:    方案名才**豁免**（Sell / Reduce / Trim / Exit / 清仓 / 卖出 / 减仓 / 减持）
#:    ——「不选Sell：512800仍具有…」= 研究经理在解释「**为什么不选**那个方案」⇒ 不是否决 512800。
#:    ⚠️ 「**建仓/持有**类」方案名（Buy / Add / Hold / 买入 / 加仓 / 持有 / 维持）
#:    ⇒「不选买入512800」= **真否决** ⇒ **不许**豁免
#:    （旧口径把两类混在一个闭集 ⇒ 真否决被放过）。
_REJECT_EXIT_PLAN_RE = re.compile(
    r"(?:sell|reduce|trim|exit|清仓|卖出|减仓|减持|清掉)",
    re.IGNORECASE,
)

_REJECT_SCOPE_AFTER: tuple[str, ...] = (
    "整个", "全部", "整体", "全仓", "全组合", "该主题", "这个主题", "主题",
    "组合", "该板块", "这个板块", "板块", "全市场",
)

_REJECT_VERB_RES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (v, re.compile(re.escape(v), re.IGNORECASE)) for v in _REJECT_VERBS
)

#: 判定单元的切分：表格行（**整行**）与散文句（按「。」切，**不**按「；」切）。
_CLAUSE_SPLIT_RE = re.compile(r"[，；]")

#: 「顿号」并列项切分：把**逗号子句**再切成**并列项**（item）。
#: 🔴 2026-11-11 修（真误伤）：子句切分只按「，」「；」⇒ 一串并列项
#:    （`512800、515450、512170、159207与563020`）被合并成**同一个子句**
#:    ⇒ 该子句**末尾**的否决动词（`不买`）**反向污染**前面的标的。
_ITEM_SPLIT_RE = re.compile(r"、")

#: 「动作词」表：并列项里**去掉否决动词之后**仍有动作词 ⇒ 该项**自带动作**
#:    （自成一体）⇒ **不被别的项的否决动词管辖**。
#: 🔴 2026-11-11：必须**先剔除否决动词再看动作词** —— `159207与563020不买`
#:    里的「买」是**否决动词的一部分** ⇒ 否则该项会被误当成「自带动作」。
_ITEM_ACTION_WORDS: tuple[str, ...] = (
    "只", "仅", "买", "建仓", "加仓", "补仓", "保留", "持有", "维持", "留",
    "卖", "清仓", "减持", "减仓", "减到", "换成", "换入", "配置", "观望", "观察",
)


def _heading_spans(text: str) -> list[tuple[int, str]]:
    """markdown 标题的 (起始偏移, 标题原文) 列表（用于给命中句定位章节）。"""
    out: list[tuple[int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append((pos, stripped.lstrip("#").strip()))
        pos += len(line)
    return out


def _section_at(headings: list[tuple[int, str]], pos: int) -> str:
    """`pos` 处所在的**最近上级标题**（没有 ⇒ 空串）。"""
    name = ""
    for start, title in headings:
        if start > pos:
            break
        name = title
    return name


def _table_cells(row: str) -> list[str]:
    """表格行 ⇒ 单元格列表（去掉首尾的空格子）。"""
    parts = row.strip().split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _clauses(sentence: str) -> list[tuple[int, str]]:
    """按「，」「；」切子句 ⇒ [(在句中的起点, 子句)]。"""
    out: list[tuple[int, str]] = []
    pos = 0
    for m in _CLAUSE_SPLIT_RE.finditer(sentence):
        seg = sentence[pos:m.start()]
        if seg.strip():
            out.append((pos, seg))
        pos = m.end()
    seg = sentence[pos:]
    if seg.strip():
        out.append((pos, seg))
    return out


def _reject_verb_hits(text: str) -> list[tuple[int, str]]:
    """本子句里的「否决动词」命中 ⇒ [(位置, 词)]。

    🔴 「不建议清仓」这类**持有语义**的命中**作废**（见 `_REJECT_KEEP_ACTIONS`）。
    """
    keep_alt = "|".join(_REJECT_KEEP_ACTIONS)
    out: list[tuple[int, str]] = []
    for _word, rx in _REJECT_VERB_RES:
        for m in rx.finditer(text):
            # 🔴 2026-11-11 审计修：允许「也/则/并」+ 可选「不」再接持有动作
            #    （`不买也不卖 512800` = 维持 ⇒ 不是否决；旧口径只认紧邻的「卖」）。
            if re.match(r"[\s*`_~]*(?:也|则|并|且)?不?(" + keep_alt + ")", text[m.end():]):
                continue
            _tail = text[m.end():].lstrip("`*（( ")
            # 🔴 2026-11-13（真实交付件 `snapshots/20261113_092926` 实证）：
            #    「**不选 <方案名>**」= 研究经理在解释「**为什么不选**空头的 Sell 方案」
            #    ⇒ 「不选」后面跟的是**方案名**（Sell / 清仓 / 减持…）而不是**代码**
            #    ⇒ **不是**否决那只代码。
            #    实测误伤：「- 不选Sell：512800仍具有入选组合中最完整的相对趋势证据…」
            #    ⇒ 512800 被误判成一票否决 ⇒ 交付层把它变成「维持席位」、
            #       真正该顺延补位的 515450 消失 ⇒ 入选名单整体错。
            if m.group(0) in ("不选", "不选择"):
                _t14 = _tail[:14]
                _opt = _REJECT_EXIT_PLAN_RE.match(_t14) if _t14 else None
                if _opt and not _CODE_RE.match(_t14):
                    # 🔴 2026-11-13（独立审查）：方案名后面**紧跟代码**
                    #    ⇒ 那不是方案名，是「不选<动作>某只标的」⇒ **真否决** ⇒ 不许豁免。
                    if not _CODE_RE.match(_t14[_opt.end():]):
                        continue
            # 🔴 2026-11-13（同源第二处）：「**放弃整个银行主题**」= 放弃**主题**
            #    （范围级）⇒ 不是否决某一只代码。
            if m.group(0) == "放弃" and any(
                _tail.startswith(w) for w in _REJECT_SCOPE_AFTER
            ):
                # 🔴 2026-11-13（独立审查 + 变异探针实证）：「放弃**组合内的**512800」
                #    = **点名**否决 512800 ⇒ 范围词后面若跟了**具体代码**，就不许豁免。
                _after = _tail
                for _w in _REJECT_SCOPE_AFTER:
                    if _after.startswith(_w):
                        _after = _after[len(_w):]
                        break
                _seg = re.split(r"[，,。；;\n]", _after)[0]
                if not _CODE_RE.search(_seg):
                    continue
            out.append((m.start(), m.group(0)))
    out.sort()
    return out


#: 🔴 2026-11-13（真实交付件 `snapshots/20261113_092926` 实证）：「**否定式剔除**」
#:    ——「**不满足**从候选池彻底剔除的必要性」= **明确否掉剔除** ⇒ 不是一票否决。
#:    实测事故：该句在研究经理段 ⇒ 512800 被误判成**否决** ⇒ 交付层把 512800 变成
#:    「维持席位」、把真正该顺延的 515450 挤掉 ⇒ 入选名单整体错。
#:    ⇒ 口径：**否定词 + ≤16 字 + 剔除类动词 + 「的必要性/的条件/…」** ⇒ 豁免。
#:    ⚠️ 与 `_REJECT_NEGATION_RE` 的差别：后者只看「否定词 + 动词」（会漏掉
#:       「不满足…剔除的**必要性**」这种带**尾巴**的写法）；本条**必须**带尾巴
#:       ⇒ 「不满足条件即剔除 512800」（真否决）**不会被**误豁免。
_REJECT_NEG_NECESSITY_RE = re.compile(
    r"(?:不满足|不支持|不具备|不足以|无需|无须|不需要|没有|并非|不构成|不再)"
    r"[^，；。｜|\n]{0,16}?"
    r"(?:否决|剔除|排除|移除|放弃|回避)"
    r"[^，；。｜|\n]{0,6}?"
    r"(?:的)?(?:必要性|的条件|的理由|的依据|的门槛|的前提|标准|空间|需要)"
)


def _guarded(clause: str) -> bool:
    """本子句是否被反向语义护栏 / 能力陈述中和。"""
    if any(neg in clause for neg in _REJECT_NEGATIONS):
        return True
    if _REJECT_NEGATION_RE.search(clause):
        return True
    if _REJECT_NEG_NECESSITY_RE.search(clause):
        return True
    return any(mark in clause for mark in _REJECT_CAPABILITY)


def _buy_intent(text: str) -> tuple[bool, set[str]]:
    """(有无「买入意图」, 被该意图**独占**绑定的代码集合)。

    独占绑定 = 买入意图标记**紧跟**某个代码（`条件买入 515450`）⇒ 该意图只保护
    那个代码，同句/同行的**其它**代码照常判定。
    """
    attached: set[str] = set()
    found = bool(_BUY_INTENT_RE.search(text))
    for word in _BUY_INTENT_WORDS:
        for m in re.finditer(re.escape(word), text):
            found = True
            cm = _CODE_RE.search(text[m.end():m.end() + _BUY_INTENT_TAIL])
            if cm:
                attached.add(cm.group(1))
    return found, attached


def _is_name_like(text: str) -> bool:
    """这段文字**像不像标的名称**（不像 ⇒ 取名返回空串，**不猜**）。"""
    txt = text.strip().strip("*`_~ ").strip()
    if not txt or len(txt) > 30:
        return False
    if not re.search(r"[一-鿿A-Za-z]", txt):
        return False
    if txt[0].isdigit():
        return False
    if txt[0] in "、，,。；;：:（）()《》「」和与及或":
        return False
    if "%" in txt or "％" in txt:
        return False
    if re.fullmatch(r"[\d\s.,元份股手个×xX\-—–:：/／]*", txt):
        return False
    if _CODE_RE.search(txt):
        return False
    if any(v in txt for v in _REJECT_VERBS):
        return False
    if any(a in txt for a in _REJECT_KEEP_ACTIONS):
        return False
    # 🔴 2026-11-11 审计补齐：裸方向/状态词（`否` / `维持` / `目标` …）**整格相等**
    #    ⇒ 不是标的名称（决策表的方向/状态列被误当成名称列）。
    #    ⚠️ 必须是**整格相等**：`_NAME_STOP_WORDS` 是子串判定 ⇒ 「**有**色金属ETF」
    #       这类**真名称**会被一起毙掉 ⇒ 裸词单独走 frozenset 的整格判定。
    if txt in _NAME_BARE_STATE_WORDS:
        return False
    return not any(w in txt for w in _NAME_STOP_WORDS)


def _name_near(sentence: str, code: str, code_pos: int) -> str:
    """尽力从**同一句/同一表格行**里取标的名称（取不到 ⇒ 空串，**不猜**）。

    - 表格行：`| 512800 | 银行ETF华宝 | 不选 |` ⇒ 取**下一格**；
    - 散文：代码**紧邻**的中文/英文名（如「剔除 512800 银行ETF华泰柏瑞」）。

    🔴 2026-11-11 修（旧口径会抽出垃圾名）：
       `'0%'`（权重格）、`'、516320'`（顿号开头）、`'和512170'`（含代码）、
       `'未持有'`（状态词）⇒ 现在**一律返回空串**（渲染层显示「—」）。
    """
    if "|" in sentence:
        cells = _table_cells(sentence)
        for idx, cell in enumerate(cells):
            if code in cell:
                nxt = cells[idx + 1] if idx + 1 < len(cells) else ""
                return nxt if _is_name_like(nxt) else ""
        return ""
    tail = sentence[code_pos + len(code):]
    m = re.match(r"[\s*`_~]*([^\s|，。；：,;:（）()、]{2,30})", tail)
    if m and _is_name_like(m.group(1)):
        return m.group(1)
    head = sentence[:code_pos]
    m2 = re.search(r"([^\s|，。；：,;:（）()、]{2,30})\s*$", head)
    if m2 and _is_name_like(m2.group(1)):
        return m2.group(1)
    return ""


def _positive_weight(cells: list[str], code_idx: int) -> bool:
    """本行**目标权重**是否 > 0% ⇒ 本行代码**不是**否决。

    🔴 2026-11-11 审计修（旧口径只看「动作格**之后**」的格子）：
       动作词落在**理由格**时（`| 563020 | 买 | 8% | 不买 512170 |`），
       它**前面**的权重列 `8%` 从不被检查 ⇒ **买行被误否**。
    ⇒ 现在：取「代码格之后、**最后一格（理由列）之前**」的**第一个百分比**
       —— 那才是目标权重列；理由列里的百分比**不算**（否则
       `| 563020 | 持（0仓观察） | 0% | …不值得20%… |` 会被误豁免）。
    """
    for cell in cells[code_idx + 1:-1]:
        m = _PCT_RE.search(cell)
        if m:
            return float(m.group(1)) > 0
    return False


#: 「**弱**否决动词」：单独出现时**未必**是在否决某只标的
#: （「清仓等于**放弃**唯一有效趋势暴露」「**回避**高波动品种」「**暂缓**加仓」…）
#: ⇒ 只有当代码**就在同一子句**里时才算否决；
#:   **跨子句继承**来的代码**不认**弱动词（否则会把「与池内次优 515450 的最终分差达…」
#:   后面的「此时清仓等于放弃唯一有效趋势暴露」挂到 515450 头上 ⇒ 真误伤）。
#: 🔴 2026-11-11 实测（2026-11-09 那一轮真实决策 md 实证）：该句把 **515450**
#:    （TA 明写「买 515450 16%」）判成否决 ⇒ 席位被顺延成别的标的。
_REJECT_VERBS_WEAK: tuple[str, ...] = (
    "放弃", "回避", "暂缓", "拒绝", "不予考虑", "不予采用", "不做", "不再考虑",
)


#: 「**持有 / 不动**」语义（**子句级**）：出现 ⇒ 该子句里的代码**不是**否决。
#: 🔴 2026-11-11 审计补齐：`512800 保留本仓，其余不考虑。` 这类句子，
#:    后一子句的动词（「不考虑」）**不得**回溯到前一子句的「保留」代码。
_KEEP_MEANING_WORDS: tuple[str, ...] = (
    "保留", "持有", "维持", "继续持", "不清仓", "不加仓", "不卖出",
    "暂不动", "不动仓", "不调整",
)


def _keep_meaning(clause: str) -> bool:
    """本子句是否带「持有 / 不动」语义。"""
    return any(word in clause for word in _KEEP_MEANING_WORDS)


#: 「**比较引用**」：代码只是被拿来**比**，不是本句的**裁决对象**。
#: 🔴 2026-11-11 实测（2026-11-09 那一轮真实决策 md 实证）：
#:    「563020 的题设有事实错误要纠正：……但结论不变：**与 515450 相关性 0.917**、
#:      红利敞口只用 **515450 表达**足够，**不建仓**、更不给 20%。」
#:    —— 这句的**主语是 563020**（上一句就在说它），「不建仓」说的也是 563020；
#:      515450 在这里只是**比较对象 / 表达工具**。
#:    ⇒ 旧口径按「动词 40 字内最近的代码」归属 ⇒ 把 515450 判成**否决** ✗✗
#:      （而同一份 md 的方向表明写「买 515450 16%」⇒ **交付件自相矛盾**）
#:      ⇒ 后果：TA 要买的那只被剔掉 ⇒ 席位被顺延成别的标的。
#:    ⇒ 现在：命中下列**闭集**模式的代码标为「引用」⇒ **不作为否决主体**。
_MD_REF_COMPARE_RE = re.compile(
    r"(?:与|和|跟|相比|对比|较之|之于)\s*(?P<code>[1-9]\d{5})"
    r"\s*(?:的)?\s*(?:相关性|相关度|相关系数|重叠度|重合度|重叠|重合|相似度|重复度|替代性"
    r"|同族|同质|同类|同源|同族类)")
_MD_REF_TOOL_RE = re.compile(
    r"(?:只|仅|来)?\s*(?:用|由|以)\s*(?P<code>[1-9]\d{5})"
    r"\s*(?:来)?\s*(?:表达|代表|覆盖|代替|承接)")

#: 🔴 2026-11-13（真误伤 · 真实交付件 `20261113_092926` · 研究经理结论段）：
#:    「**弱于 / 优于 … CODE**」里的 `CODE` 是**比较对象**，不是本句的**裁决对象**。
#:    实测原句：
#:      「- 563020（红利低波ETF易方达）：若出现在顺延池即否决
#:        （与 515450 同族、相关性 0.919、分位 86% 拥挤、最终分 +0.0370 **弱于 515450**）」
#:    ⇒ 真机同时写明「**515450 仅条件性买入**」「| 515450 | **买** |」
#:    ⇒ 旧口径把 515450 当成**否决主体**（`_REJECT_WINDOW=40` 把它拉进动词作用域）
#:       ⇒ TA 要买的那只被剔出候选 ⇒ 席位被顺延给别的标的（本轮实际顺延给了 512890）。
#:    ⇒ 本条把「比较词 + CODE」标成**引用** ⇒ 不作否决主体（与 `_MD_REF_COMPARE_RE` 同族）。
_MD_REF_SUPERIOR_RE = re.compile(
    r"(?:弱于|强于|优于|逊于|不如|好于|差于|劣于|胜过|低于|高于)\s*"
    r"[\*`\s]*(?P<code>[1-9]\d{5})"
)

#: 🔴 2026-11-13（真误伤 · 同一交付件）：「**代码 + 带符号的小数**」= **比分引述**
#:    （「医药类 512170 -1.3950、512010 -1.4778」）⇒ 不是**裁决主体**。
#:    ⇒ 旧口径把它塞进 `prev_codes` ⇒ **跨子句继承** ⇒ 下一子句的「否决它等于…」
#:       落到 512010 头上 ⇒ 一只**没被点名否决**的标的被写进「TA 一票否决」名单。
#:    ⇒ 只在**跨子句继承**通道生效（子句内 R1/R2/R3 仍按原语义判）⇒ 口径收紧、影响面最小。
_MD_DATA_CITATION_RE = re.compile(
    r"(?<![0-9])(?P<code>[1-9]\d{5})[ \t]*[-+]\d+\.\d+"
)

#: 🔴 2026-11-11（真误伤 · 每日链快照 `20261111_142244`）：「避免与 `X` 重复/重叠」里的 `X` 是**被保留的那只**，不是被否决的那只。
#:    实测：「`515020`、`516310`、`512820` 不买，**避免与 `512800` 重复银行暴露**；
#:          其他红利低波同族产品不买，**避免与 `515450` 重复**」
#:    ⇒ `512800`（TA 明写「减，保留」）被当成下一子句的「不买」的**主体**⇒ 误杀。
_MD_REF_AVOID_RE = re.compile(
    r"(?:避免|防止|以免|杜绝)"
    r"\s*(?:与|和|跟)\s*[\*`\s]*(?P<code>[1-9]\d{5})"
)


#: 「比较指标」括号：代码**紧跟**「（相关性…）」⇒ 该代码是**比较提及**
#:    （只是拿来比分的），不是本句的**裁决对象**。
#: 🔴 2026-11-11 真实交付件 `20261111_122825`（Trader 交易计划段）：
#:    「563020 E 实为 +0.1074 **但弱于同族 515450（相关性 0.917）绝不建仓**」
#:    ⇒ 「绝不建仓」说的是 **563020**；515450 只是**比较对象**
#:    ⇒ 旧口径按「40 字内最近的代码」归属 ⇒ 把 515450（TA 明写「买 515450」）
#:    判成否决 ✗✗（⇒ 席位被顺延成别的标的）。
#:    ⇒ 注意：这**不是** `_reference_codes` 的既有语义（它要求比较词在代码**前面**）
#:       ⇒ 本条只在**并列项**判定路径里生效（`_item_scope_hits`），既有护栏一字未动。
_MD_REF_METRIC_RE = re.compile(
    r"(?P<code>[1-9]\d{5})\s*[（(]\s*(?:相关性|相关度|相关系数|重叠度|重合度|"
    r"重叠|重合|相似度|重复度|替代性)"
)


def _reference_codes(clause: str) -> set[str]:
    """本子句里属于「比较引用 / 工具引用」的代码（**不是**裁决对象）。

    🔴 口径收紧：必须**同时**命中「引用词 + 代码 + 比较/表达名词」才算引用
       ⇒ 「剔除 512170 与 512690」这种**并列主体**不会被误伤（后面没有比较名词）。
    """
    out: set[str] = set()
    for rx in (_MD_REF_COMPARE_RE, _MD_REF_TOOL_RE, _MD_REF_AVOID_RE,
               _MD_REF_SUPERIOR_RE):
        for m in rx.finditer(clause):
            out.add(m.group("code"))
    return out


def _item_spans(clause: str, cstart: int) -> list[tuple[int, int]]:
    """把逗号子句按顿号「、」切成**并列项** ⇒ [(绝对起点, 绝对终点)]。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _ITEM_SPLIT_RE.finditer(clause):
        if clause[pos:m.start()].strip():
            spans.append((cstart + pos, cstart + m.start()))
        pos = m.end()
    if clause[pos:].strip():
        spans.append((cstart + pos, cstart + len(clause)))
    return spans


def _own_action(text: str) -> bool:
    """并列项里**去掉否决动词之后**是否还剩「动作词」（⇒ 该项自带动作）。"""
    rest = list(text)
    for pos, word in _reject_verb_hits(text):
        for offset in range(pos, pos + len(word)):
            rest[offset] = " "
    tail = "".join(rest)
    return any(word in tail for word in _ITEM_ACTION_WORDS)


def _item_scope_hits(
    sentence: str,
    clause: str,
    cstart: int,
    subjects: list[tuple[int, str]],
    verbs: list[tuple[int, str]],
    attached: set[str],
) -> list[tuple[str, str]]:
    """顿号并列项（item）级的否决作用域判定 ⇒ [(代码, 名称)]。

    🔴 2026-11-11 修（真误伤 · 真实交付件 `20261111_122825`）：
       「，」「；」切出的**子句**把一串并列项合并成**同一个子句**
       ⇒ 子句**末尾**的否决动词（`不买`）**反向污染**前面的标的
       ⇒ TA 明写「条件性买入」的 515450 / 512170 被误判成否决。
    ⇒ 现在子句再按顿号切成 item：
       ① **R1 同项内**：动词与代码**同一 item** 且在 `_REJECT_WINDOW` 内 ⇒ 否决；
       ② **R2 头作用域**：动词在**本项第一个代码之前** ⇒ 管辖本项及其后**所有**项；
       ③ **R3 尾作用域**：动词在**本项最后一个代码之后** ⇒ 管辖本项及其前**所有**项；
       ④ R2/R3 **只管辖「没有自带动作」的项**（`只 / 仅 / 保留 / 减到 …` ⇒ 自成一体）。
    """
    spans = _item_spans(clause, cstart)
    if not spans:
        return []
    # 🔴 2026-11-11 真实交付件：`563020 … 但弱于同族 515450（相关性 0.917）绝不建仓`
    #    ⇒ 515450 是**比较提及**（不是裁决对象）⇒ 不作否决主体、不参与作用域。
    metric_refs = {m.group("code") for m in _MD_REF_METRIC_RE.finditer(clause)}
    subjects = [(cpos, code) for cpos, code in subjects if code not in metric_refs]
    item_codes: list[list[tuple[int, str]]] = [[] for _ in spans]
    for cpos, code in subjects:
        for idx, (start, end) in enumerate(spans):
            if start <= cpos < end:
                item_codes[idx].append((cpos, code))
                break
    item_verbs: list[list[tuple[int, str]]] = [[] for _ in spans]
    for vpos, word in verbs:
        for idx, (start, end) in enumerate(spans):
            if start <= vpos < end:
                item_verbs[idx].append((vpos, word))
                break
    item_action = [
        _own_action(clause[start - cstart:end - cstart]) for start, end in spans
    ]

    hits: dict[str, tuple[str, str]] = {}

    def _mark(cpos: int, code: str) -> None:
        if code in attached or code in hits:
            return
        hits[code] = (code, _name_near(sentence, code, cpos))

    # ① R1 同项内
    for idx in range(len(spans)):
        for cpos, code in item_codes[idx]:
            for vpos, _word in item_verbs[idx]:
                if abs(vpos - cpos) > _REJECT_WINDOW:
                    continue
                _mark(cpos, code)
                break

    # ② R2 头作用域 / ③ R3 尾作用域
    count = len(spans)
    for idx in range(count):
        for vpos, _word in item_verbs[idx]:
            scope: list[int] = []
            if item_codes[idx]:
                first = item_codes[idx][0][0]
                last = item_codes[idx][-1][0]
                if vpos < first:
                    scope = list(range(idx, count))        # 头作用域：本项及其后
                elif vpos > last:
                    scope = list(range(0, idx + 1))        # 尾作用域：本项及其前
            else:
                # 本项没有代码：动词管辖它「越过」的项（前后都算）
                for j in range(count):
                    if j == idx or not item_codes[j]:
                        continue
                    if j < idx and item_codes[j][-1][0] < vpos:
                        scope.append(j)
                    elif j > idx and item_codes[j][0][0] > vpos:
                        scope.append(j)
            for j in scope:
                if item_action[j]:
                    continue                               # 自带动作 ⇒ 外部动词不管辖
                for cpos, code in item_codes[j]:
                    _mark(cpos, code)

    return list(hits.values())


def _hypothetical_verb_positions(
    clause: str, verbs: list[tuple[int, str]]
) -> set[int]:
    """本子句里**被让步/假设连词管辖**的否决动词位置（⇒ 这些动词**不是本轮裁决**）。

    🔴 2026-11-11（真误伤 · G-TA1）：「即使否决 515450，它也不能作为顺延者」
       里的「否决 515450」是**假设**（讲的是顺延资格），却被当成**真否决**
       ⇒ 把规则层真实入选、TA 权威表写「买（条件性）」的 515450 踢出计划。

    判定口径（**紧邻**才认）：让步词与该动词之间**只允许**空白/强调标记
    ＋ **可选**一个 6 位代码 ＋ **可选**被动字 ⇒
      ① 「即使否决 515450」      ⇒ 动词紧跟让步词 ⇒ **假设**；
      ② 「即使 515450 被否决」   ⇒ 代码在让步词与动词之间 ⇒ **假设**；
      ③ 「即使拥挤度 92% 也不买 515650」 ⇒ 中间夹了内容 ⇒ **真否决**（不豁免）。
    ⇒ 只豁免**紧邻的那一个**动词；同句里后面的动词（如「…也不买 563020」）不受影响。
    """
    if not verbs:
        return set()
    positions: set[int] = set()
    for marker in _HYPOTHETICAL_MARKERS:
        for m in re.finditer(re.escape(marker), clause):
            gap = _HYPOTHETICAL_GAP_RE.match(clause, m.end())
            if gap is None:
                continue
            target = gap.end()
            for vpos, _word in verbs:
                if vpos == target:
                    positions.add(vpos)
                    break
    return positions


def _codes_with_own_action(
    clause: str, cstart: int, subjects: list[tuple[int, str]]
) -> set[str]:
    """本子句里**自带动作**（买/卖/减/保留/持有/配置…）的代码集合。

    🔴 2026-11-11（真误伤 · 每日链快照 `20261111_142244`）：
       「`**`512800`卖出超配部分、保留16%核心；`515450`复核通过后买入；……；
         弱信号、拥挤及同族重复候选否决；……」
       ⇒ 末句的「否决」是**泛指**（没有自己的代码）⇒ 旧口径**继承上一子句的代码**
       ⇒ 把 512800（TA 明写「减，保留」）与 515450（TA 明写「买」）一起判成**否决** ✗✗
       ⇒ 后果：TA 要买的 515450 被踢出计划 ⇒ 席位顺延给别的标的。
    ⇒ 口径：代码在**本子句**里自带动作 ⇒ 它**已有裁决** ⇒ 不许被下一子句继承成否决主体。
    """
    if not subjects:
        return set()
    out: set[str] = set()
    for start, end in _item_spans(clause, cstart):
        text = clause[start - cstart:end - cstart]
        if not _has_own_decision(text):
            continue
        for m in _CODE_RE.finditer(text):
            out.add(m.group(1))
    return out


#: 🔴 2026-11-11（真误伤 · 第 5 处根因）**第三方立场的「否决」元指代**
#: ------------------------------------------------------------------
#: 「否决权属于**对方/另一方**」的措辞，说的**不是**「否决子句里那只标的」：
#:   * 「第一，**熊方的否决**拿不出更优替代——515020/516310/512820 与 512800 成分重叠 …」
#:     ⇒ 512800 是**被保留**的那一只（研究经理明写「持 / 减至目标权重」），
#:       却被旧口径当成「被否决」⇒ 计划把 512800 变成「维持」席位、515450 整个消失。
#:   * 「不下调到 0（**否决熊方**）」⇒ 同一句话的动词后置写法。
#: ⇒ 命中这些写法的否决动词**不否决**子句里的任何代码。
#: 🔴 2026-11-13（真 bug · G-TA1 产物级闸门实证）：「第三方立场 + **对某只标的**的否决」
#:   —— 真机原文：`正因如此我拒绝熊方对 515650 的否决：…`
#:   ⇒ 旧正则只认「熊方的否决」⇒ 夹在中间的「对 515650」让它**匹配不上**
#:   ⇒ 「否决」动词被当成本轮的裁决 ⇒ 515650 被写进「TA 一票否决」名单。
_META_VETO_RE = re.compile(
    r"(?:熊方|牛方|空方|多头|空头|对方|对手方|多方|空方)"
    r"\s*(?:对|针对|给)?\s*(?:\d{6}\s*)?的?\s*(?:一票\s*)?否决"
    r"|否决\s*(?:熊方|牛方|空方|多头|空头|对方|对手方|多方|空方)"
)

#: 🔴 2026-11-13（真 bug · 交付件 `20261113_092926`）**「驳回否决」的元结构**
#: ------------------------------------------------------------------
#: 否决动词的**宾语是「某方的否决」而不是「某只标的」**：
#:   「正因如此我**拒绝熊方对 515650 的否决**：515650 的 E=+0.3401 是全部非银行候选里最高的」
#:   ⇒ 说话人（研究经理）**驳回**了「否决 515650」这件事，并**买入** 515650。
#:   ⇒ 旧口径：`否决` 与 `515650` 落在**同一子句** ⇒ 命中 ⇒ 515650 进「TA 一票否决」名单
#:      ⇒ 否决出处还落在**研究经理**（层级 10）⇒ 与研究经理自己的买侧指令（层级 10）撞车
#:      ⇒ G-TA1「否决名单里不许有买侧标的」红灯。
#:   ⇒ 修法：`(拒绝|不接受|不同意|反对|不采纳)` + (第三方立场/介词) + CODE + 的 + (一票)?否决
#:      ⇒ 命中 ⇒ 这段里的**否决动词与「拒绝」本身一并作废**（它驳回的是「否决这件事」）。
#:   ⚠️ 口径**只认「CODE + 的 + 否决」**这一种宾语结构（`拒绝 512800 并否决 515450`
#:      这种真否决不受影响；「拒绝」后面**不跟**「的+否决」时**不触发**）。
_REFUSED_VETO_RE = re.compile(
    r"(?:拒绝|不接受|不同意|反对|不采纳|不予接受)"
    r"[^，；。｜|\n]{0,16}?"
    r"(?P<code>[1-9]\d{5})\s*的?\s*(?:一票\s*)?否决"
)


#: 🔴 2026-11-13（真 bug · 交付件 `20261113_092926`）**否决动词的「名词用法」**
#: ------------------------------------------------------------------
#: 「只可**作否决的补充理由**——正因如此我拒绝熊方对 515650 的否决：…」
#:   ⇒ 第一个「否决」是**名词**（"作……的理由"里的宾语），不是「否决某只标的」的动词
#:   ⇒ 旧口径把它当成裁决动词 ⇒ 同子句里的 515650 被判成被否决。
#:   ⇒ 修法：`作 / 作为 / 当成 / 当作` + (可选「一票」) + `否决` ⇒ 该「否决」是名词
#:      ⇒ 动词位置作废（**不影响**「正式否决 515450」这类真裁决）。
_REJECT_NOUN_RE = re.compile(r"(?:作|作为|当成|当作)\s*(?:一票\s*)?否决")


def _noun_veto_positions(clause: str, verbs: list[tuple[int, str]]) -> set[int]:
    """「作/作为 + 否决」这类**名词用法**里的否决动词位置 ⇒ 不作裁决。"""
    out: set[int] = set()
    for match in _REJECT_NOUN_RE.finditer(clause):
        for pos, _word in verbs:
            if match.start() <= pos < match.end():
                out.add(pos)
    return out


def _strip_noun_veto(text: str) -> str:
    """抹掉「作/作为 + 否决」的名词用法。"""
    return _REJECT_NOUN_RE.sub(" ", text or "")


def _refused_veto_positions(clause: str, verbs: list[tuple[int, str]]) -> set[int]:
    """「驳回否决」元结构里（`我拒绝熊方对 515650 的否决`）的否决动词位置。

    ⇒ 这些动词**不否决**该代码（说话人驳回的是「否决这件事」，不是「否决这只标的」）。
    """
    out: set[int] = set()
    for match in _REFUSED_VETO_RE.finditer(clause):
        for pos, _word in verbs:
            if match.start() <= pos < match.end():
                out.add(pos)
    return out


def _strip_refused_veto(text: str) -> str:
    """抹掉「驳回否决」的元结构（`我拒绝熊方对 515650 的否决`）。"""
    return _REFUSED_VETO_RE.sub(" ", text or "")


def _span_code_positions(clause: str, pattern: re.Pattern) -> set[int]:
    """落在 pattern 命中区间内的 6 位代码**位置**（子句内相对下标）。

    🔴 **按位置**剔除（不是按代码）：同一子句里同一个代码可能既出现在元结构里
       （`我拒绝熊方对 515650 的否决`）、又出现在**真正的裁决**里
       （`…的否决不成立，正式否决 515650`）⇒ 按代码剔除会把真否决一起丢掉。
    """
    out: set[int] = set()
    for match in pattern.finditer(clause):
        for code_match in _CODE_RE.finditer(match.group(0)):
            out.add(match.start() + code_match.start())
    return out

#: 🔴 2026-11-11（真缺陷 · 第 5 处根因）**点名裁决条目**
#: ------------------------------------------------------------------
#: 权威节里**逐只点名**给裁决的条目：
#:   `1) 512800（银行ETF华宝）：方向「持 / 减至目标权重」…`
#:   `2) 515450（红利低波50ETF南方）：建仓至标准槽位 16%…`
#:   `4) 516620（影视ETF国泰）：**正式一票否决，不买**…`
#: ⇒ 命中 ⇒ 该代码**本轮有一句话的直接裁决**
#: ⇒ **散文推断不得推翻它**（旧口径没有这一层 ⇒ 散文里的一句元指代就能把它推翻）。
#:    🔴 **只认编号条目**（`1) 512800（…）：…`）：项目符号条目（`- **515450**：…`）**太弱** ——
#:    决策件里 `- \`515450\`若未通过12月1日复核，则不买…` 是**条件句的风险披露**，
#:    不是「这一只本轮被否」的裁决 ⇒ 把弱条目当裁决会**凭空造出否决**（实测造出 515450）。
_EXPLICIT_ITEM_RE = re.compile(
    r"^[ \t]*\d+[)）][ \t]*[*_`]*([0-9]{6})[*_`]*"
)

def _explicit_item_verdicts(text: str) -> dict[str, tuple[str, int]]:
    """权威节里**逐只点名**的裁决条目 ⇒ ``{代码: (条目原文, 条目起点偏移)}``。

    🔴 只认**行首**的编号条目（`1) 512800（…）：…`）与项目符号条目（`- **512800**：…`），
       ⇒ 正文里顺带提到的代码不会被误当成「点名裁决」。
    """
    out: dict[str, tuple[str, int]] = {}
    offset = 0
    for line in (text or "").splitlines(keepends=True):
        stripped = line.strip()
        match = _EXPLICIT_ITEM_RE.match(line)
        if match:
            out.setdefault(match.group(1), (stripped, offset))
        offset += len(line)
    return out


#: 🔴 2026-11-11（真误伤 · 第 5 处根因续）**被弱化的否决**
#: ------------------------------------------------------------------
#: 「对 `512800` 本体的一票否决**理由不足**」⇒ 这条**否决不成立**（研究经理明确否掉
#: 空头对 512800 的否决）⇒ 旧口径把「一票否决」当否定词才躲过，这次把假护栏拆掉后
#: 立刻暴露 ⇒ 必须补一条**弱化限定词**护栏。
_VETO_WEAKENED_MARKERS: tuple[str, ...] = (
    "理由不足", "理由不成立", "理由不充分", "理由欠缺", "不成立", "站不住",
    "不予采纳", "未被采纳", "不获采纳", "有误", "不成立过",
)

#: 弱化限定词与否决动词之间允许的最大距离（字）。
_VETO_WEAKEN_WINDOW = 12


def _weakened_veto_positions(clause: str, verbs: list[tuple[int, str]]) -> set[int]:
    """被**弱化限定词**跟在后面的否决动词位置（`一票否决理由不足` ⇒ 这条否决不成立）。"""
    out: set[int] = set()
    for pos, word in verbs:
        tail = clause[pos + len(word): pos + len(word) + _VETO_WEAKEN_WINDOW]
        if any(marker in tail for marker in _VETO_WEAKENED_MARKERS):
            out.add(pos)
    return out


def _meta_veto_positions(clause: str, verbs: list[tuple[int, str]]) -> set[int]:
    """第三方立场元指代（`熊方的否决` / `否决熊方`）里的否决动词位置。

    ⇒ 这些动词**不否决**子句里的任何代码（否则「被保留的那只」会被当成被否决）。
    """
    out: set[int] = set()
    for match in _META_VETO_RE.finditer(clause):
        for pos, _word in verbs:
            if match.start() <= pos < match.end():
                out.add(pos)
    return out


def _strip_meta_veto(text: str) -> str:
    """抹掉「第三方立场的否决」元指代（`熊方的否决` / `否决熊方`）。"""
    return _META_VETO_RE.sub(" ", text or "")


def _explicit_verdict_rejects(verdict: str) -> bool:
    """点名裁决条目 ⇒ 该代码本轮**是不是被一票否决**。

    🔴 判定口径：
       ① 先抹掉第三方立场的元指代（`熊方的否决` / `否决熊方`）；
       ② 再抹掉否定式否决（`不否决` / `而不是…否决` …）；
       ③ 剩下的文本里若还有**否决动词**（`_REJECT_VERBS` 全表：否决/不买/不建仓/
          放弃/暂缓/回避/不配置…）⇒ **真否决**；
          一个都没有 ⇒ **不是否决**（这一只本轮另有裁决：买 / 减 / 持 …）。

    🔴 动词表**刻意不含**「清仓 / 卖出 / 退出」—— 那是「非目标持仓清仓」的正常动作，
       不是**一票否决**（进了否决名单就会变成「维持」席位 ⇒ 卖不掉）。
    """
    # 🔴 2026-11-13（真 bug · G-TA1）：**先**抹「驳回否决」元结构
    #    （`我拒绝熊方对 515650 的否决`）⇒ 否则它剩下的「拒绝」仍会被当成否决动词
    #    ⇒ 而「拒绝」的宾语是「否决这件事」，不是「这只标的」。
    cleaned = _strip_refused_veto(verdict)
    cleaned = _strip_meta_veto(cleaned)
    # 🔴 2026-11-13（真 bug · G-TA1）：「作/作为 + 否决」是**名词用法** ⇒ 一并抹掉。
    cleaned = _strip_noun_veto(cleaned)
    for neg in _REJECT_NEGATIONS:
        cleaned = cleaned.replace(neg, " ")
    cleaned = _REJECT_NEGATION_RE.sub(" ", cleaned)
    return bool(_reject_verb_hits(cleaned))


#: 「自带**裁决**」的动作词（比 `_ITEM_ACTION_WORDS` 窄）：
#:    **只/仅** 是弱限定词（「只用 515450 表达足够」= 表达方式，不是对 515450 的裁决）
#:    ⇒ 不算自带裁决（否则会与「比较/工具/避免引用」护栏职责重叠、互相顶掉）。
_OWN_DECISION_WORDS: tuple[str, ...] = tuple(
    w for w in _ITEM_ACTION_WORDS if w not in ("只", "仅")
)


def _has_own_decision(text: str) -> bool:
    """本并列项里**去掉否决动词之后**是否还有「自带裁决」的动作词。

    🔴 与 `_own_action` 的区别：只认**真裁决词**（买/卖/减/保留/持有/配置…），
       **不认「只」「仅」** ⇒ 「红利敞口只用 515450 表达足够」不构成对 515450 的裁决
       ⇒ 该项仍可被「比较/工具/避免引用」护栏保护（职责不重叠）。
    """
    rest = list(text)
    for pos, word in _reject_verb_hits(text):
        for offset in range(pos, pos + len(word)):
            rest[offset] = " "
    tail = "".join(rest)
    return any(word in tail for word in _OWN_DECISION_WORDS)

def _prose_hits(sentence: str) -> list[tuple[str, str]]:
    """散文句里的否决命中 ⇒ [(代码, 名称)]。

    🔴 **判定粒度 = 子句**（动作词与代码必须落在同一子句，或代码写在上一子句）。
    🔴 **子句内再按顿号切成并列项（item）**：否决动词只管它**自己那一项**
       （+ 头/尾作用域里**没有自带动作**的项）⇒ 见 `_item_scope_hits`。
    🔴 **条件句豁免**：复用既有 `_is_conditional`（条件句里的动作不是本轮裁决）。
    🔴 **买入意图豁免**：整句出现「条件买入 / 若…则买入 / 条件满足」⇒ 不算否决。
    🔴 **让步/假设豁免**：「即使否决 X」这类**假设**里的否决动词不作数
       （⇒ 假设子句里的代码也不许**继承**给下一子句）⇒ 见 `_hypothetical_verb_positions`。
    """
    if _is_conditional(sentence):
        return []
    has_intent, attached = _buy_intent(sentence)
    if has_intent and not attached:
        return []
    out: list[tuple[str, str]] = []
    prev_codes: list[tuple[int, str]] = []
    for cstart, clause in _clauses(sentence):
        if _guarded(clause):
            continue
        own_codes = [(cstart + m.start(), m.group(1)) for m in _CODE_RE.finditer(clause)]
        clause_verbs = _reject_verb_hits(clause)
        # 🔴 2026-11-11（真误伤 · G-TA1）：**让步/假设连词**管辖的否决动词剔除
        #    （「即使否决 515450」⇒ 假设 ⇒ 不判否决）。
        hypo_local = _hypothetical_verb_positions(clause, clause_verbs)
        # 🔴 2026-11-11（真误伤 · 第 5 处根因）：「**熊方的否决**」这类
        #    **第三方立场**的元指代 ⇒ 否决权属于对方，**不否决**本子句里任何代码
        #    （否则「研究经理明写『持 / 减至目标权重』的 512800」会被当成被否决）。
        meta_local = _meta_veto_positions(clause, clause_verbs)
        # 🔴 2026-11-11（真误伤 · 第 5 处根因续）：「一票否决**理由不足**」
        #    ⇒ 这条**否决不成立**（权威段明确否掉了对方的否决）⇒ 否决动词作废。
        weak_local = _weakened_veto_positions(clause, clause_verbs)
        # 🔴 2026-11-13（真 bug · G-TA1）：**「驳回否决」**元结构
        #    （`我拒绝熊方对 515650 的否决`）里的否决动词**不否决**该代码
        #    ⇒ 说话人驳回的是「否决这件事」，并且往往**买入**这只标的。
        refused_local = _refused_veto_positions(clause, clause_verbs)
        # 🔴 2026-11-13（真 bug · G-TA1）：「只可**作否决的补充理由**」里的「否决」
        #    是**名词**（宾语），不是裁决动词 ⇒ 位置作废。
        noun_local = _noun_veto_positions(clause, clause_verbs)
        verbs = [
            (cstart + pos, word)
            for pos, word in clause_verbs
            if pos not in hypo_local and pos not in meta_local
            and pos not in weak_local and pos not in refused_local
            and pos not in noun_local
        ]
        # 🔴 2026-11-11 修（真误伤）：**比较引用 / 工具引用**里的代码
        #    （「与 515450 相关性 0.917」「只用 515450 表达足够」）
        #    **不是本句的裁决对象** ⇒ 不得当成否决主体，也不得被下一子句继承。
        _refs = _reference_codes(clause)
        # 🔴 2026-11-13（真 bug · G-TA1）：**元结构里的代码不是裁决对象**
        #    （`我拒绝熊方对 515650 的否决` 里的 515650 是「被驳回的否决」的宾语）
        #    ⇒ 按**位置**剔除（同一子句里别处的同代码裁决不受影响）。
        _meta_positions = (_span_code_positions(clause, _REFUSED_VETO_RE)
                           | _span_code_positions(clause, _META_VETO_RE))
        subjects = [(cpos, code) for cpos, code in own_codes
                    if code not in _refs and (cpos - cstart) not in _meta_positions]
        if verbs:
            if subjects:
                # 🔴 2026-11-11 修（真误伤）：子句内按顿号切成**并列项** ⇒
                #    否决动词**只管它自己那一项**（头/尾作用域另算），
                #    不再**反向污染**同一子句里前面那些自带动作的标的。
                out.extend(
                    _item_scope_hits(sentence, clause, cstart, subjects, verbs, attached))
            else:
                #    ⇒ 只剩引用代码时 ⇒ 回落到上一子句继承的代码（无则本句不判否决）。
                for cpos, code in prev_codes:
                    if code in attached:
                        continue
                    for vpos, word in verbs:
                        if abs(vpos - cpos) > _REJECT_WINDOW:
                            continue
                        # 🔴 2026-11-11 修（真误伤）：**弱**否决动词 + **跨子句继承**的代码
                        #    ⇒ 不判否决（「放弃」多用于泛化陈述，不是点名否决某只标的）。
                        if word in _REJECT_VERBS_WEAK:
                            continue
                        out.append((code, _name_near(sentence, code, cpos)))
                        break
        if own_codes:
            # 🔴 2026-11-11 审计修：前子句若带**持有语义**（保留/持有/维持…）
            #    ⇒ **不把它的代码继承给下一子句**。
            #    反例：`512800 保留本仓，其余不考虑。` ⇒ 旧口径把 512800 判成否决。
            # 🔴 2026-11-11 修（真误伤）：继承的必须是**主体代码**，
            #    **不能**把「与 515450 相关性…」「只用 515450 表达…」里的**引用**代码传下去
            #    ⇒ 否则下一个子句的「不建仓」还是会落到 515450 头上。
            # 🔴 2026-11-11（真误伤 · G-TA1）：**假设子句**里的代码也不许继承给
            #    下一子句（否则下一子句的「不建仓」还是会落到假设对象头上）。
            # 🔴 2026-11-11（真误伤 · 每日链快照 `20261111_142244`）：
            #    「……**`512800`卖出超配部分、保留16%核心；`515450`复核通过后买入；……；
            #      弱信号、拥挤及同族重复候选否决；……」
            #    ⇒ 「弱信号……否决」是**泛指**（没有自己的代码）⇒ 旧口径**继承上一子句**
            #      的代码 ⇒ 把 512800 / 515450 判成否决 ✗✗
            #      （而同一份决策 md 的方向表明写「减，保留」与「买」）
            #    ⇒ 规则：代码在本子句里**自带动作**（买/卖/减/保留/持有…）
            #      ⇒ 它**已有裁决** ⇒ 不许被下一子句继承成否决主体。
            spoken = _codes_with_own_action(clause, cstart, subjects)
            # 🔴 2026-11-13（真误伤 · 交付件 `20261113_092926`）：**比分引述**
            #    （「医药类 512170 -1.3950、512010 -1.4778」）里的代码**不是裁决主体**
            #    ⇒ 不许**跨子句继承**成下一子句「否决它等于…」的主体
            #    ⇒ 否则一只没被点名否决的标的会被写进「TA 一票否决」名单。
            #    ⇒ 只收窄**继承**通道；子句内 R1/R2/R3 的判定口径一字未动。
            _cited = {m.group("code") for m in _MD_DATA_CITATION_RE.finditer(clause)}
            prev_codes = ([] if (_keep_meaning(clause) or hypo_local) else
                         [(cpos, code) for cpos, code in subjects
                          if code not in spoken and code not in _cited])
    return out


def _row_hits(row: str) -> list[tuple[str, str]]:
    """表格行的否决命中 ⇒ [(代码, 名称)]。

    🔴 2026-11-11 修：**整行判定**（不再按「；」把单元格切碎）⇒
       `| 515450 | 当前不买；条件买入 | 重跑后最多10% |` 里的「条件买入」看得见。
       动作词取**动作格**（`方向/动作` 那一格），代码取**代码格** ⇒
       「核心理由」格里顺带提到的别的代码**不**跟着被否。
    """
    cells = _table_cells(row)
    code_idx = -1
    code_list: list[str] = []
    for idx, cell in enumerate(cells):
        found = [m.group(1) for m in _CODE_RE.finditer(cell)]
        if found:
            code_idx, code_list = idx, found
            break
    if code_idx < 0:
        return []
    action_idx = -1
    for idx in range(code_idx, len(cells)):
        if _reject_verb_hits(cells[idx]):
            action_idx = idx
            break
    if action_idx < 0:
        return []
    action = cells[action_idx]
    if _guarded(action):
        return []
    has_intent, attached = _buy_intent(row)
    if has_intent and not attached:
        return []
    codes = list(dict.fromkeys(
        code_list + [m.group(1) for m in _CODE_RE.finditer(action)]))
    codes = [c for c in codes if c not in attached]
    if _positive_weight(cells, code_idx):
        # 🔴 本行目标权重 > 0 ⇒ **本行自己的代码**不是否决（买行）。
        #    但**动作格里别的代码**照常判（`| 563020 | 买 | 8% | 不买 512170 |`
        #    ⇒ 563020 豁免、512170 仍被否）。
        codes = [c for c in codes if c not in code_list]
    if not codes:
        return []
    name = ""
    if code_idx + 1 < len(cells) and _is_name_like(cells[code_idx + 1]):
        name = cells[code_idx + 1]
    return [(c, name) for c in codes]


def _iter_units(text: str):
    """把文本切成判定单元 ⇒ (kind, 起始偏移, 单元文本)。

    - `row`：表格行（**整行**，不切）；
    - `prose`：散文句（按「。」切；**不**按「；」切 —— 「；」只是子句分隔）。
    """
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("|"):
            yield ("row", pos, line)
        else:
            start = 0
            for idx, ch in enumerate(line):
                if ch == "。":
                    seg = line[start:idx]
                    if seg.strip():
                        yield ("prose", pos + start, seg)
                    start = idx + 1
            tail = line[start:]
            if tail.strip():
                yield ("prose", pos + start, tail)
        pos += len(line)


#: 🔴 2026-11-13（真 bug · G-TA1 产物级闸门实证）**权威段落的层级**
#: ------------------------------------------------------------------
#: 决策 md 里同一个人/同一层可能**先后**改口：
#:   研究经理（10）买 515650 → 辩论裁决（20）驳回熊方的否决 → 风险裁决（20）改为否决
#:   → 最终交易决定（40）确认否决。
#: ⇒ 「**约束力最强**的那一处」才是生效的裁决 ⇒ 否决的**出处章节**必须记**最高层级**
#:   （否则交付件记成「研究经理」，与研究经理自己的买侧指令撞车 ⇒ G-TA1 误判红灯）。
#: ⚠️ 本表与 `probes/probe_verify_guards.py::_AUTHORITY_RANKS` **必须逐字一致**
#:    （`tests/test_llm_final_veto_authority_attribution.py` 有漂移守卫）。
_AUTHORITY_RANK_MARKERS: tuple[tuple[str, int], ...] = (
    ("最终交易决定", 40),
    ("Trader", 30),
    ("辩论裁决", 20),
    ("风险裁决", 20),
    ("研究经理", 10),
    ("Research Manager", 10),
)


def _authority_rank(section: object) -> int:
    """章节标题 ⇒ 权威层级（认不出来 ⇒ 0 = 观点层/未知）。"""
    text = str(section or "").strip()
    if not text:
        return 0
    for marker, rank in _AUTHORITY_RANK_MARKERS:
        if marker in text:
            return rank
    return 0


def extract_rejections(text: str) -> list[dict]:
    """抽出「TA 点名否决 / 剔除某只标的」的条目（合规筛选：否决 + 顺延）。

    返回：``[{"code","name","reason","source_section"}]``

    🔴 **保守判定（宁漏勿误）**
    1. **判定单元**：表格行按**整行**判；散文按**句**判、动作落到**子句**判；
    2. 单元内出现 **6 位 ETF 代码**（复用 `_CODE_RE` ⇒ 首位非 0，
       且不会被「0.006126」这类小数误伤）；
    3. 同子句（代码写在上一子句也算）出现剔除类动词 ⇒ 候选；
    4. **四道豁免**（任一命中 ⇒ 不是否决）：
       ① **条件句**（复用既有 `_is_conditional`）；
       ② **买入意图**（条件买入 / 若…则买入 / 条件满足 / 视情况买入）；
       ③ **目标权重 > 0%**（表格行）；
       ④ **反向语义护栏**（否决权 / 不否决 / 若否决 …，**子句级**）与
          **能力陈述**（必要时 / 任一 …）。

    🔴 `reason` = 命中单元原文（截断 ≤ 200 字）；`source_section` = 所在章节名。
    🔴 **不动既有语义**：`extract_final_decision` 等函数一字未改。
    """
    out: list[dict] = []
    if not text:
        return out
    headings = _heading_spans(text)
    seen: set[str] = set()
    rescued: set[str] = set()
    # 🔴 2026-11-11（真缺陷 · 第 5 处根因）：**点名裁决优先**
    #    权威节里逐只点名的条目（`1) 512800（…）：方向「持 / 减至目标权重」…`）
    #    = 这一只**本轮的直接裁决** ⇒ 散文推断**不得推翻**它：
    #      · 条目说买/减/持 ⇒ 这只**不是**被一票否决的（⇒ 不得进否决名单）
    #      · 条目说否决/不买/不建仓 ⇒ 这只**必须**进否决名单（⇒ 不许漏放）
    explicit = _explicit_item_verdicts(text)
    # 🔴 2026-11-13（真 bug · G-TA1）：否决**出处章节**取**权威层级最高**的那一处
    #    （同层级保留先出现的那条 ⇒ 与既有语义一致）。
    #    ⇒ 一只标的在「研究经理（10）→ 辩论裁决（20）→ 风险裁决（20）→ 最终交易决定（40）」
    #      多段都被否决时，交付件记的是**约束力最强**的那一段
    #      ⇒ 闸门才能拿「否决层级 vs 买侧层级」判出真矛盾。
    by_code: dict[str, dict] = {}
    for kind, start, unit in _iter_units(text):
        hits = _row_hits(unit) if kind == "row" else _prose_hits(unit)
        reason = " ".join(unit.split())[:_REJECT_REASON_MAX]
        section = _section_at(headings, start)
        rank = _authority_rank(section)
        for code, name in hits:
            if code in seen:
                prev = by_code[code]
                if rank > _authority_rank(prev.get("source_section")):
                    by_code[code] = {
                        "code": code, "name": name, "reason": reason,
                        "source_section": section,
                    }
                continue
            if code in rescued:
                continue
            verdict = explicit.get(code)
            if verdict is not None and not _explicit_verdict_rejects(verdict[0]):
                rescued.add(code)          # 有点名裁决且不是否决 ⇒ 永久豁免
                continue
            seen.add(code)
            by_code[code] = {
                "code": code,
                "name": name,
                "reason": reason,
                "source_section": section,
            }
    out.extend(by_code.values())
    # 🔴 点名条目里明确写了「否决 / 不买 / 不建仓」⇒ 补抓（散文漏放时兜底）
    for code, (verdict, offset) in explicit.items():
        if code in seen or code in rescued:
            continue
        if not _explicit_verdict_rejects(verdict):
            continue
        seen.add(code)
        out.append({
            "code": code,
            "name": _name_near(verdict, code, verdict.find(code)),
            "reason": " ".join(verdict.split())[:_REJECT_REASON_MAX],
            "source_section": _section_at(headings, offset),
        })
    return out


#: 🔴 2026-11-11（真机实跑 · 一票否决**作用域**修复）
#: ------------------------------------------------------------------
#: **否决（一票否决）只认「权威裁决段落」；分析师观点段落没有否决权。**
#:
#: **实测事故（2026-11-11 真机跑批）**：`_decision_text()` 把决策 JSON 的 **7 个段落**
#: （研究经理 / 多头 / 空头 / 激进派 / 保守派 / 中立派 / 最终交易决定）拼成一段文本，
#: `extract_rejections` 扫**全文** ⇒ 把**观点**当**否决**：
#:
#:   * `investment_debate.bear_history`（**空头**观点）含
#:     「暂不买入 `515450`、`515650`、…」⇒ 515450 / 515650 被误判成否决
#:     （TA 的**最终裁决**明确「买 515450 8% / 买 515650 6%」）；
#:   * `risk_debate.aggressive_history`（**激进派**观点）含
#:     「清仓512800，等于放弃…」⇒ 512800 被误判成否决
#:     （TA 的最终裁决是「**减**至 10%，硬上限 12%」＝减仓指令，不是不建仓）。
#:
#: ⇒ 真实否决集应是 **13 只**（0 仓观察那批），实跑却得到 **16 只**（多 3 只误伤）。
#:
#: **修法**：否决解析**只喂权威段落**（本表的 5 段）⇒ 实测恰好 13 只、零误伤。
#: 观点段落仍照旧喂给**冲突表/目标权重**解析（那里**必须**保留各方声音）。
AUTHORITY_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("研究经理", ("investment_plan",)),
    ("辩论裁决", ("investment_debate", "judge_decision")),
    ("风险裁决", ("risk_debate", "judge_decision")),
    ("Trader 交易计划", ("trader_investment_plan",)),
    ("最终交易决定", ("final_trade_decision",)),
)

#: `.md`（展示件）**兜底**时保留的章节标题关键词。
#: 🔴 展示件里「多空辩论要点」「风险辩论」是**观点**段 ⇒ 一律不保留。
#: 🔴 2026-11-11 审计修：还要保留 **裁决子章节**
#:    （`### 辩论裁决` / `### 风险裁决` / `### 1. …基准调仓表` / `### 五、最终调仓方向表`）
#:    —— 它们是 JSON 路径里 `AUTHORITY_SECTIONS` 对应内容的**展示版**。
_AUTHORITY_MD_MARKERS: tuple[str, ...] = (
    "研究经理", "Trader", "最终交易决定",
    "辩论裁决", "风险裁决", "基准调仓表", "最终调仓方向表",
)


def _dig(payload: dict, key_path: tuple[str, ...]) -> str:
    """按路径取嵌套字符串（任一层缺失/类型不对 ⇒ 空串）。"""
    cur: object = payload
    for key in key_path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return cur if isinstance(cur, str) else ""


def _authority_md_text(text: str) -> str:
    """从 `.md` 展示件里**只保留权威章节**（含其子章节）。

    🔴 2026-11-11 审计修（发现 3 · 高）：旧口径只按 `## ` 一级标题切块 ⇒
       `### 辩论裁决` / `### 风险裁决` 落在被丢弃的 `## 三、多空辩论要点`
       / `## 四、风险辩论` 块里**一起被丢掉** ⇒ **JSON 缺失时漏判真否决**
       ⇒ 与 JSON 路径口径不一致（JSON 路径 5 段，md 路径只 3 段）。
    ⇒ 现在按 **markdown 标准章节语义**切块：标题层级 + 「下一个同级或更高级标题」收尾
       ⇒ 权威标题命中即保留**整个章节（含子章节）**。

    🔴 观点章节（多头/空头/激进派/保守派/中立派）**一律不保留** ⇒ 观点没有否决权。
    """
    lines = text.splitlines()
    heads: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            heads.append((idx, len(m.group(1)), m.group(2).strip()))
    if not heads:
        return ""
    keep_idx: set[int] = set()
    for pos, (start, level, title) in enumerate(heads):
        if not any(marker in title for marker in _AUTHORITY_MD_MARKERS):
            continue
        end = len(lines)
        for n_start, n_level, _t in heads[pos + 1:]:
            if n_level <= level:
                end = n_start
                break
        keep_idx.update(range(start, end))
    return "\n".join(line for i, line in enumerate(lines) if i in keep_idx)


def extract_authority_rejections(decision_path: str | Path | None) -> list[dict]:
    """🔴 **一票否决只从「权威裁决段落」抽**（观点段落没有否决权）。

    🔴 **为什么必须有这一层（2026-11-11 真机事故）**
    ------------------------------------------------------------------
    `extract_rejections` 的语义是「保守地抽出点名否决」——**前提是输入里只有裁决**。
    但决策 JSON 同时装了**分析师观点**（多头/空头/激进派/保守派/中立派），
    观点里出现「暂不买入 X」「清仓 X」这类措辞时，会被误判成**最终否决**：

      * 空头观点「暂不买入 `515450`、`515650`」 ⇒ 误伤 2 只（TA 最终裁决是**买**）；
      * 激进派观点「清仓512800，等于放弃…」      ⇒ 误伤 1 只（TA 最终裁决是**减**）。

    ⇒ 本函数**只**把 `AUTHORITY_SECTIONS` 这 5 段拼起来喂给 `extract_rejections`，
    实测真实决策件 ⇒ 恰好 **13 只**、且 **515450 / 515650 / 512800 零误伤**。

    🔴 **兜底**：没有 `.json` 时退到 `.md`，且**只保留**「研究经理 / Trader /
    最终交易决定」三个章节（观点章节仍丢弃）。
    🔴 **不改既有语义**：`extract_rejections` / `extract_final_decision` 一字未改。
    """
    if not decision_path:
        return []
    path = Path(decision_path)
    json_path = path if path.suffix == ".json" else path.with_suffix(".json")
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            chunks = [
                f"## {title}\n{body}"
                for title, key_path in AUTHORITY_SECTIONS
                if (body := _dig(payload, key_path)).strip()
            ]
            if chunks:
                return extract_rejections("\n\n".join(chunks))
    md_path = path if path.suffix == ".md" else path.with_suffix(".md")
    if not md_path.exists():
        return []
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return extract_rejections(_authority_md_text(text))


def load_from_decision(decision_path: str | Path | None) -> dict:
    """从决策文件（MD）抽最终裁决；文件不存在 ⇒ 返回 available=False。

    🔴 **只读 MD**：决策 md 是链路上被 sha1 指纹钉住的那一份 ⇒ 用它做输入 ⇒
    交付件里出现的每一个最终裁决数字都能在**用户能读到的文件**里回放。
    """
    if not decision_path:
        return {"available": False, "note": "未提供决策文件路径", "targets": {},
                "post_trade": {}, "unparsed_codes": []}
    path = Path(decision_path)
    if not path.exists():
        return {"available": False, "note": f"决策文件不存在：{path}", "targets": {},
                "post_trade": {}, "unparsed_codes": []}
    text = path.read_text(encoding="utf-8")
    out = extract_final_decision(text)
    out["source_file"] = str(path)
    return out


def load_from_json(decision_json_path: str | Path | None) -> dict:
    """从决策 JSON 的 `final_trade_decision` 字段抽（更完整，未被 md 截断）。"""
    if not decision_json_path:
        return {"available": False, "targets": {}, "post_trade": {}, "unparsed_codes": []}
    path = Path(decision_json_path)
    if not path.exists():
        return {"available": False, "targets": {}, "post_trade": {}, "unparsed_codes": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "note": f"决策 json 读取失败：{exc}",
                "targets": {}, "post_trade": {}, "unparsed_codes": []}
    text = payload.get("final_trade_decision") or ""
    out = extract_final_decision(text)
    out["source_file"] = str(path)
    out["source_field"] = "final_trade_decision"
    return out
