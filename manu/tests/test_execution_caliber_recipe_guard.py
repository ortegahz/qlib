"""🔴 2026-11-20 新增：执行口径（收盘 / 开盘）**配方完整性守卫**。

【背景：为什么这个守卫必须存在（本轮实测发现，不是推测）】
12-01 实盘前必修 ④ 项里，第 ① 项是「把执行盘口从「收盘」参数化成「收盘 / 开盘」」。
既有配方登记的是 ``final_rebalance_plan.py`` 的 **5 处**（3246 / 3695 / 3697 / 3770 / 3818），
本轮穷举复核结论：

  * 那 5 行**恰好**是全文里字面含「收盘下单 / 收盘执行」的**全部**行
    ⇒ 配方清单是**按窄模式 grep** 得到的
    ⇒ 所有「用『收盘』表达执行时点、但不落在该短语上」的表述**全部漏掉**。
  * 本文件用 **AST 全量字符串字面量抽取**重扫 **6 个交付相关文件**：
    **103 条**含「收盘 / 开盘」的字符串字面量 ⇒ 逐条登记（``REGISTRY``）：
      - ``MUST``   **48 条** = **执行时点口径**（改盘口时**必须**改）
      - ``BASIS``  **29 条** = **价格基准口径**（信号日收盘价仍是折算基准 ⇒ **保留**）
      - ``NEUTRAL`` **26 条** = 与两者都无关（docstring / MA 计算 / 免责声明）
    🔴 **解释器前提**：登记序号按 ``(lineno, end_lineno, col_offset)`` 排序 ⇒ 依赖
    **PEP 701**（**Python ≥ 3.12**）的 f-string 位置信息；低版本（3.8~3.11）下
    f-string 的各片段共用「整条 f-string 的起始位」⇒ 同文件内的序号互换
    ⇒ P2 / M2 报**环境假红**（不是产物回归）⇒ 本模块在 < 3.12 下**整模块 skip**
    （**不弱化任何断言**，只是拒绝在「序号口径本身不成立」的环境上判红）。
  * 🔴 **更严重的一条**：口径不只写在文案里，还有**数据层**源头
    —— ``analyze.py`` 的 ``execution_rule`` / ``execution_note``、
      ``daily/calendar_gate.py`` 的同名两字段
    ⇒ 它们被 ``final_rebalance_plan.py`` **原样注入** md / json ⇒ **只改文案改不干净**。
  * 🔴 **已实测的交付件矛盾**（快照 ``20261119_171532``，实测字符串，非推断）：
      - ``execution_card_20261119.json`` 的 ``execution_basis``
        = 「信号日 T 收盘出信号 → 执行日 T+1 **开盘**执行」
        （来自 ``daily/agents_rebalance.py`` 的常量 ``EXECUTION_BASIS``）
      - 而 ``final_rebalance_plan.md`` / ``.json`` 与 ``etf_analysis_report.md``
        写的是「再于 2026-12-02 **收盘**下单」「真正下单在 12-02 收盘」
      ⇒ **同一份快照里，执行卡说开盘、计划件与研究报告说收盘** ⇒ 交付件自相矛盾。
    这正是 ``daily/daily_config.py:276-279`` 明令
    「执行卡里**必须显式标注**这个差异，**不许混称**」所禁止的状态。

【本文件钉死的 5 件事】
  P1 **不漏点**：6 个交付相关文件里的**每一条**含「收盘 / 开盘」的字符串字面量
     都必须在 ``REGISTRY`` 里登记 ⇒ 新增一条没登记的口径字面量 ⇒ 立刻报红。
  P2 **不漂移**：每条登记项的**文本指纹**必须与源码现状**逐字一致**
     ⇒ 文案被改 / 条目被删 / 顺序变了 ⇒ 报红（强制重新登记）。
  P3 **不误标**：``MUST`` 条目所在的**语句上下文**必须真的在讲**执行时点**
     （下单 / 成交 / 执行 / 复核 / 挂单 / 盯市）⇒ 不许把「价格基准」误登记成
     「必须改」（否则照抄会改错地方）；``BASIS`` 条目必须真的在讲**价格基准**。
  P4 **不半改**：**已知矛盾态**（计划件族 = 收盘 vs 执行卡族 = 开盘）必须与登记一致
     ⇒ 谁只改了一边 ⇒ 报红 ⇒ 强制**显式**重新登记（不许悄悄漂移）。
  P5 **配方覆盖到位**：``MUST`` 集合必须覆盖「计划件 / 数据源 / 节点日历 /
     执行卡 / 挂单清单 / 节点日重算」**六个交付族**（缺一族 = 照抄会漏一族）。

【写法与纪律】
* 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
  两者必须**逐字节一致**）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
* 登记表用「**文件 + 序号**」定位（序号 = 该文件里口径字面量按
  ``(lineno, end_lineno, col_offset)`` 排序后的下标）
  ⇒ **任何**新增 / 删除 / 改写都会让序号漂移 ⇒ 守卫立刻报红 ⇒ 强制重新登记。
* **变异反证**：M1 / M2 / M3 三段，改的是**内存里的源码副本字符串**，
  🔴 **绝不碰**仓库里的生产文件。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

#: 🔴 登记序号依赖 PEP 701（Python ≥ 3.12）的 f-string 位置信息 ⇒ 低版本会「环境假红」
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="登记序号依赖 Python ≥3.12（PEP 701 f-string 位置信息）；低版本下 P2/M2 属环境假红",
)

#: 🔴 **本文件的模块 docstring**（头部计数声明就写在这里 ⇒ P0 拿它当被测对象）
_MODULE_DOC = __doc__ or ""


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/etf_rotation/final_rebalance_plan.py`` 的那一层）。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "etf_rotation" / "final_rebalance_plan.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


PROJECT_DIR = _find_project_dir()

#: 交付相关的 6 个文件（全部口径字面量都在这 6 个文件里）
CALIBER_FILES = (
    "manu/etf_rotation/final_rebalance_plan.py",
    "manu/etf_rotation/daily/agents_rebalance.py",
    "manu/etf_rotation/daily/report.py",
    "manu/etf_rotation/daily/calendar_gate.py",
    "manu/etf_rotation/analyze.py",
    "manu/etf_rotation/reprice_node_day.py",
)

#: 每个交付族**当前**的口径集合（由该族 ``MUST`` 条目里出现的「收盘 / 开盘」决定）
#: 🔴 现状是**已知矛盾态**：计划件 / 数据源 / 节点重算 = 收盘；执行卡 / 挂单清单 = 开盘。
FAMILY_SESSION = {
    "manu/etf_rotation/final_rebalance_plan.py": {"收盘"},
    "manu/etf_rotation/analyze.py": {"收盘"},
    "manu/etf_rotation/daily/calendar_gate.py": {"收盘", "开盘"},
    "manu/etf_rotation/daily/agents_rebalance.py": {"收盘", "开盘"},
    "manu/etf_rotation/daily/report.py": {"开盘"},
    "manu/etf_rotation/reprice_node_day.py": {"收盘"},
}

#: 「执行时点」判定用词（MUST 条目的上下文必须命中其一）
TIMING_RE = re.compile(r"(下单|成交|执行|复核|挂单|盯市)")
#: 「价格基准」判定用词（BASIS 条目的上下文必须命中其一）
BASIS_RE = re.compile(r"(收盘价|折算|基准|重算|节点日价|价日期)")

#: 登记表：(文件, 序号, 归类, 文本指纹前缀)
#: 归类 ∈ {"MUST"（执行时点，必须改）/ "BASIS"（价格基准，保留）/ "NEUTRAL"（无关）}
REGISTRY = (
    # ── 计划件（final_rebalance_plan.py）（32 条口径字面量）
    ("manu/etf_rotation/final_rebalance_plan.py", 0, "BASIS", 'code -> (收盘价, 价日期)；取 asof 及之前的最后一根。'),
    ("manu/etf_rotation/final_rebalance_plan.py", 1, "MUST", '于**执行日 ** 收盘下单'),
    ("manu/etf_rotation/final_rebalance_plan.py", 2, "MUST", '** 收盘下单'),
    ("manu/etf_rotation/final_rebalance_plan.py", 3, "MUST", '**维持当前持仓**（本轮被 TA 一票否决 ⇒ 既不建仓也不卖出；目标股数＝当前股数 股，'),
    ("manu/etf_rotation/final_rebalance_plan.py", 4, "MUST", '）**收盘起按当日价格盯市，至**执行日（'),
    ("manu/etf_rotation/final_rebalance_plan.py", 5, "MUST", '）**收盘前复核（本轮不产生任何买卖动作）'),
    ("manu/etf_rotation/final_rebalance_plan.py", 6, "BASIS", '收盘价'),
    ("manu/etf_rotation/final_rebalance_plan.py", 7, "BASIS", '目标股数按 收盘价 折算；实际在**信号日（）**收盘后按当日价格重新折算整手股数，'),
    ("manu/etf_rotation/final_rebalance_plan.py", 8, "BASIS", '）**收盘后按当日价格重新折算整手股数，'),
    ("manu/etf_rotation/final_rebalance_plan.py", 9, "BASIS", '收盘价（信号日价）'),
    ("manu/etf_rotation/final_rebalance_plan.py", 10, "BASIS", '收盘价（信号日价）'),
    ("manu/etf_rotation/final_rebalance_plan.py", 11, "BASIS", '收盘价（信号日价）'),
    ("manu/etf_rotation/final_rebalance_plan.py", 12, "BASIS", '收盘价（信号日价）'),
    ("manu/etf_rotation/final_rebalance_plan.py", 13, "MUST", '是信号日**（T 日收盘出信号），**实际下单日 ='),
    ("manu/etf_rotation/final_rebalance_plan.py", 14, "MUST", '** 是信号日**（T 日收盘出信号），**实际下单日 = **（T+1 收盘执行）。本文件'),
    ("manu/etf_rotation/final_rebalance_plan.py", 15, "MUST", '**（T+1 收盘执行）。本文件的股数是按 **'),
    ("manu/etf_rotation/final_rebalance_plan.py", 16, "MUST", '收盘价**折算的样例值 ⇒ 须在信号日收盘后按当日收盘价重算整手股数，再于执行日收盘下单；在'),
    ("manu/etf_rotation/final_rebalance_plan.py", 17, "BASIS", '**价格基准**：目标股数一律按 asof（信号日）收盘价折算；账户快照里的「现价」是更晚一'),
    ("manu/etf_rotation/final_rebalance_plan.py", 18, "MUST", '🔴 **信号日 ≠ 执行日**：本文件里的调仓节点是**信号日**（T 日收盘出信号）；按 '),
    ("manu/etf_rotation/final_rebalance_plan.py", 19, "MUST", '**执行日必须重算**：信号日收盘后要按当日收盘价重新折算整手股数，并重新跑一遍信号（LLM'),
    ("manu/etf_rotation/final_rebalance_plan.py", 20, "BASIS", '收盘价算的样例值，执行前必须重算。'),
    ("manu/etf_rotation/final_rebalance_plan.py", 21, "MUST", '⇒ ** 只是信号日，真正下单在 收盘**；本文件的股数与金额是按 ** 收盘价**折算的样'),
    ("manu/etf_rotation/final_rebalance_plan.py", 22, "MUST", '收盘**；本文件的股数与金额是按 **'),
    ("manu/etf_rotation/final_rebalance_plan.py", 23, "BASIS", '收盘价**折算的样例值，**须在'),
    ("manu/etf_rotation/final_rebalance_plan.py", 24, "BASIS", '收盘后按当日收盘价重算整手股数**，再于'),
    ("manu/etf_rotation/final_rebalance_plan.py", 25, "MUST", '收盘下单。'),
    ("manu/etf_rotation/final_rebalance_plan.py", 26, "BASIS", '> ℹ️ **本清单的股数与金额是按 （信号日）收盘价折算的样例值**；'),
    ("manu/etf_rotation/final_rebalance_plan.py", 27, "BASIS", '（信号日）收盘价折算的样例值**；'),
    ("manu/etf_rotation/final_rebalance_plan.py", 28, "MUST", '**信号日 收盘后必须按当日收盘价重算整手股数**，并于**执行日 收盘**下单'),
    ("manu/etf_rotation/final_rebalance_plan.py", 29, "BASIS", '收盘后必须按当日收盘价重算整手股数**，并于**执行日'),
    ("manu/etf_rotation/final_rebalance_plan.py", 30, "MUST", '收盘**下单'),
    ("manu/etf_rotation/final_rebalance_plan.py", 31, "BASIS", '> ⚠️ 但**成交动作仍以规则层 + 真实持仓为准**：最终裁决给的是**目标权重**，*'),
    # ── 执行卡（daily/agents_rebalance.py）（12 条口径字面量）
    ("manu/etf_rotation/daily/agents_rebalance.py", 0, "MUST", '信号日 T 收盘出信号 → 执行日 T+1 开盘执行'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 1, "BASIS", '节点日收盘价 ≠ 执行日成交价，实际股数/金额可能不同'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 2, "NEUTRAL", '⚠️ **样本不足**：MA 需要 根收盘价，实际只有 根 ⇒ **不做破位判定**（不编数'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 3, "NEUTRAL", '根收盘价，实际只有'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 4, "NEUTRAL", '收盘价'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 5, "NEUTRAL", '收盘价 有效跌破 MA（），跌破幅度 ⇒ 触发持仓止损纪律'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 6, "NEUTRAL", '收盘'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 7, "NEUTRAL", '🔴 **紧急避险**： 收盘 < MA （）⇒ 已生成紧急平仓告警（**不自动下单**）'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 8, "NEUTRAL", '✅ **趋势完好**：收盘'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 9, "NEUTRAL", '✅ **趋势完好**：收盘 高于 MA （安全垫 ）'),
    ("manu/etf_rotation/daily/agents_rebalance.py", 10, "NEUTRAL", '**统一流水线**：六阶段全流程 → 落一个**不可变快照** → 确定性执行卡片。 参数 '),
    ("manu/etf_rotation/daily/agents_rebalance.py", 11, "NEUTRAL", '把研究链的动作表折算成**可下单的执行卡片**（含研究链没有的硬约束）。 硬约束（全部来自 '),
    # ── 每日报告（daily/report.py）（3 条口径字面量）
    ("manu/etf_rotation/daily/report.py", 0, "MUST", '## 挂单清单（次日开盘人工执行）'),
    ("manu/etf_rotation/daily/report.py", 1, "BASIS", '按本表在券商 App 手工挂单，参考价 = 信号日收盘价，实际成交价可能不同。'),
    ("manu/etf_rotation/daily/report.py", 2, "BASIS", '🔴 **本表不可下单**：本轮是**观察态**（未开 `--live-trade`）⇒ **'),
    # ── 节点日历（daily/calendar_gate.py）（14 条口径字面量）
    ("manu/etf_rotation/daily/calendar_gate.py", 0, "NEUTRAL", '下一个调仓节点（口径与研究链 `analyze.py::_next_rebalance` 对'),
    ("manu/etf_rotation/daily/calendar_gate.py", 1, "MUST", '收盘出信号、执行日 = 之后第'),
    ("manu/etf_rotation/daily/calendar_gate.py", 2, "MUST", '信号日 收盘出信号、执行日 = 之后第 个交易日（DC.EXECUTION_LAG_DAYS'),
    ("manu/etf_rotation/daily/calendar_gate.py", 3, "MUST", '收盘出信号 ⇒ 执行日'),
    ("manu/etf_rotation/daily/calendar_gate.py", 4, "MUST", '信号日 收盘出信号 ⇒ 执行日 （T+ 个交易日收盘执行）'),
    ("manu/etf_rotation/daily/calendar_gate.py", 5, "MUST", '个交易日收盘执行）'),
    ("manu/etf_rotation/daily/calendar_gate.py", 6, "MUST", '收盘出信号、T+'),
    ("manu/etf_rotation/daily/calendar_gate.py", 7, "MUST", '信号日 收盘出信号、T+ 个交易日收盘执行（DC.EXECUTION_LAG_DAYS=）'),
    ("manu/etf_rotation/daily/calendar_gate.py", 8, "MUST", '个交易日收盘执行（DC.EXECUTION_LAG_DAYS='),
    ("manu/etf_rotation/daily/calendar_gate.py", 9, "MUST", '是锁定的下一正式调仓**信号日**（DC.NEXT_SIGNAL_DATE） ⇒ 收盘出信号'),
    ("manu/etf_rotation/daily/calendar_gate.py", 10, "MUST", '是锁定的下一正式调仓**信号日**（DC.NEXT_SIGNAL_DATE） ⇒ 收盘出信号'),
    ("manu/etf_rotation/daily/calendar_gate.py", 11, "MUST", '开盘）'),
    ("manu/etf_rotation/daily/calendar_gate.py", 12, "MUST", '是上一信号日的**执行日**（DC.NEXT_EXECUTION_DATE） ⇒ 执行卡在*'),
    ("manu/etf_rotation/daily/calendar_gate.py", 13, "MUST", '是上一信号日的**执行日**（DC.NEXT_EXECUTION_DATE） ⇒ 执行卡在*'),
    # ── 研究报告（analyze.py）（26 条口径字面量）
    ("manu/etf_rotation/analyze.py", 0, "NEUTRAL", '🔴 **数据新鲜度守卫**（M-R，2026-11-05 05:2x 新增）。 【为什么必须'),
    ("manu/etf_rotation/analyze.py", 1, "NEUTRAL", '🔴 **ETF 行情数据够不到信号日**：`` 最新只到 ，而信号日是 （数据落后 ****'),
    ("manu/etf_rotation/analyze.py", 2, "NEUTRAL", '** 个自然日） ⇒ 拒绝出报告。 原因：特征层按「`date <= asof` 截断后取最'),
    ("manu/etf_rotation/analyze.py", 3, "NEUTRAL", '🔴 **ETF 行情在信号日当天没有任何 bar**：`` 覆盖到 ，但 ** 当天一只 E'),
    ("manu/etf_rotation/analyze.py", 4, "NEUTRAL", '当天一只 ETF 的 bar 都没有** ⇒ 特征层会退回用**更早的收盘价**算 MA/动'),
    ("manu/etf_rotation/analyze.py", 5, "MUST", '收盘出信号 ⇒ 执行日'),
    ("manu/etf_rotation/analyze.py", 6, "MUST", '信号日 收盘出信号 ⇒ 执行日 （T+ 收盘执行）'),
    ("manu/etf_rotation/analyze.py", 7, "MUST", '收盘执行）'),
    ("manu/etf_rotation/analyze.py", 8, "MUST", 'T 日收盘出信号、T+ 收盘执行（config.EXECUTION_LAG_DAYS=）'),
    ("manu/etf_rotation/analyze.py", 9, "MUST", 'T 日收盘出信号、T+'),
    ("manu/etf_rotation/analyze.py", 10, "MUST", '收盘执行（config.EXECUTION_LAG_DAYS='),
    ("manu/etf_rotation/analyze.py", 11, "NEUTRAL", '收盘价跌破 MA（破位）⇒ 不许新开仓'),
    ("manu/etf_rotation/analyze.py", 12, "NEUTRAL", '收盘价跌破 MA'),
    ("manu/etf_rotation/analyze.py", 13, "NEUTRAL", '新开仓标的收盘价须站上 MA（防守资产豁免；已持有的留给 MA 规则）'),
    ("manu/etf_rotation/analyze.py", 14, "NEUTRAL", '新开仓标的收盘价须站上 MA'),
    ("manu/etf_rotation/analyze.py", 15, "MUST", '成本口径：佣金 bp/边 + 滑点 bp/边（压力情景 bp + bp）；迟滞缓冲 hold'),
    ("manu/etf_rotation/analyze.py", 16, "MUST", '用于压换手，但**换手仍是主要成本项**；人工收盘前挂单的成交偏差（集合竞价、盘口深度、涨跌'),
    ("manu/etf_rotation/analyze.py", 17, "NEUTRAL", '本线用**日线收盘价**计算动量与破位，**未建模**折溢价率、跟踪误差、成分股停牌与指数调'),
    ("manu/etf_rotation/analyze.py", 18, "NEUTRAL", '本线用**日线收盘价**计算动量与破位，**未建模**折溢价率、跟踪误差、成分股停牌与指数调'),
    ("manu/etf_rotation/analyze.py", 19, "MUST", '**（T 日收盘出信号 / T+1 收盘执行，`execution_lag_days='),
    ("manu/etf_rotation/analyze.py", 20, "MUST", '🔴 **执行日 = **（T 日收盘出信号 / T+1 收盘执行，`execution_la'),
    ("manu/etf_rotation/analyze.py", 21, "MUST", '`）⇒ **12-01 只是信号日，真正下单在 12-02 收盘**；股数须按 **12-0'),
    ("manu/etf_rotation/analyze.py", 22, "BASIS", '收盘价算的样例）。'),
    ("manu/etf_rotation/analyze.py", 23, "NEUTRAL", '收盘价跌破 ⇒ 不许新开仓'),
    ("manu/etf_rotation/analyze.py", 24, "MUST", 'T 日收盘信号，T+1 收盘执行'),
    ("manu/etf_rotation/analyze.py", 25, "MUST", '**执行日（T+1 收盘）**'),
    # ── 节点日重算（reprice_node_day.py）（16 条口径字面量）
    ("manu/etf_rotation/reprice_node_day.py", 0, "NEUTRAL", '节点日**股数/金额重算**工具（M-S3，2026-11-05 07:1x）。 【定位：只'),
    ("manu/etf_rotation/reprice_node_day.py", 1, "NEUTRAL", '解析某只标的在节点日用的价格，返回 ``(价格, 价格日期, 提示)``。 口径（M-S3 '),
    ("manu/etf_rotation/reprice_node_day.py", 2, "BASIS", '节点日 当天该 ETF **无 bar**（停牌/未上市） ⇒ 沿用最近一根 收盘价'),
    ("manu/etf_rotation/reprice_node_day.py", 3, "BASIS", '收盘价'),
    ("manu/etf_rotation/reprice_node_day.py", 4, "BASIS", '收盘价（节点日价）'),
    ("manu/etf_rotation/reprice_node_day.py", 5, "BASIS", '收盘价（节点日价）'),
    ("manu/etf_rotation/reprice_node_day.py", 6, "BASIS", '收盘价（节点日价）'),
    ("manu/etf_rotation/reprice_node_day.py", 7, "BASIS", '收盘价（节点日价）'),
    ("manu/etf_rotation/reprice_node_day.py", 8, "NEUTRAL", '按**节点日价格**重算账户总资产：现金 + Σ(**账户全部持仓**股数 × 节点日价)。'),
    ("manu/etf_rotation/reprice_node_day.py", 9, "MUST", '> ⚠️ **节点日收盘价 ≠ 执行日成交价**：股数是「按节点日价折算」，实际在**执行日'),
    ("manu/etf_rotation/reprice_node_day.py", 10, "BASIS", '总资产口径：plan=计划件（信号日快照，默认）｜node=节点日价格口径（现金 + Σ持仓'),
    ("manu/etf_rotation/reprice_node_day.py", 11, "BASIS", 'node（节点日价格口径：现金 + Σ持仓×节点日收盘价）'),
    ("manu/etf_rotation/reprice_node_day.py", 12, "MUST", '🔴 **节点日收盘价 ≠ 执行日成交价**：本清单的股数按 **'),
    ("manu/etf_rotation/reprice_node_day.py", 13, "MUST", '🔴 **节点日收盘价 ≠ 执行日成交价**：本清单的股数按 ** 收盘价**折算，实际在执行'),
    ("manu/etf_rotation/reprice_node_day.py", 14, "BASIS", '收盘价**折算，实际在执行日 **'),
    ("manu/etf_rotation/reprice_node_day.py", 15, "MUST", '收盘**成交 ⇒ 成交股数/金额与本表可能不同。'),
)


def _norm(text: str) -> str:
    """归一化（折叠空白）⇒ 文本指纹可比。"""
    return " ".join(text.split())


def _caliber_entries(src: str) -> list[dict]:
    """抽出源码里**所有**含「收盘 / 开盘」的字符串字面量（AST 全量 ⇒ 注释/docstring 也算）。

    🔴 ``ast.walk`` 会同时产出 f-string 整体（``JoinedStr``）与它的**常量片段**
    ⇒ 二者都是**独立可改**的文案 ⇒ 都必须登记（漏掉片段 = 照抄漏一半）。
    """
    tree = ast.parse(src)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    items: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                p.value for p in node.values
                if isinstance(p, ast.Constant) and isinstance(p.value, str)
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        else:
            continue
        if ("收盘" not in text) and ("开盘" not in text):
            continue
        ctx = node
        holder = parent.get(node)
        if isinstance(holder, ast.JoinedStr):
            ctx = holder
        items.append({
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "col_offset": node.col_offset,
            "ctx": (ctx.lineno, ctx.end_lineno),
            "norm": _norm(text),
        })
    items.sort(key=lambda it: (it["lineno"], it["end_lineno"], it["col_offset"]))
    return items


def _context_text(src: str, span: tuple[int, int]) -> str:
    """取「语句上下文」的文本（登记项所在**整条 f-string / 整行**）。"""
    lines = src.splitlines()
    start, end = span
    return "\n".join(lines[start - 1:end])


def _load_sources(files: tuple[str, ...] = CALIBER_FILES) -> dict[str, str]:
    """读 6 个交付相关文件的源码。"""
    out: dict[str, str] = {}
    for rel in files:
        out[rel] = (PROJECT_DIR / rel).read_text(encoding="utf-8")
    return out


def _declared_counts(doc: str) -> dict[str, int]:
    """从**本文件头部 docstring** 里抠出「声明的计数」（MUST / BASIS / NEUTRAL）。"""
    out: dict[str, int] = {}
    for cls in ("MUST", "BASIS", "NEUTRAL"):
        matched = re.search(r"`*" + cls + r"`*\s*\*\*(\d+)\s*条\*\*", doc)
        out[cls] = int(matched.group(1)) if matched else -1
    return out


def _measured_counts() -> dict[str, int]:
    """``REGISTRY`` 的**实测**归类计数。"""
    return {
        cls: sum(1 for _rel, _idx, kind, _token in REGISTRY if kind == cls)
        for cls in ("MUST", "BASIS", "NEUTRAL")
    }


def _count_defects(declared: dict[str, int], measured: dict[str, int], total: int) -> list[str]:
    """头部声明 vs 实测：逐类比对 ＋ 合计比对。"""
    defects = [
        f"{cls} 头部声明 {declared[cls]} ≠ 实测 {measured[cls]}"
        for cls in sorted(measured)
        if declared[cls] != measured[cls]
    ]
    if total != len(REGISTRY):
        defects.append(f"头部声明合计 {total} ≠ REGISTRY 条数 {len(REGISTRY)}")
    return defects


def _detect_unregistered(
    entries_by_file: dict[str, list[dict]],
    registry: tuple[tuple[str, int, str, str], ...],
) -> list[str]:
    """P1：有没有**没登记**的口径字面量。"""
    registered = {(rel, idx) for rel, idx, _cls, _token in registry}
    defects: list[str] = []
    for rel, entries in entries_by_file.items():
        for idx, ent in enumerate(entries):
            if (rel, idx) not in registered:
                defects.append(
                    f"未登记口径字面量：{rel}#{idx} L{ent['lineno']} 「{ent['norm'][:40]}」"
                )
    return defects


def _detect_drift(
    entries_by_file: dict[str, list[dict]],
    registry: tuple[tuple[str, int, str, str], ...],
) -> list[str]:
    """P2：登记项是否**失效 / 漂移**（文案被改、条目被删、序号越界）。"""
    defects: list[str] = []
    for rel, idx, _cls, token in registry:
        entries = entries_by_file.get(rel) or []
        if idx >= len(entries):
            defects.append(f"登记项失效（条目越界）：{rel}#{idx}")
            continue
        ent = entries[idx]
        if not ent["norm"].startswith(token):
            defects.append(
                f"登记项漂移（文本指纹不符）：{rel}#{idx} L{ent['lineno']} "
                f"期望「{token[:30]}」实际「{ent['norm'][:30]}」"
            )
    return defects


def _detect_mislabel(
    entries_by_file: dict[str, list[dict]],
    sources: dict[str, str],
    registry: tuple[tuple[str, int, str, str], ...],
) -> list[str]:
    """P3：归类是否**误标**（MUST 必须真在讲执行时点；BASIS 必须真在讲价格基准）。"""
    defects: list[str] = []
    for rel, idx, cls, _token in registry:
        entries = entries_by_file.get(rel) or []
        if idx >= len(entries):
            continue
        ent = entries[idx]
        ctx = _context_text(sources[rel], ent["ctx"])
        if cls == "MUST" and not TIMING_RE.search(ctx):
            defects.append(f"归类误标（MUST 但上下文无执行时点语义）：{rel}#{idx} L{ent['lineno']}")
        if cls == "BASIS" and not BASIS_RE.search(ctx):
            defects.append(f"归类误标（BASIS 但上下文无价格基准语义）：{rel}#{idx} L{ent['lineno']}")
    return defects


def _family_sessions(
    entries_by_file: dict[str, list[dict]],
    registry: tuple[tuple[str, int, str, str], ...],
) -> dict[str, set[str]]:
    """P4：每个交付族里 ``MUST`` 条目提到的口径集合。"""
    out: dict[str, set[str]] = {}
    for rel, idx, cls, _token in registry:
        if cls != "MUST":
            continue
        entries = entries_by_file.get(rel) or []
        if idx >= len(entries):
            continue
        norm = entries[idx]["norm"]
        sessions = set()
        if "收盘" in norm:
            sessions.add("收盘")
        if "开盘" in norm:
            sessions.add("开盘")
        if sessions:
            out.setdefault(rel, set()).update(sessions)
    return out


def _entries_by_file(sources: dict[str, str]) -> dict[str, list[dict]]:
    return {rel: _caliber_entries(src) for rel, src in sources.items()}


# =============================================================================
# P0 ~ P5：live 集成断言（对**真实工程目录**跑，不许 skip、不许弱化）
# =============================================================================


def test_P0_本文件头部声明的计数必须与登记表实测一致() -> None:
    """P0：头部不许自报错的计数 —— 声明的 MUST/BASIS/NEUTRAL 条数必须等于 ``REGISTRY`` 实测。

    🔴 **为什么需要**：本轮实测抓到过「头部写 BASIS 34 / NEUTRAL 21、实测却是 29 / 26」
    （等和 ⇒ 掩盖了错配）⇒ 计数类口径**必须机器自证**（`rules.md §十四.127` / `§十四.129`）。
    """
    declared = _declared_counts(_MODULE_DOC)
    measured = _measured_counts()
    defects = _count_defects(declared, measured, sum(declared.values()))
    assert defects == [], "P0 失败：头部计数声明与 REGISTRY 实测不一致\n" + "\n".join(defects)


def test_P1_每条口径字面量都必须已登记() -> None:
    """P1：不漏点 —— 新增一条没登记的口径字面量 ⇒ 必须报红。"""
    sources = _load_sources()
    defects = _detect_unregistered(_entries_by_file(sources), REGISTRY)
    assert defects == [], "P1 失败：存在未登记的口径字面量\n" + "\n".join(defects)


def test_P2_登记项不得失效或漂移() -> None:
    """P2：不漂移 —— 文案被改 / 条目被删 ⇒ 必须报红。"""
    sources = _load_sources()
    defects = _detect_drift(_entries_by_file(sources), REGISTRY)
    assert defects == [], "P2 失败：登记表与源码已漂移\n" + "\n".join(defects)


def test_P3_MUST与BASIS的归类不得误标() -> None:
    """P3：不误标 —— MUST 必须真在讲执行时点、BASIS 必须真在讲价格基准。"""
    sources = _load_sources()
    defects = _detect_mislabel(_entries_by_file(sources), sources, REGISTRY)
    assert defects == [], "P3 失败：登记表归类误标\n" + "\n".join(defects)


def test_P4_已知矛盾态必须与登记一致_半改即红() -> None:
    """P4：不半改 —— 计划件族（收盘）与执行卡族（开盘）的现状必须与登记一致。"""
    sources = _load_sources()
    actual = _family_sessions(_entries_by_file(sources), REGISTRY)
    defects = []
    for rel, expected in FAMILY_SESSION.items():
        got = actual.get(rel, set())
        if got != expected:
            defects.append(f"交付族口径漂移：{rel} 期望 {sorted(expected)} 实际 {sorted(got)}")
    assert defects == [], "P4 失败：已知的「计划件=收盘 vs 执行卡=开盘」矛盾态变了\n" + "\n".join(defects)


def test_P5_MUST必须覆盖六个交付族() -> None:
    """P5：配方覆盖到位 —— 六个交付族每个都至少有一条 MUST（缺一族 = 照抄会漏一族）。"""
    sources = _load_sources()
    entries_by_file = _entries_by_file(sources)
    must_files = {rel for rel, idx, cls, _t in REGISTRY if cls == "MUST"}
    defects = [rel for rel in CALIBER_FILES if rel not in must_files]
    assert defects == [], f"P5 失败：这些交付族没有任何 MUST 条目：{defects}"
    # 登记的 MUST 条目数量必须与实测一致（防登记表里塞空条目）
    for rel in CALIBER_FILES:
        got = sum(1 for r, _i, cls, _t in REGISTRY if r == rel and cls == "MUST")
        assert got > 0, f"P5 失败：{rel} 的 MUST 条目数为 0"


# =============================================================================
# 变异反证：M1 / M2 / M3（改的是**内存里的源码副本字符串**，绝不碰仓库里的生产文件）
# =============================================================================

#: M1 的注入片段：一条**新的**、没登记的口径字面量
_M1_INJECT = '\n_M1_MUTANT = "于执行日 X 收盘下单"\n'
#: M2 的目标片段（改一个字 ⇒ 文本指纹漂移）
_M2_TARGET = "信号日 T 收盘出信号 → 执行日 T+1 开盘执行"
_M2_REPLACEMENT = "信号日 T 收盘出信号 → 执行日 T+1 开市执行"
#: M3 的目标片段（把执行卡族改成收盘 ⇒ 半改 ⇒ 族口径漂移）
_M3_TARGET = "执行日 T+1 开盘执行"
_M3_REPLACEMENT = "执行日 T+1 收盘执行"


def test_变异M1_新增一条没登记的口径字面量_必须报红() -> None:
    """🔴 变异反证 M1：新增一条没登记的口径字面量 ⇒ P1 必须报红。"""
    sources = _load_sources()
    baseline = _detect_unregistered(_entries_by_file(sources), REGISTRY)
    assert baseline == [], "对照组：未变异时必须一条未登记都没有"

    rel = "manu/etf_rotation/daily/agents_rebalance.py"
    mutated = dict(sources)
    mutated[rel] = sources[rel] + _M1_INJECT
    assert mutated[rel] != sources[rel], "M1 变异必须真的改到东西"

    defects = _detect_unregistered(_entries_by_file(mutated), REGISTRY)
    assert defects != [], "M1 变异必须被 P1 抓到（新增口径字面量没登记）"
    assert any("未登记口径字面量" in d for d in defects)


def test_变异M2_改掉登记项的文案_必须报红() -> None:
    """🔴 变异反证 M2：改掉一条已登记字面量的文案 ⇒ P2 必须报红。"""
    sources = _load_sources()
    baseline = _detect_drift(_entries_by_file(sources), REGISTRY)
    assert baseline == [], "对照组：未变异时登记项必须全部有效"

    rel = "manu/etf_rotation/daily/agents_rebalance.py"
    assert sources[rel].count(_M2_TARGET) == 1, "M2 目标片段必须唯一"
    mutated = dict(sources)
    mutated[rel] = sources[rel].replace(_M2_TARGET, _M2_REPLACEMENT)
    assert mutated[rel] != sources[rel], "M2 变异必须真的改到东西"

    defects = _detect_drift(_entries_by_file(mutated), REGISTRY)
    assert defects != [], "M2 变异必须被 P2 抓到（登记项文本指纹漂移）"
    assert any("登记项漂移" in d for d in defects)


def test_变异M3_只把执行卡族改成收盘_必须报红() -> None:
    """🔴 变异反证 M3：只把执行卡族改成收盘（半改）⇒ P4 必须报红。"""
    sources = _load_sources()
    entries_by_file = _entries_by_file(sources)
    baseline = _family_sessions(entries_by_file, REGISTRY)
    for rel, expected in FAMILY_SESSION.items():
        assert baseline.get(rel, set()) == expected, "对照组：未变异时族口径必须与登记一致"

    rel = "manu/etf_rotation/daily/agents_rebalance.py"
    assert sources[rel].count(_M3_TARGET) == 1, "M3 目标片段必须唯一"
    mutated = dict(sources)
    mutated[rel] = sources[rel].replace(_M3_TARGET, _M3_REPLACEMENT)
    assert mutated[rel] != sources[rel], "M3 变异必须真的改到东西"

    actual = _family_sessions(_entries_by_file(mutated), REGISTRY)
    defects = []
    for r, expected in FAMILY_SESSION.items():
        got = actual.get(r, set())
        if got != expected:
            defects.append(f"{r} 期望 {sorted(expected)} 实际 {sorted(got)}")
    assert defects != [], "M3 变异必须被 P4 抓到（半改 ⇒ 族口径漂移）"
    assert any("agents_rebalance" in d for d in defects)


def test_变异M3b_只把计划件族改成开盘_必须报红() -> None:
    """🔴 变异反证 M3b（反向）：只把计划件族改成开盘 ⇒ P4 同样必须报红。"""
    sources = _load_sources()
    rel = "manu/etf_rotation/final_rebalance_plan.py"
    target = "再于执行日收盘下单"
    assert sources[rel].count(target) == 1, "M3b 目标片段必须唯一"
    mutated = dict(sources)
    mutated[rel] = sources[rel].replace(target, "再于执行日开盘下单")
    assert mutated[rel] != sources[rel], "M3b 变异必须真的改到东西"

    actual = _family_sessions(_entries_by_file(mutated), REGISTRY)
    got = actual.get(rel, set())
    assert got != FAMILY_SESSION[rel], "M3b 变异必须被 P4 抓到（半改 ⇒ 族口径漂移）"


def test_变异M4_把头部声明的计数改错_必须报红() -> None:
    """🔴 变异反证 M4：把**头部 docstring 的计数**改错 ⇒ P0 必须报红。

    🔴 变异对象是**内存里的 docstring 副本**（绝不碰仓库里的文件本体）。
    """
    declared = _declared_counts(_MODULE_DOC)
    measured = _measured_counts()
    baseline = _count_defects(declared, measured, sum(declared.values()))
    assert baseline == [], "对照组：未变异时头部计数必须与实测一致"

    mutated = re.sub(
        r"(`*MUST`*\s*\*\*)(\d+)(\s*条\*\*)",
        lambda m: f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}",
        _MODULE_DOC,
        count=1,
    )
    assert mutated != _MODULE_DOC, "M4 变异必须真的改到东西"

    mutated_declared = _declared_counts(mutated)
    defects = _count_defects(mutated_declared, measured, sum(mutated_declared.values()))
    assert defects != [], "M4 变异必须被 P0 抓到（头部计数声明与实测不一致）"
    assert any("MUST" in d for d in defects)
