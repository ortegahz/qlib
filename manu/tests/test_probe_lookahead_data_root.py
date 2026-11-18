"""🔴 2026-11-10 新增：快照口径下，**个股分数 / 个股日线**必须从**同一个快照目录**读。

【背景（服务器实测红灯）】
    ``probe_lookahead_snapshot`` 阶段红灯：
    ``ValueError: 2026-11-05 个股分数为空，无法复算 E 上界``
    根因：``--snapshot-root`` 只把**报告**切到快照目录，个股分数仍读 canonical 根目录
    ⇒ 断言对象（快照）与数据（canonical）**不同源** ⇒ asof 过滤后为空 ⇒ 崩。

【口径（本文件锁住的行为）】
  ① 给了 ``--snapshot-root`` / ``--snapshot`` 且**成功选中快照**
     ⇒ 数据根目录 = **快照目录**（快照自包含：契约 16 件里就有这两个 csv）；
  ② 快照里缺 ``stock_scores.csv`` / ``stock_prices.csv``
     ⇒ **退出码 2「不适用」**（≠ 通过），**不许**回落 canonical 再报「通过」；
  ③ 不给这两个参数 ⇒ 数据根目录仍是 canonical ``RUN_ROOT``（基线逐字节不变）。

【夹具】全部自造（``tmp_path``），**不依赖服务器路径**：
``ETF_DATA`` / ``RUN_ROOT`` 一律 monkeypatch 到临时目录。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

from manu.etf_rotation.probes import probe_lookahead  # type: ignore  # noqa: E402

#: 夹具用的信号日
ASOF = "2026-11-03"
#: 夹具里的 ETF 代码（与 etf_holdings.csv 一致）
ETF_CODE = "510300"

#: 三只成分股（权重 50 / 30 / 20）⇒ E 的 z 加权均值可**独立**手算复核
HOLDINGS_CSV = """etf_code,stock_code,stock_exchange,weight_pct,fetch_date
510300,600000,SH,50,2026-11-04
510300,000001,SZ,30,2026-11-04
510300,600519,SH,20,2026-11-04
"""

ETF_PRICES_CSV = """date,close
2026-11-02,4.0
2026-11-03,4.1
"""

ETF_UNIVERSE_CSV = """etf_code,fetch_date
510300,2026-11-04
"""

CALENDAR_CSV = """date
2026-11-02
2026-11-03
"""

#: 快照里的个股分数：**最晚日期 2026-11-04**（> asof ⇒ 会进台账），
#: 当日分数 = 1 / 2 / 4（⇒ z 形态与 canonical 的 1 / 2 / 3 **不同** ⇒ E 值可区分）
SNAPSHOT_SCORES_CSV = """instrument,date,score
SH600000,2026-11-03,1.0
SZ000001,2026-11-03,2.0
SH600519,2026-11-03,4.0
SH600000,2026-11-04,1.0
"""

#: canonical（根目录）里的个股分数：最晚日期 **2026-12-31**（⇒ 与快照可区分）
CANONICAL_SCORES_CSV = """instrument,date,score
SH600000,2026-11-03,1.0
SZ000001,2026-11-03,2.0
SH600519,2026-11-03,3.0
SH600000,2026-12-31,1.0
"""

SNAPSHOT_PRICES_CSV = """instrument,date,close
SH600000,2026-11-03,10.0
SH600000,2026-11-04,10.5
"""

CANONICAL_PRICES_CSV = """instrument,date,close
SH600000,2026-11-03,10.0
SH600000,2026-12-31,11.0
"""

MD_TEXT = """# 夹具报告

## 八之四、前视审计

夹具：没有真实披露。

## 九、风险披露

夹具：没有真实披露。
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_etf_data(root: Path) -> Path:
    """ETF_DATA 夹具（数据集目录：``etf_holdings`` / ``etf_prices`` / ``etf_universe``）。"""
    _write(root / "etf_holdings.csv", HOLDINGS_CSV)
    _write(root / "etf_prices.csv", ETF_PRICES_CSV)
    _write(root / "etf_universe.csv", ETF_UNIVERSE_CSV)
    _write(root / "trade_calendar.csv", CALENDAR_CSV)
    return root


