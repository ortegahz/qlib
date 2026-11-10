"""TradingAgents 冲突表解析器回归探针（只读；退出码 0=通过 / 1=失败）。

【为什么要有这个探针】
2026-11-04 夜 E2E 首跑实测到一类**假冲突**：LLM 的表格表头若**没有**能被识别的
「目标/执行后/调整到」列名（例：表头写「ETF | 权重 | 方向 | 理由」），旧兜底逻辑
「取行内**第 2 个**百分比当目标权重」会命中**理由栏里的句子**：
    | 159929 | **卖清** | 0% | 已被规则列入跌破MA60的强制退出名单；+9.11%的浮盈不能覆盖退出信号 |
    → 旧解析：159929 目标权重 = 9.11%（其实是**浮盈率**）⇒ 交付件写出
      「规则 0% vs LLM 9.11%」这种与 LLM 原意**完全相反**的结论（LLM 说的是清仓 ⇒ 0%）。

【探针断言】
A1 两份真实决策（官方 Underweight / scratch Hold）解析出的冲突集合 == 金标值；
A2 A 实例那种「表头无目标列」的形态 ⇒ 目标权重取**方向列的下一格**，不是理由栏的百分比；
A3 方向为「卖/清仓/不买」而目标格里**没有**百分比 ⇒ 目标权重 = 0%（语义兜底）；
A4 任何一行的目标权重**不得**等于该行理由栏里出现的百分比（除非它本来就是目标格）。

【口径】
只读：不写任何产物；用**唯一落点根**下的真实决策（`<落点根>/runs`）+ 内联合成表。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）
import final_rebalance_plan as F  # noqa: E402
import llm_final as LF  # noqa: E402
import verify_artifacts as VA  # noqa: E402  （生产语义守卫 = 方向-权重判据的**单一事实源**）

#: 运行根目录（**允许用环境变量覆盖** ⇒ 探针可以在**副本**上迭代，不碰官方产物）。
#:    用法：`ETF_RUN_ROOT=<落点根>/runs/_probe_iter python manu/etf_rotation/probes/probe_llm_parser.py`
#:    （官方流水线正在重写产物时，用副本迭代才不会读到半写状态、也不会互相打架。）
RUN_ROOT = Path(os.environ.get("ETF_RUN_ROOT", str(C.ETF_RUN_ROOT)))
#: 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出）：
#:    探针自称「只读：不写任何产物」，实际把 5 个临时 md **写进官方产物目录** ⇒
#:    ① 中途崩了就留残留；② `verify_artifacts` 的密钥扫描会把残留一起扫；
#:    ③ 两个探针并发跑会撞同名文件。
#:    ⇒ 改写到**临时工作区** `<数据集根>/_work/`（仍在持久盘上，不写 /tmp、不写官方产物树）。
SCRATCH = C.WORK_ROOT / "_probe_llm_parser"

#: 金标：两份真实决策的冲突集合（code → (规则%, LLM%, 谁说的, 方向)）
#: 🔴 数值来自 2026-11-04 夜实测产出（官方 final_rebalance_plan.json / scratch 同款），
#:    口径 = 「冲突表里报的是**与规则层差得最远**的那个视图」。
# 🔴 2026-11-05 01:0x（M-K）更新 official 金标：**不是**「把现状抄成标准」。
#:    变更有两个来源，都要记清楚：
#:    ① **决策文件换了**：M-K 改了报告正文 ⇒ 必须重跑 TA ⇒ 新的
#:       `tradingagents_decision.md/json`（2026-11-05 00:37~00:54，979s，Underweight）
#:       与本金标所依据的 2026-11-04 夜那份**不是同一份**。
#:    ② **解析器修了三处**（M-K 同时发现并修掉）：
#:       (a) 代码格允许反引号 ⇒ **研究经理的「Strategic Actions」表不再被静默丢弃**
#:           （旧实现连表头都认不出 ⇒ 9 行只剩「现金」1 行）；
#:       (b) 说话人归属用**分隔符规则** ⇒ 正文小标题「## 多头所谓的…」不再把
#:           后续所有表格（含 Trader 最终裁决）的归属污染成多头；
#:       (c) 目标用**金额**表达（「约2,985元」）时折成百分比 ⇒ 多头「仅10%试仓」不再丢。
#:    改完后的 official 冲突集合**逐条对着新决策原文核过**：
#:       511660/511990 → 三方一致「不买 0%」；
#:       511880 → 三方一致「买 33.5%」（1 手 10,000 元 ≈ 总资产 33.5%）；
#:       512800 → 研究经理与空头「减至 10%」，多头主张维持 20%（与规则层一致 ⇒ 非分歧）；
#:       563020 → 研究经理与空头「不买 0%」，多头「仅 10% 试仓」（= 约2,985元）。
#: 🔴 2026-11-05 02:5x（M-L）**再次**更新 official 金标：**不是**「把现状抄成标准」。
#:    本轮变更**两个**来源，都要记清楚（与上一轮同一纪律）：
#:    ① **决策文件换了**：M-L 改了报告正文（新增「八之四、前视审计」段）⇒ 必须重跑 TA
#:       ⇒ 新的 `tradingagents_decision.md/json`（2026-11-05 01:44~01:55，769s，Underweight）
#:       与上一轮金标所依据的那份**不是同一份**（且本轮研究经理的评级是 Hold 而非 Underweight）。
#:    ② **解析器修了一处真 bug**（`final_rebalance_plan.py`）：
#:       本轮空头表头是「代码 | 当前权重 | **机械目标** | 熊方动作」⇒ 旧实现把
#:       「机械目标」列（= **规则层**的数字）当成**空头自己的目标**
#:       ⇒ 产出「方向=不买、目标=20%」这种**自相矛盾**的视图 ⇒ 语义守卫必然 FAIL。
#:       修法：列名带「机械/规则/量化层」的目标列**不登记为 target**；
#:       并新增「方向格自带百分比」路径（仅当**没有**真正的目标列时才生效）。
#:       🔴 **scratch 金标逐位未变** ⇒ 证明该改动**向后兼容**（A2/A3/A4/A6/A7/A8 全不受影响）。
#:    改完后的 official 冲突集合**逐条对着新决策原文核过**：
#:       511660 / 511880 / 511990 / 563020 ⇒ 空头表「**不买，0%**」+ 执行表「不买 | 0%」⇒ (0.0, 不买)；
#:       512800 ⇒ 空头表「**减至10%左右，上限15%**」+ 执行表「减 | 基准10%，上限15%」⇒ (10.0, 减)。
#:    🔴 who 全是 `bear` 的原因（本轮金标与上一轮的**唯一结构性差别**）：
#:       本轮研究经理的「调整后目标结构」表是「资产 | 目标 | 金额 | 占比」——
#:       **没有方向列** ⇒ 解析器判不出方向 ⇒ 该行整行丢弃 ⇒ 研究经理**没有可解析的视图**
#:       ⇒ 分歧由**空头**提出（口径仍是「取与规则层差得最远的那个视图」）。
#: 🔴 2026-11-05 02:5x（M-L 第二次）**再次**更新 official 金标 + **加 sha1 守卫**：
#:    ① **决策文件又换了**：流水线第 6 步重跑 TA（2026-11-05 02:02~02:17）
#:       ⇒ 新 `tradingagents_decision.md`（sha1 `94d427e9…`）⇒ 研究经理这次给出了
#:       「标的 | 方向 | 目标权重 | 具体执行」**标准表** ⇒ 解析器这次能拿到**研究经理**的视图
#:       ⇒ 冲突的说话人从 `bear` 变回 `research`（**数值口径不变**）。
#:    ② 🔴 **更要紧的是**：本轮实测发现「金标绑死具体决策文件」是**流水线级脆弱点** ——
#:       LLM 每次重跑都可能换措辞 / 换说话人 ⇒ 金标必失效 ⇒ 流水线第 14 步 FATAL
#:       （本轮实测：14 步跑到 13/14 挂掉，**产物本身没问题**却被误杀）。
#:       ⇒ 修法（结构性）：
#:         · **金标**改为 **sha1 守卫**：只有决策文件 sha1 与金标记录一致时才逐条比对；
#:           决策换了 ⇒ 金标**显式报 N/A**（不适用 ≠ 通过）+ 打印当前 sha1；
#:         · 新增 **I1~I4 冲突表不变量**（与措辞无关 ⇒ LLM 重跑不会误杀）：
#:           ① 每条冲突**方向-权重自洽**（零方向 ⇒ 目标 0%；减 ⇒ 目标 < 规则%；买 ⇒ 目标 ≥ 规则%）；
#:           ② 冲突集合 == 「|规则% − LLM%| > 2」的代码集（**不多不少**）；
#:           ③ 说话人必须 ∈ 已知集合；
#:           ④ 冲突表**不得为空**（LLM 有意见却解析不出 ⇒ 解析器坏了）。
#:         · 不变量**带真负例**（注入自相矛盾的一行 ⇒ I1 必须 FAIL）。
#:    ③ 改完后的 official 冲突集合**逐条对着新决策原文核过**（研究经理表 82~92 行）：
#:       512800 减 | 10%左右；159929/513330/513770/512690 卖 | 0%；
#:       563020/511880/511660/511990 不买 | 0%
#:       ⇒ 与 M-K 那份金标**逐条相同**，只有 511880 由「买 33.5%」变为「不买 0%」。
# 🔴 2026-11-05 03:2x（M-L 修完**第二个真 bug**后）**第三次**更新 official 金标：
#    ① **决策文件又换了**：修完「负盈亏率被读成目标权重」的真 bug 后重跑整条 14 步流水线
#       ⇒ 新 `tradingagents_decision.md`（sha1 `795f6f38…`，TA signal=**Sell**、耗时 1362.1 秒）
#       ⇒ 与上一轮金标所依据的那份**不是同一份**（上一轮是 `94d427e9…` / Underweight）。
#    ② **本轮金标的 512800 从 10% 变 9%、说话人从 research 变 final** —— 这是**决策内容真的变了**
#       （Trader 最终裁决表写「约9%，执行区间8%—10%」；研究经理与空头都写「约12%」），
#       **不是**解析器改口径。⇒ 冲突口径仍是「取与规则层**差得最远**的那个视图」：
#       规则 20% vs research/bear 12%（差 8pp）vs final 9%（差 11pp）⇒ 取 final。
#    ③ **逐条对着新决策原文核过**（不是「把现状抄成标准」）：
#       研究经理表（标的 | 当前权重 | 方向 | 12月1日目标 | 执行说明）：
#         512800 减/约12%；159929·513330·513770·512690 卖/0%；563020·511880·511660·511990 不买/0%；
#       空头表（代码 | 当前权重 | 熊派方向 | 建议目标 | 核心理由）：512800 减/约12%，其余同；
#       Trader 最终裁决表（标的 | 当前权重 | 规则机械目标 | 方向判断 | 2026-12-01最终目标）：
#         512800 减/约9%（区间8%—10%）；159929·513330·513770·512690 卖/0%；
#         563020·511880·511660·511990 卖（不买）/0%；
#       ⇒ 冲突集 = 511660·511880·511990·563020（规则 20% vs LLM 0% / 不买 / research）
#         + 512800（规则 20% vs LLM 9% / 减 / final）= **5 条**，与交付件逐位一致。
#    ④ 🔴 **本轮顺带实测到一个已知缺口（不修、只披露）**：多头表表头是
#       （标的 | 当前权重 | 方向 | 12月1日建议 | 核心理由）⇒ 「12月1日建议」**不含**
#       「目标/执行后/调整到/调后」⇒ 该列**不登记为 target** ⇒ 多头的 3 行
#       （512800「减至约20%」、563020「条件式建至20%上限」、511880「只买一手，约1万元」）
#       **被保守丢弃**（不猜 ⇒ 宁可不报）。🔴 **本轮不影响交付件**：
#       这 3 行的目标分别是 20% / 20% / 33.5%，而冲突口径取「差得最远」的视图 ⇒
#       511880 仍是 research 的「不买 0%」（差 20pp > 13.5pp）、563020 同理、512800 由 final 胜出。
#: official 金标所对应的**决策文件 sha1**（决策换了 ⇒ 金标自动失效并显式报 N/A）
#: 🔴 2026-11-05 04:8x（M-N 修；reviewer D-NIT）：**必须同时守 `.md` 与 `.json`** ——
#:    `final_rebalance_plan._decision_text` **优先读 `.json`**（字段完整、无截断），
#:    而旧守卫只守 `.md` ⇒ 若某次 TA 只重写了 `.json`（解析结果已变），
#:    守卫会**放行**（金标失效但没报 N/A）⇒ A1 要么误杀流水线、要么放行错误结果。
# 🔴 2026-11-05 05:4x（M-N 收口）**第四次**更新 official 金标 —— 变更来源有**三个**，都要记清楚
#    （纪律：不是「把现状抄成标准」）：
#    ① **决策文件换了**：2026-11-05 03:55~04:06 重跑整条 14 步流水线 ⇒ 新
#       `tradingagents_decision.json`（sha1 `5cde7bc0…`，TA signal=**Underweight**）
#       与本金标所依据的那份**不是同一份**（上一份是 `e2e365dd…` / signal=Sell）。
#    ② **解析器修了一个真 bug**（`final_rebalance_plan.py`）：官方多头表头是
#       「代码 | 方向 | **当前权重→建议权重** | 核心理由」⇒ 单元格是「22.54%→20%」
#       ⇒ 旧取数「取**第一个**百分数」⇒ 取到 **22.54%（= 当前权重）**
#       ⇒ 方向「减」配目标 22.54% ⇒ 语义守卫 **18 条异常** ⇒ 流水线第 8 步 FATAL。
#       ⇒ 修法：**箭头后面**的百分数才是目标（探针 A18 + 真负例守住）。
#    ③ **决策 md 的渲染器修了**（`llm/run_tradingagents.py::_tail`）：旧口径按字符裁
#       「头 1/3 + 尾 2/3」⇒ 官方 md 里多头表被腰斩、**空头表整表消失**
#       ⇒ 用户读的纪要里没有空头的意见 ⇒ 修法：**表格块整体保留**（只裁散文）
#       ⇒ md 内容变了 ⇒ md sha1 也变（`795f6f38…` → `58ae42c3…`）。
#    ⇒ 改完后的 official 冲突集合**逐条对着新决策原文核过**（多头表 + 空头表全文）：
#       · 多头表（代码 | 方向 | 当前权重→建议权重 | 核心理由）：
#         512800 减至20%（22.54%→20%）｜159929/513330/513770/512690 卖（→0%）
#         ｜563020 买但只试仓约10%（0%→约10%）｜511880 买一手（0%→约33.5%）
#         ｜511660/511990 不买（0%→0%）
#       · 空头表（代码 | 方向 | 建议权重 | 空头理由）：
#         512800 减（约10%，风险上限15%）｜其余 8 只一律 0%（卖或不买）
#       ⇒ 冲突集 = 511660 / 511880 / 511990 / 563020（规则 20% vs LLM 0% / 不买）
#         + 512800（规则 20% vs LLM 10% / 减）= **5 条**，与交付件逐位一致；
#         159929/513330/513770/512690 规则 0% vs LLM 0% ⇒ **不构成冲突**（不在表里）。
#       ⇒ 主视图口径仍是「取与规则层**差得最远**的那个视图」：
#         511880：bull 33.5%（差 13.5pp）vs bear 0%（差 20pp）⇒ bear；
#         563020：bull 10%（差 10pp）vs bear 0%（差 20pp）⇒ bear；
#         512800：bull 20%（差 0 ⇒ 非分歧）vs bear 10%（差 10pp）⇒ bear；
#         511660/511990：bull 与 bear 都是 0%（差值并列）⇒ 按 `_LLM_SECTIONS` 顺序
#         **多头在前** ⇒ bull（排序稳定 ⇒ 同分先到先得）。
GOLDEN_OFFICIAL_DECISION_SHA1 = "58ae42c35dbdcf5261b62b1b4b409fd6711ca17f"
GOLDEN_OFFICIAL_DECISION_JSON_SHA1 = "5cde7bc048802cacd331581dd1aff410dfbf1075"

GOLDEN = {
    "official": {
        "511660": (20.0, 0.0, "bull", "不买"),
        "511880": (20.0, 0.0, "bear", "不买"),
        "511990": (20.0, 0.0, "bull", "不买"),
        "512800": (20.0, 10.0, "bear", "减"),
        "563020": (20.0, 0.0, "bear", "不买"),
    },
    "scratch": {
        "511660": (20.0, 0.0, "research", "不买"),
        "511880": (20.0, 0.0, "research", "不买"),
        "511990": (20.0, 0.0, "research", "不买"),
        "512800": (20.0, 10.0, "research", "减"),
        "563020": (20.0, 0.0, "research", "不买"),
    },
}


def _old_extract_pre_ML(cell: str, anchor_required: bool = False) -> float | None:
    """🔴 **负例用**：M-L **之前**的统一取数（正则 `目标[^0-9]{0,6}(-?\d+...)`）。

    🔴 复现 2026-11-05 02:39 流水线第 8 步 FATAL 的**同一个真 bug**：
       填充字符类 `[^0-9]` 把**负号一起吃掉** ⇒「未进入目标组合，当前-18.91%」
       被读成**正的 18.91%** ⇒ 方向=卖、目标=18.91%（语义守卫必然 FAIL）。
    """
    for _w, _n in re.findall(r"目标([^0-9]{0,6})(-?\d+(?:\.\d+)?)\s*%", cell or ""):
        return float(_n)
    return None


def _old_extract_ML(cell: str, anchor_required: bool = False) -> float | None:
    """🔴 **负例用**：M-L 之后的「负号保留 + 目标窗口盈亏词守卫」版。

    🔴 复现 reviewer A2 指出的副作用：
       「目标从当前 10% 升至 15%」「目标收益 20%」⇒ 旧实现判 ``None``
       ⇒ 该行被丢弃 ⇒ **真实分歧静默漏报**（比误报更难发现）。
    """
    _words = ("当前", "现价", "现值", "浮盈", "浮亏", "亏损", "盈利", "盈亏", "收益",
              "涨幅", "跌幅", "涨跌", "净值", "成本", "价格", "收益率", "回报")
    for _w, _n in re.findall(r"目标([^0-9-]{0,6})(-?\d+(?:\.\d+)?)\s*%", cell or ""):
        if any(_x in _w for _x in _words):
            continue
        return float(_n)
    return None


def _old_extract_no_guard(cell: str, anchor_required: bool = False) -> float | None:
    """🔴 **负例用**：只取格内**第一个**百分数、**完全没有语境守卫**。

    🔴 复现 reviewer B1/B2 指出的残余路径：
       方向格「卖，浮亏18.91%」/ 目标格「浮盈9.11%，清仓」⇒ 被读成目标 18.91% / 9.11%
       ⇒ 与 M-L 修掉的 bug **完全同型** ⇒ 语义守卫必然 FAIL。
    """
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", cell or "")
    return float(m.group(1)) if m else None


def _old_extract_no_arrow(cell: str, anchor_required: bool = False) -> float | None:
    """🔴 **负例用**：M-N **箭头修复之前**的取数（不认「当前权重→建议权重」）。

    🔴 复现 2026-11-05 04:0x 流水线第 8 步 FATAL 的**同一个真 bug**：
       官方决策多头表头是「代码 | 方向 | **当前权重→建议权重** | 核心理由」
       ⇒ 单元格是「22.54%→20%」⇒ 旧取数「取**第一个**百分数」⇒ 取到 **22.54%**
       （= **当前**权重，不是目标）⇒ 方向「减」配目标 22.54% ⇒ 语义守卫 18 条异常
       ⇒ **交付件不可交付**。
    """
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", cell or "")
    if not m:
        return None
    value = float(m.group(1))
    return value if 0.0 <= value <= 100.0 else None


def _conflicts(plan: dict, views: dict) -> dict:
    """复刻 final_rebalance_plan 的冲突构建（口径：取**差得最远**的那个视图）。"""
    out: dict[str, tuple] = {}
    for action in plan.get("actions") or []:
        code = str(action["code"])
        code_views = views.get(code) or []
        if not code_views:
            continue
        rule_pct = float(action.get("target_weight") or 0.0) * 100.0
        div = [v for v in code_views if abs(rule_pct - v["target_pct"]) > 2.0]
        if not div:
            continue
        div.sort(key=lambda v: abs(rule_pct - v["target_pct"]), reverse=True)
        top = div[0]
        out[code] = (round(rule_pct, 2), top["target_pct"], top["who"], top["direction"])
    return out


def _invariant_failures(plan: dict, views: dict | None = None) -> list[str]:
    """冲突表**不变量**判定：返回违例描述列表（空 = 全部自洽）。

    🔴 2026-11-10：**改为委托生产语义守卫**（`verify_artifacts._check_llm_direction_semantics`）
       ⇒ 探针与生产**同一套判据**，杜绝「第三套独立漂移的口径」。

    🔴 旧实现（探针自己写的一套）为什么是错的：
       · 它拿**规则层 %** 当参照，写成「买 ⇒ 目标 ≥ 规则%」⇒ 把 LLM **合法表达**的
         「买，但上限 10%（半档）」判成缺陷 ⇒ 2026-11-10 闸门假红
         （实测本轮 515450 / 515650 两行：规则 20%、LLM 「买，但上限10%」）。
       · 「减」那一档也该参照**当前权重**（生产口径），不是规则 %。
       · 词表也比生产守卫窄 ⇒ 解析器能产出的方向词大半不受约束（漏检）。
    ⇒ 正确判据（生产守卫 `verify_artifacts.py` 5 档，**以该行自己的当前权重为参照**）：
       ZERO ⇒ 目标 = 0%；POS ⇒ 目标 > 0%；REDUCE ⇒ 目标 < 当前权重；
       INCREASE ⇒ 目标 > 当前权重；NEUTRAL ⇒ 不设约束；**未分类方向词 ⇒ FAIL**。
       （规则层 % 只回答「这条算不算分歧」，**不判定方向与数字是否自洽**。）

    🔴 「不许放宽成永真」：本探针同时保留 **N-I1 真负例**（注入「不买 + 20%」⇒ 必须 FAIL），
       即「原样交付件必须 PASS」＋「自相矛盾必须 FAIL」两头都有牙。
    """
    ok, detail = VA._check_llm_direction_semantics(plan)
    return [] if ok else [detail]


# ---------------------------------------------------------------------------
# 🔴 2026-11-05 09:5x（M-R 修）：**内容无关**的断言工具（把「抄金标」换成「可回放不变量」）
#
# 【为什么必须重写】2026-11-05 凌晨有人把「上一份 LLM 决策原文的具体数字」抄成了探针金标
#   ⇒ 夜间重跑 LLM 换了措辞 ⇒ 探针红了 ⇒ 有人「更新金标」迁就新输出
#   ⇒ 把**两个真 bug**盖成了**假绿**：
#     ① 「条件买入」被漏抽 ⇒ 511880 整条裁决掉进 `unparsed_codes`；
#     ② 「现阶段风险预算定为约 15%」抽不出 ⇒ 反而抓到条件句里的「将仓位进一步降至约10%」
#        ⇒ 目标权重写成 10%（真实裁决是 15%）。
# ⇒ 下面这些 helper 的口径：**只从「决策 md 原文」或「计划自己算出来的量」推导**，
#   **不写死任何一份具体决策的内容**（代码、数值、行数、措辞）。
# ---------------------------------------------------------------------------

#: 表格块的最小行数（≥2 行才算一个表格块；孤立单行 = 腰斩残行的痕迹）
_MIN_TABLE_ROWS = 2

#: 表头里表示「方向」的列名关键词（用来定位方向列 ⇒ 代码按**代码格**归属）
_DIR_HEADERS = ("方向", "操作", "动作")

#: md 里表示**零方向**的写法（与 `llm_final._ACTION_RULES` 的动作词对齐）
_ZERO_WORDS = ("不建仓", "不买", "卖出", "清仓")

#: 计划里允许的**零方向**取值
_ZERO_DIRS = ("不买", "不建仓", "卖", "清仓")

#: 手数的中文写法（与 `llm_final._CN_NUM` 对齐）
_CN_LOTS: dict[int, str] = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五",
                             6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}

#: 6 位代码正则（**照抄解析器口径** `llm_final._CODE_RE`：首位不能是 0，
#:    否则「最终分差仅 0.006126」里的 "006126" 会被当成一个代码）
_CODE_RE = re.compile(r"(?<![\d.])([1-9]\d{5})(?!\d)")

#: 清仓类动作词（照抄解析器 `llm_final._ACTION_RULES` 第 ① 档）
_ZERO_CLEAR_WORDS = ("全部卖出", "卖出全部", "清仓", "清掉", "清零")

#: 「不买」类动作词（照抄解析器 `llm_final._ACTION_RULES`）
_ZERO_NOBUY_WORDS = ("不建仓", "不买入", "不买")

#: 部分卖出（减仓）标记：**不是**零方向（目标权重另由「权重/目标」句给）
_PARTIAL_WORDS = ("减仓", "减持", "减至", "减到", "压缩至", "压到", "下调至", "下调到")

#: md 的强调/代码标记（LLM 常写「卖出约 **2,600份**」⇒ 动词与数量之间夹着 `**`）
_MD_NOISE = r"[\s*`_~]*"

#: 带数量的「卖出 N 份/股」= 部分卖出（**不是**清仓）
#:    🔴 2026-11-10：与解析器 `llm_final._ACTION_RULES` ② **同口径**（探针与被测物必须一致）
#:    —— ① 允许夹 markdown 强调标记；② 同样认「减持/减仓 + 数量」。
_SELL_QTY_RE = re.compile(
    r"(?:卖出|卖掉|减掉|减持|减仓)" + _MD_NOISE + r"(?:约)?" + _MD_NOISE
    + r"[\d,]+(?:\.\d+)?" + _MD_NOISE + r"(?:股|份)")

#: 否定标记（与解析器 `llm_final._NEG_MARKERS` **同口径** ⇒ 探针与被测物必须一致）
_NEG_MARKERS = ("不是", "非", "不", "无需", "避免", "杜绝", "不予", "排除", "拒绝", "免于")

#: 否定**作用域**的断点词（与解析器 `llm_final._SCOPE_BREAKERS` 同口径）
_SCOPE_BREAKERS = ("但", "不过", "然而", "却", "反而", "而是", "改为", "转而")

#: 买入类动作词（照抄解析器 `llm_final._ACTION_RULES`）
_BUY_ACTION_RE = re.compile(
    r"买\s*(?:入)?\s*(?:\d+|[一二两三四五六七八九十])\s*手"
    r"|条件买入|有条件买入|条件建仓")

#: 条件句标记（照抄解析器 `llm_final._CONDITIONAL_WORDS` +「条件/触发」）
#:    🔴 与解析器的唯一差别：解析器还把裸「只有」算条件词；探针**不算**
#:       （「只有 512800 保留，其余清仓」这种句子仍要能看出清仓 ⇒ 少判条件更安全）。
_COND_WORDS = ("若", "如果", "假如", "倘若", "一旦", "除非", "只有当", "条件", "触发")

#: 🔴 2026-11-09（Mode C v2 审计实证）：「**已触发**」是**既成事实**，不是条件。
#:    与解析器 `llm_final._FACT_TRIGGER_RE` **同口径**（探针与被测物必须一致）：
#:    实测：LLM 最终表写「| **159929** | **卖出** | 0% | 已触发持仓MA60强制退出 …」
#:    ⇒ 旧口径把「触发」当条件词 ⇒ 这条**真裁决**被当成条件句跳过
#:    ⇒ 三条清仓指令整条漏抽（落进 unparsed_codes）＋ 证据句被误判为条件句。
_FACT_TRIGGER_RE = re.compile(r"(?:已|已经|均|皆|都|全部)?触发(?![后则时条])")

#: 「否决 / 备选」类 ⇒ 子句里的清仓词**不是**本轮裁决（「清零方案 1 票对 2 票被否」）
_REJECT_WORDS = ("方案", "被否", "否决", "不考虑", "未采纳", "备选")


def _md_table_blocks(text: str) -> list[list[str]]:
    """把 md 切成**表格块**：连续 ≥2 行以 `|` 开头 ⇒ 一个块。

    🔴 孤立单行（前后都不是表格行）**不算块** ⇒ 它们会在「表格总行数 == Σ(各块行数)」
       这条自洽断言里暴露出来（= 腰斩残行的痕迹）。
    """
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("|"):
            cur.append(line)
            continue
        if len(cur) >= _MIN_TABLE_ROWS:
            blocks.append(cur)
        cur = []
    if len(cur) >= _MIN_TABLE_ROWS:
        blocks.append(cur)
    return blocks


def _md_cells(line: str) -> list[str]:
    """把一行 markdown 表格切成单元格（去掉首尾的空单元格）。"""
    parts = line.split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _md_table_codes(block: list[str]) -> set[str]:
    """表格块里的**代码集合**：先把单元格里的 `**` 和反引号剥掉再匹配 6 位代码。

    🔴 为什么不能对整行做子串匹配：**理由格**里会提到别的代码
       （实测 513770 的理由格写了 513330）⇒ 整行子串计数会把「被提到的代码」
       误当成「该行的代码」⇒ 集合被污染。
    """
    codes: set[str] = set()
    for line in block:
        for cell in _md_cells(line):
            clean = cell.replace("**", "").replace("`", "")
            for m in re.findall(r"(?<!\d)\d{6}(?!\d)", clean):
                codes.add(m)
    return codes


def _md_table_invariant(text: str) -> tuple[bool, str]:
    """**表格块不变量**（A19② 的口径）：判据依据是**与是哪一份 md 无关的结构不变量**。

    **前置**：含代码的表格块 ≥2 个。

    **两条判据**（2026-11-09 起生效；① 于 2026-11-10 **换判据**，理由见下面注释）：
    ① **孤表**必须被「已重复代码集合」的**并集**覆盖
       （⇒ 只出现 1 次、且含有重复集合里没有的代码的表格块 ⇒ FAIL）；
    ② **≥2 个不同的代码集合**各出现 ≥2 次（⇒ 多头表 + 空头头表**都在**）。

    ⇒ 两条判据能抓住的缺陷（**不依赖任何行数/代码阈值**）：
      · **多头表腰斩 / 空头表整表切掉** ⇒ 重复出现的代码集合只剩 **1** 个
        （多头表与空头表不再**成对出现**）⇒ **判据② 失效 ⇒ FAIL**；
      · **多出一张含新代码的孤表** ⇒ 该表只出现 1 次、且它的代码不在重复集合的
        并集里 ⇒ **判据① 失效 ⇒ FAIL**。

    🔴 旧判据（③「重复出现的代码集合必须**同时是所有块里最大的**」）已被**替换**
      （换掉的理由是**实测误红**，不是「比旧判据更强」）：
      实测 2026-11-09 那一轮决策 md —— 多头表 / 空头表 / 研究经理三张表是
      **同一个 9 代码集合**（出现 **3** 次），Trader 的最终表是 **10 代码集合**
      （出现 **2** 次）⇒ 旧判据取「出现 3 次的 9 代码集合」当基准，
      再看到「存在 10 代码的更大集合」⇒ 报「多出了半个表」
      ⇒ **误红**（那一轮 md **并没有**半张表）。

    ⇒ 判据只看**代码集合的重复次数**（不写死代码集合、不写死行数）
      ⇒ LLM 换表格写法不误红，而真腰斩 / 真切表仍红
      （`_tail` 侧的变异负例见 A19④）。
    """
    blocks = _md_table_blocks(text)
    coded = [b for b in blocks if _md_table_codes(b)]
    if len(coded) < 2:
        return False, f"含代码的表格块只有 {len(coded)} 个（必须 ≥2）"
    counts: dict[frozenset[str], int] = {}
    for b in coded:
        key = frozenset(_md_table_codes(b))
        counts[key] = counts.get(key, 0) + 1
    detail_sets = "；".join(
        f"{sorted(s)}×{c}" for s, c in sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]))))
    # 🔴 2026-11-09（Mode C v2 审计实证）**判据修正**：
    #    旧写法把「**出现次数最多**的集合」与「**最大**集合」当成同一个 ⇒ 误红。
    #    实测 2026-11-09 这一轮决策 md：多头/空头/研究经理三张表是**同一个 9 代码集合**
    #    （出现 3 次），Trader 的最终表是**10 代码集合**（出现 2 次）
    #    ⇒ 旧判据取「出现 3 次的 9 代码集合」当基准，再发现「存在 10 代码的更大集合」
    #    ⇒ 报「多出了半个表」⇒ **误红**（这一轮 md **没有**半张表）。
    #    ⇒ 判据（**与旧判据不同** —— 不是「更强」而是「不误红」，仍不写死代码/行数）：
    #      ① **孤表**必须被「已重复代码集合」的**并集**覆盖；
    #      ② **≥2 个不同的代码集合**各出现 ≥2 次（⇒ 多头表 + 空头表**都在**
    #         ⇒ 任一张被腰斩/整表切掉 ⇒ 重复的集合就只剩 1 个 ⇒ 立刻 FAIL）。
    #    ⇒ 判据仍能抓住 docstring 列出的两类缺陷（多头表腰斩 / 空头表整表切掉、
    #      凭空多出含新代码的孤表），`_tail` 侧的**变异负例**见 A19④
    #      （真负例 ⇒ 不许 vacuous 通过）。
    # 🔴 2026-11-10（实测红灯）：判据① 从「**全局最大**集合必须重复」换成
    #    「**孤表**必须被已重复集合的**并集**覆盖」。换判据的**事实依据**（不是「变松」）：
    #    本轮决策 md（750 行 / 110 表格行 / 10 块 / 6 个代码集合）——
    #    多头表（行 174-185）与空头表（行 430-436）**都在且完整**，
    #    重复集合有 2 个（5 代码 ×4、15 代码 ×2 ⇒ 判据② PASS）；
    #    而那张 **19 代码**的表是**研究经理「各标的操作意见」汇总表**（行 686-706）
    #    = 上面两个重复集合的**并集** ⇒ 它**合法地只出现 1 次**
    #    ⇒ 旧判据①（「全局最大集合必须重复」）⇒ **误红**。
    #    ⇒ 新判据①：只出现 1 次的表，必须是重复集合并集的**子集**（汇总表合法）
    #      —— 含并集里没有的新代码 ⇒ **孤表** ⇒ FAIL。
    max_size = max(len(s) for s in counts)
    repeated_sets = [s for s, c in counts.items() if c >= 2]
    if len(repeated_sets) < 2:
        return False, (f"只有 {len(repeated_sets)} 个代码集合在 ≥2 个表格块里重复出现"
                       f"（必须 ≥2 ⇒ 多头表与空头表都在；有一张被腰斩/切掉了）"
                       f"｜各集合出现次数：{detail_sets}")
    union = set().union(*repeated_sets) if repeated_sets else set()
    orphans = [s for s, c in counts.items() if c == 1 and not set(s) <= union]
    if orphans:
        return False, (f"有 {len(orphans)} 张表含重复集合里没有的代码（孤表）"
                       f"｜孤表代码集合={[sorted(s) for s in orphans]}"
                       f"｜重复集合并集 {len(union)} 个代码｜各集合出现次数：{detail_sets}")
    return True, (f"含代码的表格块 {len(coded)} 个｜最大代码集合 {max_size} 个代码"
                  f"｜重复出现的代码集合 {len(repeated_sets)} 个"
                  f"（并集 {len(union)} 个代码）｜孤表 0 张"
                  f"｜各集合出现次数：{detail_sets}")


def _md_table_structure(text: str) -> tuple[bool, str]:
    """**表格块结构自洽**（A19③ 的口径）：不写死行数。

    ① 每个表格块 ≥2 行；
    ② 每个块的第 2 行是**分隔行**（单元格只含 `-` `:` `|` 空格）；
    ③ 表格总行数 == Σ(各块行数)（⇒ 没有游离在块外的孤立表格行 = 腰斩残行）。

    ⇒ 能抓住的缺陷：表格块内部被截断（分隔行被切掉）/ 混进非表格行 / 出现孤立残行。
    """
    lines = (text or "").splitlines()
    total = sum(1 for ln in lines if ln.startswith("|"))
    blocks = _md_table_blocks(text)
    bad: list[str] = []
    for i, b in enumerate(blocks):
        if len(b) < _MIN_TABLE_ROWS:
            bad.append(f"块{i} 只有 {len(b)} 行")
            continue
        sep = _md_cells(b[1])
        if not sep or not all(re.fullmatch(r"[-: ]+", c) for c in sep):
            bad.append(f"块{i} 第2行不是分隔行：{b[1][:60]!r}")
    block_sum = sum(len(b) for b in blocks)
    ok = not bad and total == block_sum
    detail = (f"表格行 {total} 行｜{len(blocks)} 个块（块内行数合计 {block_sum}）"
              f"｜结构异常 {bad[:2]}")
    if total != block_sum:
        detail += f"｜有 {total - block_sum} 行游离在表格块之外（= 腰斩残行）"
    return ok, detail


def _md_number_variants(value: float) -> list[str]:
    """百分数的**写法变体**（原文可能写 15 / 15.0 / 15.00）。"""
    out: list[str] = []
    for text in (f"{float(value):g}", f"{float(value):.1f}", f"{float(value):.2f}"):
        if text not in out:
            out.append(text)
    return out


def _md_number_hit(md_text: str, value: float) -> str | None:
    """百分数的数字串能否在**决策 md 原文**里找到（命中则返回命中的写法）。

    🔴 用「独立数字 token」而不是裸子串：`512800` 里的 `128`、`2,600` 里的 `600`
       都不算命中（否则等于没查）。
    """
    for cand in _md_number_variants(value):
        if re.search(r"(?<![\d.,])" + re.escape(cand) + r"(?![\d.,])", md_text or ""):
            return cand
    return None


def _md_lots_hit(md_text: str, lots: int) -> str | None:
    """手数能否在**决策 md 原文**里回放（命中则返回命中的写法）。

    🔴 口径**照抄解析器自己的取数语法**（`llm_final._BUY_LOTS_RE` / `_MAX_LOTS_RE`）：
       手数只可能来自「买 N 手」/「最多 N 手」这类**买 / 上限**表述
       ⇒ ① 用同样的语法回放 ⇒ 解析器抽出来的值必然命中（不会误杀）；
         ② 「三手货币 ETF」「买不齐三手」这种**不是手数**的写法不会被误命中
            （突变测试实测：裸数字匹配会把这两句误当成「3 手」）。
    """
    nums = [str(int(lots))]
    cn = _CN_LOTS.get(int(lots))
    if cn:
        nums.append(cn)
    if int(lots) == 2:
        nums.append("二")
    alt = "|".join(re.escape(n) for n in nums)
    pat = (r"(?:买\s*(?:入)?\s*|最多\s*|至多\s*|不超过\s*|不超\s*)"
           + rf"(?:{alt})\s*\**\s*手")
    m = re.search(pat, md_text or "")
    return m.group(0) if m else None


def _md_zero_dir_word(cell: str) -> str | None:
    """判断一个**方向格**是不是零方向（不买 / 不建仓 / 卖出 / 清仓）。

    🔴 为什么要把否定式里的「买」消掉再看：「本轮不买」含「买」字，
       直接判「含买 ⇒ 买入方向」会把零方向误判成买入方向。
    """
    text = (cell or "").replace("**", "").replace("`", "").strip()
    hit = next((w for w in _ZERO_WORDS if w in text), None)
    if hit is None:
        return None
    probe = text
    for w in ("不建仓", "不买入", "不买"):
        probe = probe.replace(w, w.replace("买", ""))
    if "买" in probe:
        return None
    return hit


def _md_sentences(text: str) -> list[str]:
    """按「。；换行」切句（照抄解析器 `llm_final._sentence_offsets` 的口径）。"""
    return [s for s in re.split(r"[。；\n]", text or "") if s.strip()]


def _md_clauses(sentence: str) -> list[str]:
    """按「，」切子句（照抄解析器 `llm_final._clause_spans` 的口径）。

    🔴 2026-11-10 实测漂移：旧实现连 **半角逗号** 一起切 ⇒ 「卖出约 **2,600份**」
       被切成「卖出约 **2」＋「600份**…」 ⇒ **千分位数字被腰斩**
       ⇒ 「部分卖出」正则匹配不上 ⇒ 该子句被误判成**裸卖出（清仓）**
       ⇒ 512800 被探针误判成「零方向」⇒ A20⑥ 假红。
       ⇒ 解析器只切**全角**「，」（半角逗号留给千分位）⇒ 探针必须**逐字对齐**。
    """
    return [c for c in re.split(r"，", sentence or "") if c.strip()]


def _md_codes_in(text: str) -> list[str]:
    """文本里的 6 位代码（**照抄解析器的代码正则** ⇒ 首位不能是 0）。"""
    return [m.group(1) for m in _CODE_RE.finditer(text or "")]


def _md_is_conditional(sentence: str) -> bool:
    """是不是**条件句**（照抄解析器 `llm_final._is_conditional` 的口径）。

    🔴 条件句里的数字**不是**最终裁决（那是触发器）：
       「若 512800 的 E 转负…将仓位进一步降至约 10%」是触发条件，不是本轮落点。
    """
    head = re.sub(r"^[\-\*\+>#\s]*", "", (sentence or "").strip())
    head = re.sub(r"^\d+[\.、)\]]\s*", "", head)
    if any(head.startswith(w) for w in ("若", "如果", "假如", "倘若", "一旦", "除非", "只有当")):
        return True
    probe = re.sub(r"有条件买入|条件买入|条件建仓|有条件建仓", "", head)
    # 🔴 2026-11-09：抹掉「已/均/皆 + 触发」这类**既成事实**（与解析器同口径）
    probe = _FACT_TRIGGER_RE.sub("", probe)
    first = re.split(r"[，,；;：:]", probe)[0]
    return any(w in first for w in _COND_WORDS)


def _md_negated(text: str, pos: int, matched: str = "") -> bool:
    """`pos` 处的动作词是否被否定（**子句级否定作用域**）。

    🔴 2026-11-10 实测事故（A20⑥ 红灯）：LLM 写
       「采纳本轮**战术性卖出、降低权益敞口**，但不把 `512800` 一次清仓」
       ⇒ 旧口径只看动作词**前 4 个字**（「一次」）⇒ 否定词「不」落在窗口外
       ⇒ 「清仓」被当成**清仓指令** ⇒ 512800 被误判成「零方向」。
       ⇒ 与解析器 `llm_final._negated` **同口径**（子句级作用域 + 转折断点 + 代码断点）：
          ① 动作词之前必须有否定标记（取最近的）；② 否定与动作词之间有转折词 ⇒ 管不到；
          ③ 否定与动作词之间夹着别的代码、且子句里还有否定之前的代码 ⇒ 管不到；
          ④ 动作词自带否定（「不买」「不建仓」）⇒ 本身就是零方向指令。
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


