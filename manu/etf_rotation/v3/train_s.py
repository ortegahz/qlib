"""🔴 V3 S 层：**训练 + 打分**（研发分支；V2 是现役，本文件**不 import、不改写** V2）。

**V3 与 V2 的实质差异（对齐 ①②③④ 四项，其余逐项同构 ⇒ 可比）**

  ==========  ==========================================  ==============================
  维度        V2（现役）                                V3（本文件，实验臂）
  ==========  ==========================================  ==============================
  损失        LightGBM **MSE**                          LightGBM **lambdarank**（排序）
  标签后处理  DropnaLabel + **CSRankNorm**              DropnaLabel（🔴 不做 CSRankNorm）
  标签数值    排名（0~1）                               **数值超额**（减当日截面均值）
  训练池      csi300                                    **ETF 成分股并集**（自建 instruments）
  样本权重    无                                        **ΣW 加权**（sum_w = 字面 ΣW）
  截面中性化  无                                        规模档 + 流动性残差化（🔴 无行业）
  滚动重训    无                                        **每 60 个交易日扩窗重训**
  ==========  ==========================================  ==============================

**🔴 硬隔离契约**
* 本文件**不 import** V1 冻结件（``qlib_scores`` / 根 ``config`` / ``engine.*``）；
* 本文件**不 import** V2 的任何可执行模块（V2 只作**只读参考**；本文件是 V3 本地复刻）；
* 🔴 **唯一允许的跨包 import** = ``manu/etf_rotation/data/signal_policy.py``（按**文件路径**
  加载）—— 它是**每日链**的打分日栅格事实源，**不在** V1 冻结集、**不在** V2 包内。

**🔴 标签中心化的真实作用（不许夸大）**
「逐日减截面均值」在**排序 / IC 目标下是数学不变量**（同一天内整体平移不改变组内次序），
它的真实价值在 ① MSE 对照臂（数值口径才看得到）② 评估层的超额口径
③ 明确「跑赢大盘」的训练语义 ⇒ **manifest 里必须原样披露**，不许说成「提升了排序能力」。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import pickle
import platform
import sys
import time
import warnings
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
    "alpha158_handler_class",
    "build_score_handler",
    "build_train_handler",
    "extract_frozen_norm",
    "frozen_norm_class",
    "init_qlib",
    "prepare_training_frame",
    "score_universe",
    "train_and_score",
]

#: 🔴 **V3 训练池 / 打分池的 instruments 市场名**
#:    （qlib 按 `<provider>/instruments/<name>.txt` 解析）
V3_INSTRUMENTS_MARKET = "v3_etf_pool"
#: 🔴 **打分日栅格步长**（交易日）—— 与 V1/V2 的信号栅格步长同口径（10 个交易日）。
#:    ⚠️ ``config_v3`` **没有**这个常量（契约缺口）⇒ 本地显式写死并**在 manifest 披露**。
#: 打分分块前的**预热自然日**（Alpha158 最长回看 60 日 ⇒ 留 120 天预热；同 V2 口径）


# ----------------------------------------------------------------- 加载工具
def _load_by_path(path: Path, module_name: str):
    """按**绝对路径**加载一个模块（🔴 唯一登记进 ``sys.modules``，避免同名模块撞车）。

    🔴 为什么不用扁平 ``import splits``：V2 目录里也有 ``splits.py`` / ``labels.py``
       ⇒ 扁平 import 会随 ``sys.path`` 顺序**静默拿到 V2 的实现** ⇒ 必须按路径加载。
    """
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"🔴 无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    added = str(_V3_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(_V3_DIR))
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if added:
            try:
                sys.path.remove(str(_V3_DIR))
            except ValueError:  # pragma: no cover
                pass
    return module


#: 🔴 V3 模型层（按**路径**加载 ⇒ 任何导入方式都成立，且不与 V2 的同名模块撞车）
V3_MODEL = _load_by_path(_V3_DIR / "model.py", "manu_v3_model")


def _load_sibling(module_name: str):
    """加载 V3 的兄弟模块（``universe`` / ``neutralize`` / ``labels_v3`` / ``splits``）。

    🔴 这些模块由**另一位 agent** 同步编写（V3 的研发分支分工）⇒ 本文件**只按公开名字调用**，
       **绝不**为了「让它能跑」去改它们；缺失时抛中文异常（fail-closed，不静默降级）。
    """
    path = _V3_DIR / f"{module_name}.py"
    if not path.is_file():
        raise ImportError(
            f"🔴 V3 兄弟模块缺失：{path}（V3 训练链依赖它；请先完成该模块）"
        )
    return _load_by_path(path, f"manu_v3_{module_name}")


def _load_signal_policy():
    """按绝对路径加载 ``manu/etf_rotation/data/signal_policy.py``（🔴 唯一跨包 import）。

    🔴 它是**每日链**的打分日栅格事实源（**不在** V1 冻结集、**不在** V2 包内）⇒ 允许引用；
       写法照抄 ``manu/etf_rotation/daily/run_qlib_score.py::_load_signal_policy``。
    """
    path = C.V3_ROOT.parent / "data" / "signal_policy.py"
    return _load_by_path(path, "manu_v3_signal_policy")


def _qlib_fetch_kwargs() -> tuple[str, str]:
    """qlib handler 的取数参数 ``(col_set, data_key)``。

    🔴 优先用 qlib 自己的常量（``DataHandlerLP.CS_ALL`` / ``DataHandlerLP.DK_L``）；
       qlib **没装**时退回**逐字等价**的字面量（``"__all"`` / ``"learn"``，
       已对 qlib 0.9.8 的 ``qlib/data/dataset/handler.py`` 核过）⇒
       本模块的**纯 pandas 部分**（训练矩阵装配）在没有 qlib 的机器上也能跑与测。
    """
    try:
        from qlib.data.dataset.handler import DataHandlerLP

        return DataHandlerLP.CS_ALL, DataHandlerLP.DK_L
    except ImportError:  # pragma: no cover - 取决于运行环境
        return "__all", "learn"


def md5_of(path: Path) -> str:
    """算文件 md5（产物指纹；口径同 V2）。"""
    h = hashlib.md5()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------- qlib 初始化
def init_qlib(provider_uri: str) -> None:
    """初始化 qlib（🔴 必须先 init，否则 handler 构造直接报错）。

    🔴 **取数并行度必须锁死**：qlib 的 ``StaticDatasetProvider.fetch`` 会按
       ``C["kernels"]``（默认＝**可用核数**）用 ``backend="multiprocessing"``
       **fork** 出等量子进程（80 核 ⇒ **78** 个），每个都继承父进程的全部内存
       ⇒ 折级并发时整机内存被打爆（实测 used 111 GB / 125 GB）。
       ⇒ init 之后立刻把 ``C["kernels"]`` 压到 ``config_v3.QLIB_FETCH_KERNELS``
       （**只改并行度，不改任何数据/口径**）。
    """
    import qlib
    from qlib.constant import REG_CN

    qlib.init(provider_uri=str(provider_uri), region=REG_CN)
    try:
        # 🔴 用属性访问而不是 ``from qlib.config import ...``：
        #    后者会被 V3 的「不许 import V1 冻结件」扫描器按**模块名 token** 误判
        #    （``qlib.config`` 的 ``config`` 段命中 V1 冻结件名）⇒ 这里绕开该误报，
        #    **不改动扫描器**（扫描器的 teeth 是对的，宁可改调用方式）。
        qlib.config.C["kernels"] = int(C.QLIB_FETCH_KERNELS)
    except Exception as exc:  # pragma: no cover - qlib 配置结构变化时的兜底
        print(f"[WARN] 未能设置 qlib kernels（{exc!r}）⇒ 取数并行度仍是默认（核数）")


# ----------------------------------------------------------------- handler
def alpha158_handler_class(drop: tuple[str, ...] = ()):
    """返回**按名消融**的 Alpha158 handler 子类（V3 本地复刻；**不 import V2**）。

    🔴 qlib 的 ``Alpha158`` handler 内部用 ``self.get_feature_config()`` 取特征清单
       ⇒ 子类覆写它即可**精确剔名**（不影响其它同窗口特征）。
    """
    from qlib.contrib.data.handler import Alpha158

    drop_set = {str(x) for x in (drop or ())}

    class Alpha158V3(Alpha158):
        """V3 版 Alpha158：支持按特征名精确剔除（与 V2 同口径的 ROC5 消融）。"""

        def get_feature_config(self):
            """按名剔除特征（V3 沿用 ``config_v3.DROPPED_FEATURES`` 口径）。"""
            fields, names = super().get_feature_config()
            keep = [(f, n) for f, n in zip(fields, names) if str(n) not in drop_set]
            return [f for f, _ in keep], [n for _, n in keep]

    return Alpha158V3


def frozen_norm_class():
    """**训练期拟合 → 推理期冻结**的 ZScoreNorm（V3 本地复刻，不 import V1 / V2）。

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
            if (getattr(self, "mean_train", None) is None
                    or getattr(self, "std_train", None) is None):
                raise ValueError("FrozenZScoreNorm 尚未注入训练期统计量")
            return self

    return FrozenZScoreNorm


def _infer_processors(frozen=None) -> list[dict]:
    """推理期处理器链（**取自** ``config_v3.LABEL_INFER_PROCESSORS``）。

    🔴 打分期把链里的 ``ZScoreNorm`` 换成**训练期冻结实例**（其余处理器照旧）。
    """
    base = [dict(p) for p in C.LABEL_INFER_PROCESSORS]
    if frozen is None:
        return base
    out: list[Any] = []
    for proc in base:
        if str(proc.get("class", "")) == "ZScoreNorm":
            out.append(frozen)
        else:
            out.append(dict(proc))
    return out


def _learn_processors() -> list[dict]:
    """学习期处理器链（**取自** ``config_v3.LABEL_LEARN_PROCESSORS``）。

    🔴 V3 的学习期链**只有** ``DropnaLabel``（🔴 **不含** CSRankNorm）
       ⇒ 标签保持**数值超额**（做了 CSRankNorm 就会把数值抹成排名 ⇒ 排序目标失真）。
    """
    return [dict(p) for p in C.LABEL_LEARN_PROCESSORS]


