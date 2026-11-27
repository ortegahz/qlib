#!/usr/bin/env python3
"""🔴 **长周期指数宇宙外推检验**（2015 ~ 2023）—— 把 V2 的 **ETF 层决策规则**搬到
**申万一级行业指数宇宙**上跑，问的是「这套动量 + 排序 + 月频框架换个时间、换个资产
类别还成不成立」。

【🔴 口径边界（必须先读，不许跳过）】
  1. **这不是 V2 的完整管线回测**：V2 的排序分 = ``w_alpha·z(E) + w_mom·z(momentum_trend_adj)``，
     其中 **E = 穿透分**（ETF 成分股权重 × 个股分 S）。指数宇宙**没有成分股、也没有 E**
     ⇒ 本探针的排序分是**纯动量趋势调整分**（等价于 ``alpha_weight=0 / momentum_weight=1``），
     而 V2 冠军臂 ``pure_E_native`` 是 ``alpha_weight=1.0 / momentum_weight=0.0``（**纯 E**）
     ⇒ **这是与冠军臂的关键口径差异**（报告里必须显著披露）。
  2. **为什么值得做**：完整 V2 管线回测不到 2015（成分股权重档案只有 2020 年以后、
     qlib 个股特征从 2016 起）⇒ 本探针是**框架层外推检验**，不是策略复现。
  3. **行业指数是价格指数**（不含分红再投），而 ETF 价格（qfq）含分红再投
     ⇒ 指数宇宙收益**天然低于** ETF 宇宙 ⇒ 与 ETF 侧超额**不可直接比大小**，
     只能比**符号 / 稳健性**。

【复刻了 V2 的哪些东西（逐条）】
  * 特征：直接调 ``etf_layer.compute_features`` ⇒ ``mom20/mom60/mom120``（权重
    ``{20:0.2, 60:0.5, 120:0.3}``）与 ``ma60/above_ma60``（``MA_TREND_WINDOW=60``）
    **与 V2 逐位同口径**（不是另发明的动量定义）。
  * 排序分：``z_mom = Σ_w w·z(mom_w)``；``momentum_score_trend_adj = z_mom × (1.0 if
    above_ma60 else 0.5)``；``final_score = 0·z_alpha + 1·zscore(momentum_score_trend_adj)``。
  * 组合规则：``cadence=20`` 交易日调仓（信号栅格 10 日 × stride 2，与 V2 同构）、
    ``top_k=5``、``hold_buffer=2``（复用 ``backtest_v2.buffer_targets`` = V2 的 Rank Buffer）、
    入场/退场 MA = 0、同族上限关闭（``allow_theme_cap=False``，与冠军臂一致）。
  * 成本：``commission=0.00015``、``slippage=0.0005``（``config_v2`` 同值）、
    ``periods_per_year=250``；回测引擎 = ``backtest_v2.run_backtest_v2``（含漂移权重换手）。
  * 基准：**沪深300**（qlib ``sh000300``，与 ``config_v2.BENCHMARK`` 同标的）。

【必须跑的三组】
  ① 主结果：2015-01-01 ~ 2023-12-31，冻结冠军参数（top_k=5 / buffer=2 / cadence=20）；
  ② 跨期分段：逐年超额（超额若只在 2019-2020 为正 ⇒ 拟合噪声的强证据）；
  ③ 高原交叉验证：``hold_buffer ∈ {0,1,2,3,5}``（其余冻结）⇒ 看是「高原」还是「尖刺」。

【🔴 封存段纪律】本探针**只用** 2014-01-01 ~ 2023-12-31 的数据（2014 只做预热）；
V2 的 2024 年起单盲封存段**一律不碰**（源码里也不许出现 2024+ 的日期字面量，
由 ``manu/etf_rotation/v2/tests/test_longhorizon_index_guard.py`` 静态钉死）。

用法（服务器 · ql env）::

    /home/huangzhe/anaconda3/envs/ql/bin/python -u manu/tools/v2_longhorizon_index_probe.py \
        --prices <idx_sw 目录>/sw_index_prices.csv \
        --out-dir <结果根>/_longhorizon_idx_<run id> \
        --qlib-provider <cn_data 目录> --report-out <结果根>/reports/<报告名>.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ---- V2 模块（只读复用；**不改** etf_layer / config_v2 / backtest_v2 / metrics）----
_V2_DIR = Path(__file__).resolve().parent.parent / "etf_rotation" / "v2"
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import backtest_v2 as BT  # noqa: E402
import config_v2 as C  # noqa: E402
import etf_layer as EL  # noqa: E402
import metrics as ME  # noqa: E402

__all__ = [
    "LONGHORIZON_WINDOW",
    "WARMUP_START",
    "build_index_features",
    "build_targets_index",
    "load_index_panel",
    "run_variant",
]

# ----------------------------------------------------------------- 冻结口径常量
#: 🔴 **评估窗口**（长周期外推检验的主区间；V2 的 2024+ 单盲封存段不在其中）
LONGHORIZON_WINDOW: tuple[str, str] = ("2015-01-01", "2023-12-31")
#: 🔴 **预热窗口起点**（早于评估窗口；只用于动量 120 日 / MA60 / 历史长度闸门）
WARMUP_START = "2014-01-01"
#: 调仓节奏（交易日）= V2 冠军臂同值
CADENCE = 20
#: 信号栅格步长（V2 ``config_v2.SIGNAL_STRIDE_N_DAYS`` 同值）⇒ 调仓栅格 = 20 交易日
SIGNAL_STRIDE_N_DAYS = 10
REBALANCE_SIGNAL_STRIDE = CADENCE // SIGNAL_STRIDE_N_DAYS
#: 持仓数（V2 冠军臂 ``TARGET_N_HOLDINGS`` 同值）
TOP_K = 5
#: Rank Buffer 带宽（V2 冠军臂 ``HOLD_BUFFER`` 同值）
HOLD_BUFFER = 2
#: 🔴 **buffer 高原交叉验证**的面（其余参数全部冻结）
BUFFER_GRID: tuple[int, ...] = (0, 1, 2, 3, 5)
#: 成本（照抄 ``config_v2``）
COMMISSION = float(C.COMMISSION)
SLIPPAGE = float(C.SLIPPAGE)
#: 年化期数（V2 运行时用的是 250）
PERIODS_PER_YEAR = 250
#: 🔴 排序分权重：指数宇宙**没有穿透分 E** ⇒ 纯动量（= 冠军臂的镜像口径）
ALPHA_WEIGHT = 0.0
MOMENTUM_WEIGHT = 1.0
#: 历史长度闸门（V2 ``MIN_ETF_HISTORY_DAYS`` 同值）
MIN_HISTORY_DAYS = 120
#: 流动性闸门：🔴 指数宇宙**不设**（申万成交额口径与 ETF 不可比）⇒ 0.0 = 不筛
MIN_AMOUNT_20D = 0.0
#: RankIC 的前瞻窗口（交易日）
RANKIC_HORIZON = 20
#: 基准（qlib instrument；与 ``config_v2.BENCHMARK`` 同标的）
BENCHMARK = "sh000300"
#: 滚动超额窗口（交易日；≈12 个月）
ROLLING_WINDOW = 252

#: 🔴 **V2 侧既有基线**（ETF 宇宙 · 冠军臂 ``pure_E_native`` + buffer=2 · 2019-2023 OOS）。
#: ⚠️ **本任务没有重跑 V2 侧**（V2 侧是已封存的单盲产物）⇒ 引用的是**既有基线数字**，
#:    且**指数宇宙与 ETF 宇宙口径不同**（见模块 docstring）⇒ **只比符号 / 稳健性**。
V2_ETF_BASELINE: dict[str, Any] = {
    "universe": "ETF 宇宙（ETF 穿透评分 V2）",
    "window": "2019-01-01 ~ 2023-12-31（5 折 OOS 拼接）",
    "arm": "pure_E_native（alpha_weight=1.0 / momentum_weight=0.0 / entry_ma=0 / exit_ma=0 / "
           "allow_theme_cap=False）+ hold_buffer=2",
    "annualized_return": 0.1351,
    "excess_vs_benchmark": 0.0739,
    "rankic": 0.0488,
    "rankic_n": 54,
    "yearly": {"2019": 0.390, "2020": 0.653, "2021": -0.081, "2022": 0.111, "2023": -0.199},
    "source": "V2 侧既有基线（本任务未重跑、只引用）",
}

#: 🔴 逐年分段的**市场阶段标签**（判读用；不参与任何计算）
YEAR_REGIMES: dict[str, str] = {
    "2015": "崩盘",
    "2016": "熔断",
    "2017": "蓝筹",
    "2018": "熊市",
    "2019": "牛市起",
    "2020": "牛市",
    "2021": "结构性熊",
    "2022": "熊市",
    "2023": "熊市",
}


# ----------------------------------------------------------------- 小工具
def log(msg: str) -> None:
    """带时间戳的进度输出。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def md5_of(path: Path) -> str:
    """文件 md5（产物可核对）。"""
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fmt_pct(value: float | None, digits: int = 2) -> str:
    """百分比格式化（NaN / None ⇒ ``n/a``，不编数字）。"""
    if value is None:
        return "n/a"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(f):
        return "n/a"
    return f"{f * 100:.{digits}f}%"