def _md_first_unnegated(text: str, words: tuple[str, ...]) -> str | None:
    """text 里第一个**没有被否定**的 words 里的词（「不清仓」「不是清零」不算）。"""
    for w in words:
        for m in re.finditer(re.escape(w), text or ""):
            if _md_negated(text or "", m.start(), m.group(0)):
                continue
            return w
    return None


def _md_clause_codes(clause: str) -> list[str]:
    """子句里**属于本子句裁决对象**的代码（理由里顺带提到的**不算**）。

    🔴 口径：**第一组「连写」的代码**（代码之间只有「、/ / 与 和 及」这类连接词）
       ⇒「清仓 A、B、C」这种**列表**写法**全算上**；
       理由 / 括号里顺带提到的代码（如「…（不到 512800 的三分之一）」）**不算**
       ⇒ 实测 2026-11-05 10:41 那份决策里，512800 只是**比较对象**
         （裁决对象是 563020）⇒ 若把整句的代码都算进零方向集合 S，
         就会把 512800 误判成「零方向」⇒ **探针误杀**（流水线 FATAL）。
    """
    hits = list(_CODE_RE.finditer(clause or ""))
    if not hits:
        return []
    group = [hits[0].group(1)]
    prev_end = hits[0].end()
    for m in hits[1:]:
        gap = (clause or "")[prev_end:m.start()]
        if re.fullmatch(r"[\s、/,，和与及]*", gap):
            group.append(m.group(1))
            prev_end = m.end()
            continue
        break
    return group