def build_train_handler(universe_name: str, drop: tuple[str, ...], fit_end: str, start_time: str):
    """训练期 handler：股票池 = ``universe_name``（**自建 instruments 的市场名**）。

    🔴 ``fit_end`` 必须是 **embargo 裁剪后**的训练末日 ⇒ 归一化统计量**不包含**
       embargo 之后的样本（``config_v3`` 的 fit 窗随训练段同步收窄）。
    """
    handler_cls = alpha158_handler_class(drop)
    return handler_cls(
        start_time=start_time,
        end_time=fit_end,
        fit_start_time=C.FIT_START,
        fit_end_time=fit_end,
        instruments=universe_name,
        label=([C.LABEL_EXPRESSION], ["LABEL0"]),
        infer_processors=_infer_processors(),
        learn_processors=_learn_processors(),
    )


def build_score_handler(universe_name: str, drop: tuple[str, ...], frozen,
                        start_time: str, end_time: str):
    """打分期 handler：股票池 = ``universe_name``，**归一化统计量冻结自训练期**。"""
    handler_cls = alpha158_handler_class(drop)
    return handler_cls(
        start_time=start_time,
        end_time=end_time,
        fit_start_time=C.FIT_START,
        fit_end_time=frozen.fit_end_time,
        instruments=universe_name,
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
    mean_bad = not np.isfinite(np.asarray(zs.mean_train)).all()
    std_bad = not np.isfinite(np.asarray(zs.std_train)).all()
    if mean_bad or std_bad:
        raise RuntimeError("训练期 ZScoreNorm 统计量含 NaN/Inf ⇒ fit 窗无效，拒绝继续")
    frozen = frozen_cls(zs.fit_start_time, zs.fit_end_time, fields_group=zs.fields_group)
    frozen.mean_train = np.array(zs.mean_train, copy=True)
    frozen.std_train = np.array(zs.std_train, copy=True)
    frozen.ignore = np.array(zs.ignore, copy=True)
    frozen.cols = list(zs.cols)
    return frozen


# ----------------------------------------------------------------- 长表工具
def _normalize_long_frame(df: pd.DataFrame) -> pd.DataFrame:
    """把 handler 取出的宽表转成**长表**（列 = ``date`` / ``instrument`` / 特征 / ``LABEL0``）。

    🔴 qlib 的 handler 索引是 ``(datetime, instrument)``（Alpha158 的 loader 做了 swaplevel），
       但不同 qlib 版本 / 不同 loader 的层级顺序可能不同 ⇒ 这里**按索引名**归一，
       名字缺失时按「(datetime, instrument)」的位置约定兜底。
    """
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[-1]) for c in out.columns]
    out = out.reset_index()
    if "datetime" in out.columns:
        out = out.rename(columns={"datetime": "date"})
    if "date" not in out.columns or "instrument" not in out.columns:
        raise RuntimeError(
            f"🔴 handler 输出的索引层无法归一（列={list(out.columns)[:4]}）"
        )
    out["date"] = pd.to_datetime(out["date"])
    out["instrument"] = out["instrument"].astype(str)
    return out


def _center_label(values: np.ndarray, dates: pd.Index, instruments: pd.Index,
                  mode: str | None, bench_fwd: pd.Series | None) -> np.ndarray:
    """标签中心化（**数值层面**，不做排名）⇒ 数值超额（**返回面板口径的二维数组**）。

    🔴 口径来自 ``config_v3.LABEL_CENTERING``：
       * ``"market_mean"`` ⇒ 减**当日全市场截面均值**（贴「跑赢大盘」的用户表述）；
       * ``"benchmark"``   ⇒ 减**基准同窗口收益**（沪深300，逐日常数）；
       * ``None`` / ``""`` ⇒ 不中心化（原始窗口收益）。

    🔴 **诚实披露**：在排序 / IC 目标下「逐日中心化」是**不变量**（组内整体平移不改变次序），
       它的真实作用在 ① MSE 对照臂 ② 评估层超额口径 ③ 「跑赢大盘」的训练语义。
    """
    labels = _load_sibling("labels_v3")
    if mode in (None, "", "none"):
        return values
    if mode == "benchmark" and bench_fwd is None:
        raise RuntimeError("🔴 中心化模式 = benchmark ⇒ 必须提供 bench_fwd")
    if mode not in ("market_mean", "benchmark"):
        raise ValueError(f"🔴 未知的标签中心化模式：{mode!r}")
    panel = pd.DataFrame(values, index=dates, columns=instruments)
    centered, _stats = labels.center_label(panel, mode=mode, bench_fwd=bench_fwd)
    return centered.to_numpy(dtype=float)


def _rank_grades(values: np.ndarray, dates: pd.Index, instruments: pd.Index) -> np.ndarray:
    """数值超额 ⇒ **整数 relevance**（``labels_v3.rank_grades``，默认 5 档）。"""
    labels = _load_sibling("labels_v3")
    panel = pd.DataFrame(values, index=dates, columns=instruments)
    grades = labels.rank_grades(panel, n_grades=int(C.RANK_GRADE_COUNT))
    arr = np.rint(grades.to_numpy(dtype=float))
    if not np.isfinite(arr).all():
        raise RuntimeError(
            "🔴 relevance grade 出现 NaN/Inf ⇒ 拒绝训练（标签面板里有非有限值）"
        )
    n_grades = int(C.RANK_GRADE_COUNT)
    if arr.size and (float(arr.min()) < 0 or float(arr.max()) > n_grades - 1):
        raise RuntimeError(
            f"🔴 relevance grade 越界（[{float(arr.min())}, {float(arr.max())}]，"
            f"合法 [0, {n_grades - 1}]）⇒ 拒绝训练"
        )
    # 🔴 **整数 dtype**（lambdarank 的 label 必须是整数 relevance）
    return arr.astype(np.int64)


def _sample_weights(long: pd.DataFrame, universe_info: dict | None) -> np.ndarray:
    """ΣW 样本权重（口径 = ``config_v3.SAMPLE_WEIGHT_MODE``；缺失给 1.0）。"""
    info = universe_info or {}
    sum_w = info.get("sum_w") or {}
    if hasattr(sum_w, "to_dict"):
        sum_w = sum_w.to_dict()
    mode = str(C.SAMPLE_WEIGHT_MODE)
    out = np.empty(len(long), dtype=float)
    for i, inst in enumerate(long["instrument"].to_numpy()):
        raw = sum_w.get(inst, None)
        value = 1.0 if raw is None else float(raw)
        if not math.isfinite(value):
            value = 1.0
        if mode == "sum_w":
            out[i] = max(value, 0.0)
        elif mode == "sqrt_sum_w":
            out[i] = math.sqrt(max(value, 0.0))
        elif mode == "uniform":
            out[i] = 1.0
        else:
            raise ValueError(f"🔴 未知的样本权重模式：{mode!r}")
    return out


def _normalize_group_weights(weights: np.ndarray, groups: list[int]) -> np.ndarray:
    """组内均值归一（每组的平均权重 = 1 ⇒ 组与组之间可比，组内相对权重不变）。"""
    out = np.array(weights, dtype=float, copy=True)
    start = 0
    for size in groups:
        block = out[start:start + size]
        mean = float(block.mean()) if block.size else 0.0
        if mean > 0:
            out[start:start + size] = block / mean
        start += size
    return out


def _long_to_panel(long: pd.DataFrame, feature_cols: list[str]):
    """长表（date × instrument）⇒ ``(面板3D, date_pos, inst_pos, dates, instruments)``。

    🔴 **唯一装配点**：训练侧（:func:`prepare_training_frame`）与打分侧
       （:func:`score_universe`）必须用**同一个**面板装配逻辑 ⇒ 否则
       「训练时被残差化的特征」与「打分时喂给模型的特征」会错位
       （train/serve skew）。
    🔴 缺格 = NaN（不填 0）⇒ 后面残差化时按「有效行掩码」处理。
    """
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(long["date"]).unique()))
    instruments = pd.Index(sorted(long["instrument"].unique()))
    date_pos = dates.get_indexer(pd.to_datetime(long["date"]).to_numpy())
    inst_pos = instruments.get_indexer(long["instrument"].to_numpy())
    panel = np.full((len(dates), len(instruments), len(feature_cols)), np.nan)
    panel[date_pos, inst_pos, :] = long[feature_cols].to_numpy(dtype=float)
    return panel, date_pos, inst_pos, dates, instruments


def _usable_rows(X: np.ndarray, y: np.ndarray | None = None) -> np.ndarray:
    """样本可用掩码（🔴 **训练侧与打分侧必须用同一条规则**）。

    * 至少要有**一个有限**的残差化特征 ⇒ 控制变量缺测（整行残差 NaN）的标的
      **没有可发表的意见**（训练时被剔除，打分侧也剔除 ⇒ 两边样本集一致）；
    * 训练侧额外要求**残差化标签有限**（打分侧没有标签，故不适用）。
    """
    mask = np.isfinite(np.asarray(X, dtype="float64")).any(axis=1)
    if y is not None:
        mask &= np.isfinite(np.asarray(y, dtype="float64"))
    return mask


def _pit_membership_panel(universe_info: dict | None):
    """取 PIT 成员面板（🔴 没有 ⇒ 返回 None ⇒ **默认路径逐位不变**）。"""
    panel = (universe_info or {}).get(C.PIT_MEMBERSHIP_KEY)
    if panel is None:
        return None
    # 🔴 面板必须是 DataFrame（否则掩码会静默失效 ⇒ 口径错乱 ⇒ 拒绝继续）
    if not (hasattr(panel, "to_numpy") and hasattr(panel, "index")):
        raise RuntimeError(
            "🔴 PIT 成员面板不是 DataFrame（universe_info 的口径错乱）⇒ 拒绝继续"
        )
    return panel


