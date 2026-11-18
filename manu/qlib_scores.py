"""全市场个股打分（S）—— ETF 映射层 `E = W × S` 的上游输入。

【定位】
`manu/experiments/ashare_topk_selection/` 已经在 **csi300** 上训练好 LightGBM+Alpha158
（test RankIC 0.038）。但 ETF 的成分股**大量落在 csi300 之外**（中证500/1000/创业板/科创板…）
⇒ 必须把打分池扩到**全市场**（`instruments="all"`，6174 只）。

【口径纪律（rules.md §12.3 / §12.7）】
- **建模参数一律从个股线 config 复用**（`config.py` 里的 `STOCK`），本文件**不重复定义**；
  只改「股票池」这一项（csi300 → all），其余（Alpha158 / CSRankNorm+VWAP 标签 /
  ZScoreNorm 拟合窗 / LGBModel 超参 / 早停关闭 / 切分）**逐项不变**；
- 特征与标签的 `fit_*` 统计量**仍只取个股线训练窗**（2016-01-01~2021-12-31）
  ⇒ **评估区间不参与归一化统计**（无泄漏）；
- 🔴 **归一化统计量必须「训练期拟合 → 推理期冻结」**（见 `FrozenZScoreNorm`）：
  qlib 的 `ZScoreNorm.fit()` 在 **fit 窗为空**时 `np.nanmean` 会**静默返回 NaN**
  ⇒ `(x-NaN)/NaN` ⇒ 特征全 NaN ⇒ `Fillna(0)` 之后**全市场分数塌成同一个常数**
  （静默灾难：分数看起来有值，其实毫无信息）。本文件用
  `build_fit_handler()` 复现**训练期** handler 拿到 `mean_train/std_train`，
  再用 `FrozenZScoreNorm` 把它们**冻结**进推理期 handler ⇒ 推理特征与训练期**逐位同分布**；
- 退化守卫：任一打分日若分数方差为 0 / 唯一值数 < 10 ⇒ **直接抛错**，杜绝静默垃圾分数；
- 种子：LightGBM `random_state=42`（qlib 层无全局 seed，见 rules.md §12.3）。

【打分日 = 调仓日】
只对**每 N 个交易日**（默认 10，双周）打分一次 ⇒ 内存与耗时都省，
且与「人工双周调仓」的真实节奏一致。

【输出】
- `stock_scores.csv`：`date, instrument, score, fwd_ret`（fwd_ret = 原始前瞻收益，用于算 IC）
- `stock_scores_manifest.json`：IC 统计（IC/ICIR/t）、覆盖率 M/N、模型来源与 md5、口径声明

【执行机】服务器；解释器 `/home/huangzhe/anaconda3/envs/ql/bin/python`
【用法】
    export PYTHONHASHSEED=0
    python manu/etf_rotation/qlib_scores.py --mode infer --out-dir <落点根>/runs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "data"))
import config as C  # noqa: E402
import signal_policy as SP  # noqa: E402

#: 个股线已训好的模型（控制组基准复用，不重训）
DEFAULT_MODEL_DIR = Path("/mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9")


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="全市场个股打分（ETF 映射层上游 S）")
    p.add_argument("--mode", choices=["infer", "train"], default="infer",
                   help="infer=复用个股线已训模型；train=在全市场池上重训（严格版）")
    p.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR), help="mode=infer 时的模型目录")
    p.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT), help="产物目录")
    p.add_argument("--rebalance-every", type=int, default=C.REBALANCE_EVERY_N_DAYS,
                   help=f"每 N 个交易日打分一次（默认 {C.REBALANCE_EVERY_N_DAYS}）")
    p.add_argument("--start", default=None, help="打分起始日（默认取 test 段起点）")
    p.add_argument("--end", default=None, help="打分结束日（默认数据最后交易日）")
    p.add_argument("--universe", default=C.FULL_UNIVERSE, help="qlib 股票池（默认 all = 全市场）")
    p.add_argument(
        "--provider-uri",
        default=None,
        help=(
            "qlib provider 目录（默认 = 上游基座 cn_data）；"
            "阶段 1 自建增量日时指向派生目录 cn_data_live"
        ),
    )
    p.add_argument(
        "--validate-pred",
        default=str(DEFAULT_MODEL_DIR / "pred.pkl"),
        help="个股线 pred.pkl 路径；用它交叉校验本次打分在 csi300 上是否与训练线一致",
    )
    return p.parse_args()


# ------------------------------------------------------------------ qlib 侧

#: 🔴 **provider_uri 覆盖**（阶段 1 自建增量日派生目录用）：
#: 默认仍是 `C.PROVIDER_URI`（上游基座）；传 `--provider-uri` ⇒ 指向派生目录
#: （`cn_data_live`，上游快照滞后时的兜底）⇒ **不改任何默认路径**。
_PROVIDER_URI_OVERRIDE: str | None = None


def provider_uri() -> str:
    """本次实际使用的 provider_uri（覆盖值优先）。"""
    return _PROVIDER_URI_OVERRIDE or C.PROVIDER_URI


def init_qlib() -> None:
    import qlib
    from qlib.constant import REG_CN

    qlib.init(provider_uri=provider_uri(), region=REG_CN)


class FrozenZScoreNorm:
    """占位：真实实现见下方 `_frozen_norm_class()`（需要 qlib 运行时才能继承 Processor）。"""


def _frozen_norm_class():
    """构造 `FrozenZScoreNorm` 类（延迟到 qlib 导入之后，避免模块级依赖 qlib 运行时）。"""
    from qlib.data.dataset.processor import ZScoreNorm

    class FrozenZScoreNorm(ZScoreNorm):  # noqa: D401
        """复用**训练期**统计量的 ZScoreNorm —— 推理期**不再重新拟合**。

        🔴 存在的理由（2026-11-04 实测踩坑）：
        qlib `ZScoreNorm.fit()` 用 `fetch_df_by_index(df, slice(fit_start, fit_end))`
        取统计量。当 handler 的数据窗**不覆盖** fit 窗时（例如数据窗 2023-06 起、
        fit 窗 2016-01~2021-12），切片为空 ⇒ `np.nanmean` 返回 NaN（只发一条
        `RuntimeWarning: Degrees of freedom <= 0`）⇒ 特征 `(x-NaN)/NaN` 全变 NaN
        ⇒ `Fillna(0)` 之后**所有股票的分数变成同一个常数**（min==max），
        表面「跑通了」，实际零信息。

        本类把训练期的 `mean_train/std_train` 冻结进实例，`fit()` 退化为 no-op，
        保证推理期特征与训练期**逐位同分布**（不引入任何 test 期统计量）。
        """

        def fit(self, df: pd.DataFrame = None):
            if getattr(self, "mean_train", None) is None or getattr(self, "std_train", None) is None:
                raise ValueError("FrozenZScoreNorm 尚未注入训练期统计量（mean_train/std_train）")
            return self

    return FrozenZScoreNorm


def build_fit_handler():
    """构造**训练期** handler：股票池 / 时间窗 / 处理器与个股线训练时**逐项一致**。

    唯一作用：拿到与训练时**逐位相同**的 `ZScoreNorm` 统计量（`mean_train/std_train`），
    供推理期冻结复用。特征是**回看型**（Alpha158 最长回看 60 日），所以
    `[START_TIME, FIT_END]` 的 handler 在 fit 窗内的特征值与
    `[START_TIME, END_TIME]` 的 handler **完全相同** ⇒ 统计量可复现。
    """
    from qlib.contrib.data.handler import Alpha158

    return Alpha158(
        start_time=C.STOCK.START_TIME,
        end_time=C.STOCK.FIT_END,
        fit_start_time=C.STOCK.FIT_START,
        fit_end_time=C.STOCK.FIT_END,
        instruments=C.STOCK.INSTRUMENTS,
        label=(C.STOCK.LABEL_EXPRESSIONS, C.STOCK.LABEL_NAMES),
        infer_processors=C.STOCK.INFER_PROCESSORS,
        learn_processors=C.STOCK.LEARN_PROCESSORS,
    )


def extract_frozen_norm(fit_handler):
    """从训练期 handler 里取出拟合好的 ZScoreNorm，冻结成推理期可用的处理器实例。"""
    from qlib.data.dataset.processor import ZScoreNorm

    frozen_cls = _frozen_norm_class()
    zs = None
    for proc in fit_handler.get_all_processors():
        if isinstance(proc, ZScoreNorm) and not isinstance(proc, frozen_cls):
            zs = proc
    if zs is None:
        raise RuntimeError("训练期 handler 里找不到 ZScoreNorm（个股线 config 的 INFER_PROCESSORS 变了？）")
    if not np.isfinite(np.asarray(zs.mean_train)).all() or not np.isfinite(np.asarray(zs.std_train)).all():
        raise RuntimeError("训练期 ZScoreNorm 统计量含 NaN/Inf ⇒ fit 窗无效，拒绝继续")

    frozen = frozen_cls(zs.fit_start_time, zs.fit_end_time, fields_group=zs.fields_group)
    frozen.mean_train = np.array(zs.mean_train, copy=True)
    frozen.std_train = np.array(zs.std_train, copy=True)
    frozen.ignore = np.array(zs.ignore, copy=True)
    frozen.cols = list(zs.cols)
    return frozen


def build_score_handler(universe: str, frozen, end_time: str | None = None):
    """构造**打分期** handler：全市场股票池 + **冻结的训练期**归一化统计量。

    与个股线 handler 的差异**只有股票池**（csi300 → ``universe``）；
    归一化统计量来自训练期（见 `FrozenZScoreNorm`），不是本窗口重新拟合的。

    🔴 M-S2（2026-11-05 06:2x 实测）：``end_time`` 必须跟着 **qlib 数据集实际覆盖末日**走。
       原来写死 ``C.SCORE_END_TIME``（= 2026-11-03）⇒ 数据集一旦往前重建，
       handler 的数据窗仍停在 11-03 ⇒ 新日期**打不出分**（静默无输出）。
       ⇒ 改为可传参，调用方按日历实际末日给（默认保持原值 ⇒ 行为不变）。
    """
    from qlib.contrib.data.handler import Alpha158

    infer_processors = [
        {"class": "ProcessInf", "kwargs": {}},
        frozen,
        {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
    ]
    return Alpha158(
        start_time=C.SCORE_START_TIME,
        end_time=end_time or C.SCORE_END_TIME,
        fit_start_time=C.STOCK.FIT_START,
        fit_end_time=C.STOCK.FIT_END,
        instruments=universe,
        label=(C.STOCK.LABEL_EXPRESSIONS, C.STOCK.LABEL_NAMES),
        infer_processors=infer_processors,
        learn_processors=[],
    )


def build_train_handler(universe: str):
    """`--mode train` 用：数据窗**完整覆盖 fit 窗**的全市场 handler（自洽重训口径）。"""
    from qlib.contrib.data.handler import Alpha158

    return Alpha158(
        start_time=C.STOCK.START_TIME,
        end_time=C.SCORE_END_TIME,
        fit_start_time=C.STOCK.FIT_START,
        fit_end_time=C.STOCK.FIT_END,
        instruments=universe,
        label=(C.STOCK.LABEL_EXPRESSIONS, C.STOCK.LABEL_NAMES),
        infer_processors=C.STOCK.INFER_PROCESSORS,
        learn_processors=C.STOCK.LEARN_PROCESSORS,
    )


def trading_calendar(start: str, end: str) -> pd.DatetimeIndex:
    from qlib.data import D

    cal = D.calendar(start_time=start, end_time=end, freq="day")
    return pd.DatetimeIndex(cal)


def rebalance_dates(cal: pd.DatetimeIndex, every_n: int, start: str, end: str) -> list[pd.Timestamp]:
    """从交易日历里挑出**调仓日**（= 打分日栅格）。

    🔴 M-S2（2026-11-05 06:2x）：口径已**下沉到 `data/signal_policy.py::score_grid_dates`**
       （唯一事实源）⇒ 打分脚本与「信号日可行性守卫」用的是**同一套相位口径**
       ⇒ 不会出现「守卫说不是打分日、打分脚本却打了」这种口径漂移。
       本函数保留原签名与返回类型（`pd.Timestamp` 列表），语义**逐位不变**：
       相位 = ``i % every_n == every_n - 1``，且**永远包含最后一个交易日**。
    """
    grid = SP.score_grid_dates(
        [d.date() for d in cal], every_n, start=start, end=end
    )
    return [pd.Timestamp(d) for d in grid]


def load_or_train_model(mode: str, model_dir: Path, dataset, dataset_segments: dict):
    """返回 (model, meta)。mode=infer 直接 unpickle 个股线模型；mode=train 全市场重训。"""
    from qlib.contrib.model.gbdt import LGBModel

    if mode == "infer":
        model_path = model_dir / "model.pkl"
        if not model_path.is_file():
            raise FileNotFoundError(f"找不到 {model_path}；请用 --mode train 或指定 --model-dir")
        with model_path.open("rb") as f:
            model = pickle.load(f)
        meta = {
            "source": "reuse_stock_line_model",
            "model_path": str(model_path),
            "model_md5": md5_of(model_path),
            "trained_on_universe": f"{C.STOCK.INSTRUMENTS}（个股线）",
            "scored_on_universe": None,
            "norm_stats_source": (
                f"训练期 handler（instruments={C.STOCK.INSTRUMENTS}, "
                f"fit {C.STOCK.FIT_START}~{C.STOCK.FIT_END}）拟合后冻结复用"
            ),
        }
        return model, meta

    model = LGBModel(
        loss=C.STOCK.LGB_PARAMS["loss"],
        colsample_bytree=C.STOCK.LGB_PARAMS["colsample_bytree"],
        learning_rate=C.STOCK.LGB_PARAMS["learning_rate"],
        subsample=C.STOCK.LGB_PARAMS["subsample"],
        bagging_freq=C.STOCK.LGB_PARAMS["bagging_freq"],
        lambda_l1=C.STOCK.LGB_PARAMS["lambda_l1"],
        lambda_l2=C.STOCK.LGB_PARAMS["lambda_l2"],
        max_depth=C.STOCK.LGB_PARAMS["max_depth"],
        num_leaves=C.STOCK.LGB_PARAMS["num_leaves"],
        num_threads=C.STOCK.LGB_PARAMS["num_threads"],
        random_state=C.STOCK.LGB_PARAMS["random_state"],
        deterministic=C.STOCK.LGB_PARAMS["deterministic"],
        force_row_wise=C.STOCK.LGB_PARAMS["force_row_wise"],
        verbosity=C.STOCK.LGB_PARAMS["verbosity"],
        num_boost_round=C.STOCK.NUM_BOOST_ROUND,
        early_stopping_rounds=C.STOCK.EARLY_STOPPING_ROUNDS,
    )
    model.fit(dataset, num_boost_round=C.STOCK.NUM_BOOST_ROUND)
    meta = {
        "source": "retrain_full_universe",
        "universe": dataset_segments["universe"],
        "num_boost_round": C.STOCK.NUM_BOOST_ROUND,
        "early_stopping_rounds": C.STOCK.EARLY_STOPPING_ROUNDS,
        "lgb_params": dict(C.STOCK.LGB_PARAMS),
    }
    return model, meta


def fetch_fwd_returns(dates: list[pd.Timestamp], universe: str) -> pd.DataFrame:
    """取原始前瞻收益（用于 IC 评估）：表达式与 config.LABEL_EXPRESSIONS 完全一致。

    🔴 `D.features()` 的第一个参数必须是 **list/Index**（`get_instruments_d` 只认
    dict / list / tuple / Index / ndarray，**不认 str**）⇒ 不能直接传 "all"，
    必须先 `D.list_instruments(instruments="all", as_list=True)` 展开成代码表。
    """
    from qlib.data import D

    # 🔴 `D.list_instruments` 的第一个参数必须是 **dict**（内部取 `instruments["market"]`
    #    与 `instruments["filter_pipe"]`），不是字符串 —— 传 str 会报
    #    `TypeError: string indices must be integers`
    insts = list(D.list_instruments({"market": universe, "filter_pipe": []}, as_list=True))
    if not insts:
        raise RuntimeError(f"qlib 股票池 {universe!r} 为空")
    expr = C.STOCK.LABEL_EXPRESSIONS[0]
    df = D.features(
        insts,
        [expr],
        start_time=str(dates[0].date()),
        end_time=str(dates[-1].date()),
        freq="day",
    )
    # 🔴 `D.features` 返回的 MultiIndex 名字是 **(instrument, datetime)**（不是 datetime 在前），
    #    列名是**表达式原文** ⇒ 必须显式规范化成 (date, instrument, fwd_ret)
    df = df.reset_index()
    df = df.rename(columns={"datetime": "date"})
    df = df.rename(columns={c: "fwd_ret" for c in df.columns if c not in ("date", "instrument")})
    df["date"] = pd.to_datetime(df["date"])
    df["instrument"] = df["instrument"].astype(str)
    return df[["date", "instrument", "fwd_ret"]]


def assert_non_degenerate(pred: pd.DataFrame, day) -> None:
    """打分日守卫：分数塌成常数 ⇒ 直接抛错（宁可不产出，也绝不产出假分数）。

    2026-11-04 实测踩坑：fit 窗落在数据窗之外 ⇒ ZScoreNorm 统计量 NaN ⇒
    特征全 NaN ⇒ Fillna(0) ⇒ **69 个调仓日的分数 min==max 全是 -0.1329**，
    而脚本仍然「跑完了」。这个守卫就是为拦住这类静默灾难。
    """
    vals = pred["score"].to_numpy(dtype=float)
    finite = vals[np.isfinite(vals)]
    n_unique = int(np.unique(finite).size)
    std = float(finite.std()) if finite.size else 0.0
    if finite.size == 0:
        raise RuntimeError(f"{day.date()} 打分全为空")
    if std <= 0.0 or n_unique < 10:
        raise RuntimeError(
            f"{day.date()} 打分退化：n={finite.size}, std={std:.3e}, 唯一值={n_unique} "
            "⇒ 特征/模型输出异常（大概率是归一化统计量失效），拒绝写出"
        )


def validate_against_stock_line(scores: pd.DataFrame, pred_pkl: Path) -> dict[str, Any]:
    """与个股线 `pred.pkl` 交叉校验：同一 (date, instrument) 上分数应**几乎完全一致**。

    逻辑：个股线的 pred 是「csi300 股票池 + 训练期归一化统计量」跑出来的；
    本次是「全市场股票池 + **冻结的同一套**训练期统计量」。
    对 csi300 的成分股，两条路径的输入特征应当逐位相同 ⇒ 分数应一致。
    这是「归一化统计量转移是否做对」的最强验证。
    """
    if not pred_pkl.is_file():
        return {"available": False, "reason": f"{pred_pkl} 不存在"}
    ref = pd.read_pickle(pred_pkl)
    if isinstance(ref, pd.Series):
        ref = ref.to_frame("score")
    ref = ref.reset_index()
    ref = ref.rename(columns={ref.columns[0]: "date", ref.columns[1]: "instrument"})
    if "score" not in ref.columns:
        ref = ref.rename(columns={ref.columns[2]: "score"})
    ref["date"] = pd.to_datetime(ref["date"])
    ref["instrument"] = ref["instrument"].astype(str)

    cur = scores[["date", "instrument", "score"]].copy()
    cur["date"] = pd.to_datetime(cur["date"])
    cur["instrument"] = cur["instrument"].astype(str)
    j = cur.merge(ref[["date", "instrument", "score"]], on=["date", "instrument"],
                  suffixes=("_cur", "_ref"))
    if j.empty:
        return {"available": False, "reason": "与个股线 pred.pkl 没有重叠的 (date, instrument)"}
    diff = (j["score_cur"] - j["score_ref"]).abs()
    denom = j["score_ref"].abs().clip(lower=1e-12)
    return {
        "available": True,
        "ref_path": str(pred_pkl),
        "n_overlap": int(len(j)),
        "n_dates": int(j["date"].nunique()),
        "date_min": str(j["date"].min().date()),
        "date_max": str(j["date"].max().date()),
        "max_abs_diff": float(diff.max()),
        "mean_abs_diff": float(diff.mean()),
        "max_rel_diff": float((diff / denom).max()),
        "spearman": float(j["score_cur"].corr(j["score_ref"], method="spearman")),
        "pearson": float(j["score_cur"].corr(j["score_ref"], method="pearson")),
    }


def ic_stats(scores: pd.DataFrame) -> dict[str, Any]:
    """逐日算 IC（Pearson 与 Spearman），再汇总 ICIR / t 值 / 覆盖率。"""
    per_day: list[dict[str, Any]] = []
    for date, grp in scores.groupby("date", sort=True):
        grp = grp.dropna(subset=["score", "fwd_ret"])
        n = len(grp)
        if n < 30:
            per_day.append({"date": str(date.date()), "n": n, "ic": None, "rank_ic": None})
            continue
        ic = float(np.corrcoef(grp["score"], grp["fwd_ret"])[0, 1])
        rank_ic = float(grp["score"].corr(grp["fwd_ret"], method="spearman"))
        per_day.append({"date": str(date.date()), "n": n, "ic": ic, "rank_ic": rank_ic})

    ics = [d["ic"] for d in per_day if d["ic"] is not None]
    rank_ics = [d["rank_ic"] for d in per_day if d["rank_ic"] is not None]
    out: dict[str, Any] = {"per_day": per_day}
    for name, arr in (("ic", ics), ("rank_ic", rank_ics)):
        if not arr:
            out[name] = {
                "mean": None,
                "std": None,
                "icir": None,
                "t": None,
                "n_days": 0,
                "icir_annualized": None,
            }
            continue
        mean = float(np.mean(arr))
        # 🔴 铁律六（禁编数字）：只有 1 个有效 IC 日时 ``ddof=1`` 的标准差**不存在**
        #   ⇒ 原来写死 ``0.0`` 是**编出来的 0**（会被误读成「IC 完全没有波动」）⇒ 改 ``None``。
        std = float(np.std(arr, ddof=1)) if len(arr) > 1 else None
        icir = mean / std if std is not None and std > 0 else None
        t = (
            mean / (std / np.sqrt(len(arr)))
            if std is not None and std > 0 and len(arr) > 1
            else None
        )
        out[name] = {
            "mean": mean,
            "std": std,
            "icir": icir,
            "t": t,
            "n_days": len(arr),
            "icir_annualized": icir * np.sqrt(238) if icir is not None else None,
        }
    ns = [d["n"] for d in per_day]
    out["coverage"] = {
        "n_dates": len(per_day),
        "min_n": int(min(ns)) if ns else 0,
        "max_n": int(max(ns)) if ns else 0,
        "median_n": float(np.median(ns)) if ns else 0,
    }
    return out


# ------------------------------------------------------------------ 统计量呈现

#: 缺失统计量的统一占位符（铁律六「禁编数字」：算不出来就写「—」，不许填 0 / 编造）
MISSING_STAT = "—"

#: 标签表达式里向前看的交易日数（``Ref($vwap, -2)`` ⇒ 2），用于解释「为什么 IC 算不出来」
_LABEL_REF_RE = re.compile(r"Ref\(\s*\$[A-Za-z_]\w*\s*,\s*-(\d+)\s*\)")


def fmt_stat(value: Any, fmt: str = ".6f") -> str:
    """渲染**可能缺失**的统计量：缺失（``None`` / ``NaN`` / ``inf``）一律输出「—」。

    🔴 铁律六（禁编数字）：算不出来的统计量**不许编、不许填 0**。
    🔴 崩溃根因（2026-11-05 实测）：只打 **1 个评分日**时 IC 系列统计量是 ``None``
       （打分日之后没有未来收益 ⇒ 标签算不出来 ⇒ IC 算不出来），
       旧代码直接 `f"{ic['ic']['mean']:.6f}"` ⇒ ``TypeError: unsupported format string
       passed to NoneType.__format__`` ⇒ ``--mode infer`` **非零退出**
       ⇒ 上游 ``daily/agents_rebalance.py`` 阶段 2 被判失败 ⇒ 六阶段流水线中断。
    ⇒ 统一走本助手：非缺失值按 ``fmt`` 正常格式化，缺失值输出「—」
       （``NaN`` / ``inf`` 同按缺失处理 —— 它们不是可解读的统计量）。
    """
    if value is None:
        return MISSING_STAT
    if isinstance(value, (int, float, np.integer, np.floating)) and not np.isfinite(value):
        return MISSING_STAT
    return format(value, fmt)


def label_horizon_days() -> int:
    """标签表达式向前看的交易日数（``Ref($vwap, -2)/Ref($vwap, -1)-1`` ⇒ 2 个交易日）。

    从**唯一事实源** ``config.STOCK.LABEL_EXPRESSIONS`` 解析，不写死常量。
    """
    horizons = [
        int(m)
        for expr in C.STOCK.LABEL_EXPRESSIONS
        for m in _LABEL_REF_RE.findall(expr)
    ]
    return max(horizons) if horizons else 0


def ic_stat_reason(ic: dict[str, Any], dates: list[pd.Timestamp], dataset_max: str) -> str:
    """IC 统计量缺失时的**原因说明**（铁律六：缺失必须说清为什么，不许静默填 0）。

    返回空串 = 全部统计量都可算，无需说明。
    """
    per_day = ic.get("per_day") or []
    n_valid = sum(1 for d in per_day if d.get("ic") is not None)
    missing: list[str] = []
    for key, label in (("ic", "IC"), ("rank_ic", "rankIC")):
        node = ic.get(key) or {}
        if node.get("mean") is None:
            missing.append(f"{label} mean")
        else:
            if node.get("icir") is None:
                missing.append(f"{label} ICIR")
            if node.get("t") is None:
                missing.append(f"{label} t")
    if not missing:
        return ""
    ctx: list[str] = []
    if len(dates) == 1:
        ctx.append(f"仅 1 个评分日（{dates[0].date()}）")
    if per_day and n_valid == 0:
        ctx.append(
            f"标签窗口（需打分日之后 {label_horizon_days()} 个交易日）"
            f"超出 qlib 数据集覆盖末日 {dataset_max} ⇒ 前瞻收益全为 NaN ⇒ 有效 IC 日 n=0"
        )
    elif n_valid == 1:
        ctx.append("有效 IC 日仅 1 个 ⇒ ICIR/t 不可计算（需 ≥2 日且方差 > 0）")
    head = ("；".join(ctx) + " ⇒ ") if ctx else ""
    return f"{head}以下统计量不可计算 ⇒ 显示「{MISSING_STAT}」：{'、'.join(missing)}"


def format_ic_summary(ic: dict[str, Any]) -> str:
    """收尾摘要里的 IC 一行（**缺失统计量渲染「—」**，不许编、不许填 0）。"""
    return (
        f"== IC: pearson mean={fmt_stat(ic['ic']['mean'])} "
        f"ICIR={fmt_stat(ic['ic']['icir'], '.4f')} t={fmt_stat(ic['ic']['t'], '.3f')} "
        f"| rankIC mean={fmt_stat(ic['rank_ic']['mean'])} "
        f"ICIR={fmt_stat(ic['rank_ic']['icir'], '.4f')} "
        # 🔴 「覆盖」= **有有效标签**的股票数（dropna 之后），不是打分了多少只
        #   （单日末位场景下打分了 5575 只，但标签全 NaN ⇒ 这里是 0 ⇒ 别读成「一只没打」）
        f"| 有效标签覆盖中位 {fmt_stat(ic['coverage']['median_n'], '.0f')} 只 × {ic['coverage']['n_dates']} 日"
    )


def main() -> int:
    global _PROVIDER_URI_OVERRIDE
    args = parse_args()
    t0 = time.time()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 🔴 provider_uri 覆盖（默认仍是上游基座 ⇒ **默认路径零回归**）
    if args.provider_uri:
        _PROVIDER_URI_OVERRIDE = str(Path(args.provider_uri).expanduser().resolve())
        print(f"[INFO] provider_uri 覆盖为：{_PROVIDER_URI_OVERRIDE}")
    print(f"[INFO] provider_uri = {provider_uri()}")

    init_qlib()

    start = args.start or str(C.STOCK.TEST[0])
    requested_end = args.end or str(C.STOCK.TEST[1])
    # 🔴 M-S2（2026-11-05 06:2x 实测）：**--end 原来被 `C.SCORE_END_TIME` 静默截断**
    #   （日历查询窗写死 `trading_calendar(SCORE_START_TIME, SCORE_END_TIME)`）
    #   ⇒ `--end 2026-12-01` 只会打到 2026-11-03，且**全程无任何提示**
    #   ⇒ 用户以为「已经重新打过分了」，其实还是旧分数（一小时算力白花）。
    #   ⇒ 修法：查询窗取 `max(SCORE_END_TIME, --end)`；数据集覆盖不到时**显式告警**。
    cal_end = max(C.SCORE_END_TIME, requested_end)
    cal = trading_calendar(C.SCORE_START_TIME, cal_end)
    dates = rebalance_dates(cal, args.rebalance_every, start, requested_end)
    print(f"[INFO] 交易日历 {cal[0].date()} ~ {cal[-1].date()}，共 {len(cal)} 天")
    print(f"[INFO] 调仓（打分）日 {len(dates)} 个：{dates[0].date()} ~ {dates[-1].date()}")
    data_max = str(cal[-1].date())
    # 🔴 M-S4 审计订正（2026-11-05 08:0x）：`data_max` 取的是**日历查询窗**的末日
    #   （= min(数据集真实末日, requested_end)），**不是**数据集覆盖末日
    #   ⇒ 把它当 `qlib_dataset_max_date` 写进 manifest 是**错的**：
    #   数据集重建到 2026-12-31 后跑 `--end 2026-12-01`，manifest 会写
    #   「数据集只到 2026-12-01」⇒ 用户据此判断「要不要重建数据集」会被误导。
    #   ⇒ 数据集真实覆盖末日**直读 `calendars/day.txt`**（唯一权威来源）。
    dataset_max = SP.calendar_max_date(
        Path(provider_uri()) / "calendars" / "day.txt"
    ) or data_max
    end_capped = bool(requested_end > dataset_max)
    if end_capped:
        print(
            f"[WARN] 🔴 请求的打分结束日 **{requested_end}** 超出 qlib 数据集覆盖末日 "
            f"**{dataset_max}** ⇒ 实际只打到 **{data_max}**（本次打分**不含** {requested_end}）\n"
            f"       处置：想打分到 {requested_end}，须先重建 / 更新 qlib 数据集"
            f"（**重活，需用户当轮授权**）；否则请把 --end 改成 ≤ {data_max} 的日期。"
        )
    # handler 的数据窗跟着「数据集实际覆盖末日」走（否则数据集更新后仍打不出分）
    handler_end = data_max

    # ---- handler（与个股线同构，只改股票池；归一化统计量**冻结**自训练期）----
    if args.mode == "train":
        handler = build_train_handler(args.universe)
        print(f"[INFO] mode=train：全市场自洽重训 handler（universe={args.universe}）")
    else:
        print("[INFO] 构建训练期 handler（复现个股线归一化统计量）...")
        fit_handler = build_fit_handler()
        probe = [p for p in fit_handler.get_all_processors() if hasattr(p, "mean_train")]
        if not probe or getattr(probe[0], "mean_train", None) is None:
            fit_handler.fit()
        frozen = extract_frozen_norm(fit_handler)
        print(f"[INFO] 训练期 ZScoreNorm 统计量已冻结（{len(frozen.cols)} 个特征列，"
              f"fit 窗 {frozen.fit_start_time}~{frozen.fit_end_time}）")
        handler = build_score_handler(args.universe, frozen, end_time=handler_end)
        print(f"[INFO] 打分期 handler 构建完成（universe={args.universe}，"
              f"数据窗末日={handler_end}）")

    from qlib.data.dataset import DatasetH

    dataset = DatasetH(
        handler=handler,
        segments={
            "train": C.STOCK.TRAIN,
            "valid": C.STOCK.VALID,
            "test": C.STOCK.TEST,
        },
    )

    model, meta = load_or_train_model(
        args.mode, Path(args.model_dir), dataset, {"universe": args.universe}
    )
    print(f"[INFO] 模型来源: {meta['source']}")

    # ---- 逐调仓日打分 ----
    frames: list[pd.DataFrame] = []
    for i, d in enumerate(dates, 1):
        pred = model.predict(dataset, segment=slice(d, d))
        if pred is None or len(pred) == 0:
            print(f"[WARN] {d.date()} 无预测")
            continue
        # model.predict 返回 MultiIndex(datetime, instrument) 的 Series → reset_index 后三列
        pred = pred.reset_index()
        pred = pred.rename(columns={pred.columns[0]: "date", pred.columns[1]: "instrument",
                                    pred.columns[2]: "score"})
        assert_non_degenerate(pred, d)
        frames.append(pred[["date", "instrument", "score"]])
        print(f"[{i}/{len(dates)}] {d.date()}  打分 {len(pred)} 只  score[min,max]="
              f"[{fmt_stat(pred['score'].min(), '.4f')},{fmt_stat(pred['score'].max(), '.4f')}]")

    scores = pd.concat(frames, ignore_index=True)

    # ---- 前瞻收益（IC 用）----
    fwd = fetch_fwd_returns(dates, args.universe)
    scores = scores.merge(fwd.reset_index(), on=["date", "instrument"], how="left")
    scores = scores.sort_values(["date", "instrument"]).reset_index(drop=True)

    # ---- 落盘 ----
    scores_csv = out_dir / "stock_scores.csv"
    scores.to_csv(scores_csv, index=False, encoding="utf-8-sig")
    print(f"[OK] 已写出 {scores_csv}  ({len(scores)} 行)")

    validation = validate_against_stock_line(scores, Path(args.validate_pred))
    if validation.get("available"):
        print(
            f"[CHECK] 与个股线 pred.pkl 交叉校验：n={validation['n_overlap']} "
            f"({validation['n_dates']} 日) spearman={fmt_stat(validation['spearman'])} "
            f"max|Δ|={fmt_stat(validation['max_abs_diff'], '.3e')}"
        )
    else:
        print(f"[WARN] 交叉校验不可用：{validation.get('reason')}")

    ic = ic_stats(scores)
    ic_reason = ic_stat_reason(ic, dates, dataset_max)
    manifest = {
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "host": platform.node(),
        "python": platform.python_version(),
        "mode": args.mode,
        "model_meta": meta,
        "universe": args.universe,
        "rebalance_every_n_days": args.rebalance_every,
        "n_score_dates": len(dates),
        "score_date_range": [str(dates[0].date()), str(dates[-1].date())],
        "requested_end": requested_end,
        "qlib_dataset_max_date": dataset_max,
        "score_window_max_date": data_max,
        "end_capped_by_dataset": end_capped,
        "stock_modeling_params_source": (
            "manu/experiments/ashare_topk_selection/config.py（复用，未在本文件重复定义）"
        ),
        "label_expression": C.STOCK.LABEL_EXPRESSIONS,
        "fit_window": [C.STOCK.FIT_START, C.STOCK.FIT_END],
        "provider_uri": provider_uri(),
        "region": C.REGION,
        "ic": ic,
        "ic_missing_reason": ic_reason or None,
        "validation_vs_stock_line_pred": validation,
        "outputs": {"stock_scores_csv": str(scores_csv)},
        "script_md5": md5_of(Path(__file__).resolve()),
    }
    mpath = out_dir / "stock_scores_manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[OK] 已写出 {mpath}")
    print(format_ic_summary(ic))
    if ic_reason:
        print(f"== IC 说明：{ic_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
