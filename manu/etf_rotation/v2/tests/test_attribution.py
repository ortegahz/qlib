"""🔴 V2 **ETF 层归因工具**（``v2/attribution.py``）的钉死测试。

【本文件钉死（自带夹具，不碰真实行情 / 不联网）】

  ① ``etf_forward_return_panel``：**前瞻收益按交易日行数**算，尾部 ``h`` 行**天然缺失**
     （**不许**用未来数据回填）；
  ② ``etf_rank_ic``：**ETF 层面**逐日截面 Spearman ⇒ 均值 / ICIR / t 值，
     样本不足（< 2 个有效截面）⇒ **全 None**（不许编）；
  ③ ``rebalance_decomposition``：「选中组合 vs 候选池」的前瞻收益超额；
  ④ ``pool_score_stats``：候选池 E 的中位数 / 负分占比；
  ⑤ ``compute_alpha_panel``：``E = Σ(w·z)/Σ(w)`` 与覆盖率**逐字**可复算
     （= :func:`etf_layer.build_targets` 内部用的同一份口径）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import attribution as AT  # noqa: E402
import etf_layer as EL  # noqa: E402


def _close_fixture() -> pd.DataFrame:
    """合成 ETF 收盘价（5 个交易日 × 3 只）。"""
    days = pd.bdate_range("2022-01-04", periods=5)
    return pd.DataFrame({
        "510300": [10.0, 11.0, 12.0, 13.0, 14.0],
        "510500": [20.0, 20.0, 20.0, 20.0, 20.0],
        "510880": [5.0, 4.0, 3.0, 2.0, 1.0],
    }, index=days)


def test_前瞻收益按交易日行数且尾部缺失():
    """① 前瞻收益 = ``h`` 个**交易日**后的收益；最后 ``h`` 行是 NaN。"""
    close = _close_fixture()
    fwd = AT.etf_forward_return_panel(close, 2)
    assert list(fwd.columns) == ["510300", "510500", "510880"]
    # 第 0 行 ⇒ 第 2 行：12/10 - 1 = 0.2
    assert abs(float(fwd.iloc[0]["510300"]) - 0.2) < 1e-12, (
        f"🔴 前瞻收益算错：{fwd.iloc[0]['510300']}"
    )
    assert abs(float(fwd.iloc[0]["510880"]) - (3.0 / 5.0 - 1.0)) < 1e-12
    assert fwd.iloc[-2:].isna().all().all(), "🔴 尾部两行必须是 NaN（不许回填未来）"
    assert not fwd.iloc[:3].isna().any().any(), "🔴 前 3 行不该有 NaN"


def test_ETF层RankIC_完全同向为1_反向为负():
    """② 逐日截面 Spearman ⇒ 均值 / ICIR / t 值。"""
    days = pd.bdate_range("2022-01-04", periods=5)
    codes = ["A", "B", "C", "D"]
    score = pd.DataFrame(np.tile(np.arange(4, dtype=float), (len(days), 1)),
                         index=days, columns=codes)
    fwd = pd.DataFrame(np.tile(np.arange(4, dtype=float) * 2, (len(days), 1)),
                       index=days, columns=codes)
    ic = AT.etf_rank_ic(score, fwd)
    assert abs(float(ic["ic"]) - 1.0) < 1e-9, f"🔴 同向时 IC 应为 1（实得 {ic['ic']}）"
    assert ic["n_cross_sections"] == 5
    # 🔴 五个截面 IC 全 = 1.0 ⇒ 离散度 = 0 ⇒ ICIR / t 值**无定义 ⇒ None**（不许编）
    assert ic["icir"] is None and ic["t_stat"] is None

    ic_rev = AT.etf_rank_ic(score, -fwd)
    assert abs(float(ic_rev["ic"]) + 1.0) < 1e-9, f"🔴 反向时 IC 应为 −1（实得 {ic_rev['ic']}）"


def test_ETF层RankIC_样本不足返回None():
    """② 有效截面 < 2 ⇒ ic/icir/t_stat 全 None（不许编）。"""
    days = pd.bdate_range("2022-01-04", periods=1)
    score = pd.DataFrame([[1.0, 2.0, 3.0]], index=days, columns=["A", "B", "C"])
    fwd = pd.DataFrame([[0.1, 0.2, 0.3]], index=days, columns=["A", "B", "C"])
    ic = AT.etf_rank_ic(score, fwd)
    assert ic["ic"] is None and ic["icir"] is None and ic["t_stat"] is None, (
        f"🔴 样本不足却给了数：{ic}"
    )
    assert ic["n_cross_sections"] == 1


def test_拆解_选中组合对候选池的超额():
    """③ 选中组合 vs 候选池的前瞻收益超额。"""
    days = pd.bdate_range("2022-01-04", periods=2)
    fwd = pd.DataFrame(
        {"510300": [0.10, 0.20], "510500": [0.00, 0.00], "510880": [-0.10, -0.20]},
        index=days,
    )
    rows = []
    for day in days:
        for code, sel in (("510300", True), ("510500", False), ("510880", False)):
            rows.append({
                "date": day.strftime("%Y-%m-%d"), "code": code,
                "alpha_score": 1.0, "coverage_ok": True, "liquidity_ok": True,
                "tradable": True, "selected": sel,
            })
    diag = pd.DataFrame(rows)
    dec = AT.rebalance_decomposition(diag, fwd, 20)
    assert len(dec) == 2, f"🔴 拆解出 {len(dec)} 行，应为 2"
    # 候选池等权 = (0.10 + 0.00 − 0.10)/3 = 0；选中 = 0.10 ⇒ 超额 = 0.10
    assert abs(float(dec.iloc[0]["pool_fwd"])) < 1e-12, f"🔴 pool_fwd 算错：{dec.iloc[0]['pool_fwd']}"
    assert abs(float(dec.iloc[0]["selected_fwd"]) - 0.10) < 1e-12
    assert abs(float(dec.iloc[0]["excess"]) - 0.10) < 1e-12
    summary = AT.summarize_decomposition(dec)
    assert summary["n_periods"] == 2
    assert abs(float(summary["mean_excess"]) - 0.15) < 1e-12, (
        f"🔴 平均超额算错（两天分别是 0.10 / 0.20）：{summary['mean_excess']}"
    )
    assert float(summary["hit_rate_excess_gt0"]) == 1.0


def test_候选池统计():
    """④ 候选池 E 的中位数 / 负分占比。"""
    rows = []
    for code, e, ok in (("A", 0.5, True), ("B", -0.5, True), ("C", -1.5, False)):
        rows.append({
            "date": "2022-01-04", "code": code, "alpha_score": e,
            "coverage_ok": ok, "liquidity_ok": True, "tradable": True, "selected": False,
        })
    stats = AT.pool_score_stats(pd.DataFrame(rows))
    assert len(stats) == 1
    row = stats.iloc[0]
    assert int(row["n_candidates"]) == 2, f"🔴 候选数算错：{row['n_candidates']}"
    assert abs(float(row["median_e"]) - 0.0) < 1e-12
    assert abs(float(row["frac_neg_e"]) - 0.5) < 1e-12


def test_E面板_与手算一致():
    """⑤ ``E = Σ(w·z)/Σ(w)`` 与覆盖率逐字可复算。"""
    days = pd.bdate_range("2022-01-04", periods=2)
    inst = {"A": "SH600000", "B": "SH600001"}
    scores = pd.DataFrame(
        [{"date": d, "instrument": i, "score": s}
         for d in days for i, s in (("SH600000", 1.0), ("SH600001", 3.0))])
    weights = {"510300": {"SH600000": 60.0, "SH600001": 40.0}}
    panel, meta = EL.compute_alpha_panel(scores, weights, list(days), names={"510300": "x"},
                                         codes=["510300", "510500"])
    z = EL.zscore(pd.Series({"SH600000": 1.0, "SH600001": 3.0}))
    expect = (60.0 * float(z["SH600000"]) + 40.0 * float(z["SH600001"])) / 100.0
    assert abs(float(panel.iloc[0]["510300"]) - expect) < 1e-12, (
        f"🔴 E 算错：{panel.iloc[0]['510300']} vs {expect}"
    )
    assert "510500" not in panel.columns, "🔴 不在 codes 里的 ETF 不该进面板"
    row = meta[days[0]][0]
    assert abs(float(row["coverage"]) - 1.0) < 1e-12
    assert int(row["n_stocks_scored"]) == 2