def _pit_fit_mask(panel: pd.DataFrame | None, dates, instruments,
                   what: str) -> np.ndarray | None:
    """PIT 成员面板 ⇒ 当前轴上的**拟合子集掩码**（``bool[n_dates, n_instruments]``）。

    🔴 **只作拟合掩码，绝不当行过滤器**：
       * **拟合子集** ＝ 掩码为 True 的行（回归只用它们估系数）；
       * **变换全集** ＝ 该（日期, 特征）下所有可变换的行（**掩码为 False 的行
         照样被变换**）⇒ 打分池**不按日筛** ⇒ ETF 权重有分可映射。

    🔴 **为什么两侧都要过这一关**（B1）：训练侧只在「当日 PIT 成员」上拟合，
       打分侧要对**整个打分池**出分 ⇒ 两侧必须**用同一个拟合子集**估系数
       ⇒ 否则同一天同一特征的残差不同 ⇒ train/serve skew。

    🔴 **fail-closed**：日期或标的覆盖不住 ⇒ 直接 raise（不许静默当「非成员」）。
    """
    if panel is None or panel.empty:
        return None
    index = pd.DatetimeIndex(panel.index)
    date_axis = pd.DatetimeIndex(pd.to_datetime(dates))
    missing_dates = date_axis.difference(index)
    if len(missing_dates):
        raise RuntimeError(
            f"🔴 {what}：PIT 成员面板盖不住 {len(missing_dates)} 个日期"
            f"（例 {[str(d.date()) for d in missing_dates[:3]]}）⇒ 拒绝继续"
            "（面板与本侧日期轴必须同源）"
        )
    panel_cols = pd.Index([str(c) for c in panel.columns])
    wanted = pd.Index([str(c) for c in instruments])
    missing_inst = wanted.difference(panel_cols)
    if len(missing_inst):
        raise RuntimeError(
            f"🔴 {what}：PIT 成员面板缺 {len(missing_inst)} 个标的"
            f"（例 {list(missing_inst[:3])}）⇒ 拒绝继续"
            "（面板与本侧标的集合必须逐位一致）"
        )
    sub = panel.reindex(index=date_axis, columns=wanted)
    return np.asarray(sub.to_numpy(dtype=bool))


def _apply_pit_membership_mask(long: pd.DataFrame, universe_info: dict | None):
    """🔴 **逐日 PIT 训练池掩码**：剔除「当日不在池内」的行（**只作用于训练侧**）。

    * 🔴 **打分侧不适用**（打分池必须保持 ETF 全并集口径 ⇒ 见
      `_assert_score_pool_covers_etf_union` 的硬断言）；
    * 掩码**早于**权重 / 中心化 / 中性化 ⇒ 截面统计全部在**当日池**上做
      （否则「逐日减截面均值」减的是静态池的均值 ⇒ 口径不一致）；
    * 🔴 面板**盖不住**训练日期 ⇒ raise（fail-closed：不许静默把整段样本丢掉）。

    返回 ``(long, stats)``；``stats`` 记掩码前后的行数（⇒ 写进 manifest）。
    """
    stats: dict = {
        "enabled": False,
        "n_rows_before": len(long),
        "n_rows_after": len(long),
        "n_rows_dropped": 0,
        "n_panel_days": 0,
        "n_rows_unknown_instrument": 0,
        "members_min": 0,
        "members_median": 0.0,
        "members_max": 0,
    }
    panel = _pit_membership_panel(universe_info)
    if panel is None or panel.empty:
        return long, stats
    index = pd.DatetimeIndex(panel.index)
    dates = pd.DatetimeIndex(pd.to_datetime(long["date"]))
    pos_date = index.get_indexer(dates)
    if (pos_date < 0).any():
        missing = pd.DatetimeIndex(dates[pos_date < 0]).unique()
        raise RuntimeError(
            f"🔴 PIT 成员面板盖不住训练日期（缺 {len(missing)} 天，"
            f"例 {[str(d.date()) for d in missing[:3]]}）⇒ 拒绝继续"
            "（不许静默把整段样本丢掉）"
        )
    pos_inst = panel.columns.get_indexer(long["instrument"].astype(str).to_numpy())
    # 🔴 **fail-closed（标的侧）**：面板里**根本没有**的标的 ⇒ 口径错乱
    #    （面板与训练池不是同一批标的）⇒ 旧实现把它们**静默**当成「当日非成员」
    #    ⇒ 与真正的非成员混在同一个桶里 ⇒ 掩码悄悄丢行 ⇒ 无人发现
    #    ⇒ 现在直接 raise（不许静默放大/缩小池子）。
    unknown = pos_inst < 0
    n_unknown = int(unknown.sum())
    if n_unknown:
        unknown_codes = sorted(
            {str(c) for c in long.loc[unknown, "instrument"].astype(str)}
        )
        raise RuntimeError(
            f"🔴 PIT 成员面板缺 {n_unknown} 行对应的标的（{len(unknown_codes)} 只，"
            f"例 {unknown_codes[:5]}）⇒ 未知标的会被静默当成「当日非成员」⇒ 拒绝继续"
            "（fail-closed：面板与训练池必须逐位同一批标的）"
        )
    member = np.zeros(len(long), dtype=bool)
    member[:] = panel.to_numpy()[pos_date, pos_inst]
    daily = panel.to_numpy().sum(axis=1)
    stats.update({
        "enabled": True,
        "n_rows_unknown_instrument": n_unknown,
        "n_rows_after": int(member.sum()),
        "n_rows_dropped": int((~member).sum()),
        "n_panel_days": len(daily),
        "members_min": int(daily.min()) if len(daily) else 0,
        "members_median": float(np.median(daily)) if len(daily) else 0.0,
        "members_max": int(daily.max()) if len(daily) else 0,
    })
    return long.loc[member].reset_index(drop=True), stats


def prepare_training_frame(handler, spec, universe_info: dict | None,
                           tier_panel: pd.DataFrame | None = None,
                           liquidity_panel: pd.DataFrame | None = None,
                           neutralize: bool | None = None,
                           center: str | None = None) -> dict:
    """从 handler 取「特征 + 标签」⇒ 组装训练矩阵（V3 的训练矩阵唯一装配点）。

    返回 dict
    -------
    X : 特征矩阵（index 与 ``dates``/``instruments`` 逐行对应）
    y : **数值超额**（标签减当日截面均值 ⇒ ``config_v3.LABEL_CENTERING``）
    grades : 整数 relevance（``labels_v3.rank_grades``）
    groups : 每个交易日组的行数（**按日期升序**）
    weights : ΣW 样本权重（缺失给 1.0；**最后按组内均值归一**）
    dates / instruments : 每行对应的日期 / 标的
    feature_cols : 特征列名（顺序 = X 的列顺序）

    🔴 **行顺序契约**：行必须「**按日期升序、同日期内按 instrument 升序**」
       ⇒ ``groups`` 才是连续分组（lambdarank 的组 = 交易日）。
    """
    col_set, data_key = _qlib_fetch_kwargs()
    df = handler.fetch(
        selector=slice(spec.train_start, spec.train_end),
        level="datetime",
        col_set=col_set,
        data_key=data_key,
    )
    long = _normalize_long_frame(df)
    if "LABEL0" not in long.columns:
        raise RuntimeError(f"🔴 handler 输出里没有 LABEL0（列={list(long.columns)[:5]}…）")
    feature_cols = [c for c in long.columns if c not in ("date", "instrument", "LABEL0")]
    long = long.sort_values(["date", "instrument"]).reset_index(drop=True)

    # 🔴 **逐日 PIT 训练池掩码**（新口径；🔴 默认口径下 `universe_info` 不带面板
    #    ⇒ 这一行**什么都不做** ⇒ 默认路径逐位不变）
    long, pit_stats = _apply_pit_membership_mask(long, universe_info)

    weights = _sample_weights(long, universe_info)
    keep = weights >= float(C.SAMPLE_WEIGHT_MIN)
    if not keep.all():
        long = long.loc[keep].reset_index(drop=True)
        weights = weights[keep]

    # 🔴 **没有标签的行直接丢**（= qlib ``DropnaLabel`` 的语义）
    keep_label = np.isfinite(long["LABEL0"].to_numpy(dtype=float))
    if bool(pit_stats.get("enabled")):
        # 🔴🔴 **PIT 臂：拟合子集必须「恰好」＝ 当日 PIT 成员**（B1 的根因修复）
        #    训练侧的**面板**要保留「标签缺测、但当日是 PIT 成员」的行：
        #    标签过滤只作用于**训练样本**，不作用于**拟合子集** ⇒ 否则训练侧的
        #    拟合子集比打分侧窄（打分侧根本没有标签概念）⇒ 两侧系数不同源
        #    ⇒ 同一天同一特征的残差不相等 ⇒ train/serve skew。
        #    （标签缺测的行在后面会被 ``_usable_rows(X, y)`` 剔出训练样本。）
        panel_long = long.reset_index(drop=True)
        panel_weights = weights
        long = long.loc[keep_label].reset_index(drop=True)
        weights = weights[keep_label]
    else:
        if not keep_label.all():
            long = long.loc[keep_label].reset_index(drop=True)
            weights = weights[keep_label]
        panel_long = long
        panel_weights = weights

    # ---- 面板（日期 × 标的）：缺格 = NaN ⇒ 后面逐格对齐回长表 ----
    X_panel, date_pos, inst_pos, dates, instruments = _long_to_panel(
        panel_long, feature_cols
    )
    label_panel = np.full((len(dates), len(instruments)), np.nan)
    label_panel[date_pos, inst_pos] = panel_long["LABEL0"].to_numpy(dtype=float)

    center_mode = str(C.LABEL_CENTERING if center is None else center)
    bench_fwd = None
    if center_mode == "benchmark":
        bench_fwd = _fetch_benchmark_forward(spec.train_start, spec.train_end)
    y_panel = _center_label(label_panel, dates, instruments, center_mode, bench_fwd)

    # ---- 中性化（规模档 + 流动性；🔴 行业永远关闭）----
    neutralize_enabled = bool(C.NEUTRALIZATION_ENABLED if neutralize is None else neutralize)
    neutral_stats: dict = {"enabled": neutralize_enabled, "industry": False}
    if neutralize_enabled:
        if tier_panel is None:
            raise RuntimeError(
                "🔴 开启了截面中性化但没有 tier_panel（规模档）⇒ 拒绝继续"
                "（`universe.size_tier_panel` 产出的档位面板是必需输入）"
            )
        X_panel, y_panel, neutral_stats = _neutralize_panels(
            X_panel, y_panel, dates, instruments, tier_panel, liquidity_panel,
            fit_mask=_pit_fit_mask(
                _pit_membership_panel(universe_info), dates, instruments, "训练侧"
            ),
        )

    grades_panel = _rank_grades(y_panel, dates, instruments)

    # ---- ⇒ 长表口径（逐行对齐：面板索引回长表的 (date, instrument)）----
    X = X_panel[date_pos, inst_pos, :]
    y = y_panel[date_pos, inst_pos]
    grades = grades_panel[date_pos, inst_pos]
    finite = _usable_rows(X, y)
    if not finite.all():
        # 🔴 中性化可能把某格变成 NaN（控制变量缺测）⇒ 这类样本没有可用标签 ⇒ 丢
        panel_long = panel_long.loc[finite].reset_index(drop=True)
        panel_weights = panel_weights[finite]
        X = X[finite]
        y = y[finite]
        grades = grades[finite]
    # 🔴 **分组必须连续且非空**：某日的样本被中性化/缺格**全部**剔除时，
    #    该日不能再作为 lambdarank 的一组（0 大小的组 LightGBM 会直接报错）
    #    ⇒ 这里按实际保留下来的样本**重算**分组与日期索引。
    if panel_long.empty:
        raise RuntimeError(
            "🔴 训练样本为空（标签/中性化过滤后没有可用样本）⇒ 拒绝训练"
        )
    uniq_dates, counts = np.unique(
        pd.to_datetime(panel_long["date"]).to_numpy(), return_counts=True
    )
    dates = pd.DatetimeIndex(uniq_dates)
    groups = [int(c) for c in counts]
    weights = _normalize_group_weights(panel_weights, groups)

    return {
        "X": X,
        "y": y,
        "grades": grades,
        "groups": groups,
        "weights": weights,
        # 🔴 **逐行对应值**（不是去重后的索引）⇒ 与 X / y / grades 逐行对齐
        "dates": panel_long["date"].to_numpy(),
        "instruments": panel_long["instrument"].to_numpy(),
        "unique_dates": dates,
        "unique_instruments": instruments,
        "feature_cols": feature_cols,
        "center": center_mode,
        "neutralize": neutral_stats,
        "pit": pit_stats,
    }


