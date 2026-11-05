"""test_agents_rebalance.py —— 双轨模式与执行卡的**离线**自测。

🔴 **完全离线**：不联网、不跑研究链、不调 LLM、不写仓库内任何文件；
   价格与账户全部用**合成数据**（``tmp_path``）⇒ 本机与服务器都能跑同一套断言。

覆盖（与实现规格一一对应）：

1. ``compute_ma_levels``：MA20/MA60 数值正确、**只用 asof 之前的数据**（禁未来函数）、
   样本不足 ⇒ ``ma60 is None``；
2. ``run_mode_a``：未破位 ⇒ ``status == "ok"`` 且 ``action is None``；破位 ⇒
   ``status == "broken"`` 且给出清仓指令；**返回值里没有任何新仓位动作**；
3. ``build_execution_card``：整手向下取整、单只金额上限、货基碎屑过滤、
   现金缓冲守卫、卖出含印花税；
4. ``mode_router.route``：2026-12-01 ⇒ B、2026-11-20 ⇒ A 且处于静默防守；
5. ``build_run_dir`` / ``log_path``：路径前缀与建目录行为。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_HERE = Path(__file__).resolve().parent
_DAILY = _HERE.parent
if str(_DAILY) not in sys.path:
    sys.path.insert(0, str(_DAILY))

import agents_rebalance as AR  # noqa: E402
import daily_config as DC  # noqa: E402
import mode_router as MR  # noqa: E402


# ---------------------------------------------------------------- 合成数据工具
def _price_frame(closes: list[float], code: str = "512800") -> pd.DataFrame:
    """造一段合成日线（工作日连续，无跳空）。"""
    dates = pd.bdate_range(start="2026-08-03", periods=len(closes))
    return pd.DataFrame({"code": [code] * len(closes), "date": dates, "close": closes})


def _write_prices_csv(path: Path, closes: list[float], code: str = "512800") -> Path:
    frame = _price_frame(closes, code=code)
    frame.to_csv(path, index=False)
    return path


def _all_strings(obj: object):
    """递归展开 dict / list 里的所有字符串（用来做「一个买字都不许有」的断言）。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _all_strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _all_strings(value)


# ---------------------------------------------------------------- 1. 均线口径
def test_compute_ma_levels_known_values():
    closes = [1.0 + i * 0.01 for i in range(60)]
    frame = _price_frame(closes)
    ma = AR.compute_ma_levels(frame, "512800", asof=frame["date"].max(), shares=7700)

    assert ma["code"] == "512800"
    assert ma["n_obs"] == 60
    assert ma["close"] == pytest.approx(closes[-1])
    assert ma["ma20"] == pytest.approx(sum(closes[-20:]) / 20)
    assert ma["ma60"] == pytest.approx(sum(closes) / 60)
    assert ma["above_ma20"] is True
    assert ma["above_ma60"] is True
    assert ma["distance_to_ma60_pct"] == pytest.approx(closes[-1] / (sum(closes) / 60) - 1.0)
    assert ma["safety_buffer_pct"] == pytest.approx(ma["distance_to_ma60_pct"])
    assert ma["safety_buffer_amount"] == pytest.approx(
        (closes[-1] - sum(closes) / 60) * 7700)


def test_compute_ma_levels_no_lookahead():
    """asof 之后的极端价格**不得**影响结果（禁未来函数）。"""
    closes = [1.0 + i * 0.01 for i in range(60)]
    frame = _price_frame(closes)
    asof = frame["date"].max()

    base = AR.compute_ma_levels(frame, "512800", asof=asof)
    polluted = pd.concat([
        frame,
        pd.DataFrame({
            "code": ["512800"] * 5,
            "date": pd.bdate_range(start=asof + pd.Timedelta(days=1), periods=5),
            "close": [0.01] * 5,
        }),
    ], ignore_index=True)
    after = AR.compute_ma_levels(polluted, "512800", asof=asof)

    assert after["close"] == pytest.approx(base["close"])
    assert after["ma20"] == pytest.approx(base["ma20"])
    assert after["ma60"] == pytest.approx(base["ma60"])
    assert after["n_obs"] == base["n_obs"] == 60


