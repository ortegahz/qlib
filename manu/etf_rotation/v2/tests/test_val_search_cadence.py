"""🔴 V2 **调仓节奏（cadence）进入 val 段搜索空间**的钉死测试 —— 2026-11-17。

**背景（用户红线驱动）**：用户红线 = 「换手 16.57×（V1 同口径）→ **≤4×**，回撤收敛」。
实测：在 cadence = 20 个交易日的**折叠常量**下，臂 / Top-K / 缓冲带 / MA 窗口**全部**搜完后，
val 段年化换手最低仍 **12.32×**（结构性来源 = 「持仓跌破 MA ⇒ 无条件卖出」，
Rank Buffer 对 MA 强平无效）⇒ **≤4× 只能靠调仓节奏本身达成**
⇒ cadence（``rebalance_every_n_days`` = {20, 40, 60, 90}）进入 **val 段**搜索空间。

🔴 **口径变更必须披露**（本文件钉死）：
  ① cadence = 20 与 **V1 同口径** ⇒ **保留可见**（逐 trial 记录 cadence 与 n_rebalances）；
  ② cadence ≠ 20 ⇒ 换手与 V1 的 16.57× **不是同一口径** ⇒ 不可直接对比；
  ③ cadence 越大 ⇒ 调仓次数越少 ⇒ val 段年化 / 回撤**越噪声** ⇒ 必须列出 n_rebalances；
  ④ val 段只有 ≈2 年 ⇒ cadence = 90 只剩个位数调仓 ⇒ 必须标注「样本不足，不可据此选参」。

【本文件钉死】
  ① 网格**含** cadence 维度，且 stride 映射正确（20→2 / 40→4 / 60→6 / 90→9）；
  ② 【变异反证】把 ``_build_targets_for`` 里的 cadence 读取改回常量 ⇒ ①必须转红；
  ③ ``assert_no_rebalance_keys`` **只放行**白名单键 ``rebalance_every_n_days``，
     ``stride`` / ``rebalance_signal_stride`` 等**仍必须被拒**；
  ④ val-only 守卫仍生效（搜索窗口越界即抛错）；
  ⑤ 换手闸门语义（达标 / 全超标 / 缺失）**没被改坏**（与 cadence 无关）。

⛔ **只用自造夹具** ⇒ 不联网、不跑重量级回测、不读真实行情。
⛔ **绝不落盘改真源码**（变异 = 源码 → 内存改 → tmp_path → import 副本）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import etf_layer as EL  # noqa: E402
import hyperopt as HO  # noqa: E402
import run_v2 as RV  # noqa: E402

RUNV2_SOURCE = _V2_DIR / "run_v2.py"

#: 🔴 cadence → 信号栅格步长（**唯一正确映射**，由 ``config_v2`` 推导）
EXPECTED_STRIDE = {20: 2, 40: 4, 60: 6, 90: 9}

#: 🔴 越过红线的窗口（任一命中都必须被拒）
FORBIDDEN_WINDOWS = [
    ("2024-01-01", "2024-12-31"),      # 完全落在测试段
    ("2022-01-01", "2024-01-01"),      # 终点触到测试段起点
    ("2023-06-01", "2024-06-30"),      # 跨过 val/测试边界
    ("2016-01-01", "2021-12-31"),      # 误用训练段
]


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


def _call_build_targets_for(module, tmp_path: Path, params: dict, monkeypatch):
    """🔴 用**桩件**调一次 ``_build_targets_for`` ⇒ 返回被捕获的 ``rebalance_signal_stride``。

    ⛔ 桩件：分数 CSV 用自造的；成分股权重 / 特征 / 流动性映射 / ``build_targets`` 全部打桩
    ⇒ **不读真实行情、不跑回测**。
    """
    import constituents as CON

    scores_csv = tmp_path / f"stock_scores_v2_{params['_model_name']}.csv"
    pd.DataFrame({
        "date": ["2022-01-10", "2022-01-10"],
        "instrument": ["SH600000", "SH600001"],
        "score": [0.1, -0.2],
    }).to_csv(scores_csv, index=False)

    captured: dict = {}

    def _fake_build_targets(*args, **kwargs):
        captured["stride"] = kwargs.get("rebalance_signal_stride")
        return {}, pd.DataFrame()

    monkeypatch.setattr(EL, "build_targets", _fake_build_targets)
    monkeypatch.setattr(EL, "build_weight_map", lambda holdings: {})
    monkeypatch.setattr(EL, "compute_features", lambda close, amount: {})
    monkeypatch.setattr(CON, "load_holdings", lambda *a, **k: None)
    monkeypatch.setattr(CON, "snapshot_frame", lambda snapshot: pd.DataFrame())
    monkeypatch.setattr(module, "build_etf_tradable_maps", lambda: ({}, {}))

    module._build_targets_for(
        None, tmp_path, params,
        pd.DataFrame(), pd.DataFrame(), {}, [],
    )
    return captured


def _trial(score: float, turn: float | None, params: dict,
           n_rebalances: int | None = None) -> dict:
    """造一个 trial（``objective`` 里带换手与调仓次数）。"""
    obj: dict = {"annualized_turnover": turn, "excess_vs_benchmark": 0.05}
    if n_rebalances is not None:
        obj["n_rebalances"] = n_rebalances
    return {"params": params, "score": score, "objective": obj, "status": "ok"}


# ----------------------------------------------------------------- ① 网格含 cadence 维度
def test_网格必须含cadence维度() -> None:
    """🔴 搜索空间 = 臂 × top_k × hold_buffer × **cadence**。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    assert set(grid) == {"smoothing_arm", "top_k", "hold_buffer", "rebalance_every_n_days",
                          "alpha_weight", "allow_theme_cap"}, (
        f"🔴 搜索空间里出现了不该有的键（或缺 cadence / alpha 配比 / 同族上限）：{sorted(grid)}"
    )
    assert grid["rebalance_every_n_days"] == list(C.REBALANCE_CADENCE_GRID), (
        "🔴 cadence 候选必须来自 config_v2.REBALANCE_CADENCE_GRID（不许散落在代码里）"
    )
    # 🔴 2026-11-18 口径锁定：cadence **锁死 = 20 个交易日（严格月频）**
    #    （用户目标文档：「锁死 cadence = 20」「不准搜索 90/120/150 这种反直觉长周期」）
    assert C.REBALANCE_CADENCE_GRID == (20,), (
        f"🔴 cadence 必须锁死 20（严格月频），实得 {C.REBALANCE_CADENCE_GRID}"
    )
    assert grid["rebalance_every_n_days"] == [20], (
        f"🔴 搜索空间里只该有 cadence=20，实得 {grid.get('rebalance_every_n_days')}"
    )
    assert 20 in grid["rebalance_every_n_days"], (
        "🔴 cadence=20（与 V1 同口径）必须**保留可见**"
    )