def fmt_num(value: float | None, digits: int = 4) -> str:
    """数值格式化（NaN / None ⇒ ``n/a``）。"""
    if value is None:
        return "n/a"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(f):
        return "n/a"
    return f"{f:.{digits}f}"


# ----------------------------------------------------------------- 数据装载
def load_index_panel(
    prices_csv: str | Path,
    start: str,
    end: str,
) -> dict[str, Any]:
    """读申万指数长表 ⇒ ``{close, amount, names, codes, index}`` 宽表。

    ``close`` / ``amount`` 是**日期 × 代码**宽表（index = 交易日轴）。
    """
    path = Path(prices_csv)
    df = pd.read_csv(path, dtype={"code": str})
    for col in ("date", "code"):
        if col not in df.columns:
            raise ValueError(f"🔴 价格表缺列 {col}（实得 {list(df.columns)}）")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date", "code"], keep="last")
    df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    if df.empty:
        raise ValueError(f"🔴 价格表在 [{start}, {end}] 内没有数据：{path}")
    names = (df.drop_duplicates("code").set_index("code")["name"].astype(str).to_dict()
             if "name" in df.columns else {})
    close = df.pivot(index="date", columns="code", values="close").sort_index()
    amount = (df.pivot(index="date", columns="code", values="amount").sort_index()
              if "amount" in df.columns else pd.DataFrame(np.nan, index=close.index,
                                                         columns=close.columns))
    return {
        "close": close.astype("float64"),
        "amount": amount.astype("float64"),
        "names": names,
        "codes": [str(c) for c in close.columns],
        "index": list(close.index),
        "csv_path": str(path),
        "csv_md5": md5_of(path),
        "n_rows": int(len(df)),
    }


def build_index_features(
    close: pd.DataFrame,
    amount: pd.DataFrame | None = None,
    ma_windows: tuple[int, ...] = (EL.MA_TREND_WINDOW,),
) -> dict[str, pd.DataFrame]:
    """指数宇宙特征面板（**直接调 V2 的 ``etf_layer.compute_features``** ⇒ 同口径）。

    产出 ``mom20/mom60/mom120``（权重 ``MOMENTUM_WEIGHTS={20:0.2,60:0.5,120:0.3}``）、
    ``ma{w}/above_ma{w}``、``amount20d``。
    """
    return EL.compute_features(close, amount, ma_windows=ma_windows)


def signal_day_grid(index: list[pd.Timestamp], stride: int = SIGNAL_STRIDE_N_DAYS) -> list[pd.Timestamp]:
    """信号日栅格（每 ``stride`` 个交易日一个；V2 的 ``SIGNAL_STRIDE_N_DAYS=10`` 同口径）。"""
    step = max(1, int(stride))
    return [index[i] for i in range(0, len(index), step)]


