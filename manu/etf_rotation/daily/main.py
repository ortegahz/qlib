"""main.py —— **ETF 轮动分析流水线**（单一入口 ＋ 不可变快照 ＋ 一个副作用开关）。

【2026-11-09 架构瘦身：Mode A / B / C ⇒ 一条流水线】
旧形态：三个模式分诊（A 日常巡检 / B 调仓节点 / C 影子推演）＋ 根目录全局共享文件
⇒ 三套分支、三套产物根、三套语义 ⇒「忘了加 if」就会漏出新仓位指令、也会污染生产金标。
新形态：

.. code-block:: bash

    python manu/etf_rotation/daily/main.py --asof <date> [--full] [--live-trade] [--dry-run]

* **单一入口**：一次跑完六个阶段，**没有模式分支**；
* **一个快照**：每次运行落 ``<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/``
  （自包含、只增不改 ⇒ 可以随时 diff 两份跑）；
* **一个副作用开关**：``--live-trade``。
  缺省 = **观察态**（执行卡挂单禁用、不发信）；``--live-trade`` = 实盘态（可下单、可发信）。
* **不写根目录全局文件**：canonical ``stock_scores.csv`` 只在「还没有任何快照时」
  当**一次性只读种子**用；每日链自成一链（底座 = **上一份快照**）。

【六阶段全流程】
```
 阶段1  AkShare 盘后数据管道   取 ETF 日线 + 读本地 W(45天TTL只读) + qlib 数据集滚动快照换库
   ↓
 阶段2  Qlib S 层全市场 Alpha    qlib_scores.py --mode infer --end <信号日>（≈27min）
   ↓
 阶段3  ETF 穿透 E=W×S + 均线   analyze.py：MA20 准入 / MA60 止损 → Top-5 候选
   ↓
 阶段4  TradingAgents 多智能体  run_tradingagents.py（≈15min）
   ↓
 阶段5  规则层约束对账          final_rebalance_plan.py + reprice_node_day.py
   ↓
 阶段6  实盘执行卡 + 报告       execution_card_<日期>.json + Markdown 报告（→ 窗口）
```

【用法】
```bash
# 观察态（缺省）：落快照 + 报告，不发信、不下单
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-09
# 强制全流程（与任何 skip 开关冲突时以 --full 为准，并披露被覆盖的开关）
... main.py --asof 2026-11-09 --full
# 实盘态：执行卡可下单、允许发信
... main.py --asof 2026-12-01 --live-trade --send-email
```
退出码：0 = 成功（含「非交易日静默退出」）；1 = 失败；2 = 用法/环境错误。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402
import snapshot as snap_mod  # noqa: E402

#: 日志格式：`时间 | 级别 | 阶段 | 消息`
_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)-14s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

#: 🔴 **`--mode` 已废除**（2026-11-09）—— 传了就拒，并给迁移提示
_MODE_MIGRATION_HINT = (
    "🔴 **`--mode` 已废除**（2026-11-09 架构瘦身：Mode A/B/C ⇒ 单一流水线 + 快照制）。\n"
    "   迁移对照：\n"
    "     --mode B            →  **直接去掉**（缺省就是完整六阶段流水线）\n"
    "     --mode A            →  **直接去掉**（日常巡检已并入流水线，不再单独分流）\n"
    "     --mode C            →  **直接去掉**（影子推演已废除 ⇒ 缺省即观察态）\n"
    "   新参数：`--full`（强制全流程）/ `--live-trade`（实盘副作用）/ `--dry-run`（观察态）\n"
    "            / `--snapshot-root`（快照根，缺省 daily_config.SNAPSHOT_ROOT）\n"
    "   产物：每次运行落 `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/`（自包含、可 diff）\n"
)


class StageError(RuntimeError):
    """某个阶段失败（带阶段名，便于失败报告点名）。"""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"[{stage}] {cause!r}")
        self.stage = stage
        self.cause = cause


class LockError(RuntimeError):
    """单实例锁被占（已有另一个实例在跑）—— **不是故障**。"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行解析。"""
    p = argparse.ArgumentParser(
        prog="etf-rotation-daily",
        description="ETF 轮动分析流水线（单一入口 ＋ 不可变快照；观察态缺省，实盘态 --live-trade）",
    )
    p.add_argument("--asof", default=None,
                   help="业务日期 YYYY-MM-DD（缺省取本机当天）")
    p.add_argument("--full", action="store_true",
                   help="🔴 强制走完**全部环节**（与任何 skip 开关冲突时以 --full 为准；"
                        "被覆盖的开关会在 manifest 与报告里**显著披露**）")
    p.add_argument("--live-trade", action="store_true",
                   help="🔴 **唯一**实盘副作用开关：执行卡**可下单**、允许发信"
                        "（缺省 = 观察态：执行卡挂单禁用、不发信）")
    p.add_argument("--dry-run", action="store_true",
                   help="观察态（**显式声明**）：跑完落快照 + 报告，但**不发信、不下单**"
                        "（与 --live-trade 互斥）"
                        "｜🔴 观察态**本来就是缺省行为** ⇒ 本开关**不改变任何行为**，"
                        "只为了让脚本自解释（别把它当成「不产出产物」的预演）")
    p.add_argument("--mode", default=None,
                   help="🆑 **已废除**（保留只为给出迁移提示）")
    p.add_argument("--report-md", default=None,
                   help="报告落盘路径（缺省 = <RESULTS_ROOT>/reports/report_<YYYYMMDD>.md）")
    p.add_argument("--send-email", action="store_true",
                   help="🔴 显式开启邮件推送（**默认不发**；属实盘副作用 ⇒ 需配合 --live-trade）")
    p.add_argument("--no-email", action="store_true",
                   help="兼容保留（默认行为就是不发信；此参数是空操作）")
    p.add_argument("--skip-fetch", action="store_true", help="跳过取数阶段（用现有数据）")
    p.add_argument("--skip-llm", action="store_true", help="跳过 TradingAgents（无 API 配额时用）")
    p.add_argument("--send-only", action="store_true",
                   help="🔴 只发已持久化的邮件草稿（不重跑分析）—— 按需补发时用（需 --live-trade）")
    p.add_argument("--wait-seconds", type=float, default=1800.0,
                   help="--send-only 时等待草稿出现的秒数（默认 1800 = 30 分钟）")
    p.add_argument("--refresh-scores", action=argparse.BooleanOptionalAction, default=None,
                   help="是否重跑 S 层（重活，约 27 分钟；缺省：开）")
    p.add_argument("--update-qlib-bin", action=argparse.BooleanOptionalAction, default=None,
                   help="是否刷新 qlib 数据集（滚动快照换库；缺省：开）")
    p.add_argument("--no-increment", action="store_true",
                   help="不触发「自建增量日」兜底（上游快照覆盖不到信号日时，S 层会滞后并在报告披露）")
    p.add_argument("--increment-dry-run", action="store_true",
                   help="「自建增量日」只出计划**不落盘**（默认真落盘：只写派生目录 cn_data_live，基座零改动）")
    p.add_argument("--increment-apply", action="store_true",
                   help="（兼容别名，**空操作**）：默认已是真落盘 ⇒ 想只看计划请用 --increment-dry-run")
    p.add_argument("--llm-timeout", type=float, default=None,
                   help="阶段 4（TradingAgents 辩论）的超时上限（秒）；"
                        "缺省 = daily_config.LLM_TIMEOUT（默认 5400，可由 ETF_ROTATION_LLM_TIMEOUT 覆盖）")
    p.add_argument("--reasoning-effort", default="", choices=["max", "high", "medium", "low"],
                   help="阶段 4 的 reasoning_effort 覆盖（缺省空串 = **不覆盖**，"
                        "沿用 TradingAgents/.env 里现役的 max）；"
                        "实测 MiniMax 单次调用：max 185s / high 116s / medium 91s")
    p.add_argument("--snapshot-root", default=None,
                   help="快照根目录（缺省用 daily_config.SNAPSHOT_ROOT）")
    p.add_argument("--run-root", default=None, help="（兼容）产物目录；缺省 = 本轮快照目录")
    p.add_argument("--log-dir", default=None, help="日志目录（缺省用 daily_config.LOG_DIR）")
    p.add_argument("--calendar", default=None, help="A 股交易日历 CSV（缺省用 akshare 缓存）")
    return p.parse_args(argv)


