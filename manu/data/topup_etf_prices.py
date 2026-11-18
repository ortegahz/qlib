#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 日线**定点补抓**工具 —— 只补指定的 code，不重建数据集。

【为什么需要它（2026-11-04 夜实测）】
`fetch_etf_prices.py` 按 `fetch_candidate`（成交额前 296 只）整批抓，
但**用户真实持仓未必在成交额前 296 里**：
实测 `159929`（医药ETF汇添富，真实持仓 2500 股）在 `etf_universe.csv` 里
（`fetch_candidate=False`），在 `etf_prices.csv` 里 **0 行**
⇒ analyze.py 只能判「无行情数据 ⇒ 不可评估 ⇒ 退出」，
把**数据缺口**误当成了**信号**，理由经不起追问。

本脚本：
- 只请求给定 code 列表（默认 = 账户快照里的全部持仓 code）；
- **追加**进 `etf_prices.csv`（原子写：先 `.tmp` 再 `os.replace`）；
- **合并**进 `etf_prices_manifest.json` 的 `per_code`（不冲掉上一轮的 296 条审计线索）；
- 单只失败**不中断整批**，记进 manifest 的 `failed`。

【可复现命令】（必须用 ak env，ql env 没装 akshare）
    /home/huangzhe/anaconda3/envs/ak/bin/python \
        manu/etf_rotation/data/topup_etf_prices.py --codes 159929

【口径】区间：``--start`` 默认 **20200101**；``--end`` **留空 ⇒ 自动解析为「今天之前（含）的最近一个交易日」**
   （🔴 2026-11-05 05:3x 修：旧口径把结束日**硬编码成 20261104** ⇒ 换个日子跑也抓不到新日期 ⇒ 静默空转）；
   adjust="qfq"（前复权），与既有文件同尺度；同 (date, code) **去重 keep=last** ⇒ 不会产生重复行。
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

#: 研究链根（`data/` 不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as C  # noqa: E402

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402

#: ETF 数据集目录（落点 `<落点根>/datasets/etf`；与每日链 `daily_config.ETF_DATA_ROOT` 同源）
DEFAULT_OUT_DIR = str(C.ETF_DATA_ROOT)
DEFAULT_START = "20200101"
#: 结束日**留空** ⇒ 自动解析为「今天之前（含）的最近一个交易日」（见 refresh_policy.resolve_end_date）
DEFAULT_END = ""
PRICE_COLS = ["date", "code", "open", "high", "low", "close", "volume", "amount"]

# 刷新口径统一放在 refresh_policy.py（纯函数；**ql env 也能测**）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_policy import (  # noqa: E402
    codes_needing_price_refresh,
    end_is_behind_today,
    load_trading_dates,
    resolve_end_date,
    resolve_start_date,
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch_akshare(fn: str, *args, **kwargs):
    """调 akshare 接口；屏蔽 tqdm 噪声（异常照常抛出）。"""
    import akshare as ak  # 延迟导入：本机无 akshare 时也能 py_compile 通过

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        return getattr(ak, fn)(*args, **kwargs)


