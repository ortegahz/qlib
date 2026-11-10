#!/usr/bin/env python
"""节点日「账户口径双基准」闸门（M-S5）。

【为什么需要这个探针】
M-S5 给 `reprice_node_day.py` 加了 `--total-assets-basis {plan|node}`：
* `plan` = 计划件口径（总资产取**信号日**账户快照）；
* `node` = 节点日口径（总资产 = 现金 + Σ(持仓股数 × 节点日收盘价)）。
M-S4 的审计教训：**闸门自己必须是摆设的检验器** ⇒ 光有「跑通了」不算数，
必须做到 ① 语义断言逐条**独立重算**（不许直接读产物字段当答案）、
② 关键恒等式用**变异测试**反证（把实现改坏 ⇒ 断言必须 FAIL）。

【断言清单】
P1  正例跑通（默认口径 / 显式 plan / 显式 node 三跑）
P2  三份产物 JSON 可解析
P3  meta 必备字段齐全（basis / source / plan_total / node_total / basis_note）
P4  `node_total_assets` 恒等式：独立重算 cash + Σ(股数 × 节点日价)
P5  生效总资产自洽（plan 生效值 == 计划件 total_assets；node 生效值 == effective）
P6  逐条 `shares_effective` == floor(权重 × 生效总资产 / 价格 / 整手) × 整手
P7  逐条跨档区间**独立重算**（lo/hi 公式 + 价格来源）且 window_lo ≤ 生效总资产 < window_hi
P8  逐条 `shares_plan_basis` / `shares_node_basis` 独立重算一致
P9  `n_shares_differ_plan_vs_node` 与逐条比对一致
P10 默认口径 ≡ 显式 `--total-assets-basis plan`（逐字节，剔除时间戳）
P11 风险披露必须含「总资产口径」条目且非空
P12 run-root **零新增**、既有文件 sha1 **全未变**（不许污染交付件）
D1  真负例：非法 basis 值 ⇒ 必须报错退出
D2  真负例：节点日未被行情覆盖 ⇒ 必须硬拦
D3  真负例（变异）：`_node_total_assets` 少算持仓 ⇒ P4 必须报错
P13 「第一个跨档阈值」**独立重算**（方向语义 + 取最近阈值）
P15 **计划外持仓必须计入** node 口径总资产（BLOCKING 回归防线：账户里有、计划件里没有的标的）
P14 **合成漂移场景**：人为把某只持仓的节点日价改掉 ⇒ 漂移 ≠ 0 ⇒
    方向规则必须生效（漂移向下 ⇒ 阈值只可能落在下边界 lo 上）
D4  真负例（变异）：跨档区间公式被改坏 ⇒ P7 必须报错
D5  真负例（变异）：跨档区间 **lo** 公式被改坏（少算一手）⇒ P7 必须报错
D6  真负例（变异）：第一个跨档阈值**排序被改坏**（升序→降序）⇒ P13 必须报错
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account as A  # noqa: E402
import config as C  # noqa: E402
from final_rebalance_plan import LOT, load_prices, _round_lot  # noqa: E402

TMP_ROOT = C.WORK_ROOT / "_probe_node_basis_tmp"
TOOL = ROOT / "reprice_node_day.py"


class Checker:
    """断言收集器：逐条记录 (id, 名称, 是否通过, 细节)。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((check_id, name, bool(ok), detail))

    def failed(self) -> list[tuple[str, str, bool, str]]:
        return [it for it in self.items if not it[2]]


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict:
    """run-root 的「文件名 -> sha1」快照（用于证明工具没动既有文件）。"""
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.iterdir()):
        if p.is_file():
            out[p.name] = _sha1(p)
    return out


def _covered_node_date(prices_path: Path):
    """行情覆盖到的最后一个交易日（作为正例节点日）。"""
    import pandas as pd

    df = pd.read_csv(prices_path, usecols=["date"])
    mx = pd.to_datetime(df["date"], errors="coerce").max()
    if pd.isna(mx):
        return None
    return mx.strftime("%Y-%m-%d")


def _run(python, node_date, plan_path, prices_path, out_dir, extra=None):
    """跑一次重算工具（**绝不**写 run-root，out_dir 必须在 TMP_ROOT 下）。"""
    cmd = [python, str(TOOL), "--node-date", node_date, "--plan", str(plan_path),
           "--prices", str(prices_path), "--out-dir", str(out_dir)]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900)


