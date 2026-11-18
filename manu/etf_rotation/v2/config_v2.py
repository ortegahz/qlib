"""🔴 V2 唯一配置源（2026-11-17）。

**为什么单独一个文件**：V1 的 ``manu/etf_rotation/config.py`` 是**冻结 baseline**，
V2 必须在**不改写 V1** 的前提下有一份**可读出、可对账、可冻结**的配置
（主机验收脚本会直接 grep 本文件的切分常量）。

口径来源（三处必须一致）：
  * 用户权威规格原文：``codex_overnight/V2_SPEC_pasted_text.txt``（Label/对齐/红线）；
  * 主机续跑指令（embargo 的**逐字口径**）：``codex_overnight/prompt_next.txt``；
  * V1 baseline 数值（对比表用）：``manu/memory/etf_rotation_20261104.md §18.4``。

🔴 **embargo 口径的诚实说明（不许含糊）**：
   主机规格给的是「训练集末段裁剪 20 个交易日」，并同时要求
   「**训练末样本的标签窗口结束日 < 验证集首样本日期**」这条**硬断言**。
   本实现的 V2 标签退出窗口是 **t+15 ~ t+25**（末端 = t+25）⇒
   若只裁 20 个交易日，最后一个训练样本（≈2021-12-02）的标签窗口会伸到
   **2022-01-10**，与验证集**价格窗口**重叠 6 个交易日 ⇒ **结构性泄露**。
   ⇒ 取「embargo ≥ 20 个交易日」且**以标签窗口不跨界为硬约束**：
   ``effective_embargo = max(EMBARGO_DAYS, LABEL_HORIZON_DAYS)`` = **25 个交易日**
   （训练末样本 ≈ 2021-11-26，仍满足「≤ 2021-12-31 前第 20 个交易日」这条上界断言）。
   两个数都写在这里，报告里必须同时披露，禁止只报好看的那个。
"""

from __future__ import annotations

import math
from pathlib import Path

#: 🔴 V2 版本标识（写进每个产物的 manifest / 报告标题 ⇒ 不可能与 V1 混淆）
V2_VERSION = "v2"
V2_LABEL = "ETF 穿透评分 V2 对齐版"

# ----------------------------------------------------------------- 打分池
#: 🔴 **训练池 = csi300**（与 V1 个股线同构；V1 的 S 层模型就是在 csi300 上训的）
TRAIN_UNIVERSE = "csi300"
#: **打分池 = all**（全市场；ETF 成分股大量落在 csi300 之外 ⇒ 必须扩池）
SCORE_UNIVERSE = "all"
#: 🔴 **调仓节奏（cadence）的 V1 同口径基准档 = 20 个交易日** —— 用户 2026-11-17 决定②
#:    （V1 的两段式 ``REBALANCE_EVERY_N_DAYS=10`` × ``REBALANCE_STRIDE_MULTIPLIER=2``
#:    在 V2 里折叠成单一常量 20 = V1 的 ``DEPLOYED_REBALANCE_EVERY_N_DAYS``，逐字一致）。
#:
#:    🔴 **口径变更（2026-11-17，用户红线驱动）**：调仓节奏**不再是**「不许进搜索空间」的
#:    折叠常量 —— 用户红线是「换手 16.57× → ≤4×」；实测证明：在节奏 = 20 的**折叠常量**下，
#:    臂 / Top-K / 缓冲带 / MA 窗口**全部**搜完后年化换手最低仍 **12.32×**
#:    （结构性来源 = 「持仓跌破 MA ⇒ 无条件卖出」，Rank Buffer 对 MA 强平无效）
#:    ⇒ **≤4× 只能靠调仓节奏本身达成** ⇒ cadence 进入 **val 段**搜索空间
#:    （见 :data:`REBALANCE_CADENCE_GRID`）。
#:
#:    ⇒ **20 这一档必须保留可见**（**调仓节奏**与 V1 相同 ⇒ 可与 V1 的 16.57× **趋势性**对表；
#:    🔴 换手**权重基准**仍不同源 ⇒ 只趋势性对比，不是同源）；
#:    cadence ≠ 20 的档位与 V1 **不是同一口径** ⇒ 报告里必须写明（不可直接比）。
REBALANCE_EVERY_N_DAYS = 20
#: 🔴 **调仓节奏（cadence）候选集** —— 只在 **val 段**搜（测试段单盲只跑一次）。
#:
#: 🔴🔴 **口径锁定（2026-11-18，用户目标文档「三步手术」）**：
#:    **cadence 锁死 = 20 个交易日（严格月频）** ⇒ 搜索空间只剩 ``(20,)``。
#:
#:    依据（用户原话）：「**锁死 cadence = 20（严格月频）**」「**不准搜索 90 / 120 / 150
#:    这种反直觉长周期**」—— 4 个月一调仓在 A 股抓不住行业轮动（轮动生命周期 1~2 个月），
#:    而 V2 的 20 日预测 RankIC 高达 0.1271 ⇒ **时效性 > 换手率**。
#:
#:    ⚠️ **降换手的正确姿势** = 「月频 + Rank Buffer（宽出严进）」，
#:    **不是**「拉长调仓周期」（见 :data:`HOLD_BUFFER_GRID` 与目标文档 Step 2）。
#:
#:    🔴 **历史留档**：2026-11-17 的搜索空间曾是 ``(20, 40, 60, 90)``（cadence 进网格
#:    是为了硬压换手 ≤4×）；那次搜索的产物已封存在 ``val_search.json``，
#:    **不许**回改它 ⇒ 这里只保留 :data:`REBALANCE_CADENCE_GRID_LEGACY` 供审计对表。
#:    ⚠️ **不许**把测试段（2024–2026）的任何数字带进本网格。
REBALANCE_CADENCE_GRID: tuple[int, ...] = (20,)
#: 🔴 **历史 cadence 候选集（仅供审计对表，不参与任何搜索）**（2026-11-17 用过的那一版）。
REBALANCE_CADENCE_GRID_LEGACY: tuple[int, ...] = (20, 40, 60, 90)
#: 🔴 **选参可信度门槛**：val 段调仓次数 < 该值 ⇒ 该 cadence 档的年化 / 回撤
#:    **噪声主导** ⇒ 报告与产物里必须标注「样本不足，不可据此选参」。
CADENCE_MIN_REBALANCES = 12
#: 🔴 **信号日基础步长**（V1 同口径 = 10 个交易日）：**只决定 S 层打分栅格的疏密**，
#:    **不是**调仓频率（调仓频率 = cadence，见上）。
SIGNAL_STRIDE_N_DAYS = 10
#: 信号栅格 → 调仓栅格的步数（= 20 / 10）：**由上面两个常量推导**，
#:    不许单独写死（否则"折叠"就白折叠了）—— 这是 **cadence = 20** 档的步长。
REBALANCE_SIGNAL_STRIDE = REBALANCE_EVERY_N_DAYS // SIGNAL_STRIDE_N_DAYS