def test_compute_ma_levels_insufficient_data():
    """样本不足 ⇒ ma60 = None（不许编数字），n_obs 反映实数。"""
    closes = [1.0 + i * 0.01 for i in range(30)]
    frame = _price_frame(closes)
    ma = AR.compute_ma_levels(frame, "512800", asof=frame["date"].max())

    assert ma["n_obs"] == 30
    assert ma["ma60"] is None
    assert ma["above_ma60"] is None
    assert ma["safety_buffer_amount"] is None
    assert ma["ma20"] is not None


# ---------------------------------------------------------------- 2. 模式 A
def test_run_mode_a_ok(tmp_path):
    """趋势完好 ⇒ status == "ok" 且 action 必须是 None。"""
    closes = [1.0 + i * 0.01 for i in range(60)]
    csv_path = _write_prices_csv(tmp_path / "prices.csv", closes)

    result = AR.run_mode_a(asof="2026-11-20", prices_csv=csv_path)

    assert result["mode"] == "A"
    assert result["asof"] == "2026-11-20"
    assert result["status"] == "ok"
    assert result["action"] is None
    assert result["email_kind"] == "daily_ok"
    assert result["ma"]["above_ma60"] is True
    assert result["holding"]["code"] == DC.PRIMARY_HOLDING_CODE
    assert result["holding"]["shares"] == DC.PRIMARY_HOLDING_SHARES


def test_run_mode_a_broken(tmp_path):
    """破位 ⇒ status == "broken" 且给出清仓指令（股数 / 触发价 / MA60 / 跌破幅度）。"""
    closes = [1.0] * 59 + [0.8]
    csv_path = _write_prices_csv(tmp_path / "prices.csv", closes)

    result = AR.run_mode_a(asof="2026-11-20", prices_csv=csv_path)

    assert result["status"] == "broken"
    assert result["email_kind"] == "emergency_break"
    action = result["action"]
    assert action is not None
    assert action["action"] == "紧急平仓"
    assert action["shares"] == DC.PRIMARY_HOLDING_SHARES
    assert action["trigger_price"] == pytest.approx(0.8)
    assert action["ma60"] == pytest.approx(sum(closes) / 60)
    assert action["break_pct"] < 0


def test_run_mode_a_insufficient_data(tmp_path):
    """样本不足 ⇒ insufficient_data，且**不**产生任何动作。"""
    closes = [1.0 + i * 0.01 for i in range(10)]
    csv_path = _write_prices_csv(tmp_path / "prices.csv", closes)

    result = AR.run_mode_a(asof="2026-11-20", prices_csv=csv_path)

    assert result["status"] == "insufficient_data"
    assert result["action"] is None
    assert result["email_kind"] == "daily_ok"


def test_run_mode_a_never_produces_new_position_action(tmp_path):
    """🔴 模式 A 是**构造性保证**：返回值里不允许出现任何新仓位动作。"""
    ok_csv = _write_prices_csv(tmp_path / "ok.csv", [1.0 + i * 0.01 for i in range(60)])
    broken_csv = _write_prices_csv(tmp_path / "broken.csv", [1.0] * 59 + [0.8])

    for csv_path in (ok_csv, broken_csv):
        result = AR.run_mode_a(asof="2026-11-20", prices_csv=csv_path)
        for text in _all_strings(result):
            assert "买" not in text, f"模式 A 出现了新仓位动作：{text}"
        assert not result.get("orders")
        if result["status"] == "ok":
            assert result["action"] is None