def _md_clause_zero_word(clause: str) -> str | None:
    """子句里写明的**零方向**（清仓 / 卖出全部 / 不买 / 不建仓）；没有则 None。

    🔴 **部分卖出不算零方向**：「卖出约 2,600 份」「减至约 5,100 份」是**减仓**
       （目标权重另由「权重/目标」句给）⇒ **不是**清仓
       （老 bug 正是把这里当清仓 ⇒ 目标权重被写成 0%）。
    🔴 **否定式保护**：「不是清仓 / 不清零 / 而非清仓」⇒ 不算。
    🔴 **否决 / 备选**：「清零方案 1 票对 2 票被否」⇒ 不算。
    🔴 **裸「卖出」算零方向**（照抄解析器 `_ACTION_RULES` 第 ④ 档），
       但**带数量**的「卖出 N 份/股」在它**之前**判 ⇒ 判成部分卖出（减）。
    """
    text = clause or ""
    if any(w in text for w in _REJECT_WORDS):
        return None
    clear = _md_first_unnegated(text, _ZERO_CLEAR_WORDS)
    if clear:
        return clear
    if any(w in text for w in _PARTIAL_WORDS) or _SELL_QTY_RE.search(text):
        return None
    sell = _md_first_unnegated(text, ("卖出", "卖掉"))
    if sell:
        return sell
    return _md_first_unnegated(text, _ZERO_NOBUY_WORDS)


def _md_has_partial_action(clause: str) -> bool:
    """子句里是不是**部分卖出**（减仓 / 带数量的卖出）。"""
    text = clause or ""
    return bool(any(w in text for w in _PARTIAL_WORDS) or _SELL_QTY_RE.search(text))


def _md_has_buy_action(clause: str) -> bool:
    """子句里是不是**买入类**动作（买 N 手 / 条件买入 / 条件建仓）。"""
    for m in _BUY_ACTION_RE.finditer(clause or ""):
        pre = (clause or "")[max(0, m.start() - 4):m.start()]
        if any(neg in pre for neg in ("不", "非", "未", "无", "别")):
            continue
        return True
    return False


