#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ETF 成分股权重抓取脚本（阶段 3/3）—— 产出 ``etf_holdings.csv`` + ``etf_holdings_manifest.json``。

【数据源（主 → 备）】
   1) ``ak.fund_etf_holdings_tx(symbol=<code>)`` —— 腾讯行情的 ETF **实际成分股 + 市值占比**（首选）。
      列：``证券代码`` / ``证券简称`` / ``市场代码`` / ``证券数量`` / ``最新价`` / ``市值`` / ``市值占比``。
      ⇒ ``source=tx``；``weight_pct`` 直接取 ``市值占比``（腾讯已归一到 0~100 的百分数）。
   2) ``ak.fund_etf_pcf(symbol=<code>)`` —— 基金公司每日申购赎回清单（**备源 / 交叉校验**）。
      列含 ``交易日期`` / ``证券代码`` / ``证券简称`` / ``证券数量`` / ``申赎单位净值`` / ``申购替代金额``。
      ⇒ ``source=pcf``；PCF **没有**「最新价」列，权重按下式还原：
          单价 = ``申购替代金额`` / ``证券数量``
          ``weight_pct`` = ``证券数量`` × 单价 / ``申赎单位净值`` × 100
      （即用申购替代金额对申赎单位净值归一；因篮子含少量现金，权重和一般落在 95~98 而非 100。）

【字段口径】
   - ``stock_code``    6 位证券代码 + ``.SH`` / ``.SZ`` / ``.BJ`` 后缀。
     **后缀优先取接口的 ``市场代码``**（101→SH, 102→SZ, 103→BJ, 9999→境外/商品，无后缀），
     接口缺失时才回落到代码前缀规则（6→SH、4/8→BJ、其余→SZ）。
   - ``weight_pct``    占 ETF 净值的百分比，**数值本身就是 0~100 的百分数**（如 3.9464，不是 0.039464）。
   - ``holding_type``  成分性质：``stock``（A 股股票）/ ``bond``（债券）/ ``other``（商品、境外标的等）。
   - 货币 ETF / 纯现金 ETF 的 PCF 只有「申赎现金」占位行 ⇒ 剔除后无成分，**如实记为失败**。

【抓取纪律】
   - 每次请求之间 sleep ``--sleep-min`` ~ ``--sleep-max``（默认 0.2 ~ 0.5s）。
   - 每只 ETF 最多重试 ``--max-retries`` 次（默认 3 次），指数退避 1 / 2 / 4 秒；
     主源与备源各自重试，仍失败则记入 manifest 的 ``failed`` 清单，**不中断整批**。
   - ``--resume``：已落盘的 code 自动跳过（按已有 csv 的 code 去重）。
   - 原子写：先写 ``.tmp`` 再 ``os.replace``。

【抓取时间】2026-11-04

【执行机】服务器 ``huangzhe@192.168.99.40:32222``，
   解释器 ``/home/huangzhe/anaconda3/envs/ak/bin/python``（独立 ``ak`` env）

【可复现命令】
   python manu/etf_rotation/data/fetch_etf_holdings.py --resume
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
BACKOFF = (1, 2, 4)

HOLDING_COLS = [
    "etf_code",
    "etf_name",
    "stock_code",
    "stock_name",
    "weight_pct",
    "source",
    "fetch_date",
    # ---- 附加诊断列 ----
    "stock_exchange",
    "holding_type",
    "security_qty",
    "price_used",
    "mkt_value",
    "weight_pct_recalc",
]

# 市场代码 → 交易所
MKT_CODE_EXCHANGE = {"101": "SH", "102": "SZ", "103": "BJ", "9999": ""}
# PCF 里的现金占位行（不是真成分）
CASH_PLACEHOLDER_CODES = {"SSXJ", "159900"}
CASH_PLACEHOLDER_KEYWORD = "申赎现金"

# 抓取日期（模块导入时固定，全批一致）
TODAY = datetime.now().strftime("%Y-%m-%d")

# 刷新口径统一放在 refresh_policy.py（纯函数；**ql env 也能测**）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_policy import codes_needing_holdings_refresh  # noqa: E402


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


def exchange_for(stock_code: str, mkt_code: str) -> str:
    """后缀优先取 市场代码，其次回落代码前缀规则。"""
    mkt = str(mkt_code).strip()
    if mkt in MKT_CODE_EXCHANGE and MKT_CODE_EXCHANGE[mkt]:
        return MKT_CODE_EXCHANGE[mkt]
    code = str(stock_code).strip()
    if not code:
        return ""
    if not code[:1].isdigit():
        return ""  # 境外 / 商品标的（Au9999 等），无 A 股后缀
    head = code[0]
    if head in ("5", "6", "9"):
        return "SH"
    if head in ("4", "8"):
        return "BJ"
    if head in ("1", "2", "3"):
        return "SZ"
    return "SZ"


