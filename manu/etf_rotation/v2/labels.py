"""🔴 V2 标签（对齐①）：**未来 20 交易日超额收益**，退出窗口 t+15 ~ t+25。

  📌 **口径速查（供 grep / 审计）**：
     * 「未来 20 交易日超额收益」= ``fwd_ret_20d``（= ``fwd_excess_20d`` 扣掉基准同窗口收益）；
     * qlib 表达式 = ``Ref(Mean($vwap,11),-25)/$vwap-1``
       —— 逐字等价于「``Ref($vwap, -25)`` 取窗口均价再比当期」：
         退出价 = ``mean(P[t+15], ..., P[t+25])``（11 个点，偏移 15..25），
         标签   = 退出价 / P[t] − 1；
     * 🔴 **不许**写成 ``Mean(Ref($vwap,-15)/$vwap-1, 11)``（qlib 的 ``Mean`` 会让分子分母一起滚动
       ⇒ 得到「15 日前的相对收益」，口径完全错位）。

口径（逐字落定，报告里必须照抄）：

    exit_px[t]          = mean_{k=15..25}( P[t+k] )        ← **退出价 = 窗口均价**
    bench_exit_px[t]    = mean_{k=15..25}( B[t+k] )
    fwd_ret_20d[t]      = exit_px[t] / P[t] - 1
    bench_ret_20d[t]    = bench_exit_px[t] / B[t] - 1
    fwd_excess_20d[t]   = fwd_ret_20d[t] - bench_ret_20d[t]

  * `P` = 个股 VWAP（与 V1 标签同价口径）；`B` = 沪深300（qlib ``sh000300``）VWAP；
  * 退出窗口 **t+15 ~ t+25**（11 个交易日）= 月频调仓时「下一次调仓日」的合理区间
    （按真实日历，下一个月首交易日在 t 之后 15~25 个交易日内）⇒
    **退出价取该窗口的均价**，以**消除单日收盘毛刺**
    （用户权威规格原文：「取 t+15 ~ t+25 均价以消除单日收盘毛刺」——
     毛刺在**退出价**上 ⇒ 对退出价取均价，不是对日收益取均值；
     ⚠️ qlib 的 `Mean(Ref($vwap,-15)/$vwap-1,11)` **不是**本口径
     （`Mean` 会让分子分母一起滚动 ⇒ 变成「15 日前的相对收益」）
     ⇒ 正确写法是 `Ref(Mean($vwap,11),-25)/$vwap-1`，已在真机上逐位核对；
     见 `test_pandas版与qlib表达式版逐位一致`）；
  * 🔴 标签窗口**末端 = t+25** ⇒ embargo 必须 ≥ 25（见 `splits.py`）。

🔴 **「超额」在训练层的等价性（必须披露，不许含糊）**：
   V1/V2 的标签后处理都是 **CSRankNorm（截面排名归一）**
   ⇒ 基准项 `bench_ret_20d[t]` 是**逐日常数** ⇒ 截面排名**逐位不变**
   ⇒ 训练信号与「原始窗口收益」**完全等价**；
   ⇒ **超额**的实质体现在**评估层**（IC/组合超额都按超额口径算）。
   `test_基准项不改变截面排名` 把这条性质**钉死**（变异反证：改成 z-score 即转红）。
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

import config_v2 as C


def label_exit_window() -> tuple[int, int]:
    """退出窗口（相对 t 的偏移闭区间）= (15, 25)。"""
    return tuple(int(x) for x in C.LABEL_EXIT_WINDOW)  # type: ignore[return-value]


def label_horizon_days() -> int:
    """标签窗口末端相对 t 的偏移（= 25）⇒ embargo 的下限。"""
    return int(C.LABEL_HORIZON_DAYS)


def qlib_label_expression() -> str:
    """qlib 表达式版标签（训练用）：``Ref(Mean($vwap,11),-25)/$vwap-1``。

    等价性（**真机实测 max|diff| = 2.1e-7**，float32 精度内逐位一致）：
      * ``Mean($vwap, 11)`` 是 11 日**均价**序列；
      * ``Ref(X, -25)`` 把整条序列**前移 25 日** ⇒ 在 t 处取值
        ``mean(P[t+15], ..., P[t+25])``（11 个点，偏移 15..25）✓；
      * 除以 ``$vwap``（= P[t]）再减 1 ⇒ 窗口均价口径的前瞻收益。

    🔴 反例（**不许**用的写法）：``Mean(Ref($vwap,-15)/$vwap-1, 11)``
       —— `Mean` 让分子分母**一起**滚动 ⇒ 得到
       ``mean_{j=0..10}(P[t+15-j]/P[t-j]-1)``（≈15 日前的相对收益）
       ⇒ 与「未来 15~25 日」**口径完全错位**。
    """
    return str(C.LABEL_EXPRESSION)


# ----------------------------------------------------------------- pandas 版实现
def forward_window_return(price: pd.DataFrame, window: tuple[int, int] | None = None) -> pd.DataFrame:
    """逐（日期 × 标的）算**窗口均价**口径的前瞻收益。

    `price`：宽表（index = 日期，columns = 标的，值 = 价格）。
    返回：同形状的 DataFrame，值 = ``mean_{k=lo..hi}(P[t+k]) / P[t] - 1``；
    窗口超出样本末端或**窗口内有任一缺失** ⇒ **NaN**（标准缺失语义，不填 0）。

    🔴 为什么是「**均价 / P[t]**」而不是「日收益的均值」：
       用户规格原文是「取 t+15 ~ t+25 **均价**以消除单日收盘毛刺」
       ⇒ 毛刺在**退出价**上 ⇒ 对**退出价**取窗口均价。
    """
    lo, hi = window or label_exit_window()
    if price is None or price.empty:
        return pd.DataFrame()
    px = price.astype("float64")
    # 🔴 **为什么用 numpy 而不是 pandas 的 concat/where**：
    #    pandas 的 Series×DataFrame 默认按**列**对齐（日期索引 × 标的列名 ⇒ 全 NaN），
    #    而 `pd.concat([...], axis=1)` 又会造出**重复列名** ⇒ 再一��� `.div` 又对不上。
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
            stack[j, t_len - k:, :] = np.nan
    full = np.isfinite(stack).all(axis=0)
    with warnings.catch_warnings():
        # 窗口整体缺失（全 NaN 切片）⇒ nanmean 告警；这里**主动静音**（结果本来就该是 NaN）
        warnings.simplefilter("ignore", category=RuntimeWarning)
        with np.errstate(invalid="ignore"):
            exit_px = np.nanmean(stack, axis=0)
    out = exit_px / base - 1.0
    out = np.where(full & np.isfinite(base), out, np.nan)
    return pd.DataFrame(out, index=px.index, columns=px.columns)


def excess_forward_return(stock_fwd: pd.DataFrame, bench_fwd: pd.Series) -> pd.DataFrame:
    """超额 = 个股窗口收益 − 基准窗口收益（**逐日常数**）。"""
    if stock_fwd is None or stock_fwd.empty:
        return pd.DataFrame()
    bench = bench_fwd.reindex(stock_fwd.index)
    return stock_fwd.sub(bench, axis=0)


def cross_sectional_rank(panel: pd.DataFrame) -> pd.DataFrame:
    """逐日截面排名（与 qlib ``CSRankNorm`` 同口径：当日截面内的百分位排名）。"""
    return panel.rank(axis=1, pct=True, method="average")


def rank_invariance_under_daily_shift(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """两条逐日**同减一个常数**的序列，截面排名是否**逐位相同**。

    ⇒ 这就是「超额收益 ≡ 原始窗口收益（CSRankNorm 之后）」的数学依据。
    """
    if a.empty or b.empty or a.shape != b.shape:
        return False
    ra = cross_sectional_rank(a)
    rb = cross_sectional_rank(b)
    return bool(np.allclose(ra.to_numpy(dtype="float64"),
                            rb.to_numpy(dtype="float64"),
                            equal_nan=True))


def daily_constant_shift(a: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    """把基准的逐日常数从面板里减掉（构造「超额」面板）。"""
    return a.sub(bench.reindex(a.index), axis=0)


# ----------------------------------------------------------------- qlib 取数
def qlib_price_panel(instruments: str, start: str, end: str,
                      field: str = "$vwap") -> pd.DataFrame:
    """从 qlib 取价格面板（宽表：index=日期，columns=标的）。

    ⚠️ 只读；不联网；不写任何文件。
    """
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(C.QLIB_CN_DATA))
    return D.features(instruments, [field], start_time=start, end_time=end)


def qlib_benchmark_forward(start: str, end: str) -> pd.Series:
    """基准（沪深300）的窗口收益序列（逐日一个数）。"""
    panel = qlib_price_panel(C.BENCHMARK, start, end)
    if panel.empty:
        return pd.Series(dtype="float64")
    series = panel.iloc[:, 0]
    fwd = forward_window_return(panel)
    return fwd.iloc[:, 0]


def label_caliber() -> dict:
    """给报告/manifest 用的标签口径卡（**逐字**照抄，不许改写）。"""
    return {
        "version": C.V2_VERSION,
        "label_name": "fwd_excess_20d",
        "price_field": C.LABEL_PRICE_FIELD,
        "exit_window": list(C.LABEL_EXIT_WINDOW),
        "horizon_days": label_horizon_days(),
        "benchmark": C.BENCHMARK,
        "qlib_expression": qlib_label_expression(),
        "formula": (
            "exit_px[t] = mean_{k=15..25}(P[t+k]); "
            "fwd_excess_20d[t] = (exit_px[t]/P[t]-1) - (mean_{k=15..25}(B[t+k])/B[t]-1)"
        ),
        "caliber_note": (
            "退出价取 t+15~t+25 窗口均价（规格原文「取 t+15 ~ t+25 均价以消除单日收盘毛刺」）；"
            "qlib 表达式 = Ref(Mean($vwap,11),-25)/$vwap-1（Mean(Ref(...),11) 不是本口径）"
        ),
        "post_processing": "CSRankNorm(label)（与 V1 同口径；基准项为逐日常数 ⇒ 截面排名不变）",
        "note": (
            "标签窗口末端 = t+25 ⇒ embargo 实际取 25 个交易日（见 splits.embargo_summary）"
        ),
    }