def test_cadence到stride的映射必须正确() -> None:
    """20→2 / 40→4 / 60→6 / 90→9（stride = cadence // SIGNAL_STRIDE_N_DAYS）。"""
    for cadence, stride in EXPECTED_STRIDE.items():
        assert int(C.rebalance_signal_stride_for(cadence)) == stride, (
            f"🔴 cadence={cadence} 的 stride 必须是 {stride}"
        )
    assert int(C.REBALANCE_SIGNAL_STRIDE) == EXPECTED_STRIDE[20], (
        "🔴 折叠常量 stride 必须仍是 V1 同口径档的 2"
    )


def test_非法cadence必须被拒() -> None:
    """cadence 不是信号栅格步长的整数倍 ⇒ 调仓日会漂出信号栅格 ⇒ 直接 raise。"""
    with pytest.raises(ValueError):
        C.rebalance_signal_stride_for(25)
    with pytest.raises(ValueError):
        C.rebalance_signal_stride_for(0)


# ----------------------------------------------------------------- ② 变异反证（cadence 真的被读）
def test_构建目标时必须用被搜索的cadence(tmp_path: Path, monkeypatch) -> None:
    """🔴 核心：``_build_targets_for`` 的 stride **必须**由 params 里的 cadence 决定。"""
    for cadence, stride in EXPECTED_STRIDE.items():
        params = {"_model_name": "ablate_roc5", "rebalance_every_n_days": cadence}
        captured = _call_build_targets_for(RV, tmp_path, params, monkeypatch)
        assert captured["stride"] == stride, (
            f"🔴 cadence={cadence} 时 stride 必须是 {stride}，实得 {captured['stride']}"
            f" ⇒ cadence 没有真正接进调仓栅格"
        )


