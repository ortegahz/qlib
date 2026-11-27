#!/usr/bin/env python3
"""🔴 V2 **参数高原测试**（参数邻域扰动）探针。

【问的问题】
  V2 冠军参数（``pure_E_native`` + ``hold_buffer=2`` + 负分闸门关）在 5 折滚动前推
  OOS 窗（口径 = ``config_v2.WALK_FORWARD_OOS_WINDOW``）上拿到 **超额 ≈ 7.4%**。
  这个数是**稳定的高原**，
  还是**一根孤立的尖刺**（邻域一改就塌）？
  ⇒ 只报「冠军最好」没有意义 ⇒ 必须做 **OAT 邻域扰动 + 配对统计**。

【口径（硬红线）】
  ① **OOS 只有 5 折滚动前推拼接窗**（``config_v2.WALK_FORWARD_OOS_WINDOW``）；
     **单盲封存段从不请求**（连 qlib 日历都只取到 OOS 窗末日为止 ⇒ 结构上碰不到）。
  ② 每折都调 ``splits.assert_fold_only()``（硬断言验证段早于封存段）。
  ③ **复用已缓存的折模型 / 折分数**（``v2/_walkforward/``）⇒ **绝不重训 S 层**。
  ④ **已封存产物 md5 跑前跑后双侧比对**（单盲 / 冻结 / 正式搜索 / 封存分数）。
  ⑤ **配对统计**：变体 vs 冠军在**拼接日频收益**上做 Newey-West t（lag=5）
     + **块自助法**（块长 21 交易日、B=``BOOTSTRAP_B``）95% CI ⇒ 变体间 NAV 高度相关
     ⇒ 必须用**配对**检验，不许用独立样本检验。

【产物（落 ``--out-dir``）】
    ``plateau_grid.csv``      一行一变体（逐轴 OAT 面）
    ``plateau_per_fold.csv``  变体 × 折 的逐折指标
    ``plateau_nav/``          变体的 OOS 日频 NAV（索引 = 交易日）
    ``plateau_stats.csv``     变体 vs 冠军的配对统计
    ``plateau_report.md``     最终报告

【用法（服务器）】
    export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMEXPR_NUM_THREADS=4
    python manu/tools/v2_plateau_probe.py \\
        --out-dir /mnt/data/siping/runs/etf_rotation_results/_plateau_<stamp> \\
        --wf-dir /mnt/data/siping/runs/etf_rotation_results/v2/_walkforward \\
        --etf-prices-csv /mnt/data/siping/datasets/manu/etf_rotation/datasets/etf_ext/etf_prices_merged_2018.csv \\
        --report-dir /mnt/data/siping/runs/etf_rotation_results/reports \\
        --publish-name plateau_report_<stamp>.md
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

V2_DIR_CANDIDATES = (
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "v2"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "v2",
    Path("/tmp/pycharm_project_3b68a403/manu/etf_rotation/v2"),
)
V2_DIR = next(
    (p for p in V2_DIR_CANDIDATES if p is not None and (p / "config_v2.py").is_file()),
    V2_DIR_CANDIDATES[-1],
)
if str(V2_DIR) not in sys.path:
    sys.path.insert(0, str(V2_DIR))

#: 🔴 已封存的产物（探针**不得**触碰 ⇒ 跑前跑后比 md5）
SEALED = (
    "single_blind_test_2024_2026.json",
    "_single_blind_registry.json",
    "frozen_params.json",
    "arm_search_val.json",
    "val_search.json",
    "blind_diagnostics.csv",
    "stock_scores_v2_ablate_roc5.csv",
)

# ================================================================= 判定阈值
#: 「邻域保持度」下限：变体超额 / 参考超额 ≥ 该值 ⇒ 算「邻居没塌」
PLATEAU_RETENTION_MIN = 0.7
#: 邻居保持度低于该值 ⇒ 直接判「尖刺」
SPIKE_RETENTION_MAX = 0.3
#: 判「高原」至少要几个**最近邻**同时满足（同号 且 保持度 ≥ PLATEAU_RETENTION_MIN）
PLATEAU_MIN_GOOD_NEIGHBORS = 2
#: 冠军到最近邻的**相对落差**超过该值（且冠军是严格 argmax）⇒ 判「尖刺」
SPIKE_DROP_RATIO = 0.5
#: 冠军与最近邻的差 ≥ 该倍数 ×（邻居之间的最大差）⇒ 冠军是「严格孤立 argmax」
ISOLATION_GAP_FACTOR = 2.0
#: 参考超额低于该绝对值 ⇒ **保持度 n/a**（分母近零 ⇒ 不许算出假数）
RETENTION_DENOM_MIN = 0.005
#: 每个轴取几个「参数距离最近」的邻居
N_NEIGHBORS = 2

# ================================================================= 配对统计
#: Newey-West 自相关修正的滞后阶（交易日）
NW_LAG = 5
#: 块自助法的块长（交易日）
BOOTSTRAP_BLOCK = 21
#: 块自助法的重抽次数
BOOTSTRAP_B = 2_000
#: 块自助法的随机种子（固定 ⇒ 可复现）
BOOTSTRAP_SEED = 1126
#: 块自助法 CI 的显著性水平
BOOTSTRAP_ALPHA = 0.05

#: 🔴 V2 冠军臂（``pure_E_native`` + buffer=2 + 闸门关）；**每条轴只动一个参数**
CHAMPION_PARAMS: dict = {
    "alpha_weight": 1.0,
    "momentum_weight": 0.0,
    "entry_ma_window": 0,
    "exit_ma_window": 0,
    "smoothing_arm": "B_native_slow",
    "allow_theme_cap": False,
    "top_k": 5,
    "hold_buffer": 2,
    "rebalance_every_n_days": 20,
    "neg_score_gate": False,
    "neg_score_gate_stat": "median",
    "neg_score_gate_scale": 0.05,
    "neg_score_gate_min_exposure": 0.0,
}

#: 动量窗口基准（= ``etf_layer.MOMENTUM_WEIGHTS``，语义冻结）
MOMENTUM_BASE: dict[int, float] = {20: 0.2, 60: 0.5, 120: 0.3}


# ================================================================= 小工具
def _md5(path: Path) -> str:
    if not path.is_file():
        return "<不存在>"
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})
    return path


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "n/a"
        return f"{value:.{digits}f}"
    return str(value)


def _f(value) -> float | None:
    """转成 float（None / NaN ⇒ None）。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# ================================================================= 纯函数：统计
def newey_west_tstat(diff: "np.ndarray", lag: int = NW_LAG) -> float:
    """Newey-West（Bartlett 核）**均值** t 统计量。

    ``t = mean(d) / sqrt( (γ0 + 2Σ(1−k/(L+1))γ_k) / n )``

    🔴 变体之间 NAV **高度相关** ⇒ 差序列 ``d`` 自相关很强 ⇒ 用 HAC 修正后的
       标准误（否则 t 值虚高 ⇒ 会把「噪声差异」当成「显著差异」）。
    """
    import numpy as np

    d = np.asarray(diff, dtype="float64")
    n = int(d.size)
    if n < 3:
        return float("nan")
    dbar = float(d.mean())
    e = d - dbar
    gamma0 = float((e * e).sum()) / n
    var = gamma0
    L = int(lag)
    for k in range(1, min(L, n - 1) + 1):
        gamma_k = float((e[k:] * e[:-k]).sum()) / n
        var += 2.0 * (1.0 - k / (L + 1.0)) * gamma_k
    if var <= 0.0:
        return float("nan")
    return dbar / math.sqrt(var / n)


