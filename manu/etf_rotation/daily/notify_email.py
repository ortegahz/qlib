r"""notify_email.py —— 每日流水线的**邮件推送**（`smtplib` + `email.mime`，零第三方依赖）。

【为什么用标准库】
本工程里邮件是**唯一**的对外出口（服务器没有 UI 界面），而标准库 `smtplib` + `email.mime`
已经覆盖全部需求 ⇒ 不引第三方依赖 ⇒ 服务器上不用再装包、也不用担心版本冲突。

【职责切分：渲染 ≠ 发送】
- `render_*`：**纯函数**，把数据渲染成 `EmailDraft`（不联网、不读行情、不写文件）⇒ 可单测；
- `send_email`：只负责把正文送出去（不关心业务语义）⇒ 可 dry-run 复盘。
两者都**不 import 业务模块**（只有失败告警里惰性取一次账户摘要，取不到就写「未知」）。

【126 邮箱的硬要求】
126 的 SMTP **必须**用「**客户端授权码**」登录（不是网页登录密码），且：
- 端口 **465** ⇒ 隐式 SSL（`smtplib.SMTP_SSL`）；端口 587 ⇒ STARTTLS；
- 发件人（MAIL FROM）**只能是已认证的那个账号**（实测：不带认证时
  `MAIL FROM:<zxthz@126.com>` 会被拒 `553 authentication is required`，
  换成别的域名则是 `553 Local user only`）⇒ **没有授权码就一定发不出去**；
- 授权码**只存在**服务器本地的 0600 env 文件里（`DC.SMTP_SECRETS_FILE`），
  **不进仓库、不进日志、不进异常消息**。

【四套模板（主题逐字固定 —— 下游脚本/人工按主题串识别邮件）】
| 场景 | 主题 |
|:--|:--|
| 日常巡检正常 | `[ETF量化巡检] YYYY-MM-DD 正常 \| 银行持仓均线完好` |
| 紧急破位 | `【紧急风控预警】YYYY-MM-DD 512800 跌破 MA60 触发止损` |
| 月度调仓 | `【月度调仓决策卡】2026-12-01 信号日执行方案` |
| 流水线失败 | `【流水线执行失败告警】YYYY-MM-DD <阶段> 阶段异常` |

【🔴 数字口径（两条硬规矩）】
1. **缺数据一律显示「—」，绝不编数字**（风控邮件里的假数字比空值危险得多）。
   ⇒ 缺 `ma_info` 的键时**不许**用别的键反推（例如不许拿 `close/ma60-1` 反推安全余量比例）。
2. **金额不加千分位逗号**（`30035.57` 而不是 `30,035.57`）⇒ 与账户快照 CSV /
   `account_state` 的口径逐字一致，便于对账与机器 grep。

【dry-run】
`send_email(..., dry_run=True)` **绝不联网**，把完整邮件写成 `.eml` 落盘 ⇒
没有授权码也能端到端验证渲染逻辑（这在服务器上装 cron 之前是必做的自检）。
"""
from __future__ import annotations

import contextlib
import html
import logging
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Mapping, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: 模块日志器（**只记非敏感信息** ⇒ 授权码绝不入日志）
LOGGER = logging.getLogger(__name__)


class SmtpConfigError(RuntimeError):
    """SMTP 配置不完整 / 连接认证失败（**消息里绝不含密码**）。

    继承 `RuntimeError` ⇒ 上层一个 `except RuntimeError` 就能兜住。
    """


# ---------------------------------------------------------------- 授权码来源
#: secrets env 文件里**允许**出现的键 → 环境变量名（白名单，防注入任意变量）
SECRET_KEY_MAP: dict[str, str] = {
    "SMTP_HOST": "ETF_ROTATION_SMTP_HOST",
    "SMTP_PORT": "ETF_ROTATION_SMTP_PORT",
    "SMTP_USER": "ETF_ROTATION_SMTP_USER",
    "SMTP_PASSWORD": "ETF_ROTATION_SMTP_PASSWORD",
    "SMTP_SENDER": "ETF_ROTATION_SMTP_SENDER",
}


