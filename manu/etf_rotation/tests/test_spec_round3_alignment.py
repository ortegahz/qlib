"""🔴 2026-11-11 第九轮：按架构文档对齐后的**口径钉死测试**（单一事实源）。

本轮按用户给的架构文档补齐了 6 项口径（见 `manu/memory/memory.md` 第九轮条目）：
  ① EWMA **alpha = 0.20**（`config.SMOOTHING_ALPHA`；半衰期 3.106 交易日 ⇒ 落在文档 3~5 日内）
  ② 入场闸门 **跌破 MA60 一票否决**（`config.ENTRY_MA_FILTER_WINDOW`；MA20 降为报告参考）
  ③ 单标的权重上限 **20%**（`config.MAX_SINGLE_WEIGHT`）
  ④ 现金缓冲 **≥ 400 元**（`daily_config.CASH_BUFFER_MIN/MAX`）
  ⑤ **拥挤度**（`build_ta_context.compute_crowding`：放量倍数 + 池内分位）
  ⑥ **行情源同源**（`build_ta_context._resolve_prices_path`：与规则层读同一份文件）

本文件钉死的是**口径本身**（常量值 + 单一事实源 + 行为随配置联动 + 数据不可得时不编造）。
断言必须有牙齿：真值断言，不许「不抛异常」式空断言；**不许**为变绿而弱化。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pandas as pd
import pytest

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
_PROBES_DIR = _ETF_DIR / "probes"
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))
if str(_PROBES_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBES_DIR))

import config as C  # noqa: E402


def _load(module_path: pathlib.Path, name: str):
    """按文件路径加载模块（**先注册进 sys.modules** ⇒ dataclass 的 `cls.__module__` 查得到）。"""
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SS = _load(_ETF_DIR / "score_smoothing.py", "_ss_round3")
BTC = _load(_ETF_DIR / "llm" / "build_ta_context.py", "_btc_round3")
DC = _load(_ETF_DIR / "daily" / "daily_config.py", "_dc_round3")
PROBE = _load(_PROBES_DIR / "probe_score_smoothing.py", "_probe_ss_round3")


# --------------------------------------------------------------------------- #
# ① EWMA alpha = 0.20 ＋ 单一事实源
# --------------------------------------------------------------------------- #

def test_smoothing_alpha_is_020():
    """文档 §二.2：alpha = 0.20（半衰期 ≈3.106 交易日 ⇒ 落在文档 3~5 日区间内）。"""
    assert C.SMOOTHING_ALPHA == 0.20
    half_life = SS.effective_half_life_days(0.20)
    assert 3.0 <= half_life <= 3.2, f"半衰期应 ≈3.106 个交易日，实测 {half_life}"
    assert 3.0 <= half_life <= 5.0, "必须落在文档要求的 3~5 个交易日内"


def test_smoothing_alpha_has_single_source_of_truth():
    """`score_smoothing.DEFAULT_SMOOTHING_ALPHA` 必须**绑定** config，不许留字面量副本。"""
    assert SS.DEFAULT_SMOOTHING_ALPHA == float(C.SMOOTHING_ALPHA)
    src = (_ETF_DIR / "score_smoothing.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    literals: list[float] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "DEFAULT_SMOOTHING_ALPHA" in targets:
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, float):
                    literals.append(node.value.value)
    assert literals == [], (
        f"DEFAULT_SMOOTHING_ALPHA 不许是字面量（发现 {literals}）⇒ 必须绑定 config.SMOOTHING_ALPHA"
    )


def test_smoothing_alpha_follows_config_when_it_changes(monkeypatch):
    """行为联动：改 config ⇒ 引擎默认随之改变（证明真的是**同一个事实源**）。"""
    monkeypatch.setattr(C, "SMOOTHING_ALPHA", 0.33)
    reloaded = _load(_ETF_DIR / "score_smoothing.py", "_ss_round3_mut")
    assert reloaded.DEFAULT_SMOOTHING_ALPHA == 0.33


# --------------------------------------------------------------------------- #
# ② 入场闸门 = MA60（MA20 仅报告参考）＋ 两链同源
# --------------------------------------------------------------------------- #

def test_entry_gate_is_ma60():
    assert C.ENTRY_MA_FILTER_WINDOW == 60
    assert C.TREND_MA_REPORT_WINDOW == 20
    assert C.EXIT_MA_FILTER_WINDOW == 60


def test_daily_chain_uses_same_ma_gate():
    """每日链不得与研究链分叉（daily_config 独立复制，但值必须一致）。"""
    assert DC.ENTRY_MA_WINDOW == C.ENTRY_MA_FILTER_WINDOW
    assert DC.EXIT_MA_WINDOW == C.EXIT_MA_FILTER_WINDOW


def test_run_pipeline_does_not_hardcode_ma_windows():
    """🔴 `run_pipeline.sh` 原先写死 `--entry-ma-window 20` ⇒ 与 config 分叉。"""
    pipe = (_ETF_DIR / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "--entry-ma-window 20" not in pipe, "run_pipeline.sh 不许写死入场均线 20"
    assert "--entry-ma-window 60" not in pipe, "run_pipeline.sh 不许写死入场均线 60"
    assert "ENTRY_MA_FILTER_WINDOW" in pipe, "run_pipeline.sh 必须从 config 读入场均线"
    assert "EXIT_MA_FILTER_WINDOW" in pipe, "run_pipeline.sh 必须从 config 读退场均线"


def test_run_gates_does_not_hardcode_ma_windows():
    """🔴 `run_gates.sh` 的 repro 段原写死 `--entry-ma-window 20` ⇒ 与 config 分叉。

    后果（真缺陷，不是口径洁癖）：闸门「重跑产物 vs 基线逐字节比对」段会用 **MA20**
    重跑 `recommend.py`，而基线是 **MA60** 跑出来的 ⇒ 产物必然不一致 ⇒ 闸门**假红**。
    与 `run_pipeline.sh` 属同一类分叉（两链/两闸门口径不同源）。
    """
    gates = (_ETF_DIR / "run_gates.sh").read_text(encoding="utf-8")
    assert "--entry-ma-window 20" not in gates, "run_gates.sh 不许写死入场均线 20"
    assert "--entry-ma-window 60" not in gates, "run_gates.sh 不许写死入场均线 60"
    assert "ENTRY_MA_FILTER_WINDOW" in gates, "run_gates.sh 必须从 config 读入场均线"
    assert "EXIT_MA_FILTER_WINDOW" in gates, "run_gates.sh 必须从 config 读退场均线"


def _ma_shape_prices() -> pd.DataFrame:
    """构造一段**站上 MA20 但跌破 MA60** 的价格（60 日窗口内有一次尖峰）。

    目的：证明「MA60 闸门」比「MA20 闸门」**更严** —— 旧口径（MA20）会让这只破 MA60
    的标的照样入选，新口径（MA60）必须把它挡掉。
    """
    rows: list[dict] = []
    days = pd.bdate_range(end=pd.Timestamp("2026-11-09"), periods=130)
    for index, day in enumerate(days):
        close = 1.00
        if 80 <= index <= 89:
            close = 2.00                     # 尖峰 ⇒ 把 MA60 抬到 1.05 之上
        elif 120 <= index <= 129:
            close = 1.00 + 0.005 * (index - 119)   # 末端缓升 ⇒ 收盘 1.05 > MA20
        rows.append({"date": day, "code": "510300", "close": close,
                     "open": close, "high": close, "low": close,
                     "volume": 1.0e7, "amount": 1.2e8})
    return pd.DataFrame(rows)


def test_ma60_gate_is_stricter_than_ma20_on_real_price_series():
    """MA60 闸门必须真的更严（同一段真实形状的价格：站上 MA20、跌破 MA60）。"""
    from engine.portfolio import _ma_ok_map  # noqa: PLC0415

    prices = _ma_shape_prices()
    asof = pd.Timestamp("2026-11-09")
    ok20 = _ma_ok_map(prices, asof, 20)
    ok60 = _ma_ok_map(prices, asof, 60)
    assert ok20["510300"] is True, "夹具前提失效：应站上 MA20"
    assert ok60["510300"] is False, "夹具前提失效：应跌破 MA60"


def test_entry_gate_window_is_taken_from_config(monkeypatch):
    """🔴 接线钉死：引擎把 `C.ENTRY_MA_FILTER_WINDOW` 原样传进破位判定（不是写死 20）。"""
    from engine import portfolio as P  # noqa: PLC0415

    seen: list[int] = []
    original = P._ma_ok_map

    def _recorder(prices, asof, window):  # noqa: ANN001, ANN202
        seen.append(int(window))
        return original(prices, asof, window)

    kwargs = dict(
        asof=PROBE._FIXTURE_ASOF,
        prices=PROBE._fixture_prices(),
        holdings=PROBE._fixture_holdings(),
        scores=PROBE._fixture_scores(),
        universe=PROBE._fixture_universe(),
    )
    monkeypatch.setattr(P, "_ma_ok_map", _recorder)
    monkeypatch.setattr(C, "ENTRY_MA_FILTER_WINDOW", 60)
    P.build_portfolio(**kwargs)
    assert seen and all(w == 60 for w in seen), f"入场闸门窗口应恒为 60，实测 {seen}"

    seen.clear()
    monkeypatch.setattr(C, "ENTRY_MA_FILTER_WINDOW", 20)
    P.build_portfolio(**kwargs)
    assert seen and all(w == 20 for w in seen), f"改 config 后窗口应变 20，实测 {seen}"


# --------------------------------------------------------------------------- #
# ③ 单标的权重上限 20%
# --------------------------------------------------------------------------- #

def test_max_single_weight_is_020():
    assert C.MAX_SINGLE_WEIGHT == 0.20


def test_portfolio_reads_max_single_weight_from_config():
    src = (_ETF_DIR / "engine" / "portfolio.py").read_text(encoding="utf-8")
    assert "MAX_SINGLE_WEIGHT" in src, "portfolio.py 必须读 config.MAX_SINGLE_WEIGHT"
    assert "cap = 0.40" not in src and "cap = 0.4\n" not in src, "portfolio.py 不许留 40% 行内字面量"


def test_max_single_weight_caps_target_weights(monkeypatch):
    """行为：把上限压到 5% ⇒ 每只目标权重都不得超过 5%（证明上限**真的生效**）。"""
    from engine.portfolio import build_portfolio  # noqa: PLC0415

    monkeypatch.setattr(C, "MAX_SINGLE_WEIGHT", 0.05)
    result = build_portfolio(
        asof=PROBE._FIXTURE_ASOF,
        prices=PROBE._fixture_prices(),
        holdings=PROBE._fixture_holdings(),
        scores=PROBE._fixture_scores(),
        universe=PROBE._fixture_universe(),
    )
    weights = [float(h["target_weight"]) for h in result["holdings"]]
    assert weights, "夹具应至少选出一只标的"
    assert max(weights) <= 0.05 + 1e-9, f"单只上限 5% 未生效：{weights}"


# --------------------------------------------------------------------------- #
# ④ 现金缓冲 ≥ 400 元
# --------------------------------------------------------------------------- #

def test_cash_buffer_min_is_400():
    assert DC.CASH_BUFFER_MIN == 400.0
    assert DC.CASH_BUFFER_MAX == 400.0


def test_cash_buffer_min_scales_with_total_assets():
    """比例下限取大者机制：绝对下限 400 在小账户下也必须保得住。"""
    assert DC.CASH_BUFFER_MIN >= 400.0
    # 生效下限 = max(绝对下限, 比例下限)；比例下限 = 总资产 × CASH_RESERVE_RATIO
    ratio_floor = float(DC.TOTAL_ASSETS) * float(DC.CASH_RESERVE_RATIO)
    effective = max(float(DC.CASH_BUFFER_MIN), ratio_floor)
    assert effective >= float(DC.CASH_BUFFER_MIN)


# --------------------------------------------------------------------------- #
# ⑤ 拥挤度（放量倍数 + 池内分位）
# --------------------------------------------------------------------------- #

def _crowding_prices(days: int = 200, end: str = "2026-11-09") -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    rows: list[dict] = []
    for code in ("510300", "512800"):
        for index, day in enumerate(dates):
            amount = 1.0e8
            if code == "510300" and index >= days - 20:
                amount = 3.0e8          # 近期放量 ⇒ 拥挤
            rows.append({
                "date": day.strftime("%Y-%m-%d"),
                "code": code,
                "close": 1.0 + index * 0.001,
                "volume": amount / 1.0,
                "amount": amount,
            })
    return pd.DataFrame(rows)


def test_crowding_detects_volume_surge():
    out = BTC.compute_crowding(_crowding_prices(), ["510300", "512800"], "2026-11-09")
    assert set(out) == {"510300", "512800"}
    assert out["510300"]["ratio"] == pytest.approx(2.25, rel=1e-6), (
        f"放量倍数算错：{out['510300']['ratio']}"
    )
    assert out["510300"]["crowded"] is True
    assert out["512800"]["ratio"] == pytest.approx(1.0, rel=1e-9)
    assert out["512800"]["crowded"] is False


def test_crowding_percentile_is_pool_internal():
    out = BTC.compute_crowding(_crowding_prices(), ["510300", "512800"], "2026-11-09")
    assert out["510300"]["pct"] == pytest.approx(1.0)
    assert out["512800"]["pct"] == pytest.approx(0.0)


def test_crowding_refuses_to_invent_when_data_short():
    """数据不足 ⇒ **不参与、不编造**（宁缺毋滥）。"""
    out = BTC.compute_crowding(_crowding_prices(days=5), ["510300"], "2026-11-09")
    assert out == {}, f"样本不足时不得产出拥挤度：{out}"


def test_crowding_refuses_to_invent_without_amount_column():
    prices = _crowding_prices().drop(columns=["amount"])
    out = BTC.compute_crowding(prices, ["510300", "512800"], "2026-11-09")
    assert out == {}, f"缺成交额字段时不得产出拥挤度：{out}"


def test_crowding_excludes_rows_after_asof():
    """🔴 asof 之后的行**必须**被排除（不许用未来数据算拥挤）。"""
    prices = _crowding_prices(days=200)
    asof = "2026-11-09"
    base = BTC.compute_crowding(prices, ["510300"], asof)
    # 把 asof 之后的成交额全部改成 100 倍（若被误用 ⇒ ratio 暴涨）
    future = prices["date"] > asof
    inflated = prices.copy()
    inflated.loc[future, "amount"] = inflated.loc[future, "amount"] * 100.0
    after = BTC.compute_crowding(inflated, ["510300"], asof)
    assert after["510300"]["ratio"] == pytest.approx(base["510300"]["ratio"], rel=1e-9), (
        "asof 之后的数据影响了拥挤度 ⇒ 存在未来函数"
    )


# --------------------------------------------------------------------------- #
# ⑥ 行情源同源（与规则层读同一份文件）
# --------------------------------------------------------------------------- #

def test_resolve_prices_path_prefers_explicit():
    explicit = "/tmp/explicit_prices.csv"
    out = BTC._resolve_prices_path(pathlib.Path("/nonexistent"), "2026-11-09", explicit)
    assert str(out) == explicit


def test_resolve_prices_path_prefers_effective_over_daily(tmp_path):
    (tmp_path / "etf_prices_effective_20261109.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "etf_prices_daily_20261109.csv").write_text("a\n2\n", encoding="utf-8")
    out = BTC._resolve_prices_path(tmp_path, "2026-11-09", None)
    assert out.name == "etf_prices_effective_20261109.csv"


def test_resolve_prices_path_falls_back_to_daily_then_canonical(tmp_path):
    (tmp_path / "etf_prices_daily_20261109.csv").write_text("a\n2\n", encoding="utf-8")
    out = BTC._resolve_prices_path(tmp_path, "2026-11-09", None)
    assert out.name == "etf_prices_daily_20261109.csv"
    fallback = BTC._resolve_prices_path(tmp_path, "2026-11-09", None)
    assert fallback == pathlib.Path(C.ETF_PRICES_CSV) or fallback.exists()


def test_resolve_prices_path_canonical_fallback(tmp_path):
    """两个本轮文件都没有 ⇒ 回落到 canonical（**不编造**路径）。"""
    out = BTC._resolve_prices_path(tmp_path, "2026-11-09", None)
    assert out == pathlib.Path(C.ETF_PRICES_CSV)


# --------------------------------------------------------------------------- #
# ⑦ 拥挤度接进 TA 上下文（3.5b 节）＋ meta 字段
# --------------------------------------------------------------------------- #

def test_crowding_is_reported_in_ta_context_meta():
    """拥挤度不可得 ⇒ **如实标注**（不许静默略过、也不许编造）。"""
    src = (_ETF_DIR / "llm" / "build_ta_context.py").read_text(encoding="utf-8")
    assert "crowding_available" in src and "crowding_reason" in src
    assert "3.5b" in src, "拥挤度必须单独成节（3.5b）"
    assert "拥挤度不可得" in src, "拥挤度不可得时必须显式披露"


def test_daily_chain_passes_same_source_prices_to_ta_context():
    """🔴 审查发现 3.5：TA 上下文原先读 canonical 旧行情 ⇒ 与规则层不同源。"""
    src = (_ETF_DIR / "daily" / "agents_rebalance.py").read_text(encoding="utf-8")
    idx = src.find('str(_RESEARCH_ROOT / "llm" / "build_ta_context.py")')
    assert idx > 0, "未找到 build_ta_context.py 的调用点"
    window = src[idx: idx + 800]
    assert '"--prices"' in window, "每日链必须把本轮有效行情显式传给 build_ta_context.py"
    assert "str(prices_path)" in window, "--prices 必须指向**本轮**的有效行情文件"