def _legacy_char_crop(text: str, limit: int) -> str:
    """**旧裁剪口径**的等价实现：按字符裁「头 1/3 + 省略标记 + 尾 2/3」。

    🔴 只给 **A19④ 的负例**用：正常路径优先用仓库自己的
       `llm/run_tradingagents.py::_head_tail`（那就是旧口径的活代码）；
       万一它被改名/删除 ⇒ 用这份等价实现兜底（口径必须一致，否则负例失真）。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return (text[: limit // 3] + "\n\n...[中段省略]...\n\n" + text[-(2 * limit) // 3:])
def _md_zero_direction_codes(md_text: str) -> tuple[set[str], str]:
    """从**决策 md 原文**收集「零方向」代码集合 S（**不写死任何代码**）。

    S = （表格**方向列**推导）∪ （**逐句散文**推导）：
      · 表格：代码按「**代码格**」归属（理由格里的别的代码**不算**）；
      · 散文：按**子句**归属 ⇒「清仓 A、B、C」这种**列表**写法要**全算上**；
        子句里没有动作词 ⇒ 顺带提到的代码不算（理由句里的代码**不算**）。
      · 🔴 **部分卖出不算零方向**（见 `_md_clause_zero_word`）。

    ⇒ 能抓住的缺陷：把「卖出/清仓」当成带非零目标 / 把清仓方向配上非零权重 /
      把零方向代码整条漏抽（掉进 `unparsed_codes`）/ 把「卖出约 N 份」误判成清仓。
    """
    try:
        section = LF.extract_section(md_text)
    except Exception:  # noqa: BLE001 —— 探针自身出错 ⇒ 如实退化为「没找到」
        section = None
    if not section:
        return set(), "未能定位最终裁决章节（llm_final.extract_section 返回空）"
    from_table: set[str] = set()
    from_prose: set[str] = set()
    for block in _md_table_blocks(section):
        header = _md_cells(block[0])
        dir_idx = next((i for i, h in enumerate(header)
                        if any(k in h for k in _DIR_HEADERS)), None)
        for line in block[2:]:
            cells = _md_cells(line)
            code = None
            code_idx = -1
            for i, cell in enumerate(cells):
                m = re.findall(r"(?<!\d)\d{6}(?!\d)",
                               cell.replace("**", "").replace("`", ""))
                if m:
                    code = m[0]
                    code_idx = i
                    break
            if not code:
                continue
            idx = dir_idx if dir_idx is not None else code_idx + 1
            if idx < 0 or idx >= len(cells):
                continue
            if _md_zero_dir_word(cells[idx]) or _md_zero_dir_word(cells[code_idx]):
                from_table.add(code)
    for sentence in _md_sentences(section):
        if _md_is_conditional(sentence):
            continue
        for clause in _md_clauses(sentence):
            if _md_clause_zero_word(clause):
                from_prose.update(_md_clause_codes(clause))
    codes = from_table | from_prose
    return codes, (f"最终裁决章节：表格方向列 {len(from_table)} 个代码"
                   f"｜散文子句 {len(from_prose)} 个代码｜合计 {len(codes)} 个")

def _synthetic(decision_md: Path, body: str, total_assets: float | None = None) -> dict:
    """把合成正文写成一份决策 md（带同目录 json 占位），返回解析视图。

    🔴 2026-11-04 22:5x（M-B①）：F5 判定改成「**运行内快照**」⇒ 同一个路径被复用写多份
       合成正文时，**快照会让 case 之间串味**（上一份的判定被沿用）
       ⇒ 每次合成前必须清快照（等价于「一次构建 = 一次快照」）。
    """
    F.reset_decision_failure_cache()
    decision_md.write_text(body, encoding="utf-8")
    # 🔴 2026-11-05 05:0x（M-N）：``total_assets`` 用于把「约1万元」折成百分比
    #    （不传 ⇒ 折算不出来 ⇒ 该行被丢 ⇒ 探针会误判成「建议列没认出来」）。
    return F._llm_target_views(str(decision_md), total_assets)


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    lines: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
        if not ok:
            failures.append(name)

    # ---- A1 两份真实决策 == 金标 ----
    for tag, root in (("official", RUN_ROOT),
                      ("scratch", RUN_ROOT / "_e2e_20261104T2120")):
        plan_path = root / "final_rebalance_plan.json"
        dec_path = root / "tradingagents_decision.md"
        if not (plan_path.exists() and dec_path.exists()):
            if tag == "official":
                check(f"A1 {tag} 真实决策可解析", False,
                      f"缺 {plan_path} 或 {dec_path}（**官方产物缺失 ⇒ FAIL**）")
            else:
                # 🔴 2026-11-05 01:4x（reviewer 指出）：scratch 金标目录是**一次性**的
                #    （跑完即删）⇒ 缺失只算「**不适用**」，**不许**判 FAIL
                #    （否则流水线第 13 步会被这个已清理的目录误杀 ⇒ 交付件本身没问题却 die）。
                print(f"[N/A] A1 {tag} 夹具不存在（{root}）⇒ 该金标**不适用**（不适用 ≠ 通过）")
            continue
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        views = F._llm_target_views(str(dec_path))
        got = _conflicts(plan, views)
        want = GOLDEN.get(tag, {})
        if tag == "official":
            # 🔴 2026-11-05 02:5x（M-L）：**sha1 守卫** ⇒ 决策换了就报 N/A，不误杀流水线
            _cur_sha1 = hashlib.sha1(dec_path.read_bytes()).hexdigest()
            _json_path = dec_path.with_suffix(".json")
            _cur_json_sha1 = (hashlib.sha1(_json_path.read_bytes()).hexdigest()
                              if _json_path.exists() else None)
            _sha_ok = (_cur_sha1 == GOLDEN_OFFICIAL_DECISION_SHA1
                       and _cur_json_sha1 == GOLDEN_OFFICIAL_DECISION_JSON_SHA1)
            if not _sha_ok:
                print(f"[N/A] A1 {tag} 金标：决策文件 sha1 已变"
                      f"（md 当前 {_cur_sha1[:8]}｜金标 {GOLDEN_OFFICIAL_DECISION_SHA1[:8]}；"
                      f"json 当前 {str(_cur_json_sha1)[:8]}｜金标 {GOLDEN_OFFICIAL_DECISION_JSON_SHA1[:8]}）"
                      f"⇒ 金标**不适用**（不适用 ≠ 通过；不变量 I1~I4 仍然强制）")
            else:
                check(f"A1 {tag} 冲突集合 == 金标", got == want,
                      f"一致 {len(got)}/{len(want)} 条｜差异 "
                      f"{ {k: (want.get(k), got.get(k)) for k in set(want) | set(got) if want.get(k) != got.get(k)} }")
            # ---- I1~I4：冲突表**不变量**（与决策措辞无关 ⇒ LLM 重跑不会误杀） ----
            _rows = plan.get("llm_conflicts") or []
            _bad = _invariant_failures(plan, views)
            check("I1 冲突表每行**方向-权重自洽**"
                  "（与生产语义守卫同口径：零方向⇒0%；买⇒>0%；减⇒<当前；增⇒>当前；"
                  "未分类⇒FAIL）",
                  not _bad, f"异常 {len(_bad)} 条：{_bad[:4]}" if _bad else f"{len(_rows)} 行全部自洽")
            _expect = set()
            for _a in plan.get("actions") or []:
                _c = str(_a.get("code"))
                _rp = float(_a.get("target_weight") or 0.0) * 100.0
                _cv = views.get(_c) or []
                if any(abs(_rp - float(v.get("target_pct") or 0.0)) > 2.0 for v in _cv):
                    _expect.add(_c)
            # 🔴 2026-11-05 02:4x（M-L）：**现金行是计划端单独构造的**（`final_rebalance_plan.py`
            #    的 `if "现金" in llm_targets` 分支）⇒ 规则 % = `cash_end / total_assets`
            #    （**不是** actions 里的 target_weight）⇒ 期望集**必须**单列现金，
            #    否则 I2 会把「计划有现金冲突」误判成「多了」（本轮实测：期望 5 条 vs 实际 6 条）。
            _cf = plan.get("cash_flow") or {}
            _ta = float((plan.get("meta") or {}).get("total_assets") or 0.0)
            if _ta > 0 and (plan.get("llm_targets") or {}).get("现金"):
                _cash_rule = float(_cf.get("cash_end") or 0.0) / _ta * 100.0
                _cash_views = views.get("现金") or []
                if any(abs(_cash_rule - float(v.get("target_pct") or 0.0)) > 2.0
                       for v in _cash_views):
                    _expect.add("现金")
            _got_codes = {str(_r.get("code")) for _r in _rows}
            check("I2 冲突集合 == 「|规则% − LLM%| > 2」的代码集（**不多不少**）",
                  _got_codes == _expect,
                  f"冲突表 {sorted(_got_codes)}｜期望 {sorted(_expect)}"
                  f"｜多 {sorted(_got_codes - _expect)}｜缺 {sorted(_expect - _got_codes)}")
            _whos = {str(v.get("who")) for _vs in views.values() for v in _vs}
            _legal = {"research", "bull", "bear", "final", "trader", "unknown"}
            check("I3 冲突表的说话人必须 ∈ 已知集合",
                  _whos <= _legal, f"出现的说话人={sorted(_whos)}｜未知={sorted(_whos - _legal)}")
            check("I4 冲突表**不得为空**（LLM 有意见却解析不出 ⇒ 解析器坏了）",
                  bool(_rows), f"冲突 {len(_rows)} 条")
        else:
            check(f"A1 {tag} 冲突集合 == 金标", got == want,
                  f"一致 {len(got)}/{len(want)} 条｜差异 "
                  f"{ {k: (want.get(k), got.get(k)) for k in set(want) | set(got) if want.get(k) != got.get(k)} }")

    # ---- A2/A3/A4 合成形态（A 实例失败模式）----
    tmp = SCRATCH / "_probe_llm_parser_tmp.md"
    try:
        # 表头**故意**不含能被识别的「目标/执行后/调整到/调后」列名（写「仓位」）
        # ⇒ 解析器必须靠「方向列锚定」取目标权重，而不是抓理由栏里的百分比。
        body = (
            "# 合成决策\n\n## 研究经理\n\n"
            "| ETF | 权重 | 方向 | 仓位 | 理由 |\n"
            "|:--|--:|:--|--:|:--|\n"
            "| 159929 | 11.17% | **卖清** | 0% | 已被规则列入跌破MA60的强制退出名单；+9.11%的浮盈不能覆盖退出信号 |\n"
            "| 563020 | 0% | **买** | 10% | 120日动量+4.82%、站上MA60，可保留小仓红利低波暴露 |\n"
            "| 511880 | 0% | **不买** | 0% | 一手约占33.5%，与511660优势极小 |\n"
        )
        views = _synthetic(tmp, body)
        v159 = (views.get("159929") or [{}])[0]
        v563 = (views.get("563020") or [{}])[0]
        v511 = (views.get("511880") or [{}])[0]
        check("A2 表头无目标列 ⇒ 目标取方向列下一格（159929 ≠ 9.11%）",
              v159.get("target_pct") == 0.0,
              f"159929 target_pct={v159.get('target_pct')}（理由栏里的 9.11% 不得被当成目标权重）")
        check("A2 表头无目标列 ⇒ 563020 = 10%（不是理由栏的 4.82%）",
              v563.get("target_pct") == 10.0,
              f"563020 target_pct={v563.get('target_pct')}")
        check("A3 方向=不买 且目标格无百分比 ⇒ 目标 0%（511880 ≠ 33.5%）",
              v511.get("target_pct") == 0.0,
              f"511880 target_pct={v511.get('target_pct')}（理由栏里的 33.5% 不得被当成目标权重）")
        check("A4 三行目标权重均不取自理由栏",
              all(v.get("target_pct") not in (9.11, 4.82, 33.5) for v in (v159, v563, v511)),
              f"目标 = {[v.get('target_pct') for v in (v159, v563, v511)]}")

        # A5：**表头列数与行列数不一致** ⇒ 表头角色对不上 ⇒ 同样不许抓到理由栏的百分比
        body2 = (
            "# 合成决策2\n\n## 研究经理\n\n"
            "| ETF | 权重 | 方向 | 理由 |\n"
            "|:--|--:|:--|:--|\n"
            "| 513330 | 7.60% | **卖清** | 0% | 跌破MA60，且与513770主题重复；-26.96%的亏损不是继续持有的理由 |\n"
        )
        views2 = _synthetic(tmp, body2)
        v513 = (views2.get("513330") or [{}])[0]
        check("A5 表头/行列数不符 ⇒ 目标仍非理由栏的 26.96%",
              v513.get("target_pct") not in (26.96, None),
              f"513330 target_pct={v513.get('target_pct')}")
        # ---- A6~A9：2026-11-04 21:5x 独立 reviewer 复现的四个形状 ----
        # A6：理由列叫「**备注**」（旧词表认不出）⇒ 锚定会直读备注格里的 9.11%
        body6 = (
            "# 合成决策6\n\n## 研究经理\n\n"
            "| ETF | 权重 | 方向 | 备注 |\n"
            "|:--|--:|:--|:--|\n"
            "| 159929 | 11.17% | **卖清** | 已被规则列入跌破MA60的强制退出名单；+9.11%的浮盈不能覆盖退出信号 |\n"
        )
        views6 = _synthetic(tmp, body6)
        v6 = (views6.get("159929") or [{}])[0]
        check("A6 理由列叫「备注」⇒ 目标仍是 0%（≠ 9.11%）",
              v6.get("target_pct") == 0.0,
              f"159929 target_pct={v6.get('target_pct')}")

        # A7：整张表**没有表头行** ⇒ 理由列无从识别 ⇒ 锚定直读理由格
        body7 = (
            "# 合成决策7\n\n## 研究经理\n\n"
            "| 159929 | 11.17% | **卖清** | 已被规则列入跌破MA60的强制退出名单；+9.11%的浮盈不能覆盖退出信号 |\n"
        )
        views7 = _synthetic(tmp, body7)
        v7 = (views7.get("159929") or [{}])[0]
        check("A7 无表头行 ⇒ 目标仍是 0%（≠ 9.11%）",
              v7.get("target_pct") == 0.0,
              f"159929 target_pct={v7.get('target_pct')}")

        # A8：**多头主张「512800 加到 25%」**（列数不符 ⇒ 旧实现整行丢弃 ⇒ 漏报重大分歧）
        body8 = (
            "# 合成决策8\n\n## 多头\n\n"
            "| 标的 | 当前权重 | 目标权重 | 方向 | 核心理由 |\n"
            "|:--|--:|--:|:--|:--|\n"
            "| 512800 | 22.54% | 25% | **加仓** | 红利因子仍有超额 | 一手需约2.8万 |\n"
        )
        views8 = _synthetic(tmp, body8)
        v8 = (views8.get("512800") or [{}])[0]
        check("A8 列数不符 ⇒ 多头「加仓到 25%」不得被整行丢弃",
              v8.get("target_pct") == 25.0 and v8.get("direction") == "加仓",
              f"512800 target_pct={v8.get('target_pct')} direction={v8.get('direction')!r}")

        # A9：理由格里**正负两个百分比**（浮亏 26.96% / 浮盈 9.11%）⇒ 不得取成目标权重
        body9 = (
            "# 合成决策9\n\n## 空头\n\n"
            "| ETF | 权重 | 方向 | 备注 |\n"
            "|:--|--:|:--|:--|\n"
            "| 513330 | 7.60% | **卖清** | 浮亏26.96%，但仍有9.11%浮盈，止损优先 |\n"
        )
        views9 = _synthetic(tmp, body9)
        v9 = (views9.get("513330") or [{}])[0]
        check("A9 理由格含正负两个百分比 ⇒ 目标仍是 0%（≠ 26.96 / 9.11）",
              v9.get("target_pct") == 0.0,
              f"513330 target_pct={v9.get('target_pct')}")

        # ---- A14：🔴 2026-11-05 02:5x（M-L）**真 bug 回归** ----
        #    空头表把「机械目标（**规则层**给的数）」与「熊方动作」并排放 ⇒
        #    旧实现把「机械目标」列当成**空头自己的目标** ⇒ 产出
        #    「方向=不买、目标=20%」这种**自相矛盾**的视图 ⇒ 语义守卫必然 FAIL。
        #    修法（final_rebalance_plan.py）：列名带「机械/规则/量化层」的目标列**不登记为 target**；
        #    并新增「方向格自带百分比」路径（仅当**没有**真正的目标列时才生效）。
        body14 = (
            "# 合成决策14\n\n## 空头\n\n"
            "| 代码 | 当前权重 | 机械目标 | 熊方动作 |\n"
            "|:--|--:|--:|:--|\n"
            "| 512800 | 22.54% | 20% | **减至10%左右，上限15%** |\n"
            "| 563020 | 0% | 20% | **不买，0%** |\n"
            "| 511880 | 0% | 20% | **不买，0%** |\n"
        )
        views14 = _synthetic(tmp, body14)
        v14_512 = (views14.get("512800") or [{}])[0]
        v14_563 = (views14.get("563020") or [{}])[0]
        v14_511 = (views14.get("511880") or [{}])[0]
        check("A14 「机械目标」列不得被当成说话人的目标（563020 不买 ⇒ 0%，≠ 20%）",
              v14_563.get("target_pct") == 0.0,
              f"563020 target_pct={v14_563.get('target_pct')}（机械目标列的 20% 是**规则层**的数）")
        check("A14 「机械目标」列不得被当成说话人的目标（511880 不买 ⇒ 0%，≠ 20%）",
              v14_511.get("target_pct") == 0.0,
              f"511880 target_pct={v14_511.get('target_pct')}")
        check("A14 方向格自带百分比 ⇒ 取说话人自己的落点（512800 减至10%，≠ 机械目标 20%）",
              v14_512.get("target_pct") == 10.0,
              f"512800 target_pct={v14_512.get('target_pct')}")
        check("A14 修列角色不得连带丢掉「当前权重」列（512800 current=22.54%）",
              v14_512.get("current_pct") is not None
              and abs(float(v14_512.get("current_pct")) - 22.54) < 0.01,
              f"512800 current_pct={v14_512.get('current_pct')}")

        # ---- A14b：🔴 2026-11-10（LLM 本轮实测写法）**方向词回归用例表** ----
        #    本轮 LLM 在方向格里写了「**买：否**」（= 不买）⇒ 旧解析器
        #    （最左匹配 + 长词优先）取到「买」⇒ 方向=买 + 目标 0%
        #    ⇒ 交付件出现「**方向=买 / 目标=0%**」这种**自相矛盾**的行
        #    ⇒ 交付件自带的语义守卫必然 FAIL（实测本轮 4 行：159207 / 512170 /
        #    515450 / 515650）⇒ 根因在解析器的方向分类，**不在**探针。
        #    ⇒ 这里把本轮的写法**沉淀成探针的方向词回归用例**（不许再退化）。
        #    （探针只**读**解析器：`final_rebalance_plan.py` 由解析器 owner 修。）
        _DIR_CASES = (
            ("买：否", "不买"),   # 🔴 本轮 LLM 实测写法：买：否 = 不买
            ("买", "买"),         # 阳性对照：正常买入方向必须仍然认得出
            ("不买", "不买"),     # 阳性对照：否定式不买必须仍然认得出
        )
        _dir_bad = []
        for _cell, _want in _DIR_CASES:
            _got = F._classify_dir_cell(_cell)
            if _got != _want:
                _dir_bad.append(f"{_cell!r} → {_got!r}（期望 {_want!r}）")
        check("A14b 方向词回归用例：「买：否」必须判成「不买」（LLM 本轮实测写法）",
              not _dir_bad,
              ("违例：" + "；".join(_dir_bad)) if _dir_bad
              else "；".join(f"{_c!r}→{_want!r}" for _c, _want in _DIR_CASES) + " 全对")

        # ---- A15：🔴 2026-11-05 03:0x（M-L 修）**真 bug 回归** + M-N 加固 ----
        #    症状（2026-11-05 02:39 流水线第 8 步 FATAL，产物本身没问题）：
        #    多头表头「标的 | 方向 | 建议权重/金额 | 核心理由」，
        #    512690 行的**理由格**写「未进入目标组合，当前**-18.91%**，退出比继续占用风险预算更合理」
        #    ⇒ 旧正则 `目标[^0-9]{0,6}(-?\d+...)` 的填充字符类 `[^0-9]` **把负号吃掉**
        #    ⇒ 「目标组合，当前-18.91%」被读成**正的 18.91%**
        #    ⇒ 交付件写出「方向=卖、目标=18.91%」这种**自相矛盾**的视图
        #    ⇒ 方向-权重语义自洽守卫必然 FAIL ⇒ 流水线第 8 步 FATAL。
        # 🔴 2026-11-05 04:6x（M-N 修；reviewer C1/C2）：
        #    ① 原第 2 条断言（513330 的 -26.96%）是**空断言** —— 那句理由**不含「目标」**
        #       ⇒ 旧实现同样 PASS ⇒ 证明不了新逻辑在起作用
        #       ⇒ 现已把该句改成含「目标」的写法（⇒ 旧实现会读成 +26.96 ⇒ 能区分新旧）。
        #    ② 补**真负例**：把统一取数函数**回退到旧行为**，断言必须失效。
        body15 = (
            "# 合成决策15\n\n## 多头\n\n"
            "| 标的 | 方向 | 建议权重/金额 | 核心理由 |\n"
            "|---|---|---:|---|\n"
            "| `512690` | **卖** | 清仓，卖出2,000份 | 未进入目标组合，当前-18.91%，退出比继续占用风险预算更合理 |\n"
            "| `513330` | **卖** | 清仓，卖出7,000份 | 未进入目标组合，当前-26.96%不是继续持有的理由 |\n"
        )
        views15 = _synthetic(tmp, body15)
        v15_690 = (views15.get("512690") or [{}])[0]
        v15_330 = (views15.get("513330") or [{}])[0]
        check("A15 理由格里的**负盈亏率**不得变成目标权重（512690 卖 ⇒ 0%，≠ 18.91%）",
              v15_690.get("target_pct") == 0.0,
              f"512690 target_pct={v15_690.get('target_pct')}"
              f"（理由格里的 -18.91% 是**盈亏率**，不是目标权重）")
        check("A15 负盈亏率（-26.96%）同样不得变成目标权重（513330 卖 ⇒ 0%）",
              v15_330.get("target_pct") == 0.0,
              f"513330 target_pct={v15_330.get('target_pct')}")
        # 盈亏率**不带负号**时也要挡住（语境守卫：数字**后**面贴着「浮亏」）
        body15b = (
            "# 合成决策15b\n\n## 多头\n\n"
            "| 标的 | 方向 | 建议权重/金额 | 核心理由 |\n"
            "|---|---|---:|---|\n"
            "| `512690` | **卖** | 清仓，卖出2,000份 | 未进入目标组合，当前18.91%浮亏，退出比继续占用风险预算更合理 |\n"
        )
        views15b = _synthetic(tmp, body15b)
        v15b_690 = (views15b.get("512690") or [{}])[0]
        check("A15 盈亏率**不带负号**时同样不得变成目标权重（语境守卫 ⇒ 0%，≠ 18.91%）",
              v15b_690.get("target_pct") == 0.0,
              f"512690 target_pct={v15b_690.get('target_pct')}")
        # ③ 兜底本身**不得被打死**：真的写了「目标 15%」还是要能取到
        body15c = (
            "# 合成决策15c\n\n## 空头\n\n"
            "| ETF | 方向 | 备注 |\n"
            "|:--|:--|:--|\n"
            "| 512800 | 减 | 建议目标 15%，维持小仓不动其余风险预算 |\n"
        )
        views15c = _synthetic(tmp, body15c)
        v15c_800 = (views15c.get("512800") or [{}])[0]
        check("A15 「目标 X%」兜底**仍须可用**（不得把 ③ 整体打死 ⇒ 15%）",
              v15c_800.get("target_pct") == 15.0,
              f"512800 target_pct={v15c_800.get('target_pct')}")
        # 🔴 **真负例**（reviewer C2）：回退到「负号被吃掉」的旧实现 ⇒ A15 必须失效
        _old_fn_pre = F._extract_target_pct
        try:
            F._extract_target_pct = _old_extract_pre_ML
            _old15 = _synthetic(tmp, body15)
            _old15_690 = (_old15.get("512690") or [{}])[0]
            check("A15 负例：回退「负号被吃掉」的旧实现 ⇒ A15 必须失效（目标变 18.91）",
                  _old15_690.get("target_pct") == 18.91,
                  f"旧实现下 512690 target_pct={_old15_690.get('target_pct')}"
                  f"（必须 = 18.91 ⇒ 证明是新实现修好的，不是「本来就过」）")
        finally:
            F._extract_target_pct = _old_fn_pre

        # ---- A16：**真实官方决策**全行扫描：任何一行的目标权重都不得等于
        #      该行**任何一格**里处于**盈亏/价格/当前语境**的百分数（**正负都算**）
        # 🔴 2026-11-05 04:6x（M-N 修；reviewer C3）：
        #    旧版只扫 reason 格的**负号**百分数 ⇒ ① 抓不到真实决策里空头理由的**正数**
        #    「浮亏26.96%」；② 对产出金标 512800=9% 那行**完全真空**（final 视图 reason 全空）
        #    ⇒ 「守卫真的在守」这件事本身没被证明。
        #    ⇒ 现在按 **PNL 语境**识别该行**所有格子**里的盈亏率（正负都算），
        #      并**报告非真空行数**（非真空行数 = 0 ⇒ 本项**不适用**，不许假装通过）。
        _off_dec = RUN_ROOT / "tradingagents_decision.md"
        if _off_dec.exists():
            _off_views = F._llm_target_views(str(_off_dec))
            _bad_rows = []
            _n_tgt = 0
            _n_vac = 0
            for _code, _lst in _off_views.items():
                for _v in _lst:
                    _pnl = [abs(float(q)) for q in (_v.get("pnl_pcts") or [])]
                    if _pnl:
                        _n_vac += 1
                    _tp = _v.get("target_pct")
                    if _tp is None:
                        continue
                    _n_tgt += 1
                    if any(abs(float(_tp) - _x) < 1e-9 for _x in _pnl):
                        _bad_rows.append(f"{_code}/{_v.get('who')} target={_tp} pnl={_pnl}")
            check("A16 官方决策：目标权重不得等于该行任一「盈亏/价格/当前语境」的百分数"
                  "（正负都算）",
                  (not _bad_rows) and _n_vac > 0,
                  f"有目标的行 {_n_tgt}｜**非真空行 {_n_vac}**（含盈亏语境）｜违例 {_bad_rows}"
                  f"｜非真空行数 = 0 ⇒ 本项**不适用**（不许假装通过）")
        else:
            print(f"[N/A] A16 官方决策不存在（{_off_dec}）⇒ 该项**不适用**")

        # ---- A17（M-N 新增）：**统一取数**的各条口径逐条钉死 ----
        #    ① 「建议 / 计划」类目标列 ⇒ 多头那 3 行**不再被丢**（2026-11-05 03:2x 实测：
        #       多头表头「标的 | 当前权重 | 方向 | 12月1日建议 | 核心理由」⇒
        #       旧实现认不出「12月1日建议」是目标列 ⇒ 那 3 行整行被丢
        #       ⇒ 交付件冲突表**漏掉了多头的声音**）
        #    ② 「万」单位金额 ⇒ 折成百分比（买1手 ≈ 1万元 ⇒ 33.4956%）
        #    ③ 「从 A% 降到/调至 B%」⇒ 取**落点 B**（reviewer A3）
        #    ④ 区间口径 ⇒ 取**下界**（reviewer A4：「目标(区间) 8%—10%」⇒ 8）
        #    ⑤ 「涨跌幅」列 ⇒ 锚定**不许**取它（reviewer B2）
        #    ⑥ 方向格 / 目标格里的**正数**浮盈浮亏 ⇒ 一律挡住（reviewer B1/B2）
        #    ⑦ 「目标收益 20%」「目标从当前 10% 升至 15%」⇒ **合法目标不许被误伤**（reviewer A2）
        body17a = (
            "# 合成决策17a\n\n## 多头\n\n"
            "| 标的 | 当前权重 | 方向 | 12月1日建议 | 核心理由 |\n"
            "|---|---|---|---|---|\n"
            "| **512800** | 22.54% | **减** | 减至约 **20%** | 站上MA60 |\n"
            "| **563020** | 0% | **买** | 条件式建至**20%上限** | 动量+4.82% |\n"
            "| **511880** | 0% | **买** | **只买一手**，约1万元 | 防守资产 |\n"
        )
        v17a = _synthetic(tmp, body17a, 29854.67)
        check("A17 ① 「12月1日建议」列必须被认成目标列（多头那 3 行不再被丢）",
              all(len(v17a.get(c) or []) == 1 for c in ("512800", "563020", "511880")),
              f"views={{code: len(v) for code in v in ('512800','563020','511880')}}"
              f" ⇒ {[(c, len(v17a.get(c) or [])) for c in ('512800', '563020', '511880')]}")
        check("A17 ① 多头 512800 目标 = 20%（「减至约 20%」）",
              (v17a.get("512800") or [{}])[0].get("target_pct") == 20.0,
              f"512800 target_pct={(v17a.get('512800') or [{}])[0].get('target_pct')}")
        check("A17 ① 多头 563020 目标 = 20%（「条件式建至 20% 上限」）",
              (v17a.get("563020") or [{}])[0].get("target_pct") == 20.0,
              f"563020 target_pct={(v17a.get('563020') or [{}])[0].get('target_pct')}")
        check("A17 ② 「约1万元」⇒ 33.4956%（10000 / 29854.67，**万**单位要认）",
              abs(float((v17a.get("511880") or [{}])[0].get("target_pct") or 0.0)
                  - 33.49559717122983) < 1e-6,
              f"511880 target_pct={(v17a.get('511880') or [{}])[0].get('target_pct')}")

        body17b = (
            "# 合成决策17b\n\n## 空头\n\n"
            "| ETF | 方向 | 备注 |\n"
            "|:--|:--|:--|\n"
            "| 512800 | 减 | 目标从 20% 降到 10% |\n"
            "| 563020 | 减 | 目标由 20% 下调至 12% |\n"
            "| 511660 | 减 | 目标(区间) 8%—10% |\n"
        )
        v17b = _synthetic(tmp, body17b)
        check("A17 ③ 「目标从 20% 降到 10%」⇒ 取落点 **10%**（不是 20%）",
              (v17b.get("512800") or [{}])[0].get("target_pct") == 10.0,
              f"512800 target_pct={(v17b.get('512800') or [{}])[0].get('target_pct')}")
        check("A17 ③ 「目标由 20% 下调至 12%」⇒ 取落点 **12%**（不是 20%）",
              (v17b.get("563020") or [{}])[0].get("target_pct") == 12.0,
              f"563020 target_pct={(v17b.get('563020') or [{}])[0].get('target_pct')}")
        check("A17 ④ 区间口径 ⇒ 取**下界 8%**（「目标(区间) 8%—10%」）",
              (v17b.get("511660") or [{}])[0].get("target_pct") == 8.0,
              f"511660 target_pct={(v17b.get('511660') or [{}])[0].get('target_pct')}")

        body17d = (
            "# 合成决策17d\n\n## 空头\n\n"
            "| ETF | 方向 | 涨跌幅 | 理由 |\n"
            "|:--|:--|--:|:--|\n"
            "| 512690 | **卖** | 18.91% | 退出 |\n"
        )
        v17d = _synthetic(tmp, body17d)
        check("A17 ⑤ 「涨跌幅」列**不得**被当成目标权重（18.91% ⇒ 目标 0%）",
              (v17d.get("512690") or [{}])[0].get("target_pct") == 0.0,
              f"512690 target_pct={(v17d.get('512690') or [{}])[0].get('target_pct')}"
              f"（方向=卖 ⇒ 必须 0%，18.91% 是涨跌幅）")
        _old_fn_pnl = F._pnl_column_indices
        try:
            F._pnl_column_indices = lambda cells: set()
            _old17d = _synthetic(tmp, body17d)
            check("A17 ⑤ 负例：回退「排除涨跌幅列」⇒ 必须失效（锚定取到 18.91%）",
                  (_old17d.get("512690") or [{}])[0].get("target_pct") == 18.91,
                  f"旧实现下 512690 target_pct="
                  f"{(_old17d.get('512690') or [{}])[0].get('target_pct')}"
                  f"（必须 = 18.91 ⇒ 证明是本轮修复起的作用）")
        finally:
            F._pnl_column_indices = _old_fn_pnl

        body17e = (
            "# 合成决策17e\n\n## 空头\n\n"
            "| 代码 | 当前权重 | 熊派动作 | 核心理由 |\n"
            "|:--|--:|:--|:--|\n"
            "| 512690 | 2.98% | **卖，浮亏18.91%** | 退出 |\n"
        )
        v17e = _synthetic(tmp, body17e)
        check("A17 ⑥ 方向格里的**正数**浮亏不得变成目标权重（卖，浮亏18.91% ⇒ 0%）",
              (v17e.get("512690") or [{}])[0].get("target_pct") == 0.0,
              f"512690 target_pct={(v17e.get('512690') or [{}])[0].get('target_pct')}")

        body17f = (
            "# 合成决策17f\n\n## 空头\n\n"
            "| 代码 | 方向 | 目标权重 | 核心理由 |\n"
            "|:--|:--|--:|:--|\n"
            "| 512690 | **卖** | 当前18.91%浮盈，减仓退出 | 退出 |\n"
        )
        v17f = _synthetic(tmp, body17f)
        check("A17 ⑥ 目标格里的**正数**浮盈不得变成目标权重（当前18.91%浮盈 ⇒ 0%）",
              (v17f.get("512690") or [{}])[0].get("target_pct") == 0.0,
              f"512690 target_pct={(v17f.get('512690') or [{}])[0].get('target_pct')}")
        _old_fn_nog = F._extract_target_pct
        try:
            F._extract_target_pct = _old_extract_no_guard
            _old17e = _synthetic(tmp, body17e)
            _old17f = _synthetic(tmp, body17f)
            check("A17 ⑥ 负例：回退「无语境守卫」⇒ 必须失效（方向格取到 18.91%）",
                  (_old17e.get("512690") or [{}])[0].get("target_pct") == 18.91,
                  f"旧实现下 方向格 target_pct="
                  f"{(_old17e.get('512690') or [{}])[0].get('target_pct')}")
            check("A17 ⑥ 负例：回退「无语境守卫」⇒ 必须失效（目标格取到 18.91%）",
                  (_old17f.get("512690") or [{}])[0].get("target_pct") == 18.91,
                  f"旧实现下 目标格 target_pct="
                  f"{(_old17f.get('512690') or [{}])[0].get('target_pct')}")
        finally:
            F._extract_target_pct = _old_fn_nog

        body17g = (
            "# 合成决策17g\n\n## 空头\n\n"
            "| ETF | 方向 | 备注 |\n"
            "|:--|:--|:--|\n"
            "| 512800 | 减 | 目标从当前 10% 升至 15% |\n"
            "| 563020 | 减 | 目标收益 20% |\n"
            "| 511660 | 减 | 目标较当前提升至 15% |\n"
        )
        v17g = _synthetic(tmp, body17g)
        check("A17 ⑦ 「目标从当前 10% 升至 15%」⇒ 15%（**不得**被误伤成 None）",
              (v17g.get("512800") or [{}])[0].get("target_pct") == 15.0,
              f"512800 target_pct={(v17g.get('512800') or [{}])[0].get('target_pct')}")
        check("A17 ⑦ 「目标收益 20%」⇒ 20%（**不得**被误伤成 None）",
              (v17g.get("563020") or [{}])[0].get("target_pct") == 20.0,
              f"563020 target_pct={(v17g.get('563020') or [{}])[0].get('target_pct')}")
        check("A17 ⑦ 「目标较当前提升至 15%」⇒ 15%（**不得**被误伤成 None）",
              (v17g.get("511660") or [{}])[0].get("target_pct") == 15.0,
              f"511660 target_pct={(v17g.get('511660') or [{}])[0].get('target_pct')}")
        _old_fn_ml = F._extract_target_pct
        try:
            F._extract_target_pct = _old_extract_ML
            _old17g = _synthetic(tmp, body17g)
            _old17g_vals = [(c, (_old17g.get(c) or [{}])[0].get("target_pct"))
                            for c in ("512800", "563020", "511660")]
            check("A17 ⑦ 负例：回退「目标窗口盈亏词守卫」⇒ 合法目标必须失效（全变 None）",
                  all(v is None for _c, v in _old17g_vals),
                  f"旧实现下 {_old17g_vals}（必须全 None ⇒ 证明「误伤合法目标」是旧行为）")
        finally:
            F._extract_target_pct = _old_fn_ml

        # ---- A18（M-N 新增）：「当前权重→建议权重」**合并格**的箭头语义 ----
        #    🔴 2026-11-05 04:0x 实测事故（流水线第 8 步 FATAL）：
        #       官方决策多头表头是「代码 | 方向 | **当前权重→建议权重** | 核心理由」
        #       ⇒ 单元格是「22.54%→20%」⇒ 旧取数「取**第一个**百分数」
        #       ⇒ 取到 **22.54%（= 当前权重，不是目标）**
        #       ⇒ 方向「减」配目标 22.54% ⇒ 语义守卫 **18 条异常** ⇒ 交付件不可交付。
        #    ⇒ 修法：**箭头后面**的百分数才是目标（箭头前面是「当前」）。
        body18 = (
            "# 合成决策18\n\n## 多头\n\n"
            "| 代码 | 方向 | 当前权重→建议权重 | 核心理由 |\n"
            "|---|---:|---:|---|\n"
            "| `512800` | **减至20%，但继续核心持有** | 22.54%→20% | 最终分最高 |\n"
            "| `159929` | **卖** | 11.17%→0% | 跌破MA60 |\n"
            "| `563020` | **买，但只试仓约10%** | 0%→约10% | 只做试仓 |\n"
            "| `511880` | **买一手** | 0%→约33.5% | 整手约1万元 |\n"
            "| `511660` | **不买，槽位留现金** | 0%→0% | 与511880重复 |\n"
        )
        v18 = _synthetic(tmp, body18)
        _want18 = {"512800": 20.0, "159929": 0.0, "563020": 10.0,
                   "511880": 33.5, "511660": 0.0}
        _got18 = {c: ((v18.get(c) or [{}])[0]).get("target_pct") for c in _want18}
        check("A18 「当前权重→建议权重」合并格 ⇒ 取**箭头后面**的百分数（多头 5 行全对）",
              _got18 == _want18,
              f"got={_got18}｜want={_want18}"
              f"（取第一个百分数会把 22.54%/11.17%/0% 当成目标）")
        _old_fn_arrow = F._extract_target_pct
        try:
            F._extract_target_pct = _old_extract_no_arrow
            _old18 = _synthetic(tmp, body18)
            _old18_512 = ((_old18.get("512800") or [{}])[0]).get("target_pct")
            check("A18 负例：回退「箭头语义」⇒ 必须失效（512800 目标变 22.54% 而不是 20%）",
                  _old18_512 == 22.54,
                  f"旧实现下 512800 target_pct={_old18_512}"
                  f"（必须 = 22.54 ⇒ 证明「箭头后面才是目标」是本轮修复起的作用）")
        finally:
            F._extract_target_pct = _old_fn_arrow

        # ---- A19（M-N 新增）：决策 md 的**表格完整性**（交付件级缺陷回归） ----
        #    🔴 2026-11-05 04:0x 实测：`_tail(..., 2500)` 按字符裁「头 1/3 + 尾 2/3」
        #    ⇒ 官方决策 md 里：多头表被**腰斩**（只剩 3 行 + 1 残行）、
        #      **空头表整表消失**（它正好落在正文中间）
        #    ⇒ **用户读的纪要里根本没有空头的意见**
        #      （结构化解析因为 `_decision_text` 改读 `.json` 才幸免）。
        #    ⇒ 守卫：md 里的**表格块**必须完整、且不得出现半行
        #      （表格行必须整行 ⇒ 解析器不会读到残行假目标）。
        #    🔴 2026-11-05 09:5x（M-R 修）：①②③④ 全部改成**与措辞无关的不变量**
        #       （旧写法把「某几个代码各出现 2 次」「表格行数 == 某常数」抄成了金标
        #        ⇒ LLM 换了表格写法（代码格从反引号变成粗体）就误红
        #        ⇒ 有人会去「更新金标」迁就新输出 ⇒ 真 bug 被盖成假绿）。
        _dec_md = RUN_ROOT / "tradingagents_decision.md"
        _dec_json = RUN_ROOT / "tradingagents_decision.json"
        if not (_dec_md.exists() and _dec_json.exists()):
            print("[N/A] A19 官方决策 md/json 缺失 ⇒ 该项**不适用**（不适用 ≠ 通过）")
        else:
            _md_text = _dec_md.read_text(encoding="utf-8")
            _md_rows = [ln for ln in _md_text.splitlines() if ln.startswith("|")]
            _half = [ln for ln in _md_rows if not ln.rstrip().endswith("|")]
            check("A19① 官方决策 md 的表格行必须**整行**（不得有被腰斩的残行）",
                  not _half,
                  f"表格行 {len(_md_rows)} 行｜残行 {len(_half)} 行：{_half[:2]}")
            # ② **表格块不变量**（**不写死代码集合、不写死行数**）：
            #    含代码的表格块 ≥2 个 + **最大**代码集合出现 ≥2 次
            #    + **≥2 个不同的代码集合**各出现 ≥2 次（多头表 + 空头表都在）
            #    ⇒ 能抓住：`_tail` 把多头表腰斩 / 空头表整表切掉
            #      （⇒ 重复出现的代码集合只剩 1 个 ⇒ 「≥2 个不同集合各 ≥2 次」这条失效）。
            #    🔴 旧判据（重复出现的集合必须**同时是所有块里最大的**）已在 2026-11-09
            #      实测**误红**（9 代码集合 ×3 vs 10 代码集合 ×2）⇒ 判据与理由见
            #      `_md_table_invariant` 的 docstring。
            _inv_ok, _inv_detail = _md_table_invariant(_md_text)
            check("A19② 官方决策 md 必须**同时含多头表与空头表**（表格块不变量，不写死代码/行数）",
                  _inv_ok, _inv_detail)
            # ③ **表格结构自洽**（不写死行数）：每块 ≥2 行 + 第 2 行是**分隔行**
            #    + 表格总行数 == Σ(各块行数)（⇒ 没有游离在块外的孤立表格行 = 腰斩残行）
            #    ⇒ 能抓住：表格块内部被截断（分隔行被切掉）/ 混进非表格行 / 出现孤立残行。
            _struct_ok, _struct_detail = _md_table_structure(_md_text)
            check("A19③ 官方决策 md 的**表格块结构**必须自洽（≥2 行 + 第2行是分隔行 + 总行数 == Σ块行数）",
                  _struct_ok, _struct_detail)
            # 🔴 2026-11-05 11:0x（M-R 修）**整条重写**：改成对 `_tail` **本体**的
            #    **单元级**负例（**永不 vacuous**）。
            #    🔴 旧写法（「旧 `_tail` 渲染 ⇒ 含代码表格块必须**严格少**」）依赖
            #       **真实 md 的体量 / 表格位置** ⇒ 换一轮合法决策就可能不成立
            #       （实测 10:15 那份：新旧渲染都是 4 个含代码块 ⇒ 旧断言误红）。
            #    ⇒ 现在的口径（两个子断言合起来算**一项**）：
            #      (a) **单元级**：自造一段「长前置散文 + markdown 表 + 长后置散文」，
            #          总长 > limit ⇒ 现版 `_tail` 必须**整表保留**（每个表行都在、都是整行），
            #          旧口径（字符裁「头 1/3 + 尾 2/3」）必须**把表切碎**
            #          （丢行或出现半行）⇒ **只测 `_tail` 这个函数**，与任何 md 无关；
            #      (b) **交付级**：交付件的 md 必须 == 用**现版** `_tail` 从决策 json 重渲的结果
            #          （⇒ md 不是旧裁剪口径的遗留件）。
            #    ⇒ 能抓住的缺陷：`_tail` 被改回字符裁剪（表格被腰斩 / 整表消失）
            #      ⇒ 用户读到的纪要里根本没有多空两表 ⇒ 这条立刻红。
            _rta = None
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "_probe_rta", str(ROOT / "llm" / "run_tradingagents.py"))
                _rta = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_rta)
                # (a) 单元级合成夹具（**自造**代码/数字 ⇒ 与任何一份真实决策无关）
                _syn_table = (
                    "| 标的 | 当前权重 | 方向 | 目标权重 |",
                    "|:--|--:|:--|--:|",
                    "| 900101 | 11.11% | 减 | 22.22% |",
                    "| 900102 | 33.33% | 卖 | 44.44% |",
                )
                _syn_text = ("甲" * 500 + "\n\n" + "\n".join(_syn_table)
                             + "\n\n" + "乙" * 500)
                _syn_limit = 600
                _new_out = _rta._tail(_syn_text, _syn_limit)
                _legacy = getattr(_rta, "_head_tail", None) or _legacy_char_crop
                _old_out = _legacy(_syn_text, _syn_limit)
                _new_lines = _new_out.splitlines()
                _old_lines = _old_out.splitlines()
                _keep_ok = all(ln in _new_lines for ln in _syn_table)
                _old_half = [ln for ln in _old_lines
                             if ln.startswith("|") and not ln.rstrip().endswith("|")]
                _chop_ok = ((not all(ln in _old_lines for ln in _syn_table))
                            or bool(_old_half))
                # (b) 交付级：md 必须就是「现版 `_tail`」渲染出来的（不是旧口径遗留件）
                _payload = json.loads(_dec_json.read_text(encoding="utf-8"))
                _rerendered = _rta._render_md(_payload)
                _deliver_ok = (_rerendered == _md_text)
                check("A19④ 负例：`_tail` 改回字符裁剪 ⇒ 表格必被切碎（单元级 + 交付级）",
                      _keep_ok and _chop_ok and _deliver_ok,
                      f"单元级：现版 `_tail` 保留整表={_keep_ok}（合成表 {len(_syn_table)} 行）"
                      f"｜旧口径切碎={_chop_ok}（旧口径剩表行 "
                      f"{sum(1 for ln in _syn_table if ln in _old_lines)}/{len(_syn_table)}"
                      f"，半行 {len(_old_half)} 行）"
                      f"｜交付级：md == 用现版 `_tail` 重渲={_deliver_ok}"
                      f"（重渲 {len(_rerendered)} 字 vs 交付件 {len(_md_text)} 字）")
            except Exception as exc:  # noqa: BLE001 —— 探针自身出错 ⇒ 如实报
                check("A19④ 负例：`_tail` 改回字符裁剪 ⇒ 表格必被切碎（单元级 + 交付级）",
                      False, f"负例执行异常：{type(exc).__name__}: {exc}")
        # ---- A20（M-P 新增）：**Trader 最终裁决**必须进交付件（散文抽取层） ----
        #    🔴 2026-11-05 04:4x 实测：`llm_targets` / `llm_conflicts` **只从表格取数**
        #    ⇒ Trader 的最终裁决写在「最终交易决定」章节的**散文**里（该章节没有表格）
        #    ⇒ 旧实现**完全抽不到** ⇒ 交付件里的「LLM 目标」永远是多头表/空头表的数。
        #    实测最终裁决与两张表**多处不一样**（目标权重 / 方向 / 手数都可能不同）
        #    ⇒ 冲突表按「与规则层差得最远」取行 ⇒ 报出来的是**空头**的数字
        #    ⇒ 用户读到的「LLM 结论」与最终裁决不一致（极端时方向上相反）。
        #    🔴 2026-11-05 09:5x（M-R 修）：③④⑤⑥⑦ 全部改成**与措辞无关的可回放不变量**
        #       （旧写法把「某一份决策的具体数字/手数/代码集合」抄成了金标
        #        ⇒ LLM 换了措辞就误红 ⇒ 有人「更新金标」⇒ 把
        #          「条件买入漏抽」「风险预算取错」两个真 bug 盖成假绿）。
        _plan_path = RUN_ROOT / "final_rebalance_plan.json"
        _plan_md_path = RUN_ROOT / "final_rebalance_plan.md"
        if not (_plan_path.exists() and _dec_md.exists()):
            print("[N/A] A20 官方计划/决策缺失 ⇒ 该项**不适用**（不适用 ≠ 通过）")
        else:
            _plan = json.loads(_plan_path.read_text(encoding="utf-8"))
            _fin = _plan.get("llm_final_decision") or {}
            _md_all = _dec_md.read_text(encoding="utf-8")
            check("A20① 计划必须带 `llm_final_decision` 且 available=True（最终裁决层存在）",
                  bool(_fin.get("available")),
                  f"available={_fin.get('available')}｜note={_fin.get('note')!r}")
            # ② 评级必须与决策原文逐字一致（回放）
            # 🔴 2026-11-09：LLM 有时把评级写成**裸**「Rating: Underweight」（无粗体）
            #    ⇒ 旧正则只认 `**Rating**` ⇒ 判「原文没评级」⇒ 误红。
            #    ⇒ 两种写法都认（粗体可有可无）。
            # 🔴 2026-11-09 补：与被测物 `llm_final._RATING_RE` **同口径**加左词边界
            #    （`(?<![A-Za-z])`）⇒ md 里若先出现「RiskRating:」这类**更长词**，
            #    探针与产品抽到的必须是**同一个**评级值 ⇒ 否则 A20② 会因两边口径不同而误红。
            _rm = re.search(r"\*{0,2}(?<![A-Za-z])Rating\*{0,2}\s*[:：]\s*([^\n*]{1,40})", _md_all)
            _rating_md = _rm.group(1).strip() if _rm else None
            check("A20② 最终裁决评级必须与决策原文的 `Rating` 行逐字一致（粗体可有可无）",
                  bool(_rating_md) and _fin.get("rating") == _rating_md,
                  f"原文={_rating_md!r}｜计划={_fin.get('rating')!r}")
            # ③ **可回放**（不写死任何一个代码/百分数）：
            #    (a) 每个抽出来的目标权重 ⇒ 百分数的**数字串**（15 / 15.0 / 15.00 几种写法都试）
            #        能在**决策 md 原文**里找到 + 该目标的证据句**逐字**命中原文；
            #    (b) 最终裁决章节里出现过的**每一个** 6 位代码，都必须出现在
            #        `targets` 或 `unparsed_codes` 里（**不许整条漏抽**、也不许凭空多出）；
            #    (c) `targets` 至少 1 条（**非空**即可 ⇒ **不要求**「正目标 ≥1」：
            #        合法的一轮裁决可以一个正目标都没有 ⇒ 把「≥1 个正目标」当金标
            #        = 把**上一份决策的形状**抄成标准 ⇒ LLM 换一轮就误红）；
            #    (d) 原文里写了「买 / 条件买入」的代码 ⇒ 必须出现在 `targets` 里
            #        且方向是买类（**不许**只落进 `unparsed_codes` = 整条裁决漏抽）；
            #    (e) 目标权重的**证据句不得是条件句**（方向是「条件买入」除外）
            #        ⇒ 条件句里的数字是**触发器**，不是本轮落点。
            #    ⇒ 能抓住：解析器**编造**原文没有的百分数 / 整条裁决漏抽（老 bug：
            #      「511880 条件买入」整条掉进 `unparsed_codes`）/ 从条件句里取目标权重
            #      （老 bug：把「若…将仓位进一步降至约 10%」当成裁决）。
            #    ⚠ **已知边界**（不是 bug，是取舍）：计划是从**决策 md** 构建的
            #    （`final_rebalance_plan.py` 的 `load_from_decision`）⇒ 证据句本来
            #    就来自 md ⇒ 回放一定命中；万一某轮 md 把「写百分数的那句话」裁掉了，
            #    这条会误红 ⇒ 那时先看 md 里那段是不是被裁掉 ⇒ 那是**渲染件缺内容**
            #    （A19 同一类交付件缺陷），不是解析器造数。
            _t512800 = (_fin.get("targets") or {}).get("512800") or {}
            _ev = (_t512800.get("evidence") or "").strip()
            _tgt_map = _fin.get("targets") or {}
            _unparsed_codes = {str(c) for c in (_fin.get("unparsed_codes") or [])}
            _replay_bad: list[str] = []
            for _c, _t in sorted(_tgt_map.items()):
                _v = _t.get("target_pct")
                if _v is None:
                    continue
                if not _md_number_hit(_md_all, float(_v)):
                    _replay_bad.append(
                        f"{_c}: 目标 {_v}% 的数字串在决策 md 原文里找不到"
                        f"（试过 {_md_number_variants(float(_v))}）")
                    continue
                _ev_t = str(_t.get("evidence") or "").strip()
                if _ev_t and _ev_t not in _md_all:
                    _replay_bad.append(f"{_c}: 证据句不在决策 md 原文里")
            # (b) 最终裁决章节里的每个代码都要有下落（**不许整条漏抽**）
            _sec = LF.extract_section(_md_all) or ""
            _sec_codes = set(_md_codes_in(_sec))
            _dropped = sorted(_sec_codes - set(_tgt_map) - _unparsed_codes)
            # (d) 买类动作词 ⇒ 代码必须在 `targets` 里（**不许**只落进 `unparsed_codes`）
            _buy_dropped: list[str] = []
            for _sentence in _md_sentences(_sec):
                if _md_is_conditional(_sentence):
                    continue
                for _clause in _md_clauses(_sentence):
                    if not _md_has_buy_action(_clause):
                        continue
                    for _c in _md_clause_codes(_clause):
                        _t = _tgt_map.get(_c)
                        if _t is None or str(_t.get("direction") or "") not in ("买", "条件买入"):
                            _buy_dropped.append(
                                f"{_c}（方向={(_t or {}).get('direction')!r}，"
                                f"子句：{_clause[:36]}）")
            # (e) 证据句不得是条件句（条件买入除外）
            _cond_ev: list[str] = []
            for _c, _t in sorted(_tgt_map.items()):
                if str(_t.get("direction") or "") == "条件买入":
                    continue
                _ev_t = str(_t.get("evidence") or "").strip()
                if _ev_t and _md_is_conditional(_ev_t):
                    _cond_ev.append(f"{_c}: 证据句是条件句（{_ev_t[:36]}）")
            check("A20③ 最终裁决必须**可回放**（每个目标能回放 + 每个代码都有下落 + 不取条件句）",
                  (not _replay_bad) and (not _dropped) and (not _buy_dropped)
                  and (not _cond_ev) and bool(_tgt_map),
                  f"目标 {len(_tgt_map)} 条（**非空**即可，不要求正目标）"
                  f"｜最终裁决章节里的代码 {len(_sec_codes)} 个"
                  f"｜回放失败 {_replay_bad[:2]}｜无下落的代码（整条漏抽）{_dropped[:3]}"
                  f"｜买类漏抽 {_buy_dropped[:2]}｜条件句证据 {_cond_ev[:2]}")
            check("A20④ 512800 的证据句必须**逐字**出现在决策 md 里（可回放）",
                  bool(_ev) and _ev[:60] in _md_all,
                  f"证据前 60 字命中={bool(_ev) and _ev[:60] in _md_all}｜{_ev[:80]!r}")
            # ⑤ **可回放**（不写死代码/手数）：
            #    数据侧：每个「买 / 条件买入」的最终裁决 ⇒ 手数必须是 ≥1 的整数，
            #      且**决策 md 原文**里能找到对应的手数写法（**照抄解析器的语法**：
            #      `买 N 手` / `最多 N 手`，中间允许空格或 `**`）。
            #      🔴 **不再要求「至少有一个买入类」**：新一轮合法裁决可以一个买单都没有
            #         （把「必须 ≥1」当金标 = 把**上一份决策的形状**抄成标准 ⇒ 换一轮就误红）。
            #    能力侧（保证**不 vacuous**）：合成一段最终裁决正文（**自造代码**），
            #      同时含「买 1 手」与「最多一手」⇒ `LF.extract_final_decision()`
            #      必须抽出 `lots == 1`（解析器读不出手数 ⇒ 这里红）。
            #    ⇒ 能抓住：手数抽错（原文写一手、计划写两手）/ 手数漏抽 / 解析器读不出手数。
            _buy_codes = [(c, t) for c, t in sorted(_tgt_map.items())
                          if str(t.get("direction") or "") in ("买", "条件买入")]
            _lots_bad: list[str] = []
            for _c, _t in _buy_codes:
                _lots = _t.get("lots")
                if isinstance(_lots, bool) or not isinstance(_lots, int) or _lots < 1:
                    _lots_bad.append(f"{_c}: 手数={_lots!r}（必须是 ≥1 的整数）")
                    continue
                if not _md_lots_hit(_md_all, _lots):
                    _lots_bad.append(f"{_c}: 原文里找不到「{_lots} 手」的写法")
            _syn5 = ("## 六、最终交易决定\n\n**Executive Summary**: "
                     "对 900001 买 1 手；对 900002 条件买入最多一手。\n")
            _r5 = LF.extract_final_decision(_syn5)
            _l5a = ((_r5.get("targets") or {}).get("900001") or {}).get("lots")
            _l5b = ((_r5.get("targets") or {}).get("900002") or {}).get("lots")
            _syn5_ok = (_l5a == 1 and _l5b == 1)
            check("A20⑤ 每个**买入类**最终裁决都必须能回放手数（数据侧）+ 解析器必须读得出手数（能力侧）",
                  not _lots_bad and _syn5_ok,
                  f"买入类目标 {len(_buy_codes)} 个（本轮可以**一个都没有** ⇒ 不判失败）"
                  f"｜回放问题 {_lots_bad[:3]}"
                  f"｜能力侧合成夹具（**自造代码**）：「买 1 手」⇒ lots={_l5a!r}、"
                  f"「最多一手」⇒ lots={_l5b!r}（都必须是 1）")
            # ⑥ **从 md 推导**（不写死代码）：把决策 md「最终裁决章节」里写明
            #    「零方向」（清仓 / 卖出全部 / 不买 / 不建仓）的**代码**收集成集合 S
            #    （**表格方向列** + **逐句散文**两个来源；散文按**子句**归属 ⇒
            #     「清仓 A、B、C」这种**列表**写法要全算上；理由格 / 理由句里的代码不算）。
            #    🔴 **部分卖出不算零方向**：「卖出约 2,600 份」是**减仓**，不是清仓。
            #    ⇒ S 里每个代码：计划里 target_pct == 0.0 + direction ∈ 零方向；
            #      且**不得**掉进 `unparsed_codes`（= 整条漏抽）。
            #    ⇒ 另外：**部分卖出**子句里的代码 ⇒ 计划方向必须是「减」（**不是**卖 / 清仓）。
            #    ⇒ 能力侧（永不 vacuous）：合成一段最终裁决正文（**自造代码**）：
            #      「全部卖出 X」⇒ 卖 / 0%；「卖出约 N 份」⇒ 减（**不是**清仓）。
            #    ⇒ 能抓住：把「卖出」当成带非零目标 / 把清仓方向配上非零权重 /
            #      零方向代码漏抽 / 把「卖出约 N 份」误判成清仓。
            #    ℹ S 为空 ⇒ 说明这份决策的最终裁决里**连一个清仓 / 不买都没写**
            #       ⇒ 该子项**不适用**（不适用 ≠ 失败）；能力侧合成夹具仍然强制
            #       ⇒ 这条**不会**因为「本轮没有零方向代码」而退化成恒真断言。
            _zero_codes, _zero_note = _md_zero_direction_codes(_md_all)
            _unparsed = _unparsed_codes
            _zero_bad: list[str] = []
            for _c in sorted(_zero_codes):
                _t = _tgt_map.get(_c)
                if _t is None:
                    _why = ("掉进 unparsed_codes（整条漏抽）" if _c in _unparsed
                            else "既不在 targets 也不在 unparsed_codes（整条漏抽）")
                    _zero_bad.append(f"{_c}: 原文写明零方向，但计划{_why}")
                    continue
                _v = _t.get("target_pct")
                if _v is None or abs(float(_v)) > 1e-9:
                    _zero_bad.append(f"{_c}: 原文写明零方向，但计划目标={_v!r}（必须 0.0）")
                elif str(_t.get("direction") or "") not in _ZERO_DIRS:
                    _zero_bad.append(
                        f"{_c}: 原文写明零方向，但计划方向={_t.get('direction')!r}"
                        f"（必须 ∈ {list(_ZERO_DIRS)}）")
            _partial_bad: list[str] = []
            for _sentence in _md_sentences(_sec):
                if _md_is_conditional(_sentence):
                    continue
                for _clause in _md_clauses(_sentence):
                    if not _md_has_partial_action(_clause):
                        continue
                    for _c in _md_clause_codes(_clause):
                        _t = _tgt_map.get(_c)
                        _dir = str((_t or {}).get("direction") or "")
                        if _dir != "减":
                            _partial_bad.append(
                                f"{_c}: 原文写的是**部分卖出**（{_clause[:30]}），"
                                f"但计划方向={_dir!r}（必须是「减」，**不是**清仓）")
            _syn6 = ("## 六、最终交易决定\n\n**Executive Summary**: "
                     "全部卖出 900003 回收资金；对 900004 卖出约 1,234 份，保留约 5,678 份。\n")
            _r6 = LF.extract_final_decision(_syn6)
            _t6a = (_r6.get("targets") or {}).get("900003") or {}
            _t6b = (_r6.get("targets") or {}).get("900004") or {}
            _t6a_pct = _t6a.get("target_pct")
            _syn6_ok = (str(_t6a.get("direction") or "") == "卖"
                        and _t6a_pct is not None and float(_t6a_pct) == 0.0
                        and str(_t6b.get("direction") or "") == "减"
                        and _t6b.get("target_pct") is None)
            check("A20⑥ 决策 md 里写明「清仓/不买」的代码 ⇒ 计划里必须零目标 + 零方向（+ 部分卖出≠清仓）",
                  (not _zero_bad) and (not _partial_bad) and _syn6_ok,
                  f"{_zero_note}：{sorted(_zero_codes)}｜违例 {_zero_bad[:3]}"
                  f"｜部分卖出方向违例 {_partial_bad[:3]}"
                  f"｜能力侧合成夹具（**自造代码**）：清仓⇒{_t6a.get('direction')!r}/"
                  f"{_t6a.get('target_pct')!r}、部分卖出⇒{_t6b.get('direction')!r}/"
                  f"{_t6b.get('target_pct')!r}")
            # ⑦ **自洽**（不写死代码集合）：不一致集合 == 计划自己按
            #    「|final_target_pct − rule_effective_pct| > 2.0」算出来的集合；
            #    且 n_divergent == len(该集合)；且每个不一致项 |gap_vs_rule_pp| > 2.0；
            #    且 gap_vs_rule_pp 必须就是 final − rule_eff（对账口径不许漂移）
            #    ⇒ 能抓住：对账口径漂移 / n_divergent 与明细不一致 / gap 字段与明细对不上。
            _recon_rows = _fin.get("reconciliation") or []
            _calc: set[str] = set()
            _gap_bad: list[str] = []
            for _r in _recon_rows:
                _f = _r.get("final_target_pct")
                _e = _r.get("rule_effective_pct")
                _rp = _r.get("rule_target_pct")
                _cmp = _e if _e is not None else _rp      # 与计划自己的口径一致
                if _f is None or _cmp is None:
                    continue
                _d = float(_f) - float(_cmp)
                _rc = str(_r.get("code"))
                if abs(_d) > 2.0:
                    _calc.add(_rc)
                _g = _r.get("gap_vs_rule_pp")
                if _g is not None and abs(float(_g) - _d) > 0.01:
                    _gap_bad.append(f"{_rc}: gap_vs_rule_pp={_g} ≠ final−rule_eff={_d:.4f}")
            _status_set = {str(r.get("code")) for r in _recon_rows
                           if str(r.get("status") or "") == "与规则层不一致"}
            _nd = _fin.get("n_divergent")
            _ok7 = (_calc == _status_set
                    and isinstance(_nd, int) and _nd == len(_calc)
                    and not _gap_bad
                    and all(abs(float(r.get("gap_vs_rule_pp") or 0.0)) > 2.0
                            for r in _recon_rows if str(r.get("code")) in _calc))
            check("A20⑦ 「与规则层不一致」的集合 == 计划自算集合（|final−rule_eff| > 2）且 n_divergent 一致",
                  _ok7,
                  f"自算={sorted(_calc)}｜状态集={sorted(_status_set)}｜n_divergent={_nd}"
                  f"｜gap 口径异常 {_gap_bad[:2]}")
            # ⑧ 真负例①：最终裁决里**没有数字** ⇒ 必须抽不出目标（不许编造）
            _syn1 = ("## 六、最终交易决定\n\n**Rating**: Hold\n\n"
                     "**Executive Summary**: 对 512800 维持现状，其余标的暂不调整。\n")
            _r1 = LF.extract_final_decision(_syn1)
            _s1 = list((_r1.get("targets") or {}).values())
            #    🔴 2026-11-05 04:5x 实测修正：实现是对的、**断言写错了**——
            #    「提到代码但没有任何动作词/数字」⇒ 正确行为是**一个目标都抽不出**
            #    （`targets` 为空）+ 该代码进 `unparsed_codes` ⇒ **不编造**。
            #    原断言要求「至少抽到 1 个代码」，那等于要求实现**必须编一个数出来** ⇒ 反了。
            check("A20⑧ 真负例：最终裁决**不含任何数字** ⇒ 目标权重必须为 None（不许编造）",
                  all(t.get("target_pct") is None for t in _s1)
                  and "512800" in (_r1.get("unparsed_codes") or []),
                  f"抽到 {len(_s1)} 个目标条目｜目标={[t.get('target_pct') for t in _s1]}"
                  f"（必须全为 None）｜unparsed={_r1.get('unparsed_codes')}"
                  f"（必须含 512800 ⇒ 提到了但抽不出 ⇒ 显式记下来，不猜）")
            # ⑨ 真负例②：「目标权重降至约 12%」但**同句没有代码** ⇒ 不得赋给任何代码
            _syn2 = ("## 六、最终交易决定\n\n**Executive Summary**: "
                     "目标权重降至约 12%，保留约 4,100 股。\n")
            _r2 = LF.extract_final_decision(_syn2)
            _bad = [c for c, t in (_r2.get("targets") or {}).items()
                    if t.get("target_pct") is not None or t.get("shares_keep") is not None]
            check("A20⑨ 真负例：权重句里**没有代码** ⇒ 不得把 12% / 4,100 股安给任何代码",
                  not _bad,
                  f"被误赋的代码={_bad}（必须为空）")
            # ⑩ 真负例③：「减仓不清仓」⇒ 方向=减，但目标权重必须 None（不许当清零）
            _syn3 = ("## 六、最终交易决定\n\n**Executive Summary**: "
                     "对 512800 执行\u201c减仓不清仓\u201d。\n")
            _r3 = LF.extract_final_decision(_syn3)
            _t3 = (_r3.get("targets") or {}).get("512800") or {}
            check("A20⑩ 真负例：「减仓不清仓」⇒ 方向=减、目标权重=None（不许当清零）",
                  _t3.get("direction") == "减" and _t3.get("target_pct") is None,
                  f"direction={_t3.get('direction')!r}｜target_pct={_t3.get('target_pct')}"
                  f"（减仓 ≠ 清零 ⇒ 目标必须留空）")
            # ⑪ 计划 MD 必须同时有「最终裁决」章节与「二之三」冲突表章节（编号不撞）
            _plan_md = _plan_md_path.read_text(encoding="utf-8") if _plan_md_path.exists() else ""
            check("A20⑪ 计划 MD 必须同时含「最终裁决」章节与「二之三」冲突表章节（编号不撞）",
                  ("最终裁决" in _plan_md) and ("## 二之三、" in _plan_md)
                  and ("## 二之二、" in _plan_md),
                  f"含最终裁决章节={'最终裁决' in _plan_md}｜含二之二={'## 二之二、' in _plan_md}"
                  f"｜含二之三={'## 二之三、' in _plan_md}")
        # ---- A10~A12：F5 失败判定加固 ----
        empty_md = SCRATCH / "_probe_llm_empty.md"
        fail_md = SCRATCH / "_probe_llm_failed.md"
        ok_md = SCRATCH / "_probe_llm_ok.md"
        try:
            empty_md.write_text("", encoding="utf-8")
            fail_md.write_text(
                "# TradingAgents 决策\n\n"
                "- 失败时间：2026-11-04 22:00:00 CST\n"
                "- 标的：512800 @ 2026-11-03\n"
                "- 异常：`RuntimeError: boom`\n",
                encoding="utf-8",
            )
            ok_md.write_text(
                "# TradingAgents 决策\n\n## 研究经理\n\n"
                "如果这轮 **未完成**，我会先补齐数据再给结论。\n\n"
                "| 标的 | 当前权重 | 方向 | 目标权重 |\n"
                "|:--|--:|:--|--:|\n"
                "| 512800 | 22.54% | 减 | 10% |\n",
                encoding="utf-8",
            )
            r_empty = F._decision_failure_reason(empty_md)
            check("A10 零字节决策文件 ⇒ 判为「没有 LLM 意见」", r_empty is not None,
                  f"_decision_failure_reason={r_empty!r}（修复前是 None ⇒ 谎称有第二意见）")
            r_fail = F._decision_failure_reason(fail_md)
            check("A11 失败件（无「未完成」标记但有「- 异常：」）⇒ 判为「没有 LLM 意见」",
                  r_fail is not None, f"_decision_failure_reason={r_fail!r}")
            r_ok = F._decision_failure_reason(ok_md)
            check("A12 正文含「**未完成**」但首行未标记 ⇒ **不得**误判为失败",
                  r_ok is None, f"_decision_failure_reason={r_ok!r}（修复前是 '决策文件标记为未完成'）")

            # ---- A13：「最终交易决定」章节的归属（M-C）----
            # 🔴 2026-11-04 22:5x 实测缺陷：该章节**未登记**在 `_LLM_SECTIONS`
            #    ⇒ 它的表格行会**沿用上一个说话人**（实测 who='bear'）
            #    ⇒ 交付件把 Trader 的**最终裁决**写成「空头说的」⇒ 归属错误。
            final_md = SCRATCH / "_probe_llm_final.md"
            try:
                final_md.write_text(
                    "# 合成决策3\n\n## 空头\n\n"
                    "| ETF | 当前权重 | 方向 | 目标权重 |\n"
                    "|:--|--:|:--|--:|\n"
                    "| 512800 | 22.54% | **减** | 10% |\n\n"
                    "## 最终交易决定\n\n"
                    "| ETF | 当前权重 | 方向 | 目标权重 |\n"
                    "|:--|--:|:--|--:|\n"
                    "| 512800 | 22.54% | **减** | 15% |\n",
                    encoding="utf-8",
                )
                f_views = F._llm_target_views(str(final_md))
                f512 = f_views.get("512800") or []
                f_bear = [v for v in f512 if v.get("who") == "bear"]
                f_final = [v for v in f512 if v.get("who") == "final"]
                check("A13 「最终交易决定」章节 ⇒ who='final'（不得沿用上一个说话人）",
                      len(f_final) == 1 and f_final[0].get("target_pct") == 15.0,
                      f"views={[(v.get('who'), v.get('target_pct')) for v in f512]}"
                      f"（修复前最终裁决会落成 who='bear'）")
                check("A13b 上一章节仍归 'bear'（归属未被污染）",
                      len(f_bear) == 1 and f_bear[0].get("target_pct") == 10.0,
                      f"bear 视图={[(v.get('who'), v.get('target_pct')) for v in f_bear]}")
            finally:
                if final_md.exists():
                    final_md.unlink()

            # ---- B1~B3：M-K 修的三处解析缺陷，各配一条**真负例** ----
            # 🔴 负例纪律（rules.md §十四.68）：负例必须**真的改到被断言的对象上**。
            #    这里被断言的对象是**解析器的实现** ⇒ 负例直接把实现**回退到旧行为**
            #    （monkeypatch），再断言对应断言**必须失效** ⇒ 证明"是新实现起的作用"。

            # B1 代码格带反引号（`512800`）必须被识别
            tick_md = SCRATCH / "_probe_llm_backtick.md"
            try:
                tick_md.write_text(
                    "# 合成决策B1\n\n## 研究经理\n\n"
                    "| 代码 | 动作 | 调仓后目标 | 具体执行 |\n"
                    "|:--|:--|:--|:--|\n"
                    "| `512800` | **减** | 约10% | 卖出一部分 |\n",
                    encoding="utf-8",
                )
                t_views = F._llm_target_views(str(tick_md))
                t512 = t_views.get("512800") or []
                check("B1 代码格带反引号 ⇒ 必须被解析（修复前整表被静默丢弃）",
                      len(t512) == 1 and float(t512[0].get("target_pct")) == 10.0
                      and t512[0].get("who") == "research",
                      f"views={[(v.get('who'), v.get('target_pct')) for v in t512]}")
                _old_re = F._LLM_CODE_CELL
                try:
                    F._LLM_CODE_CELL = re.compile(r"^\*{0,2}(\d{6}|现金)\*{0,2}$")
                    t_old = F._llm_target_views(str(tick_md)).get("512800") or []
                    check("B1 负例：回退旧正则 ⇒ B1 必须失效（证明是新正则起的作用）",
                          len(t_old) == 0,
                          f"旧正则下 views={t_old}（必须为空）")
                finally:
                    F._LLM_CODE_CELL = _old_re
            finally:
                if tick_md.exists():
                    tick_md.unlink()

            # B2 正文小标题「## 多头所谓的…」不得改变说话人归属
            sub_md = SCRATCH / "_probe_llm_subheading.md"
            try:
                sub_md.write_text(
                    "# 合成决策B2\n\n## 空头\n\n"
                    "## 多头所谓的\"三层验证\"，实际上没有那么独立\n\n"
                    "| ETF | 当前权重 | 方向 | 目标权重 |\n"
                    "|:--|--:|:--|--:|\n"
                    "| 512800 | 22.54% | **减** | 10% |\n",
                    encoding="utf-8",
                )
                s_views = F._llm_target_views(str(sub_md))
                s512 = s_views.get("512800") or []
                check("B2 正文里的「多头…」小标题不得改变说话人归属（应仍为 bear）",
                      len(s512) == 1 and s512[0].get("who") == "bear",
                      f"views={[(v.get('who'), v.get('target_pct')) for v in s512]}")
                _old_fn = F._heading_is_speaker
                try:
                    F._heading_is_speaker = (
                        lambda heading_text, keys: any(heading_text.startswith(k) for k in keys)
                    )
                    s_old = F._llm_target_views(str(sub_md)).get("512800") or []
                    check("B2 负例：回退旧的前缀匹配 ⇒ B2 必须失效（归属会被污染成 bull）",
                          len(s_old) == 1 and s_old[0].get("who") == "bull",
                          f"旧实现下 views={[(v.get('who'), v.get('target_pct')) for v in s_old]}")
                finally:
                    F._heading_is_speaker = _old_fn
            finally:
                if sub_md.exists():
                    sub_md.unlink()

            # B3 目标用「元」表达时必须折成百分比
            yuan_md = SCRATCH / "_probe_llm_yuan.md"
            try:
                yuan_md.write_text(
                    "# 合成决策B3\n\n## 多头\n\n"
                    "| 代码 | 方向 | 调整后目标 | 主要理由 |\n"
                    "|:--|:--|:--|:--|\n"
                    "| 563020 | **买，但仅10%试仓** | 约2,985元 | 动量有效但负E |\n",
                    encoding="utf-8",
                )
                y_views = F._llm_target_views(str(yuan_md), 29854.67)
                y563 = y_views.get("563020") or []
                check("B3 目标用「元」表达 ⇒ 必须折成百分比（10% 试仓不得整行丢弃）",
                      len(y563) == 1 and abs(float(y563[0].get("target_pct")) - 10.0) <= 0.05,
                      f"views={[(v.get('who'), v.get('target_pct')) for v in y563]}")
                _old_fn2 = F._yuan_cell_to_pct
                try:
                    F._yuan_cell_to_pct = lambda cell, total_assets: None
                    y_old = F._llm_target_views(str(yuan_md), 29854.67).get("563020") or []
                    check("B3 负例：回退「元⇒%」折算 ⇒ B3 必须失效（整行被丢弃）",
                          len(y_old) == 0,
                          f"旧行为下 views={y_old}（必须为空）")
                finally:
                    F._yuan_cell_to_pct = _old_fn2
            finally:
                if yuan_md.exists():
                    yuan_md.unlink()

            # ---- B4：当前权重**不得伪造**（M-K 收尾修的真缺陷）----
            # 🔴 2026-11-05 01:3x：旧实现在这一格上有三条造数字的路子
            #    （方向列前一格 ⇒ 读到目标列；行内第一个百分比 ⇒ 读到理由栏；
            #     兜底 current_pct = target_pct ⇒ **直接伪造**）。
            #    第三条的后果是：交付件自带的「方向-权重语义自洽」守卫
            #    按「减 ⇒ 目标 < 当前」判 ⇒ 目标 = 当前 ⇒ **假阳性拦下交付**。
            cur_md = SCRATCH / "_probe_llm_current.md"
            try:
                cur_md.write_text(
                    "# 合成决策B4\n\n## 研究经理\n\n"
                    "| 代码 | 动作 | 调仓后目标 | 具体执行 |\n"
                    "|:--|:--|:--|:--|\n"
                    "| 512800 | **减** | 约10% | 卖出一部分 |\n",
                    encoding="utf-8",
                )
                c_views = F._llm_target_views(str(cur_md))
                c512 = c_views.get("512800") or []
                check("B4 无「当前权重」列且无账户 ⇒ 当前权重必须是 None（未知），"
                      "**不得伪造成目标值 10%**",
                      len(c512) == 1 and c512[0].get("current_pct") is None,
                      f"current_pct={(c512[0].get('current_pct') if c512 else 'N/A')}"
                      f"｜target_pct={(c512[0].get('target_pct') if c512 else 'N/A')}")
                c_acct = F._llm_target_views(str(cur_md), None,
                                             {"512800": 22.541866984294252})
                c512b = c_acct.get("512800") or []
                check("B4 给了账户真值 ⇒ 当前权重 = 22.54%（不是 LLM 表里的目标 10%）",
                      len(c512b) == 1
                      and abs(float(c512b[0].get("current_pct")) - 22.541866984294252) < 1e-9,
                      f"current_pct={(c512b[0].get('current_pct') if c512b else 'N/A')}")

                # 真负例：把实现**回退**成旧行为（判不出 ⇒ 伪造成目标值）
                _old_fn3 = F._resolve_current_pct

                def _legacy_current_pct(code, cells, row_roles, reason_idx, target_idx,
                                        account_weights):
                    # 旧行为：判不出当前权重 ⇒ **直接伪造成目标权重**
                    if target_idx is not None and target_idx < len(cells):
                        _m = F._LLM_PCT_LOOSE.search(cells[target_idx])
                        if _m:
                            return float(_m.group(1))
                    return None

                try:
                    F._resolve_current_pct = _legacy_current_pct
                    c_old = F._llm_target_views(str(cur_md)).get("512800") or []
                    check("B4 负例：回退「当前权重＝目标值」的伪造 ⇒ B4 必须失效",
                          len(c_old) == 1 and c_old[0].get("current_pct") is not None
                          and abs(float(c_old[0].get("current_pct")) - 10.0) < 1e-9,
                          f"旧行为下 current_pct="
                          f"{(c_old[0].get('current_pct') if c_old else 'N/A')}（必须等于 10.0）")
                    c_old2 = F._llm_target_views(
                        str(cur_md), None, {"512800": 22.541866984294252}
                    ).get("512800") or []
                    check("B4 负例：回退伪造 ⇒ 「账户真值优先」也必须失效",
                          len(c_old2) == 1
                          and abs(float(c_old2[0].get("current_pct")) - 10.0) < 1e-9,
                          f"旧行为下 current_pct="
                          f"{(c_old2[0].get('current_pct') if c_old2 else 'N/A')}（必须等于 10.0）")
                finally:
                    F._resolve_current_pct = _old_fn3
            finally:
                if cur_md.exists():
                    cur_md.unlink()

            # ---- B5：另外**两条**造假路径也必须被锁住（reviewer 指出 B4 只覆盖第③条）----
            # 🔴 ① 「方向列**前一格**」：多头表「代码|当前权重|目标权重|机械动作的实质」的
            #    方向列前一格正是**目标列** ⇒ 旧实现会把 20%（目标）当成当前权重
            #    ⇒ 「减 ⇒ 目标 < 当前」恒不成立 ⇒ **又一次假阳性拦交付**，且无人拦截。
            bull_md = SCRATCH / "_probe_llm_bull.md"
            try:
                bull_md.write_text(
                    "# 合成决策B5\n\n## 多头\n\n"
                    "| 代码 | 当前权重 | 目标权重 | 机械动作的实质 |\n"
                    "|:--|--:|--:|:--|\n"
                    "| 512800 | 22.54% | 20% | **仅减持约758.88元，不是清仓** |\n",
                    encoding="utf-8",
                )
                v5 = F._llm_target_views(str(bull_md)).get("512800") or []
                check("B5 ① 方向列前一格正是目标列 ⇒ 当前权重必须取「当前」列 22.54%（不是目标 20%）",
                      len(v5) == 1 and abs(float(v5[0].get("current_pct")) - 22.54) < 1e-6,
                      f"current_pct={(v5[0].get('current_pct') if v5 else 'N/A')}"
                      f"｜target_pct={(v5[0].get('target_pct') if v5 else 'N/A')}")
            finally:
                if bull_md.exists():
                    bull_md.unlink()

            # 🔴 ② 「行内第一个非理由非目标百分比」：理由栏里带百分比时会被读成当前权重
            #    （实测 563020 行「不建20%仓，也不做10%试仓」⇒ 当前权重被读成 20%）。
            reason_md = SCRATCH / "_probe_llm_reason.md"
            try:
                reason_md.write_text(
                    "# 合成决策B5b\n\n## 研究经理\n\n"
                    "| 代码 | 动作 | 调仓后目标 | 具体执行 |\n"
                    "|:--|:--|:--|:--|\n"
                    "| 563020 | **不买** | 0% | 不建20%仓，也不做10%试仓 |\n",
                    encoding="utf-8",
                )
                v5b = F._llm_target_views(str(reason_md)).get("563020") or []
                check("B5 ② 理由栏里的百分比不得被当成当前权重（未知 ⇒ None，不是 20% 也不是 10%）",
                      len(v5b) == 1 and v5b[0].get("current_pct") is None,
                      f"current_pct={(v5b[0].get('current_pct') if v5b else 'N/A')}"
                      f"｜target_pct={(v5b[0].get('target_pct') if v5b else 'N/A')}")
            finally:
                if reason_md.exists():
                    reason_md.unlink()
        finally:
            for _f in (empty_md, fail_md, ok_md):
                if _f.exists():
                    _f.unlink()
    finally:
        if tmp.exists():
            tmp.unlink()

    # ---- 🔴 I1 的**真负例**：把冲突表改坏 ⇒ I1 必须 FAIL（rules.md §十四.68）----
    #    负例必须**真的改到被断言的对象上** ⇒ 这里 deepcopy 计划件后**原地**把
    #    一行改成「方向=不买、目标=20%」（M-L 解析器真 bug 的症状）再喂回判定函数。
    _plan_path = RUN_ROOT / "final_rebalance_plan.json"
    _dec_path = RUN_ROOT / "tradingagents_decision.md"
    if _plan_path.exists() and _dec_path.exists():
        _plan_bad = json.loads(_plan_path.read_text(encoding="utf-8"))
        _views_live = F._llm_target_views(str(_dec_path))
        _rows_bad = _plan_bad.get("llm_conflicts") or []
        if _rows_bad:
            _rows_bad[0]["llm_direction"] = "不买"
            _rows_bad[0]["llm_target_pct"] = 20.0
            _neg_bad = _invariant_failures(_plan_bad, _views_live)
            check("N-I1 负例：把冲突行改成「不买 20%」⇒ I1 不变量必须 FAIL",
                  bool(_neg_bad),
                  f"负例下 I1 违例 {len(_neg_bad)} 条：{_neg_bad[:2]}（必须非空 ⇒ I1 真的会拦人）")
        else:
            print("[N/A] N-I1 负例：冲突表为空 ⇒ 该负例**不适用**（不适用 ≠ 通过）")
    else:
        print("[N/A] N-I1 负例：计划件/决策件缺失 ⇒ 该负例**不适用**（不适用 ≠ 通过）")

    # 🔴 2026-11-04 夜实测修（M-F3；reviewer 指出探针把临时文件写进**官方 run-root**）：
    #    临时目录已挪到临时工作区（`<数据集根>/_work/_probe_llm_parser`），
    #    但旧收尾只 unlink **文件** ⇒ 目录残留 ⇒ 现在整体 rmtree。
    shutil.rmtree(SCRATCH, ignore_errors=True)

    text = "\n".join(lines)
    print("=" * 70)
    print("TradingAgents 冲突表解析器 · 回归探针")
    print("=" * 70)
    print(text)
    print("-" * 70)
    print(f"合计 {len(lines)} 项：PASS {len(lines) - len(failures)} / FAIL {len(failures)}")
    print(f"临时目录：{SCRATCH}（已清理）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