def rebalance_signal_stride_for(cadence: int) -> int:
    """cadence（**交易日**）→ 信号栅格步长（= cadence ``// SIGNAL_STRIDE_N_DAYS``）。

    🔴 **唯一映射入口**：20 → 2、40 → 4、60 → 6、90 → 9（信号栅格 = 10 个交易日一个信号日）。
    cadence 必须是 ``SIGNAL_STRIDE_N_DAYS`` 的整数倍，否则调仓日会**漂出**信号栅格
    ⇒ 直接 raise（不静默取整）。
    """
    days = int(cadence)
    if days <= 0:
        raise ValueError(f"🔴 cadence 必须为正整数交易日，实得 {cadence!r}")
    if days % int(SIGNAL_STRIDE_N_DAYS) != 0:
        raise ValueError(
            f"🔴 cadence={days} 不是信号栅格步长 {SIGNAL_STRIDE_N_DAYS} 的整数倍"
            f" ⇒ 调仓日会漂出信号栅格（口径不合法）"
        )
    return days // int(SIGNAL_STRIDE_N_DAYS)
#: 打分窗口起点（Alpha158 最长回看 60 日 ⇒ 起点前留 120 自然日做预热）
SCORE_WARMUP_CALENDAR_DAYS = 120

# ----------------------------------------------------------------- LightGBM
#: 🔴 **照抄 V1 个股线已定档的超参**（V1 于 2026-11-04 在 **val 段**选出：
#:    lr=0.02 / rounds=500 → valid IC 0.0386 / ICIR 0.321）。
#:    V2 **不 import V1**（硬隔离）⇒ 这里**逐项抄一份**并注明出处。
#:    🔴 V2 **没有**在 val 段重搜 S 层超参（把算力留给 ETF 层搜索）——
#:       这一点在报告里**如实披露**，不许装作「V2 自己搜过」。
LGB_PARAMS = dict(
    loss="mse",
    colsample_bytree=0.88,
    learning_rate=0.02,
    subsample=0.87,
    bagging_freq=1,
    lambda_l1=0.0,
    lambda_l2=0.0,
    max_depth=8,
    num_leaves=210,
    num_threads=4,          # 🔴 资源铁律：线程钉 4（不抢别人的 Optuna 作业）
    random_state=42,
    deterministic=True,
    force_row_wise=True,
    verbosity=-1,
)
NUM_BOOST_ROUND = 500
#: 🔴 早停必须关掉（V1 同口径：qlib 会把 [train, valid] 一起塞进 LightGBM 的
#:    valid_sets，早停后按最早最优迭代裁剪模型 ⇒ 引入不可控的隐性泄漏）
EARLY_STOPPING_ROUNDS = None