def holding_codes() -> list[str]:
    """从账户快照取全部持仓 code（默认补抓目标）。"""
    snap = Path(__file__).resolve().parent.parent / "data" / "account_snapshot.csv"
    if not snap.exists():
        return []
    df = pd.read_csv(snap, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    return sorted(set(df["code"]))


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


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 日线定点补抓")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--codes", default="", help="逗号分隔的 code；留空 = 账户快照里的全部持仓")
    ap.add_argument("--start", default=DEFAULT_START, help="起始日 YYYYMMDD")
    ap.add_argument(
        "--end",
        default=DEFAULT_END,
        help="结束日 YYYYMMDD（留空 ⇒ 今天之前（含）的最近一个交易日）",
    )
    ap.add_argument("--max-retries", type=int, default=3)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    prices_path = out_dir / "etf_prices.csv"
    manifest_path = out_dir / "etf_prices_manifest.json"
    calendar_dates = load_trading_dates(out_dir / "trade_calendar.csv")
    start_date = resolve_start_date(args.start)
    end_date = resolve_end_date(args.end, calendar_dates=calendar_dates)

    if args.codes.strip():
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        codes = holding_codes()
    if not codes:
        raise SystemExit("[FATAL] 没有待补抓的 code")

    log(
        f"机器={socket.gethostname()} python={platform.python_version()} "
        f"待补抓 {len(codes)} 只：{','.join(codes)}｜区间={start_date}~{end_date}"
    )
    if end_is_behind_today(end_date):
        log(f"[WARN] 结束日 {end_date} 早于今天 ⇒ 本轮是**补历史**，不会追到最新交易日")

    existing = pd.DataFrame(columns=PRICE_COLS)
    if prices_path.exists():
        existing = pd.read_csv(prices_path, dtype={"code": str})
        existing["code"] = existing["code"].astype(str).str.strip().str.zfill(6)
    # 🔴 口径（2026-11-05 05:3x 修）：**按「最新日是否已达目标结束日」判定**，不是按「文件里有没有这个 code」
    #    （旧口径 ⇒ 159929 已在文件里 ⇒ 每次都跳过 ⇒ 到节点日**永远补不到新数据** ⇒ 静默空转）
    todo, skipped = codes_needing_price_refresh(existing, codes, end_date)
    if skipped:
        log(f"已有数据、跳过：{','.join(skipped)}")
    if not todo:
        log("无新增需求，退出")
        return 0

    frames: list[pd.DataFrame] = []
    per_code_new: dict[str, dict] = {}
    failed: list[dict] = []
    for idx, code in enumerate(todo, start=1):
        hist = None
        last_err = ""
        t0 = time.time()
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
                    wait = 2 ** attempt
                    log(f"  [RETRY {attempt + 1}] {code} {last_err[:120]} → 退避 {wait}s")
                    time.sleep(wait)
        elapsed = time.time() - t0
        if hist is None or len(hist) == 0:
            per_code_new[code] = {
                "status": "failed",
                "rows": 0,
                "first_date": None,
                "last_date": None,
                "elapsed_s": round(elapsed, 3),
                "error": last_err,
            }
            failed.append({"code": code, "error": last_err, "elapsed_s": round(elapsed, 3)})
            log(f"[{idx}/{len(todo)}] {code} 失败 ({elapsed:.2f}s): {last_err[:150]}")
            continue
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
        ).dropna(subset=["date"])
        frames.append(d)
        per_code_new[code] = {
            "status": "ok",
            "rows": int(len(d)),
            "first_date": d["date"].iloc[0] if len(d) else None,
            "last_date": d["date"].iloc[-1] if len(d) else None,
            "elapsed_s": round(elapsed, 3),
            "error": "",
        }
        log(
            f"[{idx}/{len(todo)}] {code} OK rows={len(d)} "
            f"{d['date'].iloc[0]}~{d['date'].iloc[-1]} ({elapsed:.2f}s)"
        )

    if frames:
        merged = pd.concat([existing] + frames, ignore_index=True)
        merged["code"] = merged["code"].astype(str).str.zfill(6)
        merged = merged.drop_duplicates(subset=["date", "code"], keep="last")
        merged = merged.sort_values(["code", "date"]).reset_index(drop=True)
        atomic_write_csv(merged[PRICE_COLS], prices_path)
        log(f"已追加 {len(frames)} 只 ⇒ {prices_path}（总 {len(merged)} 行 / {merged['code'].nunique()} 只）")

    manifest = {}
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            log(f"[WARN] manifest 读取失败，将重建：{type(exc).__name__}: {exc}")
            manifest = {}
    per_code = manifest.get("per_code", {}) or {}
    per_code.update(per_code_new)
    manifest["per_code"] = per_code
    manifest.setdefault("generated_at", datetime.now().isoformat(timespec="seconds"))
    manifest["topup_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["topup_machine"] = socket.gethostname()
    manifest["topup_codes"] = todo
    manifest["n_codes_total"] = len(per_code)
    manifest["n_ok"] = sum(1 for v in per_code.values() if v.get("status") == "ok")
    manifest["failed"] = list(manifest.get("failed", []) or []) + failed
    atomic_write_json(manifest, manifest_path)
    log(f"已合并 manifest：per_code={len(per_code)} 条，failed={len(manifest['failed'])} 条")

    return 1 if failed and not frames else 0


if __name__ == "__main__":
    sys.exit(main())
