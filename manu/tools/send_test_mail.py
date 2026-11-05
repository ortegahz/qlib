#!/usr/bin/env python3
"""发测试邮件 / 任意单封邮件（SMTP，纯标准库）。

【为什么是独立脚本】
`manu/etf_rotation/daily/notify_email.py` 是**流水线内部**的发信模块，依赖项目包结构；
本脚本**零项目依赖**（只用 `smtplib` + `email.mime`）⇒ 任何仓库、任何机器都能直接跑，
用来做「通道自检」与「人工发一封」。

【凭据来源优先级】
1. 命令行显式 `--host/--port/--user/--password/--sender`（**慎用**：会进 `ps` 与 shell history）
2. 环境变量 `SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_SENDER`
3. `--secrets-file` 指向的 env 文件（键名同上；**权限必须 0600**）

🔴 **授权码 ≠ 网页登录密码**：网易系邮箱 SMTP 只认「客户端授权码」，
   用登录密码会得到 `535 authentication failed`。

【用法】
```bash
python manu/tools/send_test_mail.py \
  --secrets-file /mnt/data/siping/secrets/smtp.env \
  --to zxthz@126.com \
  --subject "[ETF量化巡检] SMTP 连通性测试" \
  --text "授权码已生效。"

# 只渲染不发送（落 .eml）
python manu/tools/send_test_mail.py --secrets-file ... --to ... --subject ... --dry-run --out-dir /tmp
```

【输出】
JSON：`{"sent","dry_run","subject","to","path","reason"}`。**绝不**打印密码或授权码。
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

#: secrets env 文件里允许出现的键（白名单，防注入任意环境变量）
ALLOWED_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_SENDER")

#: 默认落点（服务器持久盘）
DEFAULT_SECRETS = Path("/mnt/data/siping/secrets/smtp.env")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="send_test_mail",
        description="发一封 SMTP 邮件（测试 / 人工通知）。纯标准库，零项目依赖。",
    )
    p.add_argument("--to", required=True, help="收件人（逗号分隔可多个）")
    p.add_argument("--subject", required=True, help="邮件主题")
    p.add_argument("--text", default=None, help="纯文本正文")
    p.add_argument("--html", default=None, help="HTML 正文（给了则发 HTML；否则发纯文本）")
    p.add_argument("--secrets-file", default=str(DEFAULT_SECRETS),
                   help=f"凭据 env 文件（默认 {DEFAULT_SECRETS}）")
    p.add_argument("--host", default=None, help="SMTP 主机（覆盖凭据文件）")
    p.add_argument("--port", type=int, default=None, help="SMTP 端口（覆盖凭据文件）")
    p.add_argument("--user", default=None, help="SMTP 账号（覆盖凭据文件）")
    p.add_argument("--password", default=None, help="SMTP 授权码（覆盖凭据文件；慎用）")
    p.add_argument("--sender", default=None, help="发件人（默认与 --user 相同）")
    p.add_argument("--timeout", type=float, default=20.0, help="连接/IO 超时秒（默认 20）")
    p.add_argument("--dry-run", action="store_true", help="不联网，只把完整邮件写成 .eml")
    p.add_argument("--out-dir", default=None, help="--dry-run 时 .eml 的落盘目录")
    p.add_argument("--allow-insecure-perms", action="store_true",
                   help="允许读取权限过宽的凭据文件（默认拒绝）")
    return p.parse_args(argv)


def load_secrets(path: str | Path, *, allow_insecure: bool = False) -> tuple[dict, list[str]]:
    """读凭据 env 文件 ⇒ `(键值, 警告)`。

    🔴 权限过宽（`mode & 0o077 != 0`）⇒ **默认拒绝**：明文躺在别人能读的文件里
    等于没有这道门。
    """
    warnings: list[str] = []
    p = Path(path)
    if not p.exists():
        return {}, [f"凭据文件不存在：{p}"]
    try:
        mode = p.stat().st_mode & 0o777
    except OSError as exc:
        return {}, [f"读不了凭据文件权限：{exc}"]
    if mode & 0o077 and not allow_insecure:
        raise PermissionError(
            f"凭据文件权限过宽（0o{mode:04o}，要求 0600/0400）⇒ 已拒绝读取：{p}；"
            f"先 `chmod 600 {p}`，或显式加 --allow-insecure-perms"
        )
    if mode & 0o077:
        warnings.append(f"⚠️ 凭据文件权限过宽（0o{mode:04o}），已按 --allow-insecure-perms 放行")

    values: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in ALLOWED_KEYS:
            warnings.append(f"凭据文件里的键不在白名单内（已忽略）：{key}")
            continue
        values[key] = value
    return values, warnings


def resolve_config(args: argparse.Namespace) -> tuple[dict, list[str]]:
    """按「命令行 > 环境变量 > 凭据文件」组装配置。"""
    warnings: list[str] = []
    secrets: dict[str, str] = {}
    if args.secrets_file:
        try:
            secrets, warnings = load_secrets(
                args.secrets_file, allow_insecure=args.allow_insecure_perms
            )
        except PermissionError as exc:
            raise PermissionError(str(exc)) from exc

    def pick(*names: str) -> str | None:
        for name in names:
            value = getattr(args, name.lower().replace("smtp_", ""), None)
            if value:
                return str(value)
            value = os.environ.get(name)
            if value:
                return value
            if name in secrets:
                return secrets[name]
        return None

    host = pick("SMTP_HOST")
    port_raw = pick("SMTP_PORT")
    user = pick("SMTP_USER")
    password = pick("SMTP_PASSWORD")
    sender = pick("SMTP_SENDER") or user

    port: int | None = None
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            warnings.append(f"SMTP_PORT 不是整数（已忽略）：{port_raw}")

    cfg = {
        "host": host or "smtp.126.com",
        "port": port or (465 if (host or "smtp.126.com").endswith("126.com") else 25),
        "user": user,
        "password": password,
        "sender": sender,
        "timeout": float(args.timeout),
    }
    missing = [k for k in ("user", "password") if not cfg[k]]
    if missing:
        raise ValueError(
            f"缺少 SMTP 配置：{missing}；请检查凭据文件 {args.secrets_file} 或环境变量"
        )
    return cfg, warnings


def build_message(subject: str, body: str, *, is_html: bool, sender: str, to: list[str]) -> str:
    """拼一封邮件（返回 RFC822 文本）。"""
    if is_html:
        msg = MIMEText(body, "html", "utf-8")
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("ETF 量化巡检", "utf-8")), sender))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    return msg.as_string()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg, warnings = resolve_config(args)
    except (PermissionError, ValueError) as exc:
        print(json.dumps({"sent": False, "dry_run": False, "reason": f"配置错误：{exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    for note in warnings:
        print(f"[warn] {note}", file=sys.stderr)

    recipients = [x.strip() for x in args.to.split(",") if x.strip()]
    is_html = bool(args.html)
    body = args.html if is_html else (args.text or "")
    raw = build_message(args.subject, body, is_html=is_html,
                        sender=cfg["sender"], to=recipients)

    if args.dry_run:
        out_dir = Path(args.out_dir) if args.out_dir else Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.subject)[:60]
        path = out_dir / f"{stamp}_{safe}.eml"
        path.write_text(raw, encoding="utf-8")
        print(json.dumps({"sent": False, "dry_run": True, "subject": args.subject,
                          "to": recipients, "path": str(path), "reason": "dry-run：未发送"},
                         ensure_ascii=False))
        return 0

    use_ssl = int(cfg["port"]) == 465
    server = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), timeout=cfg["timeout"])
        else:
            server = smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=cfg["timeout"])
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                print("[warn] SMTP 服务端不支持 STARTTLS ⇒ 按明文继续", file=sys.stderr)
        server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["sender"], recipients, raw)
    except smtplib.SMTPAuthenticationError as exc:
        print(json.dumps({
            "sent": False, "dry_run": False, "reason":
            f"SMTP 认证失败（{cfg['user']}）：{type(exc).__name__}: {exc} —— "
            f"126 需要「客户端授权码」而不是网页登录密码",
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"sent": False, "dry_run": False,
                          "reason": f"发信失败：{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

    print(json.dumps({"sent": True, "dry_run": False, "subject": args.subject,
                      "to": recipients, "path": None, "reason": "已发送"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
