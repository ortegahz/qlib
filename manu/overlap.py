"""候选池两两重合度筛查（TA 审查参考 + 顺延判据的**单一实现**）。

【为什么单独一层】
规则层（`engine/filters.py`）已经按「成分重叠 ≥ `C.DEDUP_OVERLAP_THRESHOLD`(0.90)
**或** 120 日相关性 ≥ `C.THEME_CORR_THRESHOLD`(0.95)」**逐对拦截**冲突对（不共存）。
但 TA（TradingAgents）做**合规审查**时需要看到**更宽口径**的重合度预警
（重叠 ≥ 0.50 或相关性 ≥ 0.80 或同族）⇒ 本模块提供**唯一**实现，避免口径分叉。

🔴 **口径分层（勿混）**
- `rule_conflict`：**规则层现役判据**（直接复用 `engine.filters.build_theme_and_conflicts`
  返回的 `conflicts` 集合）⇒ 决定「两只能不能同时进组合」；
- `screen_flag`：**更宽口径的预警** ⇒ **只**用于给 TA 提示 + 顺延前的软筛查，
  **不改变规则层判定**（规则层该拒还是拒）。

🔴 **不许另写一套口径**
本模块的权重重叠公式、主题族判定、相关性窗口**全部复用现役实现**：
- 权重重叠：`engine.filters._weight_overlap`；
- 主题族 + 冲突对：`engine.filters.build_theme_and_conflicts`；
- 120 日相关性：`engine.features.compute_corr_matrix`（窗口 `C.THEME_CORR_WINDOW`）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

#: 把脚本所在目录放进 sys.path ⇒ 可 import 同目录的 `config` 与 `engine.*`
#: （与 `final_rebalance_plan.py` / `engine/filters.py` 同一套写法）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as C  # noqa: E402
from engine.features import compute_corr_matrix, load_prices  # noqa: E402
from engine.filters import _weight_overlap, build_theme_and_conflicts  # noqa: E402
from engine.weights import load_holdings, to_qlib_instrument  # noqa: E402

#: 🔴 **筛查口径比规则层宽**：成分重叠 ≥ 0.50（规则层拦截线是 0.90）。
#:    ⇒ **只**用于给 TA 提示与顺延前的软筛查，**不改变规则层判定**。
SCREEN_OVERLAP_THRESHOLD = 0.50

#: 🔴 **筛查口径比规则层宽**：120 日相关性 ≥ 0.80（规则层拦截线是 0.95）。
#:    ⇒ 同上，**只做预警**。
SCREEN_CORR_THRESHOLD = 0.80


def _normalize_holdings(holdings: pd.DataFrame | None) -> pd.DataFrame:
    """补齐 `instrument` 列（成分股归一成 qlib instrument）⇒ 与规则层同口径。

    `engine/weights.load_holdings` 已经做过这一步；这里再兜一层，
    让**合成夹具**（只有 `stock_code`）也能直接喂进来。
    """
    if holdings is None or len(holdings) == 0:
        return pd.DataFrame(columns=["etf_code", "instrument", "weight_pct"])
    df = holdings.copy()
    if "etf_code" not in df.columns:
        return pd.DataFrame(columns=["etf_code", "instrument", "weight_pct"])
    df["etf_code"] = df["etf_code"].astype(str).str.zfill(6)
    if "weight_pct" not in df.columns:
        df["weight_pct"] = 0.0
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce").fillna(0.0)
    if "instrument" not in df.columns:
        src = "stock_code" if "stock_code" in df.columns else None
        df["instrument"] = (
            df[src].map(lambda c: to_qlib_instrument(c)) if src else ""
        )
    df["instrument"] = df["instrument"].astype(str)
    return df


def build_weight_map(holdings: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    """etf_code -> {instrument: weight_pct}（成分权重映射，与规则层同口径）。"""
    df = _normalize_holdings(holdings)
    out: dict[str, dict[str, float]] = {}
    for code, grp in df.groupby("etf_code"):
        out[str(code)] = dict(zip(grp["instrument"].astype(str),
                                  grp["weight_pct"].astype(float)))
    return out


def _usable_prices(prices: pd.DataFrame | None) -> pd.DataFrame | None:
    """价格表必须含 `date` / `code` / `close` ⇒ 否则相关性算不出来（返回 None）。"""
    if prices is None or len(prices) == 0:
        return None
    cols = set(prices.columns)
    if not {"date", "code", "close"}.issubset(cols):
        return None
    return prices


def compute_overlap_pairs(
    *,
    holdings: pd.DataFrame | None,
    prices: pd.DataFrame | None,
    codes: list[str],
    asof,
    names: dict[str, str] | None = None,
    rule_overlap_thresh: float | None = None,
    rule_corr_thresh: float | None = None,
    screen_overlap_thresh: float | None = None,
    screen_corr_thresh: float | None = None,
) -> list[dict]:
    """候选池两两重合度 ⇒ 返回每一对的明细。

    参数
    ----
    holdings : 成分权重表（`load_holdings` 的输出；合成夹具亦可）
    prices   : 日线表（`load_prices` 的输出；`date` / `code` / `close`）
    codes    : 要筛查的代码列表
    asof     : 信号日（**含**）；相关性只用到该日为止的数据 ⇒ 无未来函数
    names    : code -> 名称（用于名称族判定）

    返回（每对一条）：
        ``{"a","b","a_name","b_name","weight_overlap","corr120",
           "theme_a","theme_b","same_theme","rule_conflict","screen_flag"}``

    🔴 `rule_conflict` = **规则层现役判据**（复用 `build_theme_and_conflicts` 的
       `conflicts` 集合：重叠 ≥ 0.90 **或** corr120 ≥ 0.95）；
       `rule_corr_thresh` 非空时**额外**按该阈值判定相关性冲突。
    🔴 `screen_flag` = **更宽口径的预警**（重叠 ≥ 0.50 **或** corr120 ≥ 0.80 **或** 同族）
       ⇒ **只提示、不改判**。
    """
    uniq: list[str] = []
    seen: set[str] = set()
    for c in (codes or []):
        code = str(c).strip().zfill(6)
        if code and code not in seen:
            seen.add(code)
            uniq.append(code)
    if len(uniq) < 2:
        return []

    rule_overlap_thresh = (C.DEDUP_OVERLAP_THRESHOLD if rule_overlap_thresh is None
                           else float(rule_overlap_thresh))
    screen_overlap_thresh = (SCREEN_OVERLAP_THRESHOLD if screen_overlap_thresh is None
                             else float(screen_overlap_thresh))
    screen_corr_thresh = (SCREEN_CORR_THRESHOLD if screen_corr_thresh is None
                          else float(screen_corr_thresh))

    h = _normalize_holdings(holdings)
    px = _usable_prices(prices)
    name_map = {str(k): str(v) for k, v in (names or {}).items()}

    # 🔴 规则层判据**直接复用** engine/filters 的现役实现（不重写口径）
    theme_map, conflicts = build_theme_and_conflicts(
        h, uniq, overlap_thresh=rule_overlap_thresh, names=name_map,
        prices=px, asof=asof,
    )
    corr_map = compute_corr_matrix(px, uniq, asof, C.THEME_CORR_WINDOW)
    wmap = build_weight_map(h)

    pairs: list[dict] = []
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            overlap = float(_weight_overlap(wmap.get(a, {}), wmap.get(b, {})))
            corr = 0.0
            if a in corr_map.index and b in corr_map.columns:
                try:
                    corr = float(corr_map.at[a, b])
                except (TypeError, ValueError):
                    corr = 0.0
            if not math.isfinite(corr):
                corr = 0.0
            theme_a = str(theme_map.get(a, a))
            theme_b = str(theme_map.get(b, b))
            same_theme = theme_a == theme_b
            rule_conflict = frozenset({a, b}) in conflicts
            if rule_corr_thresh is not None and corr >= float(rule_corr_thresh):
                rule_conflict = True
            screen_flag = bool(
                overlap >= screen_overlap_thresh
                or corr >= screen_corr_thresh
                or same_theme
            )
            pairs.append({
                "a": a,
                "b": b,
                "a_name": name_map.get(a, ""),
                "b_name": name_map.get(b, ""),
                "weight_overlap": round(overlap, 6),
                "corr120": round(corr, 6),
                "theme_a": theme_a,
                "theme_b": theme_b,
                "same_theme": same_theme,
                "rule_conflict": bool(rule_conflict),
                "screen_flag": screen_flag,
            })
    return pairs


def load_overlap_inputs(
    *,
    holdings_path: str | Path | None = None,
    prices_path: str | Path | None = None,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """读成分权重与日线（**只读**）；缺件 ⇒ 对应项返回 None（不编数据）。"""
    holdings = None
    prices = None
    try:
        hpath = Path(holdings_path) if holdings_path else Path(C.ETF_HOLDINGS_CSV)
        if hpath.exists():
            holdings = load_holdings(hpath)
    except Exception:  # noqa: BLE001 —— 缺件/坏件都不许让主流程崩
        holdings = None
    try:
        ppath = Path(prices_path) if prices_path else Path(C.ETF_PRICES_CSV)
        if ppath.exists():
            prices = load_prices(ppath)
    except Exception:  # noqa: BLE001
        prices = None
    return holdings, prices