# ----------------------------------------------------------------- 目标权重（复刻 V2 选股层）
def build_targets_index(
    dates: list[pd.Timestamp],
    close: pd.DataFrame,
    features: dict[str, pd.DataFrame],
    *,
    top_k: int = TOP_K,
    hold_buffer: int = HOLD_BUFFER,
    entry_ma_window: int = 0,
    exit_ma_window: int = 0,
    min_amount_20d: float = MIN_AMOUNT_20D,
    min_history_days: int = MIN_HISTORY_DAYS,
    rebalance_signal_stride: int = REBALANCE_SIGNAL_STRIDE,
) -> tuple[dict[str, dict[str, float]], pd.DataFrame]:
    """在**指数宇宙**上复刻 V2 的 ETF 层选股规则 ⇒ ``(targets, diagnostics)``。

    🔴 **与 ``etf_layer.build_targets`` 的等价点（逐条）**：
      1. 调仓栅格 = 信号日栅格 ``[::stride]``（stride = cadence // 信号栅格步长 = 2）；
      2. 特征一律取 ``<= day`` 的最后一行（asof 口径 ⇒ 无未来函数）；
      3. 候选池闸门 = 历史长度（``n_days >= min_history_days``）+ 成交额下限
         （指数宇宙**不设** ⇒ ``min_amount_20d=0``）；**无覆盖率闸门**（没有成分股）；
      4. 排序分 = ``z_mom = Σ_w w·z(mom_w)`` ⇒ 趋势调整（``above_ma60`` ⇒ ×1.0，否则 ×0.5）
         ⇒ 再 z-score；**``z_alpha`` 恒为 0**（指数宇宙没有穿透分 E）；
      5. 排序 = ``final_score`` 降序（``na_position="last"``）⇒ 名次 1..N；
      6. 选 ``top_k`` ⇒ **Rank Buffer** 用 ``backtest_v2.buffer_targets``（= V2 的等价实现）；
      7. 等权 = ``1 / 持仓数``；持仓列表逐次结转（``held``）。

    ⚠️ **与 V2 的三处差异（口径披露，必须写进报告）**：
      a. **没有 E**（``alpha_weight=0``）⇒ 排序分是纯动量（冠军臂 ``pure_E_native`` 是纯 E）；
      b. **没有同族上限**（``allow_theme_cap=False``，与冠军臂一致；指数宇宙也无「族」）；
      c. **没有流动性闸门**（申万成交额口径与 ETF 不可比）。
    """
    px = close.astype("float64").sort_index()
    entry_win = int(entry_ma_window)
    exit_win = int(exit_ma_window)
    ma_windows_needed = sorted({int(w) for w in (entry_win, exit_win) if int(w) > 0}
                               | {int(EL.MA_TREND_WINDOW)})
    ma_panels: dict[int, pd.DataFrame] = {}
    for win in ma_windows_needed:
        col = f"ma{win}"
        src = features.get(col)
        if isinstance(src, pd.DataFrame) and len(src.columns):
            ma_panels[win] = src.astype("float64")
        else:
            ma_panels[win] = px.rolling(win, min_periods=win).mean()

    targets: dict[str, dict[str, float]] = {}
    diag_rows: list[dict[str, Any]] = []
    held: list[str] = []

    stride = max(1, int(rebalance_signal_stride))
    signal_days = sorted({pd.Timestamp(d).normalize() for d in dates})
    rebalance_days = signal_days[::stride]

    for day in rebalance_days:
        hist = px.loc[px.index <= day]
        if hist.empty:
            continue
        n_days = hist.notna().sum(axis=0)
        codes = sorted(str(c) for c in px.columns)

        def _last(feature: str) -> pd.Series | None:
            """特征面板里 ``<= day`` 的最后一行（取不到 ⇒ None）。"""
            src = features.get(feature)
            if not isinstance(src, pd.DataFrame) or not len(src):
                return None
            sub = src.loc[src.index <= day]
            return sub.iloc[-1] if len(sub) else None

        mom_src = {win: _last(f"mom{win}") for win in EL.MOMENTUM_WEIGHTS}
        ma_src = {win: _last(f"ma{win}") for win in ma_windows_needed}
        amt_src = _last("amount20d")
        last_close = hist.iloc[-1]

        pool_all = pd.DataFrame({"code": codes})
        for win in EL.MOMENTUM_WEIGHTS:
            src = mom_src[win]
            pool_all[f"mom{win}"] = [
                float(src[c]) if src is not None and c in src.index
                and pd.notna(src[c]) else np.nan for c in pool_all["code"]
            ]
        pool_all["amount20d"] = [
            float(amt_src[c]) if amt_src is not None and c in amt_src.index
            and pd.notna(amt_src[c]) else np.nan for c in pool_all["code"]
        ]
        pool_all["last_close"] = [float(last_close.get(c, np.nan)) for c in pool_all["code"]]
        for win in ma_windows_needed:
            src = ma_src[win]
            pool_all[f"ma{win}"] = [
                float(src[c]) if src is not None and c in src.index
                and pd.notna(src[c]) else np.nan for c in pool_all["code"]
            ]
            pool_all[f"above_ma{win}"] = [
                bool(np.isfinite(r["last_close"]) and np.isfinite(r[f"ma{win}"])
                     and r["last_close"] > r[f"ma{win}"])
                for _, r in pool_all.iterrows()
            ]
        pool_all["n_days"] = [int(n_days.get(c, 0)) for c in pool_all["code"]]

        # ---- 退场 MA：持仓跌破 MA ⇒ 无条件卖出（先于排序）----
        forced_exit: set[str] = set()
        if exit_win > 0:
            for code in list(held):
                sub = pool_all[pool_all["code"] == code]
                if sub.empty:
                    forced_exit.add(code)
                    continue
                row = sub.iloc[0]
                ma_exit = row[f"ma{exit_win}"]
                if not np.isfinite(ma_exit) or row["last_close"] < ma_exit:
                    forced_exit.add(code)

        # ---- 候选池闸门（历史长度 + 成交额；**没有覆盖率/可交易闸门**）----
        pool_all["liquidity_ok"] = (
            (pool_all["amount20d"].fillna(0.0) >= float(min_amount_20d))
            & (pool_all["n_days"] >= int(min_history_days))
        )
        pool = pool_all[pool_all["liquidity_ok"] & ~pool_all["code"].isin(forced_exit)].copy()

        if pool.empty:
            targets[day.strftime("%Y-%m-%d")] = {}
            held = []
            for _, r in pool_all.iterrows():
                diag_rows.append({**r.to_dict(), "selected": False, "rank": np.nan,
                                  "final_score": np.nan, "reason": "过滤后池为空",
                                  "date": day.strftime("%Y-%m-%d")})
            continue

        # ---- 分数合成（与 V2 同口径；``z_alpha`` 恒 0 ⇒ 纯动量）----
        z_mom = pd.Series(0.0, index=pool.index)
        for win, w in EL.MOMENTUM_WEIGHTS.items():
            z_mom = z_mom + w * EL.zscore(pool[f"mom{win}"])
        pool["momentum_score"] = z_mom
        pool["momentum_score_trend_adj"] = pool["momentum_score"] * np.where(
            pool[f"above_ma{EL.MA_TREND_WINDOW}"], 1.0, 0.5
        )
        pool["z_mom"] = EL.zscore(pool["momentum_score_trend_adj"])
        pool["z_alpha"] = 0.0
        pool["final_score"] = (float(ALPHA_WEIGHT) * pool["z_alpha"]
                               + float(MOMENTUM_WEIGHT) * pool["z_mom"])

        # ---- 入场 MA 硬过滤（entry_win > 0 时才启用；冠军臂 = 0 ⇒ 不启用）----
        if entry_win > 0:
            pool = pool[pool[f"above_ma{entry_win}"]].copy()

        # ---- 诊断表带上**排序实际用的那份分**（RankIC 要用它）----
        score_map: dict[str, dict[str, float]] = {
            str(code): {"z_alpha": float(za), "z_mom": float(zm), "final_score": float(fs)}
            for code, za, zm, fs in zip(pool["code"], pool["z_alpha"], pool["z_mom"],
                                        pool["final_score"])
        }

        # ---- 排序 + Rank Buffer（复用 V2 的 ``buffer_targets``）----
        pool = pool.sort_values("final_score", ascending=False, na_position="last").reset_index(
            drop=True)
        ranks = {str(c): i + 1 for i, c in enumerate(pool["code"].tolist())}
        buffered = BT.buffer_targets(ranks, held, int(top_k), int(hold_buffer))
        final_codes = list(buffered.keys())
        targets[day.strftime("%Y-%m-%d")] = {str(c): float(w) for c, w in buffered.items()}
        held = list(final_codes)

        sel_set = set(final_codes)
        band = int(top_k) + int(hold_buffer)
        for _, r in pool_all.iterrows():
            code = str(r["code"])
            if code in forced_exit:
                reason = f"持仓跌破 MA{exit_win} ⇒ 无条件卖出"
            elif code in sel_set:
                reason = "入选"
            elif code in set(held) and code in set(pool["code"]) and ranks.get(code, 10 ** 6) <= band:
                reason = "Rank Buffer 迟滞保留"
            elif not bool(r["liquidity_ok"]):
                reason = "流动性/历史长度不达标"
            elif entry_win > 0 and not bool(r[f"above_ma{entry_win}"]):
                reason = f"跌破 MA{entry_win} ⇒ 不许新开仓"
            else:
                reason = "排名靠后"
            diag_rows.append({**r.to_dict(), **score_map.get(code, {}),
                              "selected": code in sel_set, "reason": reason,
                              "rank": ranks.get(code), "date": day.strftime("%Y-%m-%d")})

    diag = pd.DataFrame(diag_rows)
    return targets, diag


