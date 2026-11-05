"""calendar_gate.py —— **每日流水线的交易日历门**（fail-closed 判定 + 模式分流）。

【本模块是干什么的】
每日流水线每个交易日 15:30 被 cron 触发，但 **cron 只按「周一~周五」过滤**（`DC.CRON_EXPR`）：

    30 15 * * 1-5

⇒ 法定节假日（A股休市日）**照样会被触发** ⇒ 必须在**进程内**再判一次「今天是不是交易日」，
    不是交易日就**静默退出 0**（不产邮件、不产产物、不报错）。

🔴 **本模块的核心纪律：fail-closed（宁可炸，不许静默跳过）**
「今天是不是交易日」这个判定的**失败方向是不对称的**：

* 判**是**交易日但其实不是（把假日当交易日）⇒ 多跑一次，多写点数据，**无害**；
* 判**不是**交易日但其实**是**（把交易日当假日）⇒ **每个交易日都被静默跳过** ⇒
  调仓节点日（2026-12-01）当天什么都不跑 ⇒ 用户以为「系统正常」，
  实则**整整一个月没有信号、没有调仓、没有告警** —— 这是**最危险的静默失败**。

⇒ 所以：`is_trading_day` 在**日历读不到 / 日历覆盖不到目标日**时
  **必须抛 `CalendarUnavailable`**，**绝不允许返回 False**
  （False 的语义是「今天是假日 ⇒ 安静退出」，日历挂了却走这条分支 ⇒ 灾难）。

【为什么日期工具放在本模块而不是研究链】
研究链 `manu/etf_rotation/analyze.py` 里已有 `_next_rebalance`（调仓节点推导），
但它是**报告生成器**的一部分、且口径跟着报告走；每日流水线需要一个**独立、可测、
零副作用**的口径入口 ⇒ 放在这里，同时把「节点日 = 次月首个交易日、
执行日 = 节点日 + 1 个交易日」这条口径与研究链对齐（`manu/etf_rotation/analyze.py`
的 `_next_rebalance`）。

【口径来源】
* 交易日历：`DC.TRADE_CALENDAR_CSV`（akshare 抓的**完整**日历，含未来交易日；
  qlib 自带的 `calendars/day.txt` **只到最后一个有行情的交易日** ⇒ 推不出未来节点）。
* 日期解析与格式校验：复用研究链 `data/refresh_policy.py::load_trading_dates`
  （返回 `YYYYMMDD` 紧凑格式，本模块统一转成 `YYYY-MM-DD`）。

【纪律】
* **纯判定**：不联网、不写文件、不 import akshare；
* 每个公开函数**中文 docstring**（写「为什么」）；4 空格；snake_case；
* 读不到必需输入 ⇒ **抛中文异常**，**不许**编日期（铁律十四.5）。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

#: 导入同包常量（避免与 `manu/etf_rotation/config.py` 撞名）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: 研究链根（`manu/etf_rotation`）：`data/` 下的脚本**不是包**，只能按绝对路径加载
_RESEARCH_ROOT = _HERE.parent
if str(_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_ROOT))

#: 模式常量
MODE_A = "A"
MODE_B = "B"


class CalendarUnavailable(RuntimeError):
    """交易日历读不到 / 覆盖不到目标日期 ⇒ **拒绝判定**（不许当成「非交易日」）。"""


# ---------------------------------------------------------------- 研究链模块加载
_REFRESH_POLICY = None


def _refresh_policy():
    """按**绝对路径**加载研究链的 `data/refresh_policy.py`（它不是包，不能 `import`）。

    研究链脚本依赖 `sys.path` 里的 `manu/etf_rotation`（`import config as C`），
    本模块已把该目录加进 `sys.path`，所以按路径加载的子模块也能正常 import。
    """
    global _REFRESH_POLICY
    if _REFRESH_POLICY is None:
        path = _RESEARCH_ROOT / "data" / "refresh_policy.py"
        spec = importlib.util.spec_from_file_location("etf_rotation_refresh_policy", path)
        if spec is None or spec.loader is None:  # pragma: no cover - 防御
            raise CalendarUnavailable(f"研究链模块加载失败：`{path}`（无法创建 import spec）")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _REFRESH_POLICY = module
    return _REFRESH_POLICY


# ---------------------------------------------------------------- 基础工具
def _calendar_path(calendar_path=None) -> Path:
    """日历路径：显式传入优先，否则用 `DC.TRADE_CALENDAR_CSV`。"""
    return Path(calendar_path) if calendar_path else Path(DC.TRADE_CALENDAR_CSV)


def _parse_date(value: str, what: str = "日期") -> date:
    """严格解析 `YYYY-MM-DD`；格式非法 ⇒ 抛中文 `ValueError`（不静默当成「非交易日」）。"""
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{what}格式非法：{value!r}（要求 `YYYY-MM-DD`，例如 `2026-11-05`）"
        ) from exc


def today_str() -> str:
    """本机本地时区当天（`YYYY-MM-DD`）。

    🔴 用**本地时区**而不是 UTC：服务器在 Asia/Shanghai，
    UTC 的「今天」在 08:00 之前会落后一天 ⇒ 会把调仓节点日判错。
    """
    return date.today().isoformat()


# ---------------------------------------------------------------- 交易日历
def load_trading_dates(calendar_path=None) -> list[str]:
    """读交易日历，返回**升序** `YYYY-MM-DD` 列表。

    复用研究链 `data/refresh_policy.py::load_trading_dates`（返回 `YYYYMMDD` 紧凑格式），
    本函数统一转成 `YYYY-MM-DD`。

    🔴 **读不到 / 为空 ⇒ 抛 `CalendarUnavailable`**（不许返回空列表）：
    空列表会让下游把**每一天**都判成「非交易日」⇒ 整条流水线静默空转。
    """
    path = _calendar_path(calendar_path)
    compact = _refresh_policy().load_trading_dates(path)
    if not compact:
        raise CalendarUnavailable(
            f"交易日历读不到或为空：`{path}` 不存在 / 是空文件 / 解析不出日期"
            f" ⇒ 拒绝判定今天是不是交易日（**不许**当成「非交易日」静默跳过）"
        )
    return [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in compact]


def is_trading_day(date_str, calendar_path=None) -> bool:
    """`date_str` 是交易日 ⇒ True。

    🔴 **fail-closed 语义**：

    * 日历读不到 / 为空 ⇒ 抛 `CalendarUnavailable`（**不是** False）；
    * 日历**覆盖不到**目标日期（目标日在日历末尾之后，或在日历起点之前）⇒ 抛
      `CalendarUnavailable`（附带「日历最后一天是 X」信息）；
    * `date_str` 格式非法 ⇒ 抛 `ValueError`。

    只有「日历正常、且目标日期确实在日历里」时，答案才是 False（今天确实休市）。
    """
    target = _parse_date(date_str, "日期")
    dates = load_trading_dates(calendar_path)
    first, last = dates[0], dates[-1]
    if target.isoformat() > last or target.isoformat() < first:
        raise CalendarUnavailable(
            f"交易日历覆盖不到目标日 {target.isoformat()}"
            f"（日历覆盖区间 {first} ~ {last}，最后一天是 {last}）"
            f" ⇒ 拒绝判定（**不许**当成「非交易日」静默跳过）"
        )
    return target.isoformat() in set(dates)


# ---------------------------------------------------------------- 日期工具
def next_trading_day(date_str, calendar_path=None) -> str:
    """`date_str` 之后的**第一个**交易日（严格大于）；日历覆盖不到 ⇒ 抛 `CalendarUnavailable`。"""
    target = _parse_date(date_str, "日期")
    dates = load_trading_dates(calendar_path)
    for day in dates:
        if day > target.isoformat():
            return day
    raise CalendarUnavailable(
        f"交易日历里没有 {target.isoformat()} 之后的交易日"
        f"（日历最后一天是 {dates[-1]}）⇒ 推不出下一个交易日"
    )


def prev_trading_day(date_str, calendar_path=None) -> str:
    """`date_str` 之前的**最后一个**交易日（严格小于）；日历覆盖不到 ⇒ 抛 `CalendarUnavailable`。"""
    target = _parse_date(date_str, "日期")
    dates = load_trading_dates(calendar_path)
    for day in reversed(dates):
        if day < target.isoformat():
            return day
    raise CalendarUnavailable(
        f"交易日历里没有 {target.isoformat()} 之前的交易日"
        f"（日历最早一天是 {dates[0]}）⇒ 推不出上一个交易日"
    )


def trading_days_between(start, end, calendar_path=None) -> list[str]:
    """区间 `(start, end]` 内的交易日列表（**左开右闭**）。

    🔴 左开右闭与研究链 `rules.md §十四.7` 的持仓收益区间口径一致
    （信号日 T → 执行日 T+1 ⇒ 持仓区间取 `(exec_i, exec_{i+1}]`）。
    """
    start_day = _parse_date(start, "起始日期")
    end_day = _parse_date(end, "结束日期")
    lo, hi = start_day.isoformat(), end_day.isoformat()
    if lo > hi:
        raise ValueError(f"起始日期 {lo} 晚于结束日期 {hi} ⇒ 区间非法")
    return [day for day in load_trading_dates(calendar_path) if lo < day <= hi]


def count_trading_days(start, end, calendar_path=None) -> int:
    """区间 `(start, end]` 内的交易日**个数**（与 `trading_days_between` 同口径）。"""
    return len(trading_days_between(start, end, calendar_path))


def next_rebalance_node(after, calendar_path=None) -> dict:
    """下一个调仓节点（口径与研究链 `analyze.py::_next_rebalance` 对齐）。

    * **节点日（target_date == signal_date）** = `after` 所在自然月的**下一个自然月的第一个交易日**；
    * **执行日（execution_date）** = 节点日之后的第 `DC.EXECUTION_LAG_DAYS` 个交易日
      （当前 = 1 ⇒ 节点日次一交易日**收盘**执行）；
    * `trading_days_until` = `(after, target_date]` 内的交易日个数。

    返回键：`target_date` / `signal_date` / `execution_date` / `trading_days_until` /
    `rule` / `execution_rule` / `execution_note`。

    🔴 **日历覆盖不到执行日 ⇒ `execution_date=None` + 说明**，**不许编日期**。
    """
    after_day = _parse_date(after, "基准日期")
    dates = load_trading_dates(calendar_path)
    after_iso = after_day.isoformat()

    year, month = after_day.year, after_day.month + 1
    if month > 12:
        year, month = year + 1, 1
    month_key = f"{year}-{month:02d}"
    candidates = [d for d in dates if d > after_iso and d.startswith(month_key)]
    if not candidates:
        raise CalendarUnavailable(
            f"交易日历里没有 {month_key} 的交易日（日历覆盖 {dates[0]} ~ {dates[-1]}）"
            f" ⇒ 推不出「下一个自然月的第一个交易日」（**不许**编日期）"
        )
    target = candidates[0]

    lag = int(DC.EXECUTION_LAG_DAYS)
    after_target = [d for d in dates if d > target]
    execution = after_target[lag - 1] if lag >= 1 and len(after_target) >= lag else None
    if execution is None:
        execution_note = (
            f"⚠️ 交易日历未覆盖信号日 {target} 之后的第 {lag} 个交易日"
            f"（日历最后一天是 {dates[-1]}）⇒ **执行日算不出来**（不许编日期）"
        )
        execution_rule = (
            f"信号日 {target} 收盘出信号、执行日 = 之后第 {lag} 个交易日"
            f"（DC.EXECUTION_LAG_DAYS={lag}）；当前日历覆盖不到 ⇒ 待补日历后重算"
        )
    else:
        execution_note = (
            f"信号日 {target} 收盘出信号 ⇒ 执行日 {execution}"
            f"（T+{lag} 个交易日收盘执行）"
        )
        execution_rule = (
            f"信号日 {target} 收盘出信号、T+{lag} 个交易日收盘执行"
            f"（DC.EXECUTION_LAG_DAYS={lag}）"
        )

    return {
        "target_date": target,
        "signal_date": target,
        "execution_date": execution,
        "trading_days_until": len([d for d in dates if after_iso < d <= target]),
        "rule": "下一个自然月的第一个交易日（akshare 完整交易日历）",
        "execution_rule": execution_rule,
        "execution_note": execution_note,
    }


# ---------------------------------------------------------------- 模式分流
def is_silent_defense_window(date_str) -> bool:
    """`date_str` 落在**静默防守窗口**（含端点）⇒ True。

    静默防守窗口 = `DC.SILENT_DEFENSE_START` ~ `DC.SILENT_DEFENSE_END`：
    期间**严禁**生成任何买入指令（只刷数据 + 监控 + 披露）。
    """
    day = _parse_date(date_str, "日期")
    start = _parse_date(DC.SILENT_DEFENSE_START, "静默防守窗口起点")
    end = _parse_date(DC.SILENT_DEFENSE_END, "静默防守窗口终点")
    return start <= day <= end


def resolve_mode(date_str) -> str:
    """当日模式：`DC.NEXT_SIGNAL_DATE`（2026-12-01）⇒ 模式 B；其余 ⇒ 模式 A。

    * 模式 A：**只刷新数据 + 监控 + 推送日报**，不调仓、不调 LLM；
    * 模式 B：**调仓节点日**（出信号、生成调仓清单、跑 LLM 辩论、下发执行卡）。

    🔴 分流**只看日期**（不猜、不兜底）：信号日是用户锁死的 `DC.NEXT_SIGNAL_DATE`。
    """
    day = _parse_date(date_str, "日期")
    return MODE_B if day.isoformat() == str(DC.NEXT_SIGNAL_DATE) else MODE_A


def describe_mode(date_str) -> dict:
    """当日模式的**完整描述**（给 `main.py` / 邮件 / 产物做统一口径）。

    返回键：`date` / `mode` / `is_silent_defense` / `is_signal_date` /
    `execution_date` / `reason`。
    """
    day = _parse_date(date_str, "日期")
    mode = resolve_mode(day.isoformat())
    is_signal = day.isoformat() == str(DC.NEXT_SIGNAL_DATE)
    is_execution_day = day.isoformat() == str(DC.NEXT_EXECUTION_DATE)
    silent = is_silent_defense_window(day.isoformat())
    execution_date = (
        str(DC.NEXT_EXECUTION_DATE) if (is_signal or is_execution_day) else None
    )

    if is_signal:
        reason = (
            f"{day.isoformat()} 是锁定的下一正式调仓**信号日**（DC.NEXT_SIGNAL_DATE）"
            f" ⇒ 走模式 B：收盘出信号 → 生成调仓清单 → 跑 LLM 辩论 → 下发执行卡"
            f"（执行日 {DC.NEXT_EXECUTION_DATE} 开盘）"
        )
    elif is_execution_day:
        reason = (
            f"{day.isoformat()} 是上一信号日的**执行日**（DC.NEXT_EXECUTION_DATE）"
            f" ⇒ 模式 A（只刷数据 + 监控 + 推送日报）；执行卡在**开盘**执行，本日不产生新信号"
        )
    elif silent:
        reason = (
            f"{day.isoformat()} 落在静默防守窗口（{DC.SILENT_DEFENSE_START} ~ "
            f"{DC.SILENT_DEFENSE_END}）⇒ 只刷数据 + 监控 + 披露，**严禁**任何买入指令"
        )
    else:
        reason = (
            f"{day.isoformat()} 不是调仓信号日 ⇒ 走模式 A："
            f"只刷新数据 + 监控 + 推送日报，不调仓、不调 LLM"
        )

    return {
        "date": day.isoformat(),
        "mode": mode,
        "is_silent_defense": bool(silent),
        "is_signal_date": bool(is_signal),
        "execution_date": execution_date,
        "reason": reason,
    }


def _ensure_hash_seed() -> None:
    """固定 `PYTHONHASHSEED=0`（**不重入进程**）。

    🔴 与研究链 `analyze.py::_ensure_hash_seed` 的差别：本模块是**库**，
    会被 `pytest` / 其它模块 import ⇒ 在 import 期 `execv` 重启进程会把调用方
    （例如正在跑的测试进程）一起杀掉 ⇒ 这里只设环境变量、不重启。
    """
    import os

    if os.environ.get("PYTHONHASHSEED") is None:
        os.environ["PYTHONHASHSEED"] = "0"


if __name__ == "__main__":  # pragma: no cover - 手工自检
    import json

    _ensure_hash_seed()
    print(json.dumps(describe_mode(today_str()), ensure_ascii=False, indent=2))