# ----------------------------------------------------------------- 落点根
#: 🔴 V2 产物根：**与 V1 物理隔离**（rules.md §12.8：产物目录不得互相覆盖），
#:    但**必须落在允许的两个落点之内**（铁律①：只允许
#:    ``/mnt/data/siping/runs/etf_rotation_results/`` 与 ``/mnt/data/siping/datasets/manu/``）
#:    ⇒ V2 = V1 产物根下的**独立子目录** ``v2/``（不是 ``runs/`` 的兄弟目录）。
DEFAULT_V2_RUN_ROOT = Path("/mnt/data/siping/runs/etf_rotation_results/v2")
#: V2 代码根（本包）
V2_ROOT = Path(__file__).resolve().parent
#: qlib 数据集根（与 V1 同源，只读复用）
QLIB_CN_DATA = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/qlib/cn_data")
#: 交易日历（qlib cn_data 自带）
CALENDAR_FILE = QLIB_CN_DATA / "calendars" / "day.txt"
#: ETF 成分股/权重表（🔴 **只有一个快照日**，见 `constituents.py`）
HOLDINGS_CSV = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/etf_holdings.csv")
#: ETF 行情
ETF_PRICES_CSV = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/etf_prices.csv")

# ----------------------------------------------------------------- qlib 数据源
#: 🔴 **live** 数据集（含最新交易日 2026-11-17；``cn_data`` 只到 11-16）
QLIB_CN_DATA_LIVE = QLIB_CN_DATA.parent / "cn_data_live"
#: 本次跑 V2 用的 provider（训练 + 打分**同一个**数据集 ⇒ 口径不分叉）
DEFAULT_PROVIDER_URI = str(QLIB_CN_DATA_LIVE)


# ----------------------------------------------------------------- 时间切分（硬红线）
#: 预热起点（Alpha158 最长回看 60 交易日 ⇒ 留 1 年预热，与 V1 一致）
START_TIME = "2015-01-01"
#: 训练段（**名义**区间；实际末样本日由 embargo 裁剪，见 `splits.py`）
TRAIN = ("2016-01-01", "2021-12-31")
#: 验证段：**所有超参搜索只许在这段做**（起点不裁剪 —— 主机规格明确）
VALID = ("2022-01-01", "2023-12-31")
#: 测试段：**单盲只跑一次**（起点不动）
TEST = ("2024-01-01", "2026-11-03")
#: 归一化统计量拟合窗起点（必须严格落在训练段内 —— rules.md §12.7）
FIT_START = "2016-01-01"
#: 🔴 embargo 隔离带（**交易日**）：任务书给的下限 = 20
EMBARGO_DAYS = 20

