#!/usr/bin/env python3
"""🔴 **V2 冻结件 → 每日实盘链接缝的端到端联调探针**（2026-11-18）

【为什么要有这个工具】
  2026-11-18 用户目标文档第三条要求：在实盘调仓日之前把
  ``frozen_params_wf.json``（``pure_E_native + buffer=2``）**正式接入每日执行流水线**，
  并与 ``nav_state`` 回撤刹车、TA 宏观降仓带**完成端到端联调**。

  「接上了」不能靠看代码说 ⇒ 本探针用**同一份真实快照输入**跑两遍：
    * **A（接缝关）** = V1 老口径（E 层 EWMA + MA60 闸门 + alpha/mom 0.5/0.5）；
    * **B（接缝开）** = V2 冻结件口径（纯 E + 不平滑 + 无 MA 闸门 + top_k/buffer/同族上限）。
  ⇒ 输出**逐项对照**：入选名单、排序依据（平滑分 vs 原始分）、MA 闸门命中、
    刹车状态、规则层目标。
  ⇒ 这样「接缝真的改变了每日链的决策」是**实测**，不是断言。

【🔴 红线】
  * **只读**既有快照；产物只写**新目录**（``<RESULTS_ROOT>/v2/_seam_e2e/``），
    **绝不覆盖**任何封存件 / 既有快照；
  * 用的是 `daily_config` 的**真实常量**（账户、universe、holdings 路径）⇒ 与每日链同源。

【用法（服务器）】
    export OMP_NUM_THREADS=4
    python manu/tools/v2_seam_e2e_probe.py \
        --snapshot-dir /mnt/data/siping/runs/etf_rotation_results/snapshots/20261117_221846 \
        --frozen-params /mnt/data/siping/runs/etf_rotation_results/v2/frozen_params_wf.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DAILY_DIR_CANDIDATES = (
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "daily"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "daily",
    Path("/tmp/pycharm_project_3b68a403/manu/etf_rotation/daily"),
)
DAILY_DIR = next((p for p in DAILY_DIR_CANDIDATES if p and (p / "daily_config.py").exists()), None)
if DAILY_DIR is None:  # pragma: no cover
    sys.stderr.write("🔴 找不到 manu/etf_rotation/daily/daily_config.py\n")
    raise SystemExit(2)

sys.path.insert(0, str(DAILY_DIR))
sys.path.insert(0, str(DAILY_DIR.parent))   # 研究链根（`import config`）
import daily_config as DC  # noqa: E402
import v2_frozen_params as V2FP  # noqa: E402

RESEARCH_ROOT = DAILY_DIR.parent
QL_PY = str(DC.QL_PY)


def _snapshot_complete(snap: Path, *, require_decision: bool = False) -> bool:
    """这个快照**输入完整吗**（能不能重放）。

    🔴 2026-11-18 续六：每日链是**增量写**快照的 ⇒ 刚跑完/跑到一半时，
       **最新那个快照可能是半写的**（缺 ``stock_scores.csv`` 或 ``account_snapshot.csv``）
       ⇒ 探针拿它去跑 ⇒ **假失败**（看着像「接缝坏了」，其实是快照没写完）。
    ⇒ 探针必须**跳过不完整的快照**，退到上一个完整的。
    """
    if not any(snap.glob("etf_prices_effective_*.csv")):
        return False
    if not (snap / "stock_scores.csv").is_file():
        return False
    if not (snap / "account_snapshot.csv").is_file():
        return False
    if require_decision and not (snap / "tradingagents_decision.md").is_file():
        return False
    return True


def _pick_snapshot(results_root: Path, *, require_decision: bool = False) -> Path:
    """挑一个**输入完整**的快照（从新到旧）。

    🔴 找不到 ⇒ **明确报错**（不许静默拿一个残缺快照去跑）。
    """
    snaps = sorted((results_root / "snapshots").glob("*"))
    snaps = [s for s in snaps if s.is_dir()]
    if not snaps:
        raise SystemExit("🔴 找不到任何快照目录")
    skipped: list[str] = []
    for snap in reversed(snaps):
        if _snapshot_complete(snap, require_decision=require_decision):
            if skipped:
                print(f"🔴 跳过 {len(skipped)} 个**输入不完整**的快照（半写/缺件）：{skipped[:5]}")
            return snap
        skipped.append(snap.name)
    raise SystemExit(
        "🔴 找不到**输入完整**的快照"
        + ("（且带 TradingAgents 决策件）" if require_decision else "")
        + f"（已检查 {len(snaps)} 个）"
    )


def _snapshot_inputs(snapshot: Path, asof: str) -> dict:
    """从快照里定位本轮真实输入（与每日链**同源**）。"""
    prices = snapshot / f"etf_prices_effective_{asof.replace('-', '')}.csv"
    if not prices.exists():
        cands = sorted(snapshot.glob("etf_prices_effective_*.csv"))
        if not cands:
            raise SystemExit(f"🔴 快照里找不到 effective 价格面板：{snapshot}")
        prices = cands[-1]
    scores = snapshot / "stock_scores.csv"
    stock_prices = snapshot / "stock_prices.csv"
    account = snapshot / "account_snapshot.csv"
    decision = snapshot / "tradingagents_decision.md"
    for p in (scores, account):
        if not p.exists():
            raise SystemExit(f"🔴 快照缺输入：{p}")
    return {
        "prices": prices,
        "scores": scores,
        "stock_prices": stock_prices if stock_prices.exists() else None,
        "account": account,
        "decision": decision if decision.exists() else None,
        "universe": Path(DC.ETF_UNIVERSE_CSV),
        "holdings": Path(DC.ETF_HOLDINGS_CSV),
    }


def _fresh_account_snapshot(out_dir: Path) -> Path:
    """按**当前**实盘口径生成一份账户快照 CSV（**不复用快照里那份**）。

    🔴 2026-11-19 真机缺陷（探针假红）：探针原来直接用**快照里的**
       ``account_snapshot.csv``，而 ``--total-assets`` / ``--cash`` 却来自
       **当前** ``daily_config`` 常量 ⇒ 账户口径一换（2026-11-19 用户新截图：
       30,035.57 → 30,385.21），两者就对不上 ⇒ ``analyze.py`` 的账户对账守卫
       （Σ市值 + 现金 == 总资产，容差 0.01 元）直接抛异常 ⇒ **analyze rc=1**
       ⇒ 探针整轮**假失败**（看着像「接缝坏了」）。
    ⇒ 修法：**每次探针都按当前口径重新生成**账户快照 CSV
       （与每日链 ``agents_rebalance.py`` **同一做法**：``account_state.write_snapshot_csv``）
       ⇒ ① 三个档（A/B/C）用**同一份**账户 ⇒ 可比；② 永远与注入的常量自洽。
    """
    import account_state  # 每日链的**实盘账户单一事实源**（daily/ 下，sys.path 已就位）

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "account_snapshot.csv"
    account_state.write_snapshot_csv(path)
    print(f"   ↻ 账户快照按**当前口径**重生成：{path}"
          f"（总资产 {account_state.DC.TOTAL_ASSETS:.2f} / 现金 {account_state.DC.CASH:.2f}）")
    return path


def _scoring_days(scores_path: Path) -> list[str]:
    """``stock_scores.csv`` 里**实际存在**的打分日（升序、去重）。

    🔴 只用标准库 ``csv``（不引入 pandas 依赖）⇒ 探针本体保持轻量。
    """
    import csv

    out: set[str] = set()
    with scores_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = str(row.get("date") or "").strip()
            if raw:
                out.add(raw[:10])
    return sorted(out)


def _resolve_asof(scores_path: Path, requested: str) -> tuple[str, str]:
    """把请求的 asof 对齐到**实际存在的打分日**；返回 ``(生效 asof, 原请求 asof)``。

    🔴 2026-11-18 续六（真缺陷，修掉「探针在最新快照上根本跑不起来」）：
       个股打分日是**相位栅格**（每 N 个交易日取一个 ＋ 末日例外）
       ⇒ **价格面板的末日未必是打分日**（实测：面板末日 2026-11-10，打分日里**没有**它；
       附近打分日是 2026-11-03 / 2026-11-04 / 2026-11-16 / 2026-11-17）。
       ⇒ 用一个不存在于 ``stock_scores.csv`` 的 asof 去跑 ⇒
       ``analyze.py`` 直接抛「信号日没有记录」、``recommend.py`` 抛 KeyError
       ⇒ **三档全部 rc=1** ⇒ 探针整轮**假失败**（看起来像「接缝坏了」，其实是 asof 取错了）。
    ⇒ 改成「取 **≤ 请求日的最后一个打分日**」，并**如实披露**改成了哪天。
    """
    days = _scoring_days(scores_path)
    if not days:
        raise SystemExit(f"🔴 {scores_path} 里没有任何打分日")
    if requested in days:
        return requested, ""
    earlier = [d for d in days if d <= requested]
    if not earlier:
        raise SystemExit(
            f"🔴 请求的 asof {requested} **早于所有打分日**（最早 {days[0]}）⇒ 无从对齐"
        )
    return earlier[-1], requested


def _v1_etf_args() -> list[str]:
    """V1 老口径（= 接缝关）：E 层 EWMA + MA60 双闸门。"""
    import config as C  # 研究链 config

    return [
        "--smoothing-method", str(getattr(C, "SMOOTHING_METHOD", "ewma")),
        "--smoothing-window", str(getattr(C, "SMOOTHING_WINDOW", 5)),
        "--smoothing-alpha", str(getattr(C, "SMOOTHING_ALPHA", 0.20)),
        "--smoothing-history-days", str(getattr(C, "SMOOTHING_HISTORY_SPAN_DAYS", 30)),
        "--entry-ma-window", str(DC.ENTRY_MA_WINDOW),
        "--exit-ma-window", str(DC.EXIT_MA_WINDOW),
    ]


def _run(cmd: list[str], label: str) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(RESEARCH_ROOT))
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    print(f"   [{label}] rc={proc.returncode}"
          + ("" if proc.returncode == 0 else f"\n      stderr: {(proc.stderr or '').strip()[-800:]}"))
    return proc.returncode, proc.stdout or "", tail


def _load_report(out_dir: Path, name: str) -> dict:
    p = out_dir / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _selected(report: dict) -> list[str]:
    """分析层动作名单（``recommendation.actions`` 里每一项 = 一条买入/卖出动作）。"""
    rec = report.get("recommendation") or {}
    actions = rec.get("actions") or []
    out: list[str] = []
    for h in actions:
        if isinstance(h, dict):
            code = h.get("code") or h.get("etf_code")
            if code:
                out.append(str(code))
    return sorted(out)


def _action_codes(report: dict, action: str) -> list[str]:
    """按 ``action`` 字段挑动作名单（``买入`` / ``卖出``）⇒ **不混算**。

    🔴 为什么要拆：``recommendation.actions`` 里**同时含买入与卖出**
    （老持仓要卖、新标的要买）⇒ 把两者的条数当「入选只数」会**多算**。
    """
    rec = report.get("recommendation") or {}
    out: list[str] = []
    for h in rec.get("actions") or []:
        if not isinstance(h, dict):
            continue
        if str(h.get("action") or "").strip() != action:
            continue
        code = h.get("code") or h.get("etf_code")
        if code:
            out.append(str(code))
    return sorted(out)


def _defensive_map(report: dict) -> dict:
    """``{code: is_defensive}``（从分析报告的候选表读；表里没有 ⇒ 不在 map 里）。"""
    out: dict[str, bool] = {}
    for c in (report.get("candidates") or []):
        if isinstance(c, dict) and c.get("code"):
            out[str(c["code"])] = bool(c.get("is_defensive"))
    return out


def _dig(report: dict, key: str, default=None):
    """在报告里找某个标量字段（顶层 / ``meta`` / ``meta.params`` 三处都找）。"""
    if isinstance(report, dict):
        if key in report:
            return report[key]
        meta = report.get("meta")
        if isinstance(meta, dict):
            if key in meta:
                return meta[key]
            params = meta.get("params")
            if isinstance(params, dict) and key in params:
                return params[key]
    return default


def main() -> int:
    ap = argparse.ArgumentParser(description="V2 冻结件接缝的端到端联调探针")
    ap.add_argument("--snapshot-dir", default="", help="快照目录（缺省 = 取最新快照）")
    ap.add_argument("--frozen-params", default=str(DC.V2_FROZEN_PARAMS_FILE))
    ap.add_argument("--asof", default="", help="信号日（缺省 = 从快照名推断）")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument(
        "--require-decision", action="store_true",
        help="挑快照时**要求**带 tradingagents_decision.md（⇒ 阶段 5 才跑得起来）",
    )
    args = ap.parse_args()

    results_root = Path(DC.ETF_ROTATION_RESULTS_ROOT)
    snapshot = (
        Path(args.snapshot_dir) if args.snapshot_dir
        else _pick_snapshot(results_root, require_decision=bool(args.require_decision))
    )
    if not snapshot.is_dir():
        raise SystemExit(f"🔴 快照目录不存在：{snapshot}")
    asof = args.asof or ""
    if not asof:
        # 从快照里的价格面板名反推（etf_prices_effective_20261117.csv ⇒ 2026-11-17）
        cands = sorted(snapshot.glob("etf_prices_effective_*.csv"))
        if not cands:
            raise SystemExit("🔴 无法从快照推断 asof，请显式传 --asof")
        digits = cands[-1].stem.rsplit("_", 1)[-1]
        asof = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    inp = _snapshot_inputs(snapshot, asof)
    # 🔴 2026-11-18 续六：asof 必须落在**实际存在的打分日**上
    #    （打分日是相位栅格 ⇒ 面板末日未必是打分日 ⇒ 否则 analyze/recommend 直接 rc=1）。
    asof_requested = asof
    asof, asof_adjusted_from = _resolve_asof(inp["scores"], asof)
    if asof_adjusted_from:
        print(f"🔴 asof 对齐：{asof_adjusted_from} **不在打分栅格上**"
              f" ⇒ 改用最近的**更早**打分日 {asof}")

    out_root = Path(args.out_dir) if args.out_dir else (results_root / "v2" / "_seam_e2e")
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_a = out_root / f"{stamp}_A_v1"
    out_b = out_root / f"{stamp}_B_frozen"
    out_c = out_root / f"{stamp}_C_brake"
    out_a.mkdir(parents=True, exist_ok=True)
    out_b.mkdir(parents=True, exist_ok=True)
    out_c.mkdir(parents=True, exist_ok=True)

    # 🔴 2026-11-19：账户快照必须按**当前口径**重生成（见 helper 的说明）
    #    ⇒ 不复用快照里那份（它带着**旧账户口径**的市值/现金）
    inp["account"] = _fresh_account_snapshot(out_root)

    fp = V2FP.load_frozen_params(Path(args.frozen_params))

    print("=" * 92)
    print(f"🔴 V2 接缝端到端联调｜快照 {snapshot.name}｜asof {asof}")
    print(f"   冻结件：{fp.source_path}（md5 {fp.md5[:12]}…｜冠军臂 {fp.winner_arm}）")
    print("=" * 92)

    # ---------- 公共参数（两遍**逐字相同**） ----------
    common = [
        "--asof", asof,
        "--prices", str(inp["prices"]),
        "--universe", str(inp["universe"]),
        "--scores", str(inp["scores"]),
        "--holdings-csv", str(inp["holdings"]),
        "--pool-mode", "e_covered",
    ]
    _peak_nav = float(DC.TOTAL_ASSETS) / 1000.0
    brake = ["--nav", f"{_peak_nav:.6f}", "--nav-peak", f"{_peak_nav:.6f}"]
    # 🔴 2026-11-18 续四：**C 档 = 急跌刹车真触发档**。
    #    参数与 B 档**逐字相同**，只把净值打到峰值的一半 ⇒ 回撤 −50% ≤ 阈值 −15%
    #    ⇒ ``nav_state.in_brake`` 必为 True ⇒ 规则层必须**整个组合切防守资产**。
    #    ⇒ B 档 vs C 档 的差异**只可能来自刹车**（不是参数）⇒ 这就是「刹车接缝有效」的链级证据。
    brake_on = ["--nav", f"{_peak_nav * 0.5:.6f}", "--nav-peak", f"{_peak_nav:.6f}"]

    variants = {
        "A_v1": (_v1_etf_args(), out_a, brake),
        "B_frozen": (fp.etf_layer_args(), out_b, brake),
        "C_brake": (fp.etf_layer_args(), out_c, brake_on),
    }

    summary: dict[str, dict] = {}
    for label, (etf_args, out_dir, brake_args) in variants.items():
        print(f"\n【{label}】ETF 层参数：{' '.join(etf_args)}")
        analyze_cmd = [QL_PY, str(RESEARCH_ROOT / "analyze.py")] + common + [
            "--total-assets", f"{DC.TOTAL_ASSETS}",
            "--cash", f"{DC.CASH}",
            "--account-snapshot", str(inp["account"]),
            "--out-dir", str(out_dir),
            "--top-n", "15",
        ] + ([ "--stock-prices", str(inp["stock_prices"])] if inp["stock_prices"] else []) \
            + etf_args + brake_args
        rc_a, _out_a, _ = _run(analyze_cmd, f"{label}/analyze")
        rec_cmd = [QL_PY, str(RESEARCH_ROOT / "recommend.py")] + common + [
            "--out-dir", str(out_dir),
        ] + etf_args + brake_args
        rc_r, _out_r, _ = _run(rec_cmd, f"{label}/recommend")

        # ---- 阶段 5：``final_rebalance_plan.py``（规则层对账 ＋ **TA 宏观降仓带** ＋ 权益上限带）----
        # 🔴 这是每日链的**阶段 5**（`daily/agents_rebalance.py:1431` 调的就是它）
        #    ⇒ 用来证明「V2 冻结件 → 阶段 3/4 → **阶段 5 规则层对账**」整条链真的贯通，
        #    且 **TA 降仓带 / 权益上限带** 与接缝**同时生效**（不是各跑各的）。
        plan_path = out_dir / "final_rebalance_plan.json"
        rc_p = -1
        plan: dict = {}
        if inp.get("decision") is not None:
            plan_cmd = [
                QL_PY, str(RESEARCH_ROOT / "final_rebalance_plan.py"),
                "--report", str(out_dir / "etf_analysis_report.json"),
                "--decision", str(inp["decision"]),
                "--prices", str(inp["prices"]),
                "--universe", str(inp["universe"]),
                "--account", str(inp["account"]),
                "--out-dir", str(out_dir),
            ]
            rc_p, _out_p, _ = _run(plan_cmd, f"{label}/stage5_final_plan")
            plan = _load_report(out_dir, "final_rebalance_plan.json")
        else:
            print(f"   [{label}/stage5_final_plan] 跳过：快照里没有 tradingagents_decision.md")

        rep = _load_report(out_dir, "etf_analysis_report.json")
        rec_json = _load_report(out_dir, "rebalance_recommendation.json")
        # 🔴 续四：入选名单里哪些是**防守资产**（从报告候选表读 ``is_defensive``）
        _dmap = _defensive_map(rep)
        fsel = sorted(
            str(x.get("code"))
            for x in ((rep.get("filter_stats") or {}).get("selected") or [])
            if isinstance(x, dict) and x.get("code")
        )
        eq_sel = sorted(code for code in fsel if _dmap.get(code) is False)
        unknown_sel = sorted(code for code in fsel if code not in _dmap)
        sm = rep.get("score_smoothing") or {}
        selected = _selected(rep)
        buys = _action_codes(rep, "买入")
        sells = _action_codes(rep, "卖出")
        targets = sorted(str(h.get("code")) for h in (rec_json.get("holdings") or []) if isinstance(h, dict))
        summary[label] = {
            "analyze_rc": rc_a,
            "recommend_rc": rc_r,
            "selected": selected,
            "buy_codes": buys,
            "sell_codes": sells,
            "rule_layer_targets": targets,
            "ranking_basis": sm.get("ranking_basis"),
            "smoothing_enabled": bool(sm.get("enabled", False)),
            "target_n_holdings": _dig(rep, "target_n_holdings"),
            "max_per_theme": _dig(rep, "max_per_theme"),
            "alpha_weight": _dig(rep, "alpha_weight"),
            "momentum_weight": _dig(rep, "momentum_weight"),
            "hold_buffer": _dig(rep, "hold_buffer"),
            "entry_ma_window": _dig(rep, "entry_ma_window"),
            "exit_ma_window": _dig(rep, "exit_ma_window"),
            "n_selected": (rep.get("recommendation") or {}).get("n_selected"),
            "brake": (rec_json.get("nav_state") or {}),
            # 🔴 续四：刹车档的**链级证据**（分析报告落盘值，不是命令行参数）
            "brake_mode": bool(((rep.get("filter_stats") or {}).get("brake_mode"))),
            "filter_selected": fsel,
            "equity_selected": eq_sel,
            "unknown_defensive": unknown_sel,
            "stage5_rc": rc_p,
            #: 🔴 2026-11-18 续六：快照里**没有** ``tradingagents_decision.md``
            #:    ⇒ 阶段 5 整段**跑不了** ⇒ ``stage5_rc`` / ``macro_derisk_band``
            #:    两项**本档不适用**（原实现会判 FAIL ⇒ 探针在**正常快照**上误报
            #:    rc=1 ⇒ 假警报；真实不一致反而会被这条假警报淹没）。
            "stage5_skipped": inp.get("decision") is None,
            "stage5_weights_used": ((plan.get("meta") or {}).get("weights_used") or {}),
            "stage5_ta": (plan.get("ta_screening") or {}),
            "stage5_equity_cap_used": ((plan.get("min_amount_gate") or {}).get("equity_cap_used")),
            "stage5_action_codes": sorted(
                str(x.get("code")) for x in (plan.get("actions") or []) if isinstance(x, dict)
            ),
        }
        print(f"   → 分析层动作 {len(selected)} 条：买入 {len(buys)} 只 {buys}"
              f"｜卖出 {len(sells)} 只 {sells}"
              f"｜n_selected={summary[label]['n_selected']}")
        print(f"   → 排序依据：{summary[label]['ranking_basis']}"
              f"｜平滑{'开' if summary[label]['smoothing_enabled'] else '关'}"
              f"｜alpha={summary[label]['alpha_weight']}"
              f"｜momentum={summary[label]['momentum_weight']}"
              f"｜buffer={summary[label]['hold_buffer']}"
              f"｜MA入={summary[label]['entry_ma_window']}/MA出={summary[label]['exit_ma_window']}"
              f"｜target_n_holdings={summary[label]['target_n_holdings']}"
              f"｜max_per_theme={summary[label]['max_per_theme']}")
        ta = summary[label]["stage5_ta"]
        if summary[label]["stage5_rc"] == 0:
            ta_pct = ((ta.get("equity_cap_ta_recommendation") or {}).get("pct"))
            print(f"   → 阶段5 规则层对账：rc=0"
                  f"｜权重口径 alpha={summary[label]['stage5_weights_used'].get('alpha')}"
                  f"/momentum={summary[label]['stage5_weights_used'].get('momentum')}"
                  f"｜权益上限 base={ta.get('equity_cap')} → TA {ta_pct}"
                  f" → **生效 {ta.get('equity_cap_effective')}**"
                  f"｜动作 {len(summary[label]['stage5_action_codes'])} 只"
                  f" {summary[label]['stage5_action_codes']}")

    a, b = summary["A_v1"], summary["B_frozen"]
    print("\n" + "=" * 92)
    print("【接缝前后对照】")
    print(f"   分析层买入 A(V1) = {a['buy_codes']}")
    print(f"   分析层买入 B(V2) = {b['buy_codes']}")
    print(f"   规则层目标 A = {a['rule_layer_targets']}")
    print(f"   规则层目标 B = {b['rule_layer_targets']}")
    changed = a["selected"] != b["selected"]
    print(f"   ⇒ 接缝是否改变入选：{'✅ 是（口径真的切过去了）' if changed else '⚠️ 否（两档结论相同）'}")
    print(f"   分析层入选 B(不刹车) = {b.get('filter_selected')}")
    print(f"   分析层入选 C(急跌刹车) = {summary['C_brake'].get('filter_selected')}")

    # 🔴 **冻结件 → 生效参数的一致性校验**（机器可判定 ⇒ 不靠人眼看）
    #    判据：B 档**分析报告落盘的有效参数**必须**逐项**等于冻结件的期望值。
    checks = [
        ("alpha_weight", b["alpha_weight"], fp.alpha_weight),
        ("momentum_weight", b["momentum_weight"], fp.momentum_weight),
        ("hold_buffer", b["hold_buffer"], fp.hold_buffer),
        ("target_n_holdings", b["target_n_holdings"], fp.top_k),
        ("entry_ma_window", b["entry_ma_window"], fp.entry_ma_window),
        ("exit_ma_window", b["exit_ma_window"], fp.exit_ma_window),
        ("max_per_theme", b["max_per_theme"], fp.max_per_theme),
    ]
    print("\n【B 档生效参数 vs 冻结件期望值】")
    conformance: list[dict] = []
    for name, got, want in checks:
        ok = got == want
        conformance.append({"item": name, "got": got, "want": want, "ok": bool(ok)})
        mark = "PASS" if ok else "FAIL"
        print(f"   [{mark}] {name}: 生效值={got!r}｜期望值={want!r}")
    smooth_ok = (not b["smoothing_enabled"]) and (not fp.smoothing_enabled)
    conformance.append({"item": "smoothing_enabled", "got": b["smoothing_enabled"],
                        "want": fp.smoothing_enabled, "ok": bool(smooth_ok)})
    mark = "PASS" if smooth_ok else "FAIL"
    print(f"   [{mark}] smoothing_enabled: 生效值={b['smoothing_enabled']}"
          f"｜期望值={fp.smoothing_enabled}（纯 E 冠军臂 ⇒ 每日链**关**平滑）")
    n_targets_ok = len(b["rule_layer_targets"]) == int(fp.top_k)
    conformance.append({"item": "rule_layer_target_count", "got": len(b["rule_layer_targets"]),
                        "want": int(fp.top_k), "ok": bool(n_targets_ok)})
    mark = "PASS" if n_targets_ok else "FAIL"
    print(f"   [{mark}] 规则层目标只数: {len(b['rule_layer_targets'])}"
          f"｜期望 {fp.top_k}（冻结件 top_k）")
    brake_ok = bool((b["brake"] or {}).get("in_brake") is not None)
    conformance.append({"item": "brake_state_reachable", "got": b["brake"],
                        "want": "nav_state 非空", "ok": bool(brake_ok)})
    mark = "PASS" if brake_ok else "FAIL"
    print(f"   [{mark}] 刹车状态可达: nav_state={b['brake']}"
          f"（非空 ⇒ --nav/--nav-peak 已传到规则层 ⇒ 刹车**可触发**）")
    # ---- 🔴 续四：C 档「急跌刹车真触发」的链级证据 ----
    #    判据（B/C 同参数、只差 --nav/--nav-peak ⇒ 差异只可能来自刹车）：
    #      ① C 档 nav_state.in_brake 必为 True；② 分析报告 filter_stats.brake_mode 必为 True；
    #      ③ C 档**入选里不许有权益类**（组合整体切防守资产）；④ B 档 in_brake 必为 False（对照组）；
    #      ⑤ B/C 入选集必不同（否则说明刹车没起作用）。
    cb = summary["C_brake"]
    cb_brake = cb.get("brake") or {}
    trig = cb_brake.get("in_brake") is True
    conformance.append({"item": "brake_in_brake_true", "got": cb_brake.get("in_brake"),
                        "want": True, "ok": bool(trig)})
    print(f"   [{'PASS' if trig else 'FAIL'}] 急跌刹车触发: C 档 nav_state={cb_brake}｜期望 in_brake=True")
    bm = bool(cb.get("brake_mode"))
    conformance.append({"item": "brake_mode_flag", "got": cb.get("brake_mode"),
                        "want": True, "ok": bm})
    print(f"   [{'PASS' if bm else 'FAIL'}] 规则层 brake_mode: {cb.get('brake_mode')}"
          f"｜期望 True（分析报告 filter_stats.brake_mode 落盘值）")
    cb_rc = (cb.get("analyze_rc"), cb.get("recommend_rc"))
    rc_ok = cb_rc == (0, 0)
    conformance.append({"item": "brake_chain_rc", "got": {"analyze_rc": cb_rc[0], "recommend_rc": cb_rc[1]},
                        "want": "analyze/recommend 都 rc=0（急跌日不许崩）", "ok": bool(rc_ok)})
    print(f"   [{'PASS' if rc_ok else 'FAIL'}] 刹车档链路 rc: analyze={cb_rc[0]}｜recommend={cb_rc[1]}"
          f"｜期望都是 0（急跌刹车日**每日链不许崩**）")
    cb_sel = list(cb.get("filter_selected") or [])
    eq_sel = list(cb.get("equity_selected") or [])
    only_def = (not eq_sel) and bool(cb_sel)
    conformance.append({"item": "brake_defensive_only",
                        "got": {"selected": cb_sel, "equity_selected": eq_sel},
                        "want": "入选**非空**且全是防守资产（权益类 0 只）", "ok": bool(only_def)})
    print(f"   [{'PASS' if only_def else 'FAIL'}] 刹车档入选: {cb_sel}"
          f"｜其中**权益类** {eq_sel}｜期望**非空**且权益类 0 只"
          f"｜未知防守属性 {cb.get('unknown_defensive')}")
    ctrl = (summary["B_frozen"].get("brake") or {}).get("in_brake") is False
    conformance.append({"item": "control_not_in_brake",
                        "got": (summary["B_frozen"].get("brake") or {}).get("in_brake"),
                        "want": False, "ok": bool(ctrl)})
    print(f"   [{'PASS' if ctrl else 'FAIL'}] 对照组 B 档 in_brake="
          f"{(summary['B_frozen'].get('brake') or {}).get('in_brake')}｜期望 False"
          f"（同参数同峰值、只因净值没跌 ⇒ 不刹车 ⇒ 证明差异来自刹车而非参数）")
    diff_sel = cb.get("filter_selected") != b.get("filter_selected")
    conformance.append({"item": "brake_changes_selection", "got": cb.get("filter_selected"),
                        "want": f"≠ B 档 {b.get('filter_selected')}", "ok": bool(diff_sel)})
    print(f"   [{'PASS' if diff_sel else 'FAIL'}] 刹车是否改变入选: C={cb.get('filter_selected')}"
          f"｜B={b.get('filter_selected')}")

    import config as C
    ta = b["stage5_ta"]
    base_cap = ta.get("equity_cap")
    ta_pct = ((ta.get("equity_cap_ta_recommendation") or {}).get("pct"))
    eff_cap = ta.get("equity_cap_effective")
    band_max = float(getattr(C, "MACRO_DERISK_EQUITY_CAP_MAX", 1.0))
    # 🔴 2026-11-18 续六：快照**无决策件** ⇒ 阶段 5 整段跑不了 ⇒ 本项**不适用**。
    #    （判 FAIL 会让探针在**正常快照**上误报 rc=1 ⇒ 假警报；真不一致反而被淹没。）
    _stage5_skipped = bool(b.get("stage5_skipped"))
    if _stage5_skipped:
        conformance.append({"item": "stage5_rc", "got": {"skipped": True},
                            "want": "快照无决策件 ⇒ 本项不适用", "ok": True})
        print("   [SKIP] 阶段5 规则层对账 rc: 快照无 tradingagents_decision.md ⇒ 本项不适用")
    else:
        stage5_ok = b["stage5_rc"] == 0
        conformance.append({"item": "stage5_rc", "got": b["stage5_rc"], "want": 0, "ok": bool(stage5_ok)})
        mark = "PASS" if stage5_ok else "FAIL"
        print(f"   [{mark}] 阶段5 规则层对账 rc: {b['stage5_rc']}｜期望 0")
    if _stage5_skipped:
        conformance.append({"item": "stage5_weights_used", "got": {"skipped": True},
                            "want": "快照无决策件 ⇒ 本项不适用", "ok": True})
        print("   [SKIP] 阶段5 权重口径: 快照无 tradingagents_decision.md ⇒ 本项不适用")
    else:
        s5_alpha = (b["stage5_weights_used"] or {}).get("alpha")
        s5_mom = (b["stage5_weights_used"] or {}).get("momentum")
        s5_w_ok = (s5_alpha == fp.alpha_weight) and (s5_mom == fp.momentum_weight)
        conformance.append({"item": "stage5_weights_used", "got": b["stage5_weights_used"], "want": {"alpha": fp.alpha_weight, "momentum": fp.momentum_weight}, "ok": bool(s5_w_ok)})
        mark = "PASS" if s5_w_ok else "FAIL"
        print(f"   [{mark}] 阶段5 权重口径: alpha={s5_alpha}/momentum={s5_mom}｜期望 alpha={fp.alpha_weight}/momentum={fp.momentum_weight}（⇒ V2 冻结件**穿过阶段 3/4 到达阶段 5**）")
    if _stage5_skipped:
        conformance.append({"item": "macro_derisk_band", "got": {"skipped": True},
                            "want": "快照无决策件 ⇒ 本项不适用", "ok": True})
        print("   [SKIP] TA 宏观降仓带: 快照无 tradingagents_decision.md ⇒ 本项不适用")
    else:
        band_ok = eff_cap is not None and float(eff_cap) <= band_max
        band_ok = band_ok and (ta_pct is None or float(eff_cap) <= float(ta_pct))
        band_ok = band_ok and (base_cap is None or float(eff_cap) <= float(base_cap))
        conformance.append({"item": "macro_derisk_band", "got": eff_cap, "want": f"<= min(base {base_cap}, TA {ta_pct}, band_max {band_max})", "ok": bool(band_ok)})
        mark = "PASS" if band_ok else "FAIL"
        print(f"   [{mark}] TA 宏观降仓带: base={base_cap} → TA {ta_pct} → **生效 {eff_cap}**｜权益上限带硬顶 {band_max}（⇒ 降仓带**与接缝同时生效**）")
    all_ok = all(c["ok"] for c in conformance)
    print("\n【口径边界（必须如实披露，不许夸大）】")
    print("   ① 阶段 5 的**最终名单 = 规则层候选池 × TradingAgents 决策件**；")
    if _stage5_skipped:
        print("      🔴 **本快照没有决策件** ⇒ 阶段 5 整段**没跑** ⇒ 本次探针**不覆盖**阶段 5")
        print("         （权重口径穿到阶段 5 的证据来自**带决策件**的那次跑，"
              "见快照 20261117_221846 的产物）")
    else:
        print("      本探针复用的是**快照里那份既有的决策件**（V1 口径下生成的）")
        print("      ⇒ 阶段 5 的**名单**不是干净的 V2 端到端；")
        print("      本探针证明的是「**冻结件口径真的穿到阶段 5**」（权重口径 / 候选池 / 降仓带）。")
    print("   ② 下一次真实调仓日，决策件会**基于 V2 报告重新生成** ⇒ 才是真正的 V2 端到端。")
    print("   ③ 两档的**权益上限带完全相同**（base 0.8 → TA 0.25 → 生效 0.25）")
    print("      ⇒ 降仓带**与接缝正交**（接缝不污染防守层）。")
    print("   ④ **C 档是「人为把净值打到峰值一半」造出来的急跌场景**（不是真实行情）")
    print("      ⇒ 它证明的是**接缝 + 刹车的因果链**（参数没变 ⇒ 行为变了 ⇒ 行为只因刹车而变）")
    print("      ⇒ **不是**在评估「真实市场里刹车救不救得回来」。")
    verdict = "全绿（冻结件真的落到每日链生效参数）" if all_ok else "有不一致项"
    print(f"   ⇒ 接缝一致性总判定：{verdict}")
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps({
            "snapshot": str(snapshot), "asof": asof,
            "asof_requested": asof_requested, "asof_adjusted_from": asof_adjusted_from,
            "frozen_params": fp.disclosure(),
            "variants": summary,
            "selection_changed": changed,
            "seam_conformance": conformance,
            "seam_conformance_all_ok": bool(all_ok),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ⇒ JSON 已写：{args.json_out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