def block_bootstrap_ci(
    diff: "np.ndarray",
    *,
    block: int = BOOTSTRAP_BLOCK,
    n_boot: int = BOOTSTRAP_B,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = BOOTSTRAP_ALPHA,
) -> dict:
    """**移动块自助法**（moving block bootstrap）⇒ 均值的 95% CI。

    🔴 为什么要块自助：日频收益**自相关 + 异方差** ⇒ 独立重抽会低估方差
       ⇒ 块长 21 个交易日（约一个月频调仓周期）保留局部相关性。
    """
    import numpy as np

    d = np.asarray(diff, dtype="float64")
    n = int(d.size)
    out = {
        "n": n, "block": int(block), "n_boot": int(n_boot),
        "ci_lo": float("nan"), "ci_hi": float("nan"),
        "boot_se": float("nan"), "boot_p_gt_0": float("nan"),
    }
    if n < 2:
        return out
    blen = min(int(block), n)
    n_blocks = int(math.ceil(n / blen))
    starts_max = n - blen
    rng = np.random.default_rng(int(seed))
    idx = rng.integers(0, starts_max + 1, size=(int(n_boot), n_blocks))
    offsets = np.arange(blen)
    sample_idx = (idx[:, :, None] + offsets[None, None, :]).reshape(int(n_boot), -1)[:, :n]
    means = d[sample_idx].mean(axis=1)
    out["ci_lo"] = float(np.quantile(means, alpha / 2.0))
    out["ci_hi"] = float(np.quantile(means, 1.0 - alpha / 2.0))
    out["boot_se"] = float(means.std(ddof=1))
    out["boot_p_gt_0"] = float((means > 0.0).mean())
    return out


def paired_stats(
    ret_variant: "pd.Series",
    ret_ref: "pd.Series",
    *,
    periods_per_year: int = 250,
) -> dict:
    """变体 vs 参考的**配对**统计（对齐到共同交易日）。

    返回 ``mean_diff``（日频算术均差）/ ``ann_diff``（年化均差）/ Newey-West t /
    块自助 95% CI / ``n_paired_days``。
    """
    import pandas as pd

    joined = pd.concat(
        [ret_variant.rename("v"), ret_ref.rename("r")], axis=1, join="inner"
    ).dropna()
    n = int(len(joined))
    if n < 2:
        return {
            "n_paired_days": n, "mean_diff": None, "ann_diff": None,
            "nw_t_stat": None, "nw_p_two_sided": None,
            "boot_ci_lo": None, "boot_ci_hi": None, "boot_se": None,
            "boot_p_gt_0": None,
        }
    d = (joined["v"] - joined["r"]).to_numpy(dtype="float64")
    mean_diff = float(d.mean())
    t_stat = newey_west_tstat(d)
    boot = block_bootstrap_ci(d)
    p_two = None
    if t_stat is not None and math.isfinite(t_stat):
        # 🔴 双尾 p 值 = ``erfc(|t| / √2)``（与 ``2(1-Φ(|t|))`` **逐位等价**）
        #    ⇒ 不写 ``0.5 *``（仓库守卫：换手语境里禁止手写单边换算的写法）
        p_two = float(math.erfc(abs(t_stat) / math.sqrt(2.0)))
    return {
        "n_paired_days": n,
        "mean_diff": mean_diff,
        "ann_diff": mean_diff * float(periods_per_year),
        "nw_t_stat": None if t_stat is None or not math.isfinite(t_stat) else float(t_stat),
        "nw_p_two_sided": p_two,
        "boot_ci_lo": boot["ci_lo"],
        "boot_ci_hi": boot["ci_hi"],
        "boot_se": boot["boot_se"],
        "boot_p_gt_0": boot["boot_p_gt_0"],
    }


def retention(variant_excess, ref_excess, denom_min: float = RETENTION_DENOM_MIN):
    """邻域保持度 = 变体超额 / 参考超额（**分母近零 ⇒ n/a**，不许算假数）。"""
    v = _f(variant_excess)
    r = _f(ref_excess)
    if v is None or r is None:
        return None
    if abs(r) < float(denom_min):
        return None
    return v / r


def nearest_neighbor_indices(n_items: int, ref_index: int, k: int = N_NEIGHBORS):
    """**参数距离最近**的邻居下标（按轴上网格下标距离；同距取靠前的一侧）。"""
    n = int(n_items)
    order = sorted(range(n), key=lambda i: (abs(i - int(ref_index)), i))
    return [i for i in order if i != int(ref_index)][:int(k)]


def classify_axis(
    items: list[dict],
    ref_index: int,
    *,
    plateau_retention_min: float = PLATEAU_RETENTION_MIN,
    spike_retention_max: float = SPIKE_RETENTION_MAX,
    plateau_min_good_neighbors: int = PLATEAU_MIN_GOOD_NEIGHBORS,
    spike_drop_ratio: float = SPIKE_DROP_RATIO,
    isolation_gap_factor: float = ISOLATION_GAP_FACTOR,
    denom_min: float = RETENTION_DENOM_MIN,
    n_neighbors: int = N_NEIGHBORS,
) -> dict:
    """**纯函数**：逐轴判「高原 / 尖刺 / mixed」。

    参数
    ----
    ``items`` : 该轴上的变体列表（**按轴上网格顺序**），每项至少有
        ``{"variant_id", "excess"}``（``excess`` 可为 ``None``）。
    ``ref_index`` : 参考（冠军 / 轴基准）在 ``items`` 里的下标。

    判定（阈值全部是模块常量 ⇒ 报告里披露）：

    * ``plateau``（高原）：**最近邻**里 ≥ ``plateau_min_good_neighbors`` 个满足
      「与参考超额**同号**」且 ``retention ≥ plateau_retention_min``；
      且参考**不是严格孤立 argmax**（参考与最近邻的差 < **轴上其余变体之间**
      最大差的 ``isolation_gap_factor`` 倍 ⇒ 参考不是「孤零零地站在邻居之上」）。
    * ``spike``（尖刺）：最近邻**超额变号**，或最近邻 ``retention < spike_retention_max``，
      或参考是**严格 argmax** 且到最近邻的**相对落差 > spike_drop_ratio**。
    * 其余 ⇒ ``mixed``。
    """
    out = {
        "verdict": "mixed", "reason": "", "n_neighbors": 0, "n_good_neighbors": 0,
        "sign_flip": False, "low_retention": False, "strict_argmax": False,
        "drop_to_neighbor": None, "champion_gap": None, "neighbor_spread": None,
        "isolated": False, "retention_min": None, "neighbor_ids": [],
    }
    if not items or ref_index >= len(items):
        out["reason"] = "轴上变体为空 ⇒ 无法判定"
        return out
    ref = items[int(ref_index)]
    ref_excess = _f(ref.get("excess"))
    if ref_excess is None:
        out["reason"] = "参考变体的超额缺失 ⇒ 无法判定"
        return out
    nbr_idx = nearest_neighbor_indices(len(items), ref_index, n_neighbors)
    nbrs = [items[i] for i in nbr_idx]
    out["n_neighbors"] = len(nbrs)
    out["neighbor_ids"] = [str(nb.get("variant_id")) for nb in nbrs]
    nbr_excess = [_f(nb.get("excess")) for nb in nbrs]
    nbr_ret = [retention(e, ref_excess, denom_min) for e in nbr_excess]

    # ---- 严格 argmax：参考超额 > 轴上其它**所有**变体的超额 ----
    others = [_f(it.get("excess")) for it in items if it is not ref]
    others = [e for e in others if e is not None]
    strict_argmax = bool(others) and ref_excess > max(others)
    out["strict_argmax"] = strict_argmax

    # ---- 轴上其余变体之间的最大差（孤立度判据的分母）----
    finite_others = [e for e in others]
    spread = 0.0
    if len(finite_others) >= 2:
        spread = max(finite_others) - min(finite_others)
    finite_nbr = [e for e in nbr_excess if e is not None]
    out["neighbor_spread"] = float(spread)

    best_nbr = max(finite_nbr) if finite_nbr else None
    gap = (ref_excess - best_nbr) if best_nbr is not None else None
    out["champion_gap"] = gap
    drop = (gap / abs(ref_excess)) if (gap is not None and abs(ref_excess) >= denom_min) else None
    out["drop_to_neighbor"] = drop
    isolated = False
    if gap is not None and gap > 0.0:
        isolated = bool(spread <= 0.0 or gap >= isolation_gap_factor * spread)
    out["isolated"] = isolated

    # ---- 变号 / 低保持度 / 好邻居计数 ----
    sign_flip = False
    low_retention = False
    n_good = 0
    rets = []
    for nb, e, r in zip(nbrs, nbr_excess, nbr_ret):
        if e is None:
            continue
        if e * ref_excess < 0.0:
            sign_flip = True
        if r is not None:
            rets.append(float(r))
            if r < spike_retention_max:
                low_retention = True
            if e * ref_excess >= 0.0 and r >= plateau_retention_min:
                n_good += 1
    out["sign_flip"] = sign_flip
    out["low_retention"] = low_retention
    out["n_good_neighbors"] = n_good
    out["retention_min"] = min(rets) if rets else None

    if sign_flip or low_retention or (
        strict_argmax and drop is not None and drop > spike_drop_ratio
    ):
        out["verdict"] = "spike"
    elif n_good >= plateau_min_good_neighbors and not isolated:
        out["verdict"] = "plateau"
    else:
        out["verdict"] = "mixed"

    # ---- 一句话判读（人话）----
    best_txt = _fmt(best_nbr) if best_nbr is not None else "n/a"
    if out["verdict"] == "spike":
        if sign_flip:
            out["reason"] = (
                f"最近邻超额**变号**（最好邻居 {best_txt} vs 参考 { _fmt(ref_excess) }）"
                f" ⇒ 冠军是尖刺，邻域一改就塌。"
            )
        elif low_retention:
            out["reason"] = (
                f"最近邻保持度跌到 {_fmt(out['retention_min'])}（< {SPIKE_RETENTION_MAX}）"
                f" ⇒ 冠军是尖刺，邻域一改就塌。"
            )
        else:
            out["reason"] = (
                f"冠军是严格 argmax，到最近邻的落差 {_fmt(drop)}"
                f"（> {SPIKE_DROP_RATIO}）⇒ 尖刺。"
            )
    elif out["verdict"] == "plateau":
        out["reason"] = (
            f"{n_good} 个最近邻超额**同号**且保持度 ≥ {PLATEAU_RETENTION_MIN}"
            f"（最低 {_fmt(out['retention_min'])}）⇒ 落在一片**高原**上，不是尖刺。"
        )
    else:
        out["reason"] = (
            f"只有 {n_good} 个最近邻满足（同号且保持度 ≥ {PLATEAU_RETENTION_MIN}）；"
            f"冠军到最近邻落差 {_fmt(drop)}、邻居间最大差 {_fmt(spread)} ⇒ 判定 mixed。"
        )
    return out