def test_缺省cadence回落到V1同口径档(tmp_path: Path, monkeypatch) -> None:
    """没有 cadence 的旧调用 ⇒ 回落到 20 个交易日（stride = 2）。"""
    params = {"_model_name": "ablate_roc5"}
    captured = _call_build_targets_for(RV, tmp_path, params, monkeypatch)
    assert captured["stride"] == int(C.REBALANCE_SIGNAL_STRIDE), (
        f"🔴 缺省 stride 必须是 {C.REBALANCE_SIGNAL_STRIDE}，实得 {captured['stride']}"
    )


def test_变异反证_cadence读回常量_步长断言必须转红(tmp_path: Path, monkeypatch) -> None:
    """🔴 把 ``_build_targets_for`` 里的 cadence 读取改回常量 ⇒ stride 不再随 cadence 变
    ⇒ 「stride 必须随 cadence 变」这条断言**必须有牙**。"""
    mutant = _mutated_module(
        tmp_path,
        [("        rebalance_signal_stride=int(\n"
          "            C.rebalance_signal_stride_for(\n"
          "                int(params.get(\"rebalance_every_n_days\", C.REBALANCE_EVERY_N_DAYS)))\n"
          "        ),",
          "        rebalance_signal_stride=int(C.REBALANCE_SIGNAL_STRIDE),")],
        "runv2_cadence_back_to_const",
    )
    params = {"_model_name": "ablate_roc5", "rebalance_every_n_days": 40}
    mutant_captured = _call_build_targets_for(mutant, tmp_path, params, monkeypatch)
    real_captured = _call_build_targets_for(RV, tmp_path, params, monkeypatch)
    assert real_captured["stride"] == EXPECTED_STRIDE[40], (
        "🔴 真源码下 cadence=40 的 stride 必须是 4（否则这条断言本身是错的）"
    )
    assert mutant_captured["stride"] == int(C.REBALANCE_SIGNAL_STRIDE), (
        f"🔴 变异后 stride 仍是 {mutant_captured['stride']} ⇒ 变异没生效 ⇒ 这条断言没有牙"
    )
    assert mutant_captured["stride"] != real_captured["stride"], (
        "🔴 变异前后 stride 相同 ⇒ cadence 读回常量这件事没被测到"
    )


# ----------------------------------------------------------------- ③ 白名单守卫
def test_白名单键必须放行() -> None:
    """🔴 ``rebalance_every_n_days``（唯一白名单）**必须**放行。"""
    RV.assert_no_rebalance_keys({"rebalance_every_n_days": [20, 40, 60, 90]})
    RV.assert_no_rebalance_keys(RV.build_search_grid(list(C.TOPK_GRID),
                                                      list(C.HOLD_BUFFER_GRID)))


@pytest.mark.parametrize("bad_key", [
    "stride",
    "rebalance_signal_stride",
    "rebalance_stride",
    "signal_stride",
    "REBALANCE_EVERY_N_DAYS",
    "rebalance_every_n_days_x",
])
def test_未知stride类键必须被拒(bad_key: str) -> None:
    """🔴 白名单**只有一个键**；其余含 rebalance/stride 的键一律 raise。"""
    with pytest.raises(ValueError):
        RV.assert_no_rebalance_keys({bad_key: [1]})


def test_变异反证_白名单被改成全放行_守卫必须转红(tmp_path: Path) -> None:
    """🔴 把守卫改成「含 rebalance 就放行」⇒ ``stride`` 键能混进来 ⇒ 「stride 必须被拒」
    这条断言**必须有牙**。"""
    mutant = _mutated_module(
        tmp_path,
        [('        and str(k) not in ALLOWED_REBALANCE_KEYS', "        and False")],
        "runv2_whitelist_allows_all",
    )
    # 对照：**真源码**下 stride 类键必须被拒
    with pytest.raises(ValueError):
        RV.assert_no_rebalance_keys({"rebalance_signal_stride": [2]})
    # 变异后：不再拒绝 ⇒ 证明上面那条拒绝断言确实在起作用
    mutant.assert_no_rebalance_keys({"rebalance_signal_stride": [2]})


