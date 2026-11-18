#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 抓 ETF 成分股 / 权重 → **append-only 按日归档**（V2，对齐②）。

【它解决什么】
V1 的 ``data/fetch_etf_holdings.py`` 把 ``fetch_date`` 写死成「今天」且**每次覆盖**
同一张 ``etf_holdings.csv`` ⇒ 本地只剩 **1 个**快照日（2026-11-04），
akshare 的 ``fund_etf_holdings_tx`` / ``fund_etf_pcf`` 又**没有历史 PCF**
⇒ **历史成分变更不可观测**。
本脚本是**唯一诚实**的前进路径：**从今天起按日 append 归档**（历史从今天起积累）。

【口径与 V1 一致】
主源 ``ak.fund_etf_holdings_tx``（腾讯实际持仓，``weight_pct`` = ``市值占比``），
备源 ``ak.fund_etf_pcf``（申购赎回清单，权重 = 数量 × 单价 / 申赎单位净值 × 100）；
代码后缀按 ``市场代码``（101→SH / 102→SZ / 103→BJ / 9999→无后缀），
「申赎现金」占位行剔除。**V1 文件只读、不改**（口径在 V2 侧复刻，V2 不 import V1）。

【用法（服务器，**ak env**：akshare 只装在那个 env）】
    /home/huangzhe/anaconda3/envs/ak/bin/python \
        manu/etf_rotation/v2/fetch_holdings_archive.py --asof 2026-11-17

【失败纪律】
* **超时即退出**（默认总预算 60 秒）⇒ **不落盘**（半份快照会把没抓到的 ETF
  静默判成「无成分」⇒ 覆盖率 0 ⇒ 被悄悄剔出池子 ⇒ 比报错更坏）；
* 抓取失败（限流 / 网络）⇒ 明确报错 + **不落盘**；
* 同一 asof 已归档 ⇒ 明确报错（append-only，**不覆盖**）。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import holdings_archive as HA  # noqa: E402

try:  # 扁平导入（v2 目录进 sys.path，与其它 v2 模块同口径）
    import config_v2 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v2 as C


#: 候选池（**只读**）：与 ``daily/fetch_data.py::_pool_codes`` 同口径
UNIVERSE_CSV = Path(C.HOLDINGS_CSV).parent / "etf_universe.csv"

#: 🔴 本次抓取的**总预算**（秒）—— 抓不完就超时退出，**绝不落半份快照**
DEFAULT_TIMEOUT_S = 60.0
#: 单只 ETF 的抓取预算（秒）—— 一只不许把总预算吃光
PER_ETF_TIMEOUT_S = 60.0
#: 请求间隔（秒，带抖动）—— 对接口友好，限流时少踩
SLEEP_MIN_S, SLEEP_MAX_S = 0.2, 0.5
#: 每只 ETF 每个源的最大尝试次数
MAX_RETRIES = 3
#: 重试退避（秒）
BACKOFF = (1, 2, 4)

#: 市场代码 → 交易所（V1 ``MKT_CODE_EXCHANGE`` 同口径）
MKT_CODE_EXCHANGE = {"101": "SH", "102": "SZ", "103": "BJ", "9999": ""}
#: PCF 里的现金占位行（不是真成分；V1 ``CASH_PLACEHOLDER_CODES`` 同口径）
CASH_PLACEHOLDER_CODES = {"SSXJ", "159900"}
CASH_PLACEHOLDER_KEYWORD = "申赎现金"

#: 抓取脚本版本（进 ``meta.json`` ⇒ 审计时能知道是哪版脚本抓的）
SCRIPT_VERSION = "fetch_holdings_archive/1.0"

#: 归档表列（与 ``holdings_archive.SNAPSHOT_COLUMNS`` 同口径）
_COLS = ["etf_code", "stock_code", "weight_pct", "source"]


class FetchTimeout(RuntimeError):
    """抓取超出时间预算（⇒ 本次**不落盘**）。"""


def _log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------- 代码归一（V1 同口径）
def _exchange_for(stock_code: str, mkt_code: str) -> str:
    """后缀优先取「市场代码」，其次回落代码前缀规则（V1 ``exchange_for`` 同口径）。"""
    mkt = str(mkt_code).strip()
    if mkt in MKT_CODE_EXCHANGE and MKT_CODE_EXCHANGE[mkt]:
        return MKT_CODE_EXCHANGE[mkt]
    code = str(stock_code).strip()
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