def _neutralize_panels(X_panel: np.ndarray, y_panel: np.ndarray, dates: pd.Index,
                       instruments: pd.Index, tier_panel: pd.DataFrame,
                       liquidity_panel: pd.DataFrame | None,
                       fit_mask: np.ndarray | None = None):
    """截面残差化（🔴 **绝不做行业中性化** —— 行业内去均值会让 ``E = W × S`` 塌缩）。

    🔴 口径：``config_v3.NEUTRALIZE_SIZE`` / ``NEUTRALIZE_LIQUIDITY`` / ``MIN_CROSS_SECTION_SIZE``。
    🔴 **纯性能优化**：改走 ``neutralize.cross_sectional_residualize_panel``
       （按日期对齐 controls 只做一次 + 按有效行掩码分组的批量 lstsq），
       数值口径与逐特征调用 ``cross_sectional_residualize`` **逐元素等价**
       （事实源仍是旧函数，对拍见 ``tests/test_v3_neutralize_guard.py``）。
    """
    neu = _load_sibling("neutralize")
    # 🔴 机器化守卫：只要有人把 INDUSTRY_NEUTRALIZATION_ENABLED 打开 ⇒ 立刻 raise
    if not hasattr(neu, "assert_no_industry_neutralization"):
        raise RuntimeError(
            "🔴 v3/neutralize.py 缺少 assert_no_industry_neutralization（守卫接口）"
        )
    neu.assert_no_industry_neutralization()
    controls = neu.build_controls(
        tier_panel,
        liquidity_panel=liquidity_panel,
        size=bool(C.NEUTRALIZE_SIZE),
        liquidity=bool(C.NEUTRALIZE_LIQUIDITY),
    )
    min_size = int(C.MIN_CROSS_SECTION_SIZE)
    # 🔴 **拟合子集掩码**（PIT 臂：拟合子集＝当日 PIT 成员；变换全集＝面板全部行）
    #    ⇒ 训练侧与打分侧**系数同源** ⇒ 无 train/serve skew（B1）。
    out, _ = neu.cross_sectional_residualize_panel(
        X_panel, dates, instruments, controls, min_size=min_size, fit_mask=fit_mask
    )
    y_res, _ = neu.cross_sectional_residualize_panel(
        y_panel[:, :, None], dates, instruments, controls, min_size=min_size,
        fit_mask=fit_mask,
    )
    stats: dict = {
        "enabled": True,
        "industry": False,
        "n_residualized": int(np.asarray(X_panel).shape[2]),
    }
    return out, y_res[:, :, 0], stats


def _fetch_benchmark_forward(start: str, end: str) -> pd.Series:
    """取基准（沪深300）的**同窗口**前瞻收益（逐日常数）⇒ 供 ``center="benchmark"`` 使用。"""
    from qlib.data import D

    bench = str(C.BENCHMARK)
    df = D.features([bench], [C.LABEL_EXPRESSION], start_time=str(start), end_time=str(end),
                     freq="day")
    df = df.reset_index().rename(columns={"datetime": "date"})
    series = df.set_index("date").iloc[:, 0]
    series.index = pd.to_datetime(series.index)
    return series.astype(float)


# ----------------------------------------------------------------- 守卫与工具
def _assert_not_blind(start: str, end: str, what: str) -> None:
    """🔴 **单盲守卫**：窗口一旦触碰 ``config_v3.BLIND_SEALED_WINDOW`` ⇒ 直接 raise。

    🔴 V2 已消耗过 2024-01-01 ~ 2026-11-03（单盲只跑一次）⇒ V3 的任何对照实验
       **一律不许**用它训练/打分/选参。
    🔴 打分窗口还必须落在 OOS 窗口（``config_v3.WALK_FORWARD_OOS_WINDOW``）之内。
    """
    blind_start = str(C.BLIND_SEALED_WINDOW[0])
    blind_end = str(C.BLIND_SEALED_WINDOW[1])
    if str(end) >= blind_start:
        raise RuntimeError(
            f"🔴 {what}末日 {end} 触碰单盲封存段（{blind_start} ~ {blind_end}）⇒ 拒绝执行"
        )
    oos_start, oos_end = (str(x) for x in C.WALK_FORWARD_OOS_WINDOW)
    if str(end) > oos_end:
        raise RuntimeError(
            f"🔴 {what}末日 {end} 超出 OOS 窗口（{oos_start} ~ {oos_end}）⇒ 拒绝执行"
        )
    if str(start) < oos_start:
        raise RuntimeError(f"🔴 {what}起点 {start} 早于 OOS 窗口起点 {oos_start} ⇒ 拒绝执行")


def _calendar_dates(provider_uri: str | None = None) -> list[str]:
    """读交易日历（🔴 优先 provider 的 ``calendars/day.txt``，
    回落到 ``config_v3.CALENDAR_FILE``）。
    """
    sp = _load_signal_policy()
    candidates: list[Path] = []
    if provider_uri:
        candidates.append(Path(provider_uri) / "calendars" / "day.txt")
    candidates.append(Path(C.CALENDAR_FILE))
    for path in candidates:
        if path.is_file():
            return [d.isoformat() for d in sp.read_calendar_dates(path)]
    raise FileNotFoundError(f"🔴 找不到交易日历（试过：{[str(c) for c in candidates]}）")


def _calendar_source(provider_uri: str | None = None) -> str:
    """交易日历的来源（``"provider"`` / ``"static"``）—— 披露用（I3）。"""
    if provider_uri:
        path = Path(provider_uri) / "calendars" / "day.txt"
        if path.is_file():
            return "provider"
    return "static"


def _score_grid_dates(calendar: list[str], every_n: int, start: str, end: str) -> list[str]:
    """打分日栅格（🔴 事实源 = ``data/signal_policy.py::score_grid_dates``）。

    🔴 **相位锚点**：栅格的相位取决于传入日历的**起点** ⇒ 必须传**完整日历**（从数据集起点），
       否则相位会漂 ⇒ 与 V1/V2 的栅格对不上。
    🔴 **末日例外**：``score_grid_dates`` 会把日历最后一天无条件追加 ⇒ 先把日历裁到 ``<= end``，
       免得末日例外把**单盲段**里的日期带进打分窗口。
    """
    sp = _load_signal_policy()
    trimmed = [d for d in calendar if str(d) <= str(end)]
    return sp.score_grid_dates(trimmed, int(every_n), start=str(start), end=str(end))


def _chunk_by_month(dates: list[str], chunk_months: int) -> list[list[str]]:
    """按**自然月跨度**把打分日切块（限制 qlib 内存峰值；分块结果与整块逐位相同）。"""
    chunk = max(1, int(chunk_months))
    groups: list[list[str]] = []
    cur: list[str] = []
    for d in dates:
        ts = pd.Timestamp(d)
        if cur and (ts.year * 12 + ts.month) - (pd.Timestamp(cur[0]).year * 12
                                                 + pd.Timestamp(cur[0]).month) >= chunk:
            groups.append(cur)
            cur = []
        cur.append(d)
    if cur:
        groups.append(cur)
    return groups


def _normalize_instruments_file(path: Path) -> bool:
    """把 instruments 文件规范化成 qlib 原生 **3 列** 格式（``symbol \\t start \\t end``）。

    🔴 qlib 的 ``FileInstrumentStorage`` 用 ``usecols=[0, 1, 2]`` 读 ⇒ **只有 1 列的文件会直接崩**。
       返回 ``True`` = 本函数改写过文件（⇒ 记进 manifest 披露，不静默）。
    """
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    start, end = str(C.START_TIME), str(C.WALK_FORWARD_OOS_WINDOW[1])
    out: list[str] = []
    changed = False
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) >= 3:
            out.append(ln)
            continue
        out.append(f"{parts[0].strip()}\t{start}\t{end}")
        changed = True
    if changed:
        # 🔴 **原子写**（并发折级训练时多个进程会同时规范化同一个池文件
        #    ⇒ 非原子写会让另一个进程读到**半截文件**）。
        #    ⇒ 先写同目录临时文件，再 ``os.replace``（POSIX 原子替换）。
        tmp = Path(str(path) + f".tmp.{os.getpid()}")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, Path(path))
    return changed


