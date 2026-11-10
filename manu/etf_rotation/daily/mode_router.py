"""mode_router.py —— 每日产物的**路径**工具（不再有「模式」概念）。

🔴 2026-11-09（需求 1：删除 Mode C、精简到**单一**流水线）：
   旧版这里叫「双轨模式的判定」，对外暴露 ``route()`` ⇒ 返回模式 ``"A"`` / ``"B"``。
   **模式分诊已整体下线**（唯一入口是 ``main.py``，只有一条流水线；
   「观察态 / 实盘态」由 ``--live-trade`` 开关表达，**不是**模式）。
   ⇒ 本模块现在**只负责路径**：``normalize_date`` / ``build_run_dir`` / ``log_path`` /
   ``draft_path``；**不再有任何 mode 概念**，也不再有 ``route()``。

【职责边界】
* ``normalize_date``：**纯判定**，**不创建任何目录**；
* ``build_run_dir`` / ``log_path`` / ``draft_path``：**负责建目录**
  （``parents=True, exist_ok=True``）。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402


def normalize_date(value: object) -> str | None:
    """把各种日期表示统一成 ``"YYYY-MM-DD"``；解析不出来返回 ``None``（**不编日期**）。

    支持 ``date`` / ``datetime`` / ``"YYYY-MM-DD"`` / ``"YYYYMMDD"`` / ``"YYYY/MM/DD"``。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    text = text[:10]
    for fmt in ("%Y-%m-%d", "%Y/%M/%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def build_run_dir(asof: object = None, base: str | Path | None = None) -> Path:
    """当天（``asof``）的产物目录：``<base>/<YYYYMMDD>/``，**不存在则创建**。

    ``base`` 缺省用 ``daily_config.RUN_ROOT``（每日产物根）。
    """
    day = normalize_date(asof) or date.today().isoformat()
    root = Path(base) if base is not None else Path(DC.RUN_ROOT)
    out = root / day.replace("-", "")
    out.mkdir(parents=True, exist_ok=True)
    return out


def log_path(asof: object = None, log_dir: str | Path | None = None) -> Path:
    """当天（``asof``）的运行日志路径：``<log_dir>/daily_run_<YYYYMMDD>.log``。

    **父目录不存在则创建**（cron 首次落盘时目录可能还没有）。
    """
    day = normalize_date(asof) or date.today().isoformat()
    root = Path(log_dir) if log_dir is not None else Path(DC.LOG_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"daily_run_{day.replace('-', '')}.log"


def draft_path(asof: object = None, base: str | Path | None = None) -> Path:
    """当天（``asof``）的**邮件草稿**路径：``<run_dir>/email_draft_<YYYYMMDD>.json``。

    🔴 **为什么要有这个文件**：把「分析」与「发信」拆成两个进程后，
    16:00 / 17:00 的发信进程**不重跑分析**，只读这份持久化草稿
    ⇒ 分析跑多久都不影响发信时刻的准时性。

    **父目录不存在则创建**（与 `build_run_dir` 一致）。
    """
    day = normalize_date(asof) or date.today().isoformat()
    run_dir = build_run_dir(day, base)
    return run_dir / f"email_draft_{day.replace('-', '')}.json"