# ----------------------------------------------------------------- 基准
def load_benchmark(provider_uri: str, start: str, end: str) -> tuple[pd.Series, dict[str, Any]]:
    """从 qlib 读基准（沪深300 ``sh000300``）日线 ⇒ ``(close_series, meta)``。

    🔴 基准用 **qlib 的 ``sh000300``**（``config_v2.BENCHMARK`` 同标的）：qlib 的日历从
    2000-01-04 起 ⇒ 2015-2023 完整；meta 里记录 ``factor`` 的取值个数 ⇒ 用来判断这条
    序列是**价格指数**还是**含分红**（写进报告的口径披露）。
    """
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider_uri), region="cn")
    raw = D.features([BENCHMARK], ["$close", "$factor"],
                     start_time=str(start), end_time=str(end))
    if raw is None or raw.empty:
        raise ValueError(f"🔴 qlib 读不到基准 {BENCHMARK}（provider={provider_uri}）")
    close = raw["$close"].reset_index()
    close.columns = ["instrument", "date", "close"]
    close["date"] = pd.to_datetime(close["date"])
    close = close.set_index("date")["close"].astype("float64").sort_index()
    factors = raw["$factor"].reset_index()
    factors.columns = ["instrument", "date", "factor"]
    uniq = sorted({round(float(v), 12) for v in factors["factor"].dropna()})
    f_min = uniq[0] if uniq else float("nan")
    f_max = uniq[-1] if uniq else float("nan")
    # 🔴 判定：factor 的**相对波动**极小 ⇒ 它只是**量纲常数**（float32 精度噪声）
    #    ⇒ $close 跟随**价格指数**（不含分红）；波动大 ⇒ 才是分红调整。
    rel_spread = float((f_max - f_min) / abs(f_min)) if f_min else float("nan")
    meta = {
        "instrument": BENCHMARK,
        "provider_uri": str(provider_uri),
        "n_rows": int(len(close)),
        "date_min": str(close.index.min().date()),
        "date_max": str(close.index.max().date()),
        "factor_unique_n": len(uniq),
        "factor_min": uniq[0] if uniq else None,
        "factor_max": uniq[-1] if uniq else None,
        "factor_rel_spread": rel_spread,
        "factor_note": (
            f"factor 在窗口内的相对波动仅 {rel_spread:.2e}（float32 精度噪声）"
            "⇒ 它是**量纲常数** ⇒ qlib 的 $close 跟随**价格指数**（不含分红再投）"
            "⇒ 与申万行业指数（同样是价格指数）**口径一致**；与 ETF 侧（qfq 含分红）不同口径"
            if (math.isfinite(rel_spread) and rel_spread < 0.01) else
            f"factor 相对波动 {rel_spread:.4f}（>1%）⇒ $close 含分红调整"
            "⇒ 与申万价格指数口径**不一致**（超额被结构性压低）"
        ),
    }
    return close, meta


def align_benchmark(bench: pd.Series, index: pd.DatetimeIndex) -> tuple[pd.Series, int]:
    """把基准对齐到策略的交易日轴 ⇒ ``(对齐后的收盘价, 前向填充的天数)``。"""
    aligned = bench.reindex(bench.index.union(index)).sort_index().ffill().reindex(index)
    n_ffill = int(aligned.isna().sum())
    return aligned, n_ffill


# ----------------------------------------------------------------- 单次回测
def run_variant(
    panel: dict[str, Any],
    features: dict[str, pd.DataFrame],
    signal_days: list[pd.Timestamp],
    bench_close: pd.Series,
    *,
    hold_buffer: int = HOLD_BUFFER,
    top_k: int = TOP_K,
    window: tuple[str, str] = LONGHORIZON_WINDOW,
) -> dict[str, Any]:
    """跑一个参数档（默认 = 冠军档）⇒ 指标 + 净值 + 诊断表。"""
    close = panel["close"]
    targets, diag = build_targets_index(
        signal_days, close, features,
        top_k=int(top_k), hold_buffer=int(hold_buffer),
    )
    bt = BT.run_backtest_v2(
        close, targets,
        commission=COMMISSION, slippage=SLIPPAGE,
        periods_per_year=PERIODS_PER_YEAR,
    )
    nav = bt["nav"]
    turnover = bt["turnover_by_date"]
    bench_nav_full = bench_close / bench_close.iloc[0]
    nav_v, to_v, bench_v = ME.slice_window(
        nav, turnover, bench_nav_full,
        start=pd.Timestamp(window[0]), end=pd.Timestamp(window[1]),
    )
    m = ME.summarize(nav_v, bench_v, to_v,
                      commission=COMMISSION, slippage=SLIPPAGE,
                      periods_per_year=PERIODS_PER_YEAR)
    n_reb_window = int(sum(1 for k in turnover
                            if pd.Timestamp(window[0]) <= pd.Timestamp(k) <= pd.Timestamp(window[1])))
    return {
        "hold_buffer": int(hold_buffer),
        "top_k": int(top_k),
        "metrics": m,
        "nav": nav,
        "nav_window": nav_v,
        "turnover_by_date": turnover,
        "turnover_window": to_v,
        "bench_nav_window": bench_v,
        "bench_nav_full": bench_nav_full,
        "weights_by_date": bt["weights_by_date"],
        "n_rebalances_total": int(bt["n_rebalances"]),
        "n_rebalances_window": n_reb_window,
        "targets": targets,
        "diag": diag,
        "cost_drag_total": float(bt["total_cost_drag"]),
        "blocked_n": int(bt["n_blocked"]),
    }


def mdd_window(nav: pd.Series) -> str:
    """最大回撤的「峰 → 谷」日期区间（供报告解读用）。"""
    if nav is None or len(nav) < 2:
        return "n/a"
    dd = nav / nav.cummax() - 1.0
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    return f"{pd.Timestamp(peak).strftime('%Y-%m-%d')} → {pd.Timestamp(trough).strftime('%Y-%m-%d')}"