def test_run_mode_a_writes_artifact(tmp_path):
    csv_path = _write_prices_csv(tmp_path / "prices.csv", [1.0 + i * 0.01 for i in range(60)])
    run_dir = tmp_path / "run"
    result = AR.run_mode_a(asof="2026-11-20", prices_csv=csv_path, run_dir=run_dir)
    artifact = Path(result["artifact"])
    assert artifact.exists()
    assert artifact.name == "mode_a_20261120.json"


# ---------------------------------------------------------------- 3. 执行卡片
def _card(actions, **kwargs):
    account = kwargs.pop("account", {"total_assets": 30035.57, "cash": 23367.37})
    return AR.build_execution_card(
        actions=actions, account=account, asof="2026-12-01", execution_date="2026-12-02", **kwargs)


def test_card_shares_floor_to_lot():
    """6050 元目标、单价 0.866 ⇒ 6,900 股（不是 6,990），金额 ≤ 目标。"""
    card = _card([{
        "code": "512800", "name": "银行ETF华宝", "current_shares": 0,
        "price": 0.866, "target_amount": 6050.0,
    }])
    order = card["orders"][0]
    assert order["target_shares"] == 6900
    assert order["target_shares"] % DC.LOT_SIZE == 0
    assert order["target_amount"] <= 6050.0
    assert order["delta_shares"] == 6900


def test_card_single_position_cap():
    """单只上限 6,000：8,000 元目标 ⇒ 金额被压到 ≤ 6,000 且 capped 为 True。"""
    card = _card([{
        "code": "512800", "name": "银行ETF华宝", "current_shares": 0,
        "price": 0.866, "target_amount": 8000.0,
    }])
    order = card["orders"][0]
    assert order["capped"] is True
    assert order["target_amount"] <= DC.MAX_POSITION_AMOUNT
    assert order["target_shares"] % DC.LOT_SIZE == 0
    assert order["cap_note"]


def test_card_money_fund_slot_is_unbuildable():
    """货基碎屑：一手 10,001 元 > 6,000 上限 ⇒ 不可建仓、不下单、计入不可建仓席位。"""
    card = _card([{
        "code": "511880", "name": "银华日利", "current_shares": 0,
        "price": 100.012, "target_amount": 6000.0,
    }])
    slots = card["cash_flow"]["unbuildable_slots"]
    assert slots, "货基碎屑必须计入 unbuildable_slots"
    slot = slots[0]
    assert slot["code"] == "511880"
    assert slot["action"] == "不可建仓"
    assert slot["target_shares"] == 0
    assert "511880" not in [o["code"] for o in card["orders"]]


def test_card_cash_buffer_guard():
    """现金会被吃到 < 200 元 ⇒ 系统缩减买入，把现金拉回缓冲线之上。"""
    card = _card(
        [{"code": "512800", "name": "银行ETF华宝", "current_shares": 0,
          "price": 1.0, "target_amount": 400.0}],
        account={"total_assets": 1000.0, "cash": 500.0},
    )
    assert card["cash_flow"]["scaled_down"], "现金缓冲不足时必须缩减买入"
    assert card["post_trade"]["cash"] >= DC.CASH_BUFFER_MIN
    assert card["post_trade"]["cash_buffer_ok"] is True
    assert card["cash_flow"]["feasible"] is True


def test_card_cash_buffer_infeasible_is_reported():
    """现金连缓冲线都保不住（无买入可减）⇒ feasible False + 明确 reason。"""
    card = _card(
        [{"code": "512800", "name": "银行ETF华宝", "current_shares": 0,
          "price": 1.0, "target_amount": 400.0}],
        account={"total_assets": 1000.0, "cash": 100.0},
    )
    assert card["cash_flow"]["feasible"] is False
    assert card["post_trade"]["cash_buffer_ok"] is False
    assert any("不可行" in w for w in card["warnings"])