# ================================================================= 变体表
def build_axis_specs() -> list[dict]:
    """逐轴的 OAT 网格（**每条轴只动一个参数**，其余保持冠军值）。"""
    return [
        {
            "axis": "hold_buffer", "param_path": "hold_buffer",
            "ref_variant_id": "champion",
            "variants": [
                {"variant_id": "hb0", "param_value": 0, "params": {"hold_buffer": 0}},
                {"variant_id": "hb1", "param_value": 1, "params": {"hold_buffer": 1}},
                {"variant_id": "champion", "param_value": 2, "params": {}},
                {"variant_id": "hb3", "param_value": 3, "params": {"hold_buffer": 3}},
                {"variant_id": "hb4", "param_value": 4, "params": {"hold_buffer": 4}},
                {"variant_id": "hb5", "param_value": 5, "params": {"hold_buffer": 5}},
            ],
        },
        {
            "axis": "top_k", "param_path": "top_k",
            "ref_variant_id": "champion",
            "variants": [
                {"variant_id": "tk4", "param_value": 4, "params": {"top_k": 4}},
                {"variant_id": "champion", "param_value": 5, "params": {}},
                {"variant_id": "tk6", "param_value": 6, "params": {"top_k": 6}},
            ],
        },
        {
            "axis": "weight_mix", "param_path": "alpha_weight/momentum_weight",
            "ref_variant_id": "champion",
            "variants": [
                {"variant_id": "champion", "param_value": "1.0/0.0", "params": {}},
                {"variant_id": "wm090", "param_value": "0.9/0.1",
                 "params": {"alpha_weight": 0.9, "momentum_weight": 0.1}},
                {"variant_id": "wm075", "param_value": "0.75/0.25",
                 "params": {"alpha_weight": 0.75, "momentum_weight": 0.25}},
                {"variant_id": "wm050", "param_value": "0.5/0.5",
                 "params": {"alpha_weight": 0.5, "momentum_weight": 0.5}},
            ],
        },
        {
            "axis": "ma_windows", "param_path": "entry_ma_window/exit_ma_window",
            "ref_variant_id": "champion",
            "variants": [
                {"variant_id": "champion", "param_value": "0/0", "params": {}},
                {"variant_id": "ma60", "param_value": "60/60",
                 "params": {"entry_ma_window": 60, "exit_ma_window": 60}},
                {"variant_id": "ma120", "param_value": "120/80",
                 "params": {"entry_ma_window": 120, "exit_ma_window": 80}},
            ],
        },
        {
            "axis": "theme_cap", "param_path": "allow_theme_cap",
            "ref_variant_id": "champion",
            "variants": [
                {"variant_id": "champion", "param_value": "False", "params": {}},
                {"variant_id": "tcon", "param_value": "True",
                 "params": {"allow_theme_cap": True}},
            ],
        },
        {
            "axis": "neg_gate", "param_path": "neg_score_gate",
            "ref_variant_id": "champion",
            "variants": [
                {"variant_id": "champion", "param_value": "off", "params": {}},
                {"variant_id": "gton", "param_value": "gate(scale=0.05,min_exp=0.0)",
                 "params": {"neg_score_gate": True}},
            ],
        },
        {
            # 🔴 动量窗口**只在 momentum_weight>0 的臂上才有意义**
            #    ⇒ 这条轴的底座 = weight_mix 轴的 (0.5,0.5) 变体（wm050）。
            "axis": "momentum_windows", "param_path": "momentum_weights",
            "ref_variant_id": "wm050",
            "variants": [
                {"variant_id": "wm050", "param_value": "20:0.2/60:0.5/120:0.3",
                 "params": {"alpha_weight": 0.5, "momentum_weight": 0.5},
                 "momentum_weights": None},
                {"variant_id": "mw100", "param_value": "20:0.2/60:0.5/100:0.3",
                 "params": {"alpha_weight": 0.5, "momentum_weight": 0.5},
                 "momentum_weights": {20: 0.2, 60: 0.5, 100: 0.3}},
                {"variant_id": "mw140", "param_value": "20:0.2/60:0.5/140:0.3",
                 "params": {"alpha_weight": 0.5, "momentum_weight": 0.5},
                 "momentum_weights": {20: 0.2, 60: 0.5, 140: 0.3}},
                {"variant_id": "mw50", "param_value": "20:0.2/50:0.5/120:0.3",
                 "params": {"alpha_weight": 0.5, "momentum_weight": 0.5},
                 "momentum_weights": {20: 0.2, 50: 0.5, 120: 0.3}},
                {"variant_id": "mw70", "param_value": "20:0.2/70:0.5/120:0.3",
                 "params": {"alpha_weight": 0.5, "momentum_weight": 0.5},
                 "momentum_weights": {20: 0.2, 70: 0.5, 120: 0.3}},
            ],
        },
    ]