def holding_type_for(stock_code: str, mkt_code: str) -> str:
    code = str(stock_code).strip()
    if not code[:1].isdigit():
        return "other"
    if str(mkt_code).strip() == "9999":
        return "other"
    if code.startswith(("60", "68", "00", "30", "43", "83", "87", "92")):
        return "stock"
    return "bond"


def suffixed(stock_code: str, mkt_code: str) -> str:
    code = str(stock_code).strip()
    exch = exchange_for(code, mkt_code)
    return f"{code}.{exch}" if exch else code


def is_cash_placeholder(stock_code: str, stock_name: str) -> bool:
    code = str(stock_code).strip()
    name = str(stock_name).strip()
    return code in CASH_PLACEHOLDER_CODES or CASH_PLACEHOLDER_KEYWORD in name


# ---------------------------------------------------------------- 单只抓取
def fetch_one_with_retry(code: str, fn_name: str, max_retries: int):
    """带指数退避地调一次 akshare 接口。返回 (df, error_str)。"""
    last_err = ""
    for attempt in range(max_retries):
        try:
            return fetch_akshare(fn_name, symbol=code), ""
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries - 1:
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                log(f"  [RETRY {attempt + 1}] {code} {fn_name} {last_err[:110]} → 退避 {wait}s")
                time.sleep(wait)
    return None, last_err