def resolve_stage_flags(args: argparse.Namespace) -> dict:
    """决定**每个阶段跑不跑**（**纯函数** ⇒ 可单测）。

    规则：

    * 缺省：取数 / S 层重算 / qlib 换库 / LLM **全开**（= 完整六阶段）；
    * 显式 skip 开关 ⇒ 关掉对应阶段；
    * ``--full`` ⇒ **强制五个语义开关全开**（取数 / S 层重算 / qlib 换库 / 增量补齐 / LLM；
      同时强制 ``increment_dry_run=False`` ⇒ 共覆盖 **6 个 CLI 开关**），
      并把被覆盖的 skip 开关登记进 ``skipped``
      （🔴 绝不静默降级 ⇒ 必须披露）。
    """
    skipped: list[dict] = []
    fetch = not bool(getattr(args, "skip_fetch", False))
    refresh = getattr(args, "refresh_scores", None) is not False
    update_qlib = getattr(args, "update_qlib_bin", None) is not False
    llm = not bool(getattr(args, "skip_llm", False))
    # 🔴 2026-11-09（独立审计 P1-3）：增量开关原先**不在 `--full` 管辖内**
    #    ⇒ 「走全部流程」可以被 `--no-increment` / `--increment-dry-run` 悄悄切掉
    #    ⇒ 现在纳入同一套「`--full` 覆盖 + 登记披露」。
    increment = not bool(getattr(args, "no_increment", False))
    increment_dry_run = bool(getattr(args, "increment_dry_run", False))
    full = bool(getattr(args, "full", False))
    if full:
        for name, is_off, flag in (
            ("skip_fetch", not fetch, "--skip-fetch"),
            ("skip_llm", not llm, "--skip-llm"),
            ("no_refresh_scores", not refresh, "--no-refresh-scores"),
            ("no_update_qlib_bin", not update_qlib, "--no-update-qlib-bin"),
            ("no_increment", not increment, "--no-increment"),
            ("increment_dry_run", increment_dry_run, "--increment-dry-run"),
        ):
            if is_off:
                skipped.append({
                    "name": name,
                    "reason": f"`--full` 强制全流程 ⇒ 覆盖 {flag}",
                })
        fetch = refresh = update_qlib = llm = True
        increment = True
        increment_dry_run = False
    return {
        "fetch": fetch,
        "refresh_scores": refresh,
        "update_qlib": update_qlib,
        "llm": llm,
        "increment": increment,
        "increment_dry_run": increment_dry_run,
        "full": full,
        "skipped": skipped,
    }


def setup_logging(log_path: Path) -> logging.Logger:
    """配置结构化日志（文件 + stdout）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("etf_rotation_daily")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FMT)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
    return root


class _RunLock:
    """单实例守卫（`fcntl.flock`）—— 防同一时刻重复跑。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def __enter__(self) -> "_RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise LockError(f"已有实例在跑（锁文件 {self.path}）") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def _today_str() -> str:
    """**市场时区**当天（`YYYY-MM-DD`）—— 🔴 不许用机器本地时区（见 `DC.MARKET_TZ`）。"""
    return datetime.now(DC.MARKET_TZ).strftime("%Y-%m-%d")