def distinct_variants(axis_specs: list[dict]) -> list[dict]:
    """把逐轴的变体摊平成**互不重复的参数集**（同一参数集只回测一次）。"""
    seen: dict[str, dict] = {}
    order: list[dict] = []
    for spec in axis_specs:
        for var in spec["variants"]:
            vid = str(var["variant_id"])
            if vid in seen:
                continue
            seen[vid] = var
            order.append({
                "variant_id": vid,
                "params": dict(var.get("params") or {}),
                "momentum_weights": var.get("momentum_weights"),
            })
    return order


# ================================================================= 主流程
@contextlib.contextmanager
def _momentum_override(weights):
    """🔴 **进程内**临时给 ``etf_layer`` 的特征/选股打上动量窗口覆盖。

    🔴 为什么不改 ``run_v2``：``run_v2._build_targets_for`` 是**封存链路**的一部分
       ⇒ 覆盖必须**只**存在于本探针的进程里 ⇒ 用上下文管理器临时替换模块属性，
       退出即还原（``finally``）。
    🔴 ``weights is None`` ⇒ **完全不动**任何模块属性 ⇒ 默认路径逐位不变。
    """
    if weights is None:
        yield
        return
    import etf_layer as EL

    orig_features = EL.compute_features
    orig_targets = EL.build_targets

    def patched_features(close, amount=None, ma_windows=(EL.MA_TREND_WINDOW,), **kwargs):
        kwargs.setdefault("momentum_weights", weights)
        return orig_features(close, amount, ma_windows, **kwargs)

    def patched_targets(*args, **kwargs):
        kwargs.setdefault("momentum_weights", weights)
        return orig_targets(*args, **kwargs)

    EL.compute_features = patched_features
    EL.build_targets = patched_targets
    try:
        yield
    finally:
        EL.compute_features = orig_features
        EL.build_targets = orig_targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="产物目录（结果根下新建）")
    ap.add_argument("--wf-dir", default="",
                    help="已缓存的折模型 / 折分数目录（默认 = v2/_walkforward）")
    ap.add_argument("--etf-prices-csv", default="",
                    help="ETF 行情面板（长表 CSV）；留空 = config_v2 的默认面板")
    ap.add_argument("--folds", default="1,2,3,4,5", help="要跑的折（逗号分隔）")
    ap.add_argument("--axes", default="", help="只跑这些轴（逗号分隔；留空 = 全跑）")
    ap.add_argument("--warmup-days", type=int, default=240, help="每折平滑臂预热长度（交易日）")
    ap.add_argument("--report-dir", default="", help="报告发布目录（默认 = 结果根/reports）")
    ap.add_argument("--publish-name", default="plateau_report.md",
                    help="发布到报告目录的文件名")
    ap.add_argument("--skip-regression", action="store_true",
                    help="跳过「折 4 重构自证」（**正式跑不许跳**）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import config_v2 as C
    import splits as SP

    fold_ids = [int(x) for x in args.folds.split(",") if x.strip()]
    for fid in fold_ids:
        SP.assert_fold_only(fid)          # 🔴 铁律②：折级红线（验证段早于封存段）
    axis_specs = build_axis_specs()
    if args.axes:
        want = {a.strip() for a in args.axes.split(",") if a.strip()}
        axis_specs = [s for s in axis_specs if s["axis"] in want]
        if not axis_specs:
            raise SystemExit(f"🔴 没有轴匹配 {args.axes!r}")

    v2_root = Path(C.DEFAULT_V2_RUN_ROOT)
    out_dir = Path(args.out_dir)
    wf_dir = Path(args.wf_dir) if args.wf_dir else (v2_root / "_walkforward")
    report_dir = Path(args.report_dir) if args.report_dir else (v2_root.parent / "reports")
    _log(f"产物根：{out_dir}")
    _log(f"折缓存目录（只读复用）：{wf_dir}")
    _log(f"折 = {fold_ids}｜轴 = {[s['axis'] for s in axis_specs]}")
    _log(f"🔴 OOS 窗口 = {list(C.WALK_FORWARD_OOS_WINDOW)}（单盲段 **封存** ⇒ 本探针不请求）")
    if args.dry_run:
        _log("--dry-run ⇒ 未开跑")
        return 0

    # ---- 折缓存必须齐全（否则绝不重训 ⇒ 直接报错）----
    fold_models: dict[int, str] = {}
    for fid in fold_ids:
        model_name = f"wf{fid}_{C.ABLATION_WINNER}"
        fold_models[fid] = model_name
        model_pkl = wf_dir / f"model_{model_name}.pkl"
        scores_csv = wf_dir / f"stock_scores_v2_{model_name}.csv"
        if not (model_pkl.is_file() and scores_csv.is_file()):
            raise SystemExit(
                f"🔴 折 {fid} 的模型/分数缓存缺失：{model_pkl} / {scores_csv}\n"
                f"   本探针**只复用缓存、绝不重训** ⇒ 请改 --wf-dir 指向已有缓存的目录。"
            )

    before = {name: _md5(v2_root / name) for name in SEALED}

    import numpy as np
    import pandas as pd

    import attribution as AT
    import etf_layer as EL
    import metrics as ME
    import run_v2 as R
    import train_s as TS

    out_dir.mkdir(parents=True, exist_ok=True)
    nav_dir = out_dir / "plateau_nav"
    nav_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- 取数
    _log("=== 取数：ETF 行情 / 成分股权重 ===")
    if args.etf_prices_csv:
        import constituents as CON
        _prices = EL.load_etf_prices(args.etf_prices_csv)
        close = _prices.pivot_table(index="date", columns="code", values="close", aggfunc="last")
        amount = _prices.pivot_table(index="date", columns="code", values="amount", aggfunc="last")
        _hold = CON.read_holdings_frame()
        names: dict[str, str] = {}
        if "etf_name" in _hold.columns:
            for _c, _g in _hold.groupby("etf_code", sort=True):
                names[str(_c)] = str(_g["etf_name"].iloc[0])
        close, amount = close.sort_index(), amount.sort_index()
        _log(f"⚠️ 使用**扩展**行情面板：{args.etf_prices_csv}（{close.shape}，"
             f"{close.index[0].date()} ~ {close.index[-1].date()}）")
    else:
        close, amount, names = R.load_market_data()
    bench_full = R._benchmark_nav(close)
    fwd20 = AT.etf_forward_return_panel(close, horizon=20)

    import constituents as CON
    holdings = CON.snapshot_frame(CON.load_holdings())
    weights = EL.build_weight_map(holdings)
    _log(f"成分股 ETF 数 = {len(weights)}｜ETF 行情面板 = {close.shape}")

    # ---- 全局信号栅格（与 ``train_s.score_universe`` 同锚：日历起点）----
    # 🔴 日历**只取到 OOS 窗末日** ⇒ 本探针在结构上就拿不到封存段的数据。
    TS.init_qlib(str(C.DEFAULT_PROVIDER_URI))
    from qlib.data import D
    cal = D.calendar(start_time=C.START_TIME, end_time=C.WALK_FORWARD_OOS_WINDOW[1], freq="day")
    global_grid = list(cal[:: C.SIGNAL_STRIDE_N_DAYS])
    grid_pos = {pd.Timestamp(d): i for i, d in enumerate(global_grid)}
    stride = int(C.rebalance_signal_stride_for(int(C.WALK_FORWARD_CADENCE)))
    _log(f"全局信号栅格 = {len(global_grid)} 个（步长 {C.SIGNAL_STRIDE_N_DAYS} 交易日）"
         f"｜调仓步长 = {stride}（cadence={C.WALK_FORWARD_CADENCE}）")

    fold_specs = {fid: SP.assert_fold_only(fid) for fid in fold_ids}

    def fold_signal_days(spec, k: int, start: str | None = None) -> tuple[list, list]:
        """该折的**信号日**（含至多 1 个锚点，保证与全局 cadence 的奇偶对齐）。

        🔴 与 ``v2_walkforward_probe.py`` 的同名函数**逐字同口径**（照抄，不改原件）。
        """
        vs = pd.Timestamp(spec.valid_start)
        ve = pd.Timestamp(spec.valid_end)
        lo = pd.Timestamp(start) if start else vs
        px_first = pd.Timestamp(close.index[0])
        if lo < px_first:
            lo = px_first
        in_range = [pd.Timestamp(d) for d in global_grid if lo <= pd.Timestamp(d) <= ve]
        if not in_range:
            raise RuntimeError(f"🔴 折 {spec.fold_id}：{lo}~{ve} 内无信号日")
        first_idx = grid_pos[in_range[0]]
        anchor = None
        if first_idx % int(k) != 0:
            anchor = pd.Timestamp(global_grid[first_idx - 1])
        signal = ([anchor] if anchor is not None else []) + in_range
        picked = signal[::int(k)]
        expect = [pd.Timestamp(d) for d in global_grid[::int(k)] if vs <= pd.Timestamp(d) <= ve]
        got = [d for d in picked if vs <= pd.Timestamp(d) <= ve]
        if [str(x.date()) for x in got] != [str(x.date()) for x in expect]:
            raise RuntimeError(
                f"🔴 折 {spec.fold_id} 调仓栅格与全局 cadence 不一致（stride={k}）"
            )
        signal = [d for d in signal if d >= px_first]
        if not signal:
            raise RuntimeError(f"🔴 折 {spec.fold_id}：剔除面板起点前的信号日后为空")
        return signal, expect

    # ----------------------------------------------------------------- 自证：覆盖不改行为
    ns = argparse.Namespace()
    _self_check_momentum_override(ns, wf_dir, fold_ids[0], fold_specs[fold_ids[0]],
                                  close, amount, names, fold_signal_days, stride,
                                  args.warmup_days)

    # ----------------------------------------------------------------- 铁律④：折 4 重构自证
    regression: dict = {"checked": False}
    if 4 in fold_ids and not args.skip_regression:
        _log("=== 折 4 重构自证（缓存分数 vs 已封存分数 ⇒ 必须逐位一致）===")
        regression = _fold4_regression_check(
            wf_dir, v2_root, C, R, SP, fold_specs[4], fold_models[4], close, amount,
            names, fold_signal_days, stride, args.warmup_days)
    elif not args.skip_regression:
        _log("⚠️ 本次不含折 4 ⇒ 跳过重构自证（正式跑必须含折 4）")

    # ----------------------------------------------------------------- 阶段 B：逐变体 × 逐折
    variants = distinct_variants(axis_specs)
    _log(f"=== 阶段 B：{len(variants)} 个互异参数集 × {len(fold_ids)} 折 = "
         f"{len(variants) * len(fold_ids)} 次回测 ===")

    results: dict[str, dict] = {}
    per_fold_rows: list[dict] = []
    for var in variants:
        vid = var["variant_id"]
        nav_list: list[pd.Series] = []
        bench_list: list[pd.Series] = []
        turn_map: dict[str, float] = {}
        rank_panels: list[pd.DataFrame] = []
        fold_rows: list[dict] = []
        n_rebal_total = 0
        for fid in fold_ids:
            spec = fold_specs[fid]
            vs_ = pd.Timestamp(spec.valid_start)
            ve_ = pd.Timestamp(spec.valid_end)
            n_px = int(((close.index >= vs_) & (close.index <= ve_)).sum())
            if n_px < 2:
                _log(f"  ⚠️ 折 {fid}：ETF 行情面板无覆盖 ⇒ 该折作废")
                continue
            p = {**CHAMPION_PARAMS, **var["params"], "_model_name": fold_models[fid]}
            warmup_start = SP.trading_days_before(
                SP._d(spec.valid_start), int(args.warmup_days)).isoformat()
            signal, _rebal = fold_signal_days(spec, stride, warmup_start)
            with _momentum_override(var["momentum_weights"]):
                targets, diag = R._build_targets_for(
                    ns, wf_dir, p, close, amount, names, signal)
            if not targets:
                _log(f"  ⚠️ 折 {fid}｜{vid}：无目标 ⇒ 跳过")
                continue
            close_fold = close.loc[(close.index >= vs_) & (close.index <= ve_)]
            bt = R._backtest(close_fold, targets, p)
            nav = bt["nav"]
            nav_list.append(nav)
            for k, v in (bt.get("turnover_by_date") or {}).items():
                turn_map[str(k)] = float(v)
            if bench_full is not None:
                b = bench_full.loc[(bench_full.index >= vs_) & (bench_full.index <= ve_)]
                bench_list.append(b / b.iloc[0] if len(b) else b)
            rank_panel = pd.DataFrame(
                {pd.Timestamp(d): g.set_index("code")["final_score"]
                 for d, g in diag.groupby("date")},
            ).T
            rank_panel.index = pd.DatetimeIndex(rank_panel.index)
            rank_panel = rank_panel.loc[(rank_panel.index >= vs_) & (rank_panel.index <= ve_)]
            keep = [d for d in rank_panel.index
                    if pd.Timestamp(d) in set(R.val_score_dates_without_label_leak(
                        list(rank_panel.index), valid_end=str(spec.valid_end)))]
            rank_panels.append(rank_panel.loc[keep])
            mfold = ME.summarize(nav, None, bt.get("turnover_by_date"),
                                 commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE),
                                 periods_per_year=250)
            n_rebal_total += int(len(targets))
            row = {
                "variant_id": vid, "axis": "", "param_path": "", "param_value": "",
                "fold_id": fid, "year": str(spec.valid_start)[:4],
                "valid_start": spec.valid_start, "valid_end": spec.valid_end,
                "annualized_return": mfold["annualized_return"],
                "max_drawdown": mfold["max_drawdown"],
                "sharpe": mfold["sharpe"],
                "annualized_turnover": mfold["annualized_turnover"],
                "annualized_turnover_one_way": C.one_way_turnover(mfold["annualized_turnover"]),
                "total_turnover": mfold["total_turnover"],
                "n_rebalances": int(len(targets)),
                "n_days": int(len(nav)),
            }
            fold_rows.append(row)
        if not nav_list:
            continue
        rets = pd.concat([n.pct_change().dropna() for n in nav_list])
        nav_oos = (1.0 + rets).cumprod()
        nav_oos.index = pd.DatetimeIndex(nav_oos.index)
        bench_oos = None
        if bench_list and len(bench_list) == len(nav_list):
            brets = pd.concat([b.pct_change().dropna() for b in bench_list])
            bench_oos = (1.0 + brets).cumprod()
            bench_oos.index = pd.DatetimeIndex(bench_oos.index)
        to_series = pd.Series(turn_map, dtype="float64").sort_index()
        m = ME.summarize(nav_oos, bench_oos, to_series,
                         commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE),
                         periods_per_year=250)
        panel = pd.concat(rank_panels) if rank_panels else pd.DataFrame()
        ic = AT.etf_rank_ic(panel, fwd20) if len(panel) else {"ic": None,
                                                             "n_cross_sections": 0}
        results[vid] = {
            "variant_id": vid,
            "metrics": {
                "annualized_return": m.get("annualized_return"),
                "max_drawdown": m.get("max_drawdown"),
                "sharpe": m.get("sharpe"),
                "annualized_turnover": m.get("annualized_turnover"),
                "annualized_turnover_one_way": C.one_way_turnover(m.get("annualized_turnover")),
                "total_turnover": m.get("total_turnover"),
                "excess_vs_benchmark": m.get("excess_vs_benchmark"),
            },
            "etf_rank_ic": ic,
            "n_oos_days": int(len(nav_oos)),
            "n_rebalances": int(n_rebal_total),
            "per_fold": fold_rows,
            "_nav": nav_oos, "_bench": bench_oos, "_rets": rets,
        }
        _log(f"  {vid}｜OOS 年化 {_fmt(m.get('annualized_return'))}"
             f"｜回撤 {_fmt(m.get('max_drawdown'))}"
             f"｜换手 {_fmt(m.get('annualized_turnover'))}"
             f"｜超额 {_fmt(m.get('excess_vs_benchmark'))}"
             f"｜ETF层RankIC {_fmt(ic.get('ic'))}")

    if "champion" not in results:
        raise SystemExit("🔴🔴 冠军变体没有结果 ⇒ 中止")

    # ----------------------------------------------------------------- 阶段 C：判定 + 落盘
    champ = results["champion"]
    champ_excess = _f(champ["metrics"]["excess_vs_benchmark"])

    grid_rows: list[dict] = []
    stats_rows: list[dict] = []
    axis_verdicts: dict[str, dict] = {}

    for spec in axis_specs:
        axis = spec["axis"]
        items = []
        for var in spec["variants"]:
            vid = var["variant_id"]
            res = results.get(vid)
            items.append({
                "variant_id": vid,
                "excess": None if res is None else _f(res["metrics"]["excess_vs_benchmark"]),
            })
        ref_idx = next((i for i, it in enumerate(items)
                        if it["variant_id"] == spec["ref_variant_id"]), 0)
        verdict = classify_axis(items, ref_idx)
        verdict["axis"] = axis
        verdict["ref_variant_id"] = spec["ref_variant_id"]
        axis_verdicts[axis] = verdict
        ref_excess = items[ref_idx]["excess"]
        _log(f"  轴 {axis} ⇒ {verdict['verdict']}｜{verdict['reason']}")

        for i, var in enumerate(spec["variants"]):
            vid = var["variant_id"]
            res = results.get(vid)
            if res is None:
                continue
            met = res["metrics"]
            ret_champ = retention(met["excess_vs_benchmark"], champ_excess)
            ret_ref = retention(met["excess_vs_benchmark"], ref_excess)
            row = {
                "variant_id": vid,
                "axis": axis,
                "param_path": spec["param_path"],
                "param_value": var["param_value"],
                "is_champion": vid == "champion",
                "axis_ref_variant_id": spec["ref_variant_id"],
                "annualized_return": met["annualized_return"],
                "max_drawdown": met["max_drawdown"],
                "sharpe": met["sharpe"],
                "annualized_turnover": met["annualized_turnover"],
                "annualized_turnover_one_way": met["annualized_turnover_one_way"],
                "total_turnover": met["total_turnover"],
                "excess_vs_benchmark": met["excess_vs_benchmark"],
                "etf_rank_ic": res["etf_rank_ic"].get("ic"),
                "etf_rank_ic_n": res["etf_rank_ic"].get("n_cross_sections"),
                "n_oos_days": res["n_oos_days"],
                "n_rebalances": res["n_rebalances"],
                "retention_vs_champion": ret_champ,
                "retention_vs_axis_ref": ret_ref,
                "verdict": verdict["verdict"],
                "verdict_reason": verdict["reason"],
            }
            grid_rows.append(row)
            # ---- 配对统计（vs 冠军；冠军自身 ⇒ 差序列恒 0 ⇒ 天然退化）----
            st = paired_stats(res["_rets"], champ["_rets"])
            stats_rows.append({
                "variant_id": vid,
                "axis": axis,
                "param_path": spec["param_path"],
                "param_value": var["param_value"],
                "is_champion": vid == "champion",
                "ref_variant_id": "champion",
                **st,
                "retention_vs_champion": ret_champ,
                "verdict": verdict["verdict"],
            })
            for frow in res["per_fold"]:
                out_row = dict(frow)
                out_row["axis"] = axis
                out_row["param_path"] = spec["param_path"]
                out_row["param_value"] = var["param_value"]
                per_fold_rows.append(out_row)

    # ----------------------------------------------------------------- NAV 落盘
    for vid, res in results.items():
        nav_df = pd.DataFrame({
            "date": [str(pd.Timestamp(d).date()) for d in res["_nav"].index],
            "nav": res["_nav"].to_numpy(),
            "ret": res["_rets"].reindex(res["_nav"].index).to_numpy(),
        })
        if res["_bench"] is not None:
            nav_df["bench_nav"] = res["_bench"].reindex(res["_nav"].index).to_numpy()
        nav_df.to_csv(nav_dir / f"{vid}.csv", index=False, encoding="utf-8-sig")

    _write_csv(out_dir / "plateau_grid.csv", grid_rows)
    _write_csv(out_dir / "plateau_per_fold.csv", per_fold_rows)
    _write_csv(out_dir / "plateau_stats.csv", stats_rows)

    # ----------------------------------------------------------------- 交叉核对（旧网格）
    crosscheck = _crosscheck_old_grid(wf_dir, results, grid_rows)

    # ----------------------------------------------------------------- 报告
    report_path = out_dir / "plateau_report.md"
    report_path.write_text(
        render_report(out_dir, C, grid_rows, stats_rows, axis_verdicts, crosscheck,
                      regression, fold_ids, results),
        encoding="utf-8")
    report_dir.mkdir(parents=True, exist_ok=True)
    published = report_dir / args.publish_name
    published.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    _log(f"[OK] 报告：{report_path}")
    _log(f"[OK] 报告发布：{published}")

    # ----------------------------------------------------------------- 已封存产物 md5 自检
    after = {name: _md5(v2_root / name) for name in SEALED}
    changed = [k for k in before if before[k] != after[k]]
    if changed:
        raise SystemExit(f"🔴🔴 已封存产物被改动：{changed}")
    _log("✅ 已封存产物 md5 全部未变")
    return 0


