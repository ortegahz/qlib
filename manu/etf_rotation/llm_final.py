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
3. 只认「**最终交易决定**」章节 ⇒ 不把多头/空头/研究经理的正文混进来；
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
#: 🔴 2026-11-09（Mode C v2 审计实证）：**粗体可有可无** ——
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

#: 🔴 2026-11-09（Mode C v2 审计实证）修：「**已触发**」是**既成事实**，不是条件。
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

_TARGET_PCT_RE = re.compile(
    r"(?:" + _CODE_PCT_NOUNS + r")"
    r"[^，。；]{0,14}?"
    r"(?:降至|下调至|下调到|调至|调到|调整至|调整到|压缩至|压缩到|"
    r"定为|设为|定在|定到|控制在|压到|限定在|降|到|至)"
    r"\s*(?:的\s*)?(?:" + _MEASURE_NOUNS + r")?(?:的\s*)?"
    r"(?:约|大约|约等于|≈)?\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%")

#: 组合层「权益敞口 … 降至 X%」——**不进单只代码槽位**
_PORTFOLIO_PCT_RE = re.compile(
    r"(?:" + _PORTFOLIO_PCT_NOUNS + r")"
    r"[^，。；]{0,14}?"
    r"(?:降至|下调至|下调到|调至|调到|调整至|调整到|压缩至|压缩到|"
    r"定为|设为|定在|定到|控制在|压到|限定在|降|到|至)"
    r"\s*(?:的\s*)?(?:" + _MEASURE_NOUNS + r")?(?:的\s*)?"
    r"(?:约|大约|约等于|≈)?\s*[\*（(]{0,3}\s*(\d+(?:\.\d+)?)\s*%")

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

#: 🔴 2026-11-05 09:3x（M-Q 修）：**条件句**里的数字**不是**最终裁决。
#:    实测：「若512800的E转负但mom120仍为正且未有效跌破MA60，将仓位进一步降至约10%」
#:    ⇒ 这句是**未来触发条件**，却被当成主裁决抽成了 10%。
#:    ⇒ 第 1/2 遍都跳过条件句（句首词 + 首个子句内出现条件词都算）。
_CONDITIONAL_WORDS = ("若", "如果", "假如", "倘若", "一旦", "除非", "只有当", "只有")
#: 「条件买入/条件建仓」是**动作**（不是条件句）⇒ 判条件前先把它抹掉
_COND_BUY_SPAN = re.compile(r"有条件买入|条件买入|条件建仓|有条件建仓")

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
    #    🔴 2026-11-09 实测事故（Mode B 通宵实跑 A20⑥ 红灯）：LLM 写
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
