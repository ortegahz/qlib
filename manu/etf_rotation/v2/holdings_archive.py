"""🔴 V2 成分股 / 权重快照的 **append-only 按日归档**（对齐②）。

【为什么必须有这个模块】
akshare 的 ``fund_etf_holdings_tx``（腾讯实际持仓）与 ``fund_etf_pcf``
（每日申购赎回清单）**都不提供历史回溯** ⇒ **历史成分变更不可观测**。
V1 的 ``data/fetch_etf_holdings.py`` 把 ``fetch_date`` 写死成「今天」且**每次覆盖**
同一张 ``etf_holdings.csv`` ⇒ 本地只剩 **1 个**快照日（2026-11-04）。

⇒ 本模块给出**唯一诚实**的前进路径：**从今天开始按日 append 归档**
（历史从今天起一天天积累），并给每份快照留 ``meta.json`` 审计痕迹。
🔴 **此前（最早归档日之前）没有成分快照这件事不许被沉默掩盖**：口径披露在
``v2/constituents.py::lookahead_bias_bound`` / ``caliber_note``（**本模块不改那两处**），
本模块只负责「从今天起把真实快照攒起来」+「缺历史时**报错**而不是**倒推**」。

【append-only 的三条硬规矩】
  1. **同一 asof 只许写一次** ⇒ 重复 append 直接抛错（绝不静默覆盖 / 追加重复行）；
  2. **原子写** ⇒ 先写 ``.tmp`` 再 ``os.replace`` ⇒ 中途失败**不留半截文件**；
  3. **point-in-time 只许向后看** ⇒ ``snapshot_for_date`` 取 **≤ date** 的最近归档日；
     早于最早归档日 ⇒ 抛错（「历史成分快照**不可得** ⇒ **禁止**用未来快照倒推」）。

【落点】归档根 ``/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/holdings_archive/``
（在允许落点 ``/mnt/data/siping/datasets/manu/`` 之内）；
每份快照 = ``<root>/YYYY-MM-DD.csv`` + 伴随 ``<root>/meta/YYYY-MM-DD.json``。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd

#: 🔴 归档根（**必须在允许落点内**：`/mnt/data/siping/datasets/manu/...`）
ARCHIVE_ROOT = Path("/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/holdings_archive")

#: 快照 CSV 的列（**顺序固定**；与 ``etf_holdings.csv`` 的必需列同名）
SNAPSHOT_COLUMNS = ("fetch_date", "etf_code", "stock_code", "weight_pct", "source")

#: 写入前**必须**齐的列（``fetch_date`` 由归档日 ``asof`` 决定 ⇒ 不在输入里）
REQUIRED_INPUT_COLUMNS = ("etf_code", "stock_code", "weight_pct", "source")

#: 伴随元数据目录名（相对归档根）
META_DIRNAME = "meta"

#: 归档脚本版本（进 ``meta.json`` ⇒ 审计时能知道这条快照是谁写的）
ARCHIVE_VERSION = "holdings_archive/1.0"


# ----------------------------------------------------------------- 异常
class HoldingsArchiveError(RuntimeError):
    """归档层通用错误。"""


class SnapshotExistsError(HoldingsArchiveError):
    """同一 ``asof`` 的快照已存在 ⇒ append-only 拒绝再写。"""


class SnapshotUnavailableError(HoldingsArchiveError):
    """目标日期**早于**最早归档日 ⇒ 历史成分快照不可得（禁止倒推）。"""


class AppendResult(NamedTuple):
    """``append_snapshot`` 的返回值（可解包成 ``(行数, 文件路径)``）。"""

    rows_written: int
    path: Path
    asof: str


# ----------------------------------------------------------------- 小工具
def _normalize_date(value: Any) -> str:
    """把入参规整成 ``YYYY-MM-DD``（不是这个格式 ⇒ 直接报错）。"""
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"🔴 日期必须是 YYYY-MM-DD：{value!r}") from exc


def _is_iso_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _normalize_rows(rows: Any, asof: str) -> pd.DataFrame:
    """把入参（DataFrame / 字典序列）归一成 5 列快照表。

    * ``fetch_date`` **一律**写成归档日 ``asof``（快照日 = 文件名 = 归档日）；
    * 额外列（如 ``etf_name``）一律丢弃（归档只留 5 列，口径不漂移）。
    """
    if isinstance(rows, pd.DataFrame):
        df = rows.copy()
    elif isinstance(rows, Iterable) and not isinstance(rows, (str, bytes)):
        df = pd.DataFrame(list(rows))
    else:
        raise TypeError(f"🔴 rows 必须是 DataFrame 或字典序列，收到 {type(rows).__name__}")
    missing = [col for col in REQUIRED_INPUT_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"🔴 成分行缺列 {missing}（现有列：{list(df.columns)}）")
    df["fetch_date"] = asof
    df = df.loc[:, list(SNAPSHOT_COLUMNS)]
    df["etf_code"] = df["etf_code"].astype(str).str.strip()
    df["stock_code"] = df["stock_code"].astype(str).str.strip()
    df["source"] = df["source"].astype(str).str.strip()
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce")
    if df.empty:
        raise ValueError("🔴 空快照不许落盘（空表会让下游把整池 ETF 误判成「无成分」）")
    return df.reset_index(drop=True)


def _build_meta(df: pd.DataFrame, asof: str, extra: Mapping[str, Any] | None) -> dict:
    """伴随元数据（审计用）：这条快照是什么时候抓的、有多少行、哪些来源。"""
    meta: dict[str, Any] = {
        "fetch_date": asof,
        "n_rows": int(len(df)),
        "n_etfs": int(df["etf_code"].nunique()),
        "n_stocks": int(df["stock_code"].nunique()),
        "sources": {str(src): int(n) for src, n in df["source"].value_counts().items()},
        "columns": list(SNAPSHOT_COLUMNS),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "archive_version": ARCHIVE_VERSION,
    }
    if extra:
        meta.update({str(k): v for k, v in extra.items()})
    return meta


# ----------------------------------------------------------------- 写
def append_snapshot(
    rows: Any,
    asof: str,
    root: Path | str = ARCHIVE_ROOT,
    meta_extra: Mapping[str, Any] | None = None,
) -> AppendResult:
    """append-only 落一份快照 ⇒ 返回 ``(行数, 文件路径)``。

    * 落点 ``<root>/<asof>.csv``（UTF-8，列 = ``fetch_date,etf_code,stock_code,weight_pct,source``）；
    * 伴随 ``<root>/meta/<asof>.json``（行数 / ETF 数 / 来源 / 生成时间 / 脚本版本）；
    * 🔴 **同一 asof 已存在 ⇒ 直接抛错**（绝不覆盖、绝不追加重复行）；
    * 🔴 **原子写**：先写 ``.tmp`` 再 ``os.replace`` ⇒ 中途失败**不留半截文件**。
    """
    day = _normalize_date(asof)
    root_dir = Path(root)
    path = root_dir / f"{day}.csv"
    if path.exists():  # 🔴 append-only：同一 asof 只许写一次
        raise SnapshotExistsError(
            f"🔴 归档日 {day} 已存在（{path}）⇒ append-only 拒绝覆盖 / 重复写入"
        )
    df = _normalize_rows(rows, day)
    meta = _build_meta(df, day, meta_extra)
    meta_path = root_dir / META_DIRNAME / f"{day}.json"
    csv_tmp = path.with_name(path.name + ".tmp")
    meta_tmp = meta_path.with_name(meta_path.name + ".tmp")
    try:
        root_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv_tmp, index=False, encoding="utf-8")
        meta_tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except BaseException:
        csv_tmp.unlink(missing_ok=True)
        meta_tmp.unlink(missing_ok=True)
        raise
    os.replace(csv_tmp, path)
    try:
        os.replace(meta_tmp, meta_path)
    except BaseException:
        meta_tmp.unlink(missing_ok=True)
        raise
    return AppendResult(rows_written=int(len(df)), path=path, asof=day)


# ----------------------------------------------------------------- 读
def available_dates(root: Path | str = ARCHIVE_ROOT) -> list[str]:
    """列出全部归档日（**升序**）。

    只认文件名形如 ``YYYY-MM-DD.csv`` 的快照；其它文件（临时文件 / 手工杂物）一律忽略。
    """
    root_dir = Path(root)
    if not root_dir.exists():
        return []
    days = [p.stem for p in root_dir.glob("*.csv") if _is_iso_date(p.stem)]
    return sorted(days)


def snapshot_for_date(date: str, root: Path | str = ARCHIVE_ROOT) -> str:
    """point-in-time 解析 ⇒ 返回 **≤ date** 的**最近一个**归档日（ISO 字符串）。

    🔴 ``date`` 早于**最早**归档日 ⇒ 抛 :class:`SnapshotUnavailableError`
    （历史成分快照**不可得** ⇒ **禁止**用未来快照倒推）。
    """
    target = _normalize_date(date)
    days = available_dates(root)
    if not days:
        raise SnapshotUnavailableError(
            f"🔴 成分股归档里**没有任何快照**（根：{Path(root)}）⇒ 截至 {target} 的"
            f"历史成分快照**不可得** ⇒ **禁止**用未来快照倒推"
        )
    candidates = [d for d in days if d <= target]  # 🔴 point-in-time：只许取 ≤ date
    if not candidates:
        raise SnapshotUnavailableError(
            f"🔴 {target} 早于最早归档日 {days[0]} ⇒ 该日的历史成分快照**不可得**"
            f"（现有归档日：{days}）⇒ **禁止**用未来快照倒推"
        )
    return candidates[-1]


def snapshot_path(date: str, root: Path | str = ARCHIVE_ROOT) -> Path:
    """point-in-time 快照文件路径（可直接喂 ``constituents.load_holdings(path)``）。"""
    day = snapshot_for_date(date, root)
    return Path(root) / f"{day}.csv"


def load_point_in_time(date: str, root: Path | str = ARCHIVE_ROOT) -> tuple[str, pd.DataFrame]:
    """point-in-time 读取 ⇒ ``(快照日, 成分表)``。

    列与 ``v2/constituents.py::load_holdings`` 要求的 5 列**同名同序**
    ⇒ 归档一旦积累起来，历史成分股就能按「当日可得」口径读出来。
    """
    day = snapshot_for_date(date, root)
    path = Path(root) / f"{day}.csv"
    try:
        df = pd.read_csv(
            path,
            dtype={"etf_code": str, "stock_code": str, "source": str, "fetch_date": str},
            # 🔴 ``float_precision="round_trip"``：pandas 默认的 CSV 浮点解析**不是**
            #    round-trip 的（实测同一段文本默认解析与原文差 1 ULP）⇒ 用它保证
            #    「写出去的权重」与「读回来的权重」**逐位相同**。
            float_precision="round_trip",
        )
    except FileNotFoundError as exc:
        raise SnapshotUnavailableError(f"🔴 快照文件不见了：{path}") from exc
    missing = [col for col in SNAPSHOT_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"🔴 快照 {path} 缺列 {missing}")
    return day, df.loc[:, list(SNAPSHOT_COLUMNS)]


def snapshot_meta(date: str, root: Path | str = ARCHIVE_ROOT) -> dict:
    """读某个归档日的 ``meta.json``（审计用；**精确**日期，不做 point-in-time 解析）。"""
    day = _normalize_date(date)
    path = Path(root) / META_DIRNAME / f"{day}.json"
    if not path.exists():
        raise SnapshotUnavailableError(f"🔴 快照 {day} 没有伴随 meta.json（{path}）")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------- 缺口披露
def history_gap_note(root: Path | str = ARCHIVE_ROOT) -> str:
    """历史缺口披露文案（进报告 / 日志用）。

    🔴 **如实说**：akshare 两个成分接口都没有历史 PCF ⇒ **归档开始之前**的成分股
    永远**不可得**；归档从**最早那天的当天**起才有真实快照。
    """
    days = available_dates(root)
    if not days:
        return (
            "🔴 成分股归档**尚无任何快照** ⇒ 历史成分快照**不可得**"
            "（akshare `fund_etf_holdings_tx` / `fund_etf_pcf` 均无历史 PCF）"
            "⇒ **禁止**用未来快照倒推历史。"
        )
    return (
        f"🔴 成分股历史缺口：归档**最早日 = {days[0]}**（append-only，从该日起逐日积累）；"
        f"{days[0]} **之前**的成分股/权重**不可得**"
        "（akshare `fund_etf_holdings_tx` / `fund_etf_pcf` 均无历史 PCF）"
        f"⇒ **禁止**用未来快照倒推历史。现有归档日：{days}"
    )