def _suffixed(stock_code: str, mkt_code: str) -> str:
    """成分股代码 + 交易所后缀（境外 / 商品标的无后缀）。"""
    code = str(stock_code).strip()
    exch = _exchange_for(code, mkt_code)
    return f"{code}.{exch}" if exch else code


def _is_cash_placeholder(stock_code: str, stock_name: str) -> bool:
    """「申赎现金」占位行不是真成分（货币 ETF 常态）。"""
    code = str(stock_code).strip()
    name = str(stock_name).strip()
    return code in CASH_PLACEHOLDER_CODES or CASH_PLACEHOLDER_KEYWORD in name


# ----------------------------------------------------------------- 解析（V1 同口径）
def parse_tx(raw: pd.DataFrame, etf_code: str) -> pd.DataFrame:
    """腾讯实际持仓 → 归档行（``weight_pct`` = ``市值占比``，腾讯已归一到 0~100 百分数）。"""
    rows: list[dict] = []
    for _, r in raw.iterrows():
        code = str(r.get("证券代码", "")).strip()
        name = str(r.get("证券简称", "")).strip()
        if not code or _is_cash_placeholder(code, name):
            continue
        weight = pd.to_numeric(r.get("市值占比"), errors="coerce")
        if not (pd.notna(weight) and weight > 0):
            continue
        rows.append(
            {
                "etf_code": etf_code,
                "stock_code": _suffixed(code, str(r.get("市场代码", "")).strip()),
                "weight_pct": float(weight),
                "source": "tx",
            }
        )
    return pd.DataFrame(rows, columns=_COLS)


def parse_pcf(raw: pd.DataFrame, etf_code: str) -> pd.DataFrame:
    """申购赎回清单 → 归档行（权重 = 数量 × 单价 / 申赎单位净值 × 100）。"""
    nav_series = raw.get("申赎单位净值")
    nav = float("nan")
    if nav_series is not None:
        nav_values = pd.to_numeric(nav_series, errors="coerce")
        nav = float(nav_values.iloc[0]) if len(nav_values) else float("nan")
    rows: list[dict] = []
    for _, r in raw.iterrows():
        code = str(r.get("证券代码", "")).strip()
        name = str(r.get("证券简称", "")).strip()
        if not code or _is_cash_placeholder(code, name):
            continue
        qty = pd.to_numeric(r.get("证券数量"), errors="coerce")
        amt = pd.to_numeric(r.get("申购替代金额"), errors="coerce")
        price = amt / qty if (pd.notna(qty) and pd.notna(amt) and qty > 0) else float("nan")
        value = qty * price if (pd.notna(qty) and pd.notna(price)) else float("nan")
        weight = value / nav * 100 if (pd.notna(value) and pd.notna(nav) and nav > 0) else float("nan")
        if not (pd.notna(weight) and weight > 0):
            continue
        rows.append(
            {
                "etf_code": etf_code,
                "stock_code": _suffixed(code, str(r.get("市场代码", "")).strip()),
                "weight_pct": float(weight),
                "source": "pcf",
            }
        )
    return pd.DataFrame(rows, columns=_COLS)


# ----------------------------------------------------------------- 抓取
def _ak_call(fn_name: str, etf_code: str) -> pd.DataFrame:
    """调 akshare（**延迟 import** ⇒ 没装 akshare 的环境也能 import 本模块做单测）。"""
    try:
        import akshare as ak
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于本机 env
        raise RuntimeError(
            "🔴 本解释器没有 akshare ⇒ 请用 ak env 跑："
            "/home/huangzhe/anaconda3/envs/ak/bin/python "
            "manu/etf_rotation/v2/fetch_holdings_archive.py --asof YYYY-MM-DD"
        ) from exc
    return getattr(ak, fn_name)(symbol=etf_code)


def _fetch_with_retry(etf_code: str, fn_name: str, deadline: float) -> tuple[pd.DataFrame | None, str]:
    """带指数退避地调一次接口（受时间预算约束）⇒ ``(原始表, 错误信息)``。"""
    last_err = ""
    for attempt in range(MAX_RETRIES):
        if time.monotonic() >= deadline:
            raise FetchTimeout(f"🔴 抓 {etf_code} 超时（预算用尽）")
        try:
            return _ak_call(fn_name, etf_code), ""
        except Exception as exc:  # noqa: BLE001 - 限流 / 网络抖动都要兜住
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(wait, left))
    return None, last_err