def _write_instruments_file(provider_uri: str, universe_info: dict) -> tuple[str, Path]:
    """把 V3 自建股票池写进 provider 的 ``instruments/`` 目录（🔴 **新文件名**，不动上游既有文件）。

    🔴 qlib 的 ``instruments`` 参数只认「provider 的 instruments 目录下的**文件名（去扩展名）**」
       ⇒ 这里落 ``v3_etf_pool.txt``，调用方用**市场名** ``v3_etf_pool``。
    🔴 名字**口径中立**（池子口径随 ``config_v3.UNIVERSE_MODE`` 变 ⇒ 名字不该写死 union/large）。
    """
    universe = _load_sibling("universe")
    instruments = [str(x) for x in (universe_info.get("instruments") or [])]
    if not instruments:
        raise RuntimeError("🔴 V3 股票池为空（universe_info['instruments']）")
    inst_dir = Path(provider_uri) / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    path = inst_dir / f"{V3_INSTRUMENTS_MARKET}.txt"
    universe.write_instruments_file(path, instruments)
    _normalize_instruments_file(path)
    return V3_INSTRUMENTS_MARKET, path


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


def _fetch_forward_returns(dates: list[str], universe: str) -> pd.DataFrame:
    """取**原始窗口收益**（列 date, instrument, fwd_ret）—— CSV 的第 4 列。

    🔴 表达式与训练标签**逐字一致**（``config_v3.LABEL_EXPRESSION``）
       ⇒ IC 评估的 y 与训练目标**同一个量纲**；🔴 这里**不中心化**（要的是原始值）。
    """
    from qlib.data import D

    insts = list(D.list_instruments({"market": universe, "filter_pipe": []}, as_list=True))
    if not insts:
        raise RuntimeError(f"🔴 qlib 股票池 {universe!r} 为空")
    df = D.features(insts, [C.LABEL_EXPRESSION], start_time=str(dates[0]),
                    end_time=str(dates[-1]), freq="day")
    df = df.reset_index().rename(columns={"datetime": "date"})
    df = df.rename(columns={c: "fwd_ret" for c in df.columns if c not in ("date", "instrument")})
    df["date"] = pd.to_datetime(df["date"])
    df["instrument"] = df["instrument"].astype(str)
    return df[["date", "instrument", "fwd_ret"]]


def _fetch_turnover(instruments: list[str], start: str, end: str) -> pd.DataFrame:
    """成交额面板（``$close × $volume``，日期 × 标的）⇒ 流动性控制变量。"""
    from qlib.data import D

    df = D.features(instruments, ["$close*$volume"], start_time=str(start),
                    end_time=str(end), freq="day")
    df = df.reset_index().rename(columns={"datetime": "date"})
    value_col = next(c for c in df.columns if c not in ("date", "instrument"))
    return df.pivot(index="date", columns="instrument", values=value_col).sort_index()


def _build_neutralization_panels(
    universe_info: dict, spec, provider_uri: str, enabled: bool,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, dict]:
    """构造中性化用的**规模档面板**与**流动性面板**（🔴 绝不含行业）。"""
    if not enabled:
        return None, None, {"enabled": False, "industry": False}
    universe = _load_sibling("universe")
    neu = _load_sibling("neutralize")
    instruments = [str(x) for x in (universe_info.get("instruments") or [])]
    calendar = _calendar_dates(provider_uri)
    dates = [d for d in calendar if str(spec.train_start) <= d <= str(spec.train_end)]
    tier_panel = universe.size_tier_panel(instruments, dates, files=C.SIZE_TIER_INSTRUMENT_FILES)
    liquidity_panel = None
    turnover = _fetch_turnover(instruments, spec.train_start, spec.train_end)
    if turnover is not None and not turnover.empty:
        liquidity_panel = neu.build_liquidity_control(
            turnover, lookback_days=int(C.LIQUIDITY_LOOKBACK_DAYS)
        )
    return tier_panel, liquidity_panel, {
        "enabled": True,
        "industry": bool(C.INDUSTRY_NEUTRALIZATION_ENABLED),
        "size": bool(C.NEUTRALIZE_SIZE),
        "liquidity": bool(C.NEUTRALIZE_LIQUIDITY),
        "liquidity_lookback_days": int(C.LIQUIDITY_LOOKBACK_DAYS),
        "min_cross_section_size": int(C.MIN_CROSS_SECTION_SIZE),
        "size_proxy": "qlib 指数档位（csi300/csi500/csi1000，带日期区间）——🔴 非流通市值",
    }


def _resolve_universe(universe_info: dict | None) -> dict:
    """股票池口径（🔴 未显式传入 ⇒ 用 ``universe.build_universe`` 自建 ETF 成分股并集）。"""
    if universe_info:
        return dict(universe_info)
    universe = _load_sibling("universe")
    codes = universe.load_etf_universe()
    holdings = universe.load_holdings()
    return dict(universe.build_universe(codes, holdings, mode=None))


def _build_score_neutralization_panels(
    universe_info: dict, score_start: str, score_end: str,
    provider_uri: str | None, enabled: bool,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, dict]:
    """构造**打分窗口**的规模档面板 + 流动性面板（🔴 口径与训练侧一字不差）。

    🔴 **为什么打分侧必须再做一次**：训练侧把特征做了**逐日截面残差化**
       （规模档 + 流动性），打分侧若直接喂**原始特征** ⇒ **train/serve skew**
       ⇒ 模型在残差空间学的，却拿原始空间去预测（只读审计实测 VOL20 截面
       R²≈0.565 ⇒ 约 56% 的截面信息在训练被扣掉、在打分又被放回去）。
    🔴 流动性回看窗口必须在**打分窗口之前**就取够 ⇒ 打分首日的
       ``log_turnover`` 才不是 NaN（``rolling(min_periods=window)`` 凑不满就是 NaN）。
    """
    if not enabled:
        return None, None, {"enabled": False, "industry": False}
    universe = _load_sibling("universe")
    neu = _load_sibling("neutralize")
    instruments = [str(x) for x in (universe_info.get("instruments") or [])]
    calendar = _calendar_dates(provider_uri)
    dates = [d for d in calendar if str(score_start) <= d <= str(score_end)]
    tier_panel = universe.size_tier_panel(instruments, dates, files=C.SIZE_TIER_INSTRUMENT_FILES)
    liquidity_panel = None
    lookback = int(C.LIQUIDITY_LOOKBACK_DAYS)
    warmup = (pd.Timestamp(str(score_start))
              - pd.Timedelta(days=max(lookback * 4, 60))).strftime("%Y-%m-%d")
    turnover = _fetch_turnover(instruments, warmup, str(score_end))
    if turnover is not None and not turnover.empty:
        liquidity_panel = neu.build_liquidity_control(turnover, lookback_days=lookback)
    return tier_panel, liquidity_panel, {
        "enabled": True,
        "industry": bool(C.INDUSTRY_NEUTRALIZATION_ENABLED),
        "size": bool(C.NEUTRALIZE_SIZE),
        "liquidity": bool(C.NEUTRALIZE_LIQUIDITY),
        "liquidity_lookback_days": lookback,
        "min_cross_section_size": int(C.MIN_CROSS_SECTION_SIZE),
        "score_side": True,
        "liquidity_warmup_start": warmup,
    }