def _make_canonical(root: Path) -> Path:
    """canonical（根目录）夹具：个股分数 / 个股日线 / 报告。"""
    _write(root / "stock_scores.csv", CANONICAL_SCORES_CSV)
    _write(root / "stock_prices.csv", CANONICAL_PRICES_CSV)
    _write(root / "etf_analysis_report.json",
           json.dumps(_report(), ensure_ascii=False))
    _write(root / "etf_analysis_report.md", MD_TEXT)
    return root


def _make_snapshot(root: Path, name: str, *, with_data: bool = True) -> Path:
    """快照夹具（manifest + 报告 + 个股分数/日线）。"""
    snap = root / name
    _write(snap / "snapshot_manifest.json",
           json.dumps({"schema_version": 1, "run_id": name}, ensure_ascii=False))
    _write(snap / "etf_analysis_report.json",
           json.dumps(_report(), ensure_ascii=False))
    _write(snap / "etf_analysis_report.md", MD_TEXT)
    if with_data:
        _write(snap / "stock_scores.csv", SNAPSHOT_SCORES_CSV)
        _write(snap / "stock_prices.csv", SNAPSHOT_PRICES_CSV)
    return snap


def _report() -> dict:
    """一份**结构完整**但内容是夹具的报告（够跑完整个闸门，不会在负例里抛异常）。"""
    pair = {"selected": "510300", "challenger": "510500", "final_gap": 0.02,
            "swap_threshold_realistic": 0.12, "margin_realistic": -0.1,
            "margin_extreme": -0.2}
    return {
        "meta": {"asof": ASOF},
        "lookahead_audit": {
            "inputs": {
                "asof": ASOF,
                "n_look_ahead": 1,
                "blocking": [{"name": "etf_holdings.csv",
                              "effective_date": "2026-11-04"}],
                "entries": [{"name": "etf_holdings.csv", "role": "decision",
                             "look_ahead": True, "effective_date": "2026-11-04"}],
                "liquidity_fallback": {"n_need_fallback": 0,
                                       "n_need_fallback_pass_liq": 0,
                                       "inert": True},
            },
            "w_snapshot": {"snapshot_date": "2026-11-04", "n_snapshot_dates": 1,
                           "gap_days": 1, "fixable": False,
                           "unobservable_reason": "夹具：W 成分快照不可观测"},
            "e_bound": {
                "per_etf": [{"code": ETF_CODE, "e_score": 0.123456,
                             "delta_e_extreme": 0.5,
                             "delta_e_small_weight": 0.25}],
                "max_delta_e_realistic": 0.5,
                "max_delta_e_extreme": 0.5,
            },
            "flip_test": {
                "max_same_theme_margin_realistic": -0.1,
                "max_same_theme_margin_extreme": -0.2,
                "max_cross_theme_margin_realistic": None,
                "max_cross_theme_margin_extreme": None,
                "conclusion": "夹具结论",
                "same_theme_gaps": [dict(pair)],
                "same_theme_closest_by_gap": [dict(pair)],
                "same_theme_tightest_realistic": [dict(pair)],
                "same_theme_tightest_extreme": [dict(pair)],
                "same_theme_challengers": [dict(pair)],
            },
        },
        "risk_disclosure": [
            {"id": "in_sample_selection_bias", "detail": "夹具"},
            {"id": "current_drawdown_position", "detail": "夹具"},
            {"id": "w_snapshot_bias", "detail": "夹具：E 上界 0.5（已量化）"},
            {"id": "pool_attribute_limits", "detail": "夹具"},
            {"id": "turnover_and_cost", "detail": "夹具"},
            {"id": "non_lookthrough_etf", "detail": "夹具"},
            {"id": "strategy_capacity", "detail": "夹具"},
        ],
    }


def _item_date(ledger: dict, name: str) -> str | None:
    """从台账里取某个输入件的有效日期。"""
    for item in ledger.get("items") or []:
        if item.get("name") == name:
            return str(item.get("effective_date"))
    return None


