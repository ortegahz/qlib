#!/usr/bin/env python3
"""ETF 轮动流水线**看门狗**（**独立于工程代码**，纯标准库）。

【为什么必须有它】
定时任务**不能自己报告自己没跑**。如果发生下面任何一种情况：

* `/tmp` 下的工程代码被 `systemd-tmpfiles` 清掉（`/tmp` 默认清理 10 天未访问的文件）；
* 用户的 `crontab` 被别的操作冲掉；
* 凭据文件（`/mnt/data/siping/secrets/smtp.env`）被删或权限被改宽；
* conda 环境损坏、解释器路径失效；

⇒ 流水线会**静默消失**，而你**收不到任何信**。
本看门狗住在**持久盘** `/mnt/data/siping/apps/`（不在 `/tmp` 里），
不 `import` 任何工程模块 ⇒ **工程坏了它照样能跑、照样能报警**。

【它检查什么】
| 检查项 | 判据 |
|:--|:--|
| cron 三条是否还在 | `crontab -l` 含 `etf_rotation_daily` 标记 |
| 主程序是否还在 | `main.py` 文件存在 |
| 凭据是否可用 | secrets 文件存在、权限 0600、五个键齐全 |
| 当天分析是否产出 | `heartbeat_<YYYYMMDD>.json` 存在（休市日允许 `skipped_non_trading_day`） |
| 当天信是否真发出 | `sent_<YYYYMMDD>T*.json` 回执 ≥ 1 个且 `sent=true` |

【它做什么】
任一检查失败 ⇒ 立刻发【流水线看门狗告警】邮件（主题含失败项清单）。
全部正常 ⇒ **默认静默**（不刷屏）；`--report` 强制发一份【看门狗状态】报告。

【用法】
```bash
# 15:50 查「15:30 的分析跑没跑」
python etf_watchdog.py --asof 2026-11-05 --check run

# 17:30 查「16:00 / 17:00 的信真发出没」
python etf_watchdog.py --asof 2026-11-05 --check send

# 随时手动要一份状态报告
python etf_watchdog.py --asof 2026-11-05 --check all --report
```
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import smtplib
import subprocess
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

# ---------------------------------------------------------------- 默认路径
#: 🔴 **结果落点根**：单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的
#:    `ETF_ROTATION_RESULTS_ROOT`；本看门狗**刻意不 import 工程模块**（工程坏了它照样能跑）
#:    ⇒ 按**同一个环境变量**取值，两边不会分叉。
RESULTS_ROOT = Path(
    os.environ.get(
        "ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results"
    )
)
#: 🔴 **数据集落点根**（交易日历等数据集文件；同上：不 import 工程模块）
OUTPUT_ROOT = Path(
    os.environ.get(
        "ETF_ROTATION_OUTPUT_ROOT", "/mnt/data/siping/datasets/manu/etf_rotation"
    )
)
DEFAULT_RUN_ROOT = RESULTS_ROOT / "daily"
DEFAULT_SECRETS = Path("/mnt/data/siping/secrets/smtp.env")
DEFAULT_REPO = Path("/tmp/pycharm_project_658d3fc4")
DEFAULT_CALENDAR = OUTPUT_ROOT / "datasets" / "etf" / "trade_calendar.csv"
CRON_TAG = "etf_rotation_daily"

#: secrets 文件里必须有的键
REQUIRED_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_SENDER")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="etf-watchdog",
        description="ETF 轮动流水线看门狗（独立于工程代码；出问题才发信）",
    )
    p.add_argument("--asof", default=None, help="业务日期 YYYY-MM-DD（缺省取本机当天）")
    p.add_argument("--check", choices=["run", "send", "all"], default="all",
                   help="run=查分析产物 / send=查已发回执 / all=全查")
    p.add_argument("--report", action="store_true", help="全部正常时也强制发一份状态报告")
    p.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    p.add_argument("--secrets-file", default=str(DEFAULT_SECRETS))
    p.add_argument("--repo-root", default=str(DEFAULT_REPO))
    p.add_argument("--calendar", default=str(DEFAULT_CALENDAR))
    p.add_argument("--dry-run", action="store_true", help="不联网，只打印将要发的内容")
    return p.parse_args(argv)


# ---------------------------------------------------------------- 检查项
def check_cron() -> tuple[bool, str]:
    """用户 crontab 里还有没有本任务的标记。"""
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=30
        ).stdout
    except Exception as exc:
        return False, f"crontab -l 执行失败：{type(exc).__name__}: {exc}"
    lines = [ln for ln in out.splitlines() if ln.rstrip().endswith(f"# {CRON_TAG}")]
    pipeline = [ln for ln in lines if "daily/main.py" in ln]
    watchdog = [ln for ln in lines if "etf_watchdog.py" in ln]
    problems = []
    if len(pipeline) < 3:
        problems.append(f"流水线 cron 只有 {len(pipeline)} 条（应为 3：15:30 / 16:00 / 17:00）")
    if not watchdog:
        problems.append("看门狗 cron 没了")
    if problems:
        return False, "；".join(problems)
    return True, (
        f"crontab 正常：流水线 {len(pipeline)} 条 + 看门狗 {len(watchdog)} 条"
    )


def check_code(repo_root: Path) -> tuple[bool, str]:
    """主程序还在不在。"""
    main_py = repo_root / "manu/etf_rotation/daily/main.py"
    if not main_py.exists():
        return False, f"主程序不存在：{main_py}（/tmp 被清理了？）"
    return True, f"主程序在：{main_py}"


def check_secrets(path: Path) -> tuple[bool, str]:
    """凭据文件存在、权限 0600、五个键齐全。"""
    if not path.exists():
        return False, f"凭据文件不存在：{path}"
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return False, f"凭据文件权限过宽（0o{mode:04o}，要求 0600）⇒ 流水线会拒绝读取"
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.partition("=")[0].strip())
    missing = [k for k in REQUIRED_KEYS if k not in keys]
    if missing:
        return False, f"凭据文件缺键：{missing}"
    return True, f"凭据可用（0600，{len(keys)} 个键齐全）"


def is_trading_day(day: str, calendar_path: Path) -> bool | None:
    """今天是不是交易日。

    **读不到日历 ⇒ 返回 None（不知道）** ⇒ 调用方**不许**据此判故障
    （日历缺失属于另一种问题，日历门本身 fail-closed 会报出来）。
    """
    if not calendar_path.exists():
        return None
    try:
        with calendar_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            dates = set()
            for row in reader:
                if not row:
                    continue
                cell = str(row[0]).strip()[:10]
                if len(cell) == 10 and cell[4] == "-":
                    dates.add(cell)
        return day in dates
    except Exception:
        return None


def check_heartbeat(run_root: Path, day: str, *, required: bool) -> tuple[bool, str]:
    """当天分析有没有留下心跳。"""
    path = run_root / day.replace("-", "") / f"heartbeat_{day.replace('-', '')}.json"
    if not path.exists():
        if not required:
            return True, "非交易日 / 未要求 ⇒ 跳过心跳检查"
        return False, f"没有心跳文件：{path} ⇒ 15:30 的分析**没跑成功**"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"心跳文件读不了（{type(exc).__name__}: {exc}）：{path}"
    status = str(payload.get("status") or "")
    if status == "skipped_non_trading_day":
        return True, "心跳显示今天休市（正常跳过）"
    if status != "ok":
        return False, f"心跳状态异常：status={status!r}"
    return True, f"心跳正常（status=ok，{payload.get('updated_at')}）"


def check_receipts(run_root: Path, day: str, *, required: bool) -> tuple[bool, str]:
    """当天有没有真的发出去的信（回执）。"""
    run_dir = run_root / day.replace("-", "")
    if not run_dir.exists():
        if not required:
            return True, "未要求 ⇒ 跳过回执检查"
        return False, f"产物目录不存在：{run_dir}"
    receipts = sorted(run_dir.glob(f"sent_{day.replace('-', '')}T*.json"))
    if not receipts:
        if not required:
            return True, "未要求 ⇒ 跳过回执检查"
        return False, f"没有已发回执（sent_*.json）⇒ 16:00 / 17:00 的信**没发出去**"
    sent_ok = []
    for path in receipts:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("sent"):
            sent_ok.append(path.name)
    if not sent_ok:
        return False, f"回执都在但 sent≠true（{len(receipts)} 份）⇒ 发信实际失败"
    return True, f"已发回执 {len(sent_ok)} 份（最新 {sent_ok[-1]}）"


# ---------------------------------------------------------------- 发信
def load_secrets(path: Path) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    values: dict[str, str] = {}
    if not path.exists():
        return {}, [f"凭据文件不存在：{path}"]
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return {}, [f"凭据文件权限过宽（0o{mode:04o}）⇒ 拒绝读取"]
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values, warnings


def send_mail(
    subject: str, body: str, secrets_path: Path, *, dry_run: bool = False
) -> tuple[bool, str]:
    """发一封邮件。返回 `(成功, 说明)`；**绝不**把授权码写进返回值。"""
    secrets, warnings = load_secrets(secrets_path)
    if warnings:
        return False, "；".join(warnings)
    host = secrets.get("SMTP_HOST") or "smtp.126.com"
    port = int(secrets.get("SMTP_PORT") or 465)
    user = secrets.get("SMTP_USER") or ""
    password = secrets.get("SMTP_PASSWORD") or ""
    sender = secrets.get("SMTP_SENDER") or user
    if not user or not password:
        return False, "凭据不完整（缺 SMTP_USER / SMTP_PASSWORD）"
    to = user
    if dry_run:
        return True, f"dry-run：未发送（收件人 {to}）"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("ETF 看门狗", "utf-8")), sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    raw = msg.as_string()

    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
        server.login(user, password)
        server.sendmail(sender, [to], raw)
    except Exception as exc:
        return False, f"发信失败：{type(exc).__name__}: {exc}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return True, f"已发给 {to}"


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    day = args.asof or datetime.now().astimezone().strftime("%Y-%m-%d")
    run_root = Path(args.run_root)
    secrets_path = Path(args.secrets_file)
    repo_root = Path(args.repo_root)

    trading = is_trading_day(day, Path(args.calendar))
    is_trading = bool(trading)

    checks: list[tuple[str, bool, str]] = []
    ok, msg = check_cron()
    checks.append(("cron 任务", ok, msg))
    ok, msg = check_code(repo_root)
    checks.append(("工程代码", ok, msg))
    ok, msg = check_secrets(secrets_path)
    checks.append(("SMTP 凭据", ok, msg))

    if args.check in ("run", "all"):
        ok, msg = check_heartbeat(run_root, day, required=is_trading)
        checks.append(("15:30 分析产物", ok, msg))
    if args.check in ("send", "all"):
        ok, msg = check_receipts(run_root, day, required=is_trading)
        checks.append(("已发回执", ok, msg))

    failed = [(name, msg) for name, ok, msg in checks if not ok]
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"检查时间：{stamp}", f"业务日期：{day}（交易日：{trading}）", ""]
    for name, ok, msg in checks:
        lines.append(f"[{'✅' if ok else '🔴'}] {name}：{msg}")
    lines.append("")

    if failed:
        subject = f"【流水线看门狗告警】{day} ETF 轮动流水线异常（{len(failed)} 项）"
        lines.insert(3, f"🔴 **异常项**：{'、'.join(n for n, _ in failed)}")
        lines.append("")
        lines.append("请尽快检查服务器上的定时任务与凭据（看门狗本身在持久盘，不依赖工程代码）。")
        ok, reason = send_mail(subject, "\n".join(lines), secrets_path, dry_run=args.dry_run)
        print(json.dumps({"failed": [n for n, _ in failed], "sent": ok, "reason": reason},
                         ensure_ascii=False))
        return 0 if ok else 1

    subject = f"【看门狗状态】{day} ETF 轮动流水线全部正常（{len(checks)} 项）"
    lines.append("全部检查项通过。")
    if not args.report:
        print(json.dumps({"failed": [], "sent": False,
                          "reason": "全部正常 ⇒ 默认静默（要强制报告加 --report）"},
                         ensure_ascii=False))
        return 0
    ok, reason = send_mail(subject, "\n".join(lines), secrets_path, dry_run=args.dry_run)
    print(json.dumps({"failed": [], "sent": ok, "reason": reason}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
