"""ETF 穿透分 ``E = W × S`` 的**时间维度平滑**（EWMA / 滚动均值）。

【为什么需要这个模块】
``E = W × S`` 是**单日截面快照**：S 层每次刷新都重打分 ⇒ E 每次都变 ⇒ 截面密集区
（``z_alpha`` 互相咬得很紧）的 ETF 排名**逐日洗牌** ⇒ 大量「虚警」与高频换仓假象。
本模块把 E 放到**交易日轴**上做时间滤波，提取有**持续性（persistence）**的 Alpha。

【时间轴口径（关键，错了就等于没做）】
1. **交易日轴** = 调用方传入的 ``trading_days``（升序）⇒ 平滑的衰减单位是**交易日**，
   不是「打分点」。打分栅格是**每 10 个交易日一个点**（``REBALANCE_EVERY_N_DAYS=10``），
   若把窗口算成「5 个打分点」⇒ 实际跨 50 个交易日 ⇒ 过度平滑、迟钝到不能交易。
2. **LOCF（末值前向填充）** = 两个打分点之间 E 沿用上一个观测值（阶梯函数）⇒
   EWMA 的「记忆」按**交易日**自然衰减：旧观测点的权重每交易日乘 ``(1-α)``。
3. 🔴 **W 没有历史**（``etf_holdings.csv`` 只有 1 个 ``fetch_date`` 快照）⇒ 历史 E 是
   「用今天的成分股回算过去」⇒ **带成分股前视偏差**。这不是本模块引入的新问题
   （回测口径本就如此），但平滑把它**放大到更多天** ⇒ 必须披露。

【失败即回退，绝不编数字】
* 单只标的观测点不足 ⇒ 平滑退化为**单日瞬态分**（``smoothed_score = raw_score``），
  并逐只标记 ``fallback_to_transient=True``；
* 全局一个历史观测点都没有 ⇒ 打印 ``WARNING: Fallback to transient score``；
* 稳定性算不出来（有效点 < 2）⇒ 写 ``None``（**不写 0**）。

【EWMA 的半衰期（别照抄需求文档里的数字）】
``half_life = ln(0.5) / ln(1-α)``。``α=0.35`` ⇒ **1.60 个交易日**，
而需求文档写的是「3~5 个交易日」（那需要 ``α≈0.21``）。本模块**按实际算出来的值披露**，
不把文档措辞当成结论。
"""
from __future__ import annotations

import math
import sys
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine.weights import map_scores_to_etfs  # noqa: E402

#: 支持的平滑方法
SMOOTHING_METHODS: tuple[str, ...] = ("ewma", "rolling")

#: 默认平滑方法（需求指定：EWMA）
DEFAULT_SMOOTHING_METHOD = "ewma"

#: 默认滚动窗口（交易日）；需求指定 5 ⇒ 覆盖一个完整交易周
DEFAULT_SMOOTHING_WINDOW = 5

#: 默认 EWMA 系数；需求指定 0.35（实际半衰期约 1.60 个交易日，见模块 docstring）
DEFAULT_SMOOTHING_ALPHA = 0.35

#: 默认取多少个**交易日**的历史做 EWMA 预热（够 EWMA 收敛 ⇒ 结果与「无限历史」一致）
DEFAULT_HISTORY_SPAN_DAYS = 30

#: 全局回退时的日志字面量（探针按此字面量断言，不许改写）
FALLBACK_WARNING = "WARNING: Fallback to transient score"

#: 平滑结果表的标准列序（探针按此断言）
RESULT_COLUMNS: tuple[str, ...] = (
    "code",
    "raw_score",
    "smoothed_score",
    "score_stability",
    "n_obs",
    "last_obs_date",
    "days_since_last_obs",
    "fallback_to_transient",
)


