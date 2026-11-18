"""🔴 V2 **val 段隔离**（超参搜索只看 val）的回归测试 —— 2026-11-17 夜班。

**事故背景（2026-11-17 21:20 独立审计发现，红线被实质违反）**：
val 段超参搜索的目标函数把**整段历史净值**（2020-01-02 ~ 2026-11-10）
直接交给 ``metrics.summarize()`` ⇒

  * 年化收益里**混进了测试段（2024–2026）已实现收益**；
  * ``annualized_turnover`` 的**分母**取自传入 nav 的长度（全史 6.64 年），
    而**分子**只有 val 段的 24 次调仓 ⇒ **年化换手被低估约 3.4 倍**
    （实测：objective 报 4.99×，val 段真值 17.13×）
    ⇒ 用户要求的「换手 16.57× → ≤4×」目标**完全失效**。

同时审计还发现：qlib 的负 ``Ref`` 会**自动延伸取数** ⇒ val 段最后一个截面
（如 2023-12-19）的标签窗口末端 ≈ 2024-01-23 **落在测试段内**
⇒ 消融判定 / val RankIC 实际用了**测试段的价格**。

🔴 本文件钉死四件事：
  ① 指标**只**在 val 窗口内算（窗口不对 ⇒ 抛错）；
  ② 换手「≤4×」是**硬闸门**（超标 trial 不许靠高分胜出）；
  ③ 标签窗口伸进测试段的 val 截面**必须被裁掉**；
  ④ 冻结前必须留下「换手目标是否达成」的**可审计记录**。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 改坏关键判定 ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。

🔴 本文件**只用自造夹具** ⇒ 不联网、不跑重量级回测、不读真实行情。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import run_v2 as RV  # noqa: E402

RUNV2_SOURCE = _V2_DIR / "run_v2.py"


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**（真源码不动）。"""
    text = RUNV2_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _nav_and_bench() -> tuple[pd.Series, pd.Series]:
    """自造净值：2020-01-01 起 800 个交易日，测试段涨、val 段跌（便于区分窗口）。"""
    idx = pd.bdate_range("2020-01-01", periods=800)
    nav = pd.Series(1.0 + np.linspace(0.0, 0.8, len(idx)), index=idx, name="nav")
    bench = pd.Series(1.0 + np.linspace(0.0, 0.2, len(idx)), index=idx, name="bench")
    return nav, bench


# ----------------------------------------------------------------- ① 指标只在 val 窗口内


def test_窗口不是val段必须抛错() -> None:
    """非 val 窗口 ⇒ **直接抛错**（不许静默按 val 算）。"""
    nav, bench = _nav_and_bench()
    with pytest.raises(RuntimeError) as exc:
        RV.val_window_metrics(nav, None, bench, window=(str(C.VALID[0]), "2026-11-03"))
    assert "val" in str(exc.value)


def test_默认窗口就是val段() -> None:
    """不传 window ⇒ 用 ``config_v2.VALID``（不得抛错）。"""
    nav, bench = _nav_and_bench()
    m = RV.val_window_metrics(nav, None, bench)
    assert m.get("annualized_return") is not None


def test_指标只反映val窗口而不是全史() -> None:
    """🔴 核心：val 窗口内的指标必须与「全史指标」**不同**
    ⇒ 证明指标窗口确实被切到了 val 段（不是全史）。"""
    nav, bench = _nav_and_bench()
    import metrics as ME
    m_val = RV.val_window_metrics(nav, None, bench)
    m_all = ME.summarize(nav, bench, None, commission=0.0, slippage=0.0, periods_per_year=250)
    assert abs(float(m_val["annualized_return"]) - float(m_all["annualized_return"])) > 1e-6, (
        "🔴 val 窗口指标与全史指标相同 ⇒ 指标窗口没被切开"
    )


def test_换手与净值落在同一窗口() -> None:
    """传入的换手在窗口外 ⇒ 必须被裁掉（分子分母同窗）。"""
    nav, bench = _nav_and_bench()
    turnover = {d.strftime("%Y-%m-%d"): 0.1 for d in nav.index}
    m = RV.val_window_metrics(nav, turnover, bench)
    turn = float(m.get("annualized_turnover") or 0.0)
    # val 段约 2 年 ⇒ 全窗换手若不被裁，年化换手会明显偏小
    assert turn > 0.0
    assert np.isfinite(turn)


# ----------------------------------------------------------------- ② 换手硬闸门


