"""🔴 V3 唯一配置源（2026-11-26 起）—— **S 层「对齐式重构」研发分支**。

**V3 是什么**：一条**研发分支**（R&D branch），用来验证「把 S 层打分子系统与月频下游
对齐」到底能不能换来端到端收益。🔴 **V3 不替换、不改写、不接管 V2**：V2 是现役口径，
它的所有文件与产物**原样保留**，V3 只能**读**、不能**写**。

**V3 的五个对齐方向（用户 2026-11-26 目标文档）**

| # | 对齐项 | V2 现状 | **V3 口径** |
| --- | --- | --- | --- |
| ① | **预测标签** | 20 日窗口收益（t+15~t+25 均价退出），**只做 CSRankNorm** ⇒ 基准项被抹平成逐日常数（数值中心化等于没做） | **t+15~t+25 均价退出收益（≈20 日持有） − 当日基准收益（数值层面中心化，保留数值超额）** ⇒ 训练信号里真的有「超额」 |
| ② | **训练股票池** | `csi300`（≈1227 只，与 ETF 池无直接关系） | **候选 ETF 的成分股并集**（`etf_constituent_union`）｜按个股在 ETF 池中的总权重 **ΣW 做样本加权** |
| ③ | **特征预处理** | 只做时序 ZScoreNorm，**无任何截面中性化** | **规模档（size tier）残差化 ＋ 流动性残差化**；🔴 **绝不做行业中性化**（会把行业内均值拉成 0 ⇒ `E = W × S` 塌缩） |
| ④ | **模型结构 / 滚动重训** | LightGBM **MSE**，无排序损失；训练段一次性 | LightGBM **lambdarank（成对排序损失）** ＋ 强正则（`max_depth=5` 等）＋ **季度滚动重训** |
| ⑤ | **验收闭环** | — | **单变量对照**：V2 口径 = 对照臂，V3 逐项对齐 = 实验臂；下游 **V2 ETF 层参数一字不改** |

**🔴 硬隔离契约（与 `v2/__init__.py` 同款，但方向反过来）**

* 本包**不 import、不改写** V1 的 `manu/etf_rotation/{config,backtest,qlib_scores}.py`
  ＋ `manu/etf_rotation/engine/*`（V1 已被 `v2/probes/v1_frozen_md5.json` 锁死）；
* 本包**不 import、不改写** V2 的任何文件 ⇒ V3 只允许**只读**调用 V2 的 ETF 层
  （`etf_layer.build_targets` / `backtest_v2.run_backtest_v2` / `metrics.summarize`）
  用来做「端到端验收」，**只在运行时导入、只传数据、不写 V2 目录**；
* 本包**不写** `<RESULTS_ROOT>/` 根目录、不写 `<RESULTS_ROOT>/v2/`、不写
  `<RESULTS_ROOT>/reports/` ⇒ V3 产物**只落** `<RESULTS_ROOT>/v3/`（铁律〇之二：产物不得互相覆盖）；
* 本包**不改** `manu/etf_rotation/daily/`（每日链）—— 每日链接 V3 是**另一件事**
  （须用户当轮授权），V3 只在研究侧存在。

**产物命名**：分数文件 `stock_scores_v3_<model_name>.csv`（**不得**叫 `stock_scores.csv`
——那是 V1 canonical；**不得**叫 `stock_scores_v2_*.csv` —— 那是 V2 的）。

**版本标签**：:data:`V3_MODEL_VERSION_TAG` = `` "V3" `` 是写进 snapshot manifest /
报告口径表的版本标识（见 `manu/memory/rules.md §十四.140`）。
"""

from __future__ import annotations

import math
from pathlib import Path

#: 🔴 **V3 版本标识**（写进每个产物的 manifest / 报告标题 ⇒ 不可能与 V1/V2 混淆）
V3_VERSION = "v3"
V3_LABEL = "ETF 穿透评分 V3 对齐版（研发分支）"
#: 🔴 **写进 snapshot manifest / 报告口径表的版本标签**（V2 现役 = ``"V2"``）
V3_MODEL_VERSION_TAG = "V3"
#: 🔴 **V2 现役版本标签**（供对照与缺字段兜底用）
V2_MODEL_VERSION_TAG = "V2"