@dataclass(frozen=True)
class SmoothingConfig:
    """平滑参数（单一事实源在 :mod:`config`，此处只做校验与传递）。"""

    enabled: bool = True
    method: str = DEFAULT_SMOOTHING_METHOD
    window: int = DEFAULT_SMOOTHING_WINDOW
    alpha: float = DEFAULT_SMOOTHING_ALPHA
    history_span_days: int = DEFAULT_HISTORY_SPAN_DAYS

    def validated(self) -> "SmoothingConfig":
        """校验参数；非法值直接抛中文异常（宁可不产出，不产出假结果）。"""
        method = str(self.method).strip().lower()
        if method not in SMOOTHING_METHODS:
            raise ValueError(
                f"平滑方法非法：{self.method!r} ⇒ 只支持 {list(SMOOTHING_METHODS)}"
            )
        if int(self.window) < 1:
            raise ValueError(f"平滑窗口必须 ≥ 1 个交易日，当前 = {self.window}")
        if not (0.0 < float(self.alpha) <= 1.0):
            raise ValueError(f"EWMA 系数必须落在 (0, 1]，当前 = {self.alpha}")
        if int(self.history_span_days) < 1:
            raise ValueError(f"历史预热跨度必须 ≥ 1 个交易日，当前 = {self.history_span_days}")
        return SmoothingConfig(
            enabled=bool(self.enabled),
            method=method,
            window=int(self.window),
            alpha=float(self.alpha),
            history_span_days=int(self.history_span_days),
        )


@dataclass
class SmoothingOutcome:
    """平滑结果。

    ``alpha`` 表（每只 ETF 一行）的列：
    ``etf_code`` / ``etf_name`` / ``alpha_score``（原始 E）/ ``smoothed_score`` /
    ``score_stability`` / ``n_obs`` / ``last_obs_date`` / ``days_since_last_obs`` /
    ``fallback_to_transient`` / ``coverage`` / ``coverage_ok`` / ``n_stocks_scored``。
    """

    alpha: pd.DataFrame
    diagnostics: dict[str, Any] = field(default_factory=dict)


def effective_half_life_days(alpha: float) -> float:
    """EWMA 半衰期（交易日）：``ln(0.5) / ln(1-α)``；``α=1`` 时没有衰减 ⇒ 记 0。"""
    alpha = float(alpha)
    if not (0.0 < alpha < 1.0):
        return 0.0
    return math.log(0.5) / math.log(1.0 - alpha)


def _normalize_axis(trading_days: Sequence[pd.Timestamp]) -> list[pd.Timestamp]:
    """把传入的交易日整理成升序去重列表。"""
    out: list[pd.Timestamp] = []
    for day in trading_days:
        stamp = pd.Timestamp(day).normalize()
        if stamp not in out:
            out.append(stamp)
    return sorted(out)


def _locf_array(
    obs_dates: Sequence[pd.Timestamp],
    obs_values: Sequence[float],
    axis: list[pd.Timestamp],
) -> tuple[np.ndarray, int, int | None, pd.Timestamp | None]:
    """把稀疏观测摊到交易日轴上（LOCF 阶梯函数）。

    返回 ``(locf, n_obs_used, last_obs_index, last_obs_date)``：
    ``locf[i]`` = 轴上第 i 个交易日的 E（首个观测之前为 NaN）；
    ``last_obs_index`` / ``last_obs_date`` = 最后一个**真正用上**的观测。
    """
    locf = np.full(len(axis), np.nan, dtype=float)
    if not axis:
        return locf, 0, None, None
    stamped: list[tuple[int, float, pd.Timestamp]] = []
    for date, value in zip(obs_dates, obs_values):
        if value is None or not np.isfinite(float(value)):
            continue
        day = pd.Timestamp(date).normalize()
        pos = bisect_left(axis, day)
        if pos >= len(axis):
            # 观测日在轴的末端之后 ⇒ 不参与本轮平滑
            continue
        stamped.append((pos, float(value), day))
    if not stamped:
        return locf, 0, None, None
    stamped.sort(key=lambda item: item[0])
    # 同一格多次观测 ⇒ 保留**较新**的那个（日期更晚 ⇒ 覆盖）
    per_index: dict[int, tuple[float, pd.Timestamp]] = {}
    for pos, value, day in stamped:
        per_index[pos] = (value, day)
    ordered = sorted(per_index.items())
    current = np.nan
    cursor = 0
    for index in range(len(axis)):
        while cursor < len(ordered) and ordered[cursor][0] == index:
            current = ordered[cursor][1][0]
            cursor += 1
        locf[index] = current
    last_pos, last_pair = ordered[-1]
    return locf, len(stamped), last_pos, last_pair[1]


