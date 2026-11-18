#!/usr/bin/env python
"""🔴 V2 S 层：**训练 + 打分**（对齐①：标签 = 未来 20 交易日超额收益）。

**V2 独立分支**：本文件**不 import、不改写** V1 的 ``qlib_scores.py`` /
``config.py`` / ``engine/*``（V1 冻结为 baseline，见 ``v2/__init__.py``）。

与 V1 的**唯一实质差异**（其余逐项同构，保证对比可比）：

  ==========  ==========================================  ==========================
  维度        V1                                         V2（本文件）
  ==========  ==========================================  ==========================
  标签        ``Ref($vwap,-2)/Ref($vwap,-1)-1``（**1 日**）  ``Ref(Mean($vwap,11),-25)/$vwap-1``
                                                          （**t+15~t+25 窗口均价**，20 日视角）
  训练末段    2021-12-31（**与 val 首日标签重叠** ⇒ 泄露）   2021-11-26（**embargo=25** 交易日）
  特征        Alpha158 全量                                 Alpha158（**可按名消融**，如 ROC5）
  归一化      训练期拟合 → 推理期冻结（FrozenZScoreNorm）    同（V2 本地复刻，不 import V1）
  训练池      csi300                                        csi300（同）
  打分池      all                                          all（同）
  LightGBM   V1 个股线 val 段选定的超参                     **照抄**（V2 未重搜，报告如实披露）
  ==========  ==========================================  ==========================

🔴 **单盲纪律**：本脚本只负责「训练 + 打分」，**不做任何选参**；
   选参只在 val 段（由 ``run_v2.py`` 调 ``hyperopt.search``）做。

用法（服务器）：
    export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMEXPR_NUM_THREADS=4
    python manu/etf_rotation/v2/train_s.py --stage all \\
        --out-dir /mnt/data/siping/runs/etf_rotation_results/v2
"""
from __future__ import annotations

import argparse
import json
import pickle
import platform
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import features as FEAT  # noqa: E402
import splits as SP  # noqa: E402

__all__ = [
    "train_s_layer",
    "score_universe",
    "alpha158_handler_class",
    "frozen_norm_class",
]


# ----------------------------------------------------------------- 工具
def md5_of(path: Path) -> str:
    """算文件 md5（产物指纹用；V1 同款口径）。"""
    import hashlib

    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_args() -> argparse.Namespace:
    """解析命令行参数（V2 S 层的全部开关）。"""
    p = argparse.ArgumentParser(description="V2 S 层：训练 + 打分")
    p.add_argument("--out-dir", default=str(C.DEFAULT_V2_RUN_ROOT))
    p.add_argument("--provider-uri", default=C.DEFAULT_PROVIDER_URI)
    p.add_argument("--stage", choices=["train", "score", "all"], default="all")
    p.add_argument("--ablation", choices=sorted(C.ABLATION_GROUPS), default=C.ABLATION_WINNER,
                   help="特征消融组（🔴 默认 = val 段判定的胜者 `ablate_roc5`；"
                        "`baseline_full` 降为对照臂）")
    p.add_argument("--train-universe", default=C.TRAIN_UNIVERSE)
    p.add_argument("--score-universe", default=C.SCORE_UNIVERSE)
    p.add_argument("--rebalance-every", type=int, default=C.SIGNAL_STRIDE_N_DAYS,
                   help="🔴 **S 层打分栅格步长**（信号日基础步长，V1 同口径 = 10 个交易日）"
                        "｜注意：**调仓节奏 cadence**（`rebalance_every_n_days`）是 ETF 层的"
                        "独立维度，2026-11-17 夜班起**已进 val 段搜索空间**"
                        "（调仓栅格步长 = cadence // 本值）")
    p.add_argument("--score-start", default=C.VALID[0], help="打分窗口起点（含）")
    p.add_argument("--score-end", default=C.TEST[1], help="打分窗口终点（含）")
    p.add_argument("--chunk-months", type=int, default=12,
                   help="打分分块月数（限制 qlib 内存峰值；分块结果与整块**逐位相同**）")
    p.add_argument("--model-name", default=None, help="模型文件名（默认按消融组自动命名）")
    p.add_argument("--label-variant", default="window_avg_price",
                   choices=["window_avg_price"], help="标签口径（当前只实现了窗口均价）")
    return p.parse_args()