# ----------------------------------------------------------------- 落点根
#: 🔴 **V3 产物根**：与 V1（结果根根目录）/ V2（`v2/`）**物理隔离**（rules.md §12.8）
#:    ⇒ V3 = `<RESULTS_ROOT>/v3/`。
DEFAULT_V3_RUN_ROOT = Path("/mnt/data/siping/runs/etf_rotation_results/v3")
#: V3 代码根（本包）
V3_ROOT = Path(__file__).resolve().parent
#: 🔴 V2 代码根（**只读引用**，用于端到端验收时调用 V2 的 ETF 层）
V2_ROOT = V3_ROOT.parent / "v2"
#: V1 研究链根（**只读引用**；🔴 其中 7 个文件被 `v1_frozen_md5.json` 锁死 ⇒ 永不改）
V1_ROOT = V3_ROOT.parent

# ----------------------------------------------------------------- 数据源（只读）
#: qlib 数据集（与 V1/V2 同源）
QLIB_CN_DATA = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/qlib/cn_data")
QLIB_CN_DATA_LIVE = QLIB_CN_DATA.parent / "cn_data_live"
DEFAULT_PROVIDER_URI = str(QLIB_CN_DATA_LIVE)
#: 交易日历
CALENDAR_FILE = QLIB_CN_DATA / "calendars" / "day.txt"
#: 🔴 **qlib 取数并行度上限**（进程数）。
#:    qlib 的 ``StaticDatasetProvider.fetch`` 会按 ``C["kernels"]``（默认＝**可用核数**）
#:    用 ``backend="multiprocessing"`` **fork** 出等量子进程 ⇒ 80 核机器上单次取数
#:    fork **78** 个子进程，每个都继承父进程的全部内存（COW）⇒ **内存爆炸**
#:    （实测 5 折并发时整机 used 冲到 **111 GB / 125 GB**，available 只剩 12 GB）。
#:    ⇒ 这里把取数并行度**锁死在小值**（**只改并行度、不改任何数据/口径**）。
QLIB_FETCH_KERNELS = 4
#: 🔴 **指数档位成分表**（带**日期区间** ⇒ 可还原「时变规模档」）
#:    文件格式：`SH600000\t2005-04-08\t2026-11-20`（qlib 原生 instruments 格式）
QLIB_INSTRUMENTS_DIR = QLIB_CN_DATA_LIVE / "instruments"
SIZE_TIER_INSTRUMENT_FILES = {
    "csi300": QLIB_INSTRUMENTS_DIR / "csi300.txt",
    "csi500": QLIB_INSTRUMENTS_DIR / "csi500.txt",
    "csi1000": QLIB_INSTRUMENTS_DIR / "csi1000.txt",
}
#: ETF 成分股/权重表（🔴 **只有一个快照日** 2026-11-04，历史成分变更不可观测）
HOLDINGS_CSV = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/etf_holdings.csv")
#: 🔴 **候选 ETF 池**（每日链 `daily_config.ETF_UNIVERSE_CSV` 同源）
ETF_UNIVERSE_CSV = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/etf_universe.csv")
#: 成分股快照日（与 V2 同源口径，披露用）
HOLDINGS_SNAPSHOT_DATE = "2026-11-04"