def _load(out_dir: Path, node_date: str) -> dict:
    """读重算产物 JSON（文件名里的日期不带横杠）。"""
    stem = "node_reprice_" + node_date.replace("-", "")
    path = out_dir / (stem + ".json")
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_tripwire(rows: list[dict], plan_total: float, node_total: float,
                        eff: float):
    """**独立重算**「第一个跨档阈值」⇒ (|漂移|, code, side, 漂移) 或 None。

    方向语义（与实现一致，但**独立重写**，不许读产物字段当答案）：
    * 漂移 < 0（总资产在跌）⇒ 先跨的是**下边界 `lo`**；
    * 漂移 > 0 ⇒ 先跨的是**上边界 `hi`**；
    * 漂移 ≈ 0（两口径相等）⇒ 两侧都算，取 |漂移| 最小者。
    """
    drift = node_total - plan_total
    pct = (drift / plan_total * 100) if plan_total else None
    if pct is not None and pct < -1e-9:
        allowed = ("drift_to_window_lo_pct",)
    elif pct is not None and pct > 1e-9:
        allowed = ("drift_to_window_hi_pct",)
    else:
        allowed = ("drift_to_window_lo_pct", "drift_to_window_hi_pct")
    cands = []
    for r in rows or []:
        for key in allowed:
            value = r.get(key)
            if value is None:
                continue
            if abs(float(value)) > 0:
                cands.append((abs(float(value)), str(r.get("code")), key, float(value)))
    cands.sort()
    return cands[0] if cands else None


def _strip_ts(payload: dict) -> str:
    """剔除时间戳后的产物 JSON（用于逐字节比较两次运行）。"""
    clone = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    (clone.get("meta") or {}).pop("generated_at", None)
    return json.dumps(clone, ensure_ascii=False, sort_keys=True)