def smooth_panel(
    observations: pd.DataFrame,
    axis: Sequence[pd.Timestamp],
    method: str = DEFAULT_SMOOTHING_METHOD,
    window: int = DEFAULT_SMOOTHING_WINDOW,
    alpha: float = DEFAULT_SMOOTHING_ALPHA,
) -> pd.DataFrame:
    """把稀疏的 ``(date, code, score)`` 观测摊到交易日轴后做时间平滑。

    这是本模块的**纯函数核心**：不碰文件、不碰网络，探针直接对它造假数据。

    返回列：``code`` / ``raw_score`` / ``smoothed_score`` / ``score_stability`` /
    ``n_obs`` / ``last_obs_date`` / ``days_since_last_obs`` / ``fallback_to_transient``。
    """
    config = SmoothingConfig(
        enabled=True, method=method, window=window, alpha=alpha
    ).validated()
    axis_list = _normalize_axis(axis)
    if observations is None or len(observations) == 0 or not axis_list:
        return pd.DataFrame(columns=list(RESULT_COLUMNS))
    frame = observations.copy()
    for required in ("date", "code", "score"):
        if required not in frame.columns:
            raise ValueError(
                f"平滑输入缺列 `{required}`（要求 date / code / score 三列）"
                "⇒ 拒绝计算（宁可不产出，不产出假结果）"
            )
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    rows: list[dict[str, Any]] = []
    for code, group in frame.groupby("code", sort=True):
        group = group.sort_values("date")
        obs_dates = list(group["date"])
        obs_values = list(group["score"])
        locf, n_obs, last_index, last_date = _locf_array(obs_dates, obs_values, axis_list)
        finite = np.isfinite(locf)
        raw_value = float(locf[-1]) if finite.any() else None
        smoothed = np.full(len(axis_list), np.nan, dtype=float)
        if config.method == "ewma":
            state = np.nan
            for index in range(len(axis_list)):
                value = locf[index]
                if not np.isfinite(value):
                    continue
                state = (
                    value
                    if not np.isfinite(state)
                    else config.alpha * value + (1.0 - config.alpha) * state
                )
                smoothed[index] = state
        else:
            for index in range(len(axis_list)):
                start = max(0, index - config.window + 1)
                chunk = locf[start : index + 1]
                usable = chunk[np.isfinite(chunk)]
                if usable.size:
                    smoothed[index] = float(usable.mean())
        tail = locf[max(0, len(axis_list) - config.window) :]
        tail_finite = tail[np.isfinite(tail)]
        # 有效点不足 2 ⇒ 稳定性**算不出来** ⇒ 写 None（不写 0）
        stability = float(tail_finite.std(ddof=0)) if tail_finite.size >= 2 else None
        smoothed_value = float(smoothed[-1]) if np.isfinite(smoothed[-1]) else None
        fallback = bool(n_obs <= 1)
        if fallback and smoothed_value is None:
            # 历史全无 ⇒ 退化为单日瞬态分
            smoothed_value = raw_value
        rows.append(
            {
                "code": str(code),
                "raw_score": raw_value,
                "smoothed_score": smoothed_value,
                "score_stability": stability,
                "n_obs": int(n_obs),
                "last_obs_date": (
                    last_date.date().isoformat() if last_date is not None else None
                ),
                "days_since_last_obs": (
                    int(len(axis_list) - 1 - last_index) if last_index is not None else None
                ),
                "fallback_to_transient": fallback,
            }
        )
    return pd.DataFrame(rows, columns=list(RESULT_COLUMNS))


def _score_dates(scores: pd.DataFrame) -> list[pd.Timestamp]:
    """从个股分数表里取全部打分日（升序、去重）。"""
    if scores is None or len(scores) == 0 or "date" not in scores.columns:
        return []
    dates = pd.to_datetime(scores["date"]).dt.normalize()
    return sorted({stamp for stamp in dates.dropna().unique()})


