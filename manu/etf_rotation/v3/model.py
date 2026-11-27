"""🔴 V3 模型层：LightGBM **lambdarank**（实验臂）＋ **MSE**（对照臂）。

**为什么是排序损失（用户 2026-11-26 目标文档第 ④ 条）**
下游 ``E = W × S`` 选的是**头部 ETF** ⇒ 对绝对收益率数值的回归精度（MSE）不敏感，
只关心**相对排序** ⇒ 训练目标改用 **lambdarank**（成对排序损失）：
组 = 交易日，label = 整数 relevance（由「数值超额」分档得到）⇒ 损失直接对齐「谁排前面」。

**本文件的边界（刻意做窄）**
* **纯 lightgbm 调用**：不依赖 qlib（训练矩阵由 ``train_s.py`` 用 pandas 组装好再喂进来）；
* **不 import V2**：V3 硬隔离（V2 是现役口径，只读参考不许 import）；
* 🔴 **绝不用 early_stopping**：qlib 的 ``LGBModel.fit`` 会把 [train, valid] 一起塞进
  ``valid_sets`` ⇒ 早停会引入**隐性泄漏**（config_v3.EARLY_STOPPING_ROUNDS = None）；
  ⇒ 本模块**一律不传** ``valid_sets`` / ``callbacks``。

**label_gain 的口径**：LightGBM 默认 ``label_gain = [0, 1, 3, 7, 15, ...]``（2 的幂），
V3 统一用**线性档位** ``[0, 1, 2, 3, 4]``（长度 = ``config_v3.RANK_GRADE_COUNT`` = 5）
⇒ 分档之间的「档差」在损失里是等距的，与「5 等分位」的语义一致。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_V3_DIR = Path(__file__).resolve().parent
_ADDED_PATH = str(_V3_DIR) not in sys.path
if _ADDED_PATH:
    sys.path.insert(0, str(_V3_DIR))
try:
    import config_v3 as C
except ModuleNotFoundError:  # pragma: no cover —— 仅当以包方式被 import 时走这条
    from . import config_v3 as C
finally:
    if _ADDED_PATH:
        try:
            sys.path.remove(str(_V3_DIR))
        except ValueError:  # pragma: no cover
            pass

__all__ = [
    "build_rank_dataset",
    "default_label_gain",
    "predict_scores",
    "train_ranker",
    "train_regressor",
]


# ----------------------------------------------------------------- 工具
def _import_lightgbm():
    """按需 import lightgbm（🔴 本机/部分环境没装 ⇒ 报错信息要说人话）。

    🔴 **为什么延迟 import**：本模块被 ``probes/probe_v3_isolation.py`` 的 AST 扫描
       与 ``tests/`` 引用；lightgbm 缺失时**模块本身仍须可导入**（否则守卫探针会误红）。
    """
    try:
        import lightgbm as lgb
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于运行环境
        raise ImportError(
            "🔴 V3 训练需要 lightgbm，但当前环境没装"
            "（服务器 conda 环境 `ql` 已装 lightgbm 4.7.0；本机请 `pip install lightgbm`）"
        ) from exc
    return lgb


def default_label_gain(n_grades: int | None = None) -> list[int]:
    """默认 ``label_gain`` = **线性档位** ``[0, 1, ..., n-1]``（不是 LightGBM 的 2 的幂）。

    🔴 长度必须 ≥ 最大 grade + 1（否则 LightGBM 会静默截断档位 ⇒ 排序目标失真）。
    """
    n = int(C.RANK_GRADE_COUNT if n_grades is None else n_grades)
    if n < 2:
        raise ValueError(f"🔴 相关性档位数必须 ≥ 2，实得 {n}")
    return list(range(n))


def _as_frame(X: Any) -> pd.DataFrame:
    """把输入统一成 ``DataFrame``（列名保留 ⇒ LightGBM 的特征名可用）。"""
    if isinstance(X, pd.DataFrame):
        return X
    arr = np.asarray(X, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if arr.ndim != 2:
        raise ValueError(f"🔴 特征矩阵必须是二维，实得 ndim={arr.ndim}")
    return pd.DataFrame(arr)


def _check_group_sizes(group_sizes: Any, n_rows: int) -> list[int]:
    """校验分组：必须是一组正整数，且**总和 = 行数**（否则行序与 group 错位 ⇒ 静默学错）。"""
    if group_sizes is None:
        raise ValueError("🔴 group_sizes 不能为空")
    sizes = [int(x) for x in group_sizes]
    if any(s <= 0 for s in sizes):
        raise ValueError(f"🔴 每个交易日组的行数必须为正，实得 {sizes}")
    total = int(sum(sizes))
    if total != int(n_rows):
        raise ValueError(
            f"🔴 分组行数合计 {total} ≠ 特征行数 {n_rows} ⇒ 行序与 group 错位，拒绝训练"
        )
    return sizes


def _check_grades(grades: Any, n_rows: int, n_grades: int) -> np.ndarray:
    """校验 relevance：整数、落在 ``[0, n_grades-1]``。"""
    arr = np.asarray(grades)
    if arr.ndim != 1:
        raise ValueError(f"🔴 grades 必须是一维，实得 ndim={arr.ndim}")
    if arr.shape[0] != int(n_rows):
        raise ValueError(
            f"🔴 grades 长度 {arr.shape[0]} ≠ 特征行数 {n_rows} ⇒ 行序错位，拒绝训练"
        )
    if not np.issubdtype(arr.dtype, np.integer):
        rounded = np.rint(arr.astype(float))
        if not np.allclose(arr.astype(float), rounded):
            raise ValueError("🔴 grades 必须是整数（relevance 分档）")
        arr = rounded.astype(np.int64)
    arr = arr.astype(np.int64)
    if arr.size and (int(arr.min()) < 0 or int(arr.max()) > int(n_grades) - 1):
        raise ValueError(
            f"🔴 grades 超出 [0, {int(n_grades) - 1}]（实得 [{int(arr.min())}, {int(arr.max())}]）"
        )
    return arr


def _check_weights(weights: Any, n_rows: int) -> np.ndarray | None:
    """校验样本权重：一维、非负、长度对齐（🔴 负权重会让 LightGBM 直接报错）。"""
    if weights is None:
        return None
    arr = np.asarray(weights, dtype=float).reshape(-1)
    if arr.shape[0] != int(n_rows):
        raise ValueError(
            f"🔴 weights 长度 {arr.shape[0]} ≠ 特征行数 {n_rows} ⇒ 行序错位，拒绝训练"
        )
    if not np.isfinite(arr).all():
        raise ValueError("🔴 weights 含 NaN/Inf")
    if arr.size and float(arr.min()) < 0:
        raise ValueError(f"🔴 weights 不得为负（实得 min={float(arr.min())}）")
    return arr


def _base_params(params: dict | None, fallback: dict) -> dict:
    """合并调用方参数与默认参数（🔴 调用方可以覆盖，但 objective 由本模块钉死）。"""
    out = dict(fallback if params is None else params)
    for key in ("verbosity", "deterministic", "force_row_wise"):
        out.setdefault(key, True if key == "deterministic" else -1 if key == "verbosity" else True)
    return out


# ----------------------------------------------------------------- 数据集
def build_rank_dataset(
    X: pd.DataFrame,
    grades: np.ndarray,
    group_sizes: list[int],
    weights: np.ndarray | None = None,
    params: dict | None = None,
):
    """构造 **lambdarank** 数据集（组 = 交易日）。

    参数
    ----
    X : 特征矩阵（index = 行，columns = 特征）
    grades : 整数 relevance（长度必须 = 行数；取值域 ``[0, RANK_GRADE_COUNT-1]``）
    group_sizes : 每个交易日组的行数（**按日期升序的连续分组**；总和必须 = 行数）
    weights : 样本权重（长度必须 = 行数；``None`` = 不加权）
    params : 传给 LightGBM Dataset 的参数（一般直接传 ``config_v3.LGB_PARAMS``）

    🔴 **行顺序契约**：行必须「**按日期升序、同日期内按 instrument 升序**」排列，
       ``group_sizes`` 与之**逐段对应** ⇒ 错位会静默学到别的排序（本函数会校验行数总和）。
    """
    frame = _as_frame(X)
    sizes = _check_group_sizes(group_sizes, len(frame))
    y = _check_grades(grades, len(frame), int(C.RANK_GRADE_COUNT))
    w = _check_weights(weights, len(frame))
    # 🔴 **先校验契约，再 import 依赖** ⇒ 契约错位不会被「依赖缺失」掩盖
    lgb = _import_lightgbm()
    return lgb.Dataset(
        frame,
        label=y,
        group=sizes,
        weight=w,
        params=dict(params) if params else None,
        free_raw_data=False,
    )


# ----------------------------------------------------------------- 训练
def train_ranker(
    X: pd.DataFrame,
    grades: np.ndarray,
    group_sizes: list[int],
    weights: np.ndarray | None = None,
    params: dict | None = None,
    num_boost_round: int | None = None,
    label_gain: list[int] | None = None,
):
    """训练 **lambdarank** 排序模型（V3 实验臂）。

    🔴 **不许**传 ``valid_sets`` / ``early_stopping``（隐性泄漏，见模块 docstring）。
    🔴 ``label_gain`` 默认 ``[0,1,2,3,4]``（对应 ``RANK_GRADE_COUNT = 5``）⇒ 线性档位。
    """
    gain = default_label_gain() if label_gain is None else [int(x) for x in label_gain]
    if any(g < 0 for g in gain):
        raise ValueError(f"🔴 label_gain 不得为负，实得 {gain}")
    max_grade = int(np.max(np.asarray(grades, dtype=np.int64))) if len(grades) else 0
    if max_grade >= len(gain):
        raise ValueError(
            f"🔴 最大 grade {max_grade} 超出 label_gain 长度 {len(gain)} ⇒ 档位会被静默截断"
        )
    lgb = _import_lightgbm()
    final_params = _base_params(params, dict(C.LGB_PARAMS))
    final_params["objective"] = str(C.MODEL_OBJECTIVE)
    final_params["label_gain"] = gain
    dataset = build_rank_dataset(X, grades, group_sizes, weights=weights, params=final_params)
    rounds = int(C.NUM_BOOST_ROUND if num_boost_round is None else num_boost_round)
    if rounds <= 0:
        raise ValueError(f"🔴 num_boost_round 必须为正，实得 {rounds}")
    return lgb.train(final_params, dataset, num_boost_round=rounds)


def train_regressor(
    X: pd.DataFrame,
    y: np.ndarray,
    weights: np.ndarray | None = None,
    params: dict | None = None,
    num_boost_round: int | None = None,
):
    """训练 **MSE** 回归模型（**单变量对照臂**：只把损失函数从 lambdarank 换成 MSE）。

    🔴 **单变量纪律**：超参必须与主臂 :data:`config_v3.LGB_PARAMS` **逐项相同**
       （只换 ``objective``/``metric``）⇒ 差异能归因到**损失函数**，
       而不是正则强度、树深、叶子样本数等一堆别的东西。
    🔴 2026-11-26 只读审计发现：原先这里用的是 :data:`config_v3.CONTROL_LGB_PARAMS`
       （与主臂差 8 个超参）⇒ 消融结论会被误归因 ⇒ 已改为
       :data:`config_v3.MSE_ARM_LGB_PARAMS`。
    🔴 标签表示的**伴生差异**（不可消除，如实披露）：lambdarank 只能吃**整数 relevance
       grade**（5 档），MSE 臂吃**连续数值超额** ⇒ 两臂的标签**信息量同源、表示不同**。
    """
    frame = _as_frame(X)
    target = np.asarray(y, dtype=float).reshape(-1)
    if target.shape[0] != len(frame):
        raise ValueError(
            f"🔴 标签长度 {target.shape[0]} ≠ 特征行数 {len(frame)} ⇒ 行序错位，拒绝训练"
        )
    if not np.isfinite(target).all():
        raise ValueError("🔴 标签含 NaN/Inf（训练前应已 DropnaLabel + 中心化）")
    w = _check_weights(weights, len(frame))
    lgb = _import_lightgbm()
    final_params = _base_params(params, dict(C.MSE_ARM_LGB_PARAMS))
    final_params["objective"] = str(C.CONTROL_OBJECTIVE)
    dataset = lgb.Dataset(frame, label=target, weight=w, params=final_params, free_raw_data=False)
    rounds = int(C.NUM_BOOST_ROUND if num_boost_round is None else num_boost_round)
    if rounds <= 0:
        raise ValueError(f"🔴 num_boost_round 必须为正，实得 {rounds}")
    return lgb.train(final_params, dataset, num_boost_round=rounds)


def predict_scores(booster: Any, X: pd.DataFrame) -> np.ndarray:
    """预测分数（**越大越好**；lambdarank 输出的是组内相对分，跨组不可比）。

    🔴 lambdarank 的输出**只在同一交易日组内**有意义 ⇒ 跨组比较（不同日期的分数）
       **没有绝对含义**（评估层请用 RankIC / 组内排名，不要跨组比大小）。
    """
    frame = _as_frame(X)
    pred = np.asarray(booster.predict(frame), dtype=float).reshape(-1)
    if pred.shape[0] != len(frame):
        raise ValueError(
            f"🔴 预测长度 {pred.shape[0]} ≠ 特征行数 {len(frame)}"
        )
    return pred