# ----------------------------------------------------------------- 5 折滚动前推（2026-11-18 新增）
#: 🔴 **动机（用户 2026-11-18 原话）**：「只拿 2022–2023 这 2 年做验证集……样本量偏少，
#:    而且行情风格严重偏科」—— 484 个交易日按月频只有 **24 个调仓点** ⇒ 拿 24 个样本点去
#:    网格搜 240 组超参 ⇒ **必然过拟合**；且 2022–2023 是单边熊市 ⇒ 「躺平装死」永远
#:    比「积极轮动」亏得少 ⇒ 搜出来的必然是「蜗牛参数」（cadence=90 + entry_ma=120）。
#:
#: 🔴 **处方：expanding window（滚动前推）** —— 训练段逐年变长，验证段逐年前推：
#:    折 1 = 训练 2016~2018 → 验证 2019；折 2 = 训练 2016~2019 → 验证 2020；……；
#:    折 5 = 训练 2016~2022 → 验证 2023。
#:    ⇒ 5 折的 OOS 拼起来 = **2019–2023（5 年、~1,200 个交易日、~60 个月频调仓点）**
#:    的纯样本外验证段，**同时含牛市 / 熊市 / 震荡市**；2024–2026 单盲**保持封存**。
#:
#: 🔴 **偏差披露（不藏）**：用户规格写「2015 ~ 2018 起训」，但本工程**训练段起点 =
#:    2016-01-01**（:data:`TRAIN[0]`，数据本身也从 2016 年起）⇒ **折 1 的训练段是
#:    2016~2018，比规格少 1 年** ⇒ 折 1 的结论样本量最小，报告里必须写明。
WALK_FORWARD_TRAIN_START = TRAIN[0]          # 2016-01-01（数据起点 ⇒ 比规格少 1 年）
#: 🔴 5 折的切分表：``(fold_id, 名义训练末日, 验证段起, 验证段止)``
#:    名义训练末日 = 该折训练段的**日历年末**；实际训练末样本日由 embargo
#:    **从验证段起点倒推**（见 ``splits.py::build_fold_spec``）⇒ 标签窗口绝不跨界。
WALK_FORWARD_FOLDS: tuple[tuple[int, str, str, str], ...] = (
    (1, "2018-12-31", "2019-01-01", "2019-12-31"),
    (2, "2019-12-31", "2020-01-01", "2020-12-31"),
    (3, "2020-12-31", "2021-01-01", "2021-12-31"),
    (4, "2021-12-31", "2022-01-01", "2022-12-31"),
    (5, "2022-12-31", "2023-01-01", "2023-12-31"),
)
#: 🔴 **5 折拼接后的 OOS 验证窗**（= 2019-01-01 ~ 2023-12-31）
WALK_FORWARD_OOS_WINDOW = ("2019-01-01", "2023-12-31")
#: 🔴 **压缩自由度（用户「方法 2」）**：把「物理意义明确」的参数**焊死**，只留
#:    真正需要搜的少数几维 ⇒ 网格规模从 240 降到个位数 ⇒ 24 个调仓点也能站得住。
#:    * ``cadence`` = **20**（月频，与 S 层 20 日标签同频）——**不参与搜索**；
#:    * ``top_k``   = **5**（分散度 + 单只资金门槛）——**不参与搜索**；
#:    * ``hold_buffer`` ∈ {2, 3, 5}——**唯一可搜的一维**（Rank Buffer = 降换手的正解）。
WALK_FORWARD_CADENCE = 20
WALK_FORWARD_TOPK_LOCKED = 5
WALK_FORWARD_BUFFER_GRID: tuple[int, ...] = (2, 3, 5)