# ----------------------------------------------------------------- ④ val-only 守卫
def test_搜索窗口必须等于val段() -> None:
    assert RV.val_search_window() == (str(C.VALID[0]), str(C.VALID[1]))


@pytest.mark.parametrize("start,end", FORBIDDEN_WINDOWS)
def test_越界窗口必须被拦截(start: str, end: str) -> None:
    """🔴 搜索窗口越界（碰测试段 / 用训练段）⇒ 物理拦截。"""
    with pytest.raises(ValueError):
        HO.assert_val_only(start, end)


def test_含cadence的网格搜索仍只拿到val窗口() -> None:
    """🔴 端到端：加了 cadence 维度之后，objective 收到的窗口**仍然只有** val 段。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    seen: list[tuple[str, str]] = []

    def objective(params: dict, window: tuple[str, str]) -> dict:
        seen.append((str(window[0]), str(window[1])))
        return {"score": float(params["rebalance_every_n_days"])}

    out = HO.search(objective, grid, val_window=RV.val_search_window())
    assert seen, "🔴 objective 一次都没被调用"
    assert all(w == (str(C.VALID[0]), str(C.VALID[1])) for w in seen), (
        f"🔴 objective 收到了非 val 窗口：{sorted(set(seen))}"
    )
    assert out["n_trials_run"] == len(C.SMOOTHING_ARM_IDS) * len(C.TOPK_GRID) * \
        len(C.HOLD_BUFFER_GRID) * len(C.REBALANCE_CADENCE_GRID) * \
        len(C.ALPHA_WEIGHT_GRID) * len(C.THEME_CAP_GRID)
    assert out["best_params"]["rebalance_every_n_days"] == C.REBALANCE_CADENCE_GRID[0]


def test_alpha与momentum互补_且同族上限可关() -> None:
    """🔴 2026-11-18：排序分里 **alpha / 动量配比进搜索空间**（此前写死 0.5/0.5）。

    val 归因证据：纯 E（alpha=1.0）ETF 层 RankIC **+0.1016**、超额 **+7.06%**；
    V2 现状（0.5/0.5）RankIC **+0.0550**、超额 **−10.06%**
    ⇒ 配比写死 ⇒ 网格搜索**不可能**修好它。
    """
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    assert "alpha_weight" in grid, "🔴 alpha_weight 必须进 val 段搜索空间"
    assert grid["alpha_weight"] == list(C.ALPHA_WEIGHT_GRID)
    assert 1.0 in grid["alpha_weight"], "🔴 必须保留「纯 E」（alpha=1.0）这一档"
    assert "allow_theme_cap" in grid, "🔴 同族上限开关必须可搜（val 证据：它吃掉 ~9pp 超额）"
    assert grid["allow_theme_cap"] == list(C.THEME_CAP_GRID)
    assert False in grid["allow_theme_cap"], "🔴 必须保留「关同族上限」这一档"
    # 🔴 alpha 与 momentum **永远互补**（网格里只放行一个键）
    assert "momentum_weight" not in grid, "🔴 momentum_weight 不许单独进网格（会与 alpha 打架）"
    seen: list[dict] = []

    def objective(params: dict, window: tuple[str, str]) -> dict:
        seen.append(dict(params))
        return {"score": 0.0}

    HO.search(objective, grid, val_window=RV.val_search_window())
    assert seen, "🔴 objective 一次都没被调用"
    for prm in seen:
        assert abs(float(prm["alpha_weight"]) + float(prm.get("momentum_weight", 1.0))
                   - 1.0) < 1e-6 or "momentum_weight" not in prm, (
            f"🔴 alpha 与 momentum 不互补：{prm}"
        )


def test_cadence已锁死20_严格月频() -> None:
    """🔴 2026-11-18 口径锁定：搜索空间里**只允许** cadence=20（严格月频）。

    * 依据：用户目标文档「**锁死 cadence = 20（严格月频）**」「**不准搜索 90/120/150**」
      —— 4 个月一调仓在 A 股抓不住行业轮动，而 V2 的 20 日预测 RankIC 高达 0.1271
      ⇒ **时效性 > 换手率**；
    * 降换手的正确姿势 = 「月频 + Rank Buffer（宽出严进）」，**不是**拉长调仓周期；
    * 历史档位保留在 ``REBALANCE_CADENCE_GRID_LEGACY``（**只供审计对表**，不进搜索）。
    """
    assert C.REBALANCE_CADENCE_GRID == (20,), (
        f"🔴 cadence 必须锁死 20，实得 {C.REBALANCE_CADENCE_GRID}"
    )
    assert C.REBALANCE_CADENCE_GRID_LEGACY == (20, 40, 60, 90), (
        "🔴 历史 cadence 档位必须留档（审计对表用）"
    )
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    assert grid["rebalance_every_n_days"] == [20], "🔴 搜索空间里只该有 cadence=20"
    # 🔴 锁死**不许**静默回退到历史档
    assert 90 not in grid["rebalance_every_n_days"] and 150 not in grid["rebalance_every_n_days"], (
        "🔴 长周期 cadence 不许回到搜索空间"
    )


# ----------------------------------------------------------------- ⑤ 换手闸门语义
def test_闸门与cadence无关_达标时取最高分() -> None:
    """🔴 闸门只看换手：达标 trial 里取 score 最高者（cadence 无关）。"""
    result = {
        "trials": [
            _trial(0.50, 3.0, {"rebalance_every_n_days": 90}),
            _trial(0.30, 9.0, {"rebalance_every_n_days": 20}),
        ],
        "best_params": {"rebalance_every_n_days": 90},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["gate"]["ok"] is True
    assert gated["best_params"]["rebalance_every_n_days"] == 90


def test_闸门与cadence无关_全超标时按主目标取分且未达成() -> None:
    """🔴 全员超标 ⇒ 按**主目标（扣费后净超额）**取分 + ``ok=False``（不许假装达标）。

    🔴 2026-11-18 用户目标文档修正：旧口径是「取**换手最低**者」
    ⇒ **正是它**把 cadence=90 选成了胜者（换手 5.3× 对目标 4×，代价是「4 个半月才动一次」）
    ⇒ 单盲 **−12.93% / 回撤 −54.93%**。
    ⇒ 现在改成：按主目标取分，**并如实披露**「≤4× 未达成」
    （既不许假装达标，也不许用换手率把 alpha 买断）。
    """
    result = {
        "trials": [
            _trial(0.50, 9.0, {"rebalance_every_n_days": 20}),
            _trial(0.10, 6.0, {"rebalance_every_n_days": 40}),
        ],
        "best_params": {"rebalance_every_n_days": 20},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["gate"]["ok"] is False
    assert gated["best_params"]["rebalance_every_n_days"] == 20, (
        "🔴 全员超标时必须按**主目标**取分者（score 0.50 那个）"
        "⇒ 取换手最低者 = 把 cadence=90 又选回来了（旧事故）"
    )
    assert "未达成" in gated["gate"]["message"]


def test_旧口径min_turnover仍可切回_仅供审计对表(monkeypatch) -> None:
    """🔴 旧兜底口径**必须仍然可达**（审计对表用），但**默认不是它**。"""
    monkeypatch.setattr(RV.C, "TURNOVER_GATE_ON_MISS", "min_turnover")
    result = {
        "trials": [
            _trial(0.50, 9.0, {"rebalance_every_n_days": 20}),
            _trial(0.10, 6.0, {"rebalance_every_n_days": 40}),
        ],
        "best_params": {"rebalance_every_n_days": 20},
        "best_score": 0.5,
    }
    gated = RV.apply_turnover_gate(result, 4.0)
    assert gated["best_params"]["rebalance_every_n_days"] == 40, (
        "🔴 切到旧口径后必须回到「换手最低者」"
    )
    assert gated["gate"]["on_miss"] == "min_turnover"


def test_闸门在没有任何有效trial时抛错() -> None:
    """🔴 缺失（无有效 trial）⇒ 抛错，不许挑 winner。"""
    with pytest.raises(RuntimeError):
        RV.apply_turnover_gate({"trials": [], "best_params": {}, "best_score": None}, 4.0)


# ----------------------------------------------------------------- ⑥ 披露
def test_披露块必须含逐档统计与样本标注(monkeypatch) -> None:
    """🔴 披露块：逐档 trial 数 / 最低换手 / 调仓次数 / 样本充足性 / 与 V1 可比性。

    🔴 **2026-11-18**：生产 cadence 网格已锁死 ``(20,)``；但披露块的**多档能力**
    仍需保留（探针 / 历史产物对表要用）⇒ 这里把网格临时换回**历史档**来验多档披露。
    """
    monkeypatch.setattr(C, "REBALANCE_CADENCE_GRID", C.REBALANCE_CADENCE_GRID_LEGACY)
    trials = [
        _trial(0.5, 12.0, {"rebalance_every_n_days": 20}, n_rebalances=23),
        _trial(0.4, 6.5, {"rebalance_every_n_days": 60}, n_rebalances=8),
        _trial(0.3, 3.4, {"rebalance_every_n_days": 90}, n_rebalances=5),
    ]
    disc = RV.build_cadence_disclosure(trials, {"rebalance_every_n_days": 90})
    assert disc["candidates"] == [20, 40, 60, 90]
    assert disc["v1_equivalent_cadence"] == C.REBALANCE_EVERY_N_DAYS
    assert disc["selected_cadence"] == 90
    assert disc["selected_rebalance_signal_stride"] == EXPECTED_STRIDE[90]
    per = disc["per_cadence"]
    assert per["20"]["n_rebalances"] == 23
    assert per["20"]["comparable_to_v1"] is True
    assert per["40"]["n_trials"] == 0 and per["40"]["best_trial"] is None
    assert per["60"]["any_trial_within_turnover_target"] is False
    assert per["90"]["any_trial_within_turnover_target"] is True
    # 🔴 样本不足 ⇒ 必须打上「样本不足，不可据此选参」
    assert "样本不足" in per["90"]["sample_note"], (
        f"🔴 cadence=90 只有 5 次调仓 ⇒ 必须标注样本不足：{per['90']['sample_note']!r}"
    )
    assert "不可据此选参" in per["90"]["sample_note"]
    assert "样本不足" not in per["20"]["sample_note"], (
        "🔴 cadence=20 有 23 次调仓 ⇒ 不该被打上样本不足"
    )


def test_披露口径四条必须齐() -> None:
    """🔴 四条披露口径（同口径可见 / 不同口径不可比 / 噪声 / 样本不足）逐条落地。"""
    notes = " ".join(RV.cadence_disclosure_notes())
    # 🔴 2026-11-17 夜班审计修正：原口径写「cadence = 20 与 V1 **同口径** ⇒ 可直接对表」，
    #    **过强** —— 即使 cadence 相同，换手的**权重基准**仍不同源
    #    （V1 = 上期**目标**权重未漂移／V2 = 调仓前**漂移**权重）
    #    ⇒ 披露必须改成「节奏相同、但只趋势性对比」。
    assert "节奏**与 V1 相同" in notes, (
        f"🔴 披露必须写明「cadence = 20 与 V1 的**节奏**相同」：{notes!r}"
    )
    assert "只能趋势性对比" in notes, (
        f"🔴 披露必须写明「只能趋势性对比、不是同源」：{notes!r}"
    )
    assert "不可直接对比" in notes
    assert "噪声" in notes
    assert "样本不足" in notes
    assert "n_rebalances" in notes, "🔴 披露必须点名调仓次数字段（n_rebalances）"


def test_臂对照产物必须含cadence披露(monkeypatch) -> None:
    """🔴 ``arm_search_val.json`` / ``val_search.json`` 的载荷契约里必须有 cadence 披露。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    fake = {
        "best_params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                        # 🔴 2026-11-18：生产 cadence 网格锁死 20；本测试用**历史档 60**
                        #    + monkeypatch 到 legacy 网格 ⇒ 验「多档披露」这条能力不丢。
                        "rebalance_every_n_days": 60},
        "best_score": 0.4,
        "n_trials_run": 1,
        "n_trials_total": 1,
        "truncated": False,
        "val_window": list(C.VALID),
        "trials": [{
            "params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                       "rebalance_every_n_days": 60},
            "score": 0.4,
            "objective": {"annualized_return": 0.2, "excess_vs_benchmark": 0.1,
                          "annualized_turnover": 5.0, "max_drawdown": -0.12,
                          "n_rebalances": 8},
            "status": "ok",
        }],
    }
    monkeypatch.setattr(C, "REBALANCE_CADENCE_GRID", C.REBALANCE_CADENCE_GRID_LEGACY)
    payload = RV.build_arm_search_payload(fake, grid, "ablate_roc5")
    assert payload["rebalance_every_n_days"] == 60
    assert payload["rebalance_signal_stride"] == EXPECTED_STRIDE[60]
    assert payload["rebalance_candidates"] == [20, 40, 60, 90]
    disc = payload["cadence_disclosure"]
    assert disc["selected_cadence"] == 60
    assert disc["per_cadence"]["60"]["n_rebalances"] == 8
    assert "样本不足" in disc["per_cadence"]["60"]["sample_note"]
    assert payload["per_arm"]["A_half_life_20"]["rebalance_every_n_days"] == 60, (
        "🔴 逐臂必须标出该臂最佳 trial 的 cadence（臂对比跨 cadence ⇒ 必须披露）"
    )
    # 🔴 载荷必须能原样写成 json（产物落盘）
    json.dumps(payload, ensure_ascii=False, default=str)


