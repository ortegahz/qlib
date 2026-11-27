"""🔴 V3 截面中性化（对齐③·**规模 + 流动性**，🔴 **绝不行业中性化**）。

**V3 与 V2 的最大差别之一**：V2 只做时序 ZScoreNorm，**没有任何截面中性化**；
V3 对特征 / 标签做**截面 OLS 残差化**，把「规模档」与「流动性」这两个截面变量扣掉。

🔴 三条硬红线

  ① **绝不行业中性化**（用户 2026-11-26 目标文档明令禁止）：
     行业内去均值 ⇒ 行业内个股分数均值为 0 ⇒ 下游 ``E = W × S`` 全部塌缩到 0 附近
     ⇒ 直接抹杀行业轮动所需的板块景气度差异。
     :func:`assert_no_industry_neutralization` 是这条红线的**机器化 teeth**
     （``config_v3.INDUSTRY_NEUTRALIZATION_ENABLED`` 一旦是 True 立刻 raise）。
  ② **逐日独立** ⇒ 截面回归**绝不能跨日混样本**（跨日会把「时间趋势」当截面信号扣掉）。
  ③ **规模 = 指数档位代理**（不是流通市值）：qlib 的 ``instruments/csi*.txt`` 带日期区间
     ⇒ 规模档是**时变**的（见 :mod:`manu.etf_rotation.v3.universe`）。

🔴 残差化口径

  * 设计矩阵：``values[t] ~ 1 + controls[t]``（**含截距**）⇒ 残差与控制变量**截面正交**；
  * 某日**有效样本数 < min_size** ⇒ **跳过该日**（残差保留原值）并如实计数；
  * **全 NaN / 常数列自动丢弃**（秩 0 列会让最小二乘不稳）；
  * ``values`` 里的 NaN **保持 NaN**、**不参与回归**（标准缺失语义，不填 0）。

🔴 **两条等价路径**（口径同源，事实源 = :func:`cross_sectional_residualize`）

  * :func:`cross_sectional_residualize`：**单特征**逐日残差化（**事实源**，供对拍）；
  * :func:`cross_sectional_residualize_panel`：**多特征面板**单遍批量残差化
    （按日期对齐 controls 只做一次 + 按有效行掩码分组的批量 lstsq）
    ⇒ 与事实源**逐元素等价**（见 ``tests/test_v3_neutralize_guard.py`` 的对拍测试）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:  # 扁平导入（v3 目录进 sys.path，与 v2 同口径）
    import config_v3 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v3 as C

try:  # 规模档哑变量在 universe.py（🔴 V3 内部依赖，**不碰 V2**）
    from .universe import size_tier_dummies
except ImportError:  # pragma: no cover - 扁平导入路径
    from universe import size_tier_dummies


#: 流动性控制变量的列名（长表里的控制变量名）
LIQUIDITY_CONTROL_COLUMN = "log_turnover"


# ----------------------------------------------------------------- 红线守卫
def assert_no_industry_neutralization() -> None:
    """🔴 断言 ``config_v3.INDUSTRY_NEUTRALIZATION_ENABLED`` 是 False ⇒ 否则 raise。

    这是「**绝不行业中性化**」的机器化 teeth：只要有人把那个常量打开，
    任何调用 V3 中性化链路的入口都会**立刻转红**（而不是悄悄把行业信号抹平）。

    🔴 读的是**调用时**的常量值 ⇒ 守卫测试可以用 monkeypatch 把它改成 True 做变异反证。
    """
    if bool(C.INDUSTRY_NEUTRALIZATION_ENABLED):
        raise AssertionError(
            "🔴 行业中性化被打开：config_v3.INDUSTRY_NEUTRALIZATION_ENABLED = True ⇒ "
            "行业内去均值会让下游 E = W × S 塌缩到 0 附近 ⇒ V3 口径**禁止**行业中性化"
        )


# ----------------------------------------------------------------- 控制变量
def build_liquidity_control(
    turnover: pd.DataFrame,
    lookback_days: int = C.LIQUIDITY_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """成交额面板 ⇒ ``log(过去 N 日成交额均值)``（同形状）。

    * ``turnover``：index = 日期、columns = 标的、值 = **成交额**（不是收益率）；
    * 🔴 回看窗口**必须凑满 N 个交易日**（``min_periods=N``）⇒ 样本头部是 NaN
      （不许用半截窗口凑数）；
    * 成交额 ≤ 0 ⇒ NaN（``log`` 无定义，不填 0）。
    """
    window = int(lookback_days)
    if window < 1:
        raise ValueError(f"🔴 流动性回看窗口必须 ≥ 1（{window}）")
    if turnover is None or turnover.empty:
        return pd.DataFrame()
    amounts = turnover.astype("float64")
    rolling = amounts.rolling(window=window, min_periods=window).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(rolling.where(rolling > 0))
    return out


def _liquidity_long(panel: pd.DataFrame) -> pd.DataFrame:
    """流动性面板（宽）⇒ 长表（index = (date, instrument)，列 = ``log_turnover``）。"""
    dates = pd.DatetimeIndex(pd.to_datetime(panel.index))
    instruments = [str(code) for code in panel.columns]
    values = panel.to_numpy(dtype="float64")
    index = pd.MultiIndex.from_product([dates, instruments], names=["date", "instrument"])
    return pd.DataFrame({LIQUIDITY_CONTROL_COLUMN: values.reshape(-1)}, index=index)


def build_controls(
    tier_panel: pd.DataFrame,
    liquidity_panel: pd.DataFrame | None = None,
    size: bool = True,
    liquidity: bool = True,
) -> pd.DataFrame:
    """组装控制变量矩阵（**规模档哑变量 + 流动性 log 值**）。

    * ``size=True`` ⇒ 加 3 个规模档哑变量（``tier_csi300/tier_csi500/tier_csi1000``，
      基准档 tier3 已 drop ⇒ 避免与截距共线）；
    * ``liquidity=True`` **且**给了 ``liquidity_panel`` ⇒ 加 ``log_turnover``；
      🔴 面板缺失 ⇒ **跳过流动性**（不留一列 NaN 假装有控制）；
    * 🔴 返回**长表**（index = ``(date, instrument)``，columns = 控制变量名）
      ⇒ 它就是截面回归的设计矩阵（见 :func:`cross_sectional_residualize`）。
    """
    parts: list[pd.DataFrame] = []
    if size:
        if tier_panel is None or tier_panel.empty:
            raise ValueError("🔴 size=True 但 tier_panel 为空 ⇒ 无法构造规模档哑变量")
        parts.append(size_tier_dummies(tier_panel))
    if liquidity and liquidity_panel is not None and not liquidity_panel.empty:
        parts.append(_liquidity_long(liquidity_panel))
    if not parts:
        return pd.DataFrame(
            index=pd.MultiIndex.from_arrays([[], []], names=["date", "instrument"])
        )
    out = pd.concat(parts, axis=1)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _as_long_controls(controls: pd.DataFrame) -> pd.DataFrame:
    """把控制变量整理成**长表**（index = (date, instrument)）并按日期排序。"""
    if controls is None or controls.empty:
        return pd.DataFrame(
            index=pd.MultiIndex.from_arrays([[], []], names=["date", "instrument"])
        )
    if not isinstance(controls.index, pd.MultiIndex):
        raise ValueError(
            "🔴 controls 必须是 build_controls() 产出的长表（index=(date, instrument)）"
        )
    long = controls.copy()
    dates = pd.DatetimeIndex(pd.to_datetime(long.index.get_level_values(0)))
    instruments = [str(code) for code in long.index.get_level_values(1)]
    long.index = pd.MultiIndex.from_arrays(
        [dates, instruments], names=["date", "instrument"]
    )
    long = long[~long.index.duplicated(keep="last")].sort_index()
    return long


def _median_int(values: list[int]) -> int:
    """整数中位数（偶数个取下中位，整数截断）。"""
    if not values:
        return 0
    ordered = sorted(int(value) for value in values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return int(ordered[mid])
    return int((ordered[mid - 1] + ordered[mid]) // 2)


# ----------------------------------------------------------------- 残差化
def cross_sectional_residualize(
    values: pd.DataFrame,
    controls: pd.DataFrame,
    min_size: int = C.MIN_CROSS_SECTION_SIZE,
) -> tuple[pd.DataFrame, dict]:
    """逐日截面 OLS 残差化：``values[t] ~ 1 + controls[t]`` ⇒ ``(残差面板, 统计)``。

    * 🔴 **逐日独立** ⇒ 绝不能跨日混样本（跨日会把时间趋势当截面信号扣掉）；
    * 🔴 **某日有效样本数 < min_size ⇒ 跳过该日**（残差**保留原值**）并计数；
    * 🔴 **全 NaN / 常数列自动丢弃**（秩 0 列会让最小二乘不稳）；
    * ``values`` 里的 NaN **保持 NaN**、不参与回归；控制变量缺测的行 ⇒ 残差 NaN；
    * 某日**完全没有控制变量** ⇒ 该日跳过（不静默做「只去均值」的降级残差化）。

    统计 dict：``n_dates_processed`` / ``n_dates_skipped`` /
    ``n_dates_no_controls`` / ``n_controls_used`` / ``n_controls_dropped`` /
    ``n_controls_total`` / ``n_samples_total`` / ``min_cross_section_size``。
    （``n_controls_used`` / ``n_controls_dropped`` = 处理日期上的**中位数**。）
    """
    stats: dict = {
        "enabled": True,
        "n_dates_processed": 0,
        "n_dates_skipped": 0,
        "n_dates_no_controls": 0,
        "n_controls_used": 0,
        "n_controls_dropped": 0,
        "n_controls_total": 0,
        "n_samples_total": 0,
        "min_cross_section_size": int(min_size),
    }
    if values is None or values.empty:
        return (values.copy() if values is not None else pd.DataFrame()), stats
    frame = values.astype("float64")
    original_index = frame.index
    original_columns = frame.columns
    date_keys = pd.DatetimeIndex(pd.to_datetime(original_index))
    value_columns = [str(code) for code in original_columns]
    if len(set(value_columns)) != len(value_columns):
        raise ValueError("🔴 values 的列名有重复 ⇒ 无法与控制变量对齐")
    long = _as_long_controls(controls)
    stats["n_controls_total"] = int(long.shape[1])
    result = frame.to_numpy(dtype="float64").copy()
    if not long.empty:
        level = long.index.get_level_values(0)
        starts = level.searchsorted(date_keys, side="left")
        ends = level.searchsorted(date_keys, side="right")
    else:
        starts = np.zeros(len(date_keys), dtype="int64")
        ends = np.zeros(len(date_keys), dtype="int64")
    used_counts: list[int] = []
    dropped_counts: list[int] = []
    for position in range(len(date_keys)):
        block = long.iloc[starts[position] : ends[position]]
        if block.empty:
            stats["n_dates_no_controls"] += 1
            stats["n_dates_skipped"] += 1
            continue
        # 🔴 **必须先去掉日期层**：MultiIndex 上直接 ``reindex`` 会按**第一层（日期）**对齐
        #    ⇒ 全 NaN；这里先把 index 降成「标的」一层，再按标的对齐到 values 的列
        block = block.droplevel(0).reindex(value_columns)
        design = block.to_numpy(dtype="float64")
        y_all = result[position]
        keep: list[int] = []
        for col in range(design.shape[1]):
            column = design[:, col]
            finite = column[np.isfinite(column)]
            if finite.size == 0:
                continue  # 🔴 全 NaN 列 ⇒ 丢弃（秩 0）
            if finite.size == 1 or float(finite.max() - finite.min()) <= 1e-12:
                continue  # 🔴 常数列 ⇒ 与截距共线 ⇒ 丢弃
            keep.append(col)
        matrix = design[:, keep] if keep else np.empty((design.shape[0], 0), dtype="float64")
        valid = np.isfinite(y_all) & np.isfinite(matrix).all(axis=1)
        n_valid = int(valid.sum())
        if n_valid < int(min_size):
            stats["n_dates_skipped"] += 1
            continue
        design_valid = matrix[valid]
        with np.errstate(all="ignore"):
            exog = np.column_stack([np.ones(design_valid.shape[0]), design_valid])
            beta, _, _, _ = np.linalg.lstsq(exog, y_all[valid], rcond=None)
            residual = y_all[valid] - exog @ beta
        row = np.full(y_all.shape, np.nan, dtype="float64")
        row[valid] = residual
        result[position] = row
        stats["n_dates_processed"] += 1
        stats["n_samples_total"] += n_valid
        used_counts.append(len(keep))
        dropped_counts.append(int(design.shape[1]) - len(keep))
    stats["n_controls_used"] = _median_int(used_counts)
    stats["n_controls_dropped"] = _median_int(dropped_counts)
    out = pd.DataFrame(result, index=original_index, columns=original_columns)
    return out, stats


# ----------------------------------------------------------------- 批量残差化（性能路径）
def _group_features_by_mask(finite_values: np.ndarray) -> list[np.ndarray]:
    """按「有效行掩码」把特征列分组（掩码相同的特征共用一次批量 lstsq）。

    * ``finite_values``：``(n_rows, n_features)`` 的布尔矩阵（True = 该格有限）；
    * 返回：每组一个列下标数组。组内特征的**有效行掩码完全相同**
      ⇒ 设计矩阵（截距 + controls）逐行相同 ⇒ 一次**多右端项** lstsq 解出整组。
    """
    n_features = finite_values.shape[1]
    if n_features == 0:
        return []
    packed = np.packbits(finite_values, axis=0).T
    groups: dict[bytes, list[int]] = {}
    for column in range(n_features):
        groups.setdefault(packed[column].tobytes(), []).append(column)
    return [np.asarray(columns, dtype=np.intp) for columns in groups.values()]


def cross_sectional_residualize_panel(
    values_3d: np.ndarray,
    dates,
    instruments,
    controls: pd.DataFrame,
    min_size: int = C.MIN_CROSS_SECTION_SIZE,
    fit_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """面板口径的逐日截面 OLS 残差化（**与 :func:`cross_sectional_residualize` 等价**）。

    🔴 **为什么需要它**：逐特征调 :func:`cross_sectional_residualize` 时，
       **每个（特征 × 交易日）都会重做一次 pandas 的 ``droplevel + reindex``**
       ⇒ 158 特征 × 730 交易日 ≈ 11.6 万次重量级对齐（实测单折训练 > 5 分钟、单核）。
       本函数把「按日期对齐 controls」与「挑 keep 列」**提到特征循环之外**做一次，
       再按**有效行掩码分组**做批量 ``lstsq``（多右端项）⇒ 每日 1~数次 lstsq。

    🔴 **口径一字不差**（旧函数仍是事实源，本函数与之逐元素等价）：
      ① 逐日独立回归，**绝不跨日混样本**；
      ② 某日（特征）有效样本数 < ``min_size`` ⇒ **跳过该（日期, 特征）**，残差保留原值；
      ③ 控制变量里的**全 NaN 列 / 常数列**自动丢弃（与截距共线）；
      ④ ``values`` 的 NaN 保持 NaN、不参与回归；控制变量缺测的行 ⇒ 残差 NaN；
      ⑤ 某日完全没有控制变量 ⇒ 该日跳过（**不**静默降级成「只去均值」）。

    参数
    ----
    values_3d : ``(n_dates, n_instruments, n_features)`` 的浮点数组。
    dates / instruments : 与前两轴一一对应的标签（标的按 ``str()`` 与 controls 对齐）。
    controls : :func:`build_controls` 产出的长表（index = ``(date, instrument)``）。
    fit_mask : 🔴 **可选**的「拟合子集」掩码（``(n_dates, n_instruments)`` 布尔数组，
        与 ``dates`` / ``instruments`` **逐轴对齐**）。

        * ``None``（**默认**）⇒ 旧口径：拟合子集 ＝ 变换全集 ⇒ **逐位不变**；
        * 给定 ⇒ **两段式**：
          **① 拟合子集** ＝ 掩码为 True 且特征/控制变量有限的行
          （回归**只用**这些行估系数）；
          **② 变换全集** ＝ 该（日期, 特征）下所有特征/控制变量有限的行
          （**残差照样算给它们**，掩码为 False 的行**不会被剔除**）。

        🔴 **为什么需要它**：逐日 PIT 训练池下，训练侧只在「当日 PIT 成员」上拟合，
        而打分侧要覆盖**整个打分池** ⇒ 若两侧拟合子集不同 ⇒ 同一天同一特征的
        **残差不同** ⇒ train/serve skew（reviewer 实测差 0.617）。
        ⇒ 两段式让两侧**系数同源**、同时保住「打分池不按日筛」。

    返回
    ----
    ``(残差三维数组, 统计)``。统计**键名与含义**同旧函数；面板口径下的聚合规则：

    ==========================  =====================================================
    键                          面板口径（多特征）
    ==========================  =====================================================
    ``n_dates_processed``        至少有一个特征被残差化的日期数
    ``n_dates_skipped``          没有任何特征被残差化的日期数
    ``n_dates_no_controls``      控制变量块为空的日期数（与特征无关）
    ``n_controls_used``          **被处理日期**上 keep 列数的中位数
    ``n_controls_dropped``       **被处理日期**上被丢弃列数的中位数
    ``n_controls_total``         控制变量列数
    ``n_samples_total``          参与回归的（日期 × 标的）有效格数
                                （同一格被多个特征复用只计一次）
    ``min_cross_section_size``   ``min_size``
    ==========================  =====================================================
    """
    stats: dict = {
        "enabled": True,
        "n_dates_processed": 0,
        "n_dates_skipped": 0,
        "n_dates_no_controls": 0,
        "n_controls_used": 0,
        "n_controls_dropped": 0,
        "n_controls_total": 0,
        "n_samples_total": 0,
        "min_cross_section_size": int(min_size),
    }
    array = np.asarray(values_3d, dtype="float64")
    if array.ndim != 3:
        raise ValueError(
            f"🔴 values_3d 必须是三维（日期 × 标的 × 特征），实际维度 {array.ndim}"
        )
    n_dates, n_instruments, n_features = array.shape
    date_keys = pd.DatetimeIndex(pd.to_datetime(pd.Index(dates)))
    value_columns = [str(code) for code in pd.Index(instruments)]
    if len(set(value_columns)) != len(value_columns):
        raise ValueError("🔴 values 的列名有重复 ⇒ 无法与控制变量对齐")
    if n_dates == 0 or n_instruments == 0 or n_features == 0:
        return array.copy(), stats
    long = _as_long_controls(controls)
    n_controls = int(long.shape[1])
    stats["n_controls_total"] = n_controls
    result = array.copy()
    # 🔴 **拟合子集掩码**（默认 None ⇒ 旧口径 ⇒ 逐位不变）
    fit_rows_all: np.ndarray | None = None
    if fit_mask is not None:
        fit_arr = np.asarray(fit_mask, dtype=bool)
        if fit_arr.shape != (n_dates, n_instruments):
            raise ValueError(
                f"🔴 fit_mask 的形状必须是 (n_dates, n_instruments) = "
                f"({n_dates}, {n_instruments})，实际 {fit_arr.shape}"
            )
        fit_rows_all = fit_arr
    if long.empty:
        starts = np.zeros(n_dates, dtype="int64")
        ends = np.zeros(n_dates, dtype="int64")
        control_values = np.empty((0, n_controls), dtype="float64")
        instrument_pos = np.zeros(0, dtype="int64")
    else:
        level = long.index.get_level_values(0)
        starts = level.searchsorted(date_keys, side="left")
        ends = level.searchsorted(date_keys, side="right")
        control_values = long.to_numpy(dtype="float64")
        # 🔴 与旧函数一致：controls 里**不在标的对齐表**的行直接丢掉（其余补 NaN）
        instrument_pos = pd.Index(value_columns).get_indexer(long.index.get_level_values(1))
    used_counts: list[int] = []
    dropped_counts: list[int] = []
    for position in range(n_dates):
        low = int(starts[position])
        high = int(ends[position])
        if high <= low:
            stats["n_dates_no_controls"] += 1
            stats["n_dates_skipped"] += 1
            continue
        # ---- 按日期对齐 controls（**每天一次**，不再逐特征重做 pandas reindex）----
        design = np.full((n_instruments, n_controls), np.nan, dtype="float64")
        rows = instrument_pos[low:high]
        usable = rows >= 0
        design[rows[usable], :] = control_values[low:high][usable]
        # ---- 挑 keep 列（只依赖 controls ⇒ 与特征无关 ⇒ 每天只算一次）----
        # 🔴🔴 **keep 列必须由「拟合子集」判定**（有 fit_mask 时）：
        #    训练侧的面板只含 PIT 成员、打分侧的面板含**整个打分池**
        #    ⇒ 若 keep 列按「各自面板」判定，某列可能在成员里恒定（⇒ 训练侧丢弃）
        #    而在全体里变动（⇒ 打分侧保留）⇒ 两侧**系数不同源** ⇒ 残差不等 ⇒ skew。
        #    ⇒ 统一按**拟合子集**判定 ⇒ 两侧选出**同一批**控制变量列。
        fit_row_mask = (np.ones(n_instruments, dtype=bool) if fit_rows_all is None
                        else fit_rows_all[position])
        if fit_rows_all is None:
            # 🔴 默认路径 ⇒ **逐位保留旧算法**（默认路径必须逐位不变）
            finite_cols = np.isfinite(design)
            finite_count = finite_cols.sum(axis=0)
            col_max = np.where(finite_cols, design, -np.inf).max(axis=0)
            col_min = np.where(finite_cols, design, np.inf).min(axis=0)
        else:
            src = design[fit_row_mask] if bool(fit_row_mask.any()) else design
            finite_cols = np.isfinite(src)
            finite_count = finite_cols.sum(axis=0)
            col_max = np.where(finite_cols, src, -np.inf).max(axis=0)
            col_min = np.where(finite_cols, src, np.inf).min(axis=0)
        keep = (finite_count > 0) & ((col_max - col_min) > 1e-12)
        matrix = design[:, keep]
        rows_finite = np.isfinite(matrix).all(axis=1)
        n_keep = int(keep.sum())
        # ---- 逐特征：按有效行掩码分组 ⇒ 每组一次多右端项 lstsq ----
        finite_values = np.isfinite(array[position])
        block_out = result[position]
        date_processed = False
        union_mask = np.zeros(n_instruments, dtype=bool)
        for columns in _group_features_by_mask(finite_values):
            # 🔴 **变换全集**：该（日期, 特征）下所有「特征 + 控制变量」都有限的行
            mask = rows_finite & finite_values[:, columns].all(axis=1)
            # 🔴 **拟合子集**：变换全集 ∩ 掩码（默认掩码全 True ⇒ ＝ 变换全集 ⇒ 旧口径）
            fit_mask_rows = mask & fit_row_mask
            n_fit = int(fit_mask_rows.sum())
            if n_fit < int(min_size):
                continue  # 🔴 有效样本不足 ⇒ 跳过该（日期, 特征），残差保留原值
            row_index = np.flatnonzero(mask)
            fit_index = np.flatnonzero(fit_mask_rows)
            exog = np.empty((len(row_index), n_keep + 1), dtype="float64")
            exog[:, 0] = 1.0
            exog[:, 1:] = matrix[row_index]
            targets = array[position][row_index][:, columns]
            # 🔴 ``fit_index`` 是**面板轴**下标 ⇒ 映射到 ``row_index``
            #    （变换全集）里的位置
            fit_pos = np.searchsorted(row_index, fit_index)
            exog_fit = exog[fit_pos]
            targets_fit = targets[fit_pos]
            with np.errstate(all="ignore"):
                beta, _, _, _ = np.linalg.lstsq(exog_fit, targets_fit, rcond=None)
                residual = targets - exog @ beta
            block_out[np.ix_(row_index, columns)] = residual
            invalid_index = np.flatnonzero(~mask)
            if invalid_index.size:
                block_out[np.ix_(invalid_index, columns)] = np.nan
            date_processed = True
            union_mask |= mask
        if date_processed:
            stats["n_dates_processed"] += 1
            stats["n_samples_total"] += int(union_mask.sum())
            used_counts.append(n_keep)
            dropped_counts.append(int(n_controls - n_keep))
        else:
            stats["n_dates_skipped"] += 1
    stats["n_controls_used"] = _median_int(used_counts)
    stats["n_controls_dropped"] = _median_int(dropped_counts)
    return result, stats


def neutralize_frame(
    values: pd.DataFrame,
    controls: pd.DataFrame,
    enabled: bool | None = None,
) -> tuple[pd.DataFrame, dict]:
    """中性化入口（**总开关**在这里）。

    * ``enabled=None`` ⇒ 取 ``config_v3.NEUTRALIZATION_ENABLED``；
    * ``enabled=False`` ⇒ **原样返回**（统计里 ``enabled=False``，其余计数全 0）
      ⇒ 对照臂可以显式关掉，而不必换调用链。
    """
    switch = bool(C.NEUTRALIZATION_ENABLED) if enabled is None else bool(enabled)
    if not switch:
        stats = {
            "enabled": False,
            "n_dates_processed": 0,
            "n_dates_skipped": 0,
            "n_dates_no_controls": 0,
            "n_controls_used": 0,
            "n_controls_dropped": 0,
            "n_controls_total": 0,
            "n_samples_total": 0,
            "min_cross_section_size": int(C.MIN_CROSS_SECTION_SIZE),
        }
        return values.copy(), stats
    residuals, stats = cross_sectional_residualize(values, controls)
    stats["enabled"] = True
    return residuals, stats