def fetch_one(etf_code: str, deadline: float) -> tuple[pd.DataFrame, dict]:
    """抓一只 ETF 的成分（主源 tx → 备源 pcf）⇒ ``(成分行, 状态)``。"""
    info: dict[str, Any] = {
        "etf_code": etf_code, "status": "failed", "source": "", "n_rows": 0, "error": "",
    }
    raw, tx_err = _fetch_with_retry(etf_code, "fund_etf_holdings_tx", deadline)
    tx_rows = pd.DataFrame(columns=_COLS)
    if raw is not None and len(raw) > 0:
        tx_rows = parse_tx(raw, etf_code)
        if not tx_rows.empty:
            info.update({"status": "ok", "source": "tx", "n_rows": int(len(tx_rows))})
            return tx_rows, info
    raw_pcf, pcf_err = _fetch_with_retry(etf_code, "fund_etf_pcf", deadline)
    pcf_rows = pd.DataFrame(columns=_COLS)
    if raw_pcf is not None and len(raw_pcf) > 0:
        pcf_rows = parse_pcf(raw_pcf, etf_code)
        if not pcf_rows.empty:
            info.update({"status": "ok", "source": "pcf", "n_rows": int(len(pcf_rows))})
            return pcf_rows, info
    info["error"] = f"tx: {tx_err} | pcf: {pcf_err}"[:500]
    if tx_rows.empty and pcf_rows.empty and not tx_err and not pcf_err:
        # 两个源都只回了「申赎现金」占位行 ⇒ 该 ETF 无 A 股成分（货币 ETF 常态）
        info["status"] = "no_holdings"
        info["error"] = "两源都只返回「申赎现金」占位行 ⇒ 该 ETF 无 A 股成分"
    return pd.DataFrame(columns=_COLS), info


def fetch_rows(codes: Sequence[str], deadline: float) -> tuple[pd.DataFrame, dict]:
    """按候选池逐只抓 ⇒ ``(成分表, 报告)``。

    🔴 **超时 ⇒ 抛 :class:`FetchTimeout`**（⇒ 调用方**不落盘**）：
    只抓到一半的快照会把没抓到的 ETF 静默判成「无成分」⇒ 比报错更坏。
    """
    frames: list[pd.DataFrame] = []
    failed: list[dict] = []
    no_holdings: list[str] = []
    t0 = time.monotonic()
    total = len(codes)
    for idx, code in enumerate(codes):
        if time.monotonic() >= deadline:
            raise FetchTimeout(f"🔴 抓取超时：预算用尽，只抓到 {idx}/{total} 只 ⇒ **不落盘**")
        etf_deadline = min(deadline, time.monotonic() + PER_ETF_TIMEOUT_S)
        df, info = fetch_one(code, etf_deadline)
        if info["status"] == "ok":
            frames.append(df)
        elif info["status"] == "no_holdings":
            no_holdings.append(code)
        else:
            failed.append({"etf_code": code, "error": info["error"]})
        left = deadline - time.monotonic()
        if idx < total - 1 and left > 0:
            time.sleep(min(random.uniform(SLEEP_MIN_S, SLEEP_MAX_S), left))
    rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=_COLS)
    report = {
        "n_codes": total,
        "n_ok": len(frames),
        "n_no_holdings": len(no_holdings),
        "n_failed": len(failed),
        "no_holdings": no_holdings,
        "failed": failed,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    return rows, report


def _pool_codes(universe_csv: Path | None = None) -> list[str]:
    """候选池代码（**只读** ``etf_universe.csv``；口径同 ``daily/fetch_data.py``）。"""
    path = Path(universe_csv) if universe_csv else UNIVERSE_CSV
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype={"code": str})
    if "code" not in df.columns:
        return []
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    for flag in ("fetch_candidate", "in_universe"):
        if flag in df.columns:
            mask = df[flag].astype(str).str.lower().isin(("true", "1", "yes"))
            sub = df.loc[mask, "code"].tolist()
            if sub:
                return sorted(set(sub))
    return sorted(set(df["code"].tolist()))