def test_card_sell_fee_includes_stamp_duty():
    """卖出费（佣金 + 印花税）必须高于同金额买入费（只有佣金）。"""
    buy = _card(
        [{"code": "512800", "name": "银行ETF华宝", "current_shares": 0,
          "price": 1.0, "target_amount": 1000.0}],
        account={"total_assets": 5000.0, "cash": 5000.0},
    )
    sell = _card(
        [{"code": "512800", "name": "银行ETF华宝", "current_shares": 1000,
          "price": 1.0, "target_amount": 0.0}],
        account={"total_assets": 5000.0, "cash": 0.0},
    )
    buy_fee = buy["orders"][0]["est_fee"]
    sell_fee = sell["orders"][0]["est_fee"]
    assert buy_fee == pytest.approx(DC.MIN_COMMISSION)
    assert sell_fee == pytest.approx(DC.MIN_COMMISSION + 1000.0 * DC.STAMP_DUTY_RATE)
    assert sell_fee > buy_fee
    assert sell["orders"][0]["action"] == "卖出"


def test_card_missing_fields_become_none_with_warning():
    """缺字段 ⇒ 该字段为 None 并在 warnings 里点名（不编数字）。"""
    card = _card(
        [{"code": "999999", "name": "未知标的", "current_shares": 0, "price": 1.0}],
        account={"total_assets": 1000.0},
    )
    order = card["orders"][0]
    assert order["target_shares"] is None
    assert order["delta_shares"] is None
    assert card["cash_flow"]["cash_end"] is None
    assert any("999999" in w for w in card["warnings"])
    assert any("cash" in w for w in card["warnings"])


# ---------------------------------------------------------------- 4. 模式路由
def test_route_signal_date_is_mode_b():
    result = MR.route("2026-12-01")
    assert result["date"] == "2026-12-01"
    assert result["mode"] == "B"
    assert result["execution_date"] == DC.NEXT_EXECUTION_DATE
    assert result["is_silent_defense"] is False
    assert "2026-12-01" in result["reason"]


def test_route_normal_day_is_mode_a_and_silent():
    result = MR.route("2026-11-20")
    assert result["mode"] == "A"
    assert result["is_silent_defense"] is True
    assert result["execution_date"] is None
    assert result["run_dir"].endswith("20261120")


# ---------------------------------------------------------------- 5. 目录 / 日志
def test_build_run_dir_and_log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(DC, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(DC, "LOG_DIR", tmp_path / "logs")

    run_dir = MR.build_run_dir("2026-12-01")
    assert str(run_dir).startswith(str(tmp_path / "runs"))
    assert run_dir.name == "20261201"
    assert run_dir.is_dir()

    log = MR.log_path("2026-12-01")
    assert str(log).startswith(str(tmp_path / "logs"))
    assert log.name == "daily_run_20261201.log"
    assert log.parent.is_dir()


def test_route_does_not_create_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(DC, "RUN_ROOT", tmp_path / "runs")
    result = MR.route("2026-12-01")
    assert not (tmp_path / "runs").exists()
    assert result["run_dir"] == str(tmp_path / "runs" / "20261201")


# ---------------------------------------------------------------- 6. 执行卡落盘
def test_persist_execution_card_writes_audit_file(tmp_path):
    """🔴 信号日的执行卡必须落盘（邮件正文会被排版吃掉，结构化件才可追溯）。"""
    card = {"orders": [{"code": "512800", "action": "卖出"}], "post_trade": {"cash": 18215.70}}
    path = AR.persist_execution_card(card, tmp_path, "2026-12-01")

    assert path.name == "execution_card_20261201.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["orders"][0]["code"] == "512800"
    assert payload["post_trade"]["cash"] == 18215.70


def test_persist_execution_card_creates_run_dir(tmp_path):
    """落盘时 run 目录不存在 ⇒ 自动建（不该抛 FileNotFoundError）。"""
    target = tmp_path / "runs" / "20261201"
    path = AR.persist_execution_card({"orders": []}, target, "2026-12-01")
    assert path.exists()
    assert target.is_dir()