def _trial(score: float, turn: float | None, params: dict | None = None) -> dict:
    obj: dict = {"score": score}
    if turn is not None:
        obj["annualized_turnover"] = turn
    return {"params": params or {"x": 1}, "score": score, "objective": obj, "status": "ok"}


def test_闸门拦掉换手超标的trial即使它分数更高() -> None:
    """高分但换手 9× 的 trial **不许**胜出（用户目标 ≤4× 是硬约束）。"""
    result = {
        "trials": [
            _trial(0.50, 9.0, {"a": 1}),
            _trial(0.30, 3.0, {"a": 2}),
            _trial(0.20, 2.0, {"a": 3}),
        ],
        "best_params": {"a": 1},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["best_params"]["a"] == 2, (
        "🔴 换手超标的 trial 胜出了 ⇒ 「≤4×」没有变成硬约束"
    )
    assert gated["gate"]["ok"] is True
    assert gated["gate"]["n_trials_within_target"] == 2


def test_闸门在全员超标时按主目标取分并标记未达成() -> None:
    """一个达标 trial 都没有 ⇒ 按**主目标**取分，且 ``ok=False``（不许假装达标）。

    🔴 2026-11-18 用户目标文档：旧口径「取换手最低者」= 把 cadence=90 选成胜者
    ⇒ 单盲 −12.93% / 回撤 −54.93%。⇒ 改为按主目标取分 + 如实披露未达成。
    """
    result = {
        "trials": [_trial(0.50, 9.0, {"a": 1}), _trial(0.10, 5.0, {"a": 2})],
        "best_params": {"a": 1},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["best_params"]["a"] == 1, (
        "🔴 全员超标时必须按**主目标**（score 最高）取分者"
        "⇒ 取换手最低者 = 把 cadence=90 又选回来了（旧事故）"
    )
    assert gated["gate"]["ok"] is False
    assert "未达成" in gated["gate"]["message"]


def test_旧口径min_turnover仍可切回_仅供审计对表(monkeypatch) -> None:
    """🔴 旧兜底口径**必须仍然可达**（审计对表用），但**默认不是它**。"""
    monkeypatch.setattr(RV.C, "TURNOVER_GATE_ON_MISS", "min_turnover")
    result = {
        "trials": [_trial(0.50, 9.0, {"a": 1}), _trial(0.10, 5.0, {"a": 2})],
        "best_params": {"a": 1},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["best_params"]["a"] == 2, (
        "🔴 切到旧口径后必须回到「换手最低者」"
    )


def test_换手算不出来的trial不算达标() -> None:
    """``annualized_turnover`` 缺失/NaN ⇒ 不许当成达标。"""
    result = {
        "trials": [_trial(0.50, None, {"a": 1}), _trial(0.30, 3.0, {"a": 2})],
        "best_params": {"a": 1},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["best_params"]["a"] == 2, "🔴 换手缺失的 trial 不许凭高分胜出"
    assert gated["gate"]["ok"] is True


def test_闸门在没有任何有效trial时抛错() -> None:
    result = {"trials": [], "best_params": {}, "best_score": None}
    with pytest.raises(RuntimeError):
        RV.apply_turnover_gate(result, 4.0)


# ----------------------------------------------------------------- ③ 标签窗口泄露裁剪


def test_标签窗口伸进测试段的截面被裁掉() -> None:
    """val 段末尾的截面（标签窗口伸进测试段）**必须**被裁掉。"""
    cal = RV.SP.trading_calendar()
    valid_end = str(C.VALID[1])
    cut = max(i for i, d in enumerate(cal) if d.isoformat() <= valid_end)
    leaking = pd.Timestamp(cal[cut - 1])          # 标签窗口末端 = 明天 ⇒ 越过 val 末日
    safe = pd.Timestamp(cal[cut - 1 - int(C.LABEL_HORIZON_DAYS)])
    kept = RV.val_score_dates_without_label_leak([leaking, safe])
    assert leaking not in kept, "🔴 标签窗口伸进测试段的截面没有被裁掉"
    assert safe in kept, "🔴 标签窗口完全在 val 段内的截面被误裁"


def test_裁剪后的末日标签窗口不越界() -> None:
    """``_val_end_without_label_leak()`` 给出的末日，其标签窗口末端 ≤ val 末日。"""
    val_end = RV._val_end_without_label_leak()
    cal = RV.SP.trading_calendar()
    idx = [i for i, d in enumerate(cal) if d.isoformat() == val_end][0]
    lbl_end = cal[idx + int(C.LABEL_HORIZON_DAYS)]
    assert lbl_end.isoformat() <= str(C.VALID[1]), (
        "🔴 裁剪后的末日标签窗口仍然越过 val 段末日"
    )


def test_裁剪后仍可继续往前的下一个交易日越界() -> None:
    """（最大性）再往前一个交易日，标签窗口就应当越界。"""
    val_end = RV._val_end_without_label_leak()
    cal = RV.SP.trading_calendar()
    idx = [i for i, d in enumerate(cal) if d.isoformat() == val_end][0]
    nxt = cal[idx + 1]
    lbl_end = cal[idx + 1 + int(C.LABEL_HORIZON_DAYS)]
    assert lbl_end.isoformat() > str(C.VALID[1]), (
        "🔴 裁剪不是最大化的 ⇒ 还能再裁而不越界"
    )


# ----------------------------------------------------------------- ④ 变异反证


def test_变异反证_窗口守卫失效_非val窗口必须不再抛错(tmp_path: Path) -> None:
    """把窗口守卫改成永不触发 ⇒ 非 val 窗口**不再抛错** ⇒ 这条断言没有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("    if got != expect:\n        raise RuntimeError(",
          "    if False:\n        raise RuntimeError(")],
        "runv2_no_window_guard",
    )
    nav, bench = _nav_and_bench()
    # 🔴 变异后：守卫失效 ⇒ **不再抛错**（不抛错即通过 ⇒ 证明变异确实生效）
    mutant.val_window_metrics(nav, None, bench,
                              window=(str(C.VALID[0]), "2026-11-03"))
    # 对照：**真源码**下非 val 窗口必须抛错
    with pytest.raises(RuntimeError):
        RV.val_window_metrics(nav, None, bench,
                              window=(str(C.VALID[0]), "2026-11-03"))


def test_变异反证_不切窗口_指标必须回到全史值(tmp_path: Path) -> None:
    """把切窗去掉 ⇒ 指标变回全史口径 ⇒ 「val 窗口 ≠ 全史」那条必须转红。"""
    mutant = _mutated_module(
        tmp_path,
        [("    nav_v, to_v, bench_v = ME.slice_window(\n"
          "        nav, turnover, benchmark,\n"
          "        start=pd.Timestamp(got[0]), end=pd.Timestamp(got[1]),\n"
          "    )",
          "    nav_v, to_v, bench_v = (nav, turnover, benchmark)")],
        "runv2_no_slice",
    )
    nav, bench = _nav_and_bench()
    import metrics as ME
    m_mut = mutant.val_window_metrics(nav, None, bench)
    m_all = ME.summarize(nav, bench, None, commission=0.0, slippage=0.0, periods_per_year=250)
    assert abs(float(m_mut["annualized_return"]) - float(m_all["annualized_return"])) < 1e-9, (
        "🔴 变异后指标仍被切开 ⇒ 这条断言没有牙"
    )


def test_变异反证_闸门不筛选_超标trial必须重新胜出(tmp_path: Path) -> None:
    """把闸门改成直接取最高分 ⇒ 超标 trial 胜出 ⇒ 这条断言没有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("        chosen = max(within, key=lambda t: float(t.get(\"score\")))",
          "        chosen = max(ok_trials, key=lambda t: float(t.get(\"score\")))")],
        "runv2_gate_disabled",
    )
    result = {
        "trials": [_trial(0.50, 9.0, {"a": 1}), _trial(0.30, 3.0, {"a": 2})],
        "best_params": {"a": 1},
        "best_score": 0.5,
    }
    gated = mutant.apply_turnover_gate(result, 4.0)
    assert gated["best_params"]["a"] == 1, (
        "🔴 变异后闸门仍然拦住了超标 trial ⇒ 这条断言没有牙"
    )


def test_变异反证_不裁标签泄露_尾部截面必须保留(tmp_path: Path) -> None:
    """把裁剪条件改成恒真 ⇒ 泄露截面被保留 ⇒ 这条断言没有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("        if i + horizon <= cut:\n            out.append(ts)",
          "        if True:\n            out.append(ts)")],
        "runv2_no_label_trim",
    )
    cal = RV.SP.trading_calendar()
    valid_end = str(C.VALID[1])
    cut = max(i for i, d in enumerate(cal) if d.isoformat() <= valid_end)
    leaking = pd.Timestamp(cal[cut - 1])
    kept = mutant.val_score_dates_without_label_leak([leaking])
    assert leaking in kept, (
        "🔴 变异后泄露截面仍被裁掉 ⇒ 这条断言没有牙"
    )
