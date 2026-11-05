#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 全市场快照 + 可交易候选池抓取脚本（阶段 1/3）。

【数据源】
   1) ``ak.fund_etf_spot_em()``                 —— 东方财富场内 ETF 实时全市场快照（1664 只）
   2) ``ak.fund_etf_category_ths(symbol="ETF基金")`` —— 同花顺 ETF 分类（粗类型 + 申购/赎回状态）
   3) ``ak.fund_name_em()``                     —— 东方财富全市场基金名录（细粒度基金类型，ETF 全覆盖）

【产物】（落盘目录 ``--out-dir``，默认 ``/mnt/data/siping/datasets/etf``）
   - ``etf_spot.csv``      快照原样落盘 + 补 ``exchange`` 列（SH/SZ/BJ）
   - ``etf_universe.csv``  候选池（硬门槛过滤 + 上限截断）

【口径】
   - ``成交额`` / ``流通市值`` / ``总市值`` 单位均为 **元**（不是万元、不是亿元）。
   - ``流通市值 >= 2 亿元``      ⇒ 阈值 ``2e8`` 元。
   - ``20 日均成交额 >= 5000 万元`` ⇒ 阈值 ``5e7`` 元。
   - 快照阶段没有历史行情 ⇒ 暂用 **当日** ``成交额`` 代替日均（``liq_source=spot``）；
     历史行情抓到后由 ``--stage finalize`` 用真实 20 日均额重算（``liq_source=prices``）。
   - 基金类型 **不做任何预剔除**（股票型 / 指数型-固收 / 指数型-海外股票 / 货币型 /
     商品类 全部保留），后续防守资产由下游自行分桶（``fund_type_bucket``）。
   - 仅剔除硬伤：名称含 ``LOF`` / ``分级`` / ``退``（退市、退市整理）关键词者不入选；
     申购或赎回状态非正常（暂停 / 限制大额）**保留但打** ``needs_review=True``。

【单位 / 字段详见】``manu/etf_rotation/data/README.md``

【抓取时间】2026-11-04（A股交易日，行情日期 2026-11-04）

【执行机】服务器 ``huangzhe@192.168.99.40:32222``，
   解释器 ``/home/huangzhe/anaconda3/envs/ak/bin/python``（独立 ``ak`` env，
   **禁止**把 akshare 装进 qlib 的 ``ql`` env）

【可复现命令】
   python manu/etf_rotation/data/fetch_etf_universe.py --stage all
   python manu/etf_rotation/data/fetch_etf_universe.py --stage finalize   # 行情抓完后重算
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import socket
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

DEFAULT_OUT_DIR = "/mnt/data/siping/datasets/etf"

# 刷新口径统一放在 refresh_policy.py（纯函数；**ql env 也能测**）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_policy import (  # noqa: E402
    load_trading_dates,
    resolve_end_date,
    spot_snapshot_is_stale,
    today_compact,
)

# ---- 硬门槛阈值（单位：元）----
MIN_AVG_AMOUNT_20D = 50_000_000.0  # 5000 万元
MIN_FLOAT_MCAP = 200_000_000.0  # 2 亿元
MIN_HISTORY_DAYS = 120  # 至少 120 个交易日（供 120 日动量）
UNIVERSE_CAP = 250  # 候选池上限
FETCH_TOP = 300  # 抓历史行情的临时集合上限（> 候选池上限，给 finalize 重排留余量）

# 名称硬伤关键词 → 不入选
EXCLUDE_NAME_KEYWORDS = ("LOF", "分级", "退")