def load_secret_env(path: str | Path | None = None) -> tuple[dict[str, str], list[str]]:
    """读 0600 的 secrets env 文件。

    返回 `(键值字典, 警告列表)`。
    🔴 **文件权限过宽（`mode & 0o077 != 0`）⇒ 直接忽略并给警告** ——
    授权码明文躺在 0644 的文件里 = 谁都能读，等于没有授权码这道门。

    【格式与键映射】每行 `KEY=VALUE`；空行与 `#` 开头的行忽略；
    只认白名单键（其余键忽略并在警告里点名）：
        `SMTP_HOST` → `ETF_ROTATION_SMTP_HOST`
        `SMTP_PORT` → `ETF_ROTATION_SMTP_PORT`
        `SMTP_USER` → `ETF_ROTATION_SMTP_USER`
        `SMTP_PASSWORD` → `ETF_ROTATION_SMTP_PASSWORD`（126 **客户端授权码**）
        `SMTP_SENDER` → `ETF_ROTATION_SMTP_SENDER`
    """
    warnings: list[str] = []
    p = Path(path) if path else DC.SMTP_SECRETS_FILE
    if not p.exists():
        return {}, warnings
    try:
        mode = p.stat().st_mode & 0o777
    except OSError as exc:
        return {}, [f"读不了 secrets 文件权限：{exc}"]
    if mode & 0o077:
        LOGGER.warning(
            "SMTP secrets 文件权限过宽（0o%o）⇒ 已忽略（须 0600）：%s", mode, p
        )
        warnings.append(
            f"🔴 secrets 文件权限过宽（{mode:04o}，要求 0600/0400）⇒ 已忽略，"
            f"请执行：chmod 600 {p}"
        )
        return {}, warnings
    values: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f"读不了 secrets 文件：{exc}"]
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warnings.append(f"secrets 文件里有无法解析的行（已跳过）：{line[:40]}")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in SECRET_KEY_MAP:
            values[key] = value
        else:
            warnings.append(f"secrets 文件里的键不在白名单内（已忽略）：{key}")
    return values, warnings