# ----------------------------------------------------------------- ① 标签对齐
#: 退出窗口（相对 t 的偏移闭区间）—— 与 V2 同口径（t+15 ~ t+25 窗口均价）
LABEL_EXIT_WINDOW = (15, 25)
#: 标签窗口末端相对 t 的偏移 ⇒ embargo 的下限
LABEL_HORIZON_DAYS = LABEL_EXIT_WINDOW[1]
#: 价格字段（与 V1/V2 同口径：VWAP）
LABEL_PRICE_FIELD = "$vwap"
#: qlib 表达式（**原始窗口收益**，🔴 不含任何中心化 —— 中心化在 pandas 侧做）
LABEL_EXPRESSION = "Ref(Mean($vwap,11),-25)/$vwap-1"
#: 🔴 **V3 的中心化口径**（与 V2 的最大差别之一）：
#:    * ``"benchmark"`` ⇒ 减基准（沪深300）同窗口收益（V2 同口径，但 V2 用 CSRankNorm
#:      把这一步**抹平**了 ⇒ V2 的「超额」只存在于评估层）；
#:    * ``"market_mean"`` ⇒ 减**当日训练池截面均值**（ETF 成分池内；更贴「跑赢 ETF 成分池均值」的用户表述）。
#:    两者都**保留数值超额**（不转成排名）⇒ 排序损失才有「超额」可排。
LABEL_CENTERING = "market_mean"
#: 基准（用于披露与备选口径）
BENCHMARK = "sh000300"
#: 🔴 **标签后处理**：V3 **只做 DropnaLabel**，🔴 **不做 CSRankNorm**
#:    （做了就把数值超额抹成排名 ⇒ ①② 两个对齐同时失效）
LABEL_LEARN_PROCESSORS = [
    {"class": "DropnaLabel", "kwargs": {}},
]
LABEL_INFER_PROCESSORS = [
    {"class": "ProcessInf", "kwargs": {}},
    {"class": "ZScoreNorm", "kwargs": {"fields_group": "feature"}},
    {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
]

# ----------------------------------------------------------------- ② 股票池对齐
#: 🔴 **V3 主口径训练池** = 候选 ETF 成分股**并集 ∩ 中证 800 ∪ 中证 1000**
#:    （见 :data:`UNIVERSE_MODE_LARGE` 的说明；全并集 4032 只算力不可接受 ⇒ 收敛到大中盘）
UNIVERSE_MODE = "etf_constituent_large"
#: **全并集口径**（ETF 成分股并集，**不**做大中盘收敛）—— 留档对照，不作主口径
UNIVERSE_MODE_FULL_UNION = "etf_constituent_union"
#: 备选口径（对照用）：中证 800（qlib `csi800.txt` 带日期区间）
UNIVERSE_MODE_FALLBACK = "csi800"
#: 🔴 **V3 主口径（最终定稿）**：ETF 成分股并集 ∩ **中证 800 ∪ 中证 1000**
#:    ⇒ 即用户目标文档里「所有候选 ETF 的并集成分股」的**大中盘收敛版**：
#:    * 「并集」保证**只学 ETF 池里真正决定分数的权重股**（剔除与 ETF 无关的题材/微盘）；
#:    * 「∩ 中证 800/1000」剔除**小微盘噪音**（目标文档第 ② 条的另一半）；
#:    * 实测：全并集 **4032** 只（其中 807 只无法转 qlib instrument，如韩股 / 商品行）
#:      ⇒ 直接拿全并集训练，矩阵 ≈ 4032×158 特征 ⇒ 内存与算力都不可接受
#:      ⇒ 收敛到大中盘后规模落在千只量级，与目标文档「约 1,200～1,800 只」同量级。
UNIVERSE_MODE_LARGE = "etf_constituent_large"
#: 「大中盘」口径用的 instruments 文件（🔴 只读；带日期区间）
LARGE_CAP_INSTRUMENT_FILES = {
    "csi800": QLIB_INSTRUMENTS_DIR / "csi800.txt",
    "csi1000": QLIB_INSTRUMENTS_DIR / "csi1000.txt",
}
#: 🔴 **逐日 PIT 训练池口径**（新口径，🔴 **默认关闭**；只有显式指定才生效）：
#:    **ETF 成分股并集 ∩ 当日 csi800 ∪ csi1000**（逐日判定 ⇒ 读 instruments 文件的
#:    ``start``/``end`` 区间，而不是「历史上曾任成分」的静态并集）。
#:    * 🔴 **默认口径一个字都不动**：:data:`UNIVERSE_MODE` 仍是 ``"etf_constituent_large"``
#:      （静态并集 ⇒ 实测 **2635** 只，超目标区间 1200~1800 约 46%）；
#:    * 🔴 **打分池不动**：打分侧**保持 ETF 全并集口径**（不按日筛）⇒ 实测若打分池
#:      也按日 PIT，2019-04-02 平均 **62.4%** / 中位 **68.7%** 的 ETF 权重**无分可映射**
#:      （现役静态池只丢 **1.53%**）⇒ **PIT 掩码只许作用在训练样本上**；
#:    * 🔴 **规模中性化语义整体翻转**：PIT 池里 tier3（其它/不在任何档）占比
#:      **45.98% → 0** ⇒ 规模档哑变量的基准组消失 ⇒ **必须单独立臂验收**。
UNIVERSE_MODE_PIT = "etf_constituent_large_pit"
#: 🔴 **PIT 口径的描述文字**（臂定义 / 口径卡**从常量现取**，不许手写死字符串）
UNIVERSE_MODE_PIT_DESC = (
    "逐日 PIT：ETF 成分股并集 ∩ 当日 csi800 ∪ csi1000（按 instruments 的日期区间判定）"
)
#: 🔴 **PIT 成员面板的键名**（``build_universe`` 返回值里的**额外**键
#:    ⇒ 其余键（instruments / sum_w / n_stocks / mode …）与默认口径**同构**）
PIT_MEMBERSHIP_KEY = "pit_membership"
#: 🔴 **打分池权重丢失上限**（🔴 **历史留档，已停用**）。
#:    🔴 **为什么停用**：这个 1.53% 来自**单日**（2019-04-02）实测 ⇒ 与整段 OOS 的
#:    实测分布**矛盾**（见下面三个常量）⇒ 旧阈值 ``1 − 0.0153 − 0.005 = 0.9797``
#:    会把**每一个**滚动窗口都判死（实测中位数只有 0.84）⇒ 整臂算力作废。
#:    ⇒ 现行阈值见 :data:`SCORE_POOL_COVERAGE_MIN_MEDIAN` /
#:    :data:`SCORE_POOL_COVERAGE_MIN_DAY`。
SCORE_POOL_MAX_WEIGHT_LOSS = 0.0153
#: 🔴 判定容差（0.5 个百分点）：吸收「实测口径」与「逐日实现」的差异
SCORE_POOL_COVERAGE_TOL = 0.005
#: 🔴 **打分池「ETF 权重覆盖率」的下限（整段 OOS 实测，不是单日）**。
#:
#: 🔴 **实测口径（口径同 `train_s._assert_score_pool_covers_etf_union`）**
#:    * 分子 = 当日**有分**的标的的 ΣW；
#:      分母 = **静态打分池**（ETF 并集 ∩ 中证800∪中证1000 的 2635 只）的 ΣW；
#:    * 样本 = 已落盘的 6 臂 × 2019-01-01 ~ 2023-12-31 的 **701 个（臂, 信号日）观测**
#:      （只读镜像 ``<RESULTS_ROOT>/v3/_comparison_20261126/<臂>/`` 的打分文件）。
#:
#: 🔴 **实测结果**
#:    * 静态池（正确口径）：mean **0.83612** / median **0.84480** / min **0.63183**
#:      （最差日 **2019-04-11**）/ 逐窗口中位数 ∈ [0.82708, 0.86685]；
#:    * 「打分侧被误加 PIT 掩码」的变异值：mean 0.67043 / median 0.64092 /
#:      逐窗口中位数 ∈ [0.56428, **0.69154**]。
#:    ⇒ **分离带 = (0.69154, 0.82708]** ⇒ 阈值取带中点 **0.75931**（对测量噪声最稳）。
#:
#: 🔴 **不确定性**：样本只覆盖已落盘的 6 臂（未含 PIT 臂本身）；若将来打分池口径再变，
#:    这三个常量必须**重新实测**（不许沿用）。
SCORE_POOL_COVERAGE_MIN_MEDIAN = 0.75931
#: 🔴 **逐信号日的覆盖率下限**（= 静态池实测最小值 0.63183 − 判定容差 0.005）。
#:    🔴 用途：信号日数太少（滚动路径每窗口常常只有几个信号日）⇒ **中位数不可靠**
#:    ⇒ 改用「逐日不塌到实测最差日以下」这条**结构性底线**兜底。
SCORE_POOL_COVERAGE_MIN_DAY = 0.62683
#: 🔴 **信号日数 ≥ 该值才允许用中位数一刀切**；低于它 ⇒ 改为「结构性检查为主」
#:    （打分侧每日标的数必须超过当日 PIT 成员数）。
SCORE_POOL_MIN_SIGNAL_DAYS_FOR_MEDIAN = 10
#: 🔴 **实测来源说明**（写进 manifest ⇒ 阈值可追溯）
SCORE_POOL_COVERAGE_MEASUREMENT_NOTE = (
    "🔴 阈值来自整段 OOS 实测"
    "（2019-01-01~2023-12-31，6 臂 × 701 个（臂, 信号日）观测）："
    "覆盖率 = 当日有分标的的 ΣW / 静态打分池(2635)的 ΣW；"
    "静态池 median 0.84480 / min 0.63183（最差日 2019-04-11）、"
    "逐窗口中位数 ∈ [0.82708,0.86685]；"
    "「打分侧被误加 PIT 掩码」变异值逐窗口中位数 ≤ 0.69154 ⇒ 分离带 (0.69154,0.82708]，"
    "阈值取中点 0.75931；旧阈值 0.9797 来自单日 2019-04-02 实测 ⇒ 与分布矛盾，已停用"
)
#: 🔴 **样本加权**：按个股在 ETF 池中的总权重 **ΣW** 加权
#:    * ``"sum_w"``     ⇒ 权重 = max(ΣW, 0)（ETF 池里的实际重要性，**目标文档字面口径**）；
#:    * ``"sqrt_sum_w"`` ⇒ 权重 = √max(ΣW, 0)（削弱头部集中）；
#:    * ``"uniform"``   ⇒ 权重 = 1.0（不加权，对照臂）。
#:    🔴 **默认按目标文档字面取 ``"sum_w"``**（「按个股在 ETF 池中的总权重 ΣW 做样本加权」）。
SAMPLE_WEIGHT_MODE = "sum_w"
#: 样本权重下限（0 权重样本不能进训练 ⇒ 剔除）
SAMPLE_WEIGHT_MIN = 1e-6
#: 训练池最小只数（低于此值 ⇒ 判「池不可用」，fail-closed）
MIN_UNIVERSE_SIZE = 300

# ----------------------------------------------------------------- ③ 中性化
#: 🔴 **截面中性化总开关**
NEUTRALIZATION_ENABLED = True
#: 🔴 **规模（size）中性化**：用 qlib 指数档位（csi300 / csi500 / csi1000，
#:    **带日期区间** ⇒ 时变）构造规模档哑变量，对特征与标签做**截面残差化**。
#:    🔴 **口径披露**：这不是流通市值，而是**指数档位代理**（Alpha158 里没有市值字段，
#:    本仓也没有市值面板）⇒ 报告必须写明「规模中性化 = 档位代理」。
NEUTRALIZE_SIZE = True
#: 🔴 **流动性中性化**：用 20 日成交额（`$close × $volume`）的对数做截面残差化。
NEUTRALIZE_LIQUIDITY = True
#: 🔴🔴 **行业中性化：永远关闭**（用户 2026-11-26 目标文档明确禁止）
#:    理由：行业内去均值 ⇒ 行业内个股分数均值为 0 ⇒ `E = W × S` 全部塌缩到 0 附近
#:    ⇒ 直接抹杀行业轮动所需的板块景气度差异。
#:    🔴 本常量是**机器可断言**的：任何 `True` 都必须让守卫测试转红。
INDUSTRY_NEUTRALIZATION_ENABLED = False
#: 流动性中性化用的成交额回看窗口（交易日）
LIQUIDITY_LOOKBACK_DAYS = 20
#: 残差化的最小截面样本数（不足 ⇒ 该日跳过残差化并**如实记录**）
MIN_CROSS_SECTION_SIZE = 50

# ----------------------------------------------------------------- ④ 模型与滚动重训
#: 🔴 **损失函数**：LightGBM **lambdarank**（成对排序损失 ⇒ 直接对齐「选头部」的用法）
MODEL_OBJECTIVE = "lambdarank"
#: 对照臂用 V2 的 MSE 口径（单变量对照 ⇒ 一次只改一个变量）
CONTROL_OBJECTIVE = "mse"
#: 相关性等级数（label ⇒ 整数 relevance grade，组 = 交易日）
RANK_GRADE_COUNT = 5
#: 🔴 **强正则化**（目标文档：限制树深、提高叶子最小样本数、加大特征采样）
LGB_PARAMS = dict(
    objective=MODEL_OBJECTIVE,
    metric="ndcg",
    ndcg_eval_at=[5],
    learning_rate=0.02,
    num_leaves=31,
    max_depth=5,
    min_data_in_leaf=200,
    colsample_bytree=0.6,
    subsample=0.8,
    bagging_freq=1,
    lambda_l1=0.1,
    lambda_l2=5.0,
    min_gain_to_split=0.0,
    num_threads=4,          # 🔴 资源铁律：线程钉 4
    random_state=42,
    deterministic=True,
    force_row_wise=True,
    verbosity=-1,
)
#: 🔴 **单变量对照臂（只换损失函数）**：超参与 :data:`LGB_PARAMS` **逐项相同**，
#:    只把 ``objective`` 换成 ``mse``、``metric`` 换成 ``l2``（并去掉 ndcg 专用项）。
#: 🔴 2026-11-26 只读审计：原先 MSE 对照臂用的是 :data:`CONTROL_LGB_PARAMS`
#:    （与主臂差 **8 个超参**：num_leaves/max_depth/min_data_in_leaf/colsample/
#:    subsample/lambda_l1/lambda_l2/metric）⇒ 「单变量：只换损失函数」**不成立**
#:    ⇒ 消融结论会被误归因（差异可能来自正则强度而非损失）⇒ 改为本常量。
MSE_ARM_LGB_PARAMS = {
    **{k: v for k, v in LGB_PARAMS.items()
       if k not in ("objective", "metric", "ndcg_eval_at")},
    "objective": CONTROL_OBJECTIVE,
    "metric": "l2",
}
#: 🔴 **V2 同参参考集**（保留供「V2 同参 MSE」参考臂用）——
#:    🔴 **不参与**「只换损失函数」的单变量对照（那个臂用 :data:`MSE_ARM_LGB_PARAMS`）。
CONTROL_LGB_PARAMS = dict(
    objective=CONTROL_OBJECTIVE,
    metric="l2",
    learning_rate=0.02,
    num_leaves=210,
    max_depth=8,
    min_data_in_leaf=20,
    colsample_bytree=0.88,
    subsample=0.87,
    bagging_freq=1,
    lambda_l1=0.0,
    lambda_l2=0.0,
    num_threads=4,
    random_state=42,
    deterministic=True,
    force_row_wise=True,
    verbosity=-1,
)
NUM_BOOST_ROUND = 500
#: 🔴 早停必须关掉（与 V2 同理由：qlib 会把 train+valid 一起塞进 valid_sets）
EARLY_STOPPING_ROUNDS = None
#: 🔴 **滚动重训周期**（交易日）= 约一个季度 ⇒ 每 60 个交易日重训一次
ROLLING_RETRAIN_EVERY_N_TRADING_DAYS = 60

# ----------------------------------------------------------------- 特征消融（同 V2 口径）
#: Alpha158 的 5 日超短线反转特征（与 V2 同口径消融）
REVERSAL_FEATURE = "ROC5"
DROPPED_FEATURES: tuple[str, ...] = (REVERSAL_FEATURE,)

# ----------------------------------------------------------------- 时间切分（与 V2 同红线）
START_TIME = "2015-01-01"
FIT_START = "2016-01-01"
TRAIN = ("2016-01-01", "2021-12-31")
VALID = ("2022-01-01", "2023-12-31")
TEST = ("2024-01-01", "2026-11-03")
EMBARGO_DAYS = 20
#: 🔴 **打分日栅格步长**（交易日）＝ 每 10 个交易日一个信号日
#:    （与 V1/V2 同口径；事实源 = `manu/etf_rotation/data/signal_policy.py::score_grid_dates`）
SCORE_GRID_EVERY_N_TRADING_DAYS = 10
#: 🔴 **特征预热**（自然日）＝ Alpha158 最长回看 60 交易日 ⇒ 起点前留 120 自然日
SCORE_WARMUP_CALENDAR_DAYS = 120
#: 🔴 5 折滚动前推（与 V2 同表 ⇒ 可直接对表）
WALK_FORWARD_FOLDS: tuple[tuple[int, str, str, str], ...] = (
    (1, "2018-12-31", "2019-01-01", "2019-12-31"),
    (2, "2019-12-31", "2020-01-01", "2020-12-31"),
    (3, "2020-12-31", "2021-01-01", "2021-12-31"),
    (4, "2021-12-31", "2022-01-01", "2022-12-31"),
    (5, "2022-12-31", "2023-01-01", "2023-12-31"),
)
WALK_FORWARD_OOS_WINDOW = ("2019-01-01", "2023-12-31")
#: 🔴 **PIT 成员面板的日期跨度**（= 全部折训练段的并集：训练起点 ~ OOS 窗口末日）
#:    实测逐日池规模：min **1279** / 中位 **1402** / max **1628**（目标 1200~1800 ⇒ 100% 命中）
PIT_PANEL_START = TRAIN[0]
PIT_PANEL_END = WALK_FORWARD_OOS_WINDOW[1]
#: 🔴 **单盲封存段**（2024-01-01 ~ 2026-11-03）—— V3 的对照实验**一律不许触碰**
BLIND_SEALED_WINDOW = TEST

# ----------------------------------------------------------------- ⑤ 验收指标
#: ① ETF 成分股并集内的 **20 日滚动 RankIC**（均值 / ICIR）
ACCEPT_RANKIC_WINDOW = 20
#: ② 打分 **Top 10%** 成分股组合的**费后超额**（对基准 = CSI300）
ACCEPT_TOP_FRACTION = 0.10
#: ③ 端到端（V2 ETF 层 + 冻结参数）夏普 / 换手
ACCEPT_SHARPE_FREQ = 244
#: 🔴 **下游冻结参数**（端到端验收必须用**这一份**，一个字都不许改）
FROZEN_PARAMS_JSON = Path("/mnt/data/siping/runs/etf_rotation_results/v2/frozen_params_wf.json")

# ----------------------------------------------------------------- 成本口径（与 V2 同源）
COMMISSION = 0.00015
SLIPPAGE = 0.0005

# ----------------------------------------------------------------- 产物命名
#: 🔴 V3 分数文件名模板（**不得**与 V1/V2 同名）
SCORES_FILENAME_TEMPLATE = "stock_scores_v3_{model_name}.csv"


#: 🔴 **单边换手因子**（双边 → 单边）：V3 自己的常量副本。
#: 🔴 **口径同源**：V2 `config_v2.TURNOVER_ONE_WAY_FACTOR` ＝ **0.5**
#:    （单边 = 0.5 × 双边 = 0.5 × Σ|Δw|）⇒ V3 不许 import V2（隔离契约）⇒ 在此自带一份。
TURNOVER_ONE_WAY_FACTOR = 0.5


def one_way_turnover(turnover: float) -> float:
    """**双边 → 单边**换算（**V3 自己的唯一实现**）。

    🔴 **为什么不在 V3 里 import V2**：V3 是研发分支、**隔离契约**禁止 import V2 可执行模块
       ⇒ 只能自带一份实现；为避免"两份实现悄悄分叉"，口径与 V2 **逐字同源**（0.5 × 双边）。
    🔴 **NaN 语义不许混搭**（同 V2）：非法 / 非有限输入 ⇒ 返回 NaN（**不是 0.0**）。
    """
    if turnover is None:
        return float("nan")
    try:
        value = float(turnover)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(value):
        return float("nan")
    return value * TURNOVER_ONE_WAY_FACTOR


def scores_filename(model_name: str) -> str:
    """V3 分数文件名（统一入口，**不许**在别处拼字符串）。"""
    return SCORES_FILENAME_TEMPLATE.format(model_name=str(model_name))


#: 🔴 **口径卡「成员口径」文案（无参调用 ⇒ 逐字不变；见 :func:`v3_caliber_card`）**
_CALIBER_MEMBERSHIP_STATIC = {
    "default": (
        "静态并集：ETF 成分股并集 ∩ csi800 ∪ csi1000"
        "（🔴 `read_instruments_file` 丢弃成分表的日期区间 ⇒ 取的是"
        "「历史上曾任成分」的全体 ⇒ 实测 2635 只）"
    ),
    "pit": UNIVERSE_MODE_PIT_DESC,
}
#: 🔴 **打分池文案（旧版；无参调用 ⇒ 逐字不变）**
_CALIBER_SCORING_POOL_LEGACY = (
    "🔴 ETF 成分股全并集（静态，**不按日筛**）—— 打分侧**禁止**"
    "施加 PIT 掩码（实测：打分池也 PIT ⇒ 2019-04-02 平均 62.4% /"
    "中位 68.7% 的 ETF 权重无分可映射；现役静态池只丢 1.53%）"
)
#: 🔴 **打分池文案（修正版）**：实际打分池 = ETF 并集 ∩ (中证 800 ∪ 中证 1000) 的
#:    **静态池 2635 只**（🔴 **不是**「ETF 成分股全并集」—— 全并集是 4032 只，
#:    打分侧根本不打那一部分）。
_CALIBER_SCORING_POOL = (
    "🔴 打分池 = 候选 ETF 成分股并集 ∩ (中证 800 ∪ 中证 1000) 的**静态池**"
    "（实测 2635 只，**不按日筛**）—— 打分侧**禁止**施加 PIT 掩码"
    "（整段 OOS 实测：打分池一旦被按日 PIT 筛 ⇒ ETF 权重覆盖率中位数从 0.84480 "
    "掉到 0.64092 ⇒ 立刻触发 train_s 的硬断言）"
)


def v3_caliber_card(universe_mode: str | None = None) -> dict:
    """给报告 / manifest 用的 **V3 口径卡**。

    🔴 ``universe_mode=None`` ⇒ **输出逐位不变**（静态并集口径的旧文案原样保留）。
    🔴 显式给定时 ⇒ ``universe.mode`` = 该口径 ⇒ **必须**与 manifest 顶层
       ``universe.mode`` **相等**（否则同一份产物里出现两个互相矛盾的口径 ⇒ B2）。
    🔴 ``mode_pit`` / ``membership`` 段是**可选口径说明**（不是「本次口径」）
       —— 本次口径只看 ``universe.mode``。
    """
    mode = str(universe_mode or UNIVERSE_MODE)
    if universe_mode is None:
        # 🔴 无参路径 ⇒ **逐位保留旧文案**（默认路径必须逐位不变）
        membership = dict(_CALIBER_MEMBERSHIP_STATIC)
        scoring_pool = _CALIBER_SCORING_POOL_LEGACY
        universe_extra: dict = {}
    else:
        membership = {
            "default": (
                "可选口径说明①（静态并集口径）：ETF 成分股并集 ∩ csi800 ∪ csi1000"
                "（🔴 `read_instruments_file` 丢弃成分表的日期区间 ⇒ 取的是"
                "「历史上曾任成分」的全体 ⇒ 实测 2635 只）"
            ),
            "pit": "可选口径说明②（逐日 PIT 口径）：" + UNIVERSE_MODE_PIT_DESC,
        }
        scoring_pool = _CALIBER_SCORING_POOL
        universe_extra = {
            "mode_note": (
                "🔴 mode_large / mode_fallback / mode_pit 是**可选口径清单**；"
                "本次口径只看 universe.mode（必须与 manifest 顶层 universe.mode 相等）"
            ),
        }
    return {
        "version": V3_VERSION,
        "model_version_tag": V3_MODEL_VERSION_TAG,
        "label": {
            "exit_window": list(LABEL_EXIT_WINDOW),
            "horizon_days": LABEL_HORIZON_DAYS,
            "price_field": LABEL_PRICE_FIELD,
            "qlib_expression": LABEL_EXPRESSION,
            "centering": LABEL_CENTERING,
            "benchmark": BENCHMARK,
            "formula": "t+15~t+25 共 11 个交易日 VWAP 均价退出价 ⇒ 持有期收益；减当日基准同窗口收益",
            "caliber_note": "🔴 退出价取 t+15~t+25 窗口均价（不是 t+20 单日收盘）⇒ 持有期 ≈20 个交易日",
            "post_processing": "DropnaLabel（🔴 不做 CSRankNorm ⇒ 数值超额得以保留）",
        },
        "universe": {
            "mode": mode,
            "mode_large": UNIVERSE_MODE_LARGE,
            "mode_fallback": UNIVERSE_MODE_FALLBACK,
            "mode_pit": UNIVERSE_MODE_PIT,
            "membership": membership,
            "scoring_pool": scoring_pool,
            "tier_note": "🔴 PIT 池下 tier3（其它/不在任何档）占比 45.98% → 0 ⇒ "
                         "规模档哑变量的基准组消失 ⇒ **规模中性化语义整体翻转** ⇒ "
                         "PIT 臂必须**单独立臂验收**",
            "sample_weight_mode": SAMPLE_WEIGHT_MODE,
            "min_universe_size": MIN_UNIVERSE_SIZE,
            "holdings_snapshot_date": HOLDINGS_SNAPSHOT_DATE,
            **universe_extra,
        },
        "neutralization": {
            "enabled": NEUTRALIZATION_ENABLED,
            "applies_to": ["feature", "label"],
            "size": NEUTRALIZE_SIZE,
            "size_proxy": "qlib 指数档位（csi300/csi500/csi1000，带日期区间）——🔴 非流通市值",
            "liquidity": NEUTRALIZE_LIQUIDITY,
            "liquidity_lookback_days": LIQUIDITY_LOOKBACK_DAYS,
            "industry": INDUSTRY_NEUTRALIZATION_ENABLED,
            "industry_note": (
                "🔴 行业中性化**永远关闭**：行业内去均值会让 E = W × S 塌缩到 0 附近，"
                "直接抹杀行业轮动所需的板块景气度差异"
            ),
        },
        "model": {
            "objective": MODEL_OBJECTIVE,
            "control_objective": CONTROL_OBJECTIVE,
            "rank_grade_count": RANK_GRADE_COUNT,
            "num_boost_round": NUM_BOOST_ROUND,
            "rolling_retrain_every_n_trading_days": ROLLING_RETRAIN_EVERY_N_TRADING_DAYS,
        },
        "acceptance": {
            "rankic_window": ACCEPT_RANKIC_WINDOW,
            "top_fraction": ACCEPT_TOP_FRACTION,
            "frozen_params": str(FROZEN_PARAMS_JSON),
        },
        "isolation": (
            "🔴 V3 不 import/不改写 V1 冻结件，不 import/不改写 V2 任何文件；"
            "产物只落 <RESULTS_ROOT>/v3/；V2 仍是现役口径"
        ),
    }