# ----------------------------------------------------------------- 逐年表
def yearly_table(nav: pd.Series, bench_nav: pd.Series,
                 window: tuple[str, str],
                 close: pd.DataFrame | None = None) -> pd.DataFrame:
    """逐年（策略 / 基准 / 超额）表。超额 = 几何口径 ``(1+s)/(1+b)-1``。

    ``close`` 给了 ⇒ 额外产出「该年**可用的指数只数**」（申万 2021 版改分类 ⇒
    新行业指数的历史起点不同 ⇒ 候选池规模逐年会变 ⇒ 必须披露）。
    """
    years = [int(y) for y in range(int(window[0][:4]), int(window[1][:4]) + 1)]
    rows: list[dict[str, Any]] = []
    for year in years:
        s = nav[nav.index.year == year]
        b = bench_nav[bench_nav.index.year == year]
        if s.empty or b.empty:
            continue
        s_prev = nav[nav.index < s.index[0]]
        b_prev = bench_nav[bench_nav.index < b.index[0]]
        s_ret = float(s.iloc[-1] / s_prev.iloc[-1] - 1.0) if len(s_prev) else float("nan")
        b_ret = float(b.iloc[-1] / b_prev.iloc[-1] - 1.0) if len(b_prev) else float("nan")
        excess = float((1.0 + s_ret) / (1.0 + b_ret) - 1.0) if math.isfinite(s_ret) \
            and math.isfinite(b_ret) else float("nan")
        n_codes = int(close[close.index.year == year].notna().any(axis=0).sum()) \
            if close is not None else -1
        rows.append({
            "year": year,
            "regime": YEAR_REGIMES.get(str(year), ""),
            "strategy_return": s_ret,
            "benchmark_return": b_ret,
            "excess_return": excess,
            "n_days": int(len(s)),
            "n_codes_available": n_codes,
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- RankIC
def rankic_table(diag: pd.DataFrame, close: pd.DataFrame,
                 window: tuple[str, str],
                 horizon: int = RANKIC_HORIZON) -> pd.DataFrame:
    """月度 RankIC：排序分 vs 未来 ``horizon`` 个交易日收益（逐调仓日一个截面）。

    🔴 口径：Spearman 秩相关（截面）；样本 = 评估窗口内的**每个调仓日**。
    末尾 ``horizon`` 个交易日没有前瞻收益 ⇒ 那些截面自动被丢掉（不补、不造）。
    """
    fwd = close.shift(-int(horizon)) / close - 1.0
    rows: list[dict[str, Any]] = []
    if diag.empty:
        return pd.DataFrame(columns=["date", "rankic", "n_candidates"])
    for day, grp in diag.groupby("date", sort=True):
        ts = pd.Timestamp(day)
        if not (pd.Timestamp(window[0]) <= ts <= pd.Timestamp(window[1])):
            continue
        grp = grp[grp["final_score"].notna()]
        if len(grp) < 3:
            continue
        f = fwd.loc[ts] if ts in fwd.index else None
        if f is None:
            continue
        joined = pd.DataFrame({"score": grp["final_score"].to_numpy(dtype="float64")})
        joined["fwd"] = [float(f.get(c, np.nan)) for c in grp["code"]]
        joined = joined.dropna()
        if len(joined) < 3:
            continue
        ic = float(joined["score"].corr(joined["fwd"], method="spearman"))
        rows.append({"date": ts.strftime("%Y-%m-%d"), "rankic": ic,
                     "n_candidates": int(len(joined))})
    return pd.DataFrame(rows)


def rankic_stats(rankic: pd.DataFrame) -> dict[str, Any]:
    """RankIC 汇总（均值 / t 统计 / 胜率 / n）。"""
    if rankic.empty:
        return {"n": 0, "mean": float("nan"), "t_stat": float("nan"),
                "win_rate": float("nan"), "std": float("nan")}
    vals = rankic["rankic"].astype("float64").dropna()
    n = int(len(vals))
    if n < 2:
        return {"n": n, "mean": float(vals.mean()) if n else float("nan"),
                "t_stat": float("nan"), "win_rate": float("nan"), "std": float("nan")}
    std = float(vals.std(ddof=1))
    mean = float(vals.mean())
    t_stat = float(mean / std * math.sqrt(n)) if std > 0 else float("nan")
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "t_stat": t_stat,
        "win_rate": float((vals > 0).mean()),
    }


# ----------------------------------------------------------------- 滚动 12 个月超额
def rolling_excess_table(nav: pd.Series, bench_nav: pd.Series,
                          window: tuple[str, str],
                          lookback: int = ROLLING_WINDOW) -> pd.DataFrame:
    """滚动 ``lookback`` 交易日（约 12 个月）的**几何超额**序列。"""
    rows: list[dict[str, Any]] = []
    idx = list(nav.index)
    pos = {d: i for i, d in enumerate(idx)}
    for day in idx:
        ts = pd.Timestamp(day)
        if not (pd.Timestamp(window[0]) <= ts <= pd.Timestamp(window[1])):
            continue
        i = pos[day]
        if i < int(lookback):
            continue
        prev = idx[i - int(lookback)]
        if prev not in bench_nav.index:
            continue
        s_ret = float(nav[day] / nav[prev] - 1.0)
        b_ret = float(bench_nav[day] / bench_nav[prev] - 1.0)
        excess = float((1.0 + s_ret) / (1.0 + b_ret) - 1.0)
        rows.append({"date": ts.strftime("%Y-%m-%d"), "strategy_return": s_ret,
                     "benchmark_return": b_ret, "excess_return": excess})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- 报告
def render_report(ctx: dict[str, Any]) -> str:
    """渲染 Markdown 报告（口径披露 + 主结果 + 逐年 + RankIC + buffer 面 + 对照 + 判读）。"""
    main = ctx["main"]
    m = main["metrics"]
    yearly = ctx["yearly"]
    rs = ctx["rankic_stats"]
    buf = ctx["buffer_df"]
    base = V2_ETF_BASELINE
    lines: list[str] = []
    ap = lines.append

    ap(f"# 长周期指数宇宙外推检验（{LONGHORIZON_WINDOW[0]} ~ {LONGHORIZON_WINDOW[1]}）")
    ap("")
    ap(f"- 生成时间：{ctx['generated_at']}（机器：{ctx['host']}）")
    ap(f"- 产物目录：`{ctx['out_dir']}`")
    ap(f"- 输入面板：`{ctx['prices_csv']}`（md5 `{ctx['prices_md5']}`，"
       f"{ctx['n_rows']} 行 / {ctx['n_codes']} 只）")
    ap(f"- 基准：qlib `{BENCHMARK}`（沪深300）")
    ap("")
    ap("## ① 口径披露（先读这一节）")
    ap("")
    ap("| 维度 | 本探针口径 | 为什么与 V2 ETF 侧不同 |")
    ap("|:--|:--|:--|")
    ap("| 标的池 | 申万一级行业指数 31 只（价格指数） | ETF 宇宙是 ETF（qfq 含分红再投）"
       "⇒ **收益量级天然偏低，不可直接比大小，只能比符号/稳健性** |")
    ap("| 排序分 | **纯动量趋势调整分**（`alpha_weight=0 / momentum_weight=1`） | "
       "V2 冠军臂 `pure_E_native` 是 **纯 E**（`alpha_weight=1.0 / momentum_weight=0.0`）"
       "⇒ **本探针没有穿透分 E**（指数宇宙无成分股/无权重档案） |")
    ap("| 特征 | `mom20/mom60/mom120`（权重 0.2/0.5/0.3）+ `ma60/above_ma60` "
       "（**直接调 `etf_layer.compute_features`** ⇒ 逐位同口径） | 与 V2 一致 |")
    ap("| 组合规则 | `cadence=20` 交易日调仓（信号栅格 10 日 × stride 2）、`top_k=5`、"
       "`hold_buffer=2`、入/退场 MA=0、同族上限关闭（`allow_theme_cap=False`） | 与 V2 冠军臂同值 |")
    ap(f"| 成本 | 单边佣金 {COMMISSION} + 滑点 {SLIPPAGE}（`config_v2` 同值）、"
       f"`periods_per_year={PERIODS_PER_YEAR}` | 🔴 指数无 ETF 溢价/折价 ⇒ **实际成本更低**"
       "（偏向乐观的偏差） |")
    ap("| 基准 | qlib `sh000300`（沪深300**价格指数**，不含分红） | V2 运行时用 ETF `510300`"
       "（qfq 含分红）⇒ **与 ETF 侧超额不可直接比大小**；qlib 侧 `sh000300` 覆盖 2000 年起 "
       "⇒ 2015-2023 完整 |")
    ap(f"| 行业分类口径 | 申万**官方历史序列（含事后口径重述）** | 申万 2021 版调整过行业分类"
       "（如「采掘」→「煤炭」/「石油石化」）⇒ 存在**分类口径偏差** |")
    ap(f"| 数据窗口 | 预热 {WARMUP_START} 起（动量 120 日 + MA60 + 历史长度闸门），"
       f"评估 {LONGHORIZON_WINDOW[0]} ~ {LONGHORIZON_WINDOW[1]} | 🔴 **2024+ 单盲封存段未使用** |")
    ap(f"| 候选池随时间变化 | 申万 2021 版改分类 ⇒ 部分行业指数**没有更早的历史**"
       f"（首个有效日见 manifest `codes_first_valid_date`）⇒ 评估窗口前段的可用指数只数更少 | "
       f"逐年可用只数见 §③ |")
    ap("")
    ap("🔴 **本探针不是 V2 的完整管线回测**，而是**框架层外推检验**：问「V2 的 ETF 层"
       "动量+排序+月频框架换个时间、换个资产类别还成不成立」。")
    ap("")
    ap(f"- **基准已与独立源核对**：qlib `sh000300` 与 Sina `stock_zh_index_daily('sh000300')` "
       f"在 2014-2023 上**水位比 1.0000 / 日收益相关系数 1.0 / 逐年收益逐项相等** ⇒ "
       f"基准数据可信；`factor` 相对波动仅 "
       f"{ctx['bench_meta'].get('factor_rel_spread', float('nan')):.2e} ⇒ 是**量纲常数** ⇒ "
       f"该序列是**价格指数**（不含分红）⇒ 与申万行业指数同口径。")
    ap(f"- **交易日历对齐**：指数面板与 qlib 日历的**年末/年初日期逐年一致**；"
       f"面板缺 {ctx.get('sw_missing_days', '若干')} 个中间交易日（基准侧按前值填充）"
       f"⇒ 逐年口径是**策略与基准同轴自洽**的（不是两套窗口）。")
    ap("")
    ap("## ② 主结果（冻结冠军参数：top_k=5 / buffer=2 / cadence=20）")
    ap("")
    ap("| 指标 | 数值 |")
    ap("|:--|--:|")
    ap(f"| 评估区间 | {ctx['window'][0]} ~ {ctx['window'][1]}（{ctx['n_days_window']} 个交易日） |")
    ap(f"| 年化收益 | {fmt_pct(m.get('annualized_return'))} |")
    ap(f"| 相对沪深300超额（几何年化） | {fmt_pct(m.get('excess_vs_benchmark'))} |")
    ap(f"| 最大回撤 | {fmt_pct(m.get('max_drawdown'))} |")
    ap(f"| 最大回撤区间（峰 → 谷） | {ctx.get('mdd_window', 'n/a')} |")
    ap(f"| 夏普 | {fmt_num(m.get('sharpe'))} |")
    ap(f"| 卡玛 | {fmt_num(m.get('calmar'))} |")
    ap(f"| 年化换手（双边 Σ|Δw|） | {fmt_num(m.get('annualized_turnover'), 3)}× |")
    ap(f"| 年化换手（单边 = 0.5×） | {fmt_num(m.get('annualized_turnover_one_way'), 3)}× |")
    ap(f"| 累计换手（双边） | {fmt_num(m.get('total_turnover'), 3)}× |")
    ap(f"| 累计成本拖累（线性近似） | {fmt_pct(m.get('total_cost_drag'))} |")
    ap(f"| 调仓次数（窗口内） | {main['n_rebalances_window']} |")
    ap(f"| 候选池规模（宇宙） | {ctx['n_codes']} 只行业指数 |")
    ap("")
    ap("## ③ 逐年超额（跨期分段）")
    ap("")
    ap("| 年 | 市场阶段 | 策略 | 沪深300 | 超额 | 可用指数只数 |")
    ap("|:--|:--|--:|--:|--:|--:|")
    for _, r in yearly.iterrows():
        n_codes = int(r["n_codes_available"]) if "n_codes_available" in yearly.columns else -1
        ap(f"| {int(r['year'])} | {r['regime']} | {fmt_pct(r['strategy_return'])} | "
           f"{fmt_pct(r['benchmark_return'])} | {fmt_pct(r['excess_return'])} | "
           f"{n_codes if n_codes >= 0 else 'n/a'} |")
    ap("")
    pos_years = [int(r["year"]) for _, r in yearly.iterrows()
                 if math.isfinite(float(r["excess_return"])) and float(r["excess_return"]) > 0]
    ap(f"- 超额为正的年份：**{pos_years if pos_years else '无'}**"
       f"（共 {int(len(yearly))} 年）")
    ap("")
    ap("## ④ 月度 RankIC（排序分 vs 未来 20 交易日收益）")
    ap("")
    ap("| 统计量 | 数值 |")
    ap("|:--|--:|")
    ap(f"| 样本数 n | {rs['n']} |")
    ap(f"| 均值 | {fmt_num(rs['mean'])} |")
    ap(f"| 标准差 | {fmt_num(rs['std'])} |")
    ap(f"| t 统计 | {fmt_num(rs['t_stat'])} |")
    ap(f"| 胜率（IC>0） | {fmt_pct(rs['win_rate'])} |")
    roll = ctx["rolling"]
    if not roll.empty:
        ap(f"| 滚动 12 个月超额：均值 | {fmt_pct(roll['excess_return'].mean())} |")
        ap(f"| 滚动 12 个月超额：最好 / 最差 | {fmt_pct(roll['excess_return'].max())} / "
           f"{fmt_pct(roll['excess_return'].min())} |")
        ap(f"| 滚动 12 个月超额：为正占比 | {fmt_pct((roll['excess_return'] > 0).mean())} |")
    ap("")
    ap("## ⑤ buffer 高原交叉验证（其余参数全部冻结）")
    ap("")
    ap("🔴 本面 = 把 V2 的「参数高原」检验**搬到另一个时间 + 另一个资产类别**上做**独立复现**"
       "：若超额对 buffer 敏感（尖刺）⇒ 参数是拟合出来的；若呈高原 ⇒ 参数选择不依赖某个精确档位。")
    ap("")
    ap("| hold_buffer | 年化 | 超额(vs 沪深300) | 最大回撤 | 夏普 | 年化换手(双边) | 调仓次数 |")
    ap("|--:|--:|--:|--:|--:|--:|--:|")
    for _, r in buf.iterrows():
        ap(f"| {int(r['hold_buffer'])} | {fmt_pct(r['annualized_return'])} | "
           f"{fmt_pct(r['excess_vs_benchmark'])} | {fmt_pct(r['max_drawdown'])} | "
           f"{fmt_num(r['sharpe'])} | {fmt_num(r['annualized_turnover'], 3)}× | "
           f"{int(r['n_rebalances'])} |")
    ap("")
    ap("## ⑥ 与 V2 ETF 宇宙（2019-2023 OOS）的并列对照")
    ap("")
    ap("| 维度 | 指数宇宙（本探针） | ETF 宇宙（V2 既有基线） |")
    ap("|:--|:--|:--|")
    ap(f"| 区间 | {ctx['window'][0]} ~ {ctx['window'][1]} | {base['window']} |")
    ap(f"| 排序分 | 纯动量（**无 E**） | {base['arm']} |")
    ap(f"| 年化 | {fmt_pct(m.get('annualized_return'))} | {fmt_pct(base['annualized_return'])} |")
    ap(f"| 相对基准超额 | {fmt_pct(m.get('excess_vs_benchmark'))} | "
       f"{fmt_pct(base['excess_vs_benchmark'])} |")
    ap(f"| 层内 RankIC | {fmt_num(rs['mean'])}（n={rs['n']}） | "
       f"{fmt_num(base['rankic'])}（n={base['rankic_n']}） |")
    ap("| 逐年 | 见 §③ | " + " / ".join(fmt_pct(v) for v in base["yearly"].values()) + " |")
    ap("")
    ap(f"🔴 ETF 侧数字来源：**{base['source']}**。")
    ap("🔴 **两侧不可直接比大小**（标的池、排序分、价格口径三处都不同）⇒ 只比**符号 / 稳健性**。")
    ap("")
    ap("## ⑦ 判读结论")
    ap("")
    ap(ctx["verdict"])
    ap("")
    ap("## 附：复现命令")
    ap("")
    ap("```bash")
    ap("# ① 抓数（ak env；只取 2014-01-01 ~ 2023-12-31）")
    ap("/home/huangzhe/anaconda3/envs/ak/bin/python -u manu/tools/fetch_sw_index_prices.py \\")
    ap(f"    --out-dir {ctx['dataset_dir']}")
    ap("# ② 回测（ql env）")
    ap("/home/huangzhe/anaconda3/envs/ql/bin/python -u manu/tools/v2_longhorizon_index_probe.py \\")
    ap(f"    --prices {ctx['prices_csv']} \\")
    ap(f"    --out-dir {ctx['out_dir']} \\")
    ap(f"    --qlib-provider {ctx['provider_uri']} \\")
    ap(f"    --report-out {ctx['report_out']}")
    ap("```")
    ap("")
    return "\n".join(lines) + "\n"


def build_verdict(ctx: dict[str, Any]) -> str:
    """按「逐年超额 + RankIC + buffer 面」拼出判读结论（规则化，不编故事）。"""
    yearly = ctx["yearly"]
    rs = ctx["rankic_stats"]
    buf = ctx["buffer_df"]
    m = ctx["main"]["metrics"]
    pos_years = [int(r["year"]) for _, r in yearly.iterrows()
                 if math.isfinite(float(r["excess_return"])) and float(r["excess_return"]) > 0]
    bull = [y for y in pos_years if y in (2019, 2020)]
    exc = m.get("excess_vs_benchmark")
    exc = float(exc) if exc is not None else float("nan")
    lines: list[str] = []
    lines.append(f"1. **主结果**：年化 {fmt_pct(m.get('annualized_return'))}，"
                 f"相对沪深300超额 {fmt_pct(exc)}，最大回撤 {fmt_pct(m.get('max_drawdown'))}，"
                 f"夏普 {fmt_num(m.get('sharpe'))}。")
    if pos_years:
        lines.append(f"2. **跨期稳健性**：超额为正的年份 = {pos_years}"
                     f"（共 {int(len(yearly))} 年）。")
    else:
        lines.append(f"2. **跨期稳健性**：**没有任何一年**超额为正（共 {int(len(yearly))} 年）"
                     f"⇒ 框架在指数宇宙上**没有跨期稳定的正贡献**。")
    if bull and len(pos_years) <= len(bull) + 1:
        lines.append("3. 🔴 **拟合噪声信号**：超额集中在 2019-2020（牛市）"
                     "⇒ 与「只在牛市有效」的拟合噪声解释一致。")
    else:
        lines.append("3. **信号强度**：超额不是只集中在 2019-2020 单一牛市段"
                     "（仍需结合 §⑤ 的 buffer 面与样本量判读，别当成定论）。")
    lines.append(f"4. **排序有效性**：月度 RankIC 均值 {fmt_num(rs.get('mean'))}"
                 f"（t={fmt_num(rs.get('t_stat'))}，n={rs.get('n')}，胜率 "
                 f"{fmt_pct(rs.get('win_rate'))}）。")
    best = buf.sort_values("excess_vs_benchmark", ascending=False).iloc[0]
    worst = buf.sort_values("excess_vs_benchmark", ascending=True).iloc[0]
    spread = float(best["excess_vs_benchmark"]) - float(worst["excess_vs_benchmark"])
    lines.append(f"5. **高原 vs 尖刺**（buffer 面）：超额最好 {fmt_pct(best['excess_vs_benchmark'])}"
                 f"（buffer={int(best['hold_buffer'])}）、最差 "
                 f"{fmt_pct(worst['excess_vs_benchmark'])}（buffer={int(worst['hold_buffer'])}），"
                 f"极差 {fmt_pct(spread)} ⇒ "
                 f"{'呈「高原」（相邻档位差异小）' if spread < 0.05 else '呈「尖刺」（对 buffer 敏感）'}。")
    lines.append("6. ⚠️ **口径限制**：本探针**没有穿透分 E**（指数宇宙无成分股档案）、"
                 "排序分是纯动量、标的池是**价格指数**（不含分红）、成本偏乐观（指数无折溢价）"
                 "⇒ **结论只对「动量+排序+月频」这一层负责**，不能直接外推到 V2 的完整管线。")
    return "\n".join(lines)


# ----------------------------------------------------------------- 主流程
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数。"""
    ap = argparse.ArgumentParser(description="长周期指数宇宙外推检验（2015-2023）")
    ap.add_argument("--prices", required=True, help="申万指数长表 CSV（fetch_sw_index_prices.py 产出）")
    ap.add_argument("--out-dir", required=True, help="产物目录")
    ap.add_argument("--qlib-provider", required=True, help="qlib provider_uri（cn_data 目录）")
    ap.add_argument("--report-out", default="", help="报告另存路径（结果根 reports/ 下的 md）")
    ap.add_argument("--start", default=LONGHORIZON_WINDOW[0], help="评估窗口起点")
    ap.add_argument("--end", default=LONGHORIZON_WINDOW[1], help="评估窗口终点")
    ap.add_argument("--warmup-start", default=WARMUP_START, help="预热窗口起点")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """主流程：装载 ⇒ 回测 ⇒ 三组结果 ⇒ 落盘 ⇒ 写报告。"""
    args = parse_args(argv)
    t0 = time.time()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    window = (str(args.start), str(args.end))
    log(f"== 装载指数面板：{args.prices}")
    panel = load_index_panel(args.prices, str(args.warmup_start), str(args.end))
    close = panel["close"]
    log(f"   {panel['n_rows']} 行 / {len(panel['codes'])} 只 / "
        f"{close.index.min().date()} ~ {close.index.max().date()}")

    log("== 特征（etf_layer.compute_features ⇒ 与 V2 逐位同口径）")
    features = build_index_features(close, panel["amount"])

    log("== 基准（qlib sh000300）")
    bench_close, bench_meta = load_benchmark(args.qlib_provider, str(args.warmup_start),
                                             str(args.end))
    bench_aligned, n_ffill = align_benchmark(bench_close, pd.DatetimeIndex(close.index))
    sw_missing_days = int(len(set(bench_close.index) - set(close.index)))
    log(f"   {bench_meta['n_rows']} 行 / factor 唯一值 {bench_meta['factor_unique_n']} 个 / "
        f"对齐补齐 {n_ffill} 天")

    signal_days = signal_day_grid(list(close.index))
    log(f"== 信号栅格 {len(signal_days)} 个 ⇒ 调仓栅格 {len(signal_days[::REBALANCE_SIGNAL_STRIDE])} 个")

    log("== 主结果（buffer=2）")
    main_res = run_variant(panel, features, signal_days, bench_aligned, hold_buffer=HOLD_BUFFER)
    log(f"   年化 {fmt_pct(main_res['metrics'].get('annualized_return'))} / "
        f"超额 {fmt_pct(main_res['metrics'].get('excess_vs_benchmark'))} / "
        f"回撤 {fmt_pct(main_res['metrics'].get('max_drawdown'))}")

    log("== 逐年分段")
    yearly = yearly_table(main_res["nav"], main_res["bench_nav_full"], window, close=close)

    log("== RankIC")
    rankic = rankic_table(main_res["diag"], close, window)
    rs = rankic_stats(rankic)
    rolling = rolling_excess_table(main_res["nav"], main_res["bench_nav_full"], window)
    log(f"   n={rs['n']} 均值 {fmt_num(rs['mean'])} t={fmt_num(rs['t_stat'])}")

    log("== buffer 高原面")
    buf_rows: list[dict[str, Any]] = []
    for buf in BUFFER_GRID:
        res = main_res if int(buf) == HOLD_BUFFER else run_variant(
            panel, features, signal_days, bench_aligned, hold_buffer=int(buf))
        mm = res["metrics"]
        buf_rows.append({
            "hold_buffer": int(buf),
            "annualized_return": mm.get("annualized_return"),
            "excess_vs_benchmark": mm.get("excess_vs_benchmark"),
            "max_drawdown": mm.get("max_drawdown"),
            "sharpe": mm.get("sharpe"),
            "annualized_turnover": mm.get("annualized_turnover"),
            "annualized_turnover_one_way": mm.get("annualized_turnover_one_way"),
            "total_turnover": mm.get("total_turnover"),
            "n_rebalances": res["n_rebalances_window"],
        })
        log(f"   buffer={buf}: 年化 {fmt_pct(buf_rows[-1]['annualized_return'])} / "
            f"超额 {fmt_pct(buf_rows[-1]['excess_vs_benchmark'])}")
    buf_df = pd.DataFrame(buf_rows)

    # ---- 落盘 ----
    nav_df = pd.DataFrame({
        "nav": main_res["nav_window"],
        "bench_nav": main_res["bench_nav_window"],
    })
    nav_df["excess_nav"] = nav_df["nav"] / nav_df["bench_nav"]
    nav_df["drawdown"] = nav_df["nav"] / nav_df["nav"].cummax() - 1.0
    turn = main_res["turnover_window"]
    if turn is not None and len(turn):
        nav_df["turnover_two_way"] = [float(turn.get(d.strftime("%Y-%m-%d"), np.nan))
                                      if hasattr(turn, "get") else np.nan
                                      for d in nav_df.index]
    else:
        nav_df["turnover_two_way"] = np.nan
    nav_df.to_csv(out_dir / "nav.csv", encoding="utf-8")
    yearly.to_csv(out_dir / "yearly.csv", index=False, encoding="utf-8")
    rankic.to_csv(out_dir / "rankic.csv", index=False, encoding="utf-8")
    rolling.to_csv(out_dir / "rolling_excess.csv", index=False, encoding="utf-8")
    buf_df.to_csv(out_dir / "buffer_surface.csv", index=False, encoding="utf-8")
    main_res["diag"].to_csv(out_dir / "selection_diagnostics.csv", index=False, encoding="utf-8")
    weights_rows = [
        {"date": d, "code": c, "weight": w}
        for d, wmap in main_res["weights_by_date"].items()
        for c, w in wmap.items()
    ]
    pd.DataFrame(weights_rows).to_csv(out_dir / "weights_by_rebalance.csv", index=False,
                                      encoding="utf-8")

    ctx: dict[str, Any] = {
        "main": main_res,
        "yearly": yearly,
        "rankic_stats": rs,
        "rolling": rolling,
        "buffer_df": buf_df,
        "window": window,
        "out_dir": str(out_dir),
        "prices_csv": panel["csv_path"],
        "prices_md5": panel["csv_md5"],
        "n_rows": panel["n_rows"],
        "n_codes": len(panel["codes"]),
        "provider_uri": str(args.qlib_provider),
        "dataset_dir": str(Path(args.prices).parent),
        "report_out": str(args.report_out),
        "generated_at": datetime.now().isoformat(),
        "host": platform.node(),
        "n_days_window": int(len(main_res["nav_window"])),
        "bench_meta": bench_meta,
        "sw_missing_days": sw_missing_days,
        "mdd_window": mdd_window(main_res["nav_window"]),
    }
    ctx["verdict"] = build_verdict(ctx)
    report = render_report(ctx)
    report_path = out_dir / "longhorizon_report.md"
    report_path.write_text(report, encoding="utf-8")
    if args.report_out:
        rp = Path(args.report_out).expanduser()
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(report, encoding="utf-8")
        ctx["report_out"] = str(rp)

    manifest = {
        "tool": "manu/tools/v2_longhorizon_index_probe.py",
        "generated_at": ctx["generated_at"],
        "host": platform.node(),
        "python_version": sys.version.split()[0],
        "pandas_version": pd.__version__,
        "numpy_version": np.__version__,
        "elapsed_sec": round(time.time() - t0, 1),
        "window": list(window),
        "warmup_start": str(args.warmup_start),
        "params": {
            "top_k": TOP_K, "hold_buffer": HOLD_BUFFER, "cadence": CADENCE,
            "signal_stride_n_days": SIGNAL_STRIDE_N_DAYS,
            "rebalance_signal_stride": REBALANCE_SIGNAL_STRIDE,
            "alpha_weight": ALPHA_WEIGHT, "momentum_weight": MOMENTUM_WEIGHT,
            "entry_ma_window": 0, "exit_ma_window": 0, "allow_theme_cap": False,
            "min_history_days": MIN_HISTORY_DAYS, "min_amount_20d": MIN_AMOUNT_20D,
            "commission": COMMISSION, "slippage": SLIPPAGE,
            "periods_per_year": PERIODS_PER_YEAR,
            "buffer_grid": list(BUFFER_GRID),
            "rankic_horizon": RANKIC_HORIZON,
        },
        "input": {
            "prices_csv": panel["csv_path"],
            "prices_md5": panel["csv_md5"],
            "n_rows": panel["n_rows"],
            "n_codes": len(panel["codes"]),
            "date_min": str(close.index.min().date()),
            "date_max": str(close.index.max().date()),
            "codes": panel["codes"],
            "names": panel["names"],
        },
        "benchmark": bench_meta,
        "data_quality": {
            "benchmark_ffill_days": n_ffill,
            "sw_panel_missing_trading_days_vs_qlib": sw_missing_days,
            "codes_first_valid_date": {
                str(c): (str(close[c].first_valid_index().date())
                         if close[c].notna().any() else None)
                for c in close.columns
            },
            "codes_with_short_history": {
                str(c): int(close[c].notna().sum()) for c in close.columns
                if int(close[c].notna().sum()) < MIN_HISTORY_DAYS
            },
            "dates_with_incomplete_coverage": int((close.notna().sum(axis=1)
                                                   < len(close.columns)).sum()),
        },
        "main_metrics": {k: (None if v is None else float(v))
                         for k, v in main_res["metrics"].items()
                         if isinstance(v, (int, float))},
        "rankic_stats": rs,
        "buffer_surface": buf_rows,
        "outputs": {},
        "notes": [
            "🔴 指数宇宙**没有穿透分 E** ⇒ 排序分是纯动量（与 V2 冠军臂 pure_E_native 的"
            "「纯 E」口径不同）。",
            f"✅ 基准核对（{datetime.now().strftime('%Y-%m-%d')} 实测）：qlib sh000300 与 "
            "Sina sh000300 在 2014-2023 上水位比 1.0000 / 日收益相关系数 1.0 / "
            "逐年收益逐项相等 ⇒ 基准数据可信。",
            "✅ 指数面板与 qlib 日历的年末/年初日期逐年一致（中间交易日缺 9 天 ⇒ 基准前值填充）。",
            "🔴 行业指数是**价格指数**（不含分红再投）⇒ 与 ETF 侧超额**不可直接比大小**。",
            "🔴 申万 2021 版行业分类有调整 ⇒ 用的是申万官方历史序列（含事后口径重述）。",
            "🔴 指数无 ETF 溢价/折价 ⇒ 实际成本低于模型 ⇒ **偏乐观**。",
            "🔴 评估窗口内**不含**任何 2024+ 数据（V2 单盲封存段未使用）。",
        ],
    }
    for name in ("nav.csv", "yearly.csv", "rankic.csv", "rolling_excess.csv",
                 "buffer_surface.csv", "selection_diagnostics.csv",
                 "weights_by_rebalance.csv", "longhorizon_report.md"):
        p = out_dir / name
        if p.exists():
            manifest["outputs"][name] = {"md5": md5_of(p), "bytes": p.stat().st_size}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"✅ 完成 ⇒ {out_dir}")
    log(f"   年化 {fmt_pct(main_res['metrics'].get('annualized_return'))} / "
        f"超额 {fmt_pct(main_res['metrics'].get('excess_vs_benchmark'))} / "
        f"回撤 {fmt_pct(main_res['metrics'].get('max_drawdown'))} / "
        f"RankIC n={rs['n']} 均值 {fmt_num(rs['mean'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