def _expected_e(scores: list[float], weights: list[float]) -> float:
    """**独立**手算 E（z 加权均值）⇒ 不复用被测实现。"""
    mean = sum(scores) / len(scores)
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    sd = math.sqrt(var)
    z = [(s - mean) / sd for s in scores]
    return sum(w * zi for w, zi in zip(weights, z)) / sum(weights)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """一套完整夹具：ETF_DATA / canonical 根目录 / 快照根。"""
    etf_data = _make_etf_data(tmp_path / "etf_data")
    canonical = _make_canonical(tmp_path / "canonical")
    snap_root = tmp_path / "snapshots"
    snap_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(probe_lookahead, "ETF_DATA", etf_data)
    monkeypatch.setattr(probe_lookahead, "RUN_ROOT", canonical)
    return {"etf_data": etf_data, "canonical": canonical, "snap_root": snap_root}


# --------------------------------------------------------------------------- #
# ① 给了快照根 ⇒ 个股分数 / 个股日线从**快照目录**读
# --------------------------------------------------------------------------- #

def test_ledger_reads_stock_files_from_snapshot_dir(world) -> None:
    """``independent_ledger(data_root=快照)`` ⇒ 台账里的个股分数/日线日期是**快照那份**。"""
    snap = _make_snapshot(world["snap_root"], "20261109_174953")

    snap_ledger = probe_lookahead.independent_ledger(
        probe_lookahead.pd.Timestamp(ASOF), data_root=snap)
    canon_ledger = probe_lookahead.independent_ledger(
        probe_lookahead.pd.Timestamp(ASOF))

    # 快照那份：最晚日期 2026-11-04
    assert _item_date(snap_ledger, "stock_scores.csv") == "2026-11-04"
    assert _item_date(snap_ledger, "stock_prices.csv") == "2026-11-04"
    # canonical 那份：最晚日期 2026-12-31（**没被**快照口径读到）
    assert _item_date(canon_ledger, "stock_scores.csv") == "2026-12-31"
    assert _item_date(canon_ledger, "stock_prices.csv") == "2026-12-31"


def test_e_bounds_use_snapshot_stock_scores(world) -> None:
    """``independent_e_bounds(data_root=快照)`` ⇒ E 用的是**快照那份**个股分数。"""
    snap = _make_snapshot(world["snap_root"], "20261109_174953")

    snap_eb = probe_lookahead.independent_e_bounds(
        probe_lookahead.pd.Timestamp(ASOF), [ETF_CODE], data_root=snap)
    canon_eb = probe_lookahead.independent_e_bounds(
        probe_lookahead.pd.Timestamp(ASOF), [ETF_CODE])

    expected = _expected_e([1.0, 2.0, 4.0], [50.0, 30.0, 20.0])
    assert snap_eb[ETF_CODE]["e_score"] == pytest.approx(expected, abs=1e-9)
    assert snap_eb[ETF_CODE]["n_scored"] == 3
    # canonical 的分数（1 / 2 / 3）⇒ E 值**不同** ⇒ 证明数据源确实切了
    assert canon_eb[ETF_CODE]["e_score"] == pytest.approx(
        _expected_e([1.0, 2.0, 3.0], [50.0, 30.0, 20.0]), abs=1e-9)
    assert snap_eb[ETF_CODE]["e_score"] != pytest.approx(
        canon_eb[ETF_CODE]["e_score"], abs=1e-9)