def canonical_trading_axis(
    prices: pd.DataFrame | None,
    scores: pd.DataFrame | None,
    asof: pd.Timestamp,
) -> list[pd.Timestamp]:
    """**唯一**的交易日轴口径：ETF 行情日 ∪ 个股打分日（都 ≤ asof），升序去重。

    🔴 **为什么不用日历**（2026-11-09 全量闸门抓到的真缺陷的根因）：
    分析链（``analyze.py``）拿的是「qlib 日历 + akshare 日历」合并出来的日历，
    规则链（``recommend.py``）与回测链（``backtest.py``）未必拿到同一份日历
    ⇒ 各链各用自己的日历 ⇒ **轴长度不同 ⇒ EWMA 递推步长不同 ⇒ 平滑分不同**
    ⇒ 规则层与交付层必然分叉（闸门 A2「规则层与交付层入选同源」判红的真凶）。
    ⇒ 改成「**由数据本身派生**」：行情文件的日期 ∪ 打分文件的日期。
    各链读的是同一份 ``etf_prices.csv`` / ``stock_scores.csv`` ⇒ 轴必然逐位一致。

    参数
    ----
    prices : ETF 日线（含 ``date`` 列）；没有就退化成只用打分日
    scores : 个股分数（含 ``date`` 列）
    asof   : 信号日（含）
    """
    asof_stamp = pd.Timestamp(asof).normalize()
    stamps: set[pd.Timestamp] = set()
    for frame in (prices, scores):
        if frame is None or len(frame) == 0 or "date" not in frame.columns:
            continue
        dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
        for stamp in dates.unique():
            day = pd.Timestamp(stamp).normalize()
            if day <= asof_stamp:
                stamps.add(day)
    return sorted(stamps)


