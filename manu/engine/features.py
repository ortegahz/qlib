"""ETF 行情特征：动量 / 趋势 / 波动 / 相对强弱（doc1 的特征层）。

【为什么不用 Alpha158 那套微观算子】
ETF 是一篮子股票，涨跌由**宏观流动性、行业景气、风格轮动**驱动，
没有个股那套微观博弈特征 ⇒ 用 **中长周期动量 + 趋势 + 波动 + 相对强弱** 更贴切。

【严格无未来函数】
所有特征只用到 `asof`（含）为止的收盘价；执行日在 `asof` 之后（见 config.EXECUTION_LAG_DAYS）。

【输入】`etf_prices.csv`：date, code, open, high, low, close, volume, amount
【输出】每个 code 一行：close / mom20 / mom60 / mom120 / ma60 / above_ma60 / vol20 / ret_1d
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402

TRADING_DAYS_PER_YEAR = 250


def load_prices(path: Path | str = None) -> pd.DataFrame:
    """读 ETF 日线（前复权），按 code/date 排序，索引 reset。"""
    path = Path(path or C.ETF_PRICES_CSV)
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(["code", "date"]).reset_index(drop=True)


def compute_etf_features(prices: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """算出 asof 截面的 ETF 特征。

    参数
    ----
    prices : 全量日线
    asof   : 信号日（含）；只用到该日为止的数据
    """
    df = prices[prices["date"] <= pd.Timestamp(asof)].copy()
    if df.empty:
        raise ValueError(f"asof={asof} 之前没有任何价格数据")

    feats: list[dict[str, object]] = []
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("date")
        close = g["close"].to_numpy(dtype=float)
        n = len(close)
        last = close[-1]

        def ret_over(window: int) -> float:
            if n <= window:
                return float("nan")
            base = close[-1 - window]
            if not np.isfinite(base) or base == 0:
                return float("nan")
            return float(last / base - 1.0)

        ma_win = C.MA_TREND_WINDOW
        ma = float(np.mean(close[-ma_win:])) if n >= ma_win else float("nan")

        if n >= C.VOL_WINDOW + 1:
            rets = np.diff(close[-(C.VOL_WINDOW + 1):])
            vol = float(np.std(rets, ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(rets) > 1 else float("nan")
        else:
            vol = float("nan")

        feats.append(
            {
                "code": code,
                "close": last,
                "n_days": n,
                "last_date": g["date"].iloc[-1].date().isoformat(),
                "mom20": ret_over(20),
                "mom60": ret_over(60),
                "mom120": ret_over(120),
                "ma60": ma,
                "above_ma60": bool(np.isfinite(ma) and last > ma),
                "vol20": vol,
                "ret_1d": float(last / close[-2] - 1.0) if n >= 2 else float("nan"),
                "amount_20d_mean": float(g["amount"].tail(20).mean()),
                "amount_20d_median": float(g["amount"].tail(20).median()),
            }
        )
    return pd.DataFrame(feats)


def compute_corr_matrix(
    prices: pd.DataFrame,
    codes: list[str],
    asof: pd.Timestamp,
    window: int,
) -> pd.DataFrame:
    """codes 之间的日收益相关系数矩阵（只用 asof 及之前的数据）。

    - 价格取 close，做 pct_change（**必须 fill_method=None**，不要用已废弃的默认前向填充）
    - 窗口 = asof 之前最后 window 个交易日
    - 样本不足 window/2 或收益率方差为 0 的对角/元素 ⇒ 相关性记为 0（不合并）
    - 返回 DataFrame(index=codes, columns=codes)

    🔴 无未来函数：先按 `date <= asof` 截断，再取窗口；asof 之后的价格一律不参与。
    """
    codes = [str(c) for c in codes]
    out = pd.DataFrame(0.0, index=codes, columns=codes)
    window = int(window)
    if prices is None or len(codes) == 0 or window < 2 or asof is None:
        return out

    dates = pd.to_datetime(prices["date"])
    code_set = set(codes)
    mask = (dates <= pd.Timestamp(asof)) & prices["code"].astype(str).isin(code_set)
    df = prices.loc[mask].copy()
    if df.empty:
        return out
    df["code"] = df["code"].astype(str)
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    pivot = df.pivot_table(index="date", columns="code", values="close", aggfunc="last").sort_index()
    # window 个**日收益** ⇒ 需要 window+1 个收盘价（pct_change 的首行是 NaN）
    pivot = pivot.tail(window + 1)
    rets = pivot.pct_change(fill_method=None).iloc[1:]
    if rets.empty:
        return out
    rets = rets.reindex(columns=[c for c in codes if c in rets.columns])

    min_obs = max(2, window // 2)
    corr = rets.corr(min_periods=min_obs)
    corr = corr.reindex(index=codes, columns=codes)
    # 样本不足（< window/2）或收益率方差为 0 ⇒ NaN ⇒ 记 0（= 不判为同主题）
    return corr.fillna(0.0)


def add_relative_strength(feats: pd.DataFrame, bench_code: str = C.RS_BENCHMARK) -> pd.DataFrame:
    """相对强弱：ETF 的 60 日收益 − 基准 ETF 的 60 日收益（百分点）。

    基准取沪深300ETF（若缺失则退化为「不做相对调整」，只留绝对动量）。
    """
    out = feats.copy()
    out["rs60"] = np.nan
    if bench_code not in set(out["code"]):
        return out
    bench = out.loc[out["code"] == bench_code, "mom60"]
    if bench.empty or not np.isfinite(float(bench.iloc[0])):
        return out
    out["rs60"] = out["mom60"] - float(bench.iloc[0])
    return out


def momentum_score(feats: pd.DataFrame) -> pd.DataFrame:
    """动量综合分：20/60/120 日动量的**截面 z-score** 加权（权重见 config）。

    ⚠️ z-score 在**当期候选池内**做 ⇒ 只做横截面排序，不做绝对值判断
    （绝对动量判断由「双重动量防守」单独负责）。
    """
    out = feats.copy()
    z = pd.DataFrame(index=out.index)
    for win, w in C.MOMENTUM_WEIGHTS.items():
        col = f"mom{win}"
        raw = out[col].astype(float)
        mu, sd = raw.mean(), raw.std(ddof=0)
        z[col] = (raw - mu) / sd if np.isfinite(sd) and sd > 0 else 0.0
    out["momentum_score"] = sum(w * z[f"mom{win}"] for win, w in C.MOMENTUM_WEIGHTS.items())
    # 趋势过滤：价格跌破 60 日线 ⇒ 该 ETF 的动量分打折（不是硬排除，避免踩在转折点）
    out["momentum_score_trend_adj"] = out["momentum_score"] * np.where(out["above_ma60"], 1.0, 0.5)
    return out
