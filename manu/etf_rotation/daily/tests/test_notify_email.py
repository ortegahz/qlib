"""`notify_email.py` 的 pytest 自检（**完全离线**：不发真信、不联网）。

【为什么可以独立跑】
测试只 import `notify_email`（它自己会把 `daily/` 挂进 `sys.path`）⇒
不引其它仓库模块、不碰 pandas、不连网络 ⇒ 任何机器上都能复现。

【覆盖清单（对应交付规格）】
1. 四个模板的**主题逐字**；
2. 四个模板正文都含「不构成投资建议」；
3. `render_daily_ok` 的 HTML 含 `30035.57` / `23367.37` / `77.8`；
4. `render_rebalance_card` 含 `<table` 且含 action 的 code；
5. `send_email(dry_run=True)` **不联网**、落盘 `.eml`、文件里有主题与正文；
6. 缺 password ⇒ 抛 `SmtpConfigError`，异常消息**不含**密码字面量；
7. `SmtpConfig.__repr__` **不含**密码；
8. secrets 文件：`0600` 能读、`0644` 被忽略（不抛、只是读不到）；
9. `ma_info` 缺键 ⇒ 渲染成「—」（不是 0、也不是编造的数字）。
"""
from __future__ import annotations

import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

_DAILY_DIR = Path(__file__).resolve().parents[1]
if str(_DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(_DAILY_DIR))

import notify_email as NE  # noqa: E402

#: 账户快照（2026-11-05 用户口径；与 `account_state.account_summary()` 同源）
ACCOUNT: dict = {
    "snapshot_date": "2026-11-05",
    "total_assets": 30035.57,
    "cash": 23367.37,
    "market_value": 6668.20,
    "cash_weight": 23367.37 / 30035.57,
    "n_positions": 1,
}

#: 均线信息（`agents_rebalance` 产出的口径：百分比全是**小数**）
MA_INFO: dict = {
    "close": 0.866,
    "ma60": 0.850,
    "ma20": 0.842,
    "above_ma60": True,
    "distance_to_ma60_pct": 0.0188,
    "safety_buffer_amount": 123.28,
    "safety_buffer_pct": 0.0188,
}

#: 调仓动作（键与 `agents_rebalance` 的 `orders` 一致）
ACTIONS: list[dict] = [
    {
        "code": "512800",
        "name": "银行ETF华宝",
        "action": "持有",
        "target_shares": 7700,
        "est_amount": 0.0,
        "est_fee": 0.0,
        "price": 0.866,
        "reason": "保底仓，MA60 未破",
    },
    {
        "code": "159928",
        "name": "消费ETF",
        "action": "买入",
        "target_shares": 6000,
        "est_amount": 0.0,
        "est_fee": 5.0,
        "price": 1.05,
        "reason": "E=W×S 排名进入前五",
    },
]


def _daily_draft() -> NE.EmailDraft:
    return NE.render_daily_ok(asof="2026-11-05", account=ACCOUNT, ma_info=MA_INFO)


def _break_draft() -> NE.EmailDraft:
    return NE.render_emergency_break(
        asof="2026-11-05", account=ACCOUNT, ma_info=MA_INFO, shares=7700
    )


def _card_draft() -> NE.EmailDraft:
    return NE.render_rebalance_card(
        signal_date="2026-12-01",
        execution_date="2026-12-02",
        account=ACCOUNT,
        actions=ACTIONS,
        cash_after=12345.67,
        notes=["节点日收盘价 ≠ 执行日成交价"],
    )


def _failure_draft() -> NE.EmailDraft:
    return NE.render_failure_alert(
        asof="2026-11-05",
        stage="fetch_data",
        traceback_text="Traceback (most recent call last):\n  ValueError: boom",
        error="ValueError: boom",
    )


# ---------------------------------------------------------------- 1. 主题逐字
def test_subjects_are_verbatim():
    assert _daily_draft().subject == "[ETF量化巡检] 2026-11-05 正常 | 银行持仓均线完好"
    assert (
        _break_draft().subject == "【紧急风控预警】2026-11-05 512800 跌破 MA60 触发止损"
    )
    assert _card_draft().subject == "【月度调仓决策卡】2026-12-01 信号日执行方案"
    assert (
        _failure_draft().subject
        == "【流水线执行失败告警】2026-11-05 fetch_data 阶段异常"
    )