def init_qlib(provider_uri: str) -> None:
    """初始化 qlib（🔴 必须先 init，否则 handler 构造直接报错）。"""
    import qlib
    from qlib.constant import REG_CN

    qlib.init(provider_uri=str(provider_uri), region=REG_CN)


# ----------------------------------------------------------------- handler
def alpha158_handler_class(drop: tuple[str, ...] = ()):
    """返回**按名消融**的 Alpha158 handler 子类。

    🔴 qlib 的 ``Alpha158`` handler 内部用 ``self.get_feature_config()`` 取特征清单
       ⇒ 子类覆写它即可**精确剔名**（不影响其它同窗口特征）。
    """
    from qlib.contrib.data.handler import Alpha158

    drop_set = {str(x) for x in (drop or ())}

    class Alpha158V2(Alpha158):
        """V2 版 Alpha158：支持按特征名精确剔除（对齐① 的 ROC5 消融）。"""

        def get_feature_config(self):
            """按名剔除特征（对齐①的 ROC5 消融锚点）。"""
            fields, names = super().get_feature_config()
            keep = [(f, n) for f, n in zip(fields, names) if str(n) not in drop_set]
            return [f for f, _ in keep], [n for _, n in keep]

    return Alpha158V2


def frozen_norm_class():
    """**训练期拟合 → 推理期冻结**的 ZScoreNorm（V2 本地复刻，不 import V1）。

    🔴 为什么必须有：qlib ``ZScoreNorm.fit()`` 用 ``fetch_df_by_index(df, slice(fit_start,
       fit_end))`` 取统计量；当 handler 的数据窗**不覆盖** fit 窗时，切片为空
       ⇒ ``np.nanmean`` 静默返回 NaN ⇒ 特征全 NaN ⇒ ``Fillna(0)`` 之后
       **全市场分数塌成同一个常数**（表面跑通、实际零信息）。
    """
    from qlib.data.dataset.processor import ZScoreNorm

    class FrozenZScoreNorm(ZScoreNorm):
        """复用训练期统计量的 ZScoreNorm（推理期不再拟合）。"""

        def fit(self, df: pd.DataFrame = None):
            """冻结归一化统计量 ⇒ 推理期**不再拟合**（避免 fit 窗落在数据窗外 ⇒ 全 NaN）。"""
            if getattr(self, "mean_train", None) is None or getattr(self, "std_train", None) is None:
                raise ValueError("FrozenZScoreNorm 尚未注入训练期统计量")
            return self

    return FrozenZScoreNorm


