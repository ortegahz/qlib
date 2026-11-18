"""🔴 急跌刹车（组合级回撤刹车）在**每日链**里必须**真的能触发**。

**真缺陷（2026-11-18 审计 · high 级 · `no_drawdown_brake`）**
    研究链里刹车是实现了的：
      ``config.DRAWDOWN_BRAKE_THRESHOLD``
      → ``analyze.py`` / ``recommend.py`` 收 ``--nav`` / ``--nav-peak``
        ⇒ 组装 ``nav_state``
      → ``engine/portfolio.py`` 的 ``brake_mode`` ⇒ **整个组合切防守资产**。
    但**每日链从不传这两个参数** ⇒ ``nav_state`` 恒为 ``None``
    ⇒ ``in_brake`` 恒为 ``False`` ⇒ **刹车永远不触发**。

**本文件钉死**
    1. 净值 = 现金 + 持仓按 asof 收盘价重估（取不到价 ⇒ 用快照市值兜底 + 披露）；
    2. 峰值**跨日持久化**（状态文件）⇒ 回撤 = nav/peak − 1；
    3. 回撤 ≥ 阈值 ⇒ ``in_brake=True``（**刹车能触发**）；
    4. 每日链把 ``--nav`` / ``--nav-peak`` **真的传**给 analyze.py / recommend.py；
    5. **变异反证**：把阈值改成 0 ⇒ 判定必须转成 ``in_brake=True``；
       把 ``_brake_args`` 的返回改成 ``[]`` ⇒ 参数传递断言必须转红。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_DAILY = Path(__file__).resolve().parents[1]
if str(_DAILY) not in sys.path:
    sys.path.insert(0, str(_DAILY))

import agents_rebalance as AR  # noqa: E402
import daily_config as DC  # noqa: E402
import nav_state as NS  # noqa: E402


def _account(cash: float = 1000.0, shares: int = 1000, mv: float = 1000.0,
             total: float = 2000.0) -> dict:
    return {
        "total_assets": total, "cash": cash,
        "positions": [{"code": "512800", "shares": shares, "market_value": mv}],
    }


def _prices_csv(tmp_path: Path, rows: list[tuple[str, str, float]]) -> Path:
    p = tmp_path / "etf_prices.csv"
    pd.DataFrame([{"date": d, "code": c, "close": v} for d, c, v in rows]).to_csv(p, index=False)
    return p


# ----------------------------------------------------------------- 净值
def test_净值按asof收盘价重估(tmp_path: Path) -> None:
    """🔴 净值 = 现金 + Σ(股数 × asof 收盘价)。"""
    px = _prices_csv(tmp_path, [("2026-11-10", "512800", 1.25)])
    out = NS.nav_from_account(_account(cash=1000.0, shares=1000, mv=1000.0),
                              px, "2026-11-10")
    assert out["nav"] == pytest.approx(1000.0 + 1250.0)
    assert out["n_priced"] == 1 and out["n_unpriced"] == 0


def test_取不到价时用快照市值兜底并披露(tmp_path: Path) -> None:
    """🔴 没有当日价 ⇒ 用账户快照市值兜底，**不许编价**，且必须计入 n_unpriced。"""
    px = _prices_csv(tmp_path, [("2026-11-10", "999999", 1.0)])
    out = NS.nav_from_account(_account(cash=1000.0, shares=1000, mv=1000.0),
                              px, "2026-11-10")
    assert out["nav"] == pytest.approx(2000.0)          # 1000 现金 + 1000 快照市值
    assert out["n_unpriced"] == 1 and out["n_priced"] == 0
    assert "兜底" in str(out["note"])


def test_账户上下文为空时算不出净值() -> None:
    """🔴 既无现金也无持仓 ⇒ nav=None（⇒ 刹车不启用，不编数字）。"""
    out = NS.nav_from_account({}, None, "2026-11-10")
    assert out["nav"] is None


# ----------------------------------------------------------------- 峰值 / 回撤 / 刹车
def test_峰值跨日持久化且回撤触发刹车(tmp_path: Path, monkeypatch) -> None:
    """🔴 峰值来自**跨日序列** ⇒ 回撤 ≥ 阈值 ⇒ ``in_brake=True``。"""
    monkeypatch.setattr(DC, "DRAWDOWN_BRAKE_ENABLED", True)
    monkeypatch.setattr(DC, "DRAWDOWN_BRAKE_THRESHOLD", 0.15)
    monkeypatch.setattr(DC, "ACCOUNT_SNAPSHOT_DATE", "2026-11-05")
    state = tmp_path / "nav_history.json"
    px = _prices_csv(tmp_path, [("2026-11-10", "512800", 2.0),
                                ("2026-11-11", "512800", 1.0)])
    acct = _account(cash=1000.0, shares=1000, mv=2000.0, total=3000.0)

    # 第一天：净值 1000 + 1000×2.0 = 3000（峰值 3000）
    d1 = NS.prepare_daily_nav(asof="2026-11-10", account=acct, prices_csv=px,
                              notes=[], path=state)
    assert d1["nav"] == pytest.approx(3000.0)
    assert d1["peak"] == pytest.approx(3000.0)
    assert d1["drawdown"] == pytest.approx(0.0)
    assert d1["in_brake"] is False

    # 第二天：净值 1000 + 1000×1.0 = 2000 ⇒ 回撤 −33% ⇒ 刹车触发
    d2 = NS.prepare_daily_nav(asof="2026-11-11", account=acct, prices_csv=px,
                              notes=[], path=state)
    assert d2["nav"] == pytest.approx(2000.0)
    assert d2["peak"] == pytest.approx(3000.0)
    assert d2["drawdown"] == pytest.approx(-1.0 / 3.0)
    assert d2["in_brake"] is True, "🔴 回撤 33% > 阈值 15% ⇒ 刹车必须触发"
    assert d2["n_points"] >= 2

    # 状态文件真的落盘了（峰值必须能跨日）
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "2026-11-10" in saved["points"] and "2026-11-11" in saved["points"]


def test_种子用账户快照总资产(tmp_path: Path, monkeypatch) -> None:
    """🔴 第一天的峰值不能被人为压低 ⇒ 用账户快照总资产做种子。"""
    monkeypatch.setattr(DC, "DRAWDOWN_BRAKE_ENABLED", True)
    monkeypatch.setattr(DC, "ACCOUNT_SNAPSHOT_DATE", "2026-11-05")
    state = tmp_path / "nav_history.json"
    px = _prices_csv(tmp_path, [("2026-11-10", "512800", 0.5)])
    acct = _account(cash=1000.0, shares=1000, mv=500.0, total=5000.0)
    out = NS.prepare_daily_nav(asof="2026-11-10", account=acct, prices_csv=px,
                               notes=[], path=state)
    assert out["peak"] == pytest.approx(5000.0), "🔴 峰值必须 ≥ 账户快照总资产"


def test_开关关闭时不算净值不传参(tmp_path: Path, monkeypatch) -> None:
    """🔴 开关关 ⇒ nav=None、reason=disabled_by_config（绝不传 --nav）。"""
    monkeypatch.setattr(DC, "DRAWDOWN_BRAKE_ENABLED", False)
    out = NS.prepare_daily_nav(asof="2026-11-10", account=_account(),
                               prices_csv=None, notes=[], path=tmp_path / "s.json")
    assert out["nav"] is None
    assert out["enabled"] is False
    assert out["reason"] == "disabled_by_config"


def test_变异反证_阈值改零必须转成刹车(tmp_path: Path, monkeypatch) -> None:
    """🔴 变异反证：阈值改成 0 ⇒ 任何回撤都必须判「刹车」。"""
    monkeypatch.setattr(DC, "DRAWDOWN_BRAKE_ENABLED", True)
    monkeypatch.setattr(DC, "DRAWDOWN_BRAKE_THRESHOLD", 0.0)
    monkeypatch.setattr(DC, "ACCOUNT_SNAPSHOT_DATE", "2026-11-05")
    state = tmp_path / "nav_history.json"
    px = _prices_csv(tmp_path, [("2026-11-10", "512800", 2.0),
                                ("2026-11-11", "512800", 1.9)])
    acct = _account(cash=1000.0, shares=1000, mv=2000.0, total=3000.0)
    NS.prepare_daily_nav(asof="2026-11-10", account=acct, prices_csv=px,
                          notes=[], path=state)
    d2 = NS.prepare_daily_nav(asof="2026-11-11", account=acct, prices_csv=px,
                              notes=[], path=state)
    assert d2["drawdown"] < 0
    assert d2["in_brake"] is True, "🔴 阈值 0 ⇒ 回撤即刹车（变异反证失效）"


# ----------------------------------------------------------------- 每日链真的传参
def test_每日链把nav参数传给子进程() -> None:
    """🔴 ``--nav`` / ``--nav-peak`` 必须真的出现在 analyze.py / recommend.py 的命令行里。"""
    args = AR._brake_args(1234.5, 2000.0)
    assert args == ["--nav", "1234.500000", "--nav-peak", "2000.000000"]


def test_净值算不出来时不传参() -> None:
    """🔴 算不出净值 ⇒ **不传**（保持原行为，绝不编数字）。"""
    assert AR._brake_args(None, None) == []
    assert AR._brake_args(100.0, None) == []
    assert AR._brake_args(None, 100.0) == []


def test_变异反证_不传参数时刹车断言必须转红() -> None:
    """🔴 变异反证：把 nav 置空 ⇒ 必须拿不到 --nav（否则等于没修）。"""
    nav_info = {"nav": 1000.0, "peak": 1200.0}
    assert AR._brake_args(nav_info.get("nav"), nav_info.get("peak")) != []
    assert AR._brake_args(None, nav_info.get("peak")) == []


# --------------------------------------------------------------------------- #
# 🔴 2026-11-18 续四：**链级证据** —— 急跌刹车触发时，引擎**真的**把组合切防守
# --------------------------------------------------------------------------- #
# 前 5 条钉死的是「刹车**能**触发」（nav_state / 参数传递）。
# 本节钉死的是**触发之后的行为**：``in_brake=True`` ⇒ 引擎必须
#   ① 落 ``brake_mode=True``；② **只选防守资产**（黄金 / 国债 / 货币）；
#   ③ **不许退化成空仓**（空仓 = 100% 现金，不是「切防守」）。
#
# 🔴 为什么 ③ 也要钉：``POOL_MODE="e_covered"`` 是**严格口径**
#    （``POOL_ALLOW_DEFENSIVE_BYPASS=False`` ⇒ coverage 不达标的**防守资产**也剔）
#    ⇒ 若防守资产没有 A 股穿透（黄金 / 国债 ETF 本来就没有）⇒ 刹车档可能**一只都选不出**
#    ⇒ 组合变**空仓** ⇒ 「急跌 ⇒ 切防守」的承诺落空（空仓 ≠ 切防守）。
#    ⇒ 这条守卫是**行为级**的：不许靠「不抛异常」蒙混过关。

_ETF_DIR = _DAILY.parent
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import config as C  # noqa: E402

#: 夹具 ETF：3 只权益 + 2 只防守（黄金 518880 / 国债 511010）
_BRAKE_ETFS: tuple[tuple[str, str, float, float, int, float, str], ...] = (
    ("510300", "样本甲ETF", 4.0, 0.0006, 37, 0.0, "equity_cn"),
    ("510500", "样本乙ETF", 6.0, 0.0008, 53, 1.1, "equity_cn"),
    ("512100", "样本丙ETF", 2.0, 0.0010, 71, 2.3, "equity_cn"),
    ("518880", "黄金ETF", 5.0, 0.0004, 41, 0.7, "commodity"),
    ("511010", "国债ETF", 110.0, 0.0001, 61, 2.0, "bond"),
)

#: 成分股（**只有权益 ETF 有** ⇒ 防守资产 coverage = 0 ⇒ 严格口径下会被剔）
_BRAKE_HOLDINGS: tuple[tuple[str, str, str, float], ...] = (
    ("510300", "样本甲ETF", "SH600000", 60.0),
    ("510300", "样本甲ETF", "SZ000001", 40.0),
    ("510500", "样本乙ETF", "SH600519", 50.0),
    ("510500", "样本乙ETF", "SZ000002", 50.0),
    ("512100", "样本丙ETF", "SH601318", 70.0),
    ("512100", "样本丙ETF", "SZ000651", 30.0),
)

_BRAKE_ASOF = pd.Timestamp("2026-11-06")


def _brake_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """夹具：``(prices, holdings, scores, universe)``（**纯内存**，不读生产 CSV）。

    🔴 行情天数**由 config 派生**（``MIN_HISTORY_DAYS`` + 10）⇒ 配置漂移时夹具不失效。
    """
    import math

    n_days = max(int(C.MIN_HISTORY_DAYS) + 10, 130)
    days = list(pd.bdate_range(end=_BRAKE_ASOF, periods=n_days))
    rows: list[dict] = []
    for code, _name, base, drift, period, phase, _bucket in _BRAKE_ETFS:
        for index, day in enumerate(days):
            close = (
                base
                * (1 + drift * index)
                * (1 + 0.002 * math.sin(2 * math.pi * index / period + phase))
            )
            rows.append({
                "date": day, "code": code, "open": close, "high": close * 1.002,
                "low": close * 0.998, "close": close, "volume": 1.0e7, "amount": 1.2e8,
            })
    prices = pd.DataFrame(rows)
    universe = pd.DataFrame([
        {"code": code, "name": name, "avg_amount_20d": 1.2e8, "float_mcap": 1.0e10,
         "last_price": 3.0, "fund_type_bucket": bucket}
        for code, name, *_rest, bucket in _BRAKE_ETFS
    ])
    holdings = pd.DataFrame(
        list(_BRAKE_HOLDINGS),
        columns=["etf_code", "etf_name", "instrument", "weight_pct"],
    )
    instruments = sorted({row[2] for row in _BRAKE_HOLDINGS})
    score_rows: list[dict] = []
    for step, day in enumerate(days[-3:]):
        for j, instrument in enumerate(instruments):
            score_rows.append({
                "date": day, "instrument": instrument, "score": 1.0 + 0.3 * step - 0.1 * j,
            })
    scores = pd.DataFrame(score_rows)
    return prices, holdings, scores, universe


def _run_engine(*, in_brake: bool, pool_mode: str = "e_covered"):
    """跑一次 ``build_portfolio``（**V2 冠军口径**：闸门 off / 不平滑）。"""
    from engine.portfolio import build_portfolio

    prices, holdings, scores, universe = _brake_inputs()
    nav_state = {
        "nav": 500.0 if in_brake else 1000.0,
        "peak": 1000.0,
        "in_brake": bool(in_brake),
        "cooldown_left": 0,
    }
    return build_portfolio(
        asof=_BRAKE_ASOF, prices=prices, holdings=holdings, scores=scores,
        universe=universe, pool_mode=pool_mode, nav_state=nav_state,
        entry_ma_window=0, exit_ma_window=0,
    )


def test_急跌刹车触发时引擎只选防守资产() -> None:
    """🔴 刹车态 ⇒ ``brake_mode=True`` ＋ 入选**全是防守资产** ＋ **不许空仓**。"""
    res = _run_engine(in_brake=True)
    assert res["brake_mode"] is True, "刹车态必须落进产物（brake_mode）"
    codes = [str(h["code"]) for h in res["holdings"]]
    assert codes, (
        "🔴 刹车档必须选出**防守资产**；空仓（0 只）= 退化成 100% 现金，"
        "不是「整个组合切防守资产」⇒ 查 POOL_MODE 严格口径是否把防守资产剔掉了"
    )
    assert set(codes) <= set(C.DEFENSIVE_CODES), (
        f"刹车档只许选防守资产 {tuple(C.DEFENSIVE_CODES)}，实得 {codes}"
    )


def test_不刹车时权益资产照常入选_对照组() -> None:
    """🔴 对照组：不刹车的同一夹具**必须**能选出权益资产 ⇒ 证明上一条不是「本来就选不出」。"""
    res = _run_engine(in_brake=False)
    assert res["brake_mode"] is False
    codes = [str(h["code"]) for h in res["holdings"]]
    assert codes, "对照组：夹具本身必须能选出标的"
    assert any(code not in set(C.DEFENSIVE_CODES) for code in codes), (
        f"对照组：权益资产应能入选，实得 {codes}"
    )


def test_变异反证_刹车态不许退回权益资产() -> None:
    """🔴 变异反证：把 ``in_brake`` 翻成 False ⇒ 上面那条断言必须转红。

    ⇒ 证明「只选防守资产」这条断言**真的咬住了刹车开关**，不是恒真。
    """
    res = _run_engine(in_brake=False)
    codes = [str(h["code"]) for h in res["holdings"]]
    assert not (codes and set(codes) <= set(C.DEFENSIVE_CODES)), (
        "变异反证失效：不刹车时竟然全是防守资产 ⇒ 断言恒真，没有牙齿"
    )