def parse_tx(hist: pd.DataFrame, etf_code: str, etf_name: str) -> tuple[pd.DataFrame, dict]:
    """腾讯成分股 → 统一 schema。返回 (df, 统计)。"""
    stats = {"n_raw": 0, "n_placeholder": 0, "n_no_value": 0}
    rows = []
    for _, r in hist.iterrows():
        stats["n_raw"] += 1
        code = str(r.get("证券代码", "")).strip()
        name = str(r.get("证券简称", "")).strip()
        if not code:
            stats["n_no_value"] += 1
            continue
        if is_cash_placeholder(code, name):
            stats["n_placeholder"] += 1
            continue  # 货币 / 纯现金 ETF 的占位行，不是真成分
        mkt = str(r.get("市场代码", "")).strip()
        qty = pd.to_numeric(r.get("证券数量"), errors="coerce")
        px = pd.to_numeric(r.get("最新价"), errors="coerce")
        mv = pd.to_numeric(r.get("市值"), errors="coerce")
        w = pd.to_numeric(r.get("市值占比"), errors="coerce")
        if not (pd.notna(w) and w > 0):
            stats["n_no_value"] += 1
            continue
        rows.append(
            {
                "etf_code": etf_code,
                "etf_name": etf_name,
                "stock_code": suffixed(code, mkt),
                "stock_name": name,
                "weight_pct": w,
                "source": "tx",
                "stock_exchange": exchange_for(code, mkt),
                "holding_type": holding_type_for(code, mkt),
                "security_qty": qty,
                "price_used": px,
                "mkt_value": mv,
                "fetch_date": TODAY,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out, stats
    recalc = out["security_qty"] * out["price_used"]
    tot = recalc.sum()
    out["weight_pct_recalc"] = recalc / tot * 100 if tot > 0 else float("nan")
    return out, stats


def parse_pcf(hist: pd.DataFrame, etf_code: str, etf_name: str) -> tuple[pd.DataFrame, dict]:
    """申赎清单 → 统一 schema（权重 = 数量×单价 / 申赎单位净值）。返回 (df, 统计)。"""
    stats = {"n_raw": 0, "n_placeholder": 0, "n_no_value": 0}
    rows = []
    nav = pd.to_numeric(hist.get("申赎单位净值"), errors="coerce")
    nav = float(nav.iloc[0]) if len(nav) else float("nan")
    for _, r in hist.iterrows():
        code = str(r.get("证券代码", "")).strip()
        name = str(r.get("证券简称", "")).strip()
        stats["n_raw"] += 1
        if not code:
            stats["n_no_value"] += 1
            continue
        if is_cash_placeholder(code, name):
            stats["n_placeholder"] += 1
            continue
        mkt = str(r.get("市场代码", "")).strip()
        qty = pd.to_numeric(r.get("证券数量"), errors="coerce")
        amt = pd.to_numeric(r.get("申购替代金额"), errors="coerce")
        price = (amt / qty) if (qty and qty > 0 and pd.notna(amt)) else float("nan")
        value = qty * price if (pd.notna(qty) and pd.notna(price)) else float("nan")
        w = value / nav * 100 if (pd.notna(value) and pd.notna(nav) and nav > 0) else float("nan")
        if not (pd.notna(w) and w > 0):
            # 境外 / 商品标的：基金公司不披露申购替代金额 ⇒ 无法还原权重
            stats["n_no_value"] += 1
            continue
        rows.append(
            {
                "etf_code": etf_code,
                "etf_name": etf_name,
                "stock_code": suffixed(code, mkt),
                "stock_name": name,
                "weight_pct": w,
                "source": "pcf",
                "stock_exchange": exchange_for(code, mkt),
                "holding_type": holding_type_for(code, mkt),
                "security_qty": qty,
                "price_used": price,
                "mkt_value": value,
                "fetch_date": TODAY,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out, stats
    tot = out["mkt_value"].sum()
    out["weight_pct_recalc"] = out["mkt_value"] / tot * 100 if tot > 0 else float("nan")
    return out, stats


def fetch_one(etf_code: str, etf_name: str, max_retries: int):
    """主源 tx → 备源 pcf。返回 (df, info)。"""
    t0 = time.time()
    info = {
        "n_holdings": 0,
        "weight_sum": None,
        "weight_sum_recalc": None,
        "source": None,
        "status": "failed",
        "error": "",
        "elapsed_s": 0.0,
        "tx_error": "",
        "pcf_error": "",
    }
    saw_placeholder_only = False
    saw_rows_no_value = False
    detail: list[str] = []

    hist, err = fetch_one_with_retry(etf_code, "fund_etf_holdings_tx", max_retries)
    info["tx_error"] = err[:400]
    if hist is not None and len(hist) > 0:
        try:
            df, st = parse_tx(hist, etf_code, etf_name)
        except Exception as exc:  # noqa: BLE001
            df, st = pd.DataFrame(), {"n_raw": 0, "n_placeholder": 0, "n_no_value": 0}
            info["tx_error"] = f"parse: {type(exc).__name__}: {exc}"[:400]
        detail.append(
            f"tx: raw={st['n_raw']} placeholder={st['n_placeholder']} no_value={st['n_no_value']}"
        )
        if st["n_placeholder"] and st["n_placeholder"] == st["n_raw"]:
            saw_placeholder_only = True
        elif st["n_no_value"]:
            saw_rows_no_value = True
        if not df.empty:
            info["n_holdings"] = int(len(df))
            info["weight_sum"] = round(float(df["weight_pct"].sum()), 4)
            info["weight_sum_recalc"] = round(float(df["weight_pct_recalc"].sum()), 4)
            info["source"] = "tx"
            info["status"] = "ok"
            info["elapsed_s"] = round(time.time() - t0, 3)
            return df, info
        saw_placeholder_only = True

    # ---- 回落 PCF ----
    t1 = time.time()
    hist, err = fetch_one_with_retry(etf_code, "fund_etf_pcf", max_retries)
    info["pcf_error"] = err[:400]
    if hist is not None and len(hist) > 0:
        try:
            df, st = parse_pcf(hist, etf_code, etf_name)
        except Exception as exc:  # noqa: BLE001
            df, st = pd.DataFrame(), {"n_raw": 0, "n_placeholder": 0, "n_no_value": 0}
            info["pcf_error"] = f"parse: {type(exc).__name__}: {exc}"[:400]
        detail.append(
            f"pcf: raw={st['n_raw']} placeholder={st['n_placeholder']} no_value={st['n_no_value']}"
        )
        if st["n_placeholder"] and st["n_placeholder"] == st["n_raw"]:
            saw_placeholder_only = True
        elif st["n_no_value"]:
            saw_rows_no_value = True
        if not df.empty:
            info["n_holdings"] = int(len(df))
            info["weight_sum"] = round(float(df["weight_pct"].sum()), 4)
            info["weight_sum_recalc"] = round(float(df["weight_pct_recalc"].sum()), 4)
            info["source"] = "pcf"
            info["status"] = "ok"
            info["elapsed_s"] = round(time.time() - t0, 3)
            return df, info
        saw_placeholder_only = True
        info["pcf_error"] = (info["pcf_error"] + " | 无有效成分（仅现金占位行）").strip(" |")

    if saw_placeholder_only and not saw_rows_no_value:
        info["status"] = "no_holdings"
        info["error"] = (
            "两个源都只返回「申赎现金」占位行 ⇒ 该 ETF 无 A 股成分"
            "（货币 ETF 常态；部分商品 ETF 的 PCF 同样不披露实物篮子）"
        )
    elif saw_rows_no_value:
        info["status"] = "failed"
        info["error"] = (
            "两源都返回了成分行，但都没有可用估值"
            "（PCF 申购替代金额=0：境外/商品标的；tx 无市值占比）⇒ 无法还原权重。 "
            + " | ".join(detail)
        )[:600]
    else:
        info["error"] = f"tx: {info['tx_error']} | pcf: {info['pcf_error']}"[:600]
    info["elapsed_s"] = round(time.time() - t0, 3)
    return pd.DataFrame(), info


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 成分股权重抓取")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="产物落盘目录")
    ap.add_argument("--universe", default="", help="候选池 CSV（默认 <out-dir>/etf_universe.csv）")
    ap.add_argument(
        "--select",
        default="in_universe",
        choices=["in_universe", "fetch", "all"],
        help="in_universe=只抓最终候选池（默认）；fetch=抓 fetch_candidate；all=全部",
    )
    ap.add_argument("--resume", action="store_true", help="跳过已落盘的 code")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只（0=不限，调试用）")
    ap.add_argument("--sleep-min", type=float, default=0.2, help="请求间隔下限（秒）")
    ap.add_argument("--sleep-max", type=float, default=0.5, help="请求间隔上限（秒）")
    ap.add_argument("--max-retries", type=int, default=3, help="每只 ETF 每源最大重试次数")
    ap.add_argument("--flush-every", type=int, default=25, help="每 N 只落一次盘（断点保护）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    universe_path = Path(args.universe) if args.universe else out_dir / "etf_universe.csv"
    hold_path = out_dir / "etf_holdings.csv"
    manifest_path = out_dir / "etf_holdings_manifest.json"

    if not universe_path.exists():
        raise SystemExit(f"[FATAL] 缺少候选池文件: {universe_path}")
    uni = pd.read_csv(universe_path, dtype={"code": str})
    uni["code"] = uni["code"].astype(str).str.strip()
    name_map = dict(zip(uni["code"], uni.get("name", pd.Series(dtype=str)).astype(str)))

    if args.select == "in_universe":
        mask = uni["in_universe"].astype(str).str.lower().isin(["true", "1"])
    elif args.select == "fetch":
        col = "fetch_candidate" if "fetch_candidate" in uni.columns else "in_universe"
        mask = uni[col].astype(str).str.lower().isin(["true", "1"])
    else:
        mask = pd.Series(True, index=uni.index)
    targets = uni.loc[mask, "code"].tolist()
    seen: set[str] = set()
    targets = [c for c in targets if c and not (c in seen or seen.add(c))]

    log(
        f"机器={socket.gethostname()} python={platform.python_version()} "
        f"select={args.select} 待抓 {len(targets)} 只"
    )

    today_compact = TODAY.replace("-", "")
    done_codes: set[str] = set()
    prev_per_code: dict[str, dict] = {}
    prev_failed: list[dict] = []
    prev_no_holdings: list[dict] = []
    if args.resume and manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                prev_m = json.load(fh)
            prev_per_code = prev_m.get("per_code", {}) or {}
            prev_failed = prev_m.get("failed", []) or []
            prev_no_holdings = prev_m.get("no_holdings", []) or []
            log(f"--resume: 载入上一轮 manifest 明细 {len(prev_per_code)} 条")
        except Exception as exc:  # noqa: BLE001
            log(f"[WARN] 上一轮 manifest 读取失败，将重建: {type(exc).__name__}: {exc}")
            prev_per_code, prev_failed, prev_no_holdings = {}, [], []

    existing = pd.DataFrame(columns=HOLDING_COLS)
    if hold_path.exists():
        existing = pd.read_csv(hold_path, dtype={"etf_code": str, "stock_code": str})
        if not args.resume:
            log(f"未指定 --resume：会重抓并覆盖已有 {existing['etf_code'].nunique()} 只")
            existing = pd.DataFrame(columns=HOLDING_COLS)
        else:
            # 🔴 口径（2026-11-05 05:3x 修）：**按「该 code 的成分快照 fetch_date 是否已是今天」判定**，
            #    不是按「文件里有没有这个 code」⇒ 否则 W 快照永远停在旧日期，滞后天数只涨不消。
            need_codes, done_list = codes_needing_holdings_refresh(existing, targets, today_compact)
            done_codes = set(done_list)
            log(
                f"--resume: {len(done_codes)} 只快照已是 {today_compact} ⇒ 跳过；"
                f"{len(need_codes)} 只还要抓"
            )

    if args.limit > 0:
        targets = targets[: args.limit]

    per_code: dict[str, dict] = {}
    failed: list[dict] = []
    no_holdings: list[dict] = []
    frames: list[pd.DataFrame] = []
    n_ok = 0
    t_start = time.time()

    carried_ok = 0
    carried_failed = 0
    carried_no_holdings = 0
    for idx, code in enumerate(targets, start=1):
        if code in done_codes:
            # 续跑：沿用上一轮该 code 的结果，不重复请求
            prev = prev_per_code.get(code)
            if prev is not None:
                per_code[code] = prev
                if prev.get("status") == "ok":
                    carried_ok += 1
                elif prev.get("status") == "no_holdings":
                    carried_no_holdings += 1
                    no_holdings.append(
                        {"code": code, "name": name_map.get(code, ""), "error": prev.get("error", "")}
                    )
                else:
                    carried_failed += 1
                    failed.append(
                        {
                            "code": code,
                            "name": name_map.get(code, ""),
                            "error": prev.get("error", ""),
                        }
                    )
            continue
        etf_name = name_map.get(code, "")
        df, info = fetch_one(code, etf_name, args.max_retries)
        per_code[code] = info
        if info["status"] == "ok":
            frames.append(df)
            n_ok += 1
            log(
                f"[{idx}/{len(targets)}] {code} {etf_name} OK source={info['source']} "
                f"n={info['n_holdings']} wsum={info['weight_sum']} ({info['elapsed_s']}s)"
            )
        elif info["status"] == "no_holdings":
            no_holdings.append({"code": code, "name": etf_name, "error": info["error"]})
            log(
                f"[{idx}/{len(targets)}] {code} {etf_name} 无股票成分 "
                f"({info['elapsed_s']}s): {info['error'][:120]}"
            )
        else:
            failed.append({"code": code, "name": etf_name, "error": info["error"]})
            log(
                f"[{idx}/{len(targets)}] {code} {etf_name} 失败 ({info['elapsed_s']}s): "
                f"{info['error'][:140]}"
            )

        time.sleep(random.uniform(args.sleep_min, args.sleep_max))

        if args.flush_every > 0 and idx % args.flush_every == 0:
            _flush(frames, existing, hold_path, code)

    _flush(frames, existing, hold_path, "final")

    total_elapsed = time.time() - t_start
    n_requested = len([c for c in targets if c not in done_codes])
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "machine": socket.gethostname(),
        "python": platform.python_version(),
        "source_primary": "akshare.fund_etf_holdings_tx",
        "source_fallback": "akshare.fund_etf_pcf",
        "out_csv": str(hold_path),
        "select": args.select,
        "n_codes_total": len(targets),
        "n_codes_requested": n_requested,
        "n_codes_skipped_by_resume": len(targets) - n_requested,
        "n_ok": n_ok + carried_ok,
        "n_failed": len(failed),
        "n_no_holdings": len(no_holdings),
        "n_carried_over_from_previous_manifest": carried_ok + carried_failed + carried_no_holdings,
        "total_elapsed_s": round(total_elapsed, 2),
        "avg_elapsed_s_per_code": round(total_elapsed / n_requested, 3) if n_requested else 0.0,
        "per_code": per_code,
        "failed": failed,
        "no_holdings": no_holdings,
    }
    atomic_write_json(manifest, manifest_path)
    log(
        f"完成: ok={manifest['n_ok']} (本轮 {n_ok} + 沿用 {carried_ok}) "
        f"no_holdings={len(no_holdings)} failed={len(failed)} "
        f"总耗时={total_elapsed:.1f}s 平均每只={manifest['avg_elapsed_s_per_code']:.2f}s"
    )
    log(f"manifest → {manifest_path}")
    return 0


def _flush(frames: list, existing: pd.DataFrame, hold_path: Path, tag: str) -> None:
    if frames:
        new = pd.concat(frames, ignore_index=True)
    else:
        new = pd.DataFrame(columns=HOLDING_COLS)
    if existing is not None and not existing.empty:
        new_codes = set(new["etf_code"].unique()) if not new.empty else set()
        keep = existing[~existing["etf_code"].astype(str).isin(new_codes)]
        out = pd.concat([keep, new], ignore_index=True)
    else:
        out = new
    if out.empty:
        return
    out = out[HOLDING_COLS]
    out = out.sort_values(["etf_code", "weight_pct"], ascending=[True, False], kind="mergesort")
    out = out.reset_index(drop=True)
    atomic_write_csv(out, hold_path)
    log(f"  [flush @ {tag}] 落盘 {hold_path} 行数={len(out)}")


if __name__ == "__main__":
    sys.exit(main())