# ---------------------------------------------------------------- 2. 免责声明
def test_all_templates_carry_disclaimer():
    for draft in (_daily_draft(), _break_draft(), _card_draft(), _failure_draft()):
        assert "不构成投资建议" in draft.html_body
        assert "不构成投资建议" in draft.text_body


# ---------------------------------------------------------------- 3. 资产卡片数字
def test_daily_ok_html_contains_key_numbers():
    html = _daily_draft().html_body
    assert "30035.57" in html
    assert "23367.37" in html
    assert "77.8" in html
    assert "正常守纪律，无动作" in html


# ---------------------------------------------------------------- 4. 调仓卡表格
def test_rebalance_card_has_table_and_action_code():
    html = _card_draft().html_body
    assert "<table" in html
    assert "512800" in html
    assert "159928" in html
    assert "调仓后仓位分布" in html
    assert "节点日收盘价 ≠ 执行日成交价" in html


# ---------------------------------------------------------------- 5. dry-run 不联网
def test_dry_run_does_not_touch_network_and_writes_eml(tmp_path, monkeypatch):
    def _forbidden(*args, **kwargs):  # pragma: no cover - 被调用即测试失败
        raise AssertionError("dry-run 绝不允许建立 SMTP 连接")

    monkeypatch.setattr(NE.smtplib, "SMTP_SSL", _forbidden)
    monkeypatch.setattr(NE.smtplib, "SMTP", _forbidden)

    draft = _daily_draft()
    result = NE.send_email(
        draft.subject,
        draft.html_body,
        draft.text_body,
        dry_run=True,
        out_dir=tmp_path,
    )
    assert result["sent"] is False
    assert result["dry_run"] is True
    assert result["reason"] == "dry-run：未发送"
    assert result["subject"] == draft.subject
    assert result["to"] == ["zxthz@126.com"]

    path = Path(result["path"])
    assert path.exists() and path.suffix == ".eml"

    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    assert message["Subject"] == draft.subject
    # 🔴 只取 text/* 叶子节点：`get_content()` 对 multipart 容器会抛 KeyError
    parts = {
        part.get_content_type(): part.get_content()
        for part in message.walk()
        if part.get_content_maintype() == "text"
    }
    assert "text/html" in parts and "text/plain" in parts
    assert "正常守纪律，无动作" in parts["text/html"]
    assert "不构成投资建议" in parts["text/html"]
    assert "正常守纪律，无动作" in parts["text/plain"]


# ---------------------------------------------------------------- 6. 缺配置抛错
def test_missing_password_raises_and_message_hides_secrets():
    cfg = NE.SmtpConfig(
        host="smtp.126.com",
        port=465,
        user="zxthz@126.com",
        password="",
        use_ssl=True,
        timeout=20.0,
        sender="zxthz@126.com",
        recipients=("zxthz@126.com",),
    )
    with pytest.raises(NE.SmtpConfigError) as excinfo:
        NE.send_email("主题", "<p>正文</p>", cfg=cfg)
    message = str(excinfo.value)
    assert "password" in message
    assert "hunter2" not in message

    # 反向场景：密码**有值**但 user 缺 ⇒ 异常消息里绝不能出现密码字面量
    cfg2 = NE.SmtpConfig(
        host="smtp.126.com",
        port=465,
        user="",
        password="s3cr3t-auth-code",
        use_ssl=True,
        timeout=20.0,
        sender="zxthz@126.com",
        recipients=("zxthz@126.com",),
    )
    with pytest.raises(NE.SmtpConfigError) as excinfo2:
        NE.send_email("主题", "<p>正文</p>", cfg=cfg2)
    assert "s3cr3t-auth-code" not in str(excinfo2.value)


# ---------------------------------------------------------------- 7. repr 屏蔽密码
def test_repr_hides_password():
    cfg = NE.SmtpConfig(
        host="smtp.126.com",
        port=465,
        user="zxthz@126.com",
        password="s3cr3t-auth-code",
        use_ssl=True,
        timeout=20.0,
        sender="zxthz@126.com",
        recipients=("zxthz@126.com",),
    )
    assert "s3cr3t-auth-code" not in repr(cfg)
    assert "s3cr3t-auth-code" not in str(cfg)
    assert "***" in repr(cfg)


