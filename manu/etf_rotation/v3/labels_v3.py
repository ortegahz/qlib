"""🔴 V3 标签（对齐①）—— **数值层面中心化**的 20 日周期超额（t+15~t+25 均价退出）。

**V3 与 V2 的最大差别（用户 2026-11-26 目标文档「对齐①」）**

  * V2：20 日窗口收益（t+15~t+25 均价退出）→ **CSRankNorm**（截面排名归一）
    ⇒ 基准项是**逐日常数** ⇒ 截面排名**逐位不变** ⇒ **数值超额被抹平**
    ⇒ 训练信号里其实**没有**「超额」，只有「排名」；
  * 🔴 **V3：数值层面中心化**（减当日训练池截面均值 / 减基准同窗口收益）
    ⇒ **保留数值超额** ⇒ 排序损失（lambdarank）才有「超额」可排。

**口径速查（grep / 审计用）**

    exit_px[t]        = mean_{k=15..25}( P[t+k] )        ← 退出价 = 窗口均价
    fwd_ret_20d[t]    = exit_px[t] / P[t] - 1
    label[t]          = fwd_ret_20d[t] - center[t]          ← 🔴 V3 的新增一步
    grade[t]          = 分箱(fwd_ret_20d[t] - center[t])     ← 整数 relevance grade

  * ``P`` = 个股 VWAP（与 V1/V2 同价口径）；
  * 🔴 **窗口均价口径与 V2 逐位一致**（守卫测试里与 ``v2/labels.py`` 对拍）——
    注意 qlib 的 ``Mean(Ref($vwap,-15)/$vwap-1, 11)`` **不是**本口径
    （``Mean`` 让分子分母一起滚动 ⇒ 变成「15 日前的相对收益」）；
  * 🔴 **中心化在 pandas 侧做**（qlib 表达式只给原始窗口收益）⇒ 口径唯一、可对拍。
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

try:  # 扁平导入（v3 目录进 sys.path，与 v2 同口径）
    import config_v3 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v3 as C


# ----------------------------------------------------------------- 前瞻收益
def forward_window_return(
    price: pd.DataFrame,
    window: tuple[int, int] = C.LABEL_EXIT_WINDOW,
) -> pd.DataFrame:
    """逐（日期 × 标的）算**窗口均价**口径的前瞻收益。

    * ``price``：宽表（index = 日期，columns = 标的，值 = 价格）；
    * 返回：同形状的 DataFrame，值 = ``mean_{k=lo..hi}(P[t+k]) / P[t] - 1``；
      窗口超出样本末端或**窗口内有任一缺失** ⇒ **NaN**（标准缺失语义，不填 0）；
    * 🔴 **与 V2 ``v2/labels.py::forward_window_return`` 逐位一致**
      （本地等价实现；V3 **不 import** V2，守卫测试里对拍）
      —— 口径不许漂移，漂了就等于把 V2 的实验结论带歪。
    """
    lo, hi = (int(window[0]), int(window[1])) if window else (15, 25)
    if price is None or price.empty:
        return pd.DataFrame()
    px = price.astype("float64")
    # 🔴 **为什么用 numpy 而不是 pandas 的 concat/where**：
    #    pandas 的 Series×DataFrame 默认按**列**对齐（日期索引 × 标的列名 ⇒ 全 NaN），
    #    而 `pd.concat([...], axis=1)` 又会造出**重复列名** ⇒ 再一 `.div` 又对不上。
    #    ⇒ 这里显式堆成 (n_window, T, N) 的三维数组，语义直白、没有对齐坑。
    base = px.to_numpy(dtype="float64")
    base = np.where(np.isfinite(base), base, np.nan)
    stack = np.stack([np.roll(base, -k, axis=0) for k in range(lo, hi + 1)], axis=0)
    # `np.roll` 会把尾部数据**绕回**开头 ⇒ 手工把越界位打成 NaN（末端不足 ⇒ NaN）
    t_len = base.shape[0]
    for j, k in enumerate(range(lo, hi + 1)):
        if k >= t_len:
            stack[j, :, :] = np.nan
        else:
            stack[j, t_len - k :, :] = np.nan
    full = np.isfinite(stack).all(axis=0)
    with warnings.catch_warnings():
        # 窗口整体缺失（全 NaN 切片）⇒ nanmean 告警；这里**主动静音**（结果本来就该是 NaN）
        warnings.simplefilter("ignore", category=RuntimeWarning)
        with np.errstate(invalid="ignore"):
            exit_px = np.nanmean(stack, axis=0)
    out = exit_px / base - 1.0
    out = np.where(full & np.isfinite(base), out, np.nan)
    return pd.DataFrame(out, index=px.index, columns=px.columns)


# ----------------------------------------------------------------- 中心化
def market_mean_return(fwd: pd.DataFrame) -> pd.Series:
    """逐日截面均值（**nanmean**）⇒ 当日「训练池平均前瞻收益」。

    返回：index = 日期的 Series（NaN 表示该日**一个样本都没有**）。
    """
    if fwd is None or fwd.empty:
        return pd.Series(dtype="float64")
    return fwd.astype("float64").mean(axis=1, skipna=True)


def center_label(
    fwd: pd.DataFrame,
    mode: str | None = None,
    bench_fwd: pd.Series | None = None,
) -> tuple[pd.DataFrame, dict]:
    """🔴 **V3 核心差异点**：数值层面中心化。

    * ``mode='market_mean'``（默认，``config_v3.LABEL_CENTERING``）
      ⇒ 减**当日训练池截面均值**（更贴「跑赢 ETF 成分池均值」的用户表述）；
    * ``mode='benchmark'`` ⇒ 减 ``bench_fwd``（基准同窗口收益，**逐日常数**）；
    * 🔴 **保留数值超额**（**不做 CSRankNorm**）⇒ 数值层面的「超额」不会像 V2 那样
      被排名归一抹平；
    * 返回 ``(中心化后面板, 统计)``；统计 = ``mean_abs_shift`` / ``mode`` / ``n_dates``。
    """
    chosen = str(mode or C.LABEL_CENTERING)
    if fwd is None or fwd.empty:
        return (fwd if fwd is not None else pd.DataFrame()), {
            "mean_abs_shift": 0.0,
            "mode": chosen,
            "n_dates": 0,
        }
    frame = fwd.astype("float64")
    if chosen == "market_mean":
        shift = market_mean_return(frame)
    elif chosen == "benchmark":
        if bench_fwd is None:
            raise ValueError("🔴 mode='benchmark' 必须给 bench_fwd（基准同窗口收益）")
        raw = pd.Series(bench_fwd).astype("float64")
        shift = raw.reindex(frame.index)
        if bool(raw.notna().any()) and not bool(shift.notna().any()):
            raise ValueError(
                "🔴 bench_fwd 的索引与标签面板对不上 ⇒ 无法按日相减"
                f"（bench 前 3 个索引 = {list(raw.index[:3])}）"
            )
    else:
        raise ValueError(f"🔴 未知的中心化口径：{chosen}（合法 = market_mean / benchmark）")
    centered = frame.sub(shift, axis=0)
    shift_values = shift.to_numpy(dtype="float64")
    finite = shift_values[np.isfinite(shift_values)]
    mean_abs_shift = float(np.mean(np.abs(finite))) if finite.size else 0.0
    stats = {
        "mean_abs_shift": mean_abs_shift,
        "mode": chosen,
        "n_dates": int(len(frame.index)),
    }
    return centered, stats


# ----------------------------------------------------------------- relevance grade
def rank_grades(excess: pd.DataFrame, n_grades: int = C.RANK_GRADE_COUNT) -> pd.DataFrame:
    """逐日按超额分位分箱 ⇒ **整数 relevance grade** ∈ ``[0, n_grades-1]``。

    * 分箱口径：当日截面的**平均秩**（并列取平均）⇒ 独占分位
      ``(rank - 1) / n`` ∈ [0, 1) ⇒ ``grade = floor(分位 × n_grades)``
      ⇒ 与 ``pd.qcut(..., labels=range(n_grades))`` **同样等分箱**（每档一样多）；
    * 🔴 **某日截面样本 < n_grades ⇒ 该日全部记 0**（样本太少 ⇒ 分箱没意义）
      ⇒ 记在 ``attrs["n_dates_skipped"]``；
    * 返回的 DataFrame 带 ``attrs``：``n_grades`` / ``n_dates_skipped`` / ``note``。
    """
    grades = int(n_grades)
    if grades < 1:
        raise ValueError(f"🔴 relevance grade 数必须 ≥ 1（{grades}）")
    if excess is None or excess.empty:
        out = pd.DataFrame(index=pd.Index([]), columns=pd.Index([]), dtype="int64")
        out.attrs["n_grades"] = grades
        out.attrs["n_dates_skipped"] = 0
        out.attrs["note"] = "空面板"
        return out
    values = excess.astype("float64")
    out = pd.DataFrame(0, index=values.index, columns=values.columns, dtype="int64")
    skipped = 0
    for date in values.index:
        row = values.loc[date]
        vector = row.to_numpy(dtype="float64")
        valid = np.isfinite(vector)
        if int(valid.sum()) < grades:
            skipped += 1
            continue
        ranks = pd.Series(vector[valid]).rank(method="average").to_numpy()
        # 🔴 独占分位 (rank - 1) / n ⇒ 等分箱（并列取平均秩 ⇒ 并列落在同一档）
        percentile = (ranks - 1.0) / float(ranks.size)
        bucket = np.floor(percentile * grades).astype("int64")
        np.clip(bucket, 0, grades - 1, out=bucket)
        out.loc[date, row.index[valid]] = bucket
    out.attrs["n_grades"] = grades
    out.attrs["n_dates_skipped"] = int(skipped)
    out.attrs["note"] = (
        f"grade = floor(平均秩分位 × {grades})；样本 < {grades} 的日期整日记 0"
    )
    return out


# ----------------------------------------------------------------- 口径卡
def label_caliber() -> dict:
    """标签口径卡（**字段名与** ``config_v3.v3_caliber_card()['label']`` **对齐**）。

    🔴 口径文案**单一持有**在 ``config_v3`` ⇒ 这里**只转述、不重写**
    ⇒ 报告里的口径卡与代码里的常量**不可能漂移**。
    """
    card = dict(C.v3_caliber_card()["label"])
    card["centering_mode_available"] = ["market_mean", "benchmark"]
    card["rank_grade_count"] = int(C.RANK_GRADE_COUNT)
    return card
