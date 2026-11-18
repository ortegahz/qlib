"""🔴 V2 评估指标（Cost-Aware 口径）—— **V2 唯一指标口径源**。

口径与依据（逐条对齐 V1 baseline；V1 `backtest.py` / `analyze.py` 是**冻结**参照，
本模块**不 import V1**，只按其口径说明逐条复刻）：

===================  =====================================================  ==================
指标                 口径                                                  V1 依据
===================  =====================================================  ==================
``annualized_return`` 几何年化 ``(nav[-1]/nav[0])**(periods_per_year/期数) - 1``  ``backtest.py:303``
``max_drawdown``     **负数** ``min(nav / cummax(nav) - 1)``                ``backtest.py:306``
``sharpe``           标准口径 ``mean(超额)/std(超额, ddof=1) * sqrt(ppy)``   ⚠️ V1 用粗口径
``calmar``           ``年化收益 / |最大回撤|``                              ``backtest.py`` 组合层
``rank_ic``          逐日**截面** Spearman ⇒ 时间序列均值                   ``analyze.py::_rank_ic``
``icir``             ``mean(IC) / std(IC, ddof=1)``（**不**年化）           ``analyze.py::_aggregate_ic``
``t_stat``           ``mean(IC) / (std(IC)/sqrt(n))``                      ``analyze.py::_aggregate_ic``
``total_cost_drag``  ``total_turnover * (commission + slippage)``          ``backtest.py`` cost_assumption
``annualized_turnover``  ``total_turnover / 年数``（**双边** Σ|Δw|）        ``backtest.py`` 同式
``annualized_turnover_one_way``  ``0.5 × annualized_turnover``（**单边**）  Top-K 线同源口径
``annualized_cost_drag``  ``total_cost_drag / 年数``（**年化**成本拖累）    本模块派生
===================  =====================================================  ==================

🔴 **换手的单/双边口径（必须与 Top-K 线同源）**：
``manu/memory/ashare_topk_20261104.md``「单边换手定义」行 = ``0.5 × Σ|Δw|``
⇒ **Σ|Δw| 是双边**（= 总成交额 / 组合净值），**单边 = 0.5 × 双边**。
本模块的 ``annualized_turnover`` 走的就是 Σ|Δw| ⇒ 它是**双边**；
单边请用 ``annualized_turnover_one_way``（换算唯一实现 = ``config_v2.one_way_turnover``）。

⚠️ **本文件里「单边佣金 / 单边滑点」说的是*费率*口径**（每个方向各收一次），
**不是**换手口径 ⇒ 不要与上面的单/双边换手混读。

⚠️ **Sharpe 与 V1 的口径差异（必须披露）**：V1 `backtest.py:305` 用的是粗口径
``年化收益 / 年化波动``；本模块用标准口径 ``mean(超额)/std(超额) * sqrt(ppy)``。
两者在低波动下数值接近但**不等价** ⇒ 对比表里必须写明用的是哪一个（用函数名标注）。

🔴 **样本不足一律 None / NaN，不许编数字**：
  * 净值有效点数 < 2 ⇒ 年化 / Sharpe / Calmar = NaN；
  * RankIC **有效截面 < 2** ⇒ ``ic`` / ``icir`` / ``t_stat`` = None
    （V1 用更严的 ``MIN_IC_CROSS_SECTIONS``；V2 按契约取 2）；
  * RankIC 单个截面内样本 < 2 ⇒ 该截面 IC = None（不计入）。

🔴 **窗口耦合铁律（净值与换手必须同窗口）**：``summarize()`` 的
``annualized_turnover = total_turnover / years`` 里的 ``years`` 由**传入 nav 的长度**
决定 ⇒ 若净值给全史、换手只给 val 段 ⇒ **年化换手被低估**
（审计实测：objective 报 4.99×，val 段真值 17.13× ⇒ 低估约 3.4 倍）
⇒ 「换手 ≤ 4×」目标直接失效。
⇒ 统一用 :func:`slice_window` 把**净值 / 换手 / 基准同时**切到同一窗口，
   再交给 :func:`summarize`；是否达标用 :func:`check_turnover_target` 判
   （``mode="gate"`` = 旧硬闸门；``mode="disclosure"`` = 只披露不拦截）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import config_v2 as C

__all__ = [
    "annualized_return",
    "max_drawdown",
    "sharpe_ratio",
    "calmar_ratio",
    "rank_ic",
    "summarize",
    "slice_window",
    "check_turnover_target",
]


# ----------------------------------------------------------------- 基础工具
def _nav_series(nav: Any) -> pd.Series:
    """把净值输入统一成 ``float64`` Series（去掉 NaN，保留顺序）。"""
    if nav is None:
        return pd.Series(dtype="float64")
    if isinstance(nav, pd.Series):
        s = nav.astype("float64")
    elif isinstance(nav, pd.DataFrame):
        s = nav.iloc[:, 0].astype("float64")
    else:
        s = pd.Series(list(nav), dtype="float64")
    return s.replace([np.inf, -np.inf], np.nan).dropna()


def _turnover_series(turnover: Any) -> pd.Series:
    """把换手输入统一成 ``float64`` Series（支持 Series / dict / 标量 / 序列）。"""
    if turnover is None:
        return pd.Series(dtype="float64")
    if isinstance(turnover, pd.Series):
        s = turnover.astype("float64")
    elif isinstance(turnover, dict):
        s = pd.Series(turnover, dtype="float64")
    elif isinstance(turnover, (int, float, np.integer, np.floating)):
        s = pd.Series([float(turnover)], dtype="float64")
    else:
        s = pd.Series(list(turnover), dtype="float64")
    return s.replace([np.inf, -np.inf], np.nan).dropna()


def _years(n_points: int, periods_per_year: int) -> float:
    """年数：净值点数 N ⇒ 收益期数 N−1 ⇒ 年数 = (N−1)/periods_per_year。

    与 V1 ``backtest.py:302`` 的 ``years = len(日收益序列) / TRADING_DAYS_PER_YEAR``
    等价（V1 的日收益序列长度 = 净值点数 − 1）。
    """
    if n_points < 2 or periods_per_year <= 0:
        return 0.0
    return (n_points - 1) / float(periods_per_year)


# ----------------------------------------------------------------- 收益 / 风险
def annualized_return(nav: pd.Series, periods_per_year: int = 252) -> float:
    """年化收益率（**几何**口径）。

    ``(nav[-1] / nav[0]) ** (periods_per_year / 期数) - 1``，期数 = 净值有效点数 − 1。

    样本不足（< 2 点）或首尾值非正 ⇒ 返回 NaN（**不编数字**）。
    """
    s = _nav_series(nav)
    if len(s) < 2:
        return float("nan")
    total = float(s.iloc[-1] / s.iloc[0])
    if not np.isfinite(total) or total <= 0.0:
        return float("nan")
    years = _years(len(s), periods_per_year)
    if years <= 0.0:
        return float("nan")
    return float(total ** (1.0 / years) - 1.0)


def max_drawdown(nav: pd.Series) -> float:
    """最大回撤（**负数**；无回撤时为 0.0）。

    ``min(nav / cummax(nav) - 1)``（与 V1 ``backtest.py:306`` 同式）。
    净值序列为空 ⇒ NaN。
    """
    s = _nav_series(nav)
    if s.empty:
        return float("nan")
    dd = s / s.cummax() - 1.0
    value = float(dd.min())
    if not np.isfinite(value):
        return float("nan")
    return min(value, 0.0)


def sharpe_ratio(
    returns: pd.Series,
    periods_per_year: int = 252,
    rf: float = 0.0,
) -> float:
    """夏普比率（标准口径，**年化**）。

    ``mean(超额) / std(超额, ddof=1) * sqrt(periods_per_year)``。

    * ``rf`` = **年化**无风险利率（内部按 ``periods_per_year`` 折算成每期）；
    * 样本不足（< 2 期）或标准差为 0 ⇒ NaN（**不编数字**）。
    """
    r = _nav_series(returns)
    if len(r) < 2:
        return float("nan")
    per_period_rf = float(rf) / float(periods_per_year) if periods_per_year > 0 else 0.0
    excess = r - per_period_rf
    std = float(excess.std(ddof=1))
    if not np.isfinite(std) or std <= 0.0:
        return float("nan")
    return float(excess.mean() / std * np.sqrt(periods_per_year))


def calmar_ratio(ann_return: float, mdd: float) -> float:
    """卡玛比率 = ``年化收益 / |最大回撤|``。

    回撤为 0（无回撤）时该比值**不可定义** ⇒ 返回 NaN（避免 inf 污染汇总表）。
    """
    ann = float(ann_return)
    dd = abs(float(mdd))
    if not np.isfinite(ann) or not np.isfinite(dd) or dd <= 0.0:
        return float("nan")
    return float(ann / dd)


# ----------------------------------------------------------------- RankIC
def _norm_date(value: Any) -> Any:
    """日期归一化（``Timestamp`` / ``date`` / ISO 字符串 → ``Timestamp``；失败则原样保留）。"""
    if isinstance(value, pd.Timestamp):
        return value
    if isinstance(value, str):
        try:
            return pd.Timestamp(value)
        except (ValueError, TypeError):
            return value
    try:
        return pd.Timestamp(value)
    except (ValueError, TypeError):
        return value


def rank_ic(dates, pred, fwd) -> dict:
    """逐日**截面** Spearman IC ⇒ 时间序列汇总。

    口径（与 V1 ``analyze.py::_rank_ic`` / ``_aggregate_ic`` 逐字一致）：
      * 每个截面日：``corr(pred, fwd, method="spearman")``；
      * ``ic`` = 各截面 IC 的均值；
      * ``icir`` = ``mean(IC) / std(IC, ddof=1)``（**不**年化，与 V1 baseline 同口径）；
      * ``t_stat`` = ``mean(IC) / (std(IC) / sqrt(n))``。

    🔴 **样本不足 ⇒ None，不许编**：有效截面 < 2 ⇒ ic / icir / t_stat 全 None；
       单截面内样本 < 2（或 pred/fwd 常数）⇒ 该截面 IC 记 None（不计入）。

    返回 ``{"ic", "icir", "t_stat", "n_cross_sections"}``（**只这四个键**）。
    """
    df = pd.DataFrame(
        {
            "date": [_norm_date(d) for d in list(dates)],
            "pred": pd.to_numeric(pd.Series(list(pred)), errors="coerce").to_numpy(),
            "fwd": pd.to_numeric(pd.Series(list(fwd)), errors="coerce").to_numpy(),
        }
    )
    ics: list[float] = []
    if not df.empty:
        for _, grp in df.groupby("date", sort=False):
            if len(grp) < 2:
                continue
            sub = grp[["pred", "fwd"]].dropna()
            if len(sub) < 2:
                continue
            ic = sub["pred"].corr(sub["fwd"], method="spearman")
            if ic is None or not np.isfinite(ic):
                continue
            ics.append(float(ic))

    n = len(ics)
    if n < 2:
        return {"ic": None, "icir": None, "t_stat": None, "n_cross_sections": int(n)}
    arr = np.asarray(ics, dtype="float64")
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    icir = float(mean / std) if std > 0.0 else None
    t_stat = float(mean / (std / np.sqrt(n))) if std > 0.0 else None
    return {
        "ic": mean,
        "icir": icir,
        "t_stat": t_stat,
        "n_cross_sections": int(n),
    }


# ----------------------------------------------------------------- 汇总
def summarize(
    nav: pd.Series,
    benchmark_nav: pd.Series | None = None,
    turnover: Any = None,
    commission: float = C.COMMISSION,
    slippage: float = C.SLIPPAGE,
    *,
    periods_per_year: int = 252,
) -> dict:
    """组合指标汇总（Cost-Aware：换手与成本拖累一并进表）。

    参数
    ----
    nav : 净值序列（Series；也接受 dict / 序列）。
    benchmark_nav : 基准净值（可选）。给了才算 ``excess_vs_benchmark``。
    turnover : 换手序列（Series / dict / 标量 / None）——**双边**换手求和口径
        （Σ|Δw|；**单边 = 0.5×**，见模块 docstring 的单/双边口径说明）。
    commission / slippage : 单边佣金 / 滑点（**费率**口径，默认取 ``config_v2``）。
    periods_per_year : 年化用的期数（默认 252；V1 用 250，差异 0.8% 需在报告里说明）。

    返回（**键固定**）：
      ``annualized_return`` / ``max_drawdown`` / ``sharpe`` / ``calmar`` /
      ``total_turnover`` / ``annualized_turnover`` /
      ``annualized_turnover_one_way`` / ``total_cost_drag`` / ``annualized_cost_drag``
      （+ ``excess_vs_benchmark``，仅当给了基准）。

    口径：
      * ``total_turnover`` = 换手求和（Σ|Δw|，**双边**）；
      * ``annualized_turnover`` = ``total_turnover / 年数``，年数 = (净值点数−1)/periods_per_year
        （**双边**口径）；
      * ``annualized_turnover_one_way`` = ``0.5 × annualized_turnover``
        （**单边**；换算唯一实现 = ``config_v2.one_way_turnover``；双边算不出来时为 NaN）；
      * ``annualized_cost_drag`` = ``total_cost_drag / 年数``（**年化**成本拖累；
        年数 = 0 / 非法 ⇒ NaN，**不编数字**）；
      * ⚠️ **净值与换手必须同窗口**（``年数`` 由**净值点数**决定 ⇒ nav 给全史而换手只给
        窗口内时**年化换手被低估**）⇒ 请先用 :func:`slice_window` 把净值 / 换手 / 基准
        **同时**切到同一窗口再调用本函数；
      * ``total_cost_drag`` = ``total_turnover * (commission + slippage)``
        （V1 ``cost_assumption.total_cost_drag`` 同式；**线性近似**，真实拖累 ≈ 1−Π(1−cost)）；
      * ``excess_vs_benchmark`` = **相对（几何）**口径：``nav / benchmark`` 再年化
        （V1 ``backtest.py:316-318`` 的 ``excess_rel`` 同口径）。
    """
    s = _nav_series(nav)
    rets = s.pct_change().dropna() if len(s) >= 2 else pd.Series(dtype="float64")
    ann = annualized_return(s, periods_per_year=periods_per_year)
    mdd = max_drawdown(s)
    to = _turnover_series(turnover)
    total_turnover = float(to.sum()) if len(to) else 0.0
    years = _years(len(s), periods_per_year)
    ann_turnover = float(total_turnover / years) if years > 0 else float("nan")
    total_cost_drag = float(total_turnover * (float(commission) + float(slippage)))
    out = {
        "annualized_return": ann,
        "max_drawdown": mdd,
        "sharpe": sharpe_ratio(rets, periods_per_year=periods_per_year),
        "calmar": calmar_ratio(ann, mdd),
        "total_turnover": total_turnover,
        # 🔴 双边（Σ|Δw|）——单边见下一个键（Top-K 线同源口径：单边 = 0.5 × 双边）
        "annualized_turnover": ann_turnover,
        "annualized_turnover_one_way": (
            C.one_way_turnover(ann_turnover) if np.isfinite(ann_turnover) else float("nan")
        ),
        "total_cost_drag": total_cost_drag,
        # 🔴 年化成本拖累（累计值 ÷ 年数）；年数 = 0 / 非法 ⇒ NaN（**不编数字**）
        "annualized_cost_drag": (
            float(total_cost_drag / years) if years > 0 else float("nan")
        ),
    }
    if benchmark_nav is not None:
        bench = _nav_series(benchmark_nav)
        aligned = pd.concat([s.rename("nav"), bench.rename("bench")], axis=1, join="inner").dropna()
        if len(aligned) >= 2 and float(aligned["bench"].iloc[0]) > 0.0:
            excess_nav = aligned["nav"] / aligned["bench"]
            out["excess_vs_benchmark"] = annualized_return(excess_nav, periods_per_year=periods_per_year)
        else:
            out["excess_vs_benchmark"] = float("nan")
    return out


# ----------------------------------------------------------------- 窗口切片 / 换手闸门
def _window_bound(value: Any, *, upper: bool) -> pd.Timestamp | None:
    """把窗口边界归一成 ``pd.Timestamp``（``None`` ⇒ 该侧不设限）。

    口径：**按日期闭区间** —— 起点 = 当日零点（左闭），终点 = 次日零点（右开）
    ⇒ 终点当天的全部时点都算在窗口内（**不依赖**索引是否带时分）。
    """
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if upper:
        return ts.normalize() + pd.DateOffset(days=1)
    return ts.normalize()


def _align_tz(ts: pd.Timestamp, index: pd.DatetimeIndex) -> pd.Timestamp:
    """把边界时间戳对齐到索引的时区（naive ⇄ aware 混用会直接抛 ``TypeError``）。"""
    if index.tz is None and ts.tzinfo is not None:
        return ts.tz_localize(None)
    if index.tz is not None and ts.tzinfo is None:
        return ts.tz_localize(index.tz)
    return ts


def _as_dated_series(obj: Any, *, what: str) -> pd.Series:
    """把带日期索引的 Series 归一成「``DatetimeIndex`` 的 Series」（不丢值、不改数值）。"""
    if isinstance(obj, pd.DataFrame):
        obj = obj.iloc[:, 0]
    if not isinstance(obj, pd.Series):
        raise TypeError(f"{what} 必须是 pd.Series（index=DatetimeIndex）；实得 {type(obj)!r}")
    if not isinstance(obj.index, pd.DatetimeIndex):
        try:
            obj = obj.set_axis(pd.DatetimeIndex(pd.to_datetime(obj.index)), axis=0)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"{what} 的 index 必须能解析成 DatetimeIndex（日期索引）；实得 {obj.index!r}"
            ) from exc
    return obj


def _turnover_key_strings(index: pd.Index) -> pd.Index:
    """把换手索引统一成「日期字符串」（``DatetimeIndex`` ⇒ ``%Y-%m-%d``；其余逐个 ``str()``）。"""
    if isinstance(index, pd.DatetimeIndex):
        return pd.Index([ts.strftime("%Y-%m-%d") for ts in index])
    return pd.Index([str(k) for k in index])


def _parse_turnover_keys(keys: pd.Index) -> pd.DatetimeIndex:
    """把日期字符串索引解析成 ``DatetimeIndex``（供窗口比较）。

    🔴 **解析失败直接抛错**：静默丢掉换手 = 少算换手 = 年化换手被低估 ⇒ 闸门失效。
    """
    if len(keys) == 0:
        return pd.DatetimeIndex([])
    parsed = pd.to_datetime(keys, errors="coerce")
    bad = [str(k) for k, p in zip(keys, parsed) if pd.isna(p)]
    if bad:
        raise ValueError(
            f"换手索引里无法解析成日期的键：{bad[:5]!r} ⇒ 不许静默丢换手（会低估年化换手）"
        )
    return pd.DatetimeIndex(parsed)


def slice_window(
    nav: pd.Series,
    turnover: Any = None,
    benchmark: pd.Series | None = None,
    start: Any = None,
    end: Any = None,
) -> tuple[pd.Series, pd.Series | None, pd.Series | None]:
    """把净值 / 换手 / 基准**同时**切到 ``[start, end]`` 闭区间。

    🔴 **为什么必须有这个函数**（审计实测）：val 段超参搜索曾把**整段历史净值**
    （2020-01-02 → 2026-11-10）交给 ``summarize()`` 求年化 ⇒ 指标窗口与 val 声明
    **脱钩**（含测试段收益 ⇒ 违反红线）；且 ``annualized_turnover`` 的分母 ``years``
    由**传入 nav 的长度**决定 ⇒ nav 全史 + 换手仅 val 段 ⇒ **年化换手被低估约 3.4 倍**
    （实测：objective 报 4.99×，val 段真值 17.13×）⇒ 「换手 ≤ 4×」目标失效。
    本函数保证三者**落在同一个窗口**（杜绝「分子 val / 分母全史」）。

    参数
    ----
    nav : 净值序列（Series；index 须是 ``DatetimeIndex`` 或可解析成日期的索引）。
    turnover : 换手（``dict[str, float]`` / ``pd.Series`` / ``None``）——
        输出统一为 ``pd.Series``（index = 日期字符串 ``%Y-%m-%d``）。
    benchmark : 基准净值（Series / ``None``）——``None`` ⇒ 输出 ``None``。
    start / end : 窗口边界（``str`` / ``pd.Timestamp`` / ``None``）——
        ``None`` ⇒ 该侧不设限；两侧都 ``None`` ⇒ **原样返回**（不切片、不改数值）；
        口径为**按日期闭区间**（含终点当天的全部时点）。

    返回
    ----
    ``(nav_sliced, turnover_sliced_or_None, benchmark_sliced_or_None)``。
    """
    nav_s = _as_dated_series(nav, what="nav")
    start_ts = _window_bound(start, upper=False)
    end_ts = _window_bound(end, upper=True)

    def _mask(index: pd.DatetimeIndex) -> np.ndarray:
        """按窗口上下界生成布尔掩码（闭区间：起点含、终点含当日全天）。

        🔴 上下界先做时区对齐再比较 ⇒ 避免「带时区/不带时区」混比导致整段被切空。
        """
        mask = np.ones(len(index), dtype=bool)
        if start_ts is not None:
            mask &= index.normalize() >= _align_tz(start_ts, index)
        if end_ts is not None:
            mask &= index.normalize() < _align_tz(end_ts, index)
        return mask

    nav_out = nav_s[_mask(nav_s.index)]

    turn_out: pd.Series | None = None
    if turnover is not None:
        turn_series = _turnover_series(turnover)
        keys = _turnover_key_strings(turn_series.index)
        turn_series = pd.Series(turn_series.to_numpy(dtype="float64"), index=keys)
        if start_ts is not None or end_ts is not None:
            turn_series = turn_series[_mask(_parse_turnover_keys(keys))]
        turn_out = turn_series

    bench_out: pd.Series | None = None
    if benchmark is not None:
        bench_s = _as_dated_series(benchmark, what="benchmark")
        bench_out = bench_s[_mask(bench_s.index)]

    return nav_out, turn_out, bench_out


def check_turnover_target(
    metrics: dict,
    target: float | None = None,
    *,
    label: str = "",
    mode: str = "gate",
) -> dict:
    """换手目标检查（**两种模式**：硬闸门 / 只披露）。

    🔴 **两种模式的区别**（用户 2026-11-18 拍板：废除月频策略的「≤4× 硬拦截」）：

    ==============  ==========================  ==========================
    ``mode``        语义                          返回键
    ==============  ==========================  ==========================
    ``"gate"``      **硬闸门**（旧行为，默认）      ``{"ok", "annualized_turnover",
                                          "target", "message"}``
    ``"disclosure"`` **只披露**，**永不拦截**      同上 **+ ``{"blocking": False}``**
    ==============  ==========================  ==========================

    * ``mode="gate"`` ⇒ 返回结构与**旧版逐字一致**（4 个键，**不含** ``blocking``），
      且语义仍是**硬闸门**（即 blocking 行为为 True）⇒
      调用方若要统一读 blocking，请用 ``res.get("blocking", True)``；
    * ``mode="disclosure"`` ⇒ ``ok`` **如实**反映是否达标（**不粉饰**），
      但**永不抛错 / 永不判失败** ⇒ 多返回 ``"blocking": False``，
      ``message`` 改用「披露基准」措辞；
    * ``annualized_turnover`` 缺失或 NaN ⇒ ``ok=False``
      （🔴 **不许**把「算不出来」当成达标）；两种模式都**不抛错**。

    参数
    ----
    metrics : ``summarize()`` 的返回值（或任何含 ``annualized_turnover`` 的 dict）。
    target : 目标（``None`` ⇒ 取 ``config_v2.TURNOVER_TARGET``）。
    label : 报错信息前缀（例如 ``"val 段"``）。
    mode : ``"gate"``（默认，硬闸门）/ ``"disclosure"``（只披露）⇒ 其他值直接 ``ValueError``。
    """
    if mode not in ("gate", "disclosure"):
        raise ValueError(
            f"🔴 换手目标模式非法：{mode!r} ⇒ 只允许 'gate' / 'disclosure'"
        )
    target_value = float(C.TURNOVER_TARGET) if target is None else float(target)
    prefix = f"{label} " if label else ""
    raw = metrics.get("annualized_turnover") if isinstance(metrics, dict) else None
    value: float | None = None
    if raw is not None:
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            candidate = float("nan")
        if np.isfinite(candidate):
            value = candidate
    if value is None:
        if mode == "disclosure":
            return {
                "ok": False,
                "annualized_turnover": None,
                "target": target_value,
                "message": (
                    f"{prefix}换手披露基准：年化换手**算不出来**（缺失 / NaN / 非有限）"
                    f"⇒ 不许当成达标；披露基准 ≤ {target_value:.3f}×"
                    f"（**仅披露，不构成拦截**）"
                ),
                "blocking": False,
            }
        return {
            "ok": False,
            "annualized_turnover": None,
            "target": target_value,
            "message": (
                f"{prefix}换手闸门：年化换手**算不出来**（缺失 / NaN / 非有限）"
                f"⇒ 不许当成达标；目标 ≤ {target_value:.3f}×"
            ),
        }
    ok = bool(value <= target_value)
    if mode == "disclosure":
        detail = "达标" if ok else f"未达标（超出 {value - target_value:.3f}×）"
        # 🔴 2026-11-18 续四：单边换算**必须走唯一实现**（不许手写系数）。
        one_way = C.one_way_turnover(value)
        message = (
            f"{prefix}换手披露基准：年化换手（双边）{value:.3f}× vs 披露基准 "
            f"{target_value:.3f}× ⇒ {detail}（单边 {one_way:.3f}×）"
            f"｜**仅作披露基准，不构成拦截**"
        )
        return {
            "ok": ok,
            "annualized_turnover": value,
            "target": target_value,
            "message": message,
            "blocking": False,
        }
    if ok:
        message = f"{prefix}换手闸门：年化换手 {value:.3f}× ≤ 目标 {target_value:.3f}× ⇒ 达标"
    else:
        message = (
            f"{prefix}换手闸门：年化换手 {value:.3f}× 超目标 {target_value:.3f}×"
            f"（超出 {value - target_value:.3f}×）⇒ 未达标"
        )
    return {
        "ok": ok,
        "annualized_turnover": value,
        "target": target_value,
        "message": message,
    }