@dataclass(frozen=True)
class SmtpConfig:
    """SMTP 配置（**单一事实源** = 环境变量 + 0600 secrets 文件）。"""

    host: str = DC.SMTP_HOST
    port: int = DC.SMTP_PORT
    user: str = ""
    password: str = ""
    use_ssl: bool = DC.SMTP_USE_SSL
    timeout: float = DC.SMTP_TIMEOUT
    sender: str = ""
    recipients: tuple[str, ...] = (DC.MAIL_TO,)
    warnings: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def __repr__(self) -> str:
        """**屏蔽密码** —— 配置对象经常被塞进日志/异常。"""
        return (
            f"SmtpConfig(host={self.host!r}, port={self.port!r}, user={self.user!r}, "
            f"password='***', use_ssl={self.use_ssl!r}, timeout={self.timeout!r}, "
            f"sender={self.sender!r}, recipients={self.recipients!r})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SmtpConfig:
        """按「环境变量 → secrets 文件」的顺序组装配置。

        优先级：`ETF_ROTATION_SMTP_*` > 通用 `SMTP_*` > secrets 文件 > 模块默认。
        🔴 缺 `password` / `user` **不抛** —— 由 `missing_fields()` 报告，
        上层据此降级成 dry-run（而不是整个流水线崩掉）。
        """
        env = os.environ if env is None else env
        secrets, warnings = load_secret_env()
        # secrets 文件里的键映射回环境变量名（**只填空位**：显式环境变量优先）
        for src, dst in SECRET_KEY_MAP.items():
            if src in secrets:
                env = {**env, dst: secrets[src]} if dst not in env else env

        def pick(*names: str, default: str = "") -> str:
            for name in names:
                value = env.get(name)
                if value not in (None, ""):
                    return str(value)
            return default

        user = pick("ETF_ROTATION_SMTP_USER", "SMTP_USER", "SMTP_USERNAME")
        password = pick("ETF_ROTATION_SMTP_PASSWORD", "SMTP_PASSWORD")
        host = pick("ETF_ROTATION_SMTP_HOST", "SMTP_HOST", default=DC.SMTP_HOST)
        port_raw = pick("ETF_ROTATION_SMTP_PORT", "SMTP_PORT", default=str(DC.SMTP_PORT))
        sender = pick("ETF_ROTATION_SMTP_SENDER", "SMTP_SENDER", "ETF_ROTATION_MAIL_FROM",
                      default=user or DC.MAIL_FROM)
        to_raw = pick("ETF_ROTATION_MAIL_TO", "MAIL_TO", default=DC.MAIL_TO)
        recipients = tuple(x.strip() for x in to_raw.split(",") if x.strip())
        ssl_raw = pick("ETF_ROTATION_SMTP_USE_SSL", "SMTP_USE_SSL",
                       default="1" if DC.SMTP_USE_SSL else "0")
        timeout_raw = pick("ETF_ROTATION_SMTP_TIMEOUT", "SMTP_TIMEOUT",
                           default=str(DC.SMTP_TIMEOUT))
        try:
            port = int(port_raw)
        except ValueError:
            port = DC.SMTP_PORT
            warnings = list(warnings) + [
                f"SMTP_PORT 不是整数（{port_raw!r}）⇒ 用默认 {DC.SMTP_PORT}"
            ]
        try:
            timeout = float(timeout_raw)
        except ValueError:
            timeout = DC.SMTP_TIMEOUT
        return cls(
            host=host,
            port=port,
            user=user,
            password=password,
            use_ssl=ssl_raw.strip().lower() in ("1", "true", "yes", "on"),
            timeout=timeout,
            sender=sender,
            recipients=recipients or (DC.MAIL_TO,),
            warnings=tuple(warnings),
        )

    def missing_fields(self) -> list[str]:
        """缺哪些必填字段（空列表 ⇒ 配置完整）。

        🔴 **只报字段名，不报值** ⇒ 异常消息不可能泄露授权码。
        """
        missing: list[str] = []
        if not self.user:
            missing.append("user")
        if not self.password:
            missing.append("password")
        if not self.sender:
            missing.append("sender")
        if not self.recipients:
            missing.append("recipients")
        return missing

    def is_complete(self) -> bool:
        """配置是否够真发一封信（缺字段 ⇒ 上层降级 dry-run）。"""
        return not self.missing_fields()


@dataclass
class EmailDraft:
    """邮件草稿（渲染完、发送前的中间产物）。"""

    subject: str
    html_body: str
    text_body: str


# ---------------------------------------------------------------- 渲染工具
def _fmt_money(value: object, digits: int = 2) -> str:
    """金额格式化；**缺失/非法一律显示「—」**（不许编数字）。

    🔴 **不加千分位逗号**（`30035.57` 而不是 `30,035.57`）⇒ 与账户快照 CSV 的
    口径逐字一致，便于对账与机器 grep。
    """
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:  # NaN
        return "—"
    return f"{number:.{digits}f}"


def _fmt_pct(value: object, digits: int = 2) -> str:
    """百分比格式化（输入是**小数**口径，如 `0.778` ⇒ `77.80%`）；非法显示「—」。

    【口径说明】本工程 `account_state` / `agents_rebalance` 产出的
    `cash_weight` / `distance_to_ma60_pct` / `safety_buffer_pct` **全是小数**
    （0.0608 = +6.08%）⇒ 这里统一 ×100 渲染。
    """
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:
        return "—"
    return f"{number * 100:.{digits}f}%"


def _fmt_shares(value: object) -> str:
    """股数格式化（**不加千分位** ⇒ 可被 grep/对账）；缺失 ⇒ 「—」。"""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:
        return "—"
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def _get(mapping: object, key: str, default: object = None) -> object:
    """从 dict 里取键（容忍 None / 非 dict）。"""
    if isinstance(mapping, Mapping):
        return mapping.get(key, default)
    return default


def _cash_weight(account: Mapping | None) -> float | None:
    """现金占比（**小数**口径）；account 没给就用 `cash / total_assets` 推。"""
    weight = _get(account, "cash_weight")
    if weight is None:
        cash = _get(account, "cash")
        total = _get(account, "total_assets")
        if cash is None or not total:
            return None
        try:
            weight = float(cash) / float(total)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    try:
        return float(weight)
    except (TypeError, ValueError):
        return None


_CARD_TD = (
    "padding:8px 10px;border-bottom:1px solid #e8eaed;text-align:right;"
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "font-size:13px;color:#202124;"
)
_CARD_TH = (
    "padding:8px 10px;border-bottom:1px solid #e8eaed;text-align:right;"
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:13px;"
    "color:#5f6368;font-weight:600;background:#f8f9fa;"
)
_CARD_TABLE = "width:100%;border-collapse:collapse;margin:6px 0 14px 0;"


def _render_account_card(account: Mapping | None) -> str:
    """资产卡片（总资产 / 现金 / 占比 / 市值 / 持仓数）。

    🔴 缺失的项显示「—」——**不拿别的键反推**（口径一律来自上游快照）。
    """
    total = _get(account, "total_assets")
    cash = _get(account, "cash")
    market_value = _get(account, "market_value")
    snapshot_date = _get(account, "snapshot_date")
    rows = [
        ("账户快照日", "—" if snapshot_date is None else str(snapshot_date)),
        ("总资产（元）", _fmt_money(total)),
        ("可用现金（元）", _fmt_money(cash)),
        ("现金占比", _fmt_pct(_cash_weight(account), 1)),
        ("ETF 持仓市值（元）", _fmt_money(market_value)),
        ("当前持仓数", _fmt_shares(_get(account, "n_positions"))),
    ]
    cells = "".join(
        f"<tr><td style='{_CARD_TD}text-align:left;color:#5f6368;'>{html.escape(k)}</td>"
        f"<td style='{_CARD_TD}'>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    return f"<table style='{_CARD_TABLE}'>{cells}</table>"


def _footer_html(extra: str = "") -> str:
    """页脚（时间戳 + 免责声明）。**所有模板都必须带**。

    免责声明来自 `daily_config.DISCLAIMER`（含「不构成投资建议」）⇒ 四个模板
    一律经过这里，**不许**在某个模板里漏掉。
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    note = "<div style='margin-top:18px;padding-top:10px;border-top:1px solid #e8eaed;"
    note += "font-size:12px;color:#5f6368;line-height:1.6;'>"
    note += f"<div>生成时间：{html.escape(stamp)}</div>"
    if extra:
        note += f"<div>{html.escape(extra)}</div>"
    note += f"<div style='margin-top:6px;'>{html.escape(DC.DISCLAIMER)}</div></div>"
    return note


def _quote_date(ma_info: Mapping | None) -> str | None:
    """收盘价对应的**交易日**（`ma_info["close_date"]`）；取不到 ⇒ None（不许反推）。"""
    value = _get(ma_info, "close_date")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _staleness_banner(asof: str, ma_info: Mapping | None) -> str:
    """行情数据滞后告警横幅（🔴 **陈旧数据不许当成当天的收盘价**）。

    `close_date != asof` ⇒ 本邮件用的收盘价**不是** asof 那天的 ⇒
    MA60 判定建立在旧收盘价上 ⇒ 必须显著披露，否则用户会把它误读成
    「今天破位」或「今天趋势完好」。
    """
    quote_date = _quote_date(ma_info)
    if quote_date is None or quote_date == str(asof):
        return ""
    return (
        "<div style='margin:10px 0;padding:10px 12px;border-left:4px solid #f9ab00;"
        "background:#fef7e0;font-size:13px;color:#5f6368;line-height:1.6;'>"
        "⚠️ <b>行情数据滞后</b>：本邮件使用的收盘价来自 <b>"
        f"{html.escape(quote_date)}</b>，不是 {html.escape(str(asof))}"
        "（当日日线尚未进入本地缓存 / 数据源未更新）⇒ MA60 判定基于旧收盘价，"
        "请用最新行情复核后再执行任何操作。</div>"
    )


def _envelope(title: str, body_html: str) -> str:
    """统一外壳（**全部内联 CSS** ⇒ 邮件客户端不认 `<style>` 里的复杂选择器）。"""
    head = (
        "<div style='font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "color:#202124;max-width:760px;'>"
        f"<h2 style='font-size:18px;margin:12px 0 4px 0;'>{html.escape(title)}</h2>"
        "<hr style='border:none;border-top:1px solid #e8eaed;margin:8px 0 14px 0;'>"
    )
    return head + body_html + "</div>"


def _text_of(html_body: str) -> str:
    """把 HTML 粗略转成纯文本（邮件的 text/plain 备用部分）。"""
    text = re.sub(r"<br\s*/?>", "\n", html_body)
    text = re.sub(r"</(p|tr|div|h[1-6]|li)>", "\n", text)
    text = re.sub(r"</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _value_of(value: object) -> str:
    """任意文本字段 ⇒ 字符串；缺失 ⇒ 「—」（不许编数字）。"""
    if value is None:
        return "—"
    if isinstance(value, str) and not value.strip():
        return "—"
    return str(value)


def _direction(value: object) -> str:
    """买卖方向（英文 token ⇒ 中文；其余原样；缺失 ⇒ 「—」）。"""
    if value is None:
        return "—"
    text = str(value).strip()
    mapping = {
        "buy": "买入",
        "sell": "卖出",
        "clear": "清仓",
        "exit": "清仓",
        "hold": "持有",
        "keep": "持有",
    }
    return mapping.get(text.lower(), text)


def _account_one_line(account: Mapping | None = None) -> str:
    """当前账户状态一行摘要；**拿不到就写「未知」**（🔴 绝不编数字）。

    取值顺序：调用方传入的 `account` → `daily_config` 基准常量（标「基准」）
    → 「未知」。
    """
    if account:
        total = _get(account, "total_assets")
        cash = _get(account, "cash")
        if total is not None or cash is not None:
            return (
                f"总资产 {_fmt_money(total)} 元 / 现金 {_fmt_money(cash)} 元"
            )
    if DC.TOTAL_ASSETS is not None or DC.CASH is not None:
        return (
            f"总资产 {_fmt_money(DC.TOTAL_ASSETS)} 元 / 现金 {_fmt_money(DC.CASH)} 元"
            "（daily_config 基准，非实时）"
        )
    return "未知"


# ---------------------------------------------------------------- 四个模板
def render_daily_ok(
    *,
    asof: str,
    account: Mapping | None = None,
    ma_info: Mapping | None = None,
    extra_notes: Sequence[str] | None = None,
) -> EmailDraft:
    """【日常正常】巡检邮件。

    主题**逐字**：`[ETF量化巡检] {asof} 正常 | 银行持仓均线完好`

    `ma_info` 期望键：`close / ma60 / ma20 / above_ma60 / distance_to_ma60_pct /
    safety_buffer_amount / safety_buffer_pct`；**缺的键显示「—」，不许反推**。
    """
    subject = f"[ETF量化巡检] {asof} 正常 | 银行持仓均线完好"
    ma_info = ma_info or {}
    rows = [
        ("标的", f"{DC.PRIMARY_HOLDING_CODE} {DC.PRIMARY_HOLDING_NAME}"),
        ("行情数据截至（交易日）", _quote_date(ma_info) or "—"),
        ("最新收盘价（元）", _fmt_money(_get(ma_info, "close"), 4)),
        ("MA20（进场准入线）", _fmt_money(_get(ma_info, "ma20"), 4)),
        ("MA60（持仓止损线）", _fmt_money(_get(ma_info, "ma60"), 4)),
        ("是否站上 MA60", _yn(_get(ma_info, "above_ma60"))),
        ("距 MA60 幅度", _fmt_pct(_get(ma_info, "distance_to_ma60_pct"))),
        ("距 MA60 安全余量（元）", _fmt_money(_get(ma_info, "safety_buffer_amount"))),
        ("距 MA60 安全余量（%）", _fmt_pct(_get(ma_info, "safety_buffer_pct"))),
    ]
    ma_cells = "".join(
        f"<tr><td style='{_CARD_TD}text-align:left;color:#5f6368;'>{html.escape(k)}</td>"
        f"<td style='{_CARD_TD}'>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    notes_html = ""
    if extra_notes:
        items = "".join(f"<li>{html.escape(str(n))}</li>" for n in extra_notes)
        notes_html = (
            "<div style='font-size:13px;color:#202124;margin:8px 0;'>"
            f"<b>巡检备注</b><ul>{items}</ul></div>"
        )

    body = (
        "<div style='font-size:14px;color:#202124;line-height:1.7;'>"
        "<b>系统状态：正常守纪律，无动作</b><br>"
        f"{DC.PRIMARY_HOLDING_CODE} 收盘价站上 MA60 安全垫，趋势完好；"
        "按纪律躺平，不做任何调仓动作。</div>"
        + _staleness_banner(asof, ma_info)
        + _render_account_card(account)
        + f"<table style='{_CARD_TABLE}'>{ma_cells}</table>"
        + notes_html
        + _footer_html()
    )
    html_body = _envelope(f"ETF 量化巡检 · {asof}", body)
    return EmailDraft(subject=subject, html_body=html_body, text_body=_text_of(body))


def _yn(value: object) -> str:
    """布尔 ⇒ 「是」/「否」；缺失 ⇒ 「—」。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "y", "1"):
            return "是"
        if text in ("false", "no", "n", "0"):
            return "否"
        return "—"
    return "是" if bool(value) else "否"


def render_emergency_break(
    *,
    asof: str,
    account: Mapping | None = None,
    ma_info: Mapping | None = None,
    shares: int = 0,
) -> EmailDraft:
    """【紧急破位】持仓跌破 MA60 的止损告警。

    主题**逐字**：`【紧急风控预警】{asof} {code} 跌破 MA60 触发止损`
    （`code` = `daily_config.PRIMARY_HOLDING_CODE`，即 512800）。

    正文必须写明：破位价格 / MA60 / 跌破幅度 / 当前持有股数 / 建议清仓指令，
    以及「这是**规则触发**、不是模型预测」。
    """
    code = DC.PRIMARY_HOLDING_CODE
    subject = f"【紧急风控预警】{asof} {code} 跌破 MA60 触发止损"
    ma_info = ma_info or {}
    shares_text = _fmt_shares(shares)
    rows = [
        ("标的", f"{code} {DC.PRIMARY_HOLDING_NAME}"),
        ("行情数据截至（交易日）", _quote_date(ma_info) or "—"),
        ("破位收盘价（元）", _fmt_money(_get(ma_info, "close"), 4)),
        ("MA60 止损线（元）", _fmt_money(_get(ma_info, "ma60"), 4)),
        ("跌破幅度", _fmt_pct(_get(ma_info, "distance_to_ma60_pct"))),
        ("当前持有股数", "—" if shares_text == "—" else f"{shares_text} 股"),
    ]
    cells = "".join(
        f"<tr><td style='{_CARD_TD}text-align:left;color:#5f6368;'>{html.escape(k)}</td>"
        f"<td style='{_CARD_TD};font-weight:600;color:#c5221f;'>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    instruction = f"收盘有效跌破 MA60 ⇒ 清仓 {shares_text} 股"
    body = (
        "<div style='font-size:14px;color:#202124;line-height:1.7;'>"
        "<b style='color:#c5221f;'>🚨 触发止损规则</b>："
        f"{code} 收盘价有效跌破 MA60 安全线。"
        f"<br><b>建议清仓指令</b>：{html.escape(instruction)}"
        "（下一交易日开盘卖出）。"
        "<br><span style='color:#5f6368;font-size:13px;'>"
        "本指令由「收盘有效跌破 MA60 ⇒ 持仓无条件退出」的<b>规则</b>触发，"
        "不是模型预测，也不代表对后市走势的判断。</span></div>"
        + _staleness_banner(asof, ma_info)
        + _render_account_card(account)
        + f"<table style='{_CARD_TABLE}'>{cells}</table>"
        + _footer_html()
    )
    html_body = _envelope(f"紧急风控预警 · {asof}", body)
    return EmailDraft(subject=subject, html_body=html_body, text_body=_text_of(body))


def _post_trade_rows(account: Mapping | None, actions: Sequence[Mapping] | None) -> list[list[str]]:
    """调仓后仓位分布（代码 / 股数 / 市值 / 占总资产 %）。

    🔴 市值口径：优先 `目标股数 × 价格`（与执行明细同源）⇒ 退化 `target_amount`
    ⇒ 再退化 `est_amount`；仍缺 ⇒ 「—」（**不编数字**）。
    卖出到 0 的标的（`target_shares = 0`）不进分布（调仓后已不在仓）。
    """
    total_assets = _get(account, "total_assets")
    rows: list[list[str]] = []
    for item in actions or []:
        code = _get(item, "code")
        if code is None or str(code).strip() == "":
            continue
        shares = _get(item, "target_shares")
        if shares is None:
            continue
        try:
            share_count = float(shares)
        except (TypeError, ValueError):
            continue
        if share_count <= 0:
            continue
        market_value: float | None = None
        price = _get(item, "price")
        if price is not None:
            try:
                market_value = share_count * float(price)
            except (TypeError, ValueError):
                market_value = None
        if market_value is None:
            for key in ("target_amount", "est_amount"):
                fallback = _get(item, key)
                if fallback is None:
                    continue
                try:
                    market_value = float(fallback)
                except (TypeError, ValueError):
                    market_value = None
                if market_value is not None:
                    break
        weight: float | None = None
        if market_value is not None and total_assets:
            try:
                total_value = float(total_assets)
                if total_value:
                    weight = market_value / total_value
            except (TypeError, ValueError):
                weight = None
        rows.append(
            [
                str(code),
                _fmt_shares(share_count),
                _fmt_money(market_value),
                _fmt_pct(weight),
            ]
        )
    return rows


def render_rebalance_card(
    *,
    signal_date: str,
    execution_date: str,
    account: Mapping | None = None,
    actions: Sequence[Mapping] | None = None,
    cash_after: float | None = None,
    notes: Sequence[str] | None = None,
) -> EmailDraft:
    """【月度调仓】执行决策卡（HTML 表格）。

    主题**逐字**：`【月度调仓决策卡】{signal_date} 信号日执行方案`

    `actions` 每项期望键：`code / name / action / target_shares / est_amount /
    est_fee / price / reason`；**缺键显示「—」，不许编数字**。

    🔴 正文必须显式写清「信号日 T 收盘出信号 / 执行日 T+1 开盘执行」，以及
    「节点日收盘价 ≠ 执行日成交价」的差异（否则下单的人会按节点收盘价预期成本）。
    """
    subject = f"【月度调仓决策卡】{signal_date} 信号日执行方案"
    actions = list(actions or [])
    order_headers = ["代码", "名称", "方向", "目标股数", "预估金额（元）", "预估费用（元）",
                     "价格（元）", "原因"]
    head_cells = "".join(
        f"<th style='{_CARD_TH}'>{html.escape(h)}</th>" for h in order_headers
    )
    body_rows: list[str] = []
    for item in actions:
        values = [
            _value_of(_get(item, "code")),
            _value_of(_get(item, "name")),
            _direction(_get(item, "action")),
            _fmt_shares(_get(item, "target_shares")),
            _fmt_money(_get(item, "est_amount")),
            _fmt_money(_get(item, "est_fee")),
            _fmt_money(_get(item, "price"), 4),
            _value_of(_get(item, "reason")),
        ]
        body_rows.append(
            "<tr>"
            + "".join(f"<td style='{_CARD_TD}'>{html.escape(v)}</td>" for v in values)
            + "</tr>"
        )
    if not body_rows:
        body_rows.append(
            "<tr>" + "".join(f"<td style='{_CARD_TD}'>—</td>" for _ in order_headers) + "</tr>"
        )
    order_table = (
        f"<table style='{_CARD_TABLE}'><tr>{head_cells}</tr>{''.join(body_rows)}</table>"
    )

    # 调仓后仓位分布（**由动作推导**，不是调仓前的账户快照）
    post_rows = _post_trade_rows(account, actions)
    if not post_rows:
        post_rows = [["—", "—", "—", "—"]]
    post_table = (
        f"<table style='{_CARD_TABLE}'>"
        "<tr><th style='{th}'>代码</th><th style='{th}'>股数</th>"
        "<th style='{th}'>市值（元）</th><th style='{th}'>占总资产</th></tr>"
        "{rows}</table>"
    ).format(th=_CARD_TH, rows="".join(
        "<tr>"
        f"<td style='{_CARD_TD}text-align:left;'>{html.escape(row[0])}</td>"
        f"<td style='{_CARD_TD}'>{html.escape(row[1])}</td>"
        f"<td style='{_CARD_TD}'>{html.escape(row[2])}</td>"
        f"<td style='{_CARD_TD}'>{html.escape(row[3])}</td>"
        "</tr>"
        for row in post_rows
    ))

    notes_html = ""
    if notes:
        items = "".join(f"<li>{html.escape(str(n))}</li>" for n in notes)
        notes_html = (
            "<div style='font-size:13px;margin:8px 0;'>"
            f"<b>口径与风险披露</b><ul>{items}</ul></div>"
        )

    body = (
        "<div style='font-size:14px;color:#202124;line-height:1.7;'>"
        f"<b>信号日</b> {html.escape(str(signal_date))} 收盘出信号　→　"
        f"<b>执行日</b> {html.escape(str(execution_date))} 开盘执行"
        "<br><span style='color:#5f6368;font-size:13px;'>"
        "🔴 节点日收盘价 ≠ 执行日成交价 ⇒ 实际成交股数/金额可能与本表不同；"
        "本卡为规则与模型的输出，需人工复核后下单。</span></div>"
        + _render_account_card(account)
        + "<div style='font-size:14px;margin:10px 0 4px 0;'><b>执行明细</b></div>"
        + order_table
        + "<div style='font-size:14px;margin:10px 0 4px 0;'><b>调仓后仓位分布</b></div>"
        + post_table
        + f"<div style='font-size:13px;color:#202124;'>"
        f"调仓后现金：<b>{html.escape(_fmt_money(cash_after))}</b> 元</div>"
        + notes_html
        + _footer_html()
    )
    html_body = _envelope(f"月度调仓决策卡 · {signal_date}", body)
    return EmailDraft(subject=subject, html_body=html_body, text_body=_text_of(body))


def render_failure_alert(
    *,
    asof: str,
    stage: str,
    traceback_text: str,
    error: str | None = None,
    account: Mapping | None = None,
) -> EmailDraft:
    """【流水线执行失败告警】。

    主题**逐字**：`【流水线执行失败告警】{asof} {stage} 阶段异常`

    正文必须含：失败阶段 / 异常类型与消息 / **完整 Traceback**（`<pre>` 里、已转义）/
    发生时间 / 当前账户状态一行摘要（拿不到就写「未知」）。
    """
    subject = f"【流水线执行失败告警】{asof} {stage} 阶段异常"
    tb_html = html.escape(str(traceback_text or ""))
    error_html = html.escape(str(error)) if error else "未知"
    occurred_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    body = (
        "<div style='font-size:14px;color:#202124;line-height:1.7;'>"
        "<b style='color:#c5221f;'>🚨 每日 ETF 轮动流水线执行失败</b><br>"
        f"失败阶段：<b>{html.escape(str(stage))}</b><br>"
        f"异常：<code>{error_html}</code><br>"
        f"发生时间：{html.escape(occurred_at)}<br>"
        f"当前账户状态：{html.escape(_account_one_line(account))}</div>"
        "<div style='font-size:14px;margin:10px 0 4px 0;'><b>完整 Traceback</b></div>"
        "<pre style='margin:0;background:#f8f9fa;border:1px solid #e8eaed;"
        "border-radius:4px;padding:10px;font-size:12px;white-space:pre-wrap;"
        f"word-break:break-all;overflow-x:auto;'>{tb_html}</pre>"
        + _footer_html()
    )
    html_body = _envelope(f"流水线执行失败告警 · {asof}", body)
    return EmailDraft(subject=subject, html_body=html_body, text_body=_text_of(body))


# ---------------------------------------------------------------- 发送
def _build_message(draft: EmailDraft, cfg: SmtpConfig, to: Sequence[str]) -> MIMEMultipart:
    """组装 MIME 邮件。

    【为什么 plain 在前、html 在后】`multipart/alternative` 里客户端取**最后一个**
    它支持的子部分 ⇒ HTML 必须排在纯文本之后，否则 HTML 客户端也只看到纯文本。
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(draft.subject, "utf-8")
    msg["From"] = formataddr((str(Header("ETF量化巡检", "utf-8")), cfg.sender))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = (
        "<etf-rotation-"
        f"{datetime.now().astimezone().strftime('%Y%m%d%H%M%S%f')}@localhost>"
    )
    msg.attach(MIMEText(draft.text_body or "", "plain", "utf-8"))
    msg.attach(MIMEText(draft.html_body or "", "html", "utf-8"))
    return msg


def _attach_all(msg: MIMEMultipart, attachments: Sequence[tuple[str, bytes, str]] | None) -> None:
    """挂二进制附件（base64 ⇒ 邮件体是纯 ASCII，避免中文环境下的编码坑）。"""
    from email import encoders

    for name, payload, mime in attachments or []:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(payload)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)


def send_email(
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    to: str | Sequence[str] | None = None,
    cfg: SmtpConfig | None = None,
    dry_run: bool = False,
    out_dir: str | Path | None = None,
    attachments: Sequence[tuple[str, bytes, str]] | None = None,
) -> dict:
    """发一封邮件。

    参数
    ----
    dry_run:
        **绝不联网**，把完整邮件写成 `.eml` 落盘（`out_dir`；缺省 `<RUN_ROOT>/eml`）。
    to:
        收件人；字符串（逗号分隔）或序列。缺省用 `cfg.recipients`。

    返回
    ----
    dict：`{"sent","dry_run","subject","to","path","reason"}`。
    """
    cfg = cfg or SmtpConfig.from_env()
    if isinstance(to, str):
        recipients = tuple(x.strip() for x in to.split(",") if x.strip())
    elif to is None:
        recipients = tuple(cfg.recipients)
    else:
        recipients = tuple(str(x) for x in to)

    draft = EmailDraft(
        subject=subject,
        html_body=html_body,
        text_body=text_body if text_body is not None else _text_of(html_body),
    )

    if dry_run:
        target_dir = Path(out_dir) if out_dir else (DC.RUN_ROOT / "eml")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in subject)[:60]
        path = target_dir / f"{stamp}_{safe}.eml"
        msg = _build_message(draft, cfg, recipients)
        _attach_all(msg, attachments)
        path.write_bytes(msg.as_bytes())
        return {
            "sent": False,
            "dry_run": True,
            "subject": subject,
            "to": list(recipients),
            "path": str(path),
            "reason": "dry-run：未发送",
        }

    missing = cfg.missing_fields()
    if missing:
        raise SmtpConfigError(
            f"SMTP 配置不完整，缺：{missing}；"
            f"请把 126 客户端授权码写进 {DC.SMTP_SECRETS_FILE}（0600）或设环境变量 "
            f"ETF_ROTATION_SMTP_PASSWORD"
        )

    msg = _build_message(draft, cfg, recipients)
    _attach_all(msg, attachments)

    server = None
    try:
        if cfg.use_ssl:
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                # 服务端不支持 STARTTLS ⇒ 继续（内网自建 SMTP 常见）
                LOGGER.warning("SMTP 服务端不支持 STARTTLS ⇒ 按明文继续")
    except Exception as exc:
        raise SmtpConfigError(
            f"连不上 SMTP {cfg.host}:{cfg.port} —— {type(exc).__name__}: {exc}"
        ) from exc

    try:
        server.login(cfg.user, cfg.password)
        server.send_message(msg, from_addr=cfg.sender, to_addrs=list(recipients))
    except smtplib.SMTPAuthenticationError as exc:
        raise SmtpConfigError(
            f"SMTP 认证失败（{cfg.user}）—— 126 需要「客户端授权码」而不是网页登录密码："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except Exception as exc:
        raise SmtpConfigError(f"发信失败：{type(exc).__name__}: {exc}") from exc
    finally:
        # 关闭失败**不影响**已发出的结果 ⇒ 只吞这一处的异常
        with contextlib.suppress(Exception):
            server.quit()

    return {
        "sent": True,
        "dry_run": False,
        "subject": subject,
        "to": list(recipients),
        "path": None,
        "reason": "已发送",
    }


if __name__ == "__main__":  # pragma: no cover - 手工自检（渲染一封 dry-run 样例）
    import tempfile

    _draft = render_daily_ok(
        asof=DC.ACCOUNT_SNAPSHOT_DATE,
        account={
            "total_assets": DC.TOTAL_ASSETS,
            "cash": DC.CASH,
            "market_value": DC.MARKET_VALUE,
            "cash_weight": DC.CASH / DC.TOTAL_ASSETS,
            "n_positions": 1,
        },
        ma_info={"close": 0.866, "ma60": 0.85, "ma20": 0.842, "above_ma60": True},
    )
    with tempfile.TemporaryDirectory() as _tmp:
        _result = send_email(
            _draft.subject, _draft.html_body, _draft.text_body, dry_run=True, out_dir=_tmp
        )
        print(_result["path"])