# ---------------------------------------------------------------- 8. secrets 文件权限
def test_secrets_file_0600_is_read(tmp_path, monkeypatch):
    secret = tmp_path / "smtp.env"
    secret.write_text(
        "# 注释行\n\nSMTP_PASSWORD=from-secret-file\nSMTP_USER=zxthz@126.com\n",
        encoding="utf-8",
    )
    secret.chmod(0o600)
    monkeypatch.setattr(NE.DC, "SMTP_SECRETS_FILE", secret)

    values, warnings = NE.load_secret_env(secret)
    assert values["SMTP_PASSWORD"] == "from-secret-file"

    cfg = NE.SmtpConfig.from_env({})
    assert cfg.password == "from-secret-file"
    assert cfg.user == "zxthz@126.com"
    assert cfg.missing_fields() == []


def test_secrets_file_0644_is_ignored(tmp_path, monkeypatch):
    secret = tmp_path / "smtp.env"
    secret.write_text("SMTP_PASSWORD=from-secret-file\n", encoding="utf-8")
    secret.chmod(0o644)
    monkeypatch.setattr(NE.DC, "SMTP_SECRETS_FILE", secret)

    values, warnings = NE.load_secret_env(secret)
    assert values == {}
    assert warnings and "权限过宽" in warnings[0]

    cfg = NE.SmtpConfig.from_env({})
    assert cfg.password == ""
    assert "password" in cfg.missing_fields()


# ---------------------------------------------------------------- 9. 缺键渲染「—」
def test_missing_ma_info_keys_render_placeholder():
    html = NE.render_daily_ok(asof="2026-11-05", account=ACCOUNT, ma_info={}).html_body
    assert "—" in html
    assert "0.0000" not in html
    assert "0.00%" not in html

    break_html = NE.render_emergency_break(
        asof="2026-11-05", account=ACCOUNT, ma_info={}, shares=7700
    ).html_body
    assert "—" in break_html
    assert "0.0000" not in break_html

    # 调仓动作缺键 ⇒ 「—」，不许编数字
    card_html = NE.render_rebalance_card(
        signal_date="2026-12-01",
        execution_date="2026-12-02",
        account=ACCOUNT,
        actions=[{"code": "512800"}],
        cash_after=None,
    ).html_body
    assert "—" in card_html
    assert "0.00" not in card_html


# ---------------------------------------------------------------- 行情日期披露
def test_quote_date_row_is_rendered():
    """🆕 邮件必须写明**收盘价来自哪个交易日**（不许让人误以为是当天的）。"""
    html = NE.render_daily_ok(
        asof="2026-11-05",
        account=ACCOUNT,
        ma_info={"close": 0.874, "ma60": 0.841, "close_date": "2026-11-05"},
    ).html_body
    assert "行情数据截至" in html
    assert "2026-11-05" in html
    # 当天数据 ⇒ 不出滞后横幅
    assert "行情数据滞后" not in html


def test_stale_quote_date_raises_banner():
    """🔴 陈旧行情（close_date != asof）⇒ 必须显著披露，否则会误读成今日破位。"""
    html = NE.render_daily_ok(
        asof="2026-11-05",
        account=ACCOUNT,
        ma_info={"close": 0.874, "ma60": 0.841, "close_date": "2026-11-04"},
    ).html_body
    assert "行情数据滞后" in html
    assert "2026-11-04" in html
    assert "2026-11-05" in html


def test_stale_quote_date_raises_banner_in_break_alert():
    """破位告警**更**需要披露数据日期（基于旧价就叫人清仓 = 危险）。"""
    html = NE.render_emergency_break(
        asof="2026-11-05",
        account=ACCOUNT,
        ma_info={"close": 0.800, "ma60": 0.841, "close_date": "2026-11-04"},
        shares=7700,
    ).html_body
    assert "行情数据滞后" in html
    assert "2026-11-04" in html


def test_missing_close_date_renders_placeholder_without_banner():
    """缺 close_date ⇒ 渲染「—」，**不许**编一个日期出来。"""
    html = NE.render_daily_ok(
        asof="2026-11-05", account=ACCOUNT, ma_info={"close": 0.874, "ma60": 0.841}
    ).html_body
    assert "行情数据截至" in html
    assert "行情数据滞后" not in html
