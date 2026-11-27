#!/usr/bin/env python3
"""只读工具：**多重检验折算**（把 V2 冠军的 OOS 夏普按「历史试错次数」折算成真实置信度）。

【为什么需要（本工具的诞生原因）】
用户目标文档（2026-11-26）「快招④」原文：
「在同一段 34 期的数据上反复调参、对比不同分支，必然产生选择偏差。
离线评估时可以用**组合净化交叉验证**或**去偏夏普比率**，
把你历史上一共试过多少组参数当作惩罚项扣掉，
直接算出当前截面超额在统计学上的真实置信度。」
⇒ 本工具 = 该快招的**可复现实现**（铁律九：每条结论必带脚本相对路径 + 可复制完整命令）。

【算的是什么】
Bailey & López de Prado（2014）**去偏夏普比率（DSR）**：
给定 **N** 次试验后，纯运气下「最好一次」能达到的期望夏普 ``SR0``，
以及冠军实测夏普超过 ``SR0`` 的概率 ``PSR(SR0)``。

    SR0 = sqrt(V[SR]) * [(1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e))]
    PSR(SR) = Φ( (SR - SR0)·sqrt(T-1) / sqrt(1 - γ3·SR + ((γ4-1)/4)·SR²) )
（γ = 欧拉常数 ≈ 0.5772；T = 样本交易日数；γ3/γ4 = 偏度/峰度，无实测时取正态值）

【🔴 三条必须披露的口径（不许省）】
1. **N 是下限**：本工具只数**本仓已登记**的试验（v2 根目录各搜索产物 ＋ 滚动前推网格
   ＋ 参数高原扫描本身）⇒ **不含 V1 时代更早的人工搜索**。
2. **试验彼此高度相关** ⇒ 原始 N **高估**独立试验数 ⇒ 本工具另给
   **有效试验数 N_eff**（由变体日收益的**平均成对相关系数**按
   ``N_eff ≈ N / (1 + (N-1)·ρ̄)`` 推出；ρ̄ 高 ⇒ 有效独立试验少 ⇒ 折算**更严**）。
3. 🔴 **循环论证**：冠军臂 + ``hold_buffer=2`` **是在同一段 OOS 交易日上选出来的**
   ⇒ 即便折算后夏普仍显著，也**不能**证明「没有数据挖掘」；
   ⇒ 真正能证伪「拟合噪声」的证据是 ① **参数高原**（邻域稳定）② **长周期跨资产复现**
   （指数宇宙 2015-2023）。本工具只回答「**量级**上是不是运气」。

【只读】
本脚本**只读**：不写、不删任何被检查的产物；只把结论写进 ``--out-dir``（默认不开 ⇒ 只打印）。

【用法】
    python3 manu/tools/v2_overfit_trial_adjustment.py --results-root <结果根>
    python3 manu/tools/v2_overfit_trial_adjustment.py --results-root <结果根> \
        --plateau-dir <参数高原产物根> --out-dir <落点>
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
from statistics import NormalDist

#: 欧拉–马斯刻若尼常数
EULER_GAMMA = 0.5772156649015329
#: 无实测偏度/峰度时取正态（γ3=0, γ4=3）
DEFAULT_SKEW = 0.0
DEFAULT_KURTOSIS = 3.0

#: 宿主上结果根的两种落点（服务器绝对路径 / 本机 SSHFS 只读镜像）
DEFAULT_RESULTS_ROOTS = (
    "/mnt/data/siping/runs/etf_rotation_results",
    "/home/manu/mnt/data/siping/runs/etf_rotation_results",
)


# --------------------------------------------------------------------------- 工具函数
def _pick_results_root(explicit: str) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_dir():
            raise SystemExit(f"🔴 结果根不存在：{p}")
        return p
    for cand in DEFAULT_RESULTS_ROOTS:
        if Path(cand).is_dir():
            return Path(cand)
    raise SystemExit(f"🔴 找不到结果根，试过：{DEFAULT_RESULTS_ROOTS}")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm_cdf(x: float) -> float:
    return NormalDist().cdf(x)


def _norm_inv_cdf(p: float) -> float:
    return NormalDist().inv_cdf(p)


# --------------------------------------------------------------------------- 试验计数
def count_trials(results_root: Path, plateau_dir: Path | None = None) -> dict:
    """数**本仓已登记**的试验次数（逐项可追溯到具体产物文件）。"""
    v2 = results_root / "v2"
    tally: dict[str, int] = {}
    detail: list[dict] = []

    def add(label: str, n, source: str):
        if not n:
            return
        tally[label] = tally.get(label, 0) + int(n)
        detail.append({"label": label, "n": int(n), "source": source})

    # ① val 段 arm 搜索（4 臂 × 60 组合）
    d = _load_json(v2 / "arm_search_val.json")
    if isinstance(d, dict) and isinstance(d.get("per_arm"), dict):
        n = sum((v.get("n_ok_trials") or 0) for v in d["per_arm"].values())
        add("val 段 arm 搜索（4 臂 × 60 组合）", n, "v2/arm_search_val.json:per_arm.n_ok_trials")

    # ② val 段 MA 扫描
    d = _load_json(v2 / "val_ma_sweep.json")
    if isinstance(d, dict) and d.get("n_trials"):
        add("val 段 MA 扫描", d.get("n_trials"), "v2/val_ma_sweep.json:n_trials")

    # ③ 归因探针组（step1 变体 / step2 网格 / step3 闸门）
    for sub in ("_probe_attribution", "_probe_attribution_ma",
                "_probe_attribution_r2", "_probe_attribution_v2"):
        sdir = v2 / sub
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.glob("step*.json")):
            d = _load_json(f)
            if not isinstance(d, dict):
                continue
            n = d.get("n_trials") or d.get("n_variants") or d.get("n_rows")
            if n is None:
                for key in ("rows", "variants", "trials", "results"):
                    if isinstance(d.get(key), list):
                        n = len(d[key])
                        break
            add(f"归因探针 {sub}/{f.name}", n, f"v2/{sub}/{f.name}")

    # ④ S 层消融
    d = _load_json(v2 / "ablation_choice.json")
    if isinstance(d, dict) and isinstance(d.get("candidates"), list):
        add("S 层特征消融", len(d["candidates"]), "v2/ablation_choice.json:candidates")

    # ⑤ 滚动前推网格
    wf = v2 / "_walkforward" / "oos_grid.csv"
    if wf.is_file():
        with wf.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        add("5 折滚动前推网格（arm × buffer × gate）", len(rows), "v2/_walkforward/oos_grid.csv")

    # ⑥ 参数高原扫描本身（每个互异参数集 = 一次试验）
    if plateau_dir is not None:
        grid = plateau_dir / "plateau_grid.csv"
        if grid.is_file():
            seen = set()
            with grid.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    vid = row.get("variant_id")
                    if vid:
                        seen.add(vid)
            add("参数高原 OAT 扫描（互异参数集）", len(seen), "参数高原产物根/plateau_grid.csv")

    return {"total": sum(tally.values()), "tally": tally, "detail": detail}


# --------------------------------------------------------------------------- 试验夏普分布
def load_trial_sharpes(results_root: Path, plateau_dir: Path | None):
    """取试验夏普的**经验分布**（优先参数高原扫描 ⇒ 样本更多、轴更全）。"""
    src = None
    srs: list[float] = []
    if plateau_dir is not None:
        grid = plateau_dir / "plateau_grid.csv"
        if grid.is_file():
            seen = set()
            with grid.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    # 🔴 按 variant_id 去重（同一变体会在每条轴上各出现一次）
                    vid = row.get("variant_id")
                    if vid and vid in seen:
                        continue
                    if vid:
                        seen.add(vid)
                    for key in ("sharpe", "sharpe_ratio"):
                        if row.get(key):
                            try:
                                srs.append(float(row[key]))
                            except ValueError:
                                pass
            src = str(grid)
    if not srs:
        grid = results_root / "v2" / "_walkforward" / "oos_grid.csv"
        if grid.is_file():
            with grid.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row.get("sharpe"):
                        try:
                            srs.append(float(row["sharpe"]))
                        except ValueError:
                            pass
            src = str(grid)
    return srs, src


def load_nav_correlation(plateau_dir: Path | None):
    """由变体日收益求**平均成对相关系数** ρ̄（用于推有效试验数 N_eff）。"""
    if plateau_dir is None:
        return None, None, 0
    nav_dir = plateau_dir / "plateau_nav"
    if not nav_dir.is_dir():
        return None, None, 0
    files = sorted(nav_dir.glob("*.csv"))
    if len(files) < 2:
        return None, None, len(files)

    import numpy as np

    series: dict[str, np.ndarray] = {}
    index = None
    for f in files:
        try:
            with f.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except Exception:
            continue
        if not rows:
            continue
        keys = list(rows[0].keys())
        # 🔴 取「列名」而不是「列的值」：日期列名优先 date/index/日期，否则第一列
        date_key = next((k for k in keys if k.strip().lower() in ("date", "index", "datetime", "日期")), keys[0])
        val_key = next((k for k in keys if k.strip().lower() in ("nav", "nav_value", "value")), keys[-1])
        idx = [r[date_key] for r in rows]
        vals = []
        for r in rows:
            try:
                vals.append(float(r[val_key]))
            except (TypeError, ValueError):
                vals.append(float("nan"))
        if index is None:
            index = idx
        elif idx != index:
            continue
        series[f.stem] = np.asarray(vals, dtype=float)

    if len(series) < 2:
        return None, None, len(series)
    mat = np.vstack(list(series.values()))
    with np.errstate(invalid="ignore", divide="ignore"):
        rets = np.diff(mat, axis=1) / mat[:, :-1]
    # 只保留所有变体都有值的日期
    mask = np.all(np.isfinite(rets), axis=0)
    rets = rets[:, mask]
    if rets.shape[1] < 30:
        return None, None, len(series)
    corr = np.corrcoef(rets)
    k = corr.shape[0]
    off = corr[np.triu_indices(k, 1)]
    rho_bar = float(np.nanmean(off))
    return rho_bar, int(rets.shape[1]), k


# --------------------------------------------------------------------------- DSR 主计算
def deflated_sharpe(sr: float, t_days: int, n_trials: int, var_sr: float,
                    skew: float = DEFAULT_SKEW, kurtosis: float = DEFAULT_KURTOSIS) -> dict:
    sr0 = math.sqrt(var_sr) * (
        (1 - EULER_GAMMA) * _norm_inv_cdf(1 - 1 / n_trials)
        + EULER_GAMMA * _norm_inv_cdf(1 - 1 / (n_trials * math.e))
    )
    denom = 1 - skew * sr + ((kurtosis - 1) / 4) * sr ** 2
    z = (sr - sr0) * math.sqrt(max(t_days - 1, 1)) / math.sqrt(denom)
    return {
        "sr_observed": sr,
        "sr0_expected_max": sr0,
        "psr": _norm_cdf(z),
        "z": z,
        "t_implied": sr * math.sqrt(max(t_days - 1, 1)),
    }


def bonferroni_z(n_trials: int) -> float:
    return _norm_inv_cdf(1 - 0.05 / max(n_trials, 1))


# --------------------------------------------------------------------------- 报告
def render_markdown(payload: dict) -> str:
    t = payload["trials"]
    d = payload["dsr"]
    lines = []
    lines.append("# 多重检验折算（去偏夏普）· V2 冠军 OOS 证据体检")
    lines.append("")
    lines.append("> 只读工具产出：`manu/tools/v2_overfit_trial_adjustment.py`")
    lines.append("> 结果根：" + payload["results_root"])
    if payload.get("plateau_dir"):
        lines.append("> 参数高原产物根：" + payload["plateau_dir"])
    lines.append("")
    lines.append("## 1. 试验次数（已登记口径 ⇒ **下限**）")
    lines.append("")
    lines.append("| 试验组 | 次数 | 证据文件 |")
    lines.append("|---|---:|---|")
    for item in t["detail"]:
        lines.append(f"| {item['label']} | {item['n']} | `{item['source']}` |")
    lines.append(f"| **合计 N** | **{t['total']}** | — |")
    lines.append("")
    lines.append("## 2. 试验夏普的经验分布")
    lines.append("")
    if payload.get("trial_sr_source"):
        lines.append(f"- 取样来源：`{payload['trial_sr_source']}`")
        lines.append(f"- 样本数：**{payload['n_trial_sr']}**｜均值 **{payload['trial_sr_mean']:.4f}**"
                     f"｜样本标准差 **{payload['trial_sr_sd']:.4f}**"
                     f"｜最大 {payload['trial_sr_max']:.4f}｜最小 {payload['trial_sr_min']:.4f}")
        lines.append(f"- ⇒ **V[SR] = {payload['var_sr']:.6f}**（= 试验夏普方差）")
    else:
        lines.append("- 🔴 没找到试验夏普样本 ⇒ 无法算 V[SR]")
    lines.append("")
    lines.append("## 3. 折算结果")
    lines.append("")
    if payload.get("champion_sr") is not None:
        lines.append("| 量 | 值 |")
        lines.append("|---|---:|")
        lines.append(f"| 冠军 OOS 夏普（实测） | **{d['sr_observed']:.4f}** |")
        lines.append(f"| 样本长度 T | {payload['t_days']} 交易日 |")
        lines.append(f"| 零假设下「最好一次」的期望夏普 SR0（N={t['total']}） | {d['sr0_expected_max']:.4f} |")
        lines.append(f"| PSR(SR0) | **{d['psr']:.6f}** |")
        lines.append(f"| 隐含 t 值（SR·√(T−1)） | {d['t_implied']:.2f} |")
        if payload.get("n_eff"):
            lines.append(f"| 有效独立试验数 N_eff | **{payload['n_eff']}**（原始 N={t['total']}，ρ̄={payload['rho_bar']:.4f}） |")
            lines.append(f"| 按 N_eff 折算的 SR0 | {payload['sr0_neff']:.4f} |")
            lines.append(f"| 按 N_eff 折算的 PSR | **{payload['psr_neff']:.6f}** |")
        lines.append(f"| Bonferroni 阈值（α=0.05/N） | 需 z ≳ **{payload['bonferroni_z']:.3f}** |")
    lines.append("")
    lines.append("## 4. 口径披露（🔴 必读）")
    lines.append("")
    for note in payload["caveats"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_payload(results_root: Path, plateau_dir: Path | None,
                  champion_sr: float | None, t_days: int) -> dict:
    trials = count_trials(results_root, plateau_dir)
    srs, src = load_trial_sharpes(results_root, plateau_dir)
    rho_bar, nav_days, n_vars = load_nav_correlation(plateau_dir)

    var_sr = None
    if len(srs) >= 2:
        m = sum(srs) / len(srs)
        var_sr = sum((x - m) ** 2 for x in srs) / (len(srs) - 1)

    n_total = trials["total"]
    payload = {
        "results_root": str(results_root),
        "plateau_dir": str(plateau_dir) if plateau_dir else None,
        "trials": trials,
        "trial_sr_source": src,
        "n_trial_sr": len(srs),
        "trial_sr_mean": (sum(srs) / len(srs)) if srs else None,
        "trial_sr_sd": (math.sqrt(var_sr) if var_sr is not None else None),
        "trial_sr_max": max(srs) if srs else None,
        "trial_sr_min": min(srs) if srs else None,
        "var_sr": var_sr,
        "t_days": t_days,
        "champion_sr": champion_sr,
        "rho_bar": rho_bar,
        "n_nav_variants": n_vars,
        "nav_days": nav_days,
        "bonferroni_z": bonferroni_z(n_total),
        "caveats": [
            "**N 是下限**：只数本仓已登记的搜索产物，不含 V1 时代更早的人工搜索。",
            "**试验共用同一段 OOS 数据 ⇒ 彼此高度相关** ⇒ 原始 N 高估独立试验数；"
            f"本表另给 N_eff（ρ̄={rho_bar:.4f}，变体数 {n_vars}；🔴 ρ̄ 取自**参数高原那批近邻变体**"
            f" ⇒ 是**偏保守**的代理：val 段 240 组 arm 搜索彼此结构差异更大 ⇒ 真实 ρ̄ 应更低 ⇒ N_eff 更高）。"
            if rho_bar is not None else
            "**试验共用同一段 OOS 数据 ⇒ 彼此高度相关** ⇒ 原始 N 高估独立试验数（本轮没拿到变体 NAV ⇒ 未算 N_eff）。",
            "🔴 **循环论证**：冠军臂 + hold_buffer=2 是在同一段 OOS 交易日上选出来的 ⇒ "
            "即便折算后仍显著，也**不能**证明「没有数据挖掘」；能证伪「拟合噪声」的是 "
            "**参数高原**（邻域稳定）与**长周期跨资产复现**（指数宇宙 2015-2023）。",
            "本工具只回答「**量级**上是不是运气」，不回答「是不是拟合了噪声」。",
        ],
    }

    if champion_sr is not None and var_sr:
        payload["dsr"] = deflated_sharpe(champion_sr, t_days, n_total, var_sr)
        if rho_bar is not None and rho_bar > -1 / (n_total - 1):
            n_eff = n_total / (1 + (n_total - 1) * rho_bar)
            payload["n_eff"] = round(n_eff, 1)
            d2 = deflated_sharpe(champion_sr, t_days, max(int(round(n_eff)), 2), var_sr)
            payload["sr0_neff"] = d2["sr0_expected_max"]
            payload["psr_neff"] = d2["psr"]
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="多重检验折算（去偏夏普）")
    ap.add_argument("--results-root", default="", help="结果根（默认自动探测）")
    ap.add_argument("--plateau-dir", default="", help="参数高原产物根（给 N_eff 算 ρ̄）")
    ap.add_argument("--champion-sharpe", type=float, default=None, help="冠军 OOS 夏普（不给则自动读）")
    ap.add_argument("--t-days", type=int, default=0, help="OOS 交易日数（0 = 自动读）")
    ap.add_argument("--out-dir", default="", help="结论落点（不给则只打印）")
    args = ap.parse_args()

    results_root = _pick_results_root(args.results_root)
    plateau_dir = Path(args.plateau_dir) if args.plateau_dir else None

    champion_sr = args.champion_sharpe
    t_days = args.t_days
    if champion_sr is None or not t_days:
        grid = results_root / "v2" / "_walkforward" / "oos_grid.csv"
        if grid.is_file():
            with grid.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            champ = None
            for row in rows:
                if (row.get("arm") == "pure_E_native" and row.get("hold_buffer") == "2"
                        and (row.get("gate") or "").lower() == "off"):
                    champ = row
                    break
            if champ:
                if champion_sr is None and champ.get("sharpe"):
                    champion_sr = float(champ["sharpe"])
                if not t_days and champ.get("n_oos_days"):
                    t_days = int(float(champ["n_oos_days"]))

    payload = build_payload(results_root, plateau_dir, champion_sr, t_days)
    md = render_markdown(payload)
    print(md)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "overfit_trial_adjustment.md").write_text(md, encoding="utf-8")
        (out / "overfit_trial_adjustment.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 已写出：{out/'overfit_trial_adjustment.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
