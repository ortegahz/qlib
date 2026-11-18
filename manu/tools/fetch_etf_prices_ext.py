#!/usr/bin/env python3
"""扩展 ETF 行情面板（**独立脚本**：不依赖 ``manu/config.py``，服务器精简树也能跑）。

为什么要有它：
* ``manu/data/fetch_etf_prices.py`` 会 ``import config``（需要 ``manu/config.py``
  ⇒ 还要连带 ``manu/experiments/ashare_topk_selection/config.py``）；
  而服务器上的 ``manu/`` 是**精简树**（只有 AGENTS.md / data / docs / etf_rotation /
  evaluation / experiments / memory / report / skills / tools）⇒ 那个脚本在服务器上
  直接 ``ModuleNotFoundError: No module named 'config'``。
* 本脚本只依赖 ``akshare`` + ``pandas`` ⇒ 用 ``ak`` env 跑即可。

🔴 输出 schema 与封存面板**逐字一致**（``manu/data/fetch_etf_prices.py`` 同口径）::

    date,code,open,high,low,close,volume,amount

    - ``date``   ``YYYY-MM-DD``
    - ``code``   6 位代码（字符串，**不带交易所前缀**）
    - ``volume`` **手**（1 手 = 100 份）
    - ``amount`` 元

🔴 **复权口径**：默认 ``--adjust qfq``（前复权）⇒ **与封存面板
   ``manu/data/fetch_etf_prices.py`` 逐字同口径** ⇒ 两个面板可以直接拼接 /
   互相对账。**不许**用不复权（``--adjust ""``）去和封存面板混。

用法::

    /home/huangzhe/anaconda3/envs/ak/bin/python -u manu/tools/fetch_etf_prices_ext.py \
        --universe /path/etf_universe.csv --out /path/etf_prices.csv \
        --start 20180101 [--end 20261118] [--sleep-min 0.2] [--sleep-max 0.5]

落盘策略：每 ``--flush-every`` 只原子落一次盘（tmp + rename）⇒ 中途被杀也不留半截文件。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

BACKOFF = (1, 2, 4)
COLMAP = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)


def pick_codes(universe_path: Path, select: str) -> list[str]:
    df = pd.read_csv(universe_path, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    if select == "fetch_candidate" and "fetch_candidate" in df.columns:
        flag = df["fetch_candidate"].astype(str).str.lower().isin({"true", "1", "yes"})
        codes = df.loc[flag, "code"].tolist()
    elif select == "in_universe" and "in_universe" in df.columns:
        flag = df["in_universe"].astype(str).str.lower().isin({"true", "1", "yes"})
        codes = df.loc[flag, "code"].tolist()
    else:
        codes = df["code"].tolist()
    out, seen = [], set()
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_one(ak, code: str, start: str, end: str, retries: int,
              adjust: str = "qfq") -> pd.DataFrame | None:
    for attempt in range(retries + 1):
        try:
            hist = ak.fund_etf_hist_em(
                symbol=code, period="daily", start_date=start, end_date=end, adjust=adjust
            )
            if hist is None or len(hist) == 0:
                return pd.DataFrame()
            out = pd.DataFrame()
            for src, dst in COLMAP.items():
                out[dst] = (
                    pd.to_datetime(hist[src], errors="coerce").dt.strftime("%Y-%m-%d")
                    if dst == "date"
                    else pd.to_numeric(hist[src], errors="coerce")
                )
            out["code"] = code
            return out[["date", "code", "open", "high", "low", "close", "volume", "amount"]]
        except Exception as exc:  # noqa: BLE001 —— 网络抖动要重试
            if attempt >= retries:
                log(f"  ⚠️ {code} 抓取失败（重试 {retries} 次）：{type(exc).__name__}: {exc}")
                return None
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            time.sleep(wait)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 日线历史抓取（扩展面板·独立版）")
    ap.add_argument("--universe", required=True, help="候选池 CSV（etf_universe.csv）")
    ap.add_argument("--out", required=True, help="输出 etf_prices.csv")
    ap.add_argument("--manifest", default="", help="输出 manifest JSON（默认 <out>.manifest.json）")
    ap.add_argument("--select", default="fetch_candidate",
                    choices=["fetch_candidate", "in_universe", "all"])
    ap.add_argument("--start", default="20180101")
    ap.add_argument("--end", default="", help="留空 ⇒ 今天")
    ap.add_argument("--sleep-min", type=float, default=0.2)
    ap.add_argument("--sleep-max", type=float, default=0.5)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--flush-every", type=int, default=20)
    # 🔴 2026-11-18：**复权口径必须与封存面板一致**（``manu/data/fetch_etf_prices.py``
    #    用的是 ``adjust="qfq"`` = 前复权）。早先本脚本写死 ``adjust=""``（不复权）
    #    ⇒ 与封存面板**不是同一个价格尺度**：实测 296 只里 **84 只**的中位比值 ≠ 1
    #    （有 0.309 / 0.358 / 3.0 / 5.0 / 6.0 这种**分红/份额折算因子**）
    #    ⇒ 直接混用会让 MA / 动量 / 阈值判断全错 ⇒ 扩展面板必须同口径。
    ap.add_argument("--adjust", default="qfq",
                    choices=["qfq", "hfq", ""],
                    help="复权口径；**必须与封存面板一致**（默认 qfq=前复权，与 "
                         "manu/data/fetch_etf_prices.py 同口径）；空串 = 不复权")
    args = ap.parse_args()

    import akshare as ak  # 延迟导入（ak env）

    out_path = Path(args.out)
    manifest_path = Path(args.manifest) if args.manifest else out_path.with_suffix(
        out_path.suffix + ".manifest.json"
    )
    codes = pick_codes(Path(args.universe), args.select)
    end = args.end or datetime.now().strftime("%Y%m%d")
    log(f"机器={os.uname().nodename} python={sys.version.split()[0]} select={args.select} "
        f"待抓 {len(codes)} 只 区间={args.start}~{end} 复权={args.adjust or '不复权'}")

    frames: list[pd.DataFrame] = []
    ok_codes: list[str] = []
    failed_codes: list[str] = []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        df = fetch_one(ak, code, args.start, end, args.max_retries, adjust=args.adjust)
        if df is None:
            failed_codes.append(code)
        else:
            frames.append(df)
            if len(df):
                ok_codes.append(code)
        if i % args.flush_every == 0 or i == len(codes):
            atomic_write_csv(pd.concat(frames, ignore_index=True) if frames
                             else pd.DataFrame(columns=list(COLMAP.values()) + ["code"]), out_path)
            log(f"  进度 {i}/{len(codes)}｜已落盘 {sum(len(f) for f in frames)} 行｜"
                f"耗时 {time.time() - t0:.0f}s")
        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

    final = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(final):
        final = final.drop_duplicates(subset=["date", "code"], keep="last")
        final = final.sort_values(["code", "date"], kind="stable").reset_index(drop=True)
        final["code"] = final["code"].astype(str).str.zfill(6)
    atomic_write_csv(final, out_path)
    manifest = {
        "source": "akshare.fund_etf_hist_em",
        "adjust": args.adjust,
        "start": args.start,
        "end": end,
        "select": args.select,
        "n_codes_requested": len(codes),
        "n_codes_ok": len(ok_codes),
        "n_codes_failed": len(failed_codes),
        "failed_codes": failed_codes,
        "n_rows": int(len(final)),
        "n_codes_with_data": int(final["code"].nunique()) if len(final) else 0,
        "date_min": str(final["date"].min()) if len(final) else None,
        "date_max": str(final["date"].max()) if len(final) else None,
        "generated_at": datetime.now().isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ 完成：{manifest['n_rows']} 行 / {manifest['n_codes_with_data']} 只 "
        f"/ 区间 {manifest['date_min']}~{manifest['date_max']} ⇒ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