# ----------------------------------------------------------------- 打分
def score_universe(
    booster,
    frozen,
    drop: tuple[str, ...],
    universe_name: str,
    out_dir: Path,
    model_name: str,
    score_start: str,
    score_end: str,
    every_n_trading_days: int | None = None,
    chunk_months: int = 12,
    with_fwd: bool = True,
    out_path: Path | None = None,
    provider_uri: str | None = None,
    score_dates: list[str] | None = None,
    neutralize: bool | None = None,
    tier_panel: pd.DataFrame | None = None,
    liquidity_panel: pd.DataFrame | None = None,
    pit_membership: pd.DataFrame | None = None,
) -> dict:
    """在**打分日栅格**上打分并写 CSV（🔴 分块执行以限制 qlib 内存峰值）。

    🔴 分块安全性：Alpha158 的特征**全是回看型**（最长 60 日），归一化统计量**冻结**自训练期
       ⇒ 每个分块的特征与整块**逐位相同** ⇒ 分块结果可拼接。

    🔴 **显式打分日**（``score_dates``）：栅格的**相位**取决于「传给
       ``signal_policy.score_grid_dates`` 的日历切片起点」⇒ V2 走前推链、V3 走
       本包自带日历 ⇒ 两侧栅格**差 1~2 个交易日**、**交集为空**（实测）
       ⇒ 对照实验里「信号日不同」会污染端到端指标（调仓日历都不一样）。
       ⇒ 本参数让调用方**显式指定**打分日（对照臂用 V2 的那批信号日）
       ⇒ **两臂在完全相同的信号日上比较**（口径由调用方保证，**这里只做边界校验**）。

    🔴 **打分侧中性化**（train/serve skew 的修复点）：训练侧把特征做了**逐日截面
       残差化**（规模档 + 流动性）⇒ 打分侧**必须**用**同口径**再残差化一次，
       否则模型在残差空间训练、却拿原始特征预测。
       * ``neutralize=False`` ⇒ 两边都不残差化（消融臂 ``v3_no_neutralize``）；
       * ``neutralize=True`` ⇒ 必须给 ``tier_panel``（缺 ⇒ 直接 raise，不许静默退化）。
    """
    col_set, data_key = _qlib_fetch_kwargs()
    every_n = int(C.SCORE_GRID_EVERY_N_TRADING_DAYS
                  if every_n_trading_days is None else every_n_trading_days)
    if score_dates:
        dates = sorted({str(d)[:10] for d in score_dates})
        out_of_range = [d for d in dates if not (str(score_start) <= d <= str(score_end))]
        if out_of_range:
            raise ValueError(
                f"🔴 显式打分日越界：{out_of_range[:5]}（打分窗口 {score_start}~{score_end}）"
            )
        if not dates:
            raise RuntimeError("🔴 显式打分日为空")
        print(f"[INFO] 打分日 {len(dates)} 个（🔴 显式指定，与对照臂同一批信号日）："
              f"{dates[0]} ~ {dates[-1]}")
    else:
        calendar = _calendar_dates(provider_uri)
        dates = _score_grid_dates(calendar, every_n, score_start, score_end)
        if not dates:
            raise RuntimeError(f"🔴 打分窗口 {score_start}~{score_end} 内没有打分日")
        print(f"[INFO] 打分日 {len(dates)} 个：{dates[0]} ~ {dates[-1]}（步长 {every_n} 交易日）")

    # ---- 🔴 打分侧中性化：与训练侧**同口径**（规模档 + 流动性，绝不行业） ----
    neutralize_enabled = bool(C.NEUTRALIZATION_ENABLED if neutralize is None else neutralize)
    neu = None
    controls = None
    score_neutral_meta: dict = {"enabled": False, "industry": False}
    if neutralize_enabled:
        neu = _load_sibling("neutralize")
        neu.assert_no_industry_neutralization()
        if tier_panel is None or tier_panel.empty:
            raise RuntimeError(
                "🔴 打分侧中性化已开启但 tier_panel 为空 ⇒ 拒绝继续"
                "（训练侧做了残差化、打分侧没做 ⇒ train/serve skew）"
            )
        controls = neu.build_controls(
            tier_panel,
            liquidity_panel=liquidity_panel,
            size=bool(C.NEUTRALIZE_SIZE),
            liquidity=bool(C.NEUTRALIZE_LIQUIDITY),
        )
        score_neutral_meta = {
            "enabled": True,
            "industry": False,
            "n_controls_total": int(controls.shape[1]) if controls is not None else 0,
            "min_cross_section_size": int(C.MIN_CROSS_SECTION_SIZE),
            # 🔴 **拟合子集口径**（B1）：默认臂无 PIT 面板
            #    ⇒ 拟合子集＝变换全集（旧口径）
            "fit_subset": ("当日 PIT 成员（拟合子集）／打分池全集（变换全集）"
                           if pit_membership is not None
                           else "打分池全集（拟合子集＝变换全集）"),
        }

    frames: list[pd.DataFrame] = []
    n_dropped_no_controls = 0
    agg = {"n_dates_processed": 0, "n_dates_skipped": 0, "n_dates_no_controls": 0,
           "n_samples_total": 0}
    for gi, group in enumerate(_chunk_by_month(dates, chunk_months), 1):
        warmup = (pd.Timestamp(group[0])
                  - pd.Timedelta(days=C.SCORE_WARMUP_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        handler = build_score_handler(universe_name, drop, frozen, warmup, group[-1])
        frame = handler.fetch(
            selector=slice(warmup, group[-1]),
            level="datetime",
            col_set=col_set,
            data_key=data_key,
        )
        long = _normalize_long_frame(frame)
        long = long.sort_values(["date", "instrument"]).reset_index(drop=True)
        feature_cols = [c for c in long.columns if c not in ("date", "instrument", "LABEL0")]
        # 🔴 打分侧只保留**打分日**的行（warmup 段只是特征回看用，不参与打分/中性化）
        group_ts = {pd.Timestamp(d) for d in group}
        sc = long.loc[long["date"].isin(group_ts)].reset_index(drop=True)
        if sc.empty:
            raise RuntimeError(f"🔴 分块 {group[0]} ~ {group[-1]} 在打分日上无数据")
        if neutralize_enabled:
            X_panel, date_pos, inst_pos, d_axis, i_axis = _long_to_panel(sc, feature_cols)
            # 🔴🔴 **B1 的修复点**：拟合子集＝当日 PIT 成员，**变换全集＝打分池**
            #    ⇒ 与训练侧**系数同源**（无 train/serve skew），
            #    **同时**打分池一行都不少（掩码只当拟合掩码，不当行过滤器）。
            fit_mask = _pit_fit_mask(pit_membership, d_axis, i_axis, "打分侧")
            resid, stats = neu.cross_sectional_residualize_panel(
                X_panel, d_axis, i_axis, controls, min_size=int(C.MIN_CROSS_SECTION_SIZE),
                fit_mask=fit_mask,
            )
            X_rows = resid[date_pos, inst_pos, :]
            for key in agg:
                agg[key] += int(stats.get(key, 0))
            # 🔴 与训练侧**同一规则**：控制变量缺测的行（残差整行 NaN）在训练时被剔除
            #    ⇒ 打分侧同样剔除（否则就是在模型没见过的特征分布上打分）
            n_dropped_no_controls += int((~np.isfinite(X_rows).any(axis=1)).sum())
        else:
            X_rows = sc[feature_cols].to_numpy(dtype=float)
        for d in group:
            sel = (sc["date"] == pd.Timestamp(d)).to_numpy()
            if not sel.any():
                raise RuntimeError(f"🔴 {d} 打分池无数据（股票池={universe_name!r}）")
            inst_day = sc.loc[sel, "instrument"].to_numpy()
            X_day = X_rows[sel]
            if neutralize_enabled:
                keep = _usable_rows(X_day)
                drop_ratio = 1.0 - float(keep.mean()) if keep.size else 1.0
                if drop_ratio > 0.2:
                    raise RuntimeError(
                        f"🔴 {d} 打分侧有 {drop_ratio:.1%} 的行没有可用残差"
                        "（控制变量缺测或特征全缺）⇒ 拒绝产出（多半是打分侧面板没建对）"
                    )
                X_day = X_day[keep]
                inst_day = inst_day[keep]
            if X_day.shape[0] == 0:
                raise RuntimeError(f"🔴 {d} 打分池在中性化后为空 ⇒ 拒绝产出假分数")
            scores = V3_MODEL.predict_scores(booster, X_day)
            out = pd.DataFrame({
                "date": pd.Timestamp(d),
                "instrument": inst_day,
                "score": scores,
            })
            _assert_non_degenerate(out, d)
            frames.append(out)
        print(f"[{gi}] 分块 {group[0]} ~ {group[-1]} 打分完成")
        del handler, frame, long, sc

    scores = pd.concat(frames, ignore_index=True)
    scores = scores.sort_values(["date", "instrument"]).reset_index(drop=True)

    if with_fwd:
        fwd = _fetch_forward_returns(dates, universe_name)
        scores = scores.merge(fwd, on=["date", "instrument"], how="left")
        n_ok = int(scores["fwd_ret"].notna().sum())
        print(f"[INFO] 前瞻收益合并完成：{n_ok}/{len(scores)} 行有效")
    else:
        scores["fwd_ret"] = np.nan

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(out_path) if out_path is not None else out_dir / C.scores_filename(model_name)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    scores.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[OK] 分数已写出：{csv_path}（{len(scores)} 行，{scores['date'].nunique()} 个交易日）")

    return {
        "scores_path": str(csv_path),
        "scores_md5": md5_of(csv_path),
        "n_rows": len(scores),
        "n_score_dates": int(scores["date"].nunique()),
        "score_date_range": [str(scores["date"].min().date()), str(scores["date"].max().date())],
        "score_grid_every_n_trading_days": every_n,
        "chunk_months": int(chunk_months),
        "score_universe": universe_name,
        "neutralize": {**score_neutral_meta, **agg,
                       "n_rows_dropped_no_controls": int(n_dropped_no_controls)},
    }


# ----------------------------------------------------------------- 打分池守卫
def _assert_score_pool_covers_etf_union(info: dict, score_meta: dict,
                                        context: str = "") -> dict:
    """🔴 **打分池覆盖守卫**（只在「训练侧 PIT 掩码生效」时执行 ⇒ 默认路径零开销）。

    **为什么必须有这条**：实测若**打分池**也按日 PIT，ETF 权重覆盖率中位数会从
    **0.84480** 掉到 **0.64092**（整段 OOS 实测）⇒ **打分池必须保持静态口径**
    ⇒ 这条断言就是「防止将来有人把 PIT 掩码误加到打分侧」的**牙齿**
    （训练侧筛、打分侧不筛）。

    🔴 **覆盖率口径**（与整段 OOS 实测同口径 ⇒ 阈值可追溯）

      * 分子 = 当日**有分**的标的的 ΣW；分母 = **静态打分池**（ETF 并集 ∩
        中证800∪中证1000 的 2635 只）的 ΣW（= ``info["sum_w"]``）；
      * 🔴 **阈值来自整段 OOS 实测**（6 臂 × 701 个（臂, 信号日）观测）：
        静态池 median 0.84480 / min 0.63183（最差日 2019-04-11）、
        逐窗口中位数 ∈ [0.82708, 0.86685]；
        「打分侧被误加 PIT 掩码」的变异值逐窗口中位数 ≤ **0.69154**
        ⇒ 分离带 (0.69154, 0.82708] ⇒ 阈值取中点 **0.75931**。

    三条判定（任一不满足 ⇒ 直接 raise，**宁可不产出**）：

      ① **逐日底线**（永远生效）：每个信号日的覆盖率 ≥
         ``config_v3.SCORE_POOL_COVERAGE_MIN_DAY``（＝实测最小值 − 容差）；
      ② **中位数**（**只在信号日数 ≥**
         ``config_v3.SCORE_POOL_MIN_SIGNAL_DAYS_FOR_MEDIAN`` 时才用）：
         滚动路径每窗口常常只有几个信号日 ⇒ 中位数一刀切不可靠
         ⇒ 低于该阈值时改为「结构性检查为主」；
      ③ **不是 PIT 子集**（永远生效，**结构性检查**）：打分侧每日标的数**从未超过**
         PIT 当日成员数（若掩码被误加到打分侧 ⇒ 每日标的数会塌到 PIT 当日成员数
         ⇒ 转红）。

    🔴 **失败信息必须带**：最差日期 ＋ 信号日数 ＋ 折号/窗口（``context``）。
    """
    sum_w = {str(k): float(v) for k, v in (info.get("sum_w") or {}).items()}
    total_weight = float(sum(sum_w.values()))
    if total_weight <= 0:
        raise RuntimeError("🔴 训练池的 ΣW 合计为 0 ⇒ 打分池覆盖守卫无法判定")
    scores = pd.read_csv(
        score_meta["scores_path"], dtype={"instrument": str},
        usecols=["date", "instrument"], encoding="utf-8-sig",
    )
    panel = _pit_membership_panel(info)
    coverages: list[float] = []
    worst_date = ""
    worst_cov = 1.0
    n_dates = 0
    n_dates_compared = 0
    n_dates_beyond_pit = 0
    for date, block in scores.groupby("date", sort=True):
        scored = {str(x) for x in block["instrument"].tolist()}
        covered = float(sum(w for key, w in sum_w.items() if key in scored))
        coverage = covered / total_weight
        coverages.append(coverage)
        n_dates += 1
        if coverage < worst_cov:
            worst_cov = coverage
            worst_date = str(pd.Timestamp(date).date())
        if panel is not None and not panel.empty:
            stamp = pd.Timestamp(date)
            if stamp in panel.index:
                n_dates_compared += 1
                n_members = int(panel.loc[stamp].to_numpy().sum())
                if len(scored) > n_members:
                    n_dates_beyond_pit += 1
    if not coverages:
        raise RuntimeError("🔴 打分结果为空 ⇒ 无法执行打分池覆盖守卫")
    mean_cov = float(np.mean(coverages))
    median_cov = float(np.median(coverages))
    min_cov = float(np.min(coverages))
    ctx = f"（{context}）" if context else ""
    day_floor = float(C.SCORE_POOL_COVERAGE_MIN_DAY)
    median_floor = float(C.SCORE_POOL_COVERAGE_MIN_MEDIAN)
    min_signal_days = int(C.SCORE_POOL_MIN_SIGNAL_DAYS_FOR_MEDIAN)
    used_median = n_dates >= min_signal_days
    # ---- ① 逐日底线（永远生效）----
    if min_cov < day_floor:
        raise RuntimeError(
            f"🔴 打分池覆盖塌陷{ctx}：最差日 {worst_date} 的 "
            f"ETF 权重覆盖率 {min_cov:.4f}"
            f" < 逐日底线 {day_floor:.4f}（均值 {mean_cov:.4f}、"
            f"中位数 {median_cov:.4f}、信号日数 {n_dates}）"
            f"⇒ 打分池疑似被施加了 PIT 掩码 ⇒ 拒绝产出"
            "（打分池必须保持静态口径：训练侧筛、打分侧不筛）"
        )
    # ---- ② 中位数（信号日数够多才用；否则以结构性检查为主）----
    if used_median and median_cov < median_floor:
        raise RuntimeError(
            f"🔴 打分池覆盖不足{ctx}：ETF 权重覆盖率中位数 {median_cov:.4f}"
            f" < 阈值 {median_floor:.4f}（均值 {mean_cov:.4f}、最小 {min_cov:.4f}、"
            f"最差日 {worst_date}、信号日数 {n_dates}）"
            "⇒ 打分池疑似被施加了 PIT 掩码 ⇒ 拒绝产出"
            "（打分池必须保持静态口径：训练侧筛、打分侧不筛）"
        )
    # ---- ③ 结构性检查（永远生效）----
    if n_dates_compared > 0 and n_dates_beyond_pit <= 0:
        raise RuntimeError(
            f"🔴 打分侧的每日标的数从未超过 PIT 当日成员数{ctx}"
            f"（可比日期 {n_dates_compared} 个、信号日数 {n_dates}）"
            "⇒ 疑似打分池被施加了 PIT 掩码 ⇒ 拒绝产出"
            "（打分池必须保持静态口径：训练侧筛、打分侧不筛）"
        )
    return {
        "etf_union_weight_coverage_mean": mean_cov,
        "etf_union_weight_coverage_median": median_cov,
        "etf_union_weight_coverage_min": min_cov,
        "coverage_worst_date": worst_date,
        "coverage_threshold_median": median_floor,
        "coverage_threshold_day": day_floor,
        "coverage_median_check_applied": bool(used_median),
        "coverage_median_check_skipped_reason": (
            "" if used_median
            else f"信号日数 {n_dates} < {min_signal_days} ⇒ 不用中位数一刀切，"
            f"改用结构性检查"
        ),
        "coverage_measurement_note": str(C.SCORE_POOL_COVERAGE_MEASUREMENT_NOTE),
        "n_score_dates": int(n_dates),
        "n_dates_compared_with_pit_panel": int(n_dates_compared),
        "n_dates_scoring_beyond_pit_members": int(n_dates_beyond_pit),
        "context": context,
    }


# ----------------------------------------------------------------- 端到端
def _manifest_universe_and_caliber(
    info: dict,
    universe_name: str,
    instruments_path: Path,
    listed: list[str],
    pit_stats: dict,
    score_meta: dict,
    pit_calendar_meta: dict,
) -> tuple[dict, dict]:
    """manifest 的 ``universe`` 段 **与** 口径卡（**同源产出** ⇒ B2 结构性消解）。

    🔴 **B2 的根因**：口径卡里的 ``universe.mode`` 恒为静态口径，而 manifest 顶层
       ``universe.mode`` 是本次口径 ⇒ 同一份产物里出现**两个互相矛盾**的口径。
    🔴 **修法**：两处都从**同一个** ``mode`` 变量出发
       ⇒ 必然相等（不是靠约定，是靠结构）。

    返回 ``(universe_section, caliber_card)``。
    """
    mode = str(info.get("mode") or C.UNIVERSE_MODE)
    universe_section = {
        "market": universe_name,
        "instruments_file": str(instruments_path),
        "n_stocks": int(info.get("n_stocks") or len(listed)),
        "mode": mode,
        "sample_weight_mode": str(C.SAMPLE_WEIGHT_MODE),
        "score_universe": score_meta["score_universe"],
        # 🔴 **逐日 PIT 训练池**（默认口径下全为 False/0 ⇒ 默认 manifest 语义不变）
        "pit_mode": bool(pit_stats.get("enabled")),
        "pit_rows_before_mask": int(pit_stats.get("n_rows_before") or 0),
        "pit_rows_after_mask": int(pit_stats.get("n_rows_after") or 0),
        "pit_rows_dropped": int(pit_stats.get("n_rows_dropped") or 0),
        "pit_daily_members": {
            "min": int(pit_stats.get("members_min") or 0),
            "median": float(pit_stats.get("members_median") or 0.0),
            "max": int(pit_stats.get("members_max") or 0),
        },
        "pit_note": "🔴 PIT 掩码**只作用在训练样本**；打分池保持静态口径"
                    "（不按日筛，见 `score_pool_guard`）",
        "pit_rows_unknown_instrument": int(
            pit_stats.get("n_rows_unknown_instrument") or 0
        ),
        "pit_calendar": pit_calendar_meta,
    }
    return universe_section, C.v3_caliber_card(mode)


def train_and_score(
    spec,
    out_dir: Path,
    model_name: str,
    arm: str = "v3",
    provider_uri: str | None = None,
    universe_info: dict | None = None,
    neutralize: bool | None = None,
    center: str | None = None,
    score_start: str | None = None,
    score_end: str | None = None,
    rolling: dict | None = None,
    score_dates: list[str] | None = None,
) -> dict:
    """端到端：训练（``arm='v3'`` ⇒ lambdarank；``arm='control'`` ⇒ MSE）
    ⇒ 打分 ⇒ 写产物 + manifest。

    🔴 **单盲守卫**：``score_end`` 一旦触碰 ``config_v3.BLIND_SEALED_WINDOW`` ⇒ 直接 raise。
    🔴 **产物落点**：分数文件 = ``<out_dir>/stock_scores_v3_<model_name>.csv``
       （**不叫** stock_scores.csv）。
    """
    if arm not in ("v3", "control"):
        raise ValueError(f"🔴 未知的实验臂：{arm!r}（合法 = 'v3' / 'control'）")
    provider_uri = str(provider_uri or C.DEFAULT_PROVIDER_URI)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    score_start = str(score_start or spec.valid_start)
    score_end = str(score_end or spec.valid_end)
    _assert_not_blind(score_start, score_end, "打分窗口")
    if str(spec.train_end) >= str(C.BLIND_SEALED_WINDOW[0]):
        raise RuntimeError(
            f"🔴 训练段末日 {spec.train_end} 触碰单盲封存段 ⇒ 拒绝执行"
        )

    t0 = time.time()
    init_qlib(provider_uri)

    info = _resolve_universe(universe_info)
    universe_name, instruments_path = _write_instruments_file(provider_uri, info)
    from qlib.data import D

    listed = list(D.list_instruments({"market": universe_name, "filter_pipe": []}, as_list=True))
    if not listed:
        raise RuntimeError(
            f"🔴 qlib 解析不到自建股票池 {universe_name!r}（{instruments_path}）⇒ 拒绝继续"
        )
    print(f"[INFO] 训练池 = {universe_name}（{len(listed)} 只；{instruments_path}）")

    neutralize_enabled = bool(C.NEUTRALIZATION_ENABLED if neutralize is None else neutralize)
    tier_panel, liquidity_panel, neutral_meta = _build_neutralization_panels(
        info, spec, provider_uri, neutralize_enabled
    )
    # 🔴 **打分侧同口径面板**（train/serve skew 的修复点）：训练侧残差化 ⇒ 打分侧必须残差化
    score_tier_panel, score_liquidity_panel, score_neutral_meta = (
        _build_score_neutralization_panels(
            info, score_start, score_end, provider_uri, neutralize_enabled
        )
    )

    drop = tuple(C.DROPPED_FEATURES)
    handler = build_train_handler(universe_name, drop, spec.fit_end, spec.train_start)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        handler.fit()
    frozen = extract_frozen_norm(handler)
    handler.fit_process_data()
    print(f"[INFO] 训练段 {spec.train_start} ~ {spec.train_end}；特征列 {len(frozen.cols)}")

    frame = prepare_training_frame(
        handler, spec, info, tier_panel=tier_panel, liquidity_panel=liquidity_panel,
        neutralize=neutralize_enabled, center=center,
    )
    pit_stats = dict(frame.get("pit") or {})
    # 🔴 **I3**：PIT 面板的日期轴 vs 训练帧的日期轴 ⇒ 差异条数**写进 manifest**
    #    （面板日期轴必须来自**交易日历**（provider 优先），不是自然日）。
    pit_calendar_meta: dict = {
        "enabled": bool(pit_stats.get("enabled")),
        "source": "provider 优先、静态兜底（与 train_s._calendar_dates 同源）",
        "n_panel_dates": 0,
        "n_dates_panel_not_in_train_axis": 0,
        "n_dates_train_axis_not_in_panel": 0,
        "n_panel_dates_not_trading_days": 0,
    }
    if pit_stats.get("enabled"):
        pit_panel_obj = info.get(C.PIT_MEMBERSHIP_KEY)
        panel_dates = pd.DatetimeIndex(pd.to_datetime(pit_panel_obj.index))
        train_dates = pd.DatetimeIndex(pd.to_datetime(frame["unique_dates"]))
        trading = {str(d)[:10] for d in _calendar_dates(provider_uri)}
        panel_date_strs = {str(d)[:10] for d in panel_dates}
        pit_calendar_meta.update({
            "source": f"{_calendar_source(provider_uri)}（provider 优先、静态兜底）",
            "n_panel_dates": len(panel_dates),
            "n_dates_panel_not_in_train_axis": len(
                panel_dates.difference(train_dates)
            ),
            "n_dates_train_axis_not_in_panel": len(
                train_dates.difference(panel_dates)
            ),
            "n_panel_dates_not_trading_days": len(panel_date_strs - trading),
            "panel_start": str(panel_dates.min().date()) if len(panel_dates) else "",
            "panel_end": str(panel_dates.max().date()) if len(panel_dates) else "",
            "train_axis_start": (str(train_dates.min().date())
                                 if len(train_dates) else ""),
            "train_axis_end": str(train_dates.max().date()) if len(train_dates) else "",
        })
    n_samples = len(frame["y"])
    n_features = int(frame["X"].shape[1])
    print(f"[INFO] 训练矩阵：{n_samples} 行 × {n_features} 特征，{len(frame['groups'])} 个交易日组")

    if arm == "v3":
        objective = str(C.MODEL_OBJECTIVE)
        booster = V3_MODEL.train_ranker(
            frame["X"], frame["grades"], frame["groups"], weights=frame["weights"],
            params=dict(C.LGB_PARAMS),
        )
    else:
        objective = str(C.CONTROL_OBJECTIVE)
        booster = V3_MODEL.train_regressor(
            frame["X"], frame["y"], weights=frame["weights"],
            params=dict(C.MSE_ARM_LGB_PARAMS),
        )

    model_path = out_dir / f"model_v3_{model_name}.pkl"
    with model_path.open("wb") as fh:
        pickle.dump(booster, fh)
    frozen_path = out_dir / f"frozen_norm_v3_{model_name}.pkl"
    with frozen_path.open("wb") as fh:
        pickle.dump({
            "mean_train": frozen.mean_train,
            "std_train": frozen.std_train,
            "ignore": frozen.ignore,
            "cols": frozen.cols,
            "fit_start_time": frozen.fit_start_time,
            "fit_end_time": frozen.fit_end_time,
            "fields_group": frozen.fields_group,
        }, fh)

    # 🔴 **打分侧的 PIT 成员面板**（B1）：**只作拟合掩码**（拟合子集＝当日 PIT 成员）
    #    ⇒ 打分池一行都不少（不按日筛）⇒ ETF 权重有分可映射；
    #    ⇒ 同时两侧**系数同源** ⇒ 无 train/serve skew。
    pit_panel = info.get(C.PIT_MEMBERSHIP_KEY) if pit_stats.get("enabled") else None

    score_meta = score_universe(
        booster, frozen, drop, universe_name, out_dir, model_name,
        score_start, score_end, provider_uri=provider_uri,
        score_dates=score_dates,
        neutralize=neutralize_enabled,
        tier_panel=score_tier_panel,
        liquidity_panel=score_liquidity_panel,
        pit_membership=pit_panel,
    )

    # 🔴 **打分池覆盖守卫**（只在训练侧 PIT 掩码生效时执行 ⇒ 默认路径零开销）
    score_pool_guard = None
    if pit_stats.get("enabled"):
        fold_id = getattr(spec, "fold_id", None)
        window_id = (rolling or {}).get("window_id")
        if fold_id is not None:
            ctx = f"折 {int(fold_id)}｜打分窗口 {score_start}~{score_end}"
        elif window_id is not None:
            ctx = (f"窗口 {int(window_id)}｜折 {getattr(spec, 'fold_id', None)}"
                   f"｜打分窗口 {score_start}~{score_end}")
        else:
            ctx = f"打分窗口 {score_start}~{score_end}"
        score_pool_guard = _assert_score_pool_covers_etf_union(
            info, score_meta, context=ctx
        )

    # 🔴 **B2**：``universe`` 段与口径卡**同源产出** ⇒ 两处 ``universe.mode`` 必然相等
    manifest_universe, manifest_caliber = _manifest_universe_and_caliber(
        info,
        universe_name=universe_name,
        instruments_path=instruments_path,
        listed=list(listed),
        pit_stats=pit_stats,
        score_meta=score_meta,
        pit_calendar_meta=pit_calendar_meta,
    )

    manifest = {
        "model_name": model_name,
        "arm": arm,
        "objective": objective,
        "model_version_tag": C.V3_MODEL_VERSION_TAG,
        "trained_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "host": platform.node(),
        "python": platform.python_version(),
        "provider_uri": provider_uri,
        "train_segment": [spec.train_start, spec.train_end],
        "train_end_raw": getattr(spec, "train_end_raw", None),
        "fit_window": [spec.fit_start, spec.fit_end],
        "embargo_days": getattr(spec, "embargo_days", None),
        "score_window": [score_start, score_end],
        "score_dates_source": ("explicit（🔴 与对照臂同一批信号日）" if score_dates
                               else "signal_policy.score_grid_dates（栅格相位）"),
        "n_score_dates_explicit": (len(score_dates) if score_dates else 0),
        "n_samples": n_samples,
        "n_features": n_features,
        "n_groups": len(frame["groups"]),
        "n_dates": len(frame["dates"]),
        "dropped_features": list(drop),
        "label_expression": str(C.LABEL_EXPRESSION),
        "label_centering": frame["center"],
        "label_centering_note": (
            "🔴 逐日中心化在排序/IC 目标下是**不变量**（组内平移不改变次序）；"
            "真实作用在 ① MSE 对照臂 ② 评估层超额口径 ③ 「跑赢大盘」的训练语义"
        ),
        "label_learn_processors": [dict(p) for p in C.LABEL_LEARN_PROCESSORS],
        "label_infer_processors": [dict(p) for p in C.LABEL_INFER_PROCESSORS],
        "rank_grade_count": int(C.RANK_GRADE_COUNT),
        "num_boost_round": int(C.NUM_BOOST_ROUND),
        "early_stopping_rounds": C.EARLY_STOPPING_ROUNDS,
        "early_stopping": "🔴 未启用（config_v3.EARLY_STOPPING_ROUNDS=None；"
                          "早停会把 train+valid 混进 valid_sets ⇒ 隐性泄漏）",
        "lgb_params": dict(C.LGB_PARAMS if arm == "v3" else C.MSE_ARM_LGB_PARAMS),
        "single_variable_note": (
            "🔴 两臂超参**逐项同源**（只差 objective/metric）⇒ 差异可归因到损失函数"
            if arm == "control" else
            "🔴 主臂：lambdarank（标签 = 整数 relevance grade，5 档）"
        ),
        "universe": manifest_universe,
        "score_pool_guard": score_pool_guard,
        "neutralize": {**neutral_meta, **frame["neutralize"]},
        "neutralize_score": {
            **score_neutral_meta, **score_meta["neutralize"],
            "note": (
                "🔴 打分侧与训练侧**同口径**逐日截面残差化"
                "（规模档 + 流动性；绝不行业中性化）"
                "⇒ 无 train/serve skew"
                + (
                    "；🔴 PIT 臂**两侧同口径**：拟合子集＝当日 PIT 成员，"
                    "变换全集＝打分池（打分池**不按日筛** ⇒ ETF 权重有分可映射）"
                    if pit_stats.get("enabled")
                    else "（拟合子集＝变换全集＝打分池）"
                )
            ),
        },
        "rolling": rolling,
        "blind_sealed_window": list(C.BLIND_SEALED_WINDOW),
        "blind_guard": "🔴 打分窗口与训练段均已做单盲守卫（触碰 2024+ 即 raise）",
        "outputs": {
            "stock_scores_csv": score_meta["scores_path"],
            "model_path": str(model_path),
            "frozen_norm_path": str(frozen_path),
        },
        "model_md5": md5_of(model_path),
        "frozen_norm_md5": md5_of(frozen_path),
        "scores_md5": score_meta["scores_md5"],
        "n_score_dates": score_meta["n_score_dates"],
        "n_score_rows": score_meta["n_rows"],
        "score_grid_every_n_trading_days": score_meta["score_grid_every_n_trading_days"],
        "score_warmup_calendar_days": int(C.SCORE_WARMUP_CALENDAR_DAYS),
        "local_constants_note": (
            "🔴 config_v3 暂无「打分栅格步长 / 特征回看 warmup」常量 ⇒ 本文件本地定义 "
            f"C.SCORE_GRID_EVERY_N_TRADING_DAYS={C.SCORE_GRID_EVERY_N_TRADING_DAYS} / "
            f"C.SCORE_WARMUP_CALENDAR_DAYS={C.SCORE_WARMUP_CALENDAR_DAYS}"
        ),
        # 🔴 **B2**：口径卡与 manifest 顶层 ``universe.mode`` **同源产出** ⇒ 必然相等
        "caliber_card": manifest_caliber,
        "script_md5": md5_of(Path(__file__).resolve()),
    }
    manifest_path = out_dir / f"train_manifest_v3_{model_name}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    print(f"[OK] manifest 已写出：{manifest_path}")
    return manifest


# ----------------------------------------------------------------- main
def parse_args() -> argparse.Namespace:
    """命令行参数（V3 S 层的全部开关）。"""
    p = argparse.ArgumentParser(description="V3 S 层：训练 + 打分（研发分支）")
    p.add_argument("--out-dir", default=str(C.DEFAULT_V3_RUN_ROOT))
    p.add_argument("--provider-uri", default=C.DEFAULT_PROVIDER_URI)
    p.add_argument("--model-name", default="v3_rank")
    p.add_argument("--arm", choices=["v3", "control"], default="v3")
    p.add_argument("--score-start", default=None, help="打分窗口起点（含；默认 = 验证段起点）")
    p.add_argument("--score-end", default=None, help="打分窗口终点（含；默认 = 验证段终点）")
    return p.parse_args()


def main() -> int:
    """V3 S 层脚本入口（训练 + 打分一次）。"""
    args = parse_args()
    splits = _load_sibling("splits")
    spec = splits.split()
    t0 = time.time()
    train_and_score(
        spec,
        out_dir=Path(args.out_dir),
        model_name=args.model_name,
        arm=args.arm,
        provider_uri=args.provider_uri,
        score_start=args.score_start,
        score_end=args.score_end,
    )
    print(f"[INFO] 全流程结束，用时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