def test_main_snapshot_root_switches_data_root(world, monkeypatch, capsys) -> None:
    """``--snapshot-root`` ⇒ 主流程把**数据根目录**也切到快照目录（真读到快照那份）。"""
    snap = _make_snapshot(world["snap_root"], "20261109_174953")
    seen: dict[str, object] = {}

    real_ledger = probe_lookahead.independent_ledger
    real_e_bounds = probe_lookahead.independent_e_bounds

    def _spy_ledger(asof, data_root=None):
        seen["ledger_root"] = data_root
        out = real_ledger(asof, data_root=data_root)
        seen["ledger"] = out
        return out

    def _spy_e_bounds(asof, codes=None, data_root=None):
        seen["e_bounds_root"] = data_root
        return real_e_bounds(asof, codes, data_root=data_root)

    monkeypatch.setattr(probe_lookahead, "independent_ledger", _spy_ledger)
    monkeypatch.setattr(probe_lookahead, "independent_e_bounds", _spy_e_bounds)
    # 🔴 N10 MD 级真负例要往服务器持久盘（唯一落点根）写临时目录 ⇒ 本机没有 ⇒ 用守卫跳过
    monkeypatch.setenv(probe_lookahead.ENV_SKIP_MD_NEG, "1")
    monkeypatch.setattr(sys, "argv",
                        ["probe_lookahead.py", "--snapshot-root", str(world["snap_root"])])

    rc = probe_lookahead.main()
    out = capsys.readouterr().out

    assert seen["ledger_root"] == snap, "数据根目录必须是**快照目录**"
    assert seen["e_bounds_root"] == snap, "数据根目录必须是**快照目录**"
    # 真读到的台账里，个股分数/日线的日期是**快照那份**（不是 canonical 的 2026-12-31）
    ledger = seen["ledger"]
    assert _item_date(ledger, "stock_scores.csv") == "2026-11-04"
    assert _item_date(ledger, "stock_prices.csv") == "2026-11-04"
    assert "[快照口径]" in out
    assert str(snap) in out
    # 夹具报告过不了金标 ⇒ 闸门**判红**（rc=1）：证明它**进了断言**而不是被跳过
    assert rc == 1, f"夹具报告应判红(1)，实得 {rc}"


def test_main_without_snapshot_keeps_canonical_root(world, monkeypatch, capsys) -> None:
    """不给快照参数 ⇒ 数据根目录**仍是 canonical**（基线护栏逐字节不变）。"""
    _make_snapshot(world["snap_root"], "20261109_174953")
    seen: dict[str, object] = {}
    real_ledger = probe_lookahead.independent_ledger

    def _spy_ledger(asof, data_root=None):
        seen["ledger_root"] = data_root
        return real_ledger(asof, data_root=data_root)

    monkeypatch.setattr(probe_lookahead, "independent_ledger", _spy_ledger)
    monkeypatch.setenv(probe_lookahead.ENV_SKIP_MD_NEG, "1")
    monkeypatch.setattr(sys, "argv", ["probe_lookahead.py"])

    rc = probe_lookahead.main()
    out = capsys.readouterr().out

    assert seen["ledger_root"] == world["canonical"], "基线路径必须读 canonical 根目录"
    assert "快照口径" not in out
    assert rc == 1, f"夹具报告应判红(1)，实得 {rc}"


# --------------------------------------------------------------------------- #
# ② 快照里缺 stock_scores.csv ⇒ rc2「不适用」（**不是** 0，**也不许**回落 canonical）
# --------------------------------------------------------------------------- #

def test_main_snapshot_missing_stock_scores_is_not_applicable(world, monkeypatch, capsys) -> None:
    """快照缺个股分数 ⇒ **rc2「不适用」**，不许回落 canonical 再报「通过」。"""
    _make_snapshot(world["snap_root"], "20261109_174953", with_data=False)
    called: list[object] = []

    def _spy(asof, data_root=None):
        called.append(data_root)
        return {"n_decision_lookahead": 0, "items": [], "non_decision": []}

    monkeypatch.setattr(probe_lookahead, "independent_ledger", _spy)
    monkeypatch.setattr(sys, "argv",
                        ["probe_lookahead.py", "--snapshot-root", str(world["snap_root"])])

    rc = probe_lookahead.main()
    out = capsys.readouterr().out

    assert rc == 2, f"缺个股分数必须判「不适用」(2)，实得 {rc}"
    assert rc != 0, "「不适用」≠「通过」"
    assert called == [], f"不许回落 canonical 读个股分数，实得 {called}"
    assert "不适用" in out
    assert "回落" in out
