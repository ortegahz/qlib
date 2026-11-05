#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 日线历史抓取脚本（阶段 2/3）—— 产出 ``etf_prices.csv`` + ``etf_prices_manifest.json``。

【数据源】
   ``ak.fund_etf_hist_em(symbol=<code>, period="daily", start_date=..., end_date=..., adjust="qfq")``
   —— 东方财富场内基金日线行情。

【复权方式】
   ``adjust="qfq"`` = **前复权**（以最新价为基准向前调整历史价）。
   ⇒ ``etf_prices.csv`` 里的 open/high/low/close 都是**前复权价**，
   与盘口「最新价」同尺度，可直接拼接；**不可**再与不复权序列混用。

【单位 / 口径】
   - ``date``    交易日（``YYYY-MM-DD``，A股交易日历，天然跳过周末与休市日）。
   - ``open/high/low/close``  前复权后的 **元/份**。
   - ``volume``  **手**（1 手 = 100 份）。实测：``Σ成交额 / (Σ成交量 × 100)`` ≈ 区间均价。
   - ``amount``  **元**（当日成交金额）。**不是**万元、不是亿元。
   - 区间：``--start`` 默认 **20200101**；``--end`` **留空 ⇒ 自动解析为「今天之前（含）的最近一个交易日」**
     （读 ``<out-dir>/trade_calendar.csv``；读不到 ⇒ 今天）⇒ **绝不钉死某一天**
     （钉死会让刷新在调仓节点当天变成**静默空转**：0 新行，而预检照样 `die`）。

【抓取纪律】
   - 每次请求之间 sleep ``--sleep-min`` ~ ``--sleep-max``（默认 0.2 ~ 0.5s），不打爆 eastmoney。
   - 每只 ETF 最多重试 ``--max-retries`` 次（默认 3 次），指数退避 1 / 2 / 4 秒；
     仍失败则记入 manifest 的 ``failed`` 清单，**不中断整批**。
   - ``--resume``：已落盘的 code 自动跳过（按已有 csv 的 code 去重）。
   - 原子写：先写 ``.tmp`` 再 ``os.replace``。

【抓取时间】2026-11-04

【执行机】服务器 ``huangzhe@192.168.99.40:32222``，
   解释器 ``/home/huangzhe/anaconda3/envs/ak/bin/python``（独立 ``ak`` env）

【可复现命令】（**必须用 ak env**；ql env 没装 akshare）
   python manu/etf_rotation/data/fetch_etf_prices.py --resume

【🔴 `--resume` 的口径（2026-11-05 05:3x 修）】
   旧口径是「**代码在文件里就跳过**」⇒ 到节点日再跑会**全部跳过** ⇒ **0 新行** ⇒ **静默空转**。
   现口径：只跳过「**该 code 的最新日已达目标结束日**」的 code（判定逻辑见 ``data/refresh_policy.py``），
   其余（最新日落后 / 文件里根本没有）**都要重抓**。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import random
import socket
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

DEFAULT_OUT_DIR = "/mnt/data/siping/datasets/etf"
DEFAULT_START = "20200101"
#: 结束日**留空** ⇒ 自动解析为「今天之前（含）的最近一个交易日」（见 refresh_policy.resolve_end_date）
DEFAULT_END = ""
BACKOFF = (1, 2, 4)  # 指数退避秒数

PRICE_COLS = ["date", "code", "open", "high", "low", "close", "volume", "amount"]

# 刷新口径统一放在 refresh_policy.py（纯函数；**ql env 也能测**，不用装 akshare）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_policy import (  # noqa: E402
    codes_needing_price_refresh,
    end_is_behind_today,
    load_trading_dates,
    resolve_end_date,
    resolve_start_date,
)


# ---------------------------------------------------------------- 基础工具
def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
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