# ----------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="抓 ETF 成分股/权重 → append-only 按日归档（V2，对齐②）",
        epilog=(
            "示例：\n"
            "  /home/huangzhe/anaconda3/envs/ak/bin/python \\\n"
            "      manu/etf_rotation/v2/fetch_holdings_archive.py --asof 2026-11-17\n"
            "\n"
            "🔴 akshare 只装在 **ak env**；默认总预算 60 秒，超时即退出且**不落盘**。\n"
            "退出码：0=成功 / 1=抓取失败 / 2=参数错 / 3=超时 / 4=该归档日已存在。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--asof", required=True, help="归档日 YYYY-MM-DD（= 快照的 fetch_date）")
    ap.add_argument("--root", default=str(HA.ARCHIVE_ROOT), help="归档根目录（默认在允许落点内）")
    ap.add_argument("--dry-run", action="store_true", help="抓取并打印将要写入的统计量，**不落盘**")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="本次抓取总预算（秒，默认 60）")
    args = ap.parse_args(argv)

    day = str(args.asof).strip()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        print(f"🔴 --asof 必须是 YYYY-MM-DD：{args.asof!r}", file=sys.stderr)
        return 2
    today = datetime.now().strftime("%Y-%m-%d")
    if day > today:
        print(
            f"🔴 --asof {day} 在未来（今天 {today}）⇒ 未来快照会让 point-in-time 读取"
            f"**看见还没发生的事** ⇒ 拒绝",
            file=sys.stderr,
        )
        return 2

    codes = _pool_codes()
    if not codes:
        print(f"🔴 候选池为空（{UNIVERSE_CSV}）⇒ 无从抓取", file=sys.stderr)
        return 2

    budget = max(1.0, float(args.timeout))
    # 🔴 实测单只 ETF 抓取 ≈ 4.3s（V1 baseline manifest 也记着 3.36s/只）⇒
    #    预算明显不够时**先喊一声**（否则白跑一轮、超时退出、今天的快照就废了）。
    need = 3.0 * len(codes)
    if budget < need:
        print(
            f"⚠️ 预算 {budget:.0f}s 明显小于候选池 {len(codes)} 只的最低需求"
            f"（≥{need:.0f}s；实测约 4.3s/只）⇒ 大概率超时退出（超时**不落盘**）",
            file=sys.stderr,
        )
    deadline = time.monotonic() + budget
    _log(f"开始抓成分股：asof={day} 候选池={len(codes)} 只 预算={budget:.0f}s")
    try:
        rows, report = fetch_rows(codes, deadline)
    except FetchTimeout as exc:
        print(f"{exc}", file=sys.stderr)
        print("🔴 本次**不落盘**（半份快照会把没抓到的 ETF 静默判成「无成分」）", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - 网络 / 限流 / 解析都要优雅失败
        print(f"🔴 抓取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("🔴 本次**不落盘**", file=sys.stderr)
        return 1

    if rows.empty:
        print("🔴 一条成分行都没抓到 ⇒ 不落盘（空快照会让下游把整池 ETF 判成「无成分」）", file=sys.stderr)
        return 1

    sources = {str(s): int(n) for s, n in rows["source"].value_counts().items()}
    print(
        f"🔴 asof={day}：抓取成功 {report['n_ok']} 只 / 无成分 {report['n_no_holdings']} 只 / "
        f"失败 {report['n_failed']} 只；成分行 {len(rows)} 行，来源 {sources}，"
        f"耗时 {report['elapsed_s']}s"
    )
    if report["failed"]:
        print(f"🔴 失败的 ETF（**不入本快照**，与 V1 baseline 同口径）：{report['failed'][:20]}", file=sys.stderr)
    if args.dry_run:
        print("[dry-run] 不落盘")
        return 0

    meta_extra = {
        "n_codes_requested": report["n_codes"],
        "n_etfs_no_holdings": report["n_no_holdings"],
        "etfs_failed": [f["etf_code"] for f in report["failed"]],
        "failed_detail": report["failed"],
        "fetch_elapsed_s": report["elapsed_s"],
        "universe_csv": str(UNIVERSE_CSV),
        "fetch_script_version": SCRIPT_VERSION,
    }
    try:
        result = HA.append_snapshot(rows, day, root=args.root, meta_extra=meta_extra)
    except HA.SnapshotExistsError as exc:
        print(f"{exc}", file=sys.stderr)
        return 4
    print(f"🔴 已归档：{result.path}（{result.rows_written} 行）")
    print(HA.history_gap_note(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
