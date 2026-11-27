"""🔴 V2 ETF 层：``E = W × S`` → 过滤 → 排序 → **Rank Buffer** → 目标权重。

**V2 独立分支**：本文件**不 import、不改写** V1 的 ``engine/*`` / ``analyze.py``
（V1 冻结为 baseline）。口径**按 V1 逐项复刻**，差异只有三处，且**都在披露清单里**：

  ==========  ============================================  ===========================
  维度        V1                                          V2（本文件）
  ==========  ============================================  ===========================
  合成        ``w_alpha·z(E) + w_mom·z(momentum_trend_adj)``  **完全一致**
  过滤        流动性 / 覆盖率 / 入场 MA / 退场 MA / 同族上限   **完全一致**（同族上限简化版）
  迟滞        只在规则层有 ``HOLD_BUFFER``                **Rank Buffer 前移到 ETF 选股层**
  换手口径    上期**目标**权重（未漂移）                    **调仓前漂移权重**（更严，见 backtest_v2）
  ==========  ============================================  ===========================

🔴 **前视偏差（必须披露，不许藏）**：
   本地只有 **2026-11-04 单快照**的 ETF 成分股/权重
   （akshare ``fund_etf_holdings_tx`` / ``fund_etf_pcf`` 都不提供历史 PCF）
   ⇒ 历史成分变更**不可观测** ⇒ 回测用 2026 权重倒推历史。
   见 ``constituents.lookahead_bias_bound``（给出偏差上界）与 ``caliber_note()``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import smoothing as SM  # noqa: E402  —— 决策③：下游 EWMA / Rank Buffer 三臂

try:
    from . import backtest_v2  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import backtest_v2  # type: ignore[no-redef]

__all__ = [
    "FAMILY_KEYWORDS",
    "load_etf_prices",
    "build_weight_map",
    "zscore",
    "compute_features",
    "compute_alpha_panel",
    "neg_score_exposure",
    "build_targets",
]

#: 🔴 同族关键词（**照抄 V1** ``config.THEME_FAMILY_KEYWORDS``；V2 不 import V1 ⇒ 抄一份）
FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "红利": ("红利",),
    "银行": ("银行",),
    "券商": ("券商", "证券"),
    "医药": ("医药", "创新药", "医疗", "生物医药"),
    "半导体": ("半导体", "芯片"),
    "军工": ("军工", "国防"),
    "新能源": ("光伏", "电池", "新能源"),
    "资源": ("有色", "稀土", "煤炭", "钢铁", "资源"),
    "地产": ("地产", "房地产"),
    "消费": ("食品", "白酒", "消费"),
    "通信": ("通信", "云计算", "人工智能"),
    "机器人": ("机器人",),
    "电力": ("电力", "公用", "基建"),
    "农业": ("农业", "养殖"),
    "港股": ("港股",),
    "纳指": ("纳指",),
    "标普": ("标普",),
    "日经": ("日经",),
}

#: 动量权重（**照抄 V1** ``config.MOMENTUM_WEIGHTS``）
MOMENTUM_WEIGHTS: dict[int, float] = {20: 0.2, 60: 0.5, 120: 0.3}
#: 趋势均线窗口（**照抄 V1** ``config.MA_TREND_WINDOW``）
MA_TREND_WINDOW = 60


# ----------------------------------------------------------------- 取数
def load_etf_prices(path: str | Path | None = None) -> pd.DataFrame:
    """读 ETF 日线（长表：date, code, close, amount…）。"""
    p = Path(path or C.ETF_PRICES_CSV)
    df = pd.read_csv(p, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def build_weight_map(holdings: pd.DataFrame) -> dict[str, dict[str, float]]:
    """``{ETF 代码: {成分股 qlib instrument: 权重%}}``。

    🔴 只用**正权重**行；成分股代码统一转成 qlib instrument 形式
       （``600000.SH`` → ``SH600000``），与 S 层分数的 instrument 对齐。
    """
    import constituents as CON

    df = holdings.copy()
    df["etf_code"] = df["etf_code"].astype(str).str.zfill(6)
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce")
    df = df[df["weight_pct"] > 0].copy()
    if "instrument" not in df.columns:
        df["instrument"] = df["stock_code"].map(lambda c: CON._to_qlib_instrument(str(c)))
    df = df[df["instrument"].notna()]
    out: dict[str, dict[str, float]] = {}
    for code, grp in df.groupby("etf_code", sort=True):
        weights: dict[str, float] = {}
        for inst, w in zip(grp["instrument"], grp["weight_pct"]):
            weights[str(inst)] = weights.get(str(inst), 0.0) + float(w)
        out[str(code)] = weights
    return out


def zscore(values: pd.Series) -> pd.Series:
    """截面 z-score（std=0 ⇒ 全 0，中性分）。"""
    v = values.astype(float)
    mu = v.mean()
    sd = v.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=v.index)
    return (v - mu) / sd


# ----------------------------------------------------------------- 特征
def _ma_window_set(ma_windows: Sequence[int] | None) -> list[int]:
    """要算的均线窗口（**永远含** ``MA_TREND_WINDOW`` ⇒ 趋势调整恒有 ``ma60``）。"""
    wins = {int(w) for w in (ma_windows or ()) if int(w) > 0}
    wins.add(int(MA_TREND_WINDOW))
    return sorted(wins)


def _ma_panel(px: pd.DataFrame, window: int) -> pd.DataFrame:
    """``MA{window}`` 面板（**只看回看窗口** ⇒ 无未来函数）。"""
    win = int(window)
    return px.rolling(win, min_periods=win).mean()


def _momentum_weight_set(
    momentum_weights: Mapping[int, float] | None = None,
) -> dict[int, float]:
    """动量窗口 → 权重（**默认 = 模块常量** ⇒ 默认路径逐位不变）。

    🔴 ``None`` ⇒ 直接返回 :data:`MOMENTUM_WEIGHTS` **本体**（不复制、不重排）
       ⇒ 迭代顺序与浮点求和顺序都与旧口径**逐位一致**。
    🔴 给了覆盖 ⇒ 按**传入顺序**返回（求和顺序 = 调用方给定的顺序 ⇒ 可复现）。
    """
    if momentum_weights is None:
        return MOMENTUM_WEIGHTS
    return {int(k): float(v) for k, v in momentum_weights.items()}


def compute_features(
    close: pd.DataFrame,
    amount: pd.DataFrame | None = None,
    ma_windows: Sequence[int] = (MA_TREND_WINDOW,),
    *,
    momentum_weights: Mapping[int, float] | None = None,
) -> dict[str, pd.DataFrame]:
    """按**全历史**向量化算特征（全部只含回看窗口 ⇒ 无未来函数）。

    ``ma_windows`` = **要算的均线窗口**（默认只有 ``MA_TREND_WINDOW``=60 ⇒ 与旧口径**逐位一致**）。
    每个窗口产出 ``ma{w}`` 与 ``above_ma{w}``；``MA_TREND_WINDOW``（60）**永远**在算
    （趋势调整 ``momentum_score_trend_adj`` 恒用它 ⇒ 既有调用方行为不变）。

    返回 ``{"mom20","mom60","mom120","ma{w}","above_ma{w}","amount20d"}``（都是宽表）。

    🔴 ``momentum_weights`` = **动量窗口的可选显式覆盖**（``None`` = 用模块常量
       :data:`MOMENTUM_WEIGHTS` ⇒ **默认路径逐位不变**）；给了覆盖时 ``mom{w}``
       按**覆盖里的窗口**产出（供参数邻域扰动探针用）。
    """
    px = close.astype("float64").sort_index()
    out: dict[str, pd.DataFrame] = {}
    for win in _momentum_weight_set(momentum_weights):
        out[f"mom{win}"] = px / px.shift(win) - 1.0
    for win in _ma_window_set(ma_windows):
        ma = _ma_panel(px, win)
        out[f"ma{win}"] = ma
        out[f"above_ma{win}"] = px > ma
    if amount is not None:
        out["amount20d"] = amount.astype("float64").sort_index().rolling(20, min_periods=1).mean()
    else:
        out["amount20d"] = pd.DataFrame(np.nan, index=px.index, columns=px.columns)
    return out


# ----------------------------------------------------------------- 负分空仓保护
def neg_score_exposure(
    stat: float | None,
    scale: float = 0.05,
    min_exposure: float = 0.0,
) -> float:
    """🔴 负分空仓保护：横截面统计量 → **权益敞口**（``0`` ~ ``1``）。

    ``exposure = clip(1 + stat / scale, min_exposure, 1)``

    * ``stat`` = 候选 ETF 穿透分 E 的横截面统计量（默认中位数）；
      **负数 = 候选 ETF 的成分股整体跑输打分池均值**（弱势市况）；
    * ``scale`` = 敏感度（``stat = -scale`` ⇒ 敞口归零）；``<= 0`` ⇒ 闸门失效（返回 1）；
    * ``min_exposure`` = 敞口下限（不许清零 ⇒ 保留底仓）。

    🔴 **失效保护**：``stat`` 为 ``None`` / ``NaN`` ⇒ 返回 ``1.0``（**不许**因为
    统计量缺失就误清仓）。
    """
    try:
        s = float(stat)
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(s):
        return 1.0
    sc = float(scale)
    if not np.isfinite(sc) or sc <= 0.0:
        return 1.0
    lo = float(min_exposure)
    if not np.isfinite(lo):
        lo = 0.0
    lo = min(max(lo, 0.0), 1.0)
    return float(np.clip(1.0 + s / sc, lo, 1.0))


# ----------------------------------------------------------------- 穿透分面板
def compute_alpha_panel(
    scores: pd.DataFrame,
    weights: Mapping[str, Mapping[str, float]],
    dates: Sequence[pd.Timestamp],
    *,
    names: Mapping[str, str] | None = None,
    codes: Sequence[str] | None = None,
) -> tuple[pd.DataFrame, dict[pd.Timestamp, list[dict[str, Any]]]]:
    """逐**信号日**算穿透分 ``E = W × S``（面板：日期 × ETF 代码）。

    🔴 **口径（与 :func:`build_targets` 内部逐字一致 ⇒ 抽出来不影响生产行为）**：
      * 个股分数先做**当日截面 z-score**（``zscore`` ⇒ 均值 0 / 标准差 1）；
      * ``E = Σ(w_i · z_i) / Σ(w_i)``（**已命中的权重归一** ⇒ 覆盖率不足时 E 仍可比）；
      * ``coverage = Σ(w_i · 命中) / Σ(w_i)``（穿透率，< ``MIN_COVERAGE`` 的 ETF 不可穿透）；
      * 权重来自**单快照**（2026-11-04）⇒ 历史成分变更不可观测（前视偏差，见
        ``constituents.lookahead_bias_bound``）。

    参数
    ----
    scores : 长表 ``date/instrument/score``。
    weights : ``{ETF 代码: {成分股 instrument: 权重%}}``。
    dates : **信号日**（不是调仓日）列表。
    names : ``{代码: 名称}``（只写进明细表，不影响数值）。
    codes : **候选代码全集**（默认 = ``weights`` 的键）⇒ 不在此集合里的 ETF 不入面板。

    返回
    ----
    ``(e_panel, meta_by_date)``：
      * ``e_panel`` = ``DataFrame``（index=信号日，columns=ETF 代码，值 = E）；
      * ``meta_by_date`` = ``{信号日: [逐 ETF 明细 dict, ...]}``（含覆盖率 / 命中数）。
    """
    names = dict(names or {})
    signal_days = sorted({pd.Timestamp(d).normalize() for d in dates})
    code_set = set(str(c) for c in (codes if codes is not None else weights.keys()))
    scores = scores.copy()
    if "date" in scores.columns:
        scores["date"] = pd.to_datetime(scores["date"]).dt.normalize()
    scores_by_date: dict[pd.Timestamp, pd.DataFrame] = {
        pd.Timestamp(day): grp for day, grp in scores.groupby("date", sort=True)
    }

    meta_by_date: dict[pd.Timestamp, list[dict[str, Any]]] = {}
    for day in signal_days:
        day_scores = scores_by_date.get(day)
        if day_scores is None or day_scores.empty:
            meta_by_date[day] = []
            continue
        z = zscore(day_scores.set_index("instrument")["score"].astype(float))

        # ---- 穿透分 E = W × S ----
        rows: list[dict[str, Any]] = []
        for code, wmap in weights.items():
            code = str(code)
            if code not in code_set:
                continue
            total = float(sum(wmap.values()))
            if total <= 0:
                continue
            acc = 0.0
            acc_w = 0.0
            n_hit = 0
            for inst, w in wmap.items():
                if inst in z.index and np.isfinite(z.loc[inst]):
                    acc += float(w) * float(z.loc[inst])
                    acc_w += float(w)
                    n_hit += 1
            coverage = acc_w / total if total > 0 else 0.0
            e = (acc / acc_w) if acc_w > 0 else np.nan
            rows.append({
                "code": code, "name": names.get(code, code),
                "date": day.strftime("%Y-%m-%d"),
                "alpha_score": e, "coverage": coverage, "n_stocks_scored": n_hit,
                "weight_total": total, "weight_scored": acc_w,
            })
        meta_by_date[day] = rows

    all_codes = sorted({r["code"] for rows_ in meta_by_date.values() for r in rows_})
    e_panel = pd.DataFrame(index=pd.DatetimeIndex(signal_days), columns=all_codes, dtype=float)
    for day, rows_ in meta_by_date.items():
        for r in rows_:
            e_panel.at[day, r["code"]] = r["alpha_score"]
    return e_panel, meta_by_date


# ----------------------------------------------------------------- 选股
def _family_of(name: str | None) -> str:
    """按名称关键词判 ETF 所属**同族**（同族上限用）。"""
    n = str(name or "")
    for fam, keys in FAMILY_KEYWORDS.items():
        if any(k in n for k in keys):
            return fam
    return "其他"


def build_targets(
    dates: list[pd.Timestamp],
    scores: pd.DataFrame,
    weights: Mapping[str, Mapping[str, float]],
    close: pd.DataFrame,
    features: Mapping[str, pd.DataFrame],
    *,
    names: Mapping[str, str] | None = None,
    alpha_weight: float = 0.5,
    momentum_weight: float = 0.5,
    top_k: int = 5,
    hold_buffer: int = 2,
    entry_ma_window: int = 60,
    exit_ma_window: int = 60,
    min_coverage: float = 0.5,
    min_amount_20d: float = 0.0,
    min_history_days: int = 120,
    tradable: Mapping[str, Mapping[str, bool]] | None = None,
    allow_theme_cap: bool = True,
    # ---- 负分空仓保护（2026-11-18 新增；**默认关闭** ⇒ 既有调用方行为逐字不变）----
    neg_score_gate: bool = False,
    neg_score_gate_stat: str = "median",
    neg_score_gate_scale: float = 0.05,
    neg_score_gate_min_exposure: float = 0.0,
    # ---- 决策③：下游平滑（默认 = **不平滑** ⇒ 既有调用方行为不变）----
    smoothing_arm: str | None = None,
    smoothing_method: str | None = None,
    smoothing_alpha: float | None = None,
    smoothing_window: int | None = None,
    smoothing_history_span_days: int | None = None,
    # ---- 调仓栅格步长（= cadence // SIGNAL_STRIDE_N_DAYS；默认 = V1 同口径的 20 天档）----
    rebalance_signal_stride: int = C.REBALANCE_SIGNAL_STRIDE,
    # ---- 动量窗口的**可选显式覆盖**（``None`` = 模块常量 ⇒ 默认路径逐位不变）----
    momentum_weights: Mapping[int, float] | None = None,
) -> tuple[dict[str, dict[str, float]], pd.DataFrame]:
    """逐调仓日算出目标权重（**已含 Rank Buffer**）。

    🔴 **调仓栅格（步长 = 被搜索的 cadence ``// SIGNAL_STRIDE_N_DAYS``）**：
       ``dates`` 是**信号日栅格**（S 层打分日，每 ``SIGNAL_STRIDE_N_DAYS``=10 个交易日一个）；
       真正的**调仓日** = 信号日栅格里**每 ``rebalance_signal_stride`` 个取一个**
       （cadence=20 ⇒ stride=2 ⇒ 每 20 个交易日一个调仓日；cadence=90 ⇒ stride=9）。
       🔴 **口径变更（2026-11-17）**：cadence 已进 val 段搜索空间（用户红线「换手 ≤4×」
       在 cadence=20 的折叠常量下不可达）⇒ 本函数的入参默认仍是 V1 同口径的 20 天档，
       调用方（``run_v2._build_targets_for``）按**被搜索的 cadence** 传入。

    🔴 **下游平滑（决策③）**：平滑作用在 **ETF 层的穿透分 E 的时间序列**上
       （**横截面排名之前**）：先按日期算出 E ⇒ 沿**交易日轴**平滑 ⇒ 再做
       zscore / 排序 / 选股。**不许**去平滑个股分数 S。
       * ``smoothing_arm`` = 臂 ID（``config_v2.SMOOTHING_ARM_IDS``）：
         ``A_half_life_15`` / ``A_half_life_20`` / ``B_native_slow`` / ``C_v1_current``；
       * 臂 B（``B_native_slow``）= **不做**下游 EWMA ⇒ 直接用 V2 的 20 日 Label 模型
         **原生慢变量输出**（打分管线原始分数）排序；
       * **Rank Buffer 对所有臂一视同仁**（就在本函数里 ⇒ 统一评估引擎），
         臂 B 与其它臂走**同一条**评估 / 回测代码路径；
       * 什么都不给（默认）⇒ **不平滑**（保持既有调用方行为不变）。

    返回 ``(targets, diagnostics)``：
      * ``targets`` = ``{日期字符串: {代码: 目标权重}}``（直接喂 ``backtest_v2.run_backtest_v2``）；
      * ``diagnostics`` = 逐日逐标的明细（E / 覆盖率 / 动量 / final_score / 入选原因）。

    🔴 **MA 窗口（进 / 出）**：``entry_ma_window`` / ``exit_ma_window`` **真的**决定用哪条均线
       （入场 = ``above_ma{entry}``、退场 = ``ma{exit}``）；``<= 0`` 表示**不启用**该闸门。
       特征里没有对应均线 ⇒ **按需补算**（绝不退回 ``ma60``）。

    🔴 **动量窗口覆盖**：``momentum_weights`` = ``{窗口: 权重}``（``None`` = 模块常量
       :data:`MOMENTUM_WEIGHTS` ⇒ **默认路径逐位不变**）；给了覆盖时，动量分
       ``Σ w·z(mom{w})`` 按**覆盖的窗口与权重**算（特征里必须已有对应的 ``mom{w}``）。

    🔴 **负分空仓保护**（2026-11-18 新增，**默认关闭**）：
       当**候选 ETF 的穿透分 E 普遍转负**（横截面统计量 ``stat`` < 0）时，
       按比例把**权益敞口**削减到现金（剩余权重 ⇒ 回测里就是现金，收益率 0）：
       ``exposure = clip(1 + stat / scale, min_exposure, 1)``。
       * ``stat`` 取**入场 MA 过滤之前**的候选池（否则闸门会变成 MA 过滤的函数）；
       * ``stat`` 用的是**排序用的那份 E**（启用了平滑臂 ⇒ 用平滑后的 E）；
       * ``scale`` 越小 ⇒ 越敏感（``stat = -scale`` ⇒ 敞口归零）。
       用途：解决 V2 单盲里「个股 RankIC 0.1271 但组合 −12.93% / 回撤 −54.93%」的
       **系统性熊市满仓硬扛**问题（诊断见 ``runs/.../v2/V2_ATTRIBUTION_*.md``）。
    """
    names = dict(names or {})
    px = close.astype("float64").sort_index()
    scores = scores.copy()
    scores["date"] = pd.to_datetime(scores["date"]).dt.normalize()
    # 🔴 动量窗口覆盖（``None`` ⇒ 模块常量本体 ⇒ 默认路径逐位不变）
    mom_weights = _momentum_weight_set(momentum_weights)

    # ---- 均线窗口（入场 / 退场 / 趋势调整）：**不许静默退回 ma60** ----
    entry_win = int(entry_ma_window)
    exit_win = int(exit_ma_window)
    ma_windows_needed = _ma_window_set([w for w in (entry_win, exit_win) if w > 0])
    ma_panels: dict[int, pd.DataFrame] = {}
    for win in ma_windows_needed:
        col = f"ma{win}"
        src = features.get(col)
        if isinstance(src, pd.DataFrame) and len(src.columns):
            ma_panels[win] = src.astype("float64")
        else:
            # 🔴 特征里没有这条均线 ⇒ **按需补算**（绝不退回 ma60）
            ma_panels[win] = _ma_panel(px, win)

    targets: dict[str, dict[str, float]] = {}
    diag_rows: list[dict[str, Any]] = []
    held: list[str] = []

    # ---- 调仓栅格：信号日栅格里每 N 个取一个（🔴 N = 被搜索的 cadence // 信号栅格步长）----
    stride = max(1, int(rebalance_signal_stride))
    signal_days = sorted({pd.Timestamp(d).normalize() for d in dates})
    rebalance_days = signal_days[::stride]

    # ---- 决策③：解析平滑臂（None = 不平滑）----
    arm = SM.resolve_arm(
        smoothing_arm,
        method=smoothing_method,
        alpha=smoothing_alpha,
        window=smoothing_window,
        history_span_days=smoothing_history_span_days,
    )

    # ---- ① 先按日期算出 E（面板：日期 × ETF 代码）----
    #    🔴 2026-11-18：E 的计算抽到 :func:`compute_alpha_panel`（**逐字等价**），
    #    好让归因工具（``v2/attribution.py``）能在**不重跑选股**的前提下
    #    单独拿到「ETF 层的 E 面板」⇒ 算 ETF 层面的 20 日 RankIC。
    e_panel, meta_by_date = compute_alpha_panel(
        scores, weights, signal_days, names=names, codes=px.columns,
    )

    # ---- ② 沿**交易日轴**平滑 E（决策③；臂 B = 原生慢变量直通 ⇒ 不做）----
    smoothed_by_day: dict[pd.Timestamp, dict[str, float]] = {}
    if arm is not None and arm.enabled:
        axis = [pd.Timestamp(d).normalize() for d in px.index]
        for day in rebalance_days:
            res = SM.apply_spec(e_panel, axis, arm, asof=day)
            if len(res.panel) == 0:
                smoothed_by_day[day] = {}
                continue
            last = res.panel.iloc[-1]
            smoothed_by_day[day] = {
                str(code): (float(val) if pd.notna(val) else np.nan)
                for code, val in last.items()
            }

    for day in rebalance_days:
        key = day.strftime("%Y-%m-%d")
        rows = meta_by_date.get(day) or []
        if not rows:
            continue
        pool_all = pd.DataFrame(rows)
        # 平滑后的 E 进排序（臂 B / 不平滑 ⇒ 原生瞬态分）；原始 E 留档备查
        raw_map = {r["code"]: r["alpha_score"] for r in rows}
        if arm is not None and arm.enabled:
            sm_map = smoothed_by_day.get(day) or {}
            pool_all["alpha_score"] = [sm_map.get(c, np.nan) for c in pool_all["code"]]
        pool_all["alpha_score_raw"] = [raw_map.get(c, np.nan) for c in pool_all["code"]]

        # ---- 特征（asof 口径：只用 <= day 的数据）----
        hist = px.loc[px.index <= day]
        if hist.empty:
            # 🔴 2026-11-18：该日之前行情面板**没有任何价格**（信号日早于面板起点）
            #    ⇒ 该日无法构造候选池 ⇒ 跳过（原来会在下面的 `iloc[-1]` 抛 IndexError，
            #    2026-11-18 10:04 的 5 折全量跑就崩在这里）。
            #    对正常路径（该日有价格）**逐字不变**。
            continue
        n_days = hist.notna().sum(axis=0)
        feats: dict[str, Any] = {}
        for win in mom_weights:
            col = f"mom{win}"
            src = features[col]
            feats[col] = src.loc[src.index <= day].iloc[-1] if len(src.loc[src.index <= day]) else None
        for win in ma_windows_needed:
            src = ma_panels[win]
            sub = src.loc[src.index <= day]
            feats[f"ma{win}"] = sub.iloc[-1] if len(sub) else None
        amt_src = features["amount20d"].loc[features["amount20d"].index <= day]
        feats["amount20d"] = amt_src.iloc[-1] if len(amt_src) else None

        def _row(col: str, code: str):
            """从特征宽表里取某只 ETF 在某日的特征值（取不到 ⇒ NaN）。"""
            obj = feats.get(col)
            if obj is None or code not in obj.index:
                return np.nan
            val = obj[code]
            return float(val) if np.isfinite(val) else np.nan

        for win in mom_weights:
            pool_all[f"mom{win}"] = [_row(f"mom{win}", c) for c in pool_all["code"]]
        pool_all["amount20d"] = [_row("amount20d", c) for c in pool_all["code"]]
        # 用当日收盘价与 MA 比较（严格用 <= day 的数据）
        # 🔴 2026-11-18：面板起点之前的日子已在上面 `if hist.empty: continue` 整日跳过
        #    ⇒ 这里 `hist` **必非空** ⇒ 直接取最后一行（不做多余的防御分支）。
        last_close = hist.iloc[-1]
        pool_all["last_close"] = [float(last_close.get(c, np.nan)) for c in pool_all["code"]]
        for win in ma_windows_needed:
            pool_all[f"ma{win}"] = [_row(f"ma{win}", c) for c in pool_all["code"]]
            pool_all[f"above_ma{win}"] = [
                bool(np.isfinite(r["last_close"]) and np.isfinite(r[f"ma{win}"])
                     and r["last_close"] > r[f"ma{win}"])
                for _, r in pool_all.iterrows()
            ]
        pool_all["n_days"] = [int(n_days.get(c, 0)) for c in pool_all["code"]]
        pool_all["family"] = [_family_of(names.get(c, c)) for c in pool_all["code"]]

        # ---- 过滤网 ----
        pool_all["coverage_ok"] = pool_all["coverage"] >= float(min_coverage)
        pool_all["liquidity_ok"] = (
            (pool_all["amount20d"].fillna(0.0) >= float(min_amount_20d))
            & (pool_all["n_days"] >= int(min_history_days))
        )
        pool_all["tradable"] = True
        if tradable:
            day_trad = tradable.get(key) or tradable.get(day.strftime("%Y%m%d")) or {}
            pool_all["tradable"] = [
                bool(day_trad.get(c, True)) for c in pool_all["code"]
            ]

        # ---- 退场 MA：持仓跌破 MA{exit} ⇒ 无条件卖出（先于排序）----
        forced_exit: set[str] = set()
        if exit_win > 0:
            exit_col = f"ma{exit_win}"
            for code in list(held):
                sub = pool_all[pool_all["code"] == code]
                if sub.empty:
                    forced_exit.add(code)
                    continue
                r = sub.iloc[0]
                ma_exit = r[exit_col]
                if not np.isfinite(ma_exit) or r["last_close"] < ma_exit:
                    forced_exit.add(code)

        pool = pool_all[pool_all["coverage_ok"] & pool_all["liquidity_ok"] & pool_all["tradable"]].copy()
        pool = pool[~pool["code"].isin(forced_exit)].copy()

        # ---- 负分空仓保护：候选池 E 的横截面统计量（**入场 MA 过滤之前**）----
        gate_stat = np.nan
        if neg_score_gate and not pool.empty:
            vals = pd.to_numeric(pool["alpha_score"], errors="coerce").dropna()
            if len(vals):
                gate_stat = (float(vals.median()) if str(neg_score_gate_stat).lower() == "median"
                             else float(vals.mean()))
        exposure = (neg_score_exposure(gate_stat, neg_score_gate_scale, neg_score_gate_min_exposure)
                    if neg_score_gate else 1.0)

        if pool.empty:
            targets[key] = {}
            held = []
            for _, r in pool_all.iterrows():
                diag_rows.append({**r.to_dict(), "selected": False, "reason": "过滤后池为空",
                                  "neg_gate_stat": gate_stat, "neg_exposure": exposure})
            continue

        # ---- 分数合成（V1 同口径）----
        pool["z_alpha"] = zscore(pool["alpha_score"].fillna(pool["alpha_score"].mean()))
        z_mom = pd.Series(0.0, index=pool.index)
        for win, w in mom_weights.items():
            z_mom = z_mom + w * zscore(pool[f"mom{win}"])
        pool["momentum_score"] = z_mom
        pool["momentum_score_trend_adj"] = pool["momentum_score"] * np.where(
            pool[f"above_ma{MA_TREND_WINDOW}"], 1.0, 0.5
        )
        pool["z_mom"] = zscore(pool["momentum_score_trend_adj"])
        pool["final_score"] = float(alpha_weight) * pool["z_alpha"] + float(momentum_weight) * pool["z_mom"]

        # ---- 诊断表带上**排序分**（🔴 2026-11-18）----
        #    归因工具（``v2/attribution.py``）要算「**ETF 层面的 20 日 RankIC**」
        #    ⇒ 必须能拿到**排序实际用的那份分**（否则只能算原始 E 的 IC，
        #    看不出「排序把 Alpha 吃掉了多少」）。
        score_map: dict[str, dict[str, float]] = {
            str(code): {"z_alpha": float(za), "z_mom": float(zm), "final_score": float(fs)}
            for code, za, zm, fs in zip(pool["code"], pool["z_alpha"], pool["z_mom"],
                                        pool["final_score"])
        }

        # ---- 入场 MA 硬过滤（V1 同口径）----
        if entry_win > 0:
            pool = pool[pool[f"above_ma{entry_win}"]].copy()

        # ---- 排序 + 同族上限 + Rank Buffer ----
        pool = pool.sort_values("final_score", ascending=False, na_position="last").reset_index(drop=True)
        ranks = {c: i + 1 for i, c in enumerate(pool["code"].tolist())}
        sel_codes = list(pool["code"][: int(top_k)])
        if allow_theme_cap:
            seen_family: set[str] = set()
            capped: list[str] = []
            for c in sel_codes:
                fam = str(pool.loc[pool["code"] == c, "family"].iloc[0])
                if fam in seen_family:
                    continue
                seen_family.add(fam)
                capped.append(c)
            sel_codes = capped
        # Rank Buffer：已持有且排名 ≤ top_k+buffer ⇒ 继续持有
        band = int(top_k) + int(hold_buffer)
        protected = [c for c in held if c in ranks and ranks[c] <= band and c in set(pool["code"])]
        final_codes = list(sel_codes)
        for c in protected:
            if c in final_codes:
                continue
            replaceable = [x for x in final_codes if x not in set(held)]
            if replaceable:
                victim = max(replaceable, key=lambda x: ranks.get(x, 10 ** 6))
                final_codes[final_codes.index(victim)] = c
            else:
                final_codes.append(c)
        weight = 1.0 / len(final_codes) if final_codes else 0.0
        # 🔴 负分空仓保护：目标权重整体乘以敞口（剩余部分 ⇒ 现金）
        targets[key] = {c: weight * exposure for c in final_codes}
        held = list(final_codes)

        sel_set = set(final_codes)
        for _, r in pool_all.iterrows():
            code = str(r["code"])
            if code in forced_exit:
                reason = f"持仓跌破 MA{exit_win} ⇒ 无条件卖出"
            elif code in sel_set:
                reason = "入选"
            elif code in held and r["code"] in pool_all["code"].values and code in set(pool["code"]) \
                    and ranks.get(code, 10 ** 6) <= band:
                reason = "Rank Buffer 迟滞保留"
            elif not bool(r["coverage_ok"]):
                reason = f"覆盖率 {r['coverage']:.2f} < {min_coverage} ⇒ 不可穿透"
            elif not bool(r["liquidity_ok"]):
                reason = "流动性/历史长度不达标"
            elif not bool(r["tradable"]):
                reason = "停牌/涨跌停 ⇒ 不可交易"
            elif entry_win > 0 and not bool(r[f"above_ma{entry_win}"]):
                reason = f"跌破 MA{entry_win} ⇒ 不许新开仓"
            else:
                reason = "排名靠后"
            diag_rows.append({**r.to_dict(), **score_map.get(code, {}),
                              "selected": code in sel_set, "reason": reason,
                              "rank": ranks.get(code),
                              "neg_gate_stat": gate_stat, "neg_exposure": exposure})

    diag = pd.DataFrame(diag_rows)
    return targets, diag