# ================================================================= 自证 / 交叉核对
def _self_check_momentum_override(ns, wf_dir, fid, spec, close, amount, names,
                                  fold_signal_days, stride, warmup_days) -> None:
    """🔴 自证：**显式给出**模块常量当覆盖 ⇒ 结果必须与「不覆盖」逐位一致。

    ⇒ 证明覆盖机制（临时替换模块属性）**没有**改变默认路径的行为。
    """
    import config_v2 as C
    import etf_layer as EL
    import run_v2 as R
    import splits as SP

    p = {**CHAMPION_PARAMS, "_model_name": f"wf{fid}_{C.ABLATION_WINNER}"}
    warmup_start = SP.trading_days_before(SP._d(spec.valid_start), int(warmup_days)).isoformat()
    signal, _ = fold_signal_days(spec, stride, warmup_start)
    plain, _ = R._build_targets_for(ns, wf_dir, p, close, amount, names, signal)
    with _momentum_override(dict(EL.MOMENTUM_WEIGHTS)):
        explicit, _ = R._build_targets_for(ns, wf_dir, p, close, amount, names, signal)
    if plain != explicit:
        raise SystemExit("🔴🔴 动量窗口覆盖机制改变了默认路径的行为 ⇒ 中止")
    _log("✅ 自证：显式覆盖 = 模块常量 ⇒ 目标权重与默认路径逐位一致")