# ----------------------------------------------------------------- Label（对齐①）
#: 标签退出窗口（t+15 ~ t+25）：月频调仓 ⇒ 实际退出日在 t 之后 15~25 个交易日内
LABEL_EXIT_WINDOW = (15, 25)
#: 标签窗口**末端**相对 t 的偏移（= 25）⇒ embargo 必须 ≥ 它，否则标签跨界
LABEL_HORIZON_DAYS = LABEL_EXIT_WINDOW[1]
#: 标签基准（超额收益的基准）：沪深300（qlib features 目录有 sh000300）
BENCHMARK = "sh000300"
#: 标签价格口径：VWAP（与 V1 一致）
LABEL_PRICE_FIELD = "$vwap"
#: qlib 表达式版标签（**窗口均价口径**）：
#:   Ref(Mean($vwap,11), -25) = mean_{k=15..25}(P[t+k])   ← 退出价 = 窗口均价
#:   ⇒ 标签 = Ref(Mean($vwap,11),-25) / $vwap - 1
#: 🔴 **不许**写成 ``Mean(Ref($vwap,-15)/$vwap-1, 11)``：
#:    qlib 的 ``Mean`` 会让**分子分母一起滚动** ⇒ 得到「15 日前的相对收益」，
#:    **不是**「未来 15~25 日的收益」⇒ 口径完全错位（已在真机上逐位核对）。
LABEL_EXPRESSION = "Ref(Mean($vwap,11),-25)/$vwap-1"
#: 🔴 训练标签后处理：**与 V1 同口径**（CSRankNorm 截面排名）
#:    ⚠️ 超额收益里的**基准项是逐日常数** ⇒ 截面排名**不变**
#:    ⇒ 训练信号与「原始窗口收益」完全等价（`labels.py::test_基准项不改变截面排名` 钉死）
LABEL_LEARN_PROCESSORS = [
    {"class": "DropnaLabel"},
    {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}},
]
LABEL_INFER_PROCESSORS = [
    {"class": "ProcessInf", "kwargs": {}},
    {"class": "ZScoreNorm", "kwargs": {"fields_group": "feature"}},
    {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
]

# ----------------------------------------------------------------- 特征消融（对齐①）
#: 🔴 Alpha158 的 5 日「超短线反转」特征 = ``ROC5``（``Ref($close,5)/$close``
#:    = 5 日收益的**相反数** ⇒ 唯一带「5 日反转」语义的特征）
REVERSAL_FEATURE = "ROC5"
#: 🔴 **默认口径 = 消融 ROC5**（用户 2026-11-17 纠正：默认应「消融 ROC5」，
#:    ``baseline_full`` 降为**对照臂**）。
#:    判定**只在 val(2022-01-01 ~ 2023-12-31)** 做（实测见
#:    ``<V2 产物根>/ablation_choice.json``：ablate_roc5 RankIC 0.07540 / ICIR 0.6549
#:    vs baseline_full 0.07492 / 0.6387，n=48 个截面）⇒ **测试段从未参与**。
DROPPED_FEATURES: tuple[str, ...] = (REVERSAL_FEATURE,)
#: 🔴 消融决策的**冻结结论**（由 val 段判定后写死；改它 ⇒ 钉死测试必须转红）
ABLATION_WINNER = "ablate_roc5"
#: 消融判定用的指标（口径写死：逐日截面 RankIC；不许换指标后重判）
ABLATION_METRIC = "rank_ic_1d"
#: 对照实验用的消融组（报告里逐组对比）
ABLATION_GROUPS: dict[str, tuple[str, ...]] = {
    "baseline_full": (),
    "ablate_roc5": ("ROC5",),
    "ablate_roc_family": ("ROC5", "ROC10", "ROC20"),
}

# ----------------------------------------------------------------- 成分股口径（对齐②）
#: 🔴 V1 的成分股快照日（**只有一个**！历史成分变更**不可观测**）
HOLDINGS_SNAPSHOT_DATE = "2026-11-04"
#: 成分股覆盖率下限（与 V1 一致）
MIN_COVERAGE = 0.5
#: 「历史真实成分股」缺口：**必须如实标注**，禁止静默用现状倒推
CONSTITUENTS_CALIBER_NOTE = (
    "🔴 V1/V2 共同的结构性前视：本地只有 **2026-11-04 单快照**成分股/权重"
    "（akshare `fund_etf_holdings_tx` / `fund_etf_pcf` 均不提供历史 PCF）"
    "⇒ 历史成分变更**不可观测** ⇒ 回测用 2026 权重倒推历史。"
    "V2 **如实标注**该口径并量化偏差上界（见 `constituents.py::lookahead_bias_bound`），"
    "**不编造**历史成分股快照。"
)

# ----------------------------------------------------------------- 流动性约束（对齐②）
#: 涨跌停阈值（A 股主板 9.5%，与个股线 qlib Exchange 口径一致）
LIMIT_THRESHOLD = 0.095
#: 停牌判据：成交量 ≤ 0 或非有限
SUSPENDED_VOLUME = 0.0
#: ETF 侧流动性下限（与 V1 一致）
MIN_ETF_AVG_AMOUNT_20D = 50_000_000.0
MIN_ETF_FLOAT_MCAP = 200_000_000.0
MIN_ETF_HISTORY_DAYS = 120

# ----------------------------------------------------------------- Cost-Aware（对齐③）
#: 单边佣金（bp）：与 V1 同口径（ETF 免印花税）
COMMISSION = 0.00015
#: 单边滑点（bp）：V1 同口径
SLIPPAGE = 0.0005
#: 压力情景（成本翻倍/滑点 4 倍）
COMMISSION_STRESS = 0.0005
SLIPPAGE_STRESS = 0.0020
#: 🔴 **Rank Buffer（换手迟滞带）**：已持有的标的只要排名还在
#:    「目标只数 + buffer」之内就**继续持有**（V1 叫 HOLD_BUFFER）
#:    ⇒ V2 必须把「buffer 造成的**留存量**」计入换手 ⇒ 换手从
#:    「事后权重差」改成「**目标权重差（含 buffer 留存量）**」
HOLD_BUFFER = 2
TARGET_N_HOLDINGS = 5
#: buffer 搜索空间（**只在 val 段搜**）
HOLD_BUFFER_GRID = (0, 1, 2, 3, 4)
TOPK_GRID = (4, 5, 6)
#: 🔴 **排序分里 alpha 的权重**（val 段搜索空间；``momentum_weight = 1 - alpha_weight``）
#:    —— 2026-11-18 归因证据：纯 E（1.0）比 V2 现状（0.5）把 ETF 层 RankIC 从
#:    **0.0550 提到 0.1016**、超额从 **−10.06% 提到 +7.06%** ⇒ 配比必须可搜。
ALPHA_WEIGHT_GRID: tuple[float, ...] = (1.0, 0.5)
#: 🔴 **同族上限开关**（val 段搜索空间；V1 遗留规则）
#:    —— 2026-11-18 归因证据：关掉它，纯净 E 变体的超额从 −1.90% 回到 **+7.06%**。
THEME_CAP_GRID: tuple[bool, ...] = (True, False)
#: 🔴 **换手披露基准**（4.0×）—— **不再是选参硬门槛**（用户 2026-11-18 拍板）。
#:
#:    **废除「月频策略 ≤4.0× 换手」硬拦截**的依据（两条，都要写进报告）：
#:      ① 用户 2026-11-18 拍板：改「**cadence=20 先验锁定 + hold_buffer=2
#:         + 报告并列披露单/双边换手率**」；
#:      ② 实测成本可承受：五折冠军年化**双边**换手 **14.1002×**（单边 7.05×），
#:         累计成本拖累 4.4286%（累计 4.84 年）⇒ **年化 ≈ 0.915%**
#:         （= 14.10 双边 × 6.5bp）⇒ 远小于超额本身的波动
#:         ⇒ 再拿换手筛参数 = **用 alpha 买断**（与 :data:`TURNOVER_PENALTY_CAP`
#:         描述的事故机制**同源**）。
#:
#:    ⇒ **数值不变**（4.0），只改语义：现在是**披露基准**，不再是选参红线。
TURNOVER_TARGET = 4.0
#: 🔴 **换手口径约定**（与 Top-K 线**同源**）：
#:    ``manu/memory/ashare_topk_20261104.md``「单边换手定义」行 = ``0.5 × Σ|Δw|``
#:    ⇒ **Σ|Δw| 是双边**（= 总成交额 / 组合净值），**单边 = 0.5 × 双边**。
#:    V2 的 ``annualized_turnover`` = Σ|Δw| 求和 / 年数 ⇒ **双边**口径。
TURNOVER_CONVENTION = "two_way_sum_abs_dw"
#: **双边 → 单边**的换算系数（单边 = 0.5 × 双边；唯一实现见 :func:`one_way_turnover`）
TURNOVER_ONE_WAY_FACTOR = 0.5
#: 🔴 **换手目标模式**（用户 2026-11-18 拍板）：
#:    * ``"disclosure"``（**新默认**）：换手目标**只作披露基准**，
#:      **不再**作为选参硬门槛（``run_v2.apply_turnover_gate`` 不得用换手过滤候选 trial）；
#:    * ``"gate"``（**旧行为**，仅供审计对表）：达标 trial 优先，
#:      超标 trial **不许**靠高分胜出。
TURNOVER_TARGET_MODE = "disclosure"


def one_way_turnover(turnover: float) -> float:
    """**双边 → 单边**换手换算（**唯一实现**）。

    🔴 **口径同源**：本仓库 Top-K 线（``manu/memory/ashare_topk_20261104.md``
    「单边换手定义」行）定义 **单边 = 0.5 × Σ|Δw|** ⇒ **Σ|Δw| 是双边**
    （= 总成交额 / 组合净值）。V2 的 ``annualized_turnover`` 走的正是 Σ|Δw|
    ⇒ 它是**双边** ⇒ 单边必须乘 :data:`TURNOVER_ONE_WAY_FACTOR`（0.5）。

    参数
    ----
    ``turnover``
        **双边**换手（倍）。**非法 / 非有限（NaN、±inf）/ ``None`` ⇒ 返回
        ``float("nan")``（🔴 **不许返回 ``0.0``**）。

    🔴 **NaN 语义不许混搭**（``rules.md`` §十四.112.4）：双边**算不出来**时，
       单边**也必须是 NaN** ⇒ 否则读者会把「算不出来」误读成「**换手为零**」。
       ⇒ 与 V1 ``config.one_way_turnover`` 的 **NaN 透传**语义一致
       （V1：``float(turnover) * 0.5`` ⇒ NaN 进、NaN 出）。
       ⚠️ **有限输入的返回值逐位不变**（仍是 ``0.5 × 双边``）⇒ 本函数只改
       「**算不出来**」的表达方式，**一个数字都不许动**。

    例子
    ----
    >>> round(one_way_turnover(14.1002), 4)
    7.0501
    """
    try:
        turn = float(turnover)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(turn):
        return float("nan")
    return float(TURNOVER_ONE_WAY_FACTOR * turn)


#: 🔴🔴 **换手惩罚系数 + 上限**（2026-11-18 用户目标文档「为什么 V2 在盲测里大亏」修正）
#:
#:    **事故机制**：旧目标函数是 ``score = 超额 − 0.05 × max(0, 换手 − 4.0)``。
#:    在 cadence = 20 的折叠常量下，val 段实测换手最低 **12.32×**
#:    ⇒ 惩罚项恒为 ``0.05 × (12.32 − 4.0) ≈ 0.416``，
#:    而**超额本身的差异量级只有 0.05~0.20** ⇒ **惩罚项把超额完全盖住**
#:    ⇒ 目标函数事实上退化成「**最小化换手**」
#:    ⇒ 网格搜索为了攝那条「≤4×」红线，**把调仓周期拉到了 90 个交易日**
#:    ⇒ 冻结参数 = cadence 90 / MA120+80 / hold_buffer 0 / 臂 A15
#:    ⇒ 单盲组合 **−12.93% / 回撤 −54.93%**。
#:
#:    🔴 **修正**：惩罚项**必须有上限** ⇒ 它只当「超额接近时的换手 tie-breaker」，
#:    **不许**再买断 alpha。用户原话：「**时效性 > 换手率**」
#:    （V2 的 20 日 RankIC 0.1271 / ICIR 1.105 / t=9.04 ⇒ alpha 保质期 ≈ 1 个月）。
TURNOVER_PENALTY_RATE = 0.05
#: 🔴 **惩罚项上限**（无量纲，作用在「年化超额」同一量纲上）：
#:    12.32× 换手时原始惩罚 0.416 ⇒ 截断到 0.02 ⇒ 与超额差异量级（0.05~0.20）同阶，
#:    但**不再压倒**超额。
TURNOVER_PENALTY_CAP = 0.02
#: 🔴 **目标函数的唯一描述串**（2026-11-18 换手口径改版后）。
#:    * 换手惩罚**封顶** :data:`TURNOVER_PENALTY_CAP`；
#:    * :data:`TURNOVER_TARGET` **只作披露基准**，**不再是选参硬门槛**；
#:    * 换手口径 = **双边**（单边 = 0.5 × 双边）。
#:    ⇒ 🔴 **不许**在别的文件里另写一份（历史上同一句在 3 个文件里各写一遍，
#:      改口径时漏改 ⇒ 落盘产物里的 objective 字样变成**假话**）。
OBJECTIVE_DESCRIPTION = (
    f"扣费后年化净超额 − min({TURNOVER_PENALTY_CAP}, {TURNOVER_PENALTY_RATE} × "
    f"max(0, 年化换手(双边) − {TURNOVER_TARGET}))"
    f"（换手惩罚封顶 {TURNOVER_PENALTY_CAP}；{TURNOVER_TARGET}× 仅作披露基准，"
    f"不是选参硬门槛；单边换手 = 0.5 × 双边）"
)
#: 🔴 **换手闸门「一个都没达标」时的兜底口径**（2026-11-18 用户目标文档）：
#:    * ``"best_score"``（**新默认**）：按**主目标**（扣费后净超额）挑最好的那个，
#:      并把「≤4× 未达成」**如实披露**（``gate["ok"] = False``）——
#:      这才是「不许假装达标，也不许用换手率把 alpha 买断」。
#:    * ``"min_turnover"``（**旧口径**，仅供审计对表）：挑换手最低者 ⇒
#:      正是它把 cadence=90 选成了胜者。
TURNOVER_GATE_ON_MISS = "best_score"


def turnover_penalty(turnover: float, target: float | None = None) -> float:
    """换手超目标惩罚（**有上限**）——**唯一实现**（``run_v2.py`` 与滚动前推探针共用）。

    🔴 事故背景见 :data:`TURNOVER_PENALTY_CAP` 的注释：旧实现无上限 ⇒
    惩罚项（~0.42）把超额（~0.1）完全盖住 ⇒ 目标函数退化成「最小化换手」
    ⇒ 搜出 cadence=90 的「蜗牛参数」。

    参数
    ----
    ``turnover``
        年化**双边**换手（倍）；**单边 = 0.5×**（见 :func:`one_way_turnover`）。
    ``target``
        换手目标（默认 :data:`TURNOVER_TARGET` = 4.0）。
    """
    tgt = float(target if target is not None else TURNOVER_TARGET)
    try:
        turn = float(turnover)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(turn):
        return 0.0
    raw = TURNOVER_PENALTY_RATE * max(0.0, turn - tgt)
    return float(min(TURNOVER_PENALTY_CAP, raw))

# ----------------------------------------------------------------- 负分空仓保护（2026-11-18）
#: 🔴 **动机**（目标文档 Step 3）：V2 单盲「个股 RankIC 0.1271，但组合 −12.93%、
#:    回撤 −54.93%」⇒ 病根在 ETF 轮动层；当**候选 ETF 的穿透分 E 普遍转负**
#:    （成分股整体跑输打分池）时，**按比例削减权益到现金** ⇒ 不在系统性熊市里满仓硬扛。
#:
#: 🔴 **口径**：``exposure = clip(1 + stat / scale, min_exposure, 1)``
#:    （``stat`` = 候选池 E 的横截面中位数，默认 ``median``；``scale`` 越小越敏感）。
#:    **默认关闭**（``neg_score_gate = False``）⇒ 既有调用方行为逐字不变。
NEG_SCORE_GATE_ENABLED = False
#: 🔴 闸门统计量口径（候选池 E 的横截面统计）
NEG_SCORE_GATE_STAT = "median"
#: 🔴 闸门敏感度（``stat = -scale`` ⇒ 敞口归零）；**val 段**搜索空间
NEG_SCORE_GATE_SCALE_GRID: tuple[float, ...] = (0.02, 0.05, 0.10, 0.20)
#: 🔴 闸门敞口下限（不许清零 ⇒ 保留底仓）；**val 段**搜索空间
NEG_SCORE_GATE_MIN_EXPOSURE_GRID: tuple[float, ...] = (0.0, 0.3)

# ----------------------------------------------------------------- 下游平滑三臂（决策③）
#: 🔴 背景：实盘持有周期 = **20 个交易日**，而 V1 下游 EWMA 的记忆只有 **3.11 天**
#:    （V1 ``config.SMOOTHING_ALPHA = 0.20`` ⇒ 半衰期 ln0.5/ln0.8 = 3.106 交易日）
#:    ⇒ **记忆长度与持有周期不匹配** ⇒ V2 必须在 val(2022-2023) 上比完候选臂，
#:    **定死一个**写进冻结配置（2024-2026 单盲只跑最终冻结的那一臂）。
#: 臂 A（窗口拉长）：EWMA 半衰期拉到与持有周期同量级；
#:    🔴 alpha **由半衰期自算**（``alpha = 1 - 0.5**(1/half_life)``），
#:    **不许写死字面量**（0.04516 / 0.03406 只是自证值，见 ``v2/smoothing.py``）。
ARM_A_HALF_LIFE_DAYS = (15, 20)
#: 臂 B（原生平滑 + Rank Buffer）：**去掉**下游 EWMA 滤波器，
#:    改用 V2 的 20 日 Label 模型**原生慢变量输出**直接排序 + 分配 Rank Buffer。
ARM_B_ID = "B_native_slow"
#: 臂 C（现状对照）：V1 现状 alpha=0.20 / window=5 / history_span=30
#:    （🔴 **不得默认胜出**；数值抄自 V1 ``config.py``，由 ``v2/tests/test_smoothing_arms.py``
#:    与 V1 config 做**只读交叉校验**，防止两边漂移）。
ARM_C_ALPHA = 0.20
ARM_C_WINDOW = 5
ARM_C_HISTORY_SPAN_DAYS = 30
#: 🔴 候选臂清单（**只在 val 段比**；禁止把本清单里的东西送进 test 段做选择）
SMOOTHING_ARM_IDS = (
    "A_half_life_15",
    "A_half_life_20",
    ARM_B_ID,
    "C_v1_current",
)

# ----------------------------------------------------------------- V1 baseline（对比用）
#: 🔴 **V1 换手口径（2026-11-18 正名）**：V1 baseline 的 ``16.568483×`` 记的是
#:    **双边**（``Σ|Δw|`` = 总成交额 / 组合净值，与 :data:`TURNOVER_CONVENTION` 同源），
#:    **旧键名 ``annualized_one_way_turnover`` 属历史错标**（键写「单边」而值是双边）
#:    ⇒ 现正名为 ``annualized_turnover_two_way``（**数值不变**），
#:    并**并列**给出单边（``= 0.5 × 双边``，唯一实现 = :func:`one_way_turnover`）。
#:    算术自证：``16.568 × 6.5bp × 2.716 年 ≈ 2.93%`` = V1 记录的成本拖累
#:    ⇒ 16.57× 只能是**双边**。
V1_BASELINE = {
    "annualized_return": 0.19462714,      # 年化 19.4627%
    "excess_vs_510300": 0.06562178,       # 超额 +6.5622%
    "max_drawdown": -0.32285128,          # MDD −32.29%
    "benchmark_510300": 0.12106112,       # 基准 12.1061%
    # 🔴 双边（Σ|Δw|）换手 16.57×（旧键名 ``annualized_one_way_turnover`` 属历史错标 ⇒ 已正名）
    "annualized_turnover_two_way": 16.568483,
    # 🔴 单边换手 ≈ 8.28×（= 0.5 × 双边；**只走唯一实现** ``one_way_turnover``，不许写死）
    #    ⚠️ 2026-11-18 审计（NaN 语义正名）：本文件里 ``one_way_turnover`` 的**唯一**调用点
    #    就在这一行 ⇒ 入参是**有限常量** ⇒ **不依赖**旧的「NaN ⇒ 0.0」语义
    #    （该语义已废除：算不出来 ⇒ NaN）⇒ 本行数值**逐位不变**（8.2842415）。
    "annualized_turnover_one_way": one_way_turnover(16.568483),
    "rank_ic_20d": 0.0616,                # 20 日 RankIC（用户原文口径）
    "rank_ic_1d": 0.04778,
    "icir": 0.4476,
    "t_stat": 3.718,
}
