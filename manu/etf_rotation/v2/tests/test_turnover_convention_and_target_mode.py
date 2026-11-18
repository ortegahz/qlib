"""🔴 V2 **换手口径改版 + 换手目标模式**（2026-11-18 用户拍板）的回归测试。

**这次改了什么（两件事）**

  ① **口径**：V2 的 ``annualized_turnover`` = ``Σ_代码 |目标权重 − 调仓前漂移权重|``
     = **双边**（= 总成交额 / 组合净值）⇒ 一次把持仓全换掉 = **2.0**。
     本仓库 Top-K 线既有约定（``manu/memory/ashare_topk_20261104.md``「单边换手定义」行）
     = **单边 = 0.5 × Σ|Δw|** ⇒ **单边 = 双边 × 0.5**。
     旧实现把 Σ|Δw| **错标成「单边」** ⇒ 与 Top-K 线口径分叉 ⇒ 本次修掉。

  ② **目标模式**：用户 2026-11-18 拍板**废除月频策略的「≤4.0× 换手」硬拦截**
     ⇒ 改「cadence=20 先验锁定 + hold_buffer=2 + 报告**并列披露**单/双边换手率」
     ⇒ ``config_v2.TURNOVER_TARGET_MODE = "disclosure"``：
     ``apply_turnover_gate(mode="disclosure")`` **不许**再用换手达标与否过滤候选 trial。

🔴 **实测锚点**（主 agent 实测核过的事实，本文件直接当既成事实用）：
   V2 五折冠军 ``annualized_turnover`` = **14.1002（双边）** ⇒ **单边 = 7.0501×/年**；
   ``total_cost_drag`` = 4.4286%（累计 4.84 年）⇒ 年化 ≈ **0.915%/年**（= 14.10 × 6.5bp）。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 把 disclosure 分支**改回旧过滤逻辑** ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。

🔴 本文件**只用自造夹具** ⇒ 不联网、不读真实行情、不跑重量级回测。
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

#: ``manu/etf_rotation/v2/`` 目录（``metrics`` 用扁平导入 ``config_v2``）
_V2_DIR = Path(__file__).resolve().parents[1]
#: ``manu/`` 目录（生产调用点扫描用）
_MANU_DIR = Path(__file__).resolve().parents[3]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import backtest_v2  # noqa: E402
import config_v2 as C  # noqa: E402
import metrics  # noqa: E402
import run_v2 as RV  # noqa: E402

RUNV2_SOURCE = _V2_DIR / "run_v2.py"


# ----------------------------------------------------------------- 工具
def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**（真源码不动）。

    与 ``test_val_search_isolation.py`` 同款写法；这里**额外**要求锚点**唯一**
    （防止 ``str.replace`` 误伤同名代码 ⇒ 变异失真）。
    """
    text = RUNV2_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, (
            f"变异锚点必须唯一存在（源码已变）：{old!r}（命中 {text.count(old)} 次）"
        )
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _trial(score: float, turn: float | None, tag: str) -> dict:
    """自造 trial（结构与 ``hyperopt.search`` 的返回值同形）。"""
    obj: dict = {"score": score}
    if turn is not None:
        obj["annualized_turnover"] = turn
    return {"params": {"tag": tag}, "score": score, "objective": obj, "status": "ok"}


def _nav_and_turnover() -> tuple[pd.Series, dict[str, float]]:
    """自造净值（2 年）+ 调仓日换手（每 21 个交易日一次，每次 1.0）。"""
    idx = pd.bdate_range("2022-01-03", periods=504)
    nav = pd.Series(1.0 + np.linspace(0.0, 0.4, len(idx)), index=idx, name="nav")
    turnover = {d.strftime("%Y-%m-%d"): 1.0 for d in idx[::21]}
    return nav, turnover