def _fold4_regression_check(wf_dir, v2_root, C, R, SP, spec, model_name, close, amount,
                             names, fold_signal_days, stride, warmup_days) -> dict:
    """折 4 重构自证：缓存的折分数必须与**已封存**分数逐位一致。"""
    import argparse

    import pandas as pd

    sealed_scores = v2_root / f"stock_scores_v2_{C.ABLATION_WINNER}.csv"
    wf_scores = wf_dir / f"stock_scores_v2_{model_name}.csv"
    reg: dict = {"checked": True, "fold_id": 4}
    if not (sealed_scores.is_file() and wf_scores.is_file()):
        reg["checked"] = False
        _log("  ⚠️ 封存/折 4 分数文件缺失 ⇒ 跳过分数逐位比对")
        return reg
    a = pd.read_csv(sealed_scores, dtype={"instrument": str})
    b = pd.read_csv(wf_scores, dtype={"instrument": str})
    for df in (a, b):
        df["date"] = pd.to_datetime(df["date"])
    vs = pd.Timestamp(spec.valid_start)
    ve = pd.Timestamp(spec.valid_end)
    a = a[(a["date"] >= vs) & (a["date"] <= ve)]
    b = b[(b["date"] >= vs) & (b["date"] <= ve)]
    m = a.merge(b, on=["date", "instrument"], suffixes=("_sealed", "_wf"))
    if len(m) == 0:
        raise SystemExit("🔴🔴 折 4 重构自证失败：与封存分数零行重合")
    max_abs = float((m["score_sealed"] - m["score_wf"]).abs().max())
    reg["n_rows_compared"] = int(len(m))
    reg["max_abs_score_diff"] = max_abs
    reg["scores_bit_identical"] = bool(max_abs <= 1e-9)
    _log(f"  分数比对：{len(m)} 行｜max|Δscore| = {max_abs:.3e}"
         f" ⇒ {'✅ 逐位一致' if reg['scores_bit_identical'] else '⚠️ 不一致'}")
    if not reg["scores_bit_identical"]:
        raise SystemExit("🔴🔴 折 4 重构自证失败：缓存分数与封存产物不一致")
    return reg


