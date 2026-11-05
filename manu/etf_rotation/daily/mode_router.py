"""mode_router.py —— **双轨模式的判定**与每日产物路径。

【为什么要有这个模块】
每日流水线要在两条轨道之间自动分流：

* **模式 A（日常巡检）**：静默防守窗口（2026-11-06 ~ 2026-11-30）里，
  **只**检验唯一持仓 512800 现价与 MA60 的安全垫距离 —— 不打分、不调 LLM、不开新仓；
* **模式 B（调仓节点）**：正式调仓信号日（2026-12-01 收盘出信号，2026-12-02 开盘执行）
  才跑全面评估并产出**可下单的执行卡片**。

分流口径只有一条：**当天是不是锁定的正式调仓信号日**（`daily_config.NEXT_SIGNAL_DATE`）。
把它单独抽成一个模块，是为了让「谁决定今天跑哪条轨道」这件事**只有一个 owner**，
避免调度脚本、邮件脚本、行情脚本各写一份判定（口径漂移的经典来源）。

【🔴 口径一致性声明】
本模块的模式判定与 `daily/calendar_gate.py::resolve_mode` **口径一致**
（同一条规则：命中 `NEXT_SIGNAL_DATE` ⇒ B，其余 ⇒ A）。
🔴 **刻意不 import `calendar_gate`**：两个模块由不同的 agent 并行维护，
互相 import 会形成**双向依赖**，且在只有一方落地时直接 ImportError。
⇒ 这里按 `daily_config.NEXT_SIGNAL_DATE` **独立实现**同一口径，
两边的规则源都是 `daily_config`（单一事实源）⇒ 口径不会漂。

【职责边界】
* `route`：**纯判定**，**不创建任何目录**（避免「看一眼路由就把磁盘写满」）；
* `build_run_dir` / `log_path`：**负责建目录**（`parents=True, exist_ok=True`）。
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


def _calendar_days(calendar_path: str | Path) -> list[str]:
    """读交易日历文件（每行一个 ``YYYY-MM-DD``）⇒ 日期字符串列表。

    读不到 / 解析不出 ⇒ 返回空列表（**不抛异常**）：日历只是**披露**用
    （「今天是不是交易日」），不是模式判定的依据 ⇒ 不该因此让整条流水线挂掉。
    """
    path = Path(calendar_path)
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        day = normalize_date(raw.strip())
        if day:
            out.append(day)
    return out


def route(asof: object = None, calendar_path: str | Path | None = None) -> dict:
    """判定 ``asof`` 当天该走哪条轨道。

    返回
    ----
    dict，键：

    * ``date``：规范化后的日期（``"YYYY-MM-DD"``）；
    * ``mode``：``"A"``（日常巡检）或 ``"B"``（调仓节点）；
    * ``reason``：判定理由（中文，说明**为什么**是这个模式）；
    * ``is_silent_defense``：当天是否落在静默防守窗口（2026-11-06 ~ 2026-11-30）；
    * ``execution_date``：模式 B 的执行日（``2026-12-02``）；模式 A 为 ``None``；
    * ``run_dir``：当天的产物目录路径（**字符串**，**本函数不创建目录**）。

    额外键（披露用）：``notes``（提示列表）、``is_trading_day``（是否交易日，
    给了 ``calendar_path`` 且日历可读时才有值，否则 ``None``）。

    🔴 **本函数不产生任何交易指令** —— 模式判定与下单是两件事。
    """
    day = normalize_date(asof) or date.today().isoformat()

    if day == DC.NEXT_SIGNAL_DATE:
        mode = "B"
        reason = (
            f"{day} 是锁定的正式调仓信号日（daily_config.NEXT_SIGNAL_DATE）"
            f" ⇒ 模式 B：全面评估 {DC.PRIMARY_HOLDING_CODE} 并产出执行卡"
            f"（执行日 {DC.NEXT_EXECUTION_DATE}）"
        )
        execution_date = DC.NEXT_EXECUTION_DATE
    else:
        mode = "A"
        reason = (
            f"{day} 不是调仓信号日（锁定的信号日 = {DC.NEXT_SIGNAL_DATE}）"
            f" ⇒ 模式 A：只巡检 {DC.PRIMARY_HOLDING_CODE} 的 MA{DC.EXIT_MA_WINDOW} 安全垫"
            f"，不跑打分、不调 LLM、不产生任何新仓位指令"
        )
        execution_date = None

    silent = bool(DC.SILENT_DEFENSE_START <= day <= DC.SILENT_DEFENSE_END)

    notes: list[str] = []
    if silent:
        notes.append(
            f"🔴 **静默防守窗口**（{DC.SILENT_DEFENSE_START} ~ {DC.SILENT_DEFENSE_END}）："
            f"只巡检 {DC.PRIMARY_HOLDING_CODE} 与 MA{DC.EXIT_MA_WINDOW} 的距离，"
            f"严禁产生任何新仓位指令"
        )
    if mode == "A" and day == DC.NEXT_SIGNAL_DATE:
        notes.append("⚠️ 模式判定异常：命中信号日却走了模式 A ⇒ 请检查 daily_config")
    if mode == "B" and day != DC.NEXT_SIGNAL_DATE:
        notes.append("⚠️ 模式判定异常：非信号日却走了模式 B ⇒ 请检查 daily_config")

    is_trading_day: bool | None = None
    if calendar_path is not None:
        days = _calendar_days(calendar_path)
        if days:
            is_trading_day = day in set(days)
            notes.append(
                f"交易日历 `{calendar_path}`：{day} "
                + ("是交易日" if is_trading_day else "**不是交易日**（不影响模式判定）")
            )
        else:
            notes.append(f"⚠️ 交易日历 `{calendar_path}` 读不到或为空 ⇒ 不披露是否交易日")

    return {
        "date": day,
        "mode": mode,
        "reason": reason,
        "is_silent_defense": silent,
        "execution_date": execution_date,
        "run_dir": str(Path(DC.RUN_ROOT) / day.replace("-", "")),
        "is_trading_day": is_trading_day,
        "notes": notes,
    }


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