# ----------------------------------------------------------------- ① 口径：单/双边
def test_单边换手是双边的一半() -> None:
    """🔴 实测锚点：双边 14.1002 ⇒ 单边 7.0501（Top-K 线同源口径）。"""
    assert C.one_way_turnover(14.1002) == pytest.approx(7.0501, abs=1e-9), (
        "🔴 单边必须 = 0.5 × 双边（Top-K 线「单边 = 0.5 × Σ|Δw|」口径）"
    )


def test_单边换手口径常量固定() -> None:
    assert C.TURNOVER_CONVENTION == "two_way_sum_abs_dw"
    assert float(C.TURNOVER_ONE_WAY_FACTOR) == pytest.approx(0.5, abs=1e-12)


def test_单边换手对非法输入返回NaN() -> None:
    """🔴 2026-11-18 续四：非法 / 非有限 ⇒ **NaN**（**不许 0.0**，也不抛错）。

    🔴 为什么改（铁律 §十四.112.4「NaN 语义不许混搭」）：
       双边换手**算不出来** ⇒ 单边**也是 NaN**；落 ``0.0`` 会让读者把
       「算不出来」误读成「换手为零」⇒ 那是**编数字**，比 NaN 更危险。
       （旧实现落 0.0，已由独立 reviewer 抓出并更正。）
    """
    assert math.isnan(C.one_way_turnover(float("nan")))
    assert math.isnan(C.one_way_turnover(None))
    assert math.isnan(C.one_way_turnover("abc"))
    assert math.isnan(C.one_way_turnover(float("inf")))
    assert math.isnan(C.one_way_turnover(float("-inf")))


def test_单边换手对有限输入逐位不变() -> None:
    """🔴 改语义**不许改数字**：有限输入的结果必须**逐位**不变。"""
    assert C.one_way_turnover(14.100247974306782) == 7.050123987153391
    assert C.one_way_turnover(16.568483) == 8.2842415


def test_summarize同时给出双边单边与年化成本拖累() -> None:
    """``summarize()`` 必须同时落**双边**换手 / **单边**换手 / 年化成本拖累。"""
    nav, turnover = _nav_and_turnover()
    m = metrics.summarize(
        nav, None, turnover,
        commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE),
        periods_per_year=252,
    )
    for key in ("annualized_turnover", "annualized_turnover_one_way", "annualized_cost_drag"):
        assert key in m, f"🔴 summarize() 缺键 {key!r}；实得 {sorted(m)!r}"
    two_way = float(m["annualized_turnover"])
    one_way = float(m["annualized_turnover_one_way"])
    assert one_way == pytest.approx(0.5 * two_way, abs=1e-12), (
        f"🔴 单边必须 = 0.5 × 双边；双边 {two_way!r} / 单边 {one_way!r}"
    )
    assert one_way != pytest.approx(two_way, abs=1e-12), (
        "🔴 单边与双边相等 ⇒ 口径没分开（旧实现的「错标单边」就是这样混过去的）"
    )
    # 年化成本拖累 = 累计成本拖累 ÷ 年数
    years = (len(nav) - 1) / 252.0
    assert float(m["annualized_cost_drag"]) == pytest.approx(
        float(m["total_cost_drag"]) / years, rel=1e-9, abs=1e-15
    )


def test_summarize年数非法时年化成本拖累为NaN() -> None:
    """净值点数 < 2 ⇒ 年数 = 0 ⇒ **年化**成本拖累必须是 NaN（不许编数字）。"""
    nav = pd.Series([1.0], index=pd.to_datetime(["2022-01-03"]))
    m = metrics.summarize(nav, None, None, periods_per_year=252)
    assert np.isnan(float(m["annualized_cost_drag"]))
    assert np.isnan(float(m["annualized_turnover_one_way"]))


# ----------------------------------------------------------------- ② 换手目标模式
def test_目标模式默认是披露() -> None:
    """🔴 用户 2026-11-18 拍板：废除「≤4× 换手」硬拦截 ⇒ 默认模式 = 披露。"""
    assert C.TURNOVER_TARGET_MODE == "disclosure"


