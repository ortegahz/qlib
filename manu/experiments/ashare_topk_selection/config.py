"""A 股截面打分 + Top-K 选股：统一配置。

⚠️ 所有参数在此显式声明，**禁止散落在脚本里**。
日期区间由 `inspect_dataset.py` 的实测结果填入，**不得照抄文档里的 2018-2024**。

【实测覆盖（2026-11-04，inspect_dataset.py）】
  - 数据源: chenditc/investment_data `qlib_bin.tar.gz`
    md5=3010e494e31de446877894059ceb9503, 570,483,992 B, 解压 852 MB
  - 交易日历: 2000-01-04 ~ 2026-11-03, 共 6501 个交易日
  - 标的: 6174 只 (含北交所), 10 个日频字段
    (adjclose/amount/change/close/factor/high/low/open/volume/vwap)
  - 股票池文件: all / csi300 / csi500 / csi800 / csi1000 / csiall (无 csi100)
  - csi300 在最后交易日有 300 只
=> 切分按此实测重设；test 段必须含数据最后一天，否则拿不到「当期」推荐。
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- 数据与环境
PROVIDER_URI = "/mnt/data/siping/datasets/qlib/cn_data"   # 绝对路径，不依赖默认值
REGION = "cn"
DATASET_ROOT = Path("/mnt/data/siping/datasets/qlib")
# 数据来源清单（fetch_qlib_dataset.py 产出），用于 manifest 记录「数据版本」
FETCH_MANIFEST = DATASET_ROOT / "fetch_manifest.json"

# 重活产物落服务器持久盘，不落根分区
EXP_ROOT = "/mnt/data/siping/runs/ashare_topk"
RECORDER_URI = f"file:{EXP_ROOT}/mlruns"
EXPERIMENT_NAME = "ashare_topk_selection"
# 每次运行一个独立子目录: runs/<recorder_id>/{manifest.json,pred.pkl,top5.json}
RUNS_DIR = Path(EXP_ROOT) / "runs"

# ---------------------------------------------------------------- 时间切分
# 🔴 依据实测日历（2000-01-04 ~ 2026-11-03）重设，不是照抄文档
#    test 段右端 = 数据最后交易日 => 导出的 Top5 是「当期快照」
# 🔴 START_TIME 必须**早于 TRAIN 起点**，留出预热期。
#    Alpha158 最长回看 60 个交易日（Ref($close, 59)），qlib 会自动向前扩展取数
#    （LocalExpressionProvider.expression: query_start = start_index - lft_etd），
#    但扩展不出 handler start_time 之前的数据 ⇒ 若两者相同，训练段开头 60 天的
#    特征会是 NaN→Fillna 填 0 的垃圾值。这里留 1 年预热。
START_TIME = "2015-01-01"
END_TIME = "2026-11-03"
TRAIN = ("2016-01-01", "2021-12-31")     # 6 年
VALID = ("2022-01-01", "2023-12-31")     # 2 年，用于 early stopping
TEST = ("2024-01-01", "2026-11-03")      # 2.8 年，含数据最后一天
# 🔴 fit_* 必须**严格落在 train 段内**（rules.md §12.7）
#    这两个窗口只有被 ZScoreNorm 之类的处理器消费时才真正生效，
#    所以 infer_processors 里必须挂一个接受 fit_start/fit_end 的处理器。
FIT_START = "2016-01-01"
FIT_END = "2021-12-31"

# ---------------------------------------------------------------- 股票池
# 中大盘：沪深300。备选中证500（更宽的风格暴露）。
INSTRUMENTS = "csi300"

# ---------------------------------------------------------------- 数据处理器
# 🔴 loader 产出的是 MultiIndex 列（level0 = feature / label）
#    ⇒ 凡是只该作用在特征上的处理器都必须写 fields_group="feature"。
#    特别地 Fillna 若不限定 group，会把 label 的 NaN 填成 0，
#    导致后面的 DropnaLabel 失效（末位交易日无未来数据却不会被剔除）。
INFER_PROCESSORS = [
    {"class": "ProcessInf", "kwargs": {}},
    # ZScoreNorm.__init__(fit_start_time, fit_end_time, fields_group)
    # ⇒ check_transform_proc 会把 FIT_START/FIT_END 注入进来，
    #    统计量只在 train 窗口上拟合，fit_* 才是「承重」的（rules.md §12.7）
    {"class": "ZScoreNorm", "kwargs": {"fields_group": "feature"}},
    {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
]
# 标签：Alpha158 内置口径 = T+1 VWAP -> T+2 VWAP 的收益（实测比 close 口径好，见下）
LABEL_EXPRESSIONS = ["Ref($vwap, -2)/Ref($vwap, -1) - 1"]
LABEL_NAMES = ["LABEL0"]
# 🔴 标签用 **CSRankNorm** 而不是 Alpha158 默认的 CSZScoreNorm。
#    实测（2026-11-04，全量数据，daily RankIC）：
#        CSZScoreNorm 标签 -> valid IC 0.0154
#        CSRankNorm   标签 -> valid IC 0.0330   （IC 直接翻倍）
#    原因：原始标签重尾（实测 min=-7.70 / max=+10.31），z-score 标准化后
#    极端值仍主导 MSE 梯度；改成截面排名后模型学的是「相对排名」，对尾部稳健。
LEARN_PROCESSORS = [
    {"class": "DropnaLabel"},
    {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}},
]

# ---------------------------------------------------------------- 模型
# LightGBM。`random_state` 通过 kwargs → self.params 直达 LightGBM
# （qlib/contrib/model/gbdt.py:19-23），这是本臂唯一的种子挂载点。
# 🔴 超参由 2026-11-04 的网格探针在【valid 段】选出来（不看 test，避免测试集污染）：
#      lr=0.02 rounds=500 -> valid IC 0.0386 / ICIR 0.321 ; test IC 0.0381 / ICIR 0.255
#      lr=0.04 rounds=500 -> valid IC 0.0330
#      lr=0.04 rounds=1500-> valid IC 0.0254
#      lr=0.08 rounds=500 -> valid IC 0.0278
#    官方 example 的 lambda_l1=205.6999 / lambda_l2=580.9768 是为【原始尺度特征】调的；
#    本工程特征做了 ZScoreNorm（标准化到 O(1)）后这套强正则会把模型压成常数
#    （实测 valid IC 仅 0.0074），故改为不施加强 L1/L2，靠学习率 + 轮数控过拟合。
LGB_PARAMS = dict(
    loss="mse",
    colsample_bytree=0.88,
    learning_rate=0.02,
    subsample=0.87,
    # 🔴 LightGBM 的 bagging_freq 默认 0 = 关闭 bagging，
    #    此时 subsample 是静默空操作 ⇒ 必须显式打开，否则配置名不副实
    bagging_freq=1,
    lambda_l1=0.0,
    lambda_l2=0.0,
    max_depth=8,
    num_leaves=210,
    num_threads=20,        # 服务器 80 核，官方 example 用 20
    random_state=42,       # 🔴 LightGBM 臂的种子（qlib 层无 seed 参数）
    deterministic=True,    # 🔴 配合 random_state 保证同机可复现
    force_row_wise=True,
    verbosity=-1,
)
NUM_BOOST_ROUND = 500
# 🔴 早停必须关掉（None）。
#    qlib 的 LGBModel.fit 把 [train, valid] 一起塞进 LightGBM 的 valid_sets
#    （qlib/contrib/model/gbdt.py:78-86），而 LightGBM 早停后会**按最早的最优迭代裁剪模型**；
#    本数据上 valid 指标第 1~2 轮之后单调变差 => best_iteration=1~2
#    => 模型只剩 1~2 棵树，预测退化（实测 uniq_pred=4，日度 IC≈0）。
#    改用固定轮数 + 网格选参（见上）后 IC 从 ~0 提升到 0.0386。
EARLY_STOPPING_ROUNDS = None

# ---------------------------------------------------------------- 组合与回测
TOPK = 30          # 每日持有得分最高的前 30 只
N_DROP = 5         # 每次调仓换掉尾部 5 只（用户文档的「降低换手」建议）
ACCOUNT = 1_000_000.0
BENCHMARK = "SH000300"
# 策略默认值（rules.md §12.1 口径必须点名）：
#   only_tradable=False          qlib 默认，策略不对「不可交易」额外过滤
#   forbid_all_trade_at_limit=True qlib 默认，涨跌停时整单不成交
STRATEGY_EXTRA_KWARGS = {
    "only_tradable": False,
    "forbid_all_trade_at_limit": True,
}

# 成本假设（rules.md §12.1：必须点名 qlib Exchange 的 7 个参数）
EXCHANGE_KWARGS = dict(
    limit_threshold=0.095,   # A 股涨跌停：涨停买不进 / 跌停卖不出
    deal_price="close",
    open_cost=0.0003,        # 买入佣金
    close_cost=0.0013,       # 卖出佣金 + 印花税
    min_cost=5,
    trade_unit=100,          # A 股一手 = 100 股
    impact_cost=0.0,
)

# ---------------------------------------------------------------- 种子
GLOBAL_SEED = 42

# ---------------------------------------------------------------- 输出
TOPK_EXPORT_N = 5     # 最终要交付的 Top-N 清单


def build_task_config() -> dict:
    """构造 qlib workflow 的 task_config。"""
    return {
        "model": {
            "class": "LGBModel",
            "module_path": "qlib.contrib.model.gbdt",
            "kwargs": {
                **LGB_PARAMS,
                "num_boost_round": NUM_BOOST_ROUND,
                "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
            },
        },
        "dataset": {
            "class": "DatasetH",
            "module_path": "qlib.data.dataset",
            "kwargs": {
                "handler": {
                    "class": "Alpha158",
                    "module_path": "qlib.contrib.data.handler",
                    "kwargs": {
                        "start_time": START_TIME,
                        "end_time": END_TIME,
                        # 🔴 fit_* 严格落在 train 段内（rules.md §12.7）
                        "fit_start_time": FIT_START,
                        "fit_end_time": FIT_END,
                        "instruments": INSTRUMENTS,
                        # 显式指定标签，避免 handler 默认值悄悄变化导致口径漂移
                        "label": (LABEL_EXPRESSIONS, LABEL_NAMES),
                        "infer_processors": INFER_PROCESSORS,
                        "learn_processors": LEARN_PROCESSORS,
                    },
                },
                "segments": {
                    "train": TRAIN,
                    "valid": VALID,
                    "test": TEST,
                },
            },
        },
        "port_analysis": {
            "strategy": {
                "class": "TopkDropoutStrategy",
                "module_path": "qlib.contrib.strategy",
                "kwargs": {
                    "signal": "<PRED>",
                    "topk": TOPK,
                    "n_drop": N_DROP,
                    **STRATEGY_EXTRA_KWARGS,
                },
            },
            "backtest": {
                "start_time": TEST[0],
                "end_time": TEST[1],
                "account": ACCOUNT,
                "benchmark": BENCHMARK,
                "exchange_kwargs": dict(EXCHANGE_KWARGS),
            },
        },
    }


def ensure_exp_root() -> Path:
    """建好产物根目录与 runs/ 子目录。"""
    p = Path(EXP_ROOT)
    (p / "mlruns").mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return p


def latest_run_dir() -> Path | None:
    """返回 runs/ 下**含 manifest.json** 的最新一次运行目录；没有则 None。

    按 manifest 的存在性过滤，而不是按目录 mtime —— 避免把「跑到一半、
    还没写 manifest」的目录当成有效结果。
    """
    if not RUNS_DIR.is_dir():
        return None
    cands = [d for d in RUNS_DIR.iterdir() if d.is_dir() and (d / "manifest.json").is_file()]
    if not cands:
        return None
    return max(cands, key=lambda d: (d / "manifest.json").stat().st_mtime)


def dataset_version() -> dict:
    """读取数据来源清单，作为「数据版本」写进 manifest（rules.md §12.8）。"""
    if not FETCH_MANIFEST.is_file():
        return {"fetch_manifest": None, "note": f"未找到 {FETCH_MANIFEST}"}
    try:
        data = json.loads(FETCH_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"fetch_manifest": str(FETCH_MANIFEST), "error": f"{type(exc).__name__}: {exc}"}
    return {
        "fetch_manifest": str(FETCH_MANIFEST),
        "generated_at": data.get("generated_at"),
        "sources": [
            {
                "key": s.get("key"),
                "repo": s.get("repo"),
                "tag": s.get("tag"),
                "asset": s.get("asset"),
                "bytes": s.get("bytes"),
                "md5": s.get("md5"),
            }
            for s in data.get("sources", [])
        ],
    }