def run_checks(run_root: Path):
    """跑全部断言，返回 (id, 名称, 是否通过, 细节) 列表。"""
    checker = Checker()
    python = sys.executable
    plan_path = run_root / "final_rebalance_plan.json"
    prices_path = Path(str(C.ETF_PRICES_CSV))
    src = TOOL.read_text(encoding="utf-8")

    if not plan_path.exists() or not prices_path.exists():
        checker.add("P1", "前置：计划件与行情存在", False, "输入缺失")
        return checker.items

    node_date = _covered_node_date(prices_path)
    if node_date is None:
        checker.add("P1", "前置：读得到行情覆盖末日", False, "行情日期列解析失败")
        return checker.items

    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    for sub in ("default", "plan", "node"):
        (TMP_ROOT / sub).mkdir(parents=True, exist_ok=True)

    before = _snapshot(run_root)

    r_def = _run(python, node_date, plan_path, prices_path, TMP_ROOT / "default")
    r_plan = _run(python, node_date, plan_path, prices_path, TMP_ROOT / "plan",
                  ["--total-assets-basis", "plan"])
    r_node = _run(python, node_date, plan_path, prices_path, TMP_ROOT / "node",
                  ["--total-assets-basis", "node"])
    ok_def = r_def.returncode == 0
    ok_plan = r_plan.returncode == 0
    ok_node = r_node.returncode == 0
    checker.add("P1", "正例跑通（默认 / 显式 plan / 显式 node 三跑）",
                ok_def and ok_plan and ok_node,
                "节点日=" + node_date + "｜退出码 default=" + str(r_def.returncode)
                + " plan=" + str(r_plan.returncode) + " node=" + str(r_node.returncode)
                + ("" if ok_node else "｜node stderr 尾 " + (r_node.stderr or "")[-200:]))
    if not (ok_def and ok_plan and ok_node):
        return checker.items

    p_def = _load(TMP_ROOT / "default", node_date)
    p_plan = _load(TMP_ROOT / "plan", node_date)
    p_node = _load(TMP_ROOT / "node", node_date)
    checker.add("P2", "三份产物 JSON 可解析",
                bool(p_def) and bool(p_plan) and bool(p_node),
                "default=" + str(len(p_def.get("actions", [])))
                + " plan=" + str(len(p_plan.get("actions", [])))
                + " node=" + str(len(p_node.get("actions", []))) + " 条 action")
    if not (p_def and p_plan and p_node):
        return checker.items

    need_meta = ("total_assets_basis", "total_assets_source", "plan_total_assets",
                 "node_total_assets", "basis_note")
    miss = {}
    for tag, payload in (("default", p_def), ("plan", p_plan), ("node", p_node)):
        bad = [k for k in need_meta if k not in (payload.get("meta") or {})]
        if bad:
            miss[tag] = bad
    checker.add("P3", "meta 必备字段齐全（basis/source/plan_total/node_total/basis_note）",
                not miss, ("缺：" + str(miss)) if miss else "三份产物字段齐全")

    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_price_by_code = {}
    for _a in plan_doc.get("actions") or []:
        plan_price_by_code[str(_a.get("code") or "").zfill(6)] = float(_a.get("price") or 0.0)
    plan_meta = plan_doc.get("meta") or {}
    plan_cash = float(plan_meta.get("cash") or 0.0)
    plan_total = float(plan_meta.get("total_assets") or 0.0)
    prices = load_prices(str(prices_path), node_date)

    expect_node = plan_cash
    for act in plan_doc.get("actions") or []:
        code = str(act.get("code") or "").zfill(6)
        shares = int(act.get("current_shares") or 0)
        if shares <= 0:
            continue
        px = prices.get(code)
        if not px:
            continue
        expect_node += shares * float(px[0])
    got_node = float((p_node.get("meta") or {}).get("node_total_assets") or 0.0)
    diff_node = abs(got_node - expect_node)
    checker.add("P4", "node_total_assets 恒等式：独立重算 cash + Σ(股数 × 节点日价)",
                diff_node < 0.01,
                "独立重算=" + ("%.6f" % expect_node) + "｜产物=" + ("%.6f" % got_node)
                + "｜差=" + ("%.6f" % diff_node))

    meta_plan = p_plan.get("meta") or {}
    meta_node = p_node.get("meta") or {}
    bs_plan = p_plan.get("basis_summary") or {}
    bs_node = p_node.get("basis_summary") or {}
    ok_p5 = (abs(float(meta_plan.get("total_assets") or 0.0) - plan_total) < 0.01
             and abs(float(bs_plan.get("effective_total_assets") or 0.0) - plan_total) < 0.01
             and abs(float(meta_node.get("total_assets") or 0.0)
                     - float(bs_node.get("effective_total_assets") or 0.0)) < 0.01)
    checker.add("P5", "生效总资产自洽（plan 生效值 == 计划件 total_assets；node 生效值 == effective）",
                ok_p5,
                "plan total_assets=" + str(meta_plan.get("total_assets"))
                + " effective=" + str(bs_plan.get("effective_total_assets"))
                + "｜node total_assets=" + str(meta_node.get("total_assets"))
                + " effective=" + str(bs_node.get("effective_total_assets")))

    rows = p_node.get("basis_comparison") or []
    eff = float(bs_node.get("effective_total_assets") or 0.0)
    node_total = float(bs_node.get("node_total_assets") or 0.0)
    bad_p6 = []
    bad_p7 = []
    bad_p8 = []
    for r in rows:
        code = r.get("code")
        w = float(r.get("target_weight") or 0.0)
        px = float(r.get("price") or 0.0)
        if w <= 0 or px <= 0:
            continue
        want_eff = _round_lot(w * eff / px)
        if int(r.get("shares_effective") or 0) != want_eff:
            bad_p6.append(str(code) + ": 生效股数 " + str(r.get("shares_effective"))
                          + " ≠ 重算 " + str(want_eff))
        want_plan = _round_lot(w * plan_total / px)
        want_node = _round_lot(w * node_total / px)
        if int(r.get("shares_plan_basis") or 0) != want_plan:
            bad_p8.append(str(code) + ": plan 股数 " + str(r.get("shares_plan_basis"))
                          + " ≠ 重算 " + str(want_plan))
        if int(r.get("shares_node_basis") or 0) != want_node:
            bad_p8.append(str(code) + ": node 股数 " + str(r.get("shares_node_basis"))
                          + " ≠ 重算 " + str(want_node))
        # 🔴 独立核对价格：basis_comparison 的 price 应是**节点日收盘价**
        #    （节点日无行情才回退计划件价）⇒ 不许直接信产物里的 price。
        code_s = str(code)
        exp_px = (float(prices[code_s][0]) if code_s in prices
                  else plan_price_by_code.get(code_s, 0.0))
        if exp_px and abs(px - exp_px) > 1e-6:
            bad_p7.append(code_s + ": 价格与独立取价不符｜产物 " + ("%.6f" % px)
                          + " vs 独立取价 " + ("%.6f" % exp_px))
        if int(r.get("shares_effective") or 0) > 0:
            lo = float(r.get("total_assets_window_lo") or 0.0)
            hi = float(r.get("total_assets_window_hi") or 0.0)
            # 🔴 独立重算区间公式：lo = shares_eff × 价格 / 权重；
            #    hi = (shares_eff + 整手) × 价格 / 权重
            #    （旧实现只拿产物的 lo/hi 做夹逼 ⇒ 公式本身被改坏也查不出来）
            sh_eff = int(r.get("shares_effective") or 0)
            lo_exp = sh_eff * px / w
            hi_exp = (sh_eff + LOT) * px / w
            if abs(lo - lo_exp) > 1e-6 or abs(hi - hi_exp) > 1e-6:
                bad_p7.append(code_s + ": 区间公式不符｜产物 lo=" + ("%.4f" % lo)
                              + " hi=" + ("%.4f" % hi)
                              + "｜独立重算 lo=" + ("%.4f" % lo_exp)
                              + " hi=" + ("%.4f" % hi_exp))
            elif not (lo <= eff < hi):
                bad_p7.append(code_s + ": 生效总资产 " + ("%.2f" % eff)
                              + " 不在区间 [" + ("%.2f" % lo) + ", " + ("%.2f" % hi) + ")")
    checker.add("P6", "逐条 shares_effective == floor(权重×生效总资产/价格/整手)×整手",
                not bad_p6, ("违例 " + str(len(bad_p6)) + "：" + str(bad_p6[:3]))
                if bad_p6 else (str(len(rows)) + " 条全对"))
    checker.add("P7", "逐条跨档区间**独立重算**（lo/hi 公式 + 价格来源）且 window_lo ≤ 生效总资产 < window_hi",
                not bad_p7, ("违例 " + str(len(bad_p7)) + "：" + str(bad_p7[:3]))
                if bad_p7 else "区间公式与价格全部独立重算一致，且落在区间内")
    checker.add("P8", "逐条 shares_plan_basis / shares_node_basis 独立重算一致",
                not bad_p8, ("违例 " + str(len(bad_p8)) + "：" + str(bad_p8[:3]))
                if bad_p8 else "两口径股数全对")

    n_diff = sum(1 for r in rows if r.get("shares_differ_plan_vs_node"))
    _declared_raw = bs_node.get("n_shares_differ_plan_vs_node")
    # 🔴 不能写 `int(x or -1)`：合法的 0 是**假值** ⇒ 会被误判成 -1（探针自身的 bug）。
    declared = -1 if _declared_raw is None else int(_declared_raw)
    checker.add("P9", "n_shares_differ_plan_vs_node 与逐条比对一致",
                declared == n_diff, "声明 " + str(declared) + "｜实算 " + str(n_diff))

    same = _strip_ts(p_def) == _strip_ts(p_plan)
    checker.add("P10", "默认口径 ≡ 显式 --total-assets-basis plan（逐字节，剔除时间戳）",
                same, "两次产物逐字节一致" if same else "两次产物不一致")

    risks = p_node.get("risk_disclosure") or []
    hit = [r for r in risks if "总资产口径" in r]
    checker.add("P11", "风险披露必须含「总资产口径」条目且非空",
                bool(risks) and bool(hit),
                str(len(risks)) + " 条披露，其中口径条目 " + str(len(hit)) + " 条")

    after = _snapshot(run_root)
    added = sorted(set(after) - set(before))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    checker.add("P12", "run-root 零新增、既有文件 sha1 全未变（不许污染交付件）",
                not added and not changed,
                ("新增=" + str(added) + "｜被改=" + str(changed))
                if (added or changed) else "零新增零改动")

    # ---- P13：「第一个跨档阈值」独立重算 ----
    exp_tw = _expected_tripwire(rows, plan_total, node_total, eff)
    got_code = bs_node.get("first_tripwire_code")
    got_side = bs_node.get("first_tripwire_side")
    got_drift = bs_node.get("first_tripwire_drift_pct")
    bad_p13 = []
    if exp_tw is None:
        if got_code is not None:
            bad_p13.append("无阈值行但产物报了 " + str(got_code))
    else:
        if got_code != exp_tw[1]:
            bad_p13.append("阈值代码=" + str(got_code) + " ≠ 独立重算 " + str(exp_tw[1]))
        if got_side != exp_tw[2]:
            bad_p13.append("阈值侧=" + str(got_side) + " ≠ 独立重算 " + str(exp_tw[2]))
        if got_drift is None or abs(float(got_drift) - exp_tw[3]) > 1e-6:
            bad_p13.append("阈值漂移=" + str(got_drift) + " ≠ 独立重算 " + str(exp_tw[3]))
    checker.add("P13", "「第一个跨档阈值」独立重算（方向语义 + 取最近阈值）",
                not bad_p13, ("违例：" + "；".join(bad_p13)) if bad_p13
                else ("code=" + str(got_code) + "｜side=" + str(got_side)
                      + "｜drift=" + str(got_drift)
                      + "｜方向=" + str(bs_node.get("tripwire_direction"))))

    # ---- P14：合成漂移场景（漂移 ≠ 0 ⇒ 方向规则必须生效）----
    synth_ok = False
    synth_detail = ""
    try:
        import pandas as pd
        df = pd.read_csv(prices_path, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        #: 🔴 2026-11-10（实测红灯）：**变异目标不许写死**。
        #:    旧值 `159929` 已于 2026-11-05 清仓 ⇒ 不在本轮计划里 ⇒ 改它的价格
        #:    不改变 node 口径总资产 ⇒ 漂移仍是 +0.2051%（向上）⇒ 方向判定 up、
        #:    股数没变 ⇒ 场景「没 bites」⇒ P14 假红。
        #: ⇒ 改法：**从本轮计划派生** —— 取 `plan["actions"]` 里 `current_shares > 0`
        #:   的持仓（本轮 = 512800 / 7700 股）⇒ 改它的节点日收盘价 ×0.90
        #:   ⇒ node 口径总资产下降 ⇒ 漂移向下 ⇒ 阈值必落在 `drift_to_window_lo_pct`。
        #: 计划里**没有**持仓 ⇒ 明确报错（**不许**静默跳过）。
        held = [str(a.get("code") or "").zfill(6)
                for a in (plan_doc.get("actions") or [])
                if int(a.get("current_shares") or 0) > 0]
        if not held:
            raise ValueError("计划件里没有 current_shares>0 的持仓 ⇒ 无法构造合成漂移场景")
        synth_code = ""
        for _code in held:
            _hit = int(((df["code"] == _code)
                         & (df["date"].astype(str) == node_date)).sum())
            if _hit > 0:
                synth_code = _code
                break
        if not synth_code:
            raise ValueError("计划件的持仓 " + ",".join(held) + " 在节点日 " + node_date
                             + " 都没有行情行 ⇒ 无法构造合成漂移场景")
        mask = (df["code"] == synth_code) & (df["date"].astype(str) == node_date)
        if int(mask.sum()) == 0:
            synth_detail = "合成场景未命中行：" + synth_code + " @ " + node_date
        else:
            df.loc[mask, "close"] = df.loc[mask, "close"] * 0.90
            synth_prices = TMP_ROOT / "synth_prices.csv"
            df.to_csv(synth_prices, index=False)
            synth_dir = TMP_ROOT / "synth"
            synth_dir.mkdir(parents=True, exist_ok=True)
            r_syn = _run(python, node_date, plan_path, synth_prices, synth_dir,
                         ["--total-assets-basis", "node"])
            p_syn = _load(synth_dir, node_date)
            bs_syn = p_syn.get("basis_summary") or {}
            rows_syn = p_syn.get("basis_comparison") or []
            eff_syn = float(bs_syn.get("effective_total_assets") or 0.0)
            node_syn = float(bs_syn.get("node_total_assets") or 0.0)
            drift_syn = node_syn - plan_total
            pct_syn = (drift_syn / plan_total * 100) if plan_total else None
            exp_syn = _expected_tripwire(rows_syn, plan_total, node_syn, eff_syn)
            problems = []
            if r_syn.returncode != 0:
                problems.append("合成场景退出码=" + str(r_syn.returncode))
            if pct_syn is None or pct_syn >= 0:
                problems.append("合成场景漂移未向下：" + str(pct_syn))
            if bs_syn.get("tripwire_direction") != "down":
                problems.append("方向判定=" + str(bs_syn.get("tripwire_direction")) + "（应 down）")
            if bs_syn.get("first_tripwire_side") != "drift_to_window_lo_pct":
                problems.append("阈值侧=" + str(bs_syn.get("first_tripwire_side"))
                                + "（应 drift_to_window_lo_pct）")
            if exp_syn is not None:
                if bs_syn.get("first_tripwire_code") != exp_syn[1]:
                    problems.append("阈值代码=" + str(bs_syn.get("first_tripwire_code"))
                                    + " ≠ 独立重算 " + str(exp_syn[1]))
                gd = bs_syn.get("first_tripwire_drift_pct")
                if gd is None or abs(float(gd) - exp_syn[3]) > 1e-6:
                    problems.append("阈值漂移=" + str(gd) + " ≠ 独立重算 " + str(exp_syn[3]))
            if int(bs_syn.get("n_shares_differ_plan_vs_node") or 0) < 1:
                problems.append("合成场景没改任何股数（场景没 bites）")
            synth_ok = not problems
            synth_detail = (
                ("漂移=" + ("%.4f%%" % pct_syn) if pct_syn is not None else "漂移=?")
                + "｜方向=" + str(bs_syn.get("tripwire_direction"))
                + "｜阈值=" + str(bs_syn.get("first_tripwire_code"))
                + "/" + str(bs_syn.get("first_tripwire_side"))
                + "｜改单条数=" + str(bs_syn.get("n_shares_differ_plan_vs_node"))
                + "｜变异代码=" + str(synth_code)
                + ("｜" + "；".join(problems) if problems else ""))
    except Exception as exc:  # noqa: BLE001
        synth_ok = False
        synth_detail = "合成场景异常：" + repr(exc)
    checker.add("P14", "合成漂移场景：漂移≠0 ⇒ 阈值必须落在**漂移方向**的边界上",
                synth_ok, synth_detail)

    # ---- P15：计划外持仓必须计入 node 口径总资产（M-S5 审计 BLOCKING 的回归防线）----
    p15_ok = False
    p15_detail = ""
    try:
        acct_src = Path(str(A.ACCOUNT_SNAPSHOT_CSV))
        if not acct_src.exists():
            p15_detail = "账户快照缺失：" + str(acct_src)
        else:
            import pandas as pd
            adf = pd.read_csv(acct_src, dtype={"code": str})
            adf["code"] = adf["code"].astype(str).str.zfill(6)
            extra_code = "510300"
            if extra_code in set(adf["code"]):
                p15_detail = "计划外测试代码与账户快照冲突：" + extra_code
            else:
                extra_row = {c: "" for c in adf.columns}
                extra_row["code"] = extra_code
                extra_row["name"] = "计划外测试标的"
                extra_row["shares"] = 1000
                extra_row["price"] = 1.0
                extra_row["market_value"] = 1000.0
                extra_row["pnl_pct"] = 0.0
                extra_row["cost"] = 1000.0
                extra_row["pnl"] = 0.0
                adf = pd.concat([adf, pd.DataFrame([extra_row])], ignore_index=True)
                acct_csv = TMP_ROOT / "account_offplan.csv"
                adf.to_csv(acct_csv, index=False)
                acct_dir = TMP_ROOT / "acct"
                acct_dir.mkdir(parents=True, exist_ok=True)
                r_acct = _run(python, node_date, plan_path, prices_path, acct_dir,
                              ["--total-assets-basis", "node", "--account", str(acct_csv)])
                p_acct = _load(acct_dir, node_date)
                m_acct = p_acct.get("meta") or {}
                b_acct = p_acct.get("basis_summary") or {}
                got_acct = float(m_acct.get("node_total_assets") or 0.0)
                # 独立重算：现金 + Σ(快照**全部**持仓 × 节点日价)
                exp_acct = plan_cash
                for _, row in adf.iterrows():
                    code_s = str(row.get("code") or "").zfill(6)
                    sh = int(float(row.get("shares") or 0) or 0)
                    if sh <= 0:
                        continue
                    if code_s in prices:
                        exp_acct += sh * float(prices[code_s][0])
                    else:
                        exp_acct += sh * float(row.get("price") or 0.0)
                problems = []
                if r_acct.returncode != 0:
                    problems.append("退出码=" + str(r_acct.returncode))
                if abs(got_acct - exp_acct) >= 0.01:
                    problems.append("node_total_assets=" + ("%.2f" % got_acct)
                                    + " ≠ 独立重算 " + ("%.2f" % exp_acct))
                off_codes = [str(h.get("code")) for h in (b_acct.get("off_plan_holdings") or [])]
                if extra_code not in off_codes:
                    problems.append("计划外持仓未被登记：off_plan_holdings=" + str(off_codes))
                if int(b_acct.get("n_off_plan_holdings") or 0) < 1:
                    problems.append("n_off_plan_holdings=" + str(b_acct.get("n_off_plan_holdings")))
                if extra_code not in [str(h.get("code")) for h in (m_acct.get("node_holdings") or [])]:
                    problems.append("node_holdings 里没有计划外持仓")
                p15_ok = not problems
                p15_detail = ("node_total_assets=" + ("%.2f" % got_acct)
                              + "｜独立重算=" + ("%.2f" % exp_acct)
                              + "｜计划外=" + str(off_codes)
                              + ("｜" + "；".join(problems) if problems else ""))
    except Exception as exc:  # noqa: BLE001
        p15_ok = False
        p15_detail = "计划外场景异常：" + repr(exc)
    checker.add("P15", "计划外持仓必须计入 node 口径总资产（BLOCKING 回归防线）",
                p15_ok, p15_detail)

    r_d1 = _run(python, node_date, plan_path, prices_path, TMP_ROOT / "default",
                ["--total-assets-basis", "不存在的口径"])
    out_d1 = (r_d1.stdout or "") + (r_d1.stderr or "")
    checker.add("D1", "真负例：非法 basis 值 ⇒ 必须报错退出",
                r_d1.returncode != 0 and "basis" in out_d1,
                "退出码=" + str(r_d1.returncode) + "（期望 ≠0）")

    r_d2 = _run(python, "2099-12-31", plan_path, prices_path, TMP_ROOT / "default")
    out_d2 = (r_d2.stdout or "") + (r_d2.stderr or "")
    checker.add("D2", "真负例：节点日未被行情覆盖 ⇒ 必须硬拦",
                r_d2.returncode != 0 and "覆盖不到节点日" in out_d2,
                "退出码=" + str(r_d2.returncode) + "（期望 ≠0）")

    muts = [
        ("D3", "少算持仓（total += market_value → total += 0.0）",
         "        total += market_value\n", "        total += 0.0\n", "_node_total_assets"),
        ("D4", "跨档区间公式被改坏（hi 减 5000）",
         "        hi = (shares_eff + LOT) * price / weight\n",
         "        hi = (shares_eff + LOT) * price / weight - 5000.0\n", "_basis_comparison"),
        # 🔴 D5：M-S5 独立审计发现 —— 旧 P7 只做「夹逼」⇒ lo 公式被改坏（少算一手）
        #    这类**更现实**的公式错误抓不到 ⇒ 必须有一条专门的变异反证。
        ("D5", "跨档区间 **lo** 公式被改坏（少算一手）",
         "        lo = shares_eff * price / weight\n",
         "        lo = (shares_eff - LOT) * price / weight\n", "_basis_comparison"),
        # 🔴 D6：M-S5 独立审计发现 —— 「第一个跨档阈值」零断言覆盖
        #    ⇒ 排序被改坏（升序→降序）也照样 16/16 全绿 ⇒ 必须反证。
        ("D6", "第一个跨档阈值**排序被改坏**（升序 → 降序）",
         "    tripwires.sort()\n",
         "    tripwires.sort(reverse=True)\n", "_basis_summary"),
    ]
    for check_id, desc, old, new, func_name in muts:
        mut_src = src.replace(old, new)
        if mut_src == src:
            checker.add(check_id, "真负例（变异）：" + desc + " ⇒ 对应断言必须报错",
                        False, "变异没生效：源码里找不到待改的行（=> 探针该跟着升级）")
            continue
        caught = False
        detail = ""
        try:
            ns = {"__name__": "_mutant", "__file__": str(TOOL)}
            exec(compile(mut_src, str(TOOL), "exec"), ns)  # noqa: S102
            if func_name == "_node_total_assets":
                shares_map = {}
                for a in (plan_doc.get("actions") or []):
                    shares_map[str(a.get("code") or "").zfill(6)] = int(a.get("current_shares") or 0)
                m_total, _m_hold = ns["_node_total_assets"](
                    plan_doc, prices, node_date, shares_map, plan_cash)
                caught = abs(float(m_total) - expect_node) >= 0.01
                detail = ("变异后 node_total_assets=" + ("%.6f" % m_total)
                          + "｜独立重算=" + ("%.6f" % expect_node))
            elif func_name == "_basis_comparison":
                m_rows = ns["_basis_comparison"](
                    p_node.get("actions") or [], plan_total, node_total, eff)
                bad = []
                for r in m_rows:
                    w2 = float(r.get("target_weight") or 0.0)
                    px2 = float(r.get("price") or 0.0)
                    sh2 = int(r.get("shares_effective") or 0)
                    if w2 <= 0 or px2 <= 0 or sh2 <= 0:
                        continue
                    lo2 = float(r.get("total_assets_window_lo") or 0.0)
                    hi2 = float(r.get("total_assets_window_hi") or 0.0)
                    lo_exp2 = sh2 * px2 / w2
                    hi_exp2 = (sh2 + LOT) * px2 / w2
                    if abs(lo2 - lo_exp2) > 1e-6 or abs(hi2 - hi_exp2) > 1e-6:
                        bad.append(str(r.get("code")) + "(公式不符)")
                    elif not (lo2 <= eff < hi2):
                        bad.append(str(r.get("code")) + "(越界)")
                caught = bool(bad)
                detail = "变异后违例 " + str(len(bad)) + " 条：" + str(bad[:3])
            else:
                m_sum = ns["_basis_summary"](
                    p_node.get("basis_comparison") or [], plan_total, node_total, eff,
                    "node", "src", (p_node.get("meta") or {}).get("node_holdings"))
                exp_tw_m = _expected_tripwire(
                    p_node.get("basis_comparison") or [], plan_total, node_total, eff)
                got_m = (m_sum.get("first_tripwire_code"), m_sum.get("first_tripwire_side"),
                         m_sum.get("first_tripwire_drift_pct"))
                if exp_tw_m is None:
                    caught = got_m[0] is None
                    detail = "无阈值行；变异后=" + str(got_m[0])
                else:
                    caught = not (
                        got_m[0] == exp_tw_m[1] and got_m[1] == exp_tw_m[2]
                        and got_m[2] is not None
                        and abs(float(got_m[2]) - exp_tw_m[3]) <= 1e-6)
                    detail = ("变异后阈值=" + str(got_m[0]) + "/" + str(got_m[1])
                              + "｜独立重算=" + str(exp_tw_m[1]) + "/" + str(exp_tw_m[2]))
        except Exception as exc:  # noqa: BLE001
            caught = False
            detail = "变异体执行异常：" + repr(exc)
        checker.add(check_id, "真负例（变异）：" + desc + " ⇒ 对应断言必须报错",
                    caught, detail)

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return checker.items


def main() -> int:
    """命令行入口：``--run-root`` 指向产物根。"""
    import argparse

    ap = argparse.ArgumentParser(description="节点日账户口径双基准闸门（M-S5）")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()

    items = run_checks(Path(args.run_root))
    failed = [it for it in items if not it[2]]
    for check_id, name, ok, detail in items:
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", check_id, name)
              + (("｜" + detail) if detail else ""))
    print("合计 %d 项：PASS %d / FAIL %d" % (len(items), len(items) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