def test_冻结产物必须含cadence(tmp_path: Path) -> None:
    """🔴 冻结产物（含 cadence）⇒ 盲测 / 报告用**同一个**节奏，不许回落常量。"""
    import argparse

    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    fake = {
        "best_params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                        "rebalance_every_n_days": 20},
        "best_score": 0.4,
        "n_trials_run": 1,
        "n_trials_total": 1,
        "truncated": False,
        "val_window": list(C.VALID),
        "trials": [{
            "params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                       "rebalance_every_n_days": 20},
            "score": 0.4,
            "objective": {"annualized_turnover": 5.0, "n_rebalances": 8},
            "status": "ok",
        }],
    }
    payload = RV.build_arm_search_payload(fake, grid, "ablate_roc5")
    (tmp_path / "arm_search_val.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    RV.stage_freeze(argparse.Namespace(), tmp_path)

    data = json.loads((tmp_path / "frozen_params.json").read_text(encoding="utf-8"))
    assert data["params"]["rebalance_every_n_days"] == 20, (
        f"🔴 冻结产物里没有 cadence：{data['params']}"
    )
    assert data["rebalance_signal_stride"] == EXPECTED_STRIDE[20]
    assert oct((tmp_path / "frozen_params.json").stat().st_mode)[-3:] == "444", (
        "🔴 冻结产物必须是只读（0444）"
    )


def test_成绩单披露章节必须能渲染(monkeypatch) -> None:
    """🔴 披露章节（V2 报告 / 成绩单）必须能渲染出**逐档表格 + 样本标注**。"""
    monkeypatch.setattr(C, "REBALANCE_CADENCE_GRID", C.REBALANCE_CADENCE_GRID_LEGACY)
    disc = RV.build_cadence_disclosure(
        [_trial(0.5, 12.0, {"rebalance_every_n_days": 20}, n_rebalances=23),
         _trial(0.3, 3.4, {"rebalance_every_n_days": 90}, n_rebalances=5)],
        {"rebalance_every_n_days": 90},
    )
    md = "\n".join(RV.render_cadence_disclosure_markdown(disc))
    assert "调仓节奏（cadence）口径披露" in md
    assert "| 20 |" in md and "| 90 |" in md
    assert "样本不足" in md, "🔴 披露章节必须带出「样本不足」标注"
    assert "不可据此选参" in md
    assert "不可直接对比" in md


def test_成绩单必须被追加披露章节(tmp_path: Path) -> None:
    """🔴 ``stage_report`` 之后成绩单（``V2_SCOREBOARD.md``）里必须有 cadence 披露章节。"""
    disc = RV.build_cadence_disclosure(
        [_trial(0.5, 12.0, {"rebalance_every_n_days": 20}, n_rebalances=23)],
        {"rebalance_every_n_days": 20},
    )
    (tmp_path / "arm_search_val.json").write_text(
        json.dumps({"cadence_disclosure": disc}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "V2_SCOREBOARD.md").write_text("# 成绩单\n\n原有内容\n", encoding="utf-8")
    out = RV.append_cadence_disclosure_to_scorecard(tmp_path)
    assert out is not None and out.is_file()
    body = out.read_text(encoding="utf-8")
    assert "原有内容" in body, "🔴 追加披露不许覆盖成绩单原有内容"
    assert "调仓节奏（cadence）口径披露" in body