def test_披露模式下换手不拦截且blocking为假() -> None:
    """``mode="disclosure"`` ⇒ 不抛错、``blocking=False``、``ok`` 仍**如实**。"""
    res = metrics.check_turnover_target({"annualized_turnover": 14.1002}, 4.0,
                                         label="val 段", mode="disclosure")
    assert res["blocking"] is False, "🔴 披露模式绝不许拦截"
    assert res["ok"] is False, "🔴 披露模式的 ok 仍必须如实反映是否达标"
    assert res["annualized_turnover"] == pytest.approx(14.1002, abs=1e-12)
    assert "披露" in res["message"], f"🔴 message 必须换成披露措辞；实得 {res['message']!r}"


def test_披露模式下算不出来也不抛错() -> None:
    """缺失 / NaN ⇒ 不抛错（仍不许当成达标）。"""
    for raw in ({}, {"annualized_turnover": float("nan")}, {"annualized_turnover": None}):
        res = metrics.check_turnover_target(raw, 4.0, mode="disclosure")
        assert res["ok"] is False
        assert res["blocking"] is False


def test_闸门模式仍是硬闸门() -> None:
    """``mode="gate"``（默认）⇒ 旧行为**逐字不变**（含 blocking 语义 = True）。"""
    res = metrics.check_turnover_target({"annualized_turnover": 3.2}, 4.0, label="val 段")
    assert res["ok"] is True
    assert res.get("blocking", True) is True, "🔴 gate 模式必须仍然是硬闸门"
    assert "换手闸门" in res["message"], f"🔴 gate 模式文案必须逐字不变；实得 {res['message']!r}"


def test_非法模式直接抛ValueError() -> None:
    with pytest.raises(ValueError):
        metrics.check_turnover_target({"annualized_turnover": 1.0}, 4.0, mode="whatever")
    with pytest.raises(ValueError):
        RV.apply_turnover_gate({"trials": []}, 4.0, mode="whatever")


# ----------------------------------------------------------------- ③ 选参不被换手过滤
def _two_trial_result() -> dict:
    """夹具：低换手低分 vs **高换手高分**（披露模式必须选高分那个）。"""
    return {
        "trials": [
            _trial(0.10, 2.0, "low_turn_low_score"),
            _trial(0.50, 9.0, "high_turn_high_score"),
        ],
        "best_params": {"tag": "low_turn_low_score"},
        "best_score": 0.10,
    }


def test_披露模式不会因换手超标丢掉高分trial() -> None:
    """🔴 核心：换手只作披露基准 ⇒ **不许**过滤候选 trial。"""
    gated = RV.apply_turnover_gate(_two_trial_result(), 4.0, mode="disclosure")
    assert gated["best_params"]["tag"] == "high_turn_high_score", (
        "🔴 披露模式仍用换手过滤候选 ⇒ 「废除硬拦截」没落地"
    )
    assert gated["best_score"] == pytest.approx(0.50, abs=1e-12)


def test_披露模式仍如实记录闸门块() -> None:
    """披露模式**不许**丢掉披露字段（ok / 达标数 / 最低换手 / 选中者换手）。"""
    gated = RV.apply_turnover_gate(_two_trial_result(), 4.0, mode="disclosure")
    gate = gated["gate"]
    assert gate["n_trials_within_target"] == 1
    assert gate["n_trials_with_turnover"] == 2
    assert gate["min_turnover"] == pytest.approx(2.0, abs=1e-12)
    assert gate["chosen_turnover"] == pytest.approx(9.0, abs=1e-12)
    assert gate["ok"] is False, "🔴 披露模式的 ok 必须如实反映选中者是否达标"
    assert gate["blocking"] is False
    assert "披露基准" in gate["message"], f"🔴 message 必须写明披露基准；实得 {gate['message']!r}"


def test_闸门模式旧行为不变() -> None:
    """``mode="gate"`` ⇒ 仍在**达标** trial 里挑（高分超标者不许胜出）。"""
    gated = RV.apply_turnover_gate(_two_trial_result(), 4.0, mode="gate")
    assert gated["best_params"]["tag"] == "low_turn_low_score", (
        "🔴 gate 模式必须保持旧行为（达标 trial 里挑）"
    )
    assert gated["gate"]["ok"] is True
    assert gated["gate"]["blocking"] is True