def pick_codes(universe_path: Path, select: str) -> list[str]:
    """从 etf_universe.csv 取待抓 code 列表。"""
    if not universe_path.exists():
        raise SystemExit(f"[FATAL] 缺少候选池文件: {universe_path}")
    uni = pd.read_csv(universe_path, dtype={"code": str})
    uni["code"] = uni["code"].astype(str).str.strip()

    if select == "in_universe":
        mask = uni["in_universe"].astype(str).str.lower().isin(["true", "1"])
        codes = uni.loc[mask, "code"].tolist()
    elif select == "fetch":
        col = "fetch_candidate" if "fetch_candidate" in uni.columns else "in_universe"
        mask = uni[col].astype(str).str.lower().isin(["true", "1"])
        codes = uni.loc[mask, "code"].tolist()
    else:  # all
        codes = uni["code"].tolist()

    # 去重 + 保持候选池里的顺序
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def load_existing(prices_path: Path) -> pd.DataFrame:
    if not prices_path.exists():
        return pd.DataFrame(columns=PRICE_COLS)
    df = pd.read_csv(prices_path, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.strip()
    return df


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 日线历史抓取")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="产物落盘目录")
    ap.add_argument("--universe", default="", help="候选池 CSV（默认 <out-dir>/etf_universe.csv）")
    ap.add_argument(
        "--select",
        default="fetch",
        choices=["fetch", "in_universe", "all"],
        help="fetch=抓 fetch_candidate 集合（默认）；in_universe=只抓最终候选池；all=全部",
    )
    ap.add_argument("--start", default=DEFAULT_START, help="起始日 YYYYMMDD")
    ap.add_argument(
        "--end",
        default=DEFAULT_END,
        help="结束日 YYYYMMDD（留空 ⇒ 今天之前（含）的最近一个交易日；**不要钉死某一天**）",
    )
    ap.add_argument("--resume", action="store_true", help="跳过已落盘的 code")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只（0=不限，调试用）")
    ap.add_argument("--sleep-min", type=float, default=0.2, help="请求间隔下限（秒）")
    ap.add_argument("--sleep-max", type=float, default=0.5, help="请求间隔上限（秒）")
    ap.add_argument("--max-retries", type=int, default=3, help="每只 ETF 最大重试次数")
    ap.add_argument("--flush-every", type=int, default=25, help="每 N 只落一次盘（断点保护）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    universe_path = Path(args.universe) if args.universe else out_dir / "etf_universe.csv"
    prices_path = out_dir / "etf_prices.csv"
    manifest_path = out_dir / "etf_prices_manifest.json"

    codes = pick_codes(universe_path, args.select)
    calendar_dates = load_trading_dates(out_dir / "trade_calendar.csv")
    start_date = resolve_start_date(args.start)
    end_date = resolve_end_date(args.end, calendar_dates=calendar_dates)
    log(
        f"机器={socket.gethostname()} python={platform.python_version()} "
        f"select={args.select} 待抓 {len(codes)} 只 区间={start_date}~{end_date}"
        f"（交易日历 {len(calendar_dates)} 天）"
    )
    if end_is_behind_today(end_date):
        log(f"[WARN] 结束日 {end_date} 早于今天 ⇒ 本轮是**补历史**，不会追到最新交易日")

    existing = load_existing(prices_path)
    done_codes: set[str] = set()
    # 载入上一轮 manifest：--resume 跳过时把上一轮的明细带过来，
    # 否则一次「全跳过」的续跑会把 manifest 冲成空（丢掉审计线索）。
    prev_per_code: dict[str, dict] = {}
    prev_failed: list[dict] = []
    if args.resume and manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                prev_m = json.load(fh)
            prev_per_code = prev_m.get("per_code", {}) or {}
            prev_failed = prev_m.get("failed", []) or []
            log(f"--resume: 载入上一轮 manifest 明细 {len(prev_per_code)} 条")
        except Exception as exc:  # noqa: BLE001
            log(f"[WARN] 上一轮 manifest 读取失败，将重建: {type(exc).__name__}: {exc}")
            prev_per_code, prev_failed = {}, []

    if args.resume and not existing.empty:
        # 🔴 口径（2026-11-05 05:3x 修）：**按「最新日是否已达目标结束日」判定**，不是按「文件里有没有这个 code」
        need_codes, done_list = codes_needing_price_refresh(existing, codes, end_date)
        done_codes = set(done_list)
        log(f"--resume: {len(done_codes)} 只最新日已达 {end_date} ⇒ 跳过；{len(need_codes)} 只还要抓")
        if not need_codes:
            log("[INFO] 全部 code 的最新日都已到达目标结束日 ⇒ 本轮**无需刷新**（不是刷新失败）")
        existing_max = existing["date"].max() if "date" in existing.columns else ""
        log(f"--resume: 现有文件最新日={existing_max}（目标结束日={end_date}）")
    elif not existing.empty:
        log(f"未指定 --resume：会重抓并覆盖这 {len(existing['code'].unique())} 只已有数据")

    if args.limit > 0:
        codes = codes[: args.limit]

    per_code: dict[str, dict] = {}
    failed: list[dict] = []
    frames: list[pd.DataFrame] = []
    n_ok = 0
    t_start = time.time()

    carried_ok = 0
    carried_failed = 0
    for idx, code in enumerate(codes, start=1):
        if code in done_codes:
            # 续跑：沿用上一轮该 code 的结果，不重复请求
            prev = prev_per_code.get(code)
            if prev is not None:
                per_code[code] = prev
                if prev.get("status") == "ok":
                    carried_ok += 1
                else:
                    carried_failed += 1
                    failed.append(
                        {
                            "code": code,
                            "error": prev.get("error", ""),
                            "elapsed_s": prev.get("elapsed_s", 0.0),
                        }
                    )
            continue

        t0 = time.time()
        hist = None
        last_err = ""
        for attempt in range(args.max_retries):
            try:
                hist = fetch_akshare(
                    "fund_etf_hist_em",
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < args.max_retries - 1:
                    wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    log(f"  [RETRY {attempt + 1}] {code} {last_err[:120]} → 退避 {wait}s")
                    time.sleep(wait)

        elapsed = time.time() - t0
        if hist is None or len(hist) == 0:
            per_code[code] = {
                "status": "failed",
                "rows": 0,
                "first_date": None,
                "last_date": None,
                "elapsed_s": round(elapsed, 3),
                "error": last_err,
            }
            failed.append({"code": code, "error": last_err, "elapsed_s": round(elapsed, 3)})
            log(f"[{idx}/{len(codes)}] {code} 失败 ({elapsed:.2f}s): {last_err[:120]}")
        else:
            d = pd.DataFrame(
                {
                    "date": pd.to_datetime(hist["日期"], errors="coerce").dt.strftime("%Y-%m-%d"),
                    "code": code,
                    "open": pd.to_numeric(hist["开盘"], errors="coerce"),
                    "high": pd.to_numeric(hist["最高"], errors="coerce"),
                    "low": pd.to_numeric(hist["最低"], errors="coerce"),
                    "close": pd.to_numeric(hist["收盘"], errors="coerce"),
                    "volume": pd.to_numeric(hist["成交量"], errors="coerce"),
                    "amount": pd.to_numeric(hist["成交额"], errors="coerce"),
                }
            )
            d = d.dropna(subset=["date"])
            frames.append(d)
            n_ok += 1
            per_code[code] = {
                "status": "ok",
                "rows": int(len(d)),
                "first_date": d["date"].iloc[0] if len(d) else None,
                "last_date": d["date"].iloc[-1] if len(d) else None,
                "elapsed_s": round(elapsed, 3),
                "error": "",
            }
            log(
                f"[{idx}/{len(codes)}] {code} OK rows={len(d)} "
                f"{d['date'].iloc[0]}~{d['date'].iloc[-1]} ({elapsed:.2f}s)"
            )

        # 限速
        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

        # 周期性落盘（断点保护）
        if args.flush_every > 0 and idx % args.flush_every == 0:
            _flush(frames, existing, prices_path, code)

    _flush(frames, existing, prices_path, "final")

    total_elapsed = time.time() - t_start
    n_requested = len([c for c in codes if c not in done_codes])
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "machine": socket.gethostname(),
        "python": platform.python_version(),
        "source": "akshare.fund_etf_hist_em",
        "adjust": "qfq",
        "start_date": start_date,
        "end_date": end_date,
        "out_csv": str(prices_path),
        "select": args.select,
        "n_codes_total": len(codes),
        "n_codes_requested": n_requested,
        "n_codes_skipped_by_resume": len(codes) - n_requested,
        "n_ok": n_ok + carried_ok,
        "n_failed": len(failed),
        "n_carried_over_from_previous_manifest": carried_ok + carried_failed,
        "total_elapsed_s": round(total_elapsed, 2),
        "avg_elapsed_s_per_code": round(total_elapsed / n_requested, 3) if n_requested else 0.0,
        "per_code": per_code,
        "failed": failed,
    }
    atomic_write_json(manifest, manifest_path)
    log(
        f"完成: ok={manifest['n_ok']} (本轮 {n_ok} + 沿用 {carried_ok}) "
        f"failed={len(failed)} 总耗时={total_elapsed:.1f}s "
        f"平均每只={manifest['avg_elapsed_s_per_code']:.2f}s"
    )
    log(f"manifest → {manifest_path}")
    return 0


def _flush(frames: list, existing: pd.DataFrame, prices_path: Path, tag: str) -> None:
    """把本轮新增的行情与已有数据合并后原子落盘。"""
    if frames:
        new = pd.concat(frames, ignore_index=True)
    else:
        new = pd.DataFrame(columns=PRICE_COLS)
    if existing is not None and not existing.empty:
        new_codes = set(new["code"].unique()) if not new.empty else set()
        keep = existing[~existing["code"].isin(new_codes)]
        out = pd.concat([keep, new], ignore_index=True)
    else:
        out = new
    if out.empty:
        return
    out = out[PRICE_COLS]
    out = out.sort_values(["code", "date"], kind="mergesort").reset_index(drop=True)
    atomic_write_csv(out, prices_path)
    log(f"  [flush @ {tag}] 落盘 {prices_path} 行数={len(out)}")


if __name__ == "__main__":
    sys.exit(main())