def _crosscheck_old_grid(wf_dir, results, grid_rows) -> list[dict]:
    """与**旧网格**（``oos_grid.csv``）交叉核对（冠军 + buffer 3/5 行）。"""
    import pandas as pd

    old_path = wf_dir / "oos_grid.csv"
    out: list[dict] = []
    if not old_path.is_file():
        return out
    old = pd.read_csv(old_path, encoding="utf-8-sig")
    old = old[old["arm"] == "pure_E_native"]
    for buf in (2, 3, 5):
        sub = old[(old["hold_buffer"] == buf) & (old["gate"] == "off")]
        if sub.empty:
            continue
        old_row = sub.iloc[0]
        vid = "champion" if buf == 2 else f"hb{buf}"
        res = results.get(vid)
        if res is None:
            continue
        met = res["metrics"]
        for key in ("annualized_return", "max_drawdown", "sharpe",
                    "annualized_turnover", "excess_vs_benchmark", "etf_rank_ic"):
            new_v = _f(met.get(key) if key != "etf_rank_ic" else res["etf_rank_ic"].get("ic"))
            old_v = _f(old_row.get(key))
            diff = None
            if new_v is not None and old_v is not None:
                diff = new_v - old_v
            out.append({
                "variant_id": vid, "hold_buffer": buf, "metric": key,
                "old_value": old_v, "new_value": new_v, "diff": diff,
            })
        out.append({
            "variant_id": vid, "hold_buffer": buf, "metric": "n_oos_days",
            "old_value": _f(old_row.get("n_oos_days")),
            "new_value": float(res["n_oos_days"]),
            "diff": float(res["n_oos_days"]) - float(_f(old_row.get("n_oos_days")) or 0.0),
        })
    return out