def build_alpha_override(
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    prices: pd.DataFrame | None,
    asof: pd.Timestamp,
    config: SmoothingConfig,
    min_coverage: float = 0.5,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """**唯一**的平滑表生产口（分析链 / 规则链 / 引擎都走这里 ⇒ 结果逐位一致）。

    返回 ``(override, diagnostics)``：
    * ``override``：列 ``etf_code`` / ``smoothed_score`` / ``score_stability``
      ⇒ 直接喂给 :func:`apply_alpha_override`；
    * ``diagnostics``：审计用（观测日、半衰期、回退原因…）。

    🔴 各链**必须**走这个函数、**自己**派生交易日轴 ⇒ 只要输入文件一致 ⇒ 表格逐位一致
    ⇒ 「规则层与交付层入选同源」这条闸门才不会再次判红。
    """
    config = config.validated()
    axis = canonical_trading_axis(prices, scores, asof)
    outcome = compute_smoothed_alpha(holdings, scores, asof, axis, config, min_coverage)
    override = outcome.alpha[["etf_code", "smoothed_score", "score_stability"]].copy()
    return override, dict(outcome.diagnostics)


def apply_alpha_override(
    frame: pd.DataFrame, override: pd.DataFrame | None
) -> pd.DataFrame:
    """把平滑分并进候选表（**分析侧与引擎侧共用这一份** ⇒ 两份公式不会漂移）。

    新增/覆盖三列：
    * ``smoothed_score`` = 平滑后的穿透分（没给覆盖表时 = 原始分）；
    * ``score_stability`` = 平滑窗口内的得分标准差（**算不出来 = None**，不写 0）；
    * ``alpha_score_used`` = **实际参与排序**的那一份（平滑分优先，缺值退回原始分）。

    🔴 ``alpha_score``（原始 E）**永远不改** ⇒ 报告与 LLM 上下文按原始分对外披露；
    排序用平滑分这件事由 ``alpha_score_used`` 单独承担 ⇒ 口径可对账。
    """
    out = frame.copy()
    if "alpha_score" in out.columns:
        out["alpha_score_raw"] = pd.to_numeric(out["alpha_score"], errors="coerce")
    else:
        out["alpha_score_raw"] = np.nan
    if override is None or len(override) == 0:
        out["smoothed_score"] = out["alpha_score_raw"]
        # 没有覆盖表 ⇒ 稳定性**算不出来** ⇒ 写 None（不写 0）
        out["score_stability"] = None
        out["alpha_score_used"] = out["alpha_score_raw"]
        return out
    table = override.copy()
    table["etf_code"] = table["etf_code"].astype(str)
    smooth_map = dict(
        zip(table["etf_code"], pd.to_numeric(table["smoothed_score"], errors="coerce"))
    )
    if "score_stability" in table.columns:
        stability_map = dict(zip(table["etf_code"], table["score_stability"]))
    else:
        stability_map = {}
    out["smoothed_score"] = out["code"].astype(str).map(smooth_map).astype(float)
    out["score_stability"] = out["code"].astype(str).map(stability_map)
    # 平滑分缺值 ⇒ 退回该只的原始分（**不写 0**，不编数字）
    out["alpha_score_used"] = out["smoothed_score"].where(
        out["smoothed_score"].notna(), out["alpha_score_raw"]
    )
    return out


def _transient_only(
    raw_alpha: pd.DataFrame,
    asof_stamp: pd.Timestamp,
    reason: str,
    diagnostics: dict[str, Any],
) -> SmoothingOutcome:
    """全程退化成**单日瞬态分**（历史不可用时的唯一合法出口）。"""
    result = raw_alpha.copy()
    result["smoothed_score"] = result["alpha_score"]
    result["score_stability"] = None
    result["n_obs"] = 1
    result["last_obs_date"] = asof_stamp.date().isoformat()
    result["days_since_last_obs"] = 0
    result["fallback_to_transient"] = True
    diagnostics["fallback_reason"] = reason
    diagnostics["warning"] = FALLBACK_WARNING
    diagnostics["n_codes_smoothed"] = int(len(result))
    diagnostics["n_codes_fallback_transient"] = int(len(result))
    return SmoothingOutcome(alpha=result, diagnostics=diagnostics)


def compute_smoothed_alpha(
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    asof: pd.Timestamp,
    trading_days: Sequence[pd.Timestamp],
    config: SmoothingConfig,
    min_coverage: float = 0.5,
) -> SmoothingOutcome:
    """算出 asof 当日的 ETF 穿透分，并把**时间平滑分**一起给出来。

    步骤：
    1. 用 ``map_scores_to_etfs`` 重算**历史观测日**的 E（只重映射，不打分、不联网）；
    2. 把观测摊到交易日轴（LOCF）后做 EWMA / 滚动均值；
    3. 输出原始分、平滑分、稳定性与逐只回退标记。

    关闭平滑（``config.enabled=False``）⇒ 原样返回瞬态分，``smoothed_score = raw_score``。
    """
    config = config.validated()
    asof_stamp = pd.Timestamp(asof).normalize()
    if scores is None or len(scores) == 0 or "date" not in scores.columns:
        raise ValueError(
            "个股分数表缺 `date` 列或为空 ⇒ E = W×S 算不出来 ⇒ 拒绝计算（不编数字）"
        )
    raw_alpha = map_scores_to_etfs(holdings, scores, asof_stamp, min_coverage=min_coverage)
    raw_alpha = raw_alpha.copy()
    raw_alpha["etf_code"] = raw_alpha["etf_code"].astype(str)
    raw_alpha["alpha_score"] = pd.to_numeric(raw_alpha["alpha_score"], errors="coerce")

    base_columns = [
        "etf_code", "etf_name", "alpha_score", "coverage",
        "coverage_ok", "n_stocks_scored",
    ]
    axis_all = _normalize_axis(trading_days)
    axis_upto = [day for day in axis_all if day <= asof_stamp]
    if not axis_upto:
        # 没有交易日历可用 ⇒ 只能用打分日当轴（退化但不崩）
        axis_upto = [day for day in _score_dates(scores) if day <= asof_stamp]
    span = min(config.history_span_days, len(axis_upto)) if axis_upto else 0
    axis = axis_upto[-span:] if span else []

    diagnostics: dict[str, Any] = {
        "enabled": bool(config.enabled),
        "method": config.method if config.enabled else None,
        "alpha": config.alpha if config.enabled else None,
        "window": config.window,
        "history_span_trading_days": int(span),
        "axis_start": axis[0].date().isoformat() if axis else None,
        "axis_end": axis[-1].date().isoformat() if axis else None,
        "axis_len": len(axis),
        "effective_half_life_trading_days": (
            round(effective_half_life_days(config.alpha), 4) if config.enabled else None
        ),
        "observation_dates": [],
        "n_observation_dates": 0,
        "n_codes_raw": int(len(raw_alpha)),
        "n_codes_smoothed": 0,
        "n_codes_fallback_transient": 0,
        "n_codes_no_observation": 0,
        "fallback_reason": None,
        "warning": None,
        "w_snapshot_note": (
            "🔴 历史 E 是「用当前成分股快照回算过去」⇒ 带成分股前视偏差"
            "（etf_holdings.csv 只有 1 个 fetch_date 快照，无历史）"
        ),
    }

    if not config.enabled:
        diagnostics["fallback_reason"] = "smoothing_disabled"
        return _transient_only(raw_alpha[base_columns], asof_stamp, "smoothing_disabled", diagnostics)

    all_score_dates = _score_dates(scores)
    axis_set = set(axis)
    obs_dates = [day for day in all_score_dates if day in axis_set] if axis else []
    if not obs_dates:
        # 轴与打分日对不上 ⇒ 退化成「只有 asof 一个观测点」
        candidates = [day for day in all_score_dates if day <= asof_stamp]
        obs_dates = candidates[-1:] if candidates else []
    observations_rows: list[pd.DataFrame] = []
    used_dates: list[str] = []
    for day in obs_dates:
        try:
            snapshot = map_scores_to_etfs(
                holdings, scores, day, min_coverage=min_coverage
            )
        except ValueError:
            # 该打分日没有个股分数 ⇒ 跳过（不编数字）
            continue
        snapshot = snapshot.copy()
        snapshot["date"] = day
        observations_rows.append(snapshot[["date", "etf_code", "alpha_score"]])
        used_dates.append(day.date().isoformat())
    if not observations_rows:
        return _transient_only(raw_alpha[base_columns], asof_stamp, "no_observation", diagnostics)

    observations = pd.concat(observations_rows, ignore_index=True)
    observations["score"] = pd.to_numeric(observations["alpha_score"], errors="coerce")
    smoothed = smooth_panel(
        observations[["date", "etf_code", "score"]].rename(columns={"etf_code": "code"}),
        axis,
        method=config.method,
        window=config.window,
        alpha=config.alpha,
    )
    result = raw_alpha[base_columns].merge(
        smoothed.rename(columns={"code": "etf_code"}), on="etf_code", how="left"
    )
    # 平滑分缺值 ⇒ 退回该只的原始分（**不写 0**，不编数字）
    result["smoothed_score"] = result["smoothed_score"].where(
        result["smoothed_score"].notna(), result["alpha_score"]
    )
    # 🔴 合并后仍缺值的标的 = **平滑表里根本没有它的行** ⇒ 观测点真数是 **0**（不是 1）。
    #    「距上次观测几天」「上次观测日」此时**算不出来** ⇒ 留空（None），**不许写 0 蒙混**
    #    （铁律十四.57：不许用 fillna(0) 掩盖算不出来的数字）。
    result["fallback_to_transient"] = result["fallback_to_transient"].where(
        result["fallback_to_transient"].notna(), True
    ).astype(bool)
    result["n_obs"] = result["n_obs"].where(result["n_obs"].notna(), 0).astype(int)
    # 距上次观测的交易日数：缺值保持 NaN ⇒ JSON 落 null（**不写 0**）
    result["days_since_last_obs"] = pd.to_numeric(
        result["days_since_last_obs"], errors="coerce"
    )

    n_usable = int(result["smoothed_score"].notna().sum())
    n_fallback = int(result["fallback_to_transient"].sum())
    diagnostics.update(
        {
            "observation_dates": used_dates,
            "n_observation_dates": len(used_dates),
            "n_codes_smoothed": n_usable,
            "n_codes_fallback_transient": n_fallback,
            "n_codes_no_observation": int(result["alpha_score"].isna().sum()),
        }
    )
    history_dates = [day for day in used_dates if day != asof_stamp.date().isoformat()]
    if not history_dates:
        diagnostics["fallback_reason"] = "no_history"
        diagnostics["warning"] = FALLBACK_WARNING
    return SmoothingOutcome(alpha=result, diagnostics=diagnostics)