def _persist_draft(draft, run_dir: Path, asof: str, kind: str, to: list[str]) -> Path:
    """把渲染好的邮件草稿持久化 ⇒ ``<run_dir>/email_draft_<YYYYMMDD>.json``。

    🔴 保留这条链路是为了「按需补发邮件」：先跑分析（报告回窗口），
    之后若想发一封留档，``--send-only`` 只读这个草稿、**不重跑分析**。
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"email_draft_{asof.replace('-', '')}.json"
    payload = {
        "asof": asof,
        "kind": kind,
        "subject": draft.subject,
        "html_body": draft.html_body,
        "text_body": draft.text_body,
        "to": list(to),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def _write_heartbeat(
    run_dir: Path, asof: str, *, status: str, live_trade: bool = False,
    detail: dict | None = None,
) -> Path:
    """写**心跳** ⇒ ``<run_dir>/heartbeat_<YYYYMMDD>.json``（本轮跑过了的证据）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"heartbeat_{asof.replace('-', '')}.json"
    payload = {
        "asof": asof,
        "status": status,
        "live_trade": bool(live_trade),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "detail": detail or {},
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def _write_receipt(
    run_dir: Path, asof: str, *, subject: str, recipients: list[str], sent: bool
) -> Path:
    """写**已发回执** ⇒ ``<run_dir>/sent_<YYYYMMDD>T<HHMMSS>.json``。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    path = run_dir / f"sent_{asof.replace('-', '')}T{stamp}.json"
    payload = {
        "asof": asof,
        "subject": subject,
        "to": list(recipients),
        "sent": bool(sent),
        "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def _resolve_draft(asof: str, snapshot_root: Path, run_root: Path) -> Path:
    """定位当天的**邮件草稿**文件（**先快照、后旧目录**）。

    🔴 2026-11-09 修的真回归（本轮「快照化」改造带进来的）：
      · 草稿现在落在**快照目录**：``<SNAPSHOT_ROOT>/<run_id>/email_draft_<YYYYMMDD>.json``
        （``_persist_draft(draft, run_dir=snap.path, ...)``）；
      · 而 ``--send-only`` 仍按老口径去 ``<run_root>/<YYYYMMDD>/`` 找
        ⇒ **两个路径永不相交** ⇒ 空等 ``--wait-seconds``（默认 1800s）后
        **退出码 0 却根本没发信**（静默假成功，最坏的一种坏法）。

    ⇒ 现在按「**快照根（最新一份含草稿的快照）→ 旧的按日目录**」的顺序找；
      都没有 ⇒ 返回快照根下最新一份的候选路径（供等待循环继续轮询）。
    """
    day = str(asof).replace("-", "")
    name = f"email_draft_{day}.json"
    snap_dirs: list[Path] = []
    if snapshot_root.is_dir():
        snap_dirs = sorted(
            (p for p in snapshot_root.iterdir()
             if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name,
            reverse=True,          # 目录名 == 时间戳 ⇒ 字典序 == 时间序
        )
    # ① 快照根：取**最新一份确实含草稿**的快照
    for item in snap_dirs:
        cand = item / name
        if cand.is_file():
            return cand
    # ② 回落：旧的按日目录（改造前的老产物）
    legacy = Path(run_root) / day / name
    if legacy.is_file():
        return legacy
    # ③ 都没有 ⇒ 给一个候选路径，让等待循环继续轮询
    if snap_dirs:
        return snap_dirs[0] / name
    return legacy


def _send_only(args, asof: str, snapshot_root: Path, run_root: Path, log) -> int:
    """🔴 **按需补发邮件**分支：只发已持久化的邮件草稿，**不重跑任何计算**。

    行为：草稿已存在 ⇒ 立即发送（退出 0）；还没出来 ⇒ 轮询等待至多
    ``--wait-seconds`` 秒；等不到 ⇒ 写日志并退出 0（**不重复发失败告警**）。
    """
    import notify_email

    deadline = time.time() + max(0.0, float(args.wait_seconds))
    waited = 0.0
    while True:
        # 🔴 每轮都**重新解析**路径 ⇒ 等待期间新出的快照也能被看见
        draft_file = _resolve_draft(asof, snapshot_root, run_root)
        if draft_file.exists():
            break
        if time.time() >= deadline:
            log.warning(
                "延迟发信：%s 秒内没有等到草稿 %s ⇒ 本次不发", int(waited), draft_file
            )
            return 0
        time.sleep(15.0)
        waited += 15.0
        log.info("延迟发信：草稿还没生成，继续等待（已等 %.0f 秒）", waited)

    payload = json.loads(draft_file.read_text(encoding="utf-8"))
    recipients = payload.get("to") or None
    send_info = notify_email.send_email(
        payload["subject"],
        payload["html_body"],
        text_body=payload.get("text_body"),
        to=recipients,
        dry_run=False,
        out_dir=str(draft_file.parent),
    )
    log.getChild("notify").info(
        "延迟发信完成：sent=%s 主题=%s", send_info.get("sent"), payload.get("subject"),
    )
    receipt = _write_receipt(
        draft_file.parent, asof,
        subject=str(payload.get("subject") or ""),
        recipients=list(payload.get("to") or []),
        sent=bool(send_info.get("sent")),
    )
    log.getChild("notify").info("已发回执：%s", receipt)
    return 0


def _failure_report_path(asof: str) -> Path:
    """失败报告落盘路径（落交付目录 ``<RESULTS_ROOT>/reports/``）。"""
    stamp = asof.replace("-", "")
    return DC.REPORT_DIR / f"report_{stamp}_failed.md"


def _run_stages(args: argparse.Namespace, asof: str, snapshot_root: Path, log,
                state: dict | None = None) -> int:
    """六个阶段的实际执行（**单一路径**，无模式分支）。"""
    import account_state
    import agents_rebalance
    import calendar_gate
    import fetch_data
    import notify_email
    import report

    live_trade = bool(getattr(args, "live_trade", False))
    flags = resolve_stage_flags(args)

    # ---- 阶段 0：交易日历门（fail-closed）--------------------------------
    log_stage = log.getChild("calendar_gate")
    try:
        is_trading = calendar_gate.is_trading_day(asof, args.calendar)
    except Exception as exc:
        raise StageError("calendar_gate", exc) from exc
    if not is_trading:
        log_stage.info("今天不是 A 股交易日（%s）⇒ 静默退出，不产出报告", asof)
        snap = snap_mod.create_snapshot(snapshot_root, asof=asof, live_trade=live_trade)
        snap.set_stage("calendar_gate", "skipped", reason="非交易日")
        snap.set_skipped("all_stages", "非交易日 ⇒ 整轮静默退出")
        _write_heartbeat(snap.path, asof, status="skipped_non_trading_day",
                         live_trade=live_trade)
        snap.finalize()
        log_stage.info("心跳已落快照：%s", snap.path)
        return 0
    log_stage.info("交易日校验通过：%s 是 A 股交易日", asof)

    # ---- 建快照（本轮唯一产物根）------------------------------------------
    snap = snap_mod.create_snapshot(snapshot_root, asof=asof, live_trade=live_trade)
    run_dir = snap.path
    if state is not None:
        state["snapshot"] = snap
        state["run_dir"] = run_dir
    log_stage.info("📦 本轮快照目录：%s（live_trade=%s）", run_dir, live_trade)
    if live_trade:
        log_stage.info("🔴 **实盘态（--live-trade）**：执行卡可下单、允许发信")
    else:
        log_stage.info("🧪 **观察态**（未开 --live-trade）：%s", snap_mod.OBSERVATION_DISCLAIMER)
    for item in flags["skipped"]:
        snap.set_skipped(item["name"], item["reason"])
        log_stage.warning("⚠️ %s：%s", item["name"], item["reason"])

    refresh_scores = bool(flags["refresh_scores"])
    update_qlib = bool(flags["update_qlib"])
    # 🔴 2026-11-09 收口补齐：**每一个被跳过的阶段都要登记进 manifest**
    #    （原口径只登记 --skip-fetch / --no-update-qlib-bin ⇒ `--skip-llm`、
    #    `--no-refresh-scores` 会**静默降级** ⇒ 违反「绝不静默降级」）。
    if not flags["llm"]:
        snap.set_skipped("tradingagents", "--skip-llm")
    if not refresh_scores:
        snap.set_skipped("s_layer", "--no-refresh-scores")
    log_stage.info(
        "阶段开关：取数=%s｜refresh_scores=%s｜update_qlib=%s｜llm=%s｜full=%s",
        flags["fetch"], refresh_scores, update_qlib, flags["llm"], flags["full"],
    )
    # 🔴 阶段 4 的超时与推理强度：缺省读 `daily_config.LLM_TIMEOUT`（默认 5400 秒）
    llm_timeout = (
        float(DC.LLM_TIMEOUT) if args.llm_timeout is None else float(args.llm_timeout)
    )
    reasoning_effort = str(args.reasoning_effort or "")
    log_stage.info(
        "阶段4 参数：llm_timeout=%.0f 秒%s",
        llm_timeout,
        (f"｜reasoning_effort={reasoning_effort}（本轮覆盖 .env 现役值）"
         if reasoning_effort else "｜reasoning_effort=不覆盖（沿用 .env 现役值）"),
    )

    # ---- 阶段 1：AkShare 盘后数据管道 --------------------------------------
    #: 🔴 2026-11-10（独立审计）：**降级披露清单**。
    #:    凡是「本轮有环节没跑成 / 数据不新鲜」的事实，都必须**同时**进
    #:    ① manifest（阶段状态）② 报告 notes（`result["notes"]`）——
    #:    只进日志 = 用户看不到 = 「实时拉了数据」成了假象。
    degrade_notes: list[str] = []
    #: 阶段 1（取数 / 换库 / 增量）里的**硬失败**阶段名 ⇒ 也要进 --full 的失败清单
    stage_failed: list[str] = []
    fetch_info: dict = {}
    if not flags["fetch"]:
        log.getChild("fetch").info("跳过取数（--skip-fetch）")
        snap.set_skipped("fetch_data", "--skip-fetch")
    else:
        try:
            fetch_info = fetch_data.run_fetch(
                asof=asof,
                # 🔴 取数增量落**本快照**（canonical `etf_prices.csv` 只是被读）
                out_dir=run_dir,
                dry_run=False,
                # 🔴 cn_data 里只有个股、没有 ETF ⇒ 这里的「qlib 二进制增量」对本库无效；
                #    阶段 1 的个股侧由 stage1_dataset（滚动快照换库）负责。
                update_qlib=False,
            )
        except Exception as exc:
            raise StageError("fetch_data", exc) from exc
        log.getChild("fetch").info(
            "ETF 日线增量：新增 %s 行 | W 快照 %s~%s（滞后 %s 天，只披露不拦）",
            fetch_info.get("n_rows"),
            (fetch_info.get("holdings") or {}).get("min_fetch_date"),
            (fetch_info.get("holdings") or {}).get("max_fetch_date"),
            (fetch_info.get("holdings") or {}).get("stale_days"),
        )
        if fetch_info.get("guard_note"):
            log.getChild("fetch").warning("%s", fetch_info["guard_note"])
            # 🔴 2026-11-10（独立审查 NIT-4）：「今日 bar 尚未定型 ⇒ 丢弃了盘中数据」
            #    属**新鲜度降级事实** ⇒ 按铁律「降级事实必须同时进 manifest ＋ 报告 notes」
            #    ⇒ **不许**只进日志（只进日志 = 用户看不到 = 静默降级 = 违规）。
            snap.set_stage(
                "fetch_unfinalized_bar",
                "degraded",
                note=str(fetch_info["guard_note"]),
            )
            degrade_notes.append(
                "🔴 **今日 ETF 日线 bar 尚未定型**（收盘前抓取）⇒ 已丢弃盘中数据："
                f"{fetch_info['guard_note']}"
            )
        # 🔴 2026-11-10（独立审计 F2）：W（成分股权重）**永不重爬**（铁律：重爬会触发
        #    IP 封禁 ⇒ 每日链只读缓存）⇒ 它的陈旧度必须**进交付面**。
        #    否则「实时拉了数据」是**假象**：E=W×S 里的 W 可能还是 N 天前的快照，
        #    而报告/manifest 只字未提 ⇒ 用户无从判断穿透分用的是哪天的权重。
        holdings = fetch_info.get("holdings") or {}
        if holdings:
            snap.set_stage(
                "w_holdings",
                "stale" if holdings.get("stale") else "ok",
                max_fetch_date=holdings.get("max_fetch_date"),
                stale_days=holdings.get("stale_days"),
                mixed=bool(holdings.get("mixed")),
                n_codes=holdings.get("n_codes"),
            )
            if holdings.get("stale"):
                degrade_notes.append(
                    "🔴 **W（成分股权重）是只读缓存、不重爬**（铁律：重爬成分股会触发 IP 封禁）"
                    f" ⇒ 本轮 E=W×S 的 W 快照日 = {holdings.get('max_fetch_date')}"
                    f"（滞后 **{holdings.get('stale_days')} 天**）"
                    " ⇒ 「实时拉取」覆盖的是**行情 ＋ qlib 数据集**，**不覆盖 W**。"
                )

    # ---- 阶段 1（个股侧）：qlib 数据集滚动快照换库 --------------------------
    dataset_info: dict = {}
    dataset_error: str | None = None
    if update_qlib:
        try:
            import stage1_dataset

            dataset_info = stage1_dataset.run_stage1(
                asof=asof,
                provider_dir=DC.QLIB_CN_DATA,
                work_root=DC.STAGE1_WORK_ROOT,
                backup_dir=DC.STAGE1_BACKUP_DIR,
                dest_dir=DC.STAGE1_DEST_DIR,
                do_fetch=True,
                do_swap=True,
                dry_run=False,
                # 🔴 自建增量日兜底：上游快照覆盖不到 asof ⇒ 用全市场快照补缺失交易日
                #    ⇒ **只写派生目录**（`cn_data_live`），基座一个字节都不改。
                do_increment=flags["increment"],
                increment_dry_run=flags["increment_dry_run"],
            )
        except Exception as exc:
            # 🔴 **不整轮失败**：数据集刷新失败 ⇒ 降级用**旧数据集**继续跑，
            #    但必须在报告里**显著披露**。
            dataset_error = f"{type(exc).__name__}: {exc}"
            log.getChild("stage1").error(
                "qlib 数据集刷新失败（降级用旧数据集继续）：%s", dataset_error
            )
            log.getChild("stage1").error("原始报错：%s", exc)
            snap.set_stage("stage1_dataset", "failed", error=dataset_error)
            # 🔴 2026-11-10：qlib 数据集刷新失败 ⇒ 基座停在旧末日 ⇒ S 层拿不到信号日 Alpha
            #    ⇒ --full 下算「有环节没跑成」（铁律 I4：不许静默降级）。
            stage_failed.append("stage1_dataset")
        else:
            # 🔴 2026-11-10：把「换库细节」直接挂进 stage1_dataset 这**一条** manifest 条目
            #    （上游快照没推进 ⇒ 换库零收益跳过 ⇒ 读者一眼能看出「库没换」）
            snap.set_stage(
                "stage1_dataset", "ok",
                calendar_max_before=dataset_info.get("calendar_max_before"),
                calendar_max_after=dataset_info.get("calendar_max_after"),
                new_days=list(dataset_info.get("new_days") or []),
                swapped=bool(dataset_info.get("swapped")),
                downloaded=bool(dataset_info.get("downloaded")),
            )
            for note in dataset_info.get("notes") or []:
                log.getChild("stage1").info("%s", note)
            log.getChild("stage1").info(
                "qlib 数据集：%s → %s（新增 %s 个交易日）",
                dataset_info.get("calendar_max_before"),
                dataset_info.get("calendar_max_after"),
                len(dataset_info.get("new_days") or []),
            )
            increment_info = dataset_info.get("increment") or {}
            if increment_info.get("needed"):
                log.getChild("stage1").warning(
                    "🔴 上游快照覆盖不到信号日 ⇒ 触发「自建增量日」兜底（派生目录 %s）",
                    increment_info.get("entry", {}).get("live_dir"),
                )
                # 🔴 2026-11-10（独立审计 F1）：兜底**失败**原先只进 notes ⇒ notes 只进日志
                #    ⇒ 「整轮悄悄沿用旧 qlib 库、照样出执行卡」= **静默降级**（违反铁律 I4）。
                #    ⇒ 现在登记进 manifest（stage status=failed）＋ 报告披露。
                inc_ok = bool(increment_info.get("ok"))
                inc_notes = list(increment_info.get("notes") or [])
                inc_error = inc_notes[-1] if inc_notes else "（原因见日志）"
                snap.set_stage(
                    "stage1_increment",
                    "ok" if inc_ok else "failed",
                    days=list(increment_info.get("days") or []),
                    live_dir=str((increment_info.get("entry") or {}).get("live_dir") or ""),
                    error="" if inc_ok else inc_error,
                )
                if not inc_ok:
                    stage_failed.append("stage1_increment")
                    degrade_notes.append(
                        "🔴 **「自建增量日」兜底失败**（上游 qlib 快照覆盖不到信号日）"
                        " ⇒ qlib 基座停在旧末日 ⇒ S 层**只能打到旧数据**"
                        f"（失败原因：{inc_error}）"
                    )
    else:
        log.getChild("stage1").info("update_qlib=False ⇒ 本轮**不刷新** qlib 数据集")
        snap.set_skipped("stage1_dataset", "--no-update-qlib-bin")

    # ---- 阶段 1.5：合并行情（canonical + 当日增量） ------------------------
    try:
        # 🔴 **只在本快照内 glob** ⇒ 不会蹭到别的 run 的增量（跨 run 串味）
        prices_df = fetch_data.load_effective_prices(asof=asof, run_root=run_dir)
    except Exception as exc:
        raise StageError("load_effective_prices", exc) from exc
    prices_path = run_dir / f"etf_prices_effective_{asof.replace('-', '')}.csv"
    run_dir.mkdir(parents=True, exist_ok=True)
    prices_df.to_csv(prices_path, index=False, encoding="utf-8-sig")
    latest_quote = prices_df["date"].max() if not prices_df.empty else None
    log.getChild("fetch").info(
        "行情底座：%s 行（最新行情日 %s）→ %s",
        len(prices_df), latest_quote or "—", prices_path,
    )

    account = account_state.account_summary()

    # ---- 阶段 2~6：计算 ---------------------------------------------------
    log_stage = log.getChild("rebalance")
    try:
        result = agents_rebalance.run_pipeline(
            asof=asof,
            snapshot=snap,
            prices_csv=str(prices_path),
            skip_llm=(not flags["llm"]),
            llm_timeout=llm_timeout,
            reasoning_effort=reasoning_effort,
            refresh_scores=refresh_scores,
            live_trade=live_trade,
        )
    except Exception as exc:
        raise StageError("rebalance", exc) from exc

    for note in result.get("notes", []) or []:
        log_stage.info("%s", note)
    log_stage.info("统一流水线计算完成：mode=%s live_trade=%s",
                   result.get("mode"), result.get("live_trade"))
    # 🔴 2026-11-10：接住「失败阶段清单」⇒ --full 下据此硬失败（见本函数末尾）
    failed_stages = _record_pipeline_stages(
        snap, result, flags=flags, refresh_scores=refresh_scores
    )
    # 🔴 阶段 1 的失败（换库失败 / 增量兜底失败）是**直接**登记进 manifest 的
    #    （不在 ``_record_pipeline_stages`` 的管辖范围）⇒ 这里合并进来，
    #    否则「基座停在旧末日」这种降级在 --full 下仍会退出 0。
    failed_stages = list(dict.fromkeys(list(stage_failed) + list(failed_stages)))

    # ---- 阶段 6：报告（落盘 + stdout）→ 窗口 ------------------------------
    log_stage = log.getChild("report")
    result = dict(result)
    result["snapshot"] = {
        "root": str(snap.root),
        "path": str(run_dir),
        "run_id": str(snap.run_id),
    }
    # 🔴 2026-11-09（独立审计 P1-5）：被 `--full` 覆盖的 skip 开关原先**只进 manifest**、
    #    报告里看不到 ⇒ 「不许静默降级」在交付面失效。
    #    ⇒ 现在也进报告的「快照与运行口径」表（见 `report._provenance_rows`）。
    result["skipped_switches"] = list(flags.get("skipped") or [])
    # 🔴 2026-11-10：降级事实（W 陈旧 / 增量兜底失败 / --full 有环节没跑成）
    #    ⇒ 必须在**报告**里显著披露（只进日志 = 用户看不到）。
    if degrade_notes:
        result["notes"] = list(result.get("notes") or []) + degrade_notes
    if failed_stages:
        result["notes"] = list(result.get("notes") or []) + [
            "🔴 **本轮有环节没跑成**："
            + "、".join(failed_stages)
            + "（详见 `snapshot_manifest.json` 的 `stages`）"
            + ("⇒ `--full` 模式下**硬失败**：退出码 1 ＋ **不发信**（fail-closed）。"
               if flags.get("full")
               else "⇒ 本轮交付件**不完整**、**不发信**（fail-closed）＋请勿直接据此下单。")
        ]
    if dataset_info:
        result.setdefault("dataset", dataset_info)
    if dataset_error:
        result["dataset_error"] = dataset_error
        result.setdefault("notes", [])
        result["notes"] = list(result["notes"]) + [
            f"🔴 **阶段 1（qlib 数据集刷新）失败**：{dataset_error} ⇒ 本轮降级用**旧数据集**继续"
            " ⇒ 阶段 2 的 S 层**只能打到旧数据集的覆盖末日**，信号日当天的个股 Alpha 拿不到。"
            "（原始报错见日志 daily_run_<日期>.log）"
        ]
    if not result.get("node_reprice_ok", True):
        result.setdefault("notes", [])
        result["notes"] = list(result["notes"]) + [
            "🔴 **节点日重算未完成（观察态显式降级）**："
            f"{result.get('node_reprice_error') or '（原因见日志）'}"
            " ⇒ 本轮**不给节点日股数/金额**（🔴 不用信号日旧价冒充节点日价）"
            " ⇒ 执行卡只剩**方向与目标权重**、**不可下单**。"
        ]
    try:
        markdown = report.render_report(
            result, account=account, mode="unified", asof=asof, live_trade=live_trade,
        )
    except Exception as exc:
        raise StageError("report", exc) from exc

    report_path = _report_path(args.report_md, run_dir, asof)
    try:
        report.write_report(markdown, report_path)
    except Exception as exc:
        raise StageError("report_write", exc) from exc
    # 🔴 快照里也留一份（**快照才是真值**；交付目录那份是给人看的）
    snap.write_text(f"report_{asof.replace('-', '')}.md", markdown)
    log_stage.info("报告已落盘：%s（快照副本：%s）", report_path, snap.file("report.md").parent)
    _echo_report(markdown, log_stage)

    # ---- 可选：邮件（默认不发；属实盘副作用）------------------------------
    # 🔴 2026-11-10：**任何**模式下有环节没跑成 ⇒ **一律不发信**（fail-closed）：
    #    不许拿「有环节缺件」的交付件去触发实盘通知/报单。
    #    🔴 2026-11-10（独立审计补）：原先写成 `and flags.get("full")`
    #    ⇒ 非 `--full`（cron 口径）+ `--live-trade --send-email` + 阶段失败
    #    ⇒ 仍然**带着 degraded 交付件发出实盘通知**（实盘链路的真实口子）。
    #    ⇒ 现在「有失败 ⇒ 不发信」是**全局口径**；
    #    退出码口径不变（非 `--full` 仍返回 0，保持 cron 语义）。
    if args.send_email and failed_stages:
        log.getChild("notify").error(
            "🔴 本轮有环节没跑成（%s）⇒ **不发信**（fail-closed）",
            "、".join(failed_stages),
        )
    elif args.send_email:
        log_stage = log.getChild("notify")
        try:
            draft = _build_draft(notify_email, asof, result, account)
            try:
                recipients = list(notify_email.SmtpConfig.from_env().recipients)
            except Exception:
                recipients = []
            draft_path = _persist_draft(
                draft, run_dir, asof, str(result.get("email_kind") or ""), recipients
            )
            log_stage.info("邮件草稿已持久化：%s", draft_path)
            send_info = notify_email.send_email(
                draft.subject,
                draft.html_body,
                text_body=draft.text_body,
                dry_run=False,
                out_dir=str(run_dir),
            )
            log_stage.info(
                "邮件已处理：sent=%s path=%s",
                send_info.get("sent"), send_info.get("path"),
            )
        except Exception as exc:
            raise StageError("notify", exc) from exc
    else:
        log.getChild("notify").info(
            "邮件**未发送**（--send-email 未开）⇒ 本轮交付物 = 快照 + Markdown 报告"
        )

    _write_heartbeat(
        run_dir, asof,
        status="degraded" if failed_stages else "ok",
        live_trade=live_trade,
        detail={
            "report_md": str(report_path),
            "email_sent": bool(args.send_email),
            "refresh_scores": refresh_scores,
            "update_qlib": update_qlib,
            "snapshot": str(run_dir),
            "live_trade": live_trade,
        },
    )
    # 🔴 **快照冻结**：写 manifest（阶段状态 / 跳过项 / 实盘开关 / 每件 sha1）
    # 🔴 2026-11-10：manifest 里写着「有环节 failed」而进程却退出 0 = 静默降级
    #    ⇒ --full 下**硬失败**（退出码 1）；非 --full 保持退出码 0（日常 cron 语义不变），
    #    但状态一律记成 degraded（manifest + heartbeat + 报告三处都披露）。
    if failed_stages:
        snap.set_stage("pipeline", "degraded", failed_stages=list(failed_stages))
    else:
        snap.set_stage("pipeline", "ok")
    snap.add_note(
        f"实盘态 live_trade={live_trade}｜"
        f"取数={flags['fetch']}｜S层重算={refresh_scores}｜换库={update_qlib}｜LLM={flags['llm']}"
    )
    manifest_path = snap.finalize()
    log_stage.info("快照 manifest 已写：%s", manifest_path)
    if failed_stages and flags.get("full"):
        log_stage.error(
            "🔴 --full 全流程**未跑完**（失败阶段：%s）⇒ 退出码 1（fail-closed）",
            "、".join(failed_stages),
        )
        return 1
    return 0


def _record_pipeline_stages(snap, result: dict, *, flags: dict,
                             refresh_scores: bool) -> list[str]:
    """🔴 把**每一个阶段**的状态登记进 manifest（**绝不静默降级**）。

    🔴 2026-11-10：返回值 = **失败阶段清单**（原先返回 None ⇒ 调用方看不到
    「有没有环节没跑成」⇒ manifest 里写着 failed、进程却照样退出 0）。
    ⇒ 现在调用方据此在 ``--full`` 下**硬失败**（退出码 1）＋ 不发信。

    🔴 为什么 2026-11-09 才补：manifest 里原本只有 ``pipeline=ok`` 一条
    ⇒ 读者无法从快照本身判断「哪几步跑了 / 哪几步跳了 / 哪几步降级了」
    （实测真跑出来的 manifest：``stages=[('pipeline','ok')]``、``skipped`` 只登记了
    ``fetch_data`` 与 ``stage1_dataset`` ⇒ ``--skip-llm`` / ``--no-refresh-scores``
    **静默降级**、且各阶段状态**查不到**）。
    ⇒ 现在逐阶段登记：状态 ∈ ok / skipped / failed / reused / absent。
    """
    run_path = Path(str(result.get("run_dir") or ""))
    #: 失败阶段清单（状态 = "failed" 的阶段名）
    failed: list[str] = []

    def _has(name: str) -> bool:
        return bool(run_path) and (run_path / name).exists()

    def _stage(name: str, status: str, **detail) -> None:
        """登记阶段状态 ＋ 把 ``failed`` 收进清单（供 ``--full`` 硬失败用）。"""
        snap.set_stage(name, status, **detail)
        if status == "failed":
            failed.append(name)

    _stage("calendar_gate", "ok")
    if flags.get("fetch"):
        _stage("fetch_data", "ok")
    if refresh_scores:
        _stage("s_layer", "ok")
    # 🔴 2026-11-09（NIT-11）：原先用 `result.get("report_json")` 判状态 ⇒ 那是**路径字符串**
    #    （恒为非空）⇒ 状态**恒为 "ok"**，manifest 的「唯一权威索引」名不副实。
    #    ⇒ 改按**件是否存在**判定（与下面 recommend/ta_context 同款）。
    _stage("analyze", "ok" if _has("etf_analysis_report.json") else "failed")
    _stage("recommend", "ok" if _has("rebalance_recommendation.json") else "failed")
    _stage("ta_context", "ok" if _has("ta_context_meta.json") else "failed")
    # 🔴 LLM 阶段的状态是**它自己报的状态**（skipped / reused / absent / ok / 其它）
    ta_status = str(result.get("llm_decision_status") or "unknown")
    _stage("tradingagents", ta_status)
    # 🔴 2026-11-10：--full 强制 LLM 真跑 ⇒ ``tradingagents`` 的状态**只可能是 ok**；
    #    其它状态（skipped / absent / reused / unknown）都说明**有一个环节没跑**
    #    ⇒ 在 --full 下算失败（不许「有环节没跑成」还照样出一份执行卡）。
    if flags.get("full") and ta_status != "ok":
        if "tradingagents" not in failed:
            failed.append("tradingagents")
    _stage("final_plan", "ok" if _has("final_rebalance_plan.json") else "failed")
    node_ok = bool(result.get("node_reprice_ok", True))
    _stage(
        "node_reprice", "ok" if node_ok else "failed",
        error=str(result.get("node_reprice_error") or ""),
    )
    _stage(
        "execution_card",
        "ok" if _has(f"execution_card_{str(snap.asof).replace('-', '')}.json") else "failed",
    )
    # 🔴 ``node_reprice`` 的失败是**设计内降级**（观察态：卡退化为「只有方向、无数值」
    #    的观察卡 ＋ 报告显著披露 ＋ 实盘态直接崩）⇒ **不计入 --full 硬失败集**；
    #    其余「核心交付件缺失 / 环节没跑」都计入。
    hard_failed = [name for name in failed if name != "node_reprice"]
    return hard_failed


def _echo_report(markdown: str, log_stage) -> bool:
    """把报告正文打到 stdout（供主 agent 直接贴回窗口）。

    🔴 stdout 被关闭（ssh 断链 / 管道被 ``head`` 截断）时，``print`` 抛
    ``BrokenPipeError`` ⇒ 旧代码让它冒泡成「阶段失败」⇒ **已成功落盘的轮次
    反而多出一份「失败报告」** ⇒ 判据是「报告已落盘」，stdout 只是附加通道。

    返回 ``True`` = 已打印；``False`` = stdout 断链（已告警，**不影响本轮成败**）。
    """
    try:
        print("\n" + markdown + "\n", flush=True)
    except BrokenPipeError:
        log_stage.warning(
            "stdout 已关闭（BrokenPipeError）⇒ 报告**已落盘**，本轮仍算成功（仅未贴回窗口）"
        )
        return False
    log_stage.info("报告已打印到 stdout（主 agent 直接贴回窗口）")
    return True


def _report_path(explicit: str | None, run_dir: Path, asof: str) -> Path:
    """报告落盘路径。

    优先级：显式 ``--report-md`` > ``<RESULTS_ROOT>/reports/report_<YYYYMMDD>.md``（交付面）
    > ``<快照>/report_<YYYYMMDD>.md``。

    🔴 **快照才是真值**：交付目录那份是「给人看的最新一份」，每次跑的原件都在快照里。
    """
    stamp = asof.replace("-", "")
    if explicit:
        return Path(explicit)
    try:
        return DC.REPORT_DIR / f"report_{stamp}.md"
    except Exception:  # pragma: no cover - 兜底
        return run_dir / f"report_{stamp}.md"


def _build_draft(notify_email, asof: str, result: dict, account: dict):
    """渲染邮件草稿（仅在 ``--send-email`` 时用到；发信属实盘副作用 ⇒ 需 ``--live-trade``）。"""
    card = result.get("card") or {}
    actions = card.get("orders") or []
    cash_after = (card.get("post_trade") or {}).get("cash")
    return notify_email.render_rebalance_card(
        signal_date=asof,
        execution_date=result.get("execution_date") or asof,
        account=account,
        actions=actions,
        cash_after=cash_after,
        notes=list(result.get("notes") or []),
    )


def _finalize_failed_snapshot(state: dict | None, stage: str,
                              cause: BaseException, log) -> None:
    """🔴 2026-11-09（独立审计 P1-7）：**失败的运行也必须留下带 manifest 的快照**。

    旧口径：``snap.finalize()`` 只在**成功路径**与「非交易日」分支调用；
    ``StageError`` 路径直接 ``_handle_failure`` ⇒ 快照目录里躺着半程产物，却
    **没有** ``snapshot_manifest.json`` ⇒

    * 「快照根是唯一权威索引」失效（读者无从判断它**失败在哪个阶段**）；
    * ``probe_snapshot_lookahead.find_snapshot`` 要求 manifest + 报告同时在
      ⇒ 失败快照被跳过 ⇒ 变成**静默的孤儿目录**。

    ⇒ 现在失败时也 ``set_stage(stage, "failed")`` + ``finalize()``（封存）。
    🔴 封存本身失败**不许**盖掉原始错误（只记日志）。
    """
    snap = (state or {}).get("snapshot")
    if snap is None:
        return
    try:
        detail = f"{type(cause).__name__}: {cause}"
        snap.set_stage(stage, "failed", error=detail)
        snap.set_stage("pipeline", "failed", error=detail)
        path = snap.finalize()
        log.getChild("snapshot").error("失败快照已封存（含 manifest）：%s", path)
    except Exception as exc:  # pragma: no cover - 封存失败不许盖掉原始错误
        log.getChild("snapshot").error("失败快照封存失败（不影响原始错误）：%r", exc)


def _handle_failure(asof: str, stage: str, tb: str, exc: BaseException,
                    run_dir: Path, *, send_email: bool,
                    live_trade: bool = False) -> None:
    """失败处理：**先落失败报告**（一定要能看），邮件只在 ``--send-email`` 时发。

    🔴 发信本身失败不许再抛（否则连告警都没有）。
    """
    error_text = f"{type(exc).__name__}: {exc}"
    path = _failure_report_path(asof)
    try:
        import report

        markdown = report.render_failure_report(
            asof=asof, stage=stage, error=error_text, traceback_text=tb
        )
        report.write_report(markdown, path)
        sys.stderr.write(f"\n{markdown}\n")
        sys.stderr.write(f"（失败报告已落盘：{path}）\n")
    except Exception as rep_exc:  # pragma: no cover - 兜底
        # 🔴 **不许静默**：失败报告写不出来 ⇒ 必须**显式**报出来
        sys.stderr.write(f"🔴 失败报告也生成不出来：{rep_exc!r}\n")
        try:
            import logging as _logging

            _logging.getLogger("etf_rotation_daily").error(
                "失败报告落盘失败（%s）：%r", path, rep_exc
            )
        except Exception:  # pragma: no cover - 兜底
            pass
    if not send_email:
        sys.stderr.write("（--send-email 未开 ⇒ 不发失败告警邮件）\n")
        return
    if not live_trade:
        sys.stderr.write("（观察态 ⇒ **不发失败告警邮件**，避免假警报）\n")
        return
    try:
        import notify_email

        draft = notify_email.render_failure_alert(
            asof=asof, stage=stage, traceback_text=tb, error=error_text,
        )
        notify_email.send_email(
            draft.subject, draft.html_body, text_body=draft.text_body,
            dry_run=False, out_dir=str(run_dir),
        )
    except Exception as alert_exc:  # pragma: no cover - 兜底
        sys.stderr.write(f"🔴 失败告警邮件也发不出去：{alert_exc!r}\n")
        sys.stderr.write(f"（原异常 traceback 已在上方日志里：{tb}）\n")


def main(argv: list[str] | None = None) -> int:
    """流水线入口。返回进程退出码。"""
    args = parse_args(argv)

    # 🔴 **`--mode` 已废除** ⇒ 拒 + 迁移提示（退出码 2）
    if getattr(args, "mode", None):
        sys.stderr.write(_MODE_MIGRATION_HINT)
        return 2

    # 🔴 **`--send-only` 与 `--full` 互斥**（2026-11-09 独立审计发现）
    #    `--send-only` 整条流水线不跑 ⇒ `--full` 会被**静默忽略**
    #    （`resolve_stage_flags` 根本不被调用、无快照、无 manifest、退出码 0）
    #    ⇒ 与 `--mode` / `--dry-run --live-trade` 同款处理：直接拒，给退出码 2。
    if getattr(args, "send_only", False) and getattr(args, "full", False):
        sys.stderr.write(
            "🔴 **`--send-only` 与 `--full` 互斥**：`--send-only` 只发已持久化的草稿、"
            "**不跑流水线**（不取数/不换库/不重打分/不生成快照）⇒ 与「走全部流程」"
            "语义冲突。请二选一。\n"
        )
        return 2

    # 🔴 **观察态与实盘态互斥**（防止「以为在推演、其实在下单」）
    if args.dry_run and args.live_trade:
        sys.stderr.write(
            "🔴 **`--dry-run` 与 `--live-trade` 互斥**：观察态（不发信、不下单）"
            "与实盘态（可下单、可发信）不能同时开 ⇒ 请只留一个重跑。\n"
        )
        return 2

    # 🔴 **发信属实盘副作用** ⇒ 必须配合 `--live-trade`（避免假警报）
    if (args.send_email or args.send_only) and not args.live_trade:
        sys.stderr.write(
            "🔴 **发信属实盘副作用** ⇒ `--send-email` / `--send-only` 必须配合 `--live-trade`。\n"
            "   只想看报告 ⇒ 直接跑（缺省即观察态：落快照 + 报告，不发信、不下单）。\n"
        )
        return 2

    asof = args.asof or _today_str()
    snapshot_root = Path(args.snapshot_root) if args.snapshot_root else DC.SNAPSHOT_ROOT
    log_dir = Path(args.log_dir) if args.log_dir else DC.LOG_DIR
    log_path = log_dir / f"daily_run_{asof.replace('-', '')}.log"

    log = setup_logging(log_path)
    live_trade = bool(args.live_trade)
    log.info(
        "流水线启动 | asof=%s | live_trade=%s | full=%s | dry_run=%s",
        asof, live_trade, args.full, args.dry_run,
    )
    log.info("工程根=%s | 快照根=%s | 日志=%s", DC.REPO_ROOT, snapshot_root, log_path)
    log.info("触发方式=**按需分析**（用户窗口发起）；不部署 cron；邮件默认不发")

    t0 = time.time()
    state: dict = {}
    try:
        if args.send_only:
            with _RunLock(DC.LOCK_FILE_SEND):
                rc = _send_only(
                    args, asof, snapshot_root,
                    Path(args.run_root) if args.run_root else DC.RUN_ROOT, log,
                )
        else:
            with _RunLock(DC.LOCK_FILE):
                rc = _run_stages(args, asof, snapshot_root, log, state=state)
    except LockError as exc:
        log.warning("跳过本次执行：%s", exc)
        return 2
    except StageError as exc:
        tb = "".join(
            traceback.format_exception(type(exc.cause), exc.cause, exc.cause.__traceback__)
        )
        log.error("阶段 [%s] 失败：%r\n%s", exc.stage, exc.cause, tb)
        _finalize_failed_snapshot(state, exc.stage, exc.cause, log)
        _handle_failure(asof, exc.stage, tb, exc.cause,
                        state.get("run_dir") or snapshot_root,
                        send_email=args.send_email, live_trade=live_trade)
        return 1
    except Exception as exc:  # pragma: no cover - 兜底
        tb = traceback.format_exc()
        log.error("未捕获异常：%r\n%s", exc, tb)
        _finalize_failed_snapshot(state, "unknown", exc, log)
        _handle_failure(asof, "unknown", tb, exc,
                        state.get("run_dir") or snapshot_root,
                        send_email=args.send_email, live_trade=live_trade)
        return 1

    log.info("流水线结束 | 耗时 %.1f 秒", time.time() - t0)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