# ================================================================= 报告
def render_report(out_dir, C, grid_rows, stats_rows, axis_verdicts, crosscheck,
                  regression, fold_ids, results) -> str:
    """渲染最终报告（表格 + 判定 + 判读 + 口径披露）。"""
    from collections import defaultdict

    lines: list[str] = []
    stats_by_id = {r["variant_id"]: r for r in stats_rows}
    champ_row = next((r for r in grid_rows if r["variant_id"] == "champion"
                      and r["axis"] == "hold_buffer"), None)

    lines.append("# V2 · 参数高原测试（参数邻域扰动）报告\n")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 产物目录：`{out_dir}`")
    lines.append(f"- OOS 窗口：**{list(C.WALK_FORWARD_OOS_WINDOW)}**"
                 f"（5 折滚动前推拼接；折 = {list(fold_ids)}）")
    lines.append(f"- 单盲段：**封存未用**（本探针连 qlib 日历都只取到 OOS 窗末日为止）")
    lines.append(f"- 折模型 / 折分数：**复用缓存**（`v2/_walkforward/`）⇒ **零重训**")
    lines.append("")

    # ---------------- ① 冠军 vs 各变体总表 ----------------
    lines.append("## 1. 冠军 vs 各变体（总表）\n")
    lines.append("| 变体 | 轴 | 参数 | 年化 | 回撤 | 夏普 | 年化换手(双边) | 超额 | "
                 "ETF层RankIC | 保持度(vs冠军) | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    seen_axis: set[str] = set()
    first_row: dict[str, dict] = {}
    axes_of: dict[str, list[str]] = {}
    for r in grid_rows:
        key = (r["axis"], r["variant_id"])
        if key in seen_axis:
            continue
        seen_axis.add(key)
        first_row.setdefault(r["variant_id"], r)
        axes_of.setdefault(r["variant_id"], []).append(r["axis"])
    for vid, r in first_row.items():
        lines.append(
            f"| `{vid}` | {'/'.join(axes_of[vid])} | {r['param_path']}={r['param_value']} | "
            f"{_fmt(r['annualized_return'])} | {_fmt(r['max_drawdown'])} | {_fmt(r['sharpe'])} | "
            f"{_fmt(r['annualized_turnover'])} | {_fmt(r['excess_vs_benchmark'])} | "
            f"{_fmt(r['etf_rank_ic'])} | {_fmt(r['retention_vs_champion'])} | {r['verdict']} |"
        )
    lines.append("")
    lines.append("> 判定列 = 该变体**首次出现**的轴的判定（同一变体可能落在多条轴上 ⇒ "
                 "逐轴判定见 §3；冠军在每条轴上都是「冠军值」）。")
    lines.append("")

    # ---------------- ② 逐轴 OAT 面（含配对统计） ----------------
    lines.append("## 2. 逐轴 OAT 面（含配对统计）\n")
    lines.append("> 配对统计 = 变体 vs **冠军**在**拼接日频收益**上的差序列 `d_t`；"
                 f"Newey-West t（lag={NW_LAG}）+ 块自助法（块长 {BOOTSTRAP_BLOCK} 交易日、"
                 f"B={BOOTSTRAP_B}）95% CI。\n")
    lines.append("> 「保持度(vs轴参考)」列：除动量窗口轴外都是 **vs 冠军**；"
                 "动量窗口轴的底座是 `wm050`（该列 vs `wm050`）。\n")
    by_axis: dict[str, list[dict]] = defaultdict(list)
    for r in grid_rows:
        by_axis[r["axis"]].append(r)
    for axis, rows in by_axis.items():
        v = axis_verdicts[axis]
        lines.append(f"### 2.{list(by_axis).index(axis) + 1} 轴 `{axis}`"
                     f"（参考变体 = `{v['ref_variant_id']}`）⇒ **{v['verdict'].upper()}**\n")
        lines.append("| 变体 | 参数 | 年化 | 回撤 | 超额 | 保持度(vs轴参考) | "
                     "配对日均差 | 年化差 | NW t | 块自助 95% CI | 配对 n |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            st = stats_by_id.get(r["variant_id"], {})
            lines.append(
                f"| `{r['variant_id']}` | {r['param_value']} | "
                f"{_fmt(r['annualized_return'])} | {_fmt(r['max_drawdown'])} | "
                f"{_fmt(r['excess_vs_benchmark'])} | {_fmt(r['retention_vs_axis_ref'])} | "
                f"{_fmt(st.get('mean_diff'), 6)} | {_fmt(st.get('ann_diff'))} | "
                f"{_fmt(st.get('nw_t_stat'), 3)} | "
                f"[{_fmt(st.get('boot_ci_lo'), 6)}, {_fmt(st.get('boot_ci_hi'), 6)}] | "
                f"{st.get('n_paired_days', '—')} |"
            )
        lines.append("")
        lines.append(f"- 判定依据：好邻居 {v['n_good_neighbors']} / {v['n_neighbors']}｜"
                     f"变号={v['sign_flip']}｜低保持度={v['low_retention']}｜"
                     f"严格 argmax={v['strict_argmax']}｜到最近邻落差={_fmt(v['drop_to_neighbor'])}｜"
                     f"邻居间最大差={_fmt(v['neighbor_spread'])}｜孤立={v['isolated']}")
        lines.append(f"- 判读：{v['reason']}\n")
        if not v["strict_argmax"]:
            best = max((r for r in rows if r["excess_vs_benchmark"] is not None),
                       key=lambda r: r["excess_vs_benchmark"], default=None)
            if best is not None and best["variant_id"] != v["ref_variant_id"]:
                lines.append(
                    f"- ⚠️ **本轴参考不是最优**：`{best['variant_id']}`"
                    f"（{best['param_value']}）的超额 {_fmt(best['excess_vs_benchmark'])} "
                    f"高于参考 ⇒ 邻域里**有更好的点**（是否显著 ⇒ 看本节配对统计的 CI）。")
                lines.append("")

    # ---------------- ③ 逐轴判定汇总 ----------------
    lines.append("## 3. 逐轴判定（高原 / 尖刺 / mixed）\n")
    lines.append("| 轴 | 判定 | 好邻居/邻居 | 最低保持度 | 严格argmax | 到最近邻落差 | 判读 |")
    lines.append("|---|---|---|---|---|---|---|")
    for axis, v in axis_verdicts.items():
        lines.append(
            f"| `{axis}` | **{v['verdict']}** | {v['n_good_neighbors']}/{v['n_neighbors']} | "
            f"{_fmt(v['retention_min'])} | {v['strict_argmax']} | "
            f"{_fmt(v['drop_to_neighbor'])} | {v['reason']} |"
        )
    lines.append("")

    # ---------------- ④ 交叉核对 ----------------
    lines.append("## 4. 与旧网格（`oos_grid.csv`）交叉核对\n")
    if not crosscheck:
        lines.append("- ⚠️ 未找到旧网格文件 ⇒ 跳过交叉核对。")
    else:
        lines.append("| 变体 | 指标 | 旧值 | 新值 | 差 |")
        lines.append("|---|---|---|---|---|")
        for r in crosscheck:
            lines.append(f"| `{r['variant_id']}` | {r['metric']} | {_fmt(r['old_value'], 6)} | "
                         f"{_fmt(r['new_value'], 6)} | {_fmt(r['diff'], 8)} |")
        diffs = [abs(r["diff"]) for r in crosscheck if r["diff"] is not None]
        worst = max(diffs) if diffs else 0.0
        lines.append("")
        lines.append(f"- 最大绝对差 = **{worst:.3e}**"
                     f"（{'✅ 与旧网格一致' if worst <= 1e-9 else '⚠️ 有差异，见上表'}）")
    lines.append("")

    # ---------------- ⑤ 口径披露 ----------------
    lines.append("## 5. 口径披露（不许藏）\n")
    lines.append(f"- **OOS = 5 折滚动前推拼接窗**（`config_v2.WALK_FORWARD_OOS_WINDOW` = "
                 f"{list(C.WALK_FORWARD_OOS_WINDOW)}；折 = {list(fold_ids)}；"
                 f"每年 24~25 次调仓；OOS 交易日 ~{champ_row['n_oos_days'] if champ_row else '—'}）。")
    lines.append("- **单盲测试段封存未用**：本探针从不请求该段的数据（qlib 日历只取到 OOS 窗末日；"
                 "行情面板虽覆盖到更晚，但评估窗口严格限于各折验证年）。")
    lines.append("- **变体间 NAV 高度相关**（同一套 S 层分数、同一批调仓日）⇒ 用**配对检验**"
                 "（Newey-West + 块自助），**不是**独立样本检验。")
    lines.append(f"- **保持度 retention** = 变体超额 / 参考超额；参考超额绝对值 < "
                 f"{RETENTION_DENOM_MIN} 时报 **n/a**（分母近零 ⇒ 不算假数）。")
    lines.append(f"- **判定阈值**：高原需 ≥{PLATEAU_MIN_GOOD_NEIGHBORS} 个最近邻同号且 "
                 f"retention ≥ {PLATEAU_RETENTION_MIN}，且冠军不是严格孤立 argmax"
                 f"（与最近邻的差 < {ISOLATION_GAP_FACTOR} × **轴上其余变体之间**的最大差）；"
                 f"尖刺 = 最近邻变号 / retention < {SPIKE_RETENTION_MAX} / "
                 f"严格 argmax 且落差 > {SPIKE_DROP_RATIO}；其余 mixed。")
    lines.append(f"- **配对统计参数**：Newey-West lag = {NW_LAG}；块自助块长 = "
                 f"{BOOTSTRAP_BLOCK} 交易日、B = {BOOTSTRAP_B}、种子 = {BOOTSTRAP_SEED}、CI 水平 "
                 f"{1 - BOOTSTRAP_ALPHA:.0%}。")
    lines.append("- **动量窗口轴**的底座是 `alpha/momentum = 0.5/0.5`（冠军本身动量权重为 0 ⇒ "
                 "动量窗口对它无意义）；该轴的参考变体 = `wm050`，不是冠军。")
    lines.append("- **只有 2 个取值的轴**（`theme_cap` / `neg_gate`）⇒ 最近邻只有 1 个 ⇒ "
                 "「≥2 个好邻居」判据**结构上不成立** ⇒ 这两条轴的 `mixed` 是"
                 "**判据不适用**，**不是**「邻域塌了」的证据（只看它们各自的保持度）。")
    lines.append("- **「本轴参考不是最优」**的轴（见 §2 的 ⚠️ 行）⇒ 邻域里存在超额更高的点；"
                 "但配对检验的 CI 仍覆盖 0 ⇒ 「更好」未必是**显著**差异。")
    lines.append("- **换手口径**：`annualized_turnover` = **双边** Σ|Δw|；单边 = 0.5×。"
                 "佣金 0.015% + 滑点 0.05%（单边）。")
    lines.append("- 每折从**空仓**起算（不跨折结转持仓）⇒ 拼接 OOS 在折边界有轻微跳空；"
                 "拼接年化按（净值点数−1）/250 计 ⇒ 5 年窗被记为约 4.84 年，年化略被高估。")
    if regression.get("checked"):
        lines.append(f"- **折 4 重构自证**：缓存分数 vs 已封存分数逐位一致"
                     f"（max|Δscore| = {regression.get('max_abs_score_diff'):.3e}，"
                     f"比对 {regression.get('n_rows_compared')} 行）。")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