def _infer_processors(frozen=None) -> list[dict]:
    """推理期处理器链：``ProcessInf`` → 归一化 → ``Fillna(0)``。"""
    base = [
        {"class": "ProcessInf", "kwargs": {}},
        {"class": "ZScoreNorm", "kwargs": {"fields_group": "feature"}},
        {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
    ]
    if frozen is None:
        return base
    return [
        {"class": "ProcessInf", "kwargs": {}},
        frozen,
        {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
    ]


def _learn_processors() -> list[dict]:
    """学习期处理器链：``DropnaLabel`` → ``CSRankNorm``（与 V1 同口径）。"""
    return [
        {"class": "DropnaLabel", "kwargs": {}},
        {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}},
    ]


def build_train_handler(universe: str, drop: tuple[str, ...], fit_end: str):
    """训练期 handler：股票池 = universe，**fit 窗 = 裁剪后的训练段**。

    🔴 `fit_end` 必须是 **embargo 裁剪后**的训练末日（不是 2021-12-31）
       ⇒ 归一化统计量**不包含**任何 embargo 之后的样本。
    """
    handler_cls = alpha158_handler_class(drop)
    return handler_cls(
        start_time=C.START_TIME,
        end_time=fit_end,
        fit_start_time=C.FIT_START,
        fit_end_time=fit_end,
        instruments=universe,
        label=([C.LABEL_EXPRESSION], ["LABEL0"]),
        infer_processors=_infer_processors(),
        learn_processors=_learn_processors(),
    )


def build_score_handler(universe: str, drop: tuple[str, ...], frozen,
                        start_time: str, end_time: str):
    """打分期 handler：股票池 = 打分池，**归一化统计量冻结自训练期**。"""
    handler_cls = alpha158_handler_class(drop)
    return handler_cls(
        start_time=start_time,
        end_time=end_time,
        fit_start_time=C.FIT_START,
        fit_end_time=frozen.fit_end_time,
        instruments=universe,
        label=([C.LABEL_EXPRESSION], ["LABEL0"]),
        infer_processors=_infer_processors(frozen),
        learn_processors=[],
    )


def extract_frozen_norm(fit_handler):
    """从训练期 handler 里取出拟合好的 ZScoreNorm，冻结成推理期可用的实例。"""
    from qlib.data.dataset.processor import ZScoreNorm

    frozen_cls = frozen_norm_class()
    zs = None
    for proc in fit_handler.get_all_processors():
        if isinstance(proc, ZScoreNorm) and not isinstance(proc, frozen_cls):
            zs = proc
    if zs is None:
        raise RuntimeError("训练期 handler 里找不到 ZScoreNorm")
    if not np.isfinite(np.asarray(zs.mean_train)).all() or not np.isfinite(np.asarray(zs.std_train)).all():
        raise RuntimeError("训练期 ZScoreNorm 统计量含 NaN/Inf ⇒ fit 窗无效，拒绝继续")
    frozen = frozen_cls(zs.fit_start_time, zs.fit_end_time, fields_group=zs.fields_group)
    frozen.mean_train = np.array(zs.mean_train, copy=True)
    frozen.std_train = np.array(zs.std_train, copy=True)
    frozen.ignore = np.array(zs.ignore, copy=True)
    frozen.cols = list(zs.cols)
    return frozen


# ----------------------------------------------------------------- 训练
def train_s_layer(
    universe: str,
    drop: tuple[str, ...],
    out_dir: Path,
    model_name: str,
    num_boost_round: int | None = None,
    spec: Any | None = None,
) -> dict:
    """在**裁剪后**的训练段上重训 S 层（标签 = V2 标签）。

    🔴 **2026-11-18 新增 ``spec`` 参数**（5 折滚动前推用）：
       * ``spec=None`` ⇒ 用默认切分 :func:`splits.split`（2016~2021 训练 / 2022~2023 验证）
         ⇒ **既有调用方行为逐字不变**；
       * ``spec=FoldSpec`` ⇒ 用该折的训练段（2016~2018 / …… / 2016~2022），
         并调 :func:`splits.assert_fold_discipline` 做折级红线断言。

    返回 manifest（写盘在调用方）。
    """
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH

    if spec is None:
        spec = SP.split()
        SP.assert_split_discipline(spec)
    elif getattr(spec, "fold_id", None) is not None:
        # 🔴 滚动前推的折 ⇒ 折级红线（顺序 + embargo + 绝不触碰 2024+）
        SP.assert_fold_discipline(spec)
    else:
        SP.assert_split_discipline(spec)
    print(f"[INFO] 训练段：{spec.train_start} ~ {spec.train_end}"
          f"（名义末日 {spec.train_end_raw}，embargo={spec.embargo_days} 交易日）"
          + (f"｜折号 fold_id={spec.fold_id}" if getattr(spec, "fold_id", None) else ""))
    print(f"[INFO] fit 窗：{spec.fit_start} ~ {spec.fit_end}")
    print(f"[INFO] 标签：{C.LABEL_EXPRESSION}")
    print(f"[INFO] 消融组：{C.ABLATION_GROUPS.get(model_name, ())}")
    print(f"[INFO] 特征数：{len(FEAT.alpha158_feature_config(drop))}")

    handler = build_train_handler(universe, drop, spec.train_end)
    print("[INFO] 训练期 handler 构建中（含 ZScoreNorm 拟合）...")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        handler.fit()
    frozen = extract_frozen_norm(handler)
    print(f"[INFO] 训练期 ZScoreNorm 已冻结：{len(frozen.cols)} 个特征列，"
          f"fit 窗 {frozen.fit_start_time} ~ {frozen.fit_end_time}")

    dataset = DatasetH(
        handler=handler,
        segments={
            "train": (spec.train_start, spec.train_end),
            # 🔴 valid 段**故意**指向训练段：qlib 的 LGBModel.fit 会把 [train, valid]
            #    一起塞进 LightGBM 的 valid_sets；早停已关（EARLY_STOPPING_ROUNDS=None）
            #    ⇒ 不影响模型，但**不给 val 段任何机会**触碰模型
            #    （val 段只许用于 ETF 层的超参搜索，绝不许进 S 层）。
            "valid": (spec.train_start, spec.train_end),
            "test": (spec.valid_start, spec.test_end),
        },
    )

    rounds = int(C.NUM_BOOST_ROUND if num_boost_round is None else num_boost_round)
    model = LGBModel(
        loss=C.LGB_PARAMS["loss"],
        colsample_bytree=C.LGB_PARAMS["colsample_bytree"],
        learning_rate=C.LGB_PARAMS["learning_rate"],
        subsample=C.LGB_PARAMS["subsample"],
        bagging_freq=C.LGB_PARAMS["bagging_freq"],
        lambda_l1=C.LGB_PARAMS["lambda_l1"],
        lambda_l2=C.LGB_PARAMS["lambda_l2"],
        max_depth=C.LGB_PARAMS["max_depth"],
        num_leaves=C.LGB_PARAMS["num_leaves"],
        num_threads=C.LGB_PARAMS["num_threads"],
        random_state=C.LGB_PARAMS["random_state"],
        deterministic=C.LGB_PARAMS["deterministic"],
        force_row_wise=C.LGB_PARAMS["force_row_wise"],
        verbosity=C.LGB_PARAMS["verbosity"],
    )
    t0 = time.time()
    model.fit(dataset, num_boost_round=rounds)
    elapsed = time.time() - t0
    print(f"[INFO] 训练完成：{elapsed:.1f}s，rounds={rounds}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / f"model_{model_name}.pkl"
    with model_path.open("wb") as fh:
        pickle.dump(model, fh)
    frozen_path = out_dir / f"frozen_norm_{model_name}.pkl"
    with frozen_path.open("wb") as fh:
        pickle.dump(
            {
                "mean_train": frozen.mean_train,
                "std_train": frozen.std_train,
                "ignore": frozen.ignore,
                "cols": frozen.cols,
                "fit_start_time": frozen.fit_start_time,
                "fit_end_time": frozen.fit_end_time,
                "fields_group": frozen.fields_group,
            },
            fh,
        )
    print(f"[OK] 模型已写出：{model_path}")

    manifest = {
        "model_name": model_name,
        "trained_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "host": platform.node(),
        "python": platform.python_version(),
        "train_universe": universe,
        "score_universe": None,
        "label_expression": C.LABEL_EXPRESSION,
        "label_exit_window": list(C.LABEL_EXIT_WINDOW),
        "label_horizon_days": C.LABEL_HORIZON_DAYS,
        "benchmark": C.BENCHMARK,
        "dropped_features": list(drop),
        "ablation_report": FEAT.ablation_report(drop),
        "num_features": len(FEAT.alpha158_feature_config(drop)),
        "train_segment": [spec.train_start, spec.train_end],
        "train_end_raw": spec.train_end_raw,
        "embargo_days": spec.embargo_days,
        "fit_window": [spec.fit_start, spec.fit_end],
        "num_boost_round": rounds,
        "early_stopping_rounds": C.EARLY_STOPPING_ROUNDS,
        "lgb_params": dict(C.LGB_PARAMS),
        "model_path": str(model_path),
        "model_md5": md5_of(model_path),
        "frozen_norm_path": str(frozen_path),
        "hyperparam_note": (
            "🔴 V2 **未**在 val 段重搜 S 层超参：这里**照抄 V1 个股线 2026-11-04 "
            "在 val 段选定的值**（lr=0.02 / rounds=500）。如实披露，不装作 V2 自己搜过。"
        ),
        "segments": {"train": [spec.train_start, spec.train_end],
                     "valid": [spec.valid_start, spec.valid_end],
                     "test": [spec.test_start, spec.test_end]},
        # 🔴 滚动前推：折号 + 折级切分规格（默认切分时 fold_id = None）
        "fold_id": getattr(spec, "fold_id", None),
        "split_spec": spec.as_dict() if hasattr(spec, "as_dict") else None,
    }
    manifest_path = out_dir / f"train_manifest_{model_name}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    print(f"[OK] 训练 manifest 已写出：{manifest_path}")
    return manifest


# ----------------------------------------------------------------- 打分
def _assert_non_degenerate(pred: pd.DataFrame, day: Any) -> None:
    """打分日守卫：分数塌成常数 ⇒ 直接抛错（宁可不产出，也绝不产出假分数）。"""
    vals = pred["score"].to_numpy(dtype=float)
    finite = vals[np.isfinite(vals)]
    n_unique = int(np.unique(finite).size)
    std = float(finite.std()) if finite.size else 0.0
    if finite.size == 0:
        raise RuntimeError(f"{day} 打分全为空")
    if std <= 0.0 or n_unique < 10:
        raise RuntimeError(
            f"{day} 打分退化：n={finite.size}, std={std:.3e}, 唯一值={n_unique} "
            "⇒ 特征/模型输出异常（大概率是归一化统计量失效），拒绝写出"
        )


def fetch_forward_returns(dates: list[pd.Timestamp], universe: str) -> pd.DataFrame:
    """取**V2 标签口径**的前瞻收益（列 date, instrument, fwd_ret）—— 用于 IC 评估。

    🔴 表达式与训练标签**逐字一致**（``Ref(Mean($vwap,11),-25)/$vwap-1``）
       ⇒ IC 评估的 y 与训练目标**同一个量纲**（不是 V1 的 1 日收益）。
    """
    from qlib.data import D

    insts = list(D.list_instruments({"market": universe, "filter_pipe": []}, as_list=True))
    if not insts:
        raise RuntimeError(f"qlib 股票池 {universe!r} 为空")
    df = D.features(
        insts,
        [C.LABEL_EXPRESSION],
        start_time=str(dates[0].date()),
        end_time=str(dates[-1].date()),
        freq="day",
    )
    df = df.reset_index().rename(columns={"datetime": "date"})
    df = df.rename(columns={c: "fwd_ret" for c in df.columns if c not in ("date", "instrument")})
    df["date"] = pd.to_datetime(df["date"])
    df["instrument"] = df["instrument"].astype(str)
    return df[["date", "instrument", "fwd_ret"]]


def _rebalance_dates(cal: pd.DatetimeIndex, every_n: int,
                     start: str, end: str) -> list[pd.Timestamp]:
    """按 ``every_n`` 个交易日取一个调仓日（V1 同款栅格口径）。"""
    grid = cal[:: max(1, int(every_n))]
    out = [d for d in grid if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    return out


def score_universe(
    model_path: Path,
    frozen_path: Path,
    drop: tuple[str, ...],
    universe: str,
    out_dir: Path,
    score_start: str,
    score_end: str,
    rebalance_every: int,
    chunk_months: int = 12,
    model_name: str = "baseline_full",
    with_fwd: bool = True,
) -> dict:
    """在 ``all`` 池上按栅格打分（**分块**执行以限制 qlib 内存峰值）。

    🔴 分块安全性：Alpha158 的特征**全是回看型**（最长 60 日），归一化统计量
       **冻结**自训练期 ⇒ 每个分块的特征与整块**逐位相同** ⇒ 分块结果可拼接。
    """
    from qlib.data import D
    from qlib.data.dataset import DatasetH

    with model_path.open("rb") as fh:
        model = pickle.load(fh)
    frozen_blob = pickle.loads(frozen_path.read_bytes())
    frozen_cls = frozen_norm_class()
    frozen = frozen_cls(frozen_blob["fit_start_time"], frozen_blob["fit_end_time"],
                        fields_group=frozen_blob["fields_group"])
    frozen.mean_train = frozen_blob["mean_train"]
    frozen.std_train = frozen_blob["std_train"]
    frozen.ignore = frozen_blob["ignore"]
    frozen.cols = frozen_blob["cols"]

    cal = D.calendar(start_time=C.START_TIME, end_time=score_end, freq="day")
    dates = _rebalance_dates(cal, rebalance_every, score_start, score_end)
    if not dates:
        raise RuntimeError(f"打分窗口 {score_start}~{score_end} 内没有交易日")
    print(f"[INFO] 打分日 {len(dates)} 个：{dates[0].date()} ~ {dates[-1].date()}")

    frames: list[pd.DataFrame] = []
    chunk = max(1, int(chunk_months))
    groups: list[list[pd.Timestamp]] = []
    cur: list[pd.Timestamp] = []
    for d in dates:
        if cur and (d.year * 12 + d.month) - (cur[0].year * 12 + cur[0].month) >= chunk:
            groups.append(cur)
            cur = []
        cur.append(d)
    if cur:
        groups.append(cur)
    print(f"[INFO] 分 {len(groups)} 块打分（每块 ≤ {chunk} 个月）")

    for gi, group in enumerate(groups, 1):
        warmup = (group[0] - pd.Timedelta(days=C.SCORE_WARMUP_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        handler = build_score_handler(universe, drop, frozen, warmup,
                                      group[-1].strftime("%Y-%m-%d"))
        dataset = DatasetH(handler=handler, segments={"test": (warmup, group[-1].strftime("%Y-%m-%d"))})
        for d in group:
            pred = model.predict(dataset, segment=slice(d, d))
            if pred is None or len(pred) == 0:
                print(f"[WARN] {d.date()} 无预测")
                continue
            pred = pred.reset_index()
            pred = pred.rename(columns={pred.columns[0]: "date", pred.columns[1]: "instrument",
                                        pred.columns[2]: "score"})
            _assert_non_degenerate(pred, d)
            frames.append(pred[["date", "instrument", "score"]])
            print(f"[{gi}/{len(groups)}] {d.date()} 打分 {len(pred)} 只 "
                  f"score[min,max]=[{pred['score'].min():.4f},{pred['score'].max():.4f}]")
        del handler, dataset
        frames_gc = pd.concat(frames, ignore_index=True) if frames else None
        if frames_gc is not None:
            frames = [frames_gc]

    scores = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    scores["date"] = pd.to_datetime(scores["date"])
    scores = scores.sort_values(["date", "instrument"]).reset_index(drop=True)

    # ---- 前瞻收益（V2 标签口径，用于 IC / RankIC）----
    if with_fwd:
        print("[INFO] 拉取前瞻收益（V2 标签口径）...")
        try:
            fwd = fetch_forward_returns(dates, universe)
            scores = scores.merge(fwd, on=["date", "instrument"], how="left")
            n_ok = int(scores["fwd_ret"].notna().sum())
            print(f"[INFO] 前瞻收益合并完成：{n_ok}/{len(scores)} 行有效")
        except Exception as exc:  # noqa: BLE001 —— IC 算不出来**如实记录**，不许编
            print(f"[WARN] 前瞻收益拉取失败（IC 将缺失）：{exc!r}")
            scores["fwd_ret"] = np.nan

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"stock_scores_v2_{model_name}.csv"
    scores.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[OK] 分数已写出：{csv_path}（{len(scores)} 行，{scores['date'].nunique()} 个交易日）")

    manifest = {
        "model_name": model_name,
        "scored_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "score_universe": universe,
        "n_score_dates": int(scores["date"].nunique()),
        "score_date_range": [str(scores["date"].min().date()), str(scores["date"].max().date())],
        "rebalance_every_n_days": int(rebalance_every),
        "chunk_months": int(chunk),
        "n_rows": int(len(scores)),
        "model_path": str(model_path),
        "model_md5": md5_of(model_path),
        "frozen_norm_path": str(frozen_path),
        "dropped_features": list(drop),
        "outputs": {"stock_scores_csv": str(csv_path)},
        "script_md5": md5_of(Path(__file__).resolve()),
    }
    mpath = out_dir / f"score_manifest_{model_name}.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[OK] 打分 manifest 已写出：{mpath}")
    return manifest


# ----------------------------------------------------------------- main
def main() -> int:
    """S 层脚本入口（``--stage train|score|all``）。"""
    args = parse_args()
    t0 = time.time()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] provider_uri = {args.provider_uri}")
    init_qlib(args.provider_uri)

    drop = tuple(C.ABLATION_GROUPS[args.ablation])
    model_name = args.model_name or args.ablation
    model_path = out_dir / f"model_{model_name}.pkl"
    frozen_path = out_dir / f"frozen_norm_{model_name}.pkl"

    if args.stage in ("train", "all"):
        train_s_layer(args.train_universe, drop, out_dir, model_name)
    if args.stage in ("score", "all"):
        if not model_path.is_file():
            raise FileNotFoundError(f"找不到模型 {model_path}；请先 --stage train")
        score_universe(model_path, frozen_path, drop, args.score_universe, out_dir,
                       args.score_start, args.score_end, args.rebalance_every,
                       chunk_months=args.chunk_months, model_name=model_name)
    print(f"[INFO] 全流程结束，用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