def test_闸门模式是默认参数() -> None:
    """不传 mode ⇒ 必须等于 ``mode="gate"``（既有调用方行为逐字不变）。"""
    default = RV.apply_turnover_gate(_two_trial_result(), 4.0)
    explicit = RV.apply_turnover_gate(_two_trial_result(), 4.0, mode="gate")
    assert default["best_params"] == explicit["best_params"]
    assert default["gate"]["ok"] == explicit["gate"]["ok"]


# ----------------------------------------------------------------- ④ 变异反证
def test_变异反证_disclosure改回旧过滤_高分trial必须重新落选(tmp_path: Path) -> None:
    """把 disclosure 分支改回「只在达标 trial 里挑」⇒ 高分 trial 落选 ⇒ 断言必须转红。"""
    mutant = _mutated_module(
        tmp_path,
        [('\n        chosen = max(ok_trials, key=lambda t: float(t.get("score")))\n',
          '\n        chosen = max(within, key=lambda t: float(t.get("score")))\n')],
        "runv2_disclosure_filtered",
    )
    result = _two_trial_result()
    real = RV.apply_turnover_gate(result, 4.0, mode="disclosure")
    mut = mutant.apply_turnover_gate(result, 4.0, mode="disclosure")
    assert real["best_params"]["tag"] == "high_turn_high_score", (
        "🔴 真源码必须选中高分 trial（披露模式不按换手过滤）"
    )
    assert mut["best_params"]["tag"] == "low_turn_low_score", (
        "🔴 变异体必须回到旧过滤逻辑（高分超标者落选）⇒ 否则这条断言没有牙"
    )
    assert mut["best_params"] != real["best_params"], (
        "🔴 变异体与真源码结果相同 ⇒ 这条断言没有牙"
    )


# ----------------------------------------------------------------- ⑤ 口径守卫
def test_回测产物的换手定义写明双边() -> None:
    """``run_backtest_v2`` 的 ``turnover_definition`` 必须写明**双边**。"""
    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    px = pd.DataFrame(
        {"A": [10.0, 10.0, 10.0, 10.0], "B": [20.0, 20.0, 20.0, 20.0]}, index=dates
    )
    res = backtest_v2.run_backtest_v2(
        px, {"2024-01-02": {"A": 1.0}, "2024-01-03": {"B": 1.0}},
        commission=0.0, slippage=0.0,
    )
    assert "双边" in res["turnover_definition"], (
        f"🔴 换手定义必须写明双边；实得 {res['turnover_definition']!r}"
    )
    # 🔴 口径锚点：一次「全换」⇒ 双边 2.0（= 总成交额/组合净值）
    assert res["turnover_by_date"]["2024-01-03"] == pytest.approx(2.0, abs=1e-12), (
        "🔴 一次全换的双边换手必须是 2.0（旧口径把它错标成「单边」）"
    )


def test_生产调用点都显式传了mode() -> None:
    """🔴 生产调用点必须**显式**传 ``mode=``（不许吃默认值）。"""
    call_re = re.compile(r"apply_turnover_gate\((?:[^()]|\([^()]*\))*\)", re.DOTALL)
    targets = [
        _V2_DIR / "run_v2.py",
        _MANU_DIR / "tools" / "v2_val_ma_sweep.py",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        calls = []
        for match in call_re.finditer(text):
            # 函数**定义**不是调用点 ⇒ 跳过（定义里的 mode= 是默认值，不算「显式传参」）
            if text[:match.start()].rstrip().endswith("def"):
                continue
            calls.append(match.group(0))
        assert calls, f"🔴 {path} 里找不到 apply_turnover_gate( 调用"
        for call in calls:
            assert "mode=" in call, (
                f"🔴 {path} 里的 apply_turnover_gate( 调用必须显式传 mode=；实得 {call!r}"
            )
