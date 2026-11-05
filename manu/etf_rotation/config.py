"""ETF 轮动线（人工低频调仓）统一配置。

【与个股线的关系 —— qlib 基本假设不变】
本线**不替代** `manu/experiments/ashare_topk_selection/` 的个股建模：
- 个股层仍是 **Alpha158 + CSRankNorm(VWAP) 标签 + LightGBM**，股票池扩到 **全市场**；
- 超参、处理器、切分**全部从个股线 config 复用**（`from ... import config as STOCK`），
  本文件**不重复定义任何 qlib 建模参数** ⇒ 「不破坏 qlib 基本假设」；
- ETF 层是**下游**：把个股分数 S 经权重矩阵 W 映射成 ETF 分数 E = W × S，
  再做过滤 / 动量 / 防守 / 权重分配。

【用户实际约束（本线设计前提）】
- 持仓 **3~5 只 ETF**，**人工下单**（无交易 API，资金量不支持）；
- **不可能高频** ⇒ 调仓周期固定 **每 10 个交易日（双周）**；
- 只考虑 **ETF**，不选个股；
- 成本口径与个股线**不同**：ETF **免印花税**、佣金约**万 1~万 5**（个股线用万 3 + 印花税）。

【口径六项（rules.md §12.1）】
见本文件各节显式声明；引用任何回测数字必须同时给出：区间 / 标的池 / 成本 / 组合约束 /
基准 / 资金，以及 region。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# 复用个股线 config：qlib 建模参数一律不在这里重复定义（铁律：单一事实源）
# 🔴 必须用 importlib 按**路径**加载：两个 config.py 同名，
#    若用 `import config` 会命中本文件自己（circular import）。
_STOCK_DIR = Path(__file__).resolve().parent.parent / "experiments" / "ashare_topk_selection"
_STOCK_CFG_PATH = _STOCK_DIR / "config.py"
_spec = importlib.util.spec_from_file_location("manu_stock_line_config", _STOCK_CFG_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"无法加载个股线 config：{_STOCK_CFG_PATH}")
STOCK = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(STOCK)

# ---------------------------------------------------------------- qlib 侧（继承个股线）
PROVIDER_URI = STOCK.PROVIDER_URI
REGION = STOCK.REGION
#: 个股建模参数全部沿用个股线（单一事实源，此处只做断言，防止口径漂移）
assert STOCK.LABEL_EXPRESSIONS == ["Ref($vwap, -2)/Ref($vwap, -1) - 1"]
assert STOCK.LGB_PARAMS["random_state"] == 42

#: 🔴 个股打分池：**全市场**（6174 只，含北交所）—— ETF 成分股可能落在 csi300 之外
FULL_UNIVERSE = "all"
#: 打分用的 handler 时间窗：只需覆盖 [首个调仓日 - 预热期, 最后交易日]
#: 预热期 = 80 个交易日（Alpha158 最长回看 60 日，config.py 注释已核）
SCORE_START_TIME = "2023-06-01"
SCORE_END_TIME = "2026-11-03"
#: qlib 股票池在 akshare 里的映射：SH/SZ/BJ
QLIB_EXCHANGE_BY_AK_MARKET = {"101": "SH", "102": "SZ"}

# ---------------------------------------------------------------- ETF 数据（服务器持久盘）
ETF_DATA_ROOT = Path("/mnt/data/siping/datasets/etf")
ETF_SPOT_CSV = ETF_DATA_ROOT / "etf_spot.csv"
ETF_UNIVERSE_CSV = ETF_DATA_ROOT / "etf_universe.csv"
ETF_HOLDINGS_CSV = ETF_DATA_ROOT / "etf_holdings.csv"
ETF_PRICES_CSV = ETF_DATA_ROOT / "etf_prices.csv"
ETF_HOLDINGS_MANIFEST = ETF_DATA_ROOT / "etf_holdings_manifest.json"
ETF_PRICES_MANIFEST = ETF_DATA_ROOT / "etf_prices_manifest.json"

# ---------------------------------------------------------------- 产物落盘
ETF_RUN_ROOT = Path("/mnt/data/siping/runs/etf_rotation")
ETF_RUNS_DIR = ETF_RUN_ROOT / "runs"
#: 个股分数（上游 S）落盘位置
STOCK_SCORES_CSV = ETF_RUN_ROOT / "stock_scores.csv"
#: 权重矩阵 W（ETF → 成分股 → 权重）
ETF_WEIGHTS_CSV = ETF_RUN_ROOT / "etf_weights.csv"
#: 每次调仓建议（JSON + CSV）
RECOMMEND_JSON = ETF_RUN_ROOT / "rebalance_recommendation.json"
RECOMMEND_CSV = ETF_RUN_ROOT / "rebalance_recommendation.csv"
#: ETF 策略回测报告
BACKTEST_JSON = ETF_RUN_ROOT / "etf_rotation_backtest.json"

# ---------------------------------------------------------------- 调仓节奏（人工低频）
#: 每 N 个交易日调仓一次 ⇒ N=10 ≈ 双周（用户明确要求「每周/两周」）
REBALANCE_EVERY_N_DAYS = 10
#: 部署节奏倍数（2026-11-04 夜实测网格定档，铁律 14.9）：10 日网格全负、20 日最优、60 日也负
#: ⇒ 部署用 20 个交易日（月频）。REBALANCE_EVERY_N_DAYS 只是信号日基础步长，
#: 回测的 --rebalance-every N 是它的倍数（N=2 ⇒ 20 个交易日）。
REBALANCE_STRIDE_MULTIPLIER = 2
#: 对外披露口径：实际部署的调仓周期 = 20 个交易日（月频）
DEPLOYED_REBALANCE_EVERY_N_DAYS = REBALANCE_EVERY_N_DAYS * REBALANCE_STRIDE_MULTIPLIER
#: 信号日 → 执行日：**信号用 T 日收盘后数据，执行在 T+1 日收盘**（不引入未来函数）
#:   qlib 的个股分数在 T 日收盘后才能算出；T+1 收盘执行 ⇒ 手工在 T+1 收盘前挂单
EXECUTION_LAG_DAYS = 1

# ---------------------------------------------------------------- 过滤网（doc2 三道 + doc1 动量）
#: 过滤网 1 · 流动性：近 20 个交易日日均成交额下限（元）
MIN_AVG_AMOUNT_20D = 50_000_000.0
#: 过滤网 1 · 规模：ETF 流通市值下限（元）
MIN_FLOAT_MCAP = 200_000_000.0
#: 过滤网 1 · 上市时长：至少多少个交易日历史（供 120 日动量）
MIN_HISTORY_DAYS = 120
#: 过滤网 2 · 同指数去重：同一「跟踪主题」的 ETF 只保留成交额最大的一只
#:   主题由 holdings 成分重合度 + ETF 名称归一化共同判定
DEDUP_OVERLAP_THRESHOLD = 0.90
#: 过滤网 3 · 行业/风格多样性：同一大类最多入选几只
MAX_PER_THEME = 1
#: 组合规模
TARGET_N_HOLDINGS = 5
MIN_N_HOLDINGS = 3

# ---------------------------------------------------------------- 动量 / 趋势特征（doc1）
MOMENTUM_WINDOWS = (20, 60, 120)   # 日动量回看窗口（交易日）
#: 趋势过滤均线（价格在其上方才算「多头」）
MA_TREND_WINDOW = 60
#: 波动率窗口（已实现波动率，用于剔除过山车板块）
VOL_WINDOW = 20
#: 相对强弱基准（ETF 相对该指数的超额）
RS_BENCHMARK = "510300"            # 沪深300ETF
#: 动量权重（横截面打分的动量部分）
MOMENTUM_WEIGHTS = {20: 0.2, 60: 0.5, 120: 0.3}

# ---------------------------------------------------------------- 双重动量防守（doc1）
#: 权益类 ETF 的绝对动量门槛（60 日收益低于此值 ⇒ 视为空头）
ABSOLUTE_MOMENTUM_THRESHOLD = 0.0
#: 防守资产（无 A 股成分 ⇒ 不参与 W×S 映射，只参与动量/防守逻辑）
#: 🔴 **判定必须数据驱动，不能靠名字关键词**（2026-11-04 实测踩坑）：
#:    「黄金**股**ETF」（159562/517520/159321…）是 **A 股权益 ETF**（黄金矿业股），
#:    fund_type_bucket = equity_cn；若按名字含「黄金」判成防守资产，
#:    它会**绕过绝对动量过滤**，于是 mom120 = -32.5% 的标的照样被选进组合。
DEFENSIVE_CODES = ("518880", "511010", "511090", "511260", "511020")
#: 防守资产判定：① fund_type_bucket ∈ DEFENSIVE_BUCKETS（货币/现金替代）
DEFENSIVE_BUCKETS = ("money_market",)
#: ② 实物黄金（commodity 桶里名字含这些的才是**现货金**，才是防守资产）
SPOT_GOLD_KEYWORDS = ("黄金ETF", "金ETF", "上海金")
#: ③ 商品**期货** ETF（有色/豆粕/能源化工…）是**风险资产**，不算防守资产
COMMODITY_FUTURES_KEYWORDS = ("有色", "豆粕", "能源化工", "商品", "原油", "煤炭", "白银", "股指")
#: 旧的名字关键词表（保留仅供日志对照，**不再用于判定**）
DEFENSIVE_NAME_KEYWORDS = ("黄金", "国债", "债", "货币", "现金")

# ---------------------------------------------------------------- 个股分数 → ETF 分数 的融合权重
#: E = W × S 得到的「个股 alpha 暴露分」，与 ETF 自身动量分如何合成
ALPHA_SCORE_WEIGHT = 0.5      # 个股映射分（doc2）
MOMENTUM_SCORE_WEIGHT = 0.5    # ETF 自身动量分（doc1）
#: 个股分数的横截面归一化方式（在每个调仓日，对全市场个股打分做 z-score）
STOCK_SCORE_NORMALIZE = "zscore"

# ---------------------------------------------------------------- 换手控制（迟滞）
#: 已持有的标的，只要排名还在「目标只数 + 缓冲区」之内就**继续持有**（不换手）
#:   ⇒ 把「每期全换」变成「只在明显掉出前列时才换」，是人工低频策略能落地的关键
HOLD_BUFFER = 2
#: 缓冲区内允许的最大持有只数（= 目标只数 + 缓冲）
MAX_N_HOLDINGS_BUFFERED = TARGET_N_HOLDINGS + HOLD_BUFFER

# ---------------------------------------------------------------- ETF 成本口径（与个股线不同！）
#: 场内 ETF：**免印花税**、免过户费，佣金按券商实际（万 1 ~ 万 5）
ETF_COMMISSION = 0.00015        # 单边佣金 1.5bp（万 1.5），取区间中值
ETF_MIN_COMMISSION = 0.0        # 多数券商 ETF 免最低 5 元
#: 冲击/滑点基准（人工下单在收盘集合竞价，成交价贴近收盘价，但小资金仍有滑点）
ETF_SLIPPAGE = 0.0005           # 单边 5bp
#: 压力情景（用于敏感性分析）：佣金万 5 + 滑点 20bp
ETF_COMMISSION_STRESS = 0.0005
ETF_SLIPPAGE_STRESS = 0.0020
#: 基准：沪深300ETF（风险资产基准；防守策略的超额要相对它算才有意义）
ETF_BENCHMARK = "510300"
#: 初始资金
#: 🔴 2026-11-04 已按**用户真实账户**更新：总资产 29,854.67 元、现金 15,518.87 元
#:    （真实账户口径的**单一事实源**见 `manu/etf_rotation/account.py`）
ETF_ACCOUNT = 29_854.67         # 个人量级（小资金，人工下单）

# ================================================================
# v2 参数（2026-11-04 晚 · 依据「带刹车版本」复盘）
#   背景：v1 回测 MDD -39.31%、当前 -35.96% 回撤中、绝对动量(mom120>0)在急跌里
#         一次都不触发 ⇒ 没有刹车；且同质化去重漏掉「红利 vs 红利低波」。
# ================================================================

# ---------------------------------------------------------------- 1. 标的池属性规范化
#: 标的池模式：
#:   "e_covered" —— 只允许**有 A 股成分且覆盖率达标**的 ETF 入池
#                   ⇒ 保证**每只持仓都真的过了 E=W×S 穿透**（v1 的商品/跨境 ETF
#                      coverage=0 ⇒ E 算不出来，等于绕过了整个个股建模机制）
#:   "all"       —— v1 行为（商品/跨境/货币也能入池，只能靠自身动量入选）
POOL_MODE = "e_covered"
#: E 的最低覆盖率（低于此值 ⇒ E 不可信 ⇒ 不入池；对应 map_scores_to_etfs 的 min_coverage）
MIN_E_COVERAGE = 0.5
#: 「宏观袖套」最多占几席（无 A 股成分的 ETF：商品 / 跨境 / 货币）。
#:   仅在 POOL_MODE="all" 时生效；0 = 不限。
MACRO_SLEEVE_MAX_POSITIONS = 2

# ---------------------------------------------------------------- 2. 同质化去重升级
#: 主题聚类的相关性口径：近 N 个交易日的**日收益相关性** ≥ 阈值 ⇒ 同一主题。
#:   实测：红利低波(易方达) vs 红利低波(华泰柏瑞) corr120=0.983；
#         红利ETF(华泰柏瑞) vs 红利ETF(易方达) corr120=0.971 —— 这两组**持仓重叠都 <0.90**
#         ⇒ 光靠重叠判据完全漏掉 ⇒ 必须加相关性。
THEME_CORR_WINDOW = 120
#: 🔴 阈值不能太低：实测 A 股板块 ETF 之间 corr120 普遍 0.6~0.88
#:    （红利低波 563020 vs 银行 515020 = **0.875**）⇒ 阈值 0.80 会把「红利低波」和「银行」
#:    误并成同主题 ⇒ MAX_PER_THEME=1 时组合被压成 1 只。
#:    0.95 只抓「真重复」：红利ETF华泰柏瑞 vs 红利ETF易方达 = 0.971、
#:    红利低波易方达 vs 红利低波华泰柏瑞 = 0.983；
#:    而「红利 vs 红利低波」(0.787) 由**名称族**规则兜住。
THEME_CORR_THRESHOLD = 0.95
#: 🔴 **同族内「已持有品种优先」**（2026-11-04 晚新增，可 A/B 关闭）
#:   同一族里的两只 ETF 暴露几乎相同（例：512800 银行ETF华宝 vs 515020 银行ETF华夏，
#:   同跟踪中证银行指数）⇒ 在同族内部按分数换手**不改变组合暴露，只增加摩擦成本**。
#:   实测：2026-11-03 截面里 515020 最终分 1.6219 略高于 512800 的 1.6027
#:   ⇒ 原逻辑会把用户持有的 512800 换成 515020（同族内无意义换手 + 交易成本）。
#:   打开后：同族占位者若不是已持有品种，而同族存在已持有品种 ⇒ 让位给已持有的。
#:   ⚠️ 只影响**同族内部**的取舍，不改变入选只数、不改变族结构。
THEME_PREFER_HELD = True
#: 名称族互斥：同一「族」的 ETF 最多入选 1 只（MAX_PER_THEME 仍为 1）。
#:   🔴 为什么必须加这条：红利ETF(510880) vs 红利低波(563020) —— 持仓重叠 0.325、
#      corr120 0.787 ⇒ **两种数据判据都抓不到**，但二者本质都是「红利风格」，
#      同时满仓等于把 40% 押在同一个风格上。
#:   规则：ETF 名称命中任一关键词 ⇒ 归入该族；两 ETF 的族集合**有交集** ⇒ 同一主题。
THEME_FAMILY_KEYWORDS = {
    "红利": ("红利",),
    "银行": ("银行",),
    "券商": ("券商", "证券"),
    "医药": ("医药", "创新药", "医疗", "生物医药"),
    "半导体": ("半导体", "芯片"),
    "军工": ("军工", "国防"),
    "新能源": ("光伏", "电池", "新能源"),
    "资源": ("有色", "稀土", "煤炭", "钢铁", "资源"),
    "地产": ("地产", "房地产"),
    "消费": ("食品", "白酒", "消费"),
    "通信": ("通信", "云计算", "人工智能"),
    "机器人": ("机器人",),
    "电力": ("电力", "公用", "基建"),
    "农业": ("农业", "养殖"),
    "港股": ("港股",),
    "纳指": ("纳指",),
    "标普": ("标普",),
    "日经": ("日经",),
}

# ---------------------------------------------------------------- 3. 风控刹车（v2 核心）
#: 入场硬过滤：ETF 收盘价必须站上该均线，否则**不得入选**（0 = 关闭该过滤）。
#:   v1 只有「跌破 MA60 ⇒ 动量分打 0.5 折」的软惩罚 ⇒ 急跌里照样买进破位标的。
ENTRY_MA_FILTER_WINDOW = 20          # 20 日线破位 ⇒ 不许新开仓
#: **持仓**破位强制退出：已持仓的 ETF 跌破该均线 ⇒ 在下次调仓无条件卖出（不看排名）
EXIT_MA_FILTER_WINDOW = 60           # 60 日线破位 ⇒ 无条件换掉
#: 组合级回撤刹车：组合净值自**历史峰值**回撤 ≥ 阈值 ⇒ 整个组合切到防守资产
#:   （黄金 ETF / 货币 ETF），而不是继续在权益里挨打。
DRAWDOWN_BRAKE_THRESHOLD = 0.15      # 回撤 15% ⇒ 触发
#: 刹车触发后至少保持防守的调仓期数（冷静期，防止刚触发就又被打回权益）
DRAWDOWN_BRAKE_COOLDOWN = 1
#: 解除条件：净值回到「峰值 × (1 - 该值)」以内才允许恢复权益（默认离峰值 5%）
DRAWDOWN_BRAKE_RELEASE = 0.05
