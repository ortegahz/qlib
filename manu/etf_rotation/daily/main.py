"""main.py —— **ETF 轮动分析流水线**（**按需触发**：窗口喊分析 → 跑 → 报告回窗口）。

【交付形态（2026-11-05 用户改口径）】
原来的形态是「服务器 cron 每个交易日 15:30 自动跑 + 16:00/17:00 发邮件」。
用户已明确改为**按需分析**：
* **不部署任何定时任务**（服务器 crontab 里那 5 条已全部卸载）；
* **默认不发邮件**（邮件保留为 `--send-email` 显式 opt-in）；
* 每次分析产出**一份 Markdown 报告**：落 `manu/report/report_<YYYYMMDD>_mode<A|B>.md`
  **并直接打印到 stdout** ⇒ 主 agent 贴回窗口即可。

【六阶段全流程（月度调仓日 = 模式 B）】
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
**模式 A（日常巡检）**只做阶段 1 的 ETF 部分 + 512800 的 MA20/MA60 检查：
它是**构造性保证**（路径内根本没有打分 / 选股 / LLM 调用）⇒ 不可能产出新仓位指令。

【用法】
```bash
# 服务器工作副本 /tmp/pycharm_project_658d3fc4
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05 --mode B
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-12-01 --mode B --dry-run
```
退出码：0 = 成功（含「非交易日静默退出」）；1 = 失败；2 = 用法/环境错误。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: 日志格式：`时间 | 级别 | 阶段 | 消息`
_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)-14s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


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
        description="ETF 轮动分析流水线（按需触发；月度调仓日走六阶段全流程，报告回窗口）",
    )
    p.add_argument("--asof", default=None,
                   help="业务日期 YYYY-MM-DD（缺省取本机当天）")
    p.add_argument("--mode", choices=["A", "B", "auto"], default="auto",
                   help="运行模式：A=日常巡检 / B=调仓信号日六阶段全流程 / auto=按日期自动分流")
    p.add_argument("--report-md", default=None,
                   help="报告落盘路径（缺省 = manu/report/report_<YYYYMMDD>_mode<A|B>.md）")
    p.add_argument("--send-email", action="store_true",
                   help="🔴 显式开启邮件推送（**默认不发**；按需分析时报告直接回窗口）")
    p.add_argument("--no-email", action="store_true",
                   help="兼容保留（默认行为就是不发信；此参数是空操作）")
    p.add_argument("--dry-run", action="store_true",
                   help="干跑：不联网取数 + 不写产物 + 只做前置判定")
    p.add_argument("--skip-fetch", action="store_true", help="跳过取数阶段（用现有数据）")
    p.add_argument("--skip-llm", action="store_true", help="模式 B 跳过 TradingAgents（无 API 配额时用）")
    p.add_argument("--send-only", action="store_true",
                   help="🔴 只发已持久化的邮件草稿（不重跑分析）—— 按需补发时用")
    p.add_argument("--wait-seconds", type=float, default=1800.0,
                   help="--send-only 时等待草稿出现的秒数（默认 1800 = 30 分钟）")
    p.add_argument("--refresh-scores", action=argparse.BooleanOptionalAction, default=None,
                   help="是否重跑 S 层（重活，约 27 分钟；缺省：模式 B 开 / 模式 A 关）")
    p.add_argument("--update-qlib-bin", action=argparse.BooleanOptionalAction, default=None,
                   help="是否刷新 qlib 数据集（滚动快照换库；缺省：模式 B 开 / 模式 A 关）")
    p.add_argument("--llm-timeout", type=float, default=None,
                   help="阶段 4（TradingAgents 辩论）的超时上限（秒）；"
                        "缺省 = daily_config.LLM_TIMEOUT（默认 5400，可由 ETF_ROTATION_LLM_TIMEOUT 覆盖）")
    p.add_argument("--reasoning-effort", default="", choices=["max", "high", "medium", "low"],
                   help="阶段 4 的 reasoning_effort 覆盖（缺省空串 = **不覆盖**，"
                        "沿用 TradingAgents/.env 里现役的 max）；"
                        "实测 MiniMax 单次调用：max 185s / high 116s / medium 91s")
    p.add_argument("--run-root", default=None, help="产物目录（缺省用 daily_config.RUN_ROOT）")
    p.add_argument("--log-dir", default=None, help="日志目录（缺省用 daily_config.LOG_DIR）")
    p.add_argument("--calendar", default=None, help="A 股交易日历 CSV（缺省用 akshare 缓存）")
    return p.parse_args(argv)


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
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _persist_draft(draft, run_dir: Path, asof: str, kind: str, to: list[str]) -> Path:
    """把渲染好的邮件草稿持久化 ⇒ ``<run_dir>/email_draft_<YYYYMMDD>.json``。

    🔴 保留这条链路是为了「按需补发邮件」：先跑分析（报告回窗口），
    之后若想发一封留档，`--send-only` 只读这个草稿、**不重跑分析**。
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
    run_dir: Path, asof: str, *, status: str, mode: str, detail: dict | None = None
) -> Path:
    """写**心跳** ⇒ ``<run_dir>/heartbeat_<YYYYMMDD>.json``（本轮跑过了的证据）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"heartbeat_{asof.replace('-', '')}.json"
    payload = {
        "asof": asof,
        "status": status,
        "mode": mode,
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


def _send_only(args, asof: str, run_root: Path, log) -> int:
    """🔴 **按需补发邮件**分支：只发已持久化的邮件草稿，**不重跑任何计算**。

    行为：草稿已存在 ⇒ 立即发送（退出 0）；还没出来 ⇒ 轮询等待至多
    ``--wait-seconds`` 秒；等不到 ⇒ 写日志并退出 0（**不重复发失败告警**）。
    """
    import mode_router
    import notify_email

    draft_file = mode_router.draft_path(asof, run_root)
    deadline = time.time() + max(0.0, float(args.wait_seconds))
    waited = 0.0
    while True:
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
        dry_run=args.dry_run,
        out_dir=str(draft_file.parent),
    )
    log.getChild("notify").info(
        "延迟发信完成：sent=%s dry_run=%s 主题=%s",
        send_info.get("sent"), send_info.get("dry_run"), payload.get("subject"),
    )
    receipt = _write_receipt(
        draft_file.parent, asof,
        subject=str(payload.get("subject") or ""),
        recipients=list(payload.get("to") or []),
        sent=bool(send_info.get("sent")),
    )
    log.getChild("notify").info("已发回执：%s", receipt)
    return 0


def main(argv: list[str] | None = None) -> int:
    """流水线入口。返回进程退出码。"""
    args = parse_args(argv)
    asof = args.asof or _today_str()
    run_root = Path(args.run_root) if args.run_root else DC.RUN_ROOT
    log_dir = Path(args.log_dir) if args.log_dir else DC.LOG_DIR
    log_path = log_dir / f"daily_run_{asof.replace('-', '')}.log"

    log = setup_logging(log_path)
    log.info("流水线启动 | asof=%s | mode=%s | dry_run=%s", asof, args.mode, args.dry_run)
    log.info("工程根=%s | 产物根=%s | 日志=%s", DC.REPO_ROOT, run_root, log_path)
    log.info("触发方式=**按需分析**（用户窗口发起）；不部署 cron；邮件默认不发")

    t0 = time.time()
    try:
        if args.send_only:
            import mode_router

            with _RunLock(DC.LOCK_FILE_SEND):
                rc = _send_only(args, asof, run_root, log)
        else:
            with _RunLock(DC.LOCK_FILE):
                rc = _run_stages(args, asof, run_root, log)
    except LockError as exc:
        log.warning("跳过本次执行：%s", exc)
        return 2
    except StageError as exc:
        tb = "".join(
            traceback.format_exception(type(exc.cause), exc.cause, exc.cause.__traceback__)
        )
        log.error("阶段 [%s] 失败：%r\n%s", exc.stage, exc.cause, tb)
        _handle_failure(asof, exc.stage, tb, exc.cause, run_root,
                        dry_run=args.dry_run, send_email=args.send_email,
                        mode=_failed_mode(args))
        return 1
    except Exception as exc:  # pragma: no cover - 兜底
        tb = traceback.format_exc()
        log.error("未捕获异常：%r\n%s", exc, tb)
        _handle_failure(asof, "unknown", tb, exc, run_root,
                        dry_run=args.dry_run, send_email=args.send_email,
                        mode=_failed_mode(args))
        return 1

    log.info("流水线结束 | 耗时 %.1f 秒", time.time() - t0)
    return rc


def _failed_mode(args: argparse.Namespace) -> str:
    """失败时用来命名报告的模式（未知就写 ``?``，**不编**）。"""
    mode = getattr(args, "mode", None)
    return mode if mode in ("A", "B") else "?"


def _run_stages(args: argparse.Namespace, asof: str, run_root: Path, log) -> int:
    """六个阶段的实际执行。"""
    import account_state
    import agents_rebalance
    import calendar_gate
    import fetch_data
    import mode_router
    import notify_email
    import report

    # ---- 阶段 0：交易日历门（fail-closed）--------------------------------
    log_stage = log.getChild("calendar_gate")
    try:
        is_trading = calendar_gate.is_trading_day(asof, args.calendar)
    except Exception as exc:
        raise StageError("calendar_gate", exc) from exc
    if not is_trading:
        log_stage.info("今天不是 A 股交易日（%s）⇒ 静默退出，不产出报告", asof)
        _write_heartbeat(
            mode_router.build_run_dir(asof, run_root), asof,
            status="skipped_non_trading_day", mode="none",
        )
        return 0
    log_stage.info("交易日校验通过：%s 是 A 股交易日", asof)

    # ---- 阶段 0.5：模式分流 ----------------------------------------------
    log_stage = log.getChild("mode_router")
    route = mode_router.route(asof, args.calendar)
    route_mode = route["mode"]
    mode = args.mode if args.mode in ("A", "B") else route_mode
    # 🔴 **显式指定模式时必须说清楚这是「覆盖路由判定」**：
    #    否则日志会出现「模式分流：B（…… ⇒ 模式 A：只巡检……）」这种自相矛盾的行，
    #    而报告读者会误以为这是一次真实的调仓信号日全流程。
    mode_override = mode != route_mode
    if mode_override:
        log_stage.info(
            "模式分流：%s（**显式 --mode %s 覆盖路由判定**；路由原判 %s：%s）",
            mode, mode, route_mode, route.get("reason", ""),
        )
    else:
        log_stage.info("模式分流：%s（%s）", mode, route.get("reason", ""))
    run_dir = mode_router.build_run_dir(asof, run_root)
    log_stage.info("本次产物目录：%s", run_dir)

    # 🔴 重活开关：**缺省随模式**（模式 B = 六阶段全流程 ⇒ S 层重算 + 数据集刷新都开；
    #    模式 A = 轻量巡检 ⇒ 都关）。显式 --refresh-scores / --no-refresh-scores 可覆盖。
    refresh_scores = (
        args.refresh_scores if args.refresh_scores is not None else (mode == "B")
    )
    update_qlib = (
        args.update_qlib_bin if args.update_qlib_bin is not None else (mode == "B")
    )
    log_stage.info(
        "重活开关：refresh_scores=%s（阶段2 S层重算）| update_qlib=%s（阶段1 qlib 数据集换库）",
        refresh_scores, update_qlib,
    )
    # 🔴 阶段 4 的超时与推理强度：缺省读 `daily_config.LLM_TIMEOUT`（默认 5400 秒）
    #    （2026-11-05 实测：effort=max 时 MiniMax 单次调用 185 秒 ⇒ 1800 秒必超时）
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
    fetch_info: dict = {}
    if args.skip_fetch or args.dry_run:
        log.getChild("fetch").info(
            "跳过取数（skip_fetch=%s / dry_run=%s）", args.skip_fetch, args.dry_run
        )
    else:
        try:
            fetch_info = fetch_data.run_fetch(
                asof=asof,
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

    # ---- 阶段 1（个股侧）：qlib 数据集滚动快照换库 --------------------------
    dataset_info: dict = {}
    dataset_error: str | None = None
    if update_qlib and not args.dry_run:
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
            )
        except Exception as exc:
            # 🔴 **不整轮失败**：数据集刷新失败 ⇒ 降级用**旧数据集**继续跑，
            #    但必须在报告里**显著披露**（否则 S 层会静默地只用旧分数）。
            dataset_error = f"{type(exc).__name__}: {exc}"
            log.getChild("stage1").error(
                "qlib 数据集刷新失败（降级用旧数据集继续）：%s", dataset_error
            )
            log.getChild("stage1").error("原始报错：%s", exc)
        else:
            for note in dataset_info.get("notes") or []:
                log.getChild("stage1").info("%s", note)
            log.getChild("stage1").info(
                "qlib 数据集：%s → %s（新增 %s 个交易日）",
                dataset_info.get("calendar_max_before"),
                dataset_info.get("calendar_max_after"),
                len(dataset_info.get("new_days") or []),
            )
    elif update_qlib:
        log.getChild("stage1").info("dry-run ⇒ 跳过 qlib 数据集刷新")
    else:
        log.getChild("stage1").info("update_qlib=False ⇒ 本轮**不刷新** qlib 数据集")

    # ---- 阶段 1.5：合并行情（canonical + 当日增量） ------------------------
    try:
        prices_df = fetch_data.load_effective_prices(asof=asof, run_root=run_root)
    except Exception as exc:
        raise StageError("load_effective_prices", exc) from exc
    prices_path = Path(DC.ETF_PRICES_CSV)
    if not args.dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)
        prices_path = run_dir / f"etf_prices_effective_{asof.replace('-', '')}.csv"
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
        if mode == "B":
            result = agents_rebalance.run_mode_b(
                asof=asof,
                run_dir=run_dir,
                prices_csv=str(prices_path),
                dry_run=args.dry_run,
                skip_llm=args.skip_llm,
                llm_timeout=llm_timeout,
                reasoning_effort=reasoning_effort,
                refresh_scores=refresh_scores,
            )
        else:
            result = agents_rebalance.run_mode_a(
                asof=asof,
                prices_df=prices_df,
                account=account,
                run_dir=run_dir,
                dry_run=args.dry_run,
            )
    except Exception as exc:
        raise StageError(f"rebalance[{mode}]", exc) from exc

    for note in result.get("notes", []) or []:
        log_stage.info("%s", note)
    log_stage.info("模式 %s 计算完成：status=%s", mode, result.get("status", result.get("mode")))

    # ---- 阶段 6：报告（落盘 + stdout）→ 窗口 ------------------------------
    log_stage = log.getChild("report")
    result = dict(result)
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
    route_for_report = dict(route)
    route_for_report["mode_override"] = bool(mode_override)
    route_for_report["effective_mode"] = mode
    try:
        markdown = report.render_report(
            result, account=account, route=route_for_report, mode=mode, asof=asof
        )
    except Exception as exc:
        raise StageError("report", exc) from exc

    report_path = _report_path(args.report_md, run_dir, asof, mode)
    try:
        report.write_report(markdown, report_path)
    except Exception as exc:
        raise StageError("report_write", exc) from exc
    log_stage.info("报告已落盘：%s", report_path)
    print("\n" + markdown + "\n", flush=True)
    log_stage.info("报告已打印到 stdout（主 agent 直接贴回窗口）")

    # ---- 可选：邮件（默认不发） -------------------------------------------
    if args.send_email:
        log_stage = log.getChild("notify")
        try:
            draft = _build_draft(notify_email, mode, asof, result, account, route)
            if not args.dry_run:
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
                dry_run=args.dry_run,
                out_dir=str(run_dir),
            )
            log_stage.info(
                "邮件已处理：sent=%s dry_run=%s path=%s",
                send_info.get("sent"), send_info.get("dry_run"), send_info.get("path"),
            )
        except Exception as exc:
            raise StageError("notify", exc) from exc
    else:
        log.getChild("notify").info(
            "邮件**未发送**（--send-email 未开）⇒ 本轮交付物 = Markdown 报告"
        )

    _write_heartbeat(
        run_dir, asof, status="ok", mode=mode,
        detail={
            "report_md": str(report_path),
            "email_sent": bool(args.send_email),
            "refresh_scores": refresh_scores,
            "update_qlib": update_qlib,
        },
    )
    return 0


def _report_path(explicit: str | None, run_dir: Path, asof: str, mode: str = "A") -> Path:
    """报告落盘路径。

    🔴 **文件名带模式** ⇒ 同一天用不同模式跑两次**不会互相覆盖**，
    事后回看能一眼分清「这份是模式 A 的巡检」还是「那份是模式 B 的六阶段全流程」。

    优先级：显式 ``--report-md`` > ``manu/report/report_<YYYYMMDD>_mode<A|B>.md``
    > ``<run_dir>/report_<YYYYMMDD>_mode<A|B>.md``。
    """
    if explicit:
        return Path(explicit)
    stamp = asof.replace("-", "")
    name = f"report_{stamp}_mode{mode}"
    try:
        return DC.REPORT_DIR / f"{name}.md"
    except Exception:  # pragma: no cover - 兜底
        return run_dir / f"{name}.md"


def _build_draft(notify_email, mode: str, asof: str, result: dict, account: dict, route: dict):
    """按模式选模板并渲染邮件草稿（仅在 ``--send-email`` 时用到）。"""
    if mode == "B":
        card = result.get("card") or {}
        actions = card.get("orders") or []
        cash_after = (card.get("post_trade") or {}).get("cash")
        return notify_email.render_rebalance_card(
            signal_date=asof,
            execution_date=result.get("execution_date") or route.get("execution_date") or asof,
            account=account,
            actions=actions,
            cash_after=cash_after,
            notes=list(result.get("notes") or []),
        )

    status = result.get("status")
    ma_info = result.get("ma") or {}
    if status == "broken":
        holding = result.get("holding") or {}
        return notify_email.render_emergency_break(
            asof=asof,
            account=account,
            ma_info=ma_info,
            shares=int(holding.get("shares") or 0),
        )
    return notify_email.render_daily_ok(
        asof=asof,
        account=account,
        ma_info=ma_info,
        extra_notes=list(result.get("notes") or []),
    )


def _handle_failure(asof: str, stage: str, tb: str, exc: BaseException,
                    run_root: Path, *, dry_run: bool, send_email: bool,
                    mode: str = "?") -> None:
    """失败处理：**先落失败报告**（一定要能看），邮件只在 ``--send-email`` 时发。

    🔴 发信本身失败不许再抛（否则连告警都没有）。
    """
    error_text = f"{type(exc).__name__}: {exc}"
    try:
        import report

        markdown = report.render_failure_report(
            asof=asof, stage=stage, error=error_text, traceback_text=tb
        )
        path = DC.REPORT_DIR / f"report_{asof.replace('-', '')}_mode{mode}_failed.md"
        report.write_report(markdown, path)
        sys.stderr.write(f"\n{markdown}\n")
        sys.stderr.write(f"（失败报告已落盘：{path}）\n")
    except Exception as rep_exc:  # pragma: no cover - 兜底
        sys.stderr.write(f"🔴 失败报告也生成不出来：{rep_exc!r}\n")

    if not send_email:
        sys.stderr.write("（--send-email 未开 ⇒ 不发失败告警邮件）\n")
        return
    try:
        import notify_email

        draft = notify_email.render_failure_alert(
            asof=asof, stage=stage, traceback_text=tb, error=error_text,
        )
        out_dir = run_root / asof.replace("-", "")
        notify_email.send_email(
            draft.subject, draft.html_body, text_body=draft.text_body,
            dry_run=dry_run, out_dir=str(out_dir),
        )
    except Exception as alert_exc:  # pragma: no cover - 兜底
        sys.stderr.write(f"🔴 失败告警邮件也发不出去：{alert_exc!r}\n")
        sys.stderr.write(f"（原异常 traceback 已在上方日志里：{tb}）\n")


if __name__ == "__main__":
    raise SystemExit(main())