# ---------------------------------------------------------------- 基础工具
def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """先写 .tmp 再 os.replace —— 避免半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_akshare(fn: str, *args, **kwargs):
    """调 akshare 接口；屏蔽 tqdm 进度条噪声（异常照常抛出）。"""
    import akshare as ak  # 延迟导入：本机 host 无 akshare 时也能 py_compile 通过

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        return getattr(ak, fn)(*args, **kwargs)


def exchange_of(code: str) -> str:
    """代码首位 → 交易所。5/6/9 → 上交所(SH)；1/2/3 → 深交所(SZ)；4/8 → 北交所(BJ)。"""
    code = str(code).strip()
    if not code:
        return ""
    head = code[0]
    if head in ("5", "6", "9"):
        return "SH"
    if head in ("4", "8"):
        return "BJ"
    if head in ("1", "2", "3"):
        return "SZ"
    return "SZ"


def fund_type_bucket(fund_type: str) -> str:
    """细粒度基金类型 → 防守资产分桶（仅供下游参考，**不参与**筛选）。"""
    ft = str(fund_type or "")
    if ft.startswith("货币型"):
        return "money_market"
    if ft.startswith("债券型") or ft.startswith("指数型-固收"):
        return "bond"
    if "商品" in ft:
        return "commodity"
    if ft.startswith("指数型-海外") or ft.startswith("QDII"):
        return "cross_border"
    if ft.startswith("股票型") or ft.startswith("指数型-股票"):
        return "equity_cn"
    if ft.startswith("指数型-其他"):
        return "commodity"
    return "other"


# ---------------------------------------------------------------- 阶段 1：快照
def stage_spot(out_dir: Path, force: bool, target_compact: str | None = None) -> Path:
    spot_path = out_dir / "etf_spot.csv"
    # 🔴 口径（2026-11-05 05:3x 修）：**按快照里的「数据日期」判新鲜度**，不按「文件是否存在」判
    #    （旧口径 ⇒ 快照永远停在 2026-11-04 ⇒ 流动性 / 规模口径永远用旧数据，且**没有任何提示**）
    #    比较基准 = 「今天之前（含）的最近一个交易日」⇒ 非交易日跑不会反复重拉。
    target = target_compact or today_compact()
    if spot_path.exists() and not force:
        try:
            spot_df = pd.read_csv(spot_path)
        except Exception:
            spot_df = None
        stale, note = spot_snapshot_is_stale(spot_df, target)
        if not stale:
            log(f"spot 已是最新，跳过（{note}）: {spot_path}")
            return spot_path
        log(f"spot 需要重拉（{note}）: {spot_path}")

    log("拉取 ak.fund_etf_spot_em() ...")
    t0 = time.time()
    df = fetch_akshare("fund_etf_spot_em")
    log(f"spot 原始行数={len(df)} 耗时={time.time() - t0:.1f}s 列数={len(df.columns)}")

    df = df.copy()
    df["代码"] = df["代码"].astype(str).str.strip()
    df.insert(len(df.columns), "exchange", df["代码"].map(exchange_of))
    if "数据日期" in df.columns:
        df["数据日期"] = pd.to_datetime(df["数据日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "更新时间" in df.columns:
        df["更新时间"] = pd.to_datetime(df["更新时间"], errors="coerce").astype(str)

    atomic_write_csv(df, spot_path)
    log(f"spot 落盘: {spot_path} 行数={len(df)}")
    return spot_path


# ---------------------------------------------------------------- 阶段 2：候选池
def _load_metadata() -> pd.DataFrame:
    """拉取 ETF 分类 + 全市场基金名录，合成类型 / 申赎状态。"""
    log("拉取 ak.fund_name_em()（细粒度基金类型）...")
    fn = fetch_akshare("fund_name_em")
    fn = fn[["基金代码", "基金类型"]].copy()
    fn["基金代码"] = fn["基金代码"].astype(str).str.strip()
    fn = fn.rename(columns={"基金类型": "fund_type_fine"})
    fn = fn.drop_duplicates(subset=["基金代码"], keep="first")

    log("拉取 ak.fund_etf_category_ths(symbol='ETF基金')（粗类型 + 申赎状态）...")
    try:
        cat = fetch_akshare("fund_etf_category_ths", symbol="ETF基金")
        cat = cat[["基金代码", "基金类型", "申购状态", "赎回状态"]].copy()
        cat["基金代码"] = cat["基金代码"].astype(str).str.strip()
        cat = cat.rename(
            columns={
                "基金类型": "fund_type_coarse",
                "申购状态": "purchase_status",
                "赎回状态": "redeem_status",
            }
        )
        cat = cat.drop_duplicates(subset=["基金代码"], keep="first")
        log(f"  category_ths 行数={len(cat)}")
    except Exception as exc:  # noqa: BLE001
        log(f"  [WARN] category_ths 拉取失败: {type(exc).__name__}: {exc}")
        cat = pd.DataFrame(columns=["基金代码", "fund_type_coarse", "purchase_status", "redeem_status"])

    meta = fn.merge(cat, on="基金代码", how="outer")
    meta["fund_type"] = meta["fund_type_fine"].fillna(meta["fund_type_coarse"])
    meta["fund_type_src"] = meta["fund_type_fine"].notna().map({True: "fund_name_em", False: "category_ths"})
    meta["fund_type"] = meta["fund_type"].fillna("未知")
    meta["fund_type_bucket"] = meta["fund_type"].map(fund_type_bucket)
    return meta


def build_universe(out_dir: Path, fetch_top: int, universe_cap: int) -> pd.DataFrame:
    spot_path = out_dir / "etf_spot.csv"
    if not spot_path.exists():
        raise SystemExit(f"[FATAL] 缺少快照文件，请先跑 --stage spot: {spot_path}")

    spot = pd.read_csv(spot_path, dtype={"代码": str})
    spot["代码"] = spot["代码"].astype(str).str.strip()
    meta = _load_metadata()
    df = spot.merge(meta, left_on="代码", right_on="基金代码", how="left")
    df.drop(columns=[c for c in ("基金代码",) if c in df.columns], inplace=True)

    df["name"] = df["名称"].astype(str)
    df["code"] = df["代码"]
    df["last_price"] = pd.to_numeric(df["最新价"], errors="coerce")
    df["float_mcap"] = pd.to_numeric(df["流通市值"], errors="coerce")
    df["total_mcap"] = pd.to_numeric(df["总市值"], errors="coerce")
    df["spot_amount"] = pd.to_numeric(df["成交额"], errors="coerce")
    df["turnover_rate"] = pd.to_numeric(df["换手率"], errors="coerce")

    # ---- 规模门槛 ----
    df["pass_size"] = df["float_mcap"] >= MIN_FLOAT_MCAP

    # ---- 类型门槛：只剔除名称硬伤 ----
    name_upper = df["name"].str.upper()
    excl_reason = pd.Series("", index=df.index, dtype=object)
    for kw in EXCLUDE_NAME_KEYWORDS:
        hit = name_upper.str.contains(kw.upper(), regex=False, na=False)
        excl_reason = excl_reason.where(~hit, f"名称含'{kw}'")
    df["pass_type"] = excl_reason == ""

    # ---- 申赎状态异常 → 保留但打标 ----
    needs = pd.Series(False, index=df.index)
    for col in ("purchase_status", "redeem_status"):
        if col in df.columns:
            s = df[col].astype(str).str.strip()
            needs = needs | s.isin(["暂停", "限制大额"])
    df["needs_review"] = needs
    df["exclude_reason"] = excl_reason

    # ---- 流动性（快照阶段用当日成交额代理）----
    df["avg_amount_20d"] = df["spot_amount"]
    df["liq_source"] = "spot"
    df["pass_liquidity"] = df["avg_amount_20d"] >= MIN_AVG_AMOUNT_20D
    df["n_days_history"] = 0
    df["pass_history"] = False
    df["last_price_date"] = ""

    # ---- 排名 + 抓取集合 + 候选池 ----
    df = df.sort_values("spot_amount", ascending=False, kind="mergesort").reset_index(drop=True)
    df["rank_by_spot_amount"] = range(1, len(df) + 1)

    eligible = df["pass_size"] & df["pass_type"]
    df["fetch_candidate"] = eligible & (df["rank_by_spot_amount"] <= fetch_top)

    df["in_universe"] = df["fetch_candidate"] & df["pass_liquidity"] & df["pass_size"] & df["pass_type"]

    # 快照阶段 in_universe 按 spot 成交额排序截断（finalize 会用真实 20 日均额重排）
    df["rank"] = 0
    sel = df["in_universe"]
    df.loc[sel, "rank"] = range(1, int(sel.sum()) + 1)
    df.loc[sel & (df["rank"] > universe_cap), "in_universe"] = False

    df["fetch_date"] = datetime.now().strftime("%Y-%m-%d")
    return df


UNIVERSE_COLS = [
    "code",
    "name",
    "exchange",
    "fund_type",
    "fund_type_coarse",
    "fund_type_bucket",
    "avg_amount_20d",
    "float_mcap",
    "last_price",
    "liq_source",
    "n_days_history",
    "pass_liquidity",
    "pass_size",
    "in_universe",
    "fetch_date",
    # ---- 附加诊断列（不影响上面的必需列）----
    "fund_type_src",
    "purchase_status",
    "redeem_status",
    "pass_type",
    "pass_history",
    "needs_review",
    "exclude_reason",
    "fetch_candidate",
    "rank",
    "rank_by_spot_amount",
    "spot_amount",
    "total_mcap",
    "turnover_rate",
    "last_price_date",
]


def stage_universe(out_dir: Path, fetch_top: int, universe_cap: int) -> pd.DataFrame:
    df = build_universe(out_dir, fetch_top, universe_cap)
    out = df[[c for c in UNIVERSE_COLS if c in df.columns]].copy()
    atomic_write_csv(out, out_dir / "etf_universe.csv")
    log(
        f"universe 落盘: {out_dir / 'etf_universe.csv'} "
        f"总行数={len(out)} fetch_candidate={int(out['fetch_candidate'].sum())} "
        f"in_universe={int(out['in_universe'].sum())}"
    )
    return out


# ---------------------------------------------------------------- 阶段 3：finalize
def stage_finalize(out_dir: Path, universe_cap: int) -> pd.DataFrame:
    """用已抓到的 etf_prices.csv 重算真实 20 日均成交额 / 上市天数，重排候选池。"""
    uni_path = out_dir / "etf_universe.csv"
    price_path = out_dir / "etf_prices.csv"
    if not uni_path.exists():
        raise SystemExit(f"[FATAL] 缺少 {uni_path}")
    if not price_path.exists():
        raise SystemExit(f"[FATAL] 缺少 {price_path}，请先跑 fetch_etf_prices.py")

    uni = pd.read_csv(uni_path, dtype={"code": str})
    uni["code"] = uni["code"].astype(str).str.strip()

    log("读取 etf_prices.csv 计算 20 日均成交额 / 上市天数 ...")
    px = pd.read_csv(price_path, dtype={"code": str}, usecols=["date", "code", "amount"])
    px["code"] = px["code"].astype(str).str.strip()
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["amount"] = pd.to_numeric(px["amount"], errors="coerce")
    px = px.sort_values(["code", "date"], kind="mergesort")

    stats: dict[str, dict] = {}
    for code, g in px.groupby("code", sort=False):
        g = g.dropna(subset=["date"])
        if g.empty:
            continue
        tail = g.tail(20)
        stats[code] = {
            "avg_amount_20d": float(tail["amount"].mean()),
            "n_days_history": int(len(g)),
            "last_price_date": g["date"].iloc[-1].strftime("%Y-%m-%d"),
        }

    uni["avg_amount_20d"] = pd.to_numeric(uni["avg_amount_20d"], errors="coerce")
    uni["float_mcap"] = pd.to_numeric(uni["float_mcap"], errors="coerce")

    # 逐行回填（直接赋值，避免与已有同名列 concat 后产生重名列）
    upd_avg: list[float] = []
    upd_src: list[str] = []
    upd_ndays: list[int] = []
    upd_hist: list[bool] = []
    upd_last: list[str] = []
    for _, row in uni.iterrows():
        st = stats.get(row["code"])
        if st is None:
            upd_avg.append(float(row["avg_amount_20d"]))
            upd_src.append("spot")
            upd_ndays.append(0)
            upd_hist.append(False)
            upd_last.append("")
        else:
            upd_avg.append(st["avg_amount_20d"])
            upd_src.append("prices")
            upd_ndays.append(st["n_days_history"])
            upd_hist.append(st["n_days_history"] >= MIN_HISTORY_DAYS)
            upd_last.append(st["last_price_date"])
    uni["avg_amount_20d"] = upd_avg
    uni["liq_source"] = upd_src
    uni["n_days_history"] = upd_ndays
    uni["pass_history"] = upd_hist
    uni["last_price_date"] = upd_last
    uni["pass_liquidity"] = uni["avg_amount_20d"] >= MIN_AVG_AMOUNT_20D
    uni["pass_size"] = uni["float_mcap"] >= MIN_FLOAT_MCAP
    if "pass_type" in uni.columns:
        uni["pass_type"] = uni["pass_type"].fillna(True).astype(bool)
    uni["pass_history"] = uni["pass_history"].fillna(False).astype(bool)
    uni["in_universe"] = (
        uni["pass_liquidity"] & uni["pass_size"] & uni["pass_type"] & uni["pass_history"]
    )

    uni = uni.sort_values("avg_amount_20d", ascending=False, kind="mergesort").reset_index(drop=True)
    uni["rank"] = 0
    sel = uni["in_universe"]
    uni.loc[sel, "rank"] = range(1, int(sel.sum()) + 1)
    uni.loc[sel & (uni["rank"] > universe_cap), "in_universe"] = False
    uni["fetch_date"] = datetime.now().strftime("%Y-%m-%d")

    out = uni[[c for c in UNIVERSE_COLS if c in uni.columns]].copy()
    atomic_write_csv(out, uni_path)
    log(
        f"finalize 落盘: {uni_path} 行数={len(out)} "
        f"liq_source=prices 的行数={int((out['liq_source'] == 'prices').sum())} "
        f"in_universe={int(out['in_universe'].sum())}"
    )
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 快照 + 候选池抓取")
    ap.add_argument(
        "--stage",
        default="all",
        choices=["spot", "universe", "finalize", "all"],
        help="spot=只拉快照；universe=建候选池；finalize=用行情重算；all=spot+universe",
    )
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="产物落盘目录")
    ap.add_argument("--fetch-top", type=int, default=FETCH_TOP, help="抓历史行情的临时集合上限")
    ap.add_argument("--universe-cap", type=int, default=UNIVERSE_CAP, help="最终候选池上限")
    ap.add_argument("--force", action="store_true", help="已存在的快照也重新拉")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"机器={socket.gethostname()} python={platform.python_version()} out_dir={out_dir}")

    # 刷新目标日 = 今天之前（含）的最近一个交易日（**绝不钉死某一天**）
    calendar_dates = load_trading_dates(out_dir / "trade_calendar.csv")
    target_compact = resolve_end_date(None, calendar_dates=calendar_dates)
    log(f"刷新目标日（最近交易日）={target_compact}")

    if args.stage in ("spot", "all"):
        stage_spot(out_dir, args.force, target_compact)
    if args.stage in ("universe", "all"):
        stage_universe(out_dir, args.fetch_top, args.universe_cap)
    if args.stage == "finalize":
        stage_finalize(out_dir, args.universe_cap)

    log("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
