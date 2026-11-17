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
P6  逐席位口径自洽（**按席位类型分别用不同不变量**，见下）：
    交易席位（delta≠0）= 权重正推股数；维持席位 = 持仓反推权重；门槛拦下席位 = 门槛政策
P7  逐条跨档区间**独立重算**（lo/hi 公式 + 价格来源 + 漂移百分比）；
    「生效总资产 ∈ 区间」的夹逼**只对交易席位**成立（其余席位的股数不是由权重正推的）
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
D7  真负例（产物级变异）：「维持席位」被篡改成**交易席位**（目标权重与股数不自洽）⇒ P6/P7 必须报错
D8  真负例（产物级变异）：「门槛拦下行」的 `weight_to_cash` 被改成 0（而非**有符号**差额）⇒ P6 必须报错

【为什么「维持席位」与「门槛拦下席位」不能用「按权重反推股数」的不变量】
2026-11-13 夜：「方案一 · 最小调仓门槛」上线（`min_trade_gate.py`，阶段5/阶段6 共用）
⇒ 席位出现**三种语义**，「`shares_effective` == floor(权重 × 生效总资产 / 价格 / 整手) × 整手」
**只对交易席位成立**（实测：11-13 交付件 515450 / 512800 两条行被该口径误判 ⇒ 闸门假红）：
* **交易席位**（`delta_shares != 0`）：目标股数 = 由「目标权重 × 生效总资产」**正推** ⇒ 不变量成立。
* **维持席位**（TA 一票否决 ⇒ 保持现仓；`ta_rejected` / `held_not_touched`）：
  目标权重是**由持仓反推**出来的（`current_shares × 账户快照价 ÷ 计划件总资产`），
  目标股数 = 当前股数（**不做整手重算**）⇒ 「股数 == floor(权重×总资产/价格/整手)×整手」
  **本来就不成立**（权重用快照价、股数用节点日价，两边时点不同）⇒ 只能**反方向**校验：
  `target_weight_effective == 股数 × 账户快照价 ÷ 计划件总资产`。
* **门槛拦下席位**（`min_amount_skipped == true`）：**行保留**、动作改「持有 / 不建仓」、
  `delta_shares = 0`、`est_amount = 0`、`target_weight_effective` 按**当前市值**重算、
  差额记进 `weight_to_cash` ⇒ 股数是**政策作废**出来的（0 股），不是按权重算出来的
  ⇒ 按**门槛政策**校验，并**与产物里的 `min_amount_gate.skipped` 披露对账**。

【有符号 `weight_to_cash` 的含义】
`weight_to_cash` = **目标权重 − 实际落地权重** 的**有符号**差额：
正数 = 该席位的权重缺口归现金；**负数 = 超配但没减仓**（现金相应比目标少）
⇒ **不许截断成非负**（截断 ⇒ Σ`weight_to_cash` ≠ 席位缺口
⇒ 「Σ有效权重 + Σ权重归现金 = 1」恒等式崩 ⇒ 计划件自检 FATAL）。
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


#: 权重 / 区间的浮点比较容差（沿用探针既有口径：区间公式 1e-6）
TOL = 1e-6
#: 金额比较容差（元）
AMT_TOL = 0.01


def _seat_kind(action: dict, scaled: bool = False) -> str:
    """判定席位类型（**三种语义各不相同** ⇒ 不许一律套用「按权重反推股数」）。"""
    if scaled:
        return "现金被缩"
    if action.get("min_amount_skipped") is True:
        return "门槛拦下"
    if action.get("held_not_touched") or action.get("ta_rejected"):
        return "维持"
    if int(action.get("delta_shares") or 0) != 0:
        return "交易"
    return "零动作"


def _hold_account_price(plan_row: dict | None) -> float | None:
    """维持席位的**账户快照价**（独立来源：计划件行的 `account_price`）。

    ⚠ **不是**节点日价：维持席位的权重在计划件里就算好了，节点日重算**原样沿用**
    （维持 ⇒ 不动仓、不重算整手）⇒ 反向校验必须用**权重自身的口径**。
    """
    if not plan_row:
        return None
    price = plan_row.get("account_price")
    if price is not None:
        return float(price)
    mv = plan_row.get("current_market_value")
    cur = int(plan_row.get("current_shares") or 0)
    if mv is not None and cur > 0:
        return float(mv) / cur
    return None


def _hold_weight_expected(plan_row: dict | None, plan_total: float) -> float | None:
    """「维持席位」的**持仓反推权重**（独立重算，不读产物字段）。

    维持席位 ⇒ 目标权重 = **当前权重** = `current_shares × 账户快照价 ÷ 计划件总资产`。
    """
    if not plan_row:
        return None
    cur = int(plan_row.get("current_shares") or 0)
    px = _hold_account_price(plan_row)
    if cur <= 0 or px is None or not plan_total:
        return None
    return cur * px / float(plan_total)


def _ctx(payload: dict, eff: float) -> dict:
    """产物 → 逐条校验所需的索引（行 / action / 门槛披露 / 现金被缩名单 / 席位类型）。"""
    actions = {str(a.get("code") or ""): a for a in (payload.get("actions") or [])}
    gate = {str(g.get("code") or ""): g
            for g in ((payload.get("min_amount_gate") or {}).get("skipped") or [])}
    scaled = {str(s.get("code") or "")
              for s in ((payload.get("cash_flow") or {}).get("scaled_down") or [])}
    rows = payload.get("basis_comparison") or []
    kinds = {}
    for r in rows:
        code = str(r.get("code") or "")
        kinds[code] = _seat_kind(actions.get(code) or {}, code in scaled)
    return {"rows": rows, "actions": actions, "gate": gate, "scaled": scaled,
            "kinds": kinds, "eff": float(eff)}


def _clone_ctx(ctx: dict) -> dict:
    """深拷贝一个 ctx（负例 D7/D8 用：改副本，不碰真产物）。"""
    return {
        "rows": [dict(r) for r in ctx["rows"]],
        "actions": {k: dict(v) for k, v in ctx["actions"].items()},
        "gate": {k: dict(v) for k, v in ctx["gate"].items()},
        "scaled": set(ctx["scaled"]),
        "kinds": dict(ctx["kinds"]),
        "eff": ctx["eff"],
    }


def _p6_violations(ctx: dict, plan_rows: dict, plan_total: float, tag: str) -> list[str]:
    """P6：逐席位校验「股数 ⇄ 权重」自洽（**按席位类型分别用不同不变量**）。

    * 交易席位：`shares_effective == floor(权重 × 生效总资产 / 价格 / 整手) × 整手`（正向）
    * 维持席位：`target_weight_effective == 股数 × 账户快照价 ÷ 计划件总资产`（**反向**）
    * 门槛拦下席位：门槛政策（delta=0 / est_amount=0 / 动作 / 落地权重 / 有符号归现金）
      + **与 `min_amount_gate.skipped` 披露对账**
    """
    out: list[str] = []
    eff = ctx["eff"]
    for r in ctx["rows"]:
        code = str(r.get("code") or "")
        a = ctx["actions"].get(code)
        if a is None:
            out.append(code + tag + ": basis_comparison 行在 actions 里找不到 ⇒ 口径对不上")
            continue
        w = float(r.get("target_weight") or 0.0)
        px = float(r.get("price") or 0.0)
        if w <= 0 or px <= 0:
            continue
        sh_eff = int(r.get("shares_effective") or 0)
        cur = int(a.get("current_shares") or 0)
        kind = ctx["kinds"].get(code, "零动作")
        delta = int(a.get("delta_shares") or 0)
        est = float(a.get("est_amount") or 0.0)
        twe = float(a.get("target_weight_effective") or 0.0)
        w2c = float(a.get("weight_to_cash") or 0.0)
        pre = code + "[" + kind + "]" + tag
        # ---- 通用：股数 / delta / 金额 / **有符号**归现金 ----
        if delta != sh_eff - cur:
            out.append(pre + ": delta_shares=" + str(delta) + " ≠ 目标股数 " + str(sh_eff)
                       + "-当前股数 " + str(cur) + "=" + str(sh_eff - cur))
        if abs(est - delta * px) > AMT_TOL:
            out.append(pre + ": est_amount=" + ("%.2f" % est) + " ≠ delta×价格 "
                       + str(delta) + "×" + ("%.6f" % px) + "=" + ("%.2f" % (delta * px)))
        want_w2c = w - twe
        if abs(w2c - want_w2c) > TOL:
            out.append(pre + ": weight_to_cash=" + ("%.8f" % w2c)
                       + " ≠ **有符号**差额（目标权重 " + ("%.8f" % w) + "-落地权重 "
                       + ("%.8f" % twe) + "）=" + ("%.8f" % want_w2c))
        # ---- 分类不变量 ----
        if kind == "交易":
            want = _round_lot(w * eff / px)
            if sh_eff != want:
                out.append(pre + ": 生效股数 " + str(sh_eff) + " ≠ 重算 floor("
                           + ("%.8f" % w) + "×" + ("%.2f" % eff) + "/" + ("%.6f" % px)
                           + "/整手)×整手=" + str(want))
        elif kind == "现金被缩":
            want_twe = ((cur + delta) * px / eff) if eff else 0.0
            if abs(twe - want_twe) > TOL:
                out.append(pre + ": target_weight_effective=" + ("%.8f" % twe)
                           + " ≠ 节点日价重算 " + str(cur + delta) + "×" + ("%.6f" % px)
                           + "/" + ("%.2f" % eff) + "=" + ("%.8f" % want_twe))
        elif kind == "门槛拦下":
            if delta != 0:
                out.append(pre + ": delta_shares=" + str(delta) + " ≠ 0（门槛拦下 ⇒ 不调仓）")
            if abs(est) > AMT_TOL:
                out.append(pre + ": est_amount=" + ("%.2f" % est) + " ≠ 0（门槛拦下 ⇒ 金额归零）")
            if str(a.get("action") or "") not in ("持有", "不建仓"):
                out.append(pre + ": action=" + str(a.get("action")) + " ∉ {持有, 不建仓}")
            if sh_eff != cur:
                out.append(pre + ": 生效股数 " + str(sh_eff) + " ≠ 当前股数 " + str(cur)
                           + "（门槛拦下 ⇒ 股数回退到当前股数）")
            want_twe = (cur * px / eff) if eff else 0.0
            if abs(twe - want_twe) > TOL:
                out.append(pre + ": target_weight_effective=" + ("%.8f" % twe)
                           + " ≠ 当前市值权重 " + str(cur) + "×" + ("%.6f" % px) + "/"
                           + ("%.2f" % eff) + "=" + ("%.8f" % want_twe))
            rec = ctx["gate"].get(code)
            if rec is None:
                out.append(pre + ": min_amount_gate.skipped 里没有该行 ⇒ 门槛披露与行不自洽")
            else:
                if str(rec.get("kind") or "") != str(a.get("min_amount_kind") or ""):
                    out.append(pre + ": 门槛档位 " + str(rec.get("kind")) + " ≠ 行内 "
                               + str(a.get("min_amount_kind")))
                if abs(float(rec.get("amount") or 0.0)
                       - float(a.get("min_amount_skipped_amount") or 0.0)) > AMT_TOL:
                    out.append(pre + ": 门槛金额 " + str(rec.get("amount")) + " ≠ 行内 "
                               + str(a.get("min_amount_skipped_amount")))
                if abs(float(rec.get("threshold") or 0.0)
                       - float(a.get("min_amount_threshold") or 0.0)) > AMT_TOL:
                    out.append(pre + ": 门槛阈值 " + str(rec.get("threshold")) + " ≠ 行内 "
                               + str(a.get("min_amount_threshold")))
                if float(rec.get("amount") or 0.0) >= float(rec.get("threshold") or 0.0):
                    out.append(pre + ": 被拦金额 " + str(rec.get("amount")) + " ≥ 门槛 "
                               + str(rec.get("threshold")) + " ⇒ 不该被拦")
        elif kind == "维持":
            if delta != 0:
                out.append(pre + ": delta_shares=" + str(delta) + " ≠ 0（维持 ⇒ 不调仓）")
            if abs(est) > AMT_TOL:
                out.append(pre + ": est_amount=" + ("%.2f" % est) + " ≠ 0（维持 ⇒ 不调仓）")
            if str(a.get("action") or "") not in ("维持", "持有"):
                out.append(pre + ": action=" + str(a.get("action")) + " ∉ {维持, 持有}")
            if sh_eff != cur:
                out.append(pre + ": 生效股数 " + str(sh_eff) + " ≠ 当前股数 " + str(cur)
                           + "（维持 ⇒ 不做整手重算）")
            want_twe = _hold_weight_expected(plan_rows.get(code), plan_total)
            if want_twe is None:
                out.append(pre + ": 维持席位缺持仓反推口径（account_price / current_market_value）"
                           " ⇒ 无法独立重算")
            elif abs(twe - want_twe) > TOL:
                out.append(pre + ": target_weight_effective=" + ("%.8f" % twe)
                           + " ≠ 持仓反推权重 " + str(cur) + "×"
                           + ("%.6f" % float(_hold_account_price(plan_rows.get(code)) or 0.0))
                           + "/" + ("%.2f" % plan_total) + "=" + ("%.8f" % want_twe))
        else:  # 零动作：股数与权重都**不是**由本次交易算出来的 ⇒ 只校验合法口径集合
            if sh_eff != cur:
                out.append(pre + ": 生效股数 " + str(sh_eff) + " ≠ 当前股数 " + str(cur))
            cands = [w]
            if eff and cur > 0:
                cands.append(cur * px / eff)
            hold_w = _hold_weight_expected(plan_rows.get(code), plan_total)
            if hold_w is not None:
                cands.append(hold_w)
            if cur <= 0:
                cands.append(0.0)
            if not any(abs(twe - c) <= TOL for c in cands):
                out.append(pre + ": target_weight_effective=" + ("%.8f" % twe)
                           + " ∉ 合法口径集合 {沿用计划权重 " + ("%.8f" % w) + "}"
                           + ("｜节点日价重算" if eff and cur > 0 else "")
                           + "｜持仓反推")
    return out


def _p7_violations(ctx: dict, plan_rows: dict, plan_total: float, tag: str,
                   prices: dict, plan_price_by_code: dict) -> list[str]:
    """P7：逐条跨档区间**独立重算**（lo/hi 公式 + 价格来源 + 漂移百分比）。

    ⚠ 「生效总资产 ∈ [lo, hi)」的夹逼**只对交易席位**成立：
    维持席位 / 门槛拦下席位的股数**不是**由权重正推出来的
    ⇒ 用同一套夹逼去卡它们 = **假红**（11-13 实测：512800 生效总资产 30212.67
    不在区间 [30833.28, 31233.71) ⇒ 其实只是「权重用快照价、股数用节点日价」）。
    """
    out: list[str] = []
    eff = ctx["eff"]
    for r in ctx["rows"]:
        code = str(r.get("code") or "")
        w = float(r.get("target_weight") or 0.0)
        px = float(r.get("price") or 0.0)
        if w <= 0 or px <= 0:
            continue
        sh_eff = int(r.get("shares_effective") or 0)
        kind = ctx["kinds"].get(code, "零动作")
        pre = code + "[" + kind + "]" + tag
        # ① 独立核对价格来源：应是**节点日收盘价**（无行情才回退计划件价）
        exp_px = (float(prices[code][0]) if code in prices
                  else plan_price_by_code.get(code, 0.0))
        if exp_px and abs(px - exp_px) > TOL:
            out.append(pre + ": 价格与独立取价不符｜产物 " + ("%.6f" % px)
                       + " vs 独立取价 " + ("%.6f" % exp_px))
        # ② 独立重算区间公式：lo = 股数 × 价格 / 权重；hi = (股数+整手) × 价格 / 权重
        lo = float(r.get("total_assets_window_lo") or 0.0)
        hi = float(r.get("total_assets_window_hi") or 0.0)
        lo_exp = sh_eff * px / w
        hi_exp = (sh_eff + LOT) * px / w
        if abs(lo - lo_exp) > TOL or abs(hi - hi_exp) > TOL:
            out.append(pre + ": 区间公式不符｜产物 lo=" + ("%.4f" % lo)
                       + " hi=" + ("%.4f" % hi)
                       + "｜独立重算 lo=" + ("%.4f" % lo_exp)
                       + " hi=" + ("%.4f" % hi_exp))
            continue
        # ③ 夹逼：**只对交易席位**（股数由权重正推）成立
        if kind == "交易":
            if not (lo <= eff < hi):
                out.append(pre + ": 生效总资产 " + ("%.2f" % eff)
                           + " 不在区间 [" + ("%.2f" % lo) + ", " + ("%.2f" % hi) + ")")
        elif kind == "维持":
            #: 维持席位的区间按**权重自身的口径**解释（账户快照价 × 计划件总资产）：
            #:   权重 = 股数 × 账户快照价 ÷ 计划件总资产 ⇒ 区间下沿 = 计划件总资产
            #: ⇒ 「维持席位的股数在计划件总资产下不跨档」必须成立（区间语义不许被破坏）。
            acct_px = _hold_account_price(plan_rows.get(code))
            if acct_px is None:
                out.append(pre + ": 维持席位缺账户快照价 ⇒ 无法独立重算区间语义")
            else:
                lo_plan = sh_eff * acct_px / w
                hi_plan = (sh_eff + LOT) * acct_px / w
                if lo_plan - plan_total > TOL or plan_total >= hi_plan - TOL:
                    out.append(pre + ": 维持席位按权重自身口径的区间 [" + ("%.2f" % lo_plan)
                               + ", " + ("%.2f" % hi_plan) + ") 不覆盖计划件总资产 "
                               + ("%.2f" % plan_total))
        else:
            a = ctx["actions"].get(code) or {}
            if int(a.get("delta_shares") or 0) != 0 and kind != "现金被缩":
                out.append(pre + ": 非交易席位却有 delta_shares="
                           + str(a.get("delta_shares")) + " ⇒ 区间被当成「权重→股数」映射")
        # ④ 区间相对生效总资产的位置（漂移百分比）独立重算
        for key, edge in (("drift_to_window_lo_pct", lo), ("drift_to_window_hi_pct", hi)):
            got = r.get(key)
            exp = ((edge - eff) / eff * 100) if eff else None
            if exp is None:
                continue
            if got is None or abs(float(got) - exp) > 1e-4:
                out.append(pre + ": " + key + "=" + str(got) + " ≠ 独立重算 "
                           + ("%.6f" % exp))
    return out


def _seat_summary(ctx: dict, plan_rows: dict, plan_total: float, tag: str) -> str:
    """逐席位的关键数字摘要（PASS 的 detail 要能从产物数字直接看出结论）。"""
    bits = []
    for r in ctx["rows"]:
        code = str(r.get("code") or "")
        a = ctx["actions"].get(code) or {}
        w = float(r.get("target_weight") or 0.0)
        px = float(r.get("price") or 0.0)
        if w <= 0 or px <= 0:
            continue
        kind = ctx["kinds"].get(code, "零动作")
        hold_w = _hold_weight_expected(plan_rows.get(code), plan_total)
        extra = ""
        if kind == "维持" and hold_w is not None:
            extra = "｜持仓反推 " + ("%.8f" % hold_w)
        elif kind == "门槛拦下":
            rec = ctx["gate"].get(code) or {}
            extra = "｜被拦 " + str(rec.get("amount")) + " < 门槛 " + str(rec.get("threshold"))
        bits.append(code + "[" + kind + "] 权重 " + ("%.8f" % w)
                    + " 股数 " + str(int(r.get("shares_effective") or 0))
                    + " 落地权重 " + ("%.8f" % float(a.get("target_weight_effective") or 0.0))
                    + " 归现金 " + ("%.8f" % float(a.get("weight_to_cash") or 0.0))
                    + extra)
    return tag + "：" + ("；".join(bits) if bits else "无席位")


def _synth_hold_seat(ctx: dict, plan_rows: dict, plan_total: float):
    """数据集里**没有**「维持席位」时 ⇒ 合成一条（口径与真实行完全一致）。

    ⇒ D7 负例永远可跑、且不依赖具体标的 ⇒ 稳定复现（无网络 / 无时间依赖）。
    返回 (code, basis_comparison 行, action 行)；造不出来返回 (None, None, None)。
    """
    for code, a in ctx["actions"].items():
        cur = int(a.get("current_shares") or 0)
        px = float(a.get("price") or 0.0)
        w = _hold_weight_expected(plan_rows.get(code), plan_total)
        if cur <= 0 or px <= 0 or w is None or w <= 0:
            continue
        eff = ctx["eff"]
        lo = cur * px / w
        hi = (cur + LOT) * px / w
        row = {
            "code": code, "name": a.get("name") or "", "price": px, "target_weight": w,
            "shares_plan_basis": _round_lot(w * plan_total / px),
            "shares_node_basis": _round_lot(w * eff / px) if eff else 0,
            "shares_effective": cur,
            "total_assets_window_lo": lo, "total_assets_window_hi": hi,
            "drift_to_window_lo_pct": ((lo - eff) / eff * 100) if eff else None,
            "drift_to_window_hi_pct": ((hi - eff) / eff * 100) if eff else None,
        }
        act = {
            "code": code, "name": a.get("name") or "", "action": "维持",
            "current_shares": cur, "target_shares": cur, "delta_shares": 0,
            "est_amount": 0.0, "target_weight": w, "target_weight_effective": w,
            "weight_to_cash": 0.0, "price": px,
            "ta_rejected": True, "held_not_touched": True,
        }
        return code, row, act
    return None, None, None


def _synth_gate_seat(ctx: dict, plan_rows: dict, plan_total: float):
    """数据集里**没有**「门槛拦下席位」时 ⇒ 合成一条（口径与真实行完全一致）。

    ⇒ D8 负例永远可跑 ⇒ 稳定复现。返回 (code, basis 行, action 行, 门槛披露记录)。
    """
    code = "999999"
    px = 1.0
    w = 0.01
    thr = 3800.0
    amt = 500.0
    eff = ctx["eff"]
    lo = 0.0
    hi = LOT * px / w
    row = {
        "code": code, "name": "合成门槛席位", "price": px, "target_weight": w,
        "shares_plan_basis": _round_lot(w * plan_total / px),
        "shares_node_basis": _round_lot(w * eff / px) if eff else 0,
        "shares_effective": 0,
        "total_assets_window_lo": lo, "total_assets_window_hi": hi,
        "drift_to_window_lo_pct": ((lo - eff) / eff * 100) if eff else None,
        "drift_to_window_hi_pct": ((hi - eff) / eff * 100) if eff else None,
    }
    act = {
        "code": code, "name": "合成门槛席位", "action": "不建仓",
        "current_shares": 0, "target_shares": 0, "delta_shares": 0, "est_amount": 0.0,
        "target_weight": w, "target_weight_effective": 0.0, "weight_to_cash": w,
        "price": px, "min_amount_skipped": True, "min_amount_kind": "新开仓",
        "min_amount_threshold": thr, "min_amount_skipped_amount": amt,
    }
    rec = {"code": code, "name": "合成门槛席位", "kind": "新开仓",
           "amount": amt, "threshold": thr}
    return code, row, act, rec


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
    #: 🔴 plan 口径产物用 **plan 生效总资产**、node 口径产物用 **node 生效总资产**
    #:    ⇒ 两个口径的产物**都要**独立重算（只查 node 口径 ⇒ 漏掉一半覆盖面）。
    eff_plan = float(bs_plan.get("effective_total_assets") or 0.0)
    plan_rows = {str(a.get("code") or ""): a for a in (plan_doc.get("actions") or [])}
    ctx_plan = _ctx(p_plan, eff_plan)
    ctx_node = _ctx(p_node, eff)

    bad_p6 = (_p6_violations(ctx_plan, plan_rows, plan_total, "[plan口径]")
              + _p6_violations(ctx_node, plan_rows, plan_total, "[node口径]"))
    bad_p7 = (_p7_violations(ctx_plan, plan_rows, plan_total, "[plan口径]",
                             prices, plan_price_by_code)
              + _p7_violations(ctx_node, plan_rows, plan_total, "[node口径]",
                               prices, plan_price_by_code))

    bad_p8 = []
    for r in rows:
        code = r.get("code")
        w = float(r.get("target_weight") or 0.0)
        px = float(r.get("price") or 0.0)
        if w <= 0 or px <= 0:
            continue
        want_plan = _round_lot(w * plan_total / px)
        want_node = _round_lot(w * node_total / px)
        if int(r.get("shares_plan_basis") or 0) != want_plan:
            bad_p8.append(str(code) + ": plan 股数 " + str(r.get("shares_plan_basis"))
                          + " ≠ 重算 " + str(want_plan))
        if int(r.get("shares_node_basis") or 0) != want_node:
            bad_p8.append(str(code) + ": node 股数 " + str(r.get("shares_node_basis"))
                          + " ≠ 重算 " + str(want_node))

    checker.add("P6", "逐席位口径自洽（交易席位=权重正推股数；维持席位=持仓反推权重；"
                      "门槛拦下席位=门槛政策+披露对账）",
                not bad_p6, ("违例 " + str(len(bad_p6)) + "：" + str(bad_p6[:3]))
                if bad_p6 else (_seat_summary(ctx_plan, plan_rows, plan_total, "plan口径")
                                + "｜" + _seat_summary(ctx_node, plan_rows, plan_total, "node口径")))
    checker.add("P7", "逐条跨档区间**独立重算**（lo/hi 公式 + 价格来源 + 漂移百分比）；"
                      "「生效总资产∈区间」夹逼只对**交易席位**成立",
                not bad_p7, ("违例 " + str(len(bad_p7)) + "：" + str(bad_p7[:3]))
                if bad_p7 else ("plan/node 两口径区间公式与价格全部独立重算一致；"
                                + str(len(ctx_plan["rows"]) + len(ctx_node["rows"])) + " 条"))
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
        #: 🔴 2026-11-13（canonical 根实测假红）：旧实现取**第一个**有行情的持仓
        #:   ⇒ canonical 根里那是 **512690 / 2000 股 ≈ 890 元**（小仓位）
        #:   ⇒ 10% 跌幅只压低 89 元 ⇒ **压不过基线漂移 +292.5 元**
        #:   ⇒ 漂移仍是 +0.68%（向上）⇒ 场景没 bites ⇒ P14 假红。
        #: ⇒ 改法：取**节点日市值最大**的持仓（改价对 node 口径总资产影响最大），
        #:   且若 ×0.90 仍压不下漂移 ⇒ 按「压到 plan_total×98%」**反算**跌幅
        #:   ⇒ 场景**一定 bites**（断言本身一个字没改）。
        held = [str(a.get("code") or "").zfill(6)
                for a in (plan_doc.get("actions") or [])
                if int(a.get("current_shares") or 0) > 0]
        if not held:
            raise ValueError("计划件里没有 current_shares>0 的持仓 ⇒ 无法构造合成漂移场景")
        held_shares = {str(a.get("code") or "").zfill(6): int(a.get("current_shares") or 0)
                       for a in (plan_doc.get("actions") or [])}
        cands = []
        for _code in held:
            _mask = (df["code"] == _code) & (df["date"].astype(str) == node_date)
            if int(_mask.sum()) == 0:
                continue
            _close = float(df.loc[_mask, "close"].iloc[0])
            _cur = int(held_shares.get(_code) or 0)
            if _cur <= 0 or _close <= 0:
                continue
            cands.append((_cur * _close, _code))
        if not cands:
            raise ValueError("计划件的持仓 " + ",".join(held) + " 在节点日 " + node_date
                             + " 都没有行情行 ⇒ 无法构造合成漂移场景")
        cands.sort(reverse=True)
        synth_code = cands[0][1]
        mask = (df["code"] == synth_code) & (df["date"].astype(str) == node_date)
        if int(mask.sum()) == 0:
            synth_detail = "合成场景未命中行：" + synth_code + " @ " + node_date
        else:
            #: 🔴 2026-11-13（canonical 根实测假红）：×0.90 对 canonical 根**压不下**
            #:   基线漂移（+292.5 元）⇒ 漂移仍**向上** ⇒ 场景没 bites ⇒ P14 假红。
            #: ⇒ 跌幅改成**阶梯**（0.90 → 0.80 → 0.70 → 0.60）：默认 ×0.90；
            #:   场景不满足条件就逐级加大 ⇒ 场景**一定 bites**（断言本身一个字没改）。
            orig_close = df.loc[mask, "close"].copy()
            synth_dir = TMP_ROOT / "synth"
            synth_dir.mkdir(parents=True, exist_ok=True)
            problems = ["跌幅阶梯没跑完"]
            pct_syn = None
            bs_syn = {}
            synth_factor = 0.90
            for _factor in (0.90, 0.80, 0.70, 0.60):
                synth_factor = _factor
                df.loc[mask, "close"] = orig_close * _factor
                synth_prices = TMP_ROOT / "synth_prices.csv"
                df.to_csv(synth_prices, index=False)
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
                    problems.append("方向判定=" + str(bs_syn.get("tripwire_direction"))
                                    + "（应 down）")
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
                if not problems:
                    break
            synth_ok = not problems
            synth_detail = (
                ("漂移=" + ("%.4f%%" % pct_syn) if pct_syn is not None else "漂移=?")
                + "｜方向=" + str(bs_syn.get("tripwire_direction"))
                + "｜阈值=" + str(bs_syn.get("first_tripwire_code"))
                + "/" + str(bs_syn.get("first_tripwire_side"))
                + "｜改单条数=" + str(bs_syn.get("n_shares_differ_plan_vs_node"))
                + "｜变异代码=" + str(synth_code)
                + "｜跌幅×" + ("%.4f" % synth_factor)
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
                #: 🔴 变异负例必须打在**真正的断言**上 ⇒ 复用 P7 的独立重算函数
                #:    （旧实现另写了一份简化检查 ⇒ 断言与变异可能各走各的）。
                m_ctx = dict(ctx_node)
                m_ctx["rows"] = m_rows
                bad = _p7_violations(m_ctx, plan_rows, plan_total, "[变异]",
                                     prices, plan_price_by_code)
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

    # ---- D7：真负例（**产物级**变异）：维持席位被篡改成交易席位（权重与股数不自洽）----
    d7_ok = False
    d7_detail = ""
    try:
        base = _clone_ctx(ctx_node)
        hold_codes = [c for c, k in base["kinds"].items() if k == "维持"]
        synth_note = ""
        if not hold_codes:
            #: 数据集里没有维持席位 ⇒ 合成一条（口径与真实行一致）⇒ 负例永远可跑。
            code, srow, sact = _synth_hold_seat(base, plan_rows, plan_total)
            if code is None:
                raise ValueError("既无维持席位、也合成不出来 ⇒ 负例无法构造")
            base["rows"] = [r for r in base["rows"]
                            if str(r.get("code")) != code] + [srow]
            base["actions"][code] = sact
            base["kinds"][code] = "维持"
            synth_note = "（合成维持席位 " + code + "）"
        else:
            code = hold_codes[0]
        #: 正例对照：原样数据必须零违例（否则说明「变异转红」不是变异造成的）
        pre = _p6_violations(base, plan_rows, plan_total, "[D7原样]")
        pre += _p7_violations(base, plan_rows, plan_total, "[D7原样]",
                              prices, plan_price_by_code)
        #: 变异：把维持席位改成交易席位 ⇒ 股数与目标权重**不自洽**
        mut = _clone_ctx(base)
        row = next(r for r in mut["rows"] if str(r.get("code")) == code)
        act = mut["actions"][code]
        w = float(row.get("target_weight") or 0.0)
        px = float(row.get("price") or 0.0)
        cur = int(act.get("current_shares") or 0)
        want = _round_lot(w * mut["eff"] / px)
        bad_shares = want + LOT          # 保证 ≠「权重正推」的股数 ⇒ 必然不自洽
        row["shares_effective"] = bad_shares
        act["target_shares"] = bad_shares
        act["delta_shares"] = bad_shares - cur
        act["est_amount"] = (bad_shares - cur) * px
        act["action"] = "买入" if act["delta_shares"] > 0 else "卖出"
        act.pop("ta_rejected", None)
        act.pop("held_not_touched", None)
        mut["kinds"][code] = _seat_kind(act, code in mut["scaled"])
        lo = bad_shares * px / w
        hi = (bad_shares + LOT) * px / w
        row["total_assets_window_lo"] = lo
        row["total_assets_window_hi"] = hi
        row["drift_to_window_lo_pct"] = ((lo - mut["eff"]) / mut["eff"] * 100) if mut["eff"] else None
        row["drift_to_window_hi_pct"] = ((hi - mut["eff"]) / mut["eff"] * 100) if mut["eff"] else None
        post = _p6_violations(mut, plan_rows, plan_total, "[D7变异]")
        post += _p7_violations(mut, plan_rows, plan_total, "[D7变异]",
                               prices, plan_price_by_code)
        d7_ok = (not pre) and bool(post)
        d7_detail = (synth_note + "席位 " + code
                     + "｜原样违例 " + str(len(pre)) + " 条（应 0）"
                     + "｜变异后违例 " + str(len(post)) + " 条：" + str(post[:2])
                     + "｜变异股数 " + str(bad_shares) + " ≠ 权重正推 " + str(want))
    except Exception as exc:  # noqa: BLE001
        d7_ok = False
        d7_detail = "负例构造异常：" + repr(exc)
    checker.add("D7", "真负例（变异）：维持席位被篡改成交易席位（目标权重与股数不自洽）"
                      " ⇒ P6/P7 必须报错",
                d7_ok, d7_detail)

    # ---- D8：真负例（**产物级**变异）：门槛拦下行的 weight_to_cash 被抹成 0 ----
    d8_ok = False
    d8_detail = ""
    try:
        base = _clone_ctx(ctx_node)
        gate_codes = [c for c, k in base["kinds"].items() if k == "门槛拦下"]
        synth_note = ""
        if not gate_codes:
            code, srow, sact, srec = _synth_gate_seat(base, plan_rows, plan_total)
            base["rows"] = [r for r in base["rows"]
                            if str(r.get("code")) != code] + [srow]
            base["actions"][code] = sact
            base["gate"][code] = srec
            base["kinds"][code] = "门槛拦下"
            synth_note = "（合成门槛拦下席位 " + code + "）"
        else:
            code = gate_codes[0]
        pre = _p6_violations(base, plan_rows, plan_total, "[D8原样]")
        pre += _p7_violations(base, plan_rows, plan_total, "[D8原样]",
                              prices, plan_price_by_code)
        mut = _clone_ctx(base)
        act = mut["actions"][code]
        w = float(act.get("target_weight") or 0.0)
        twe = float(act.get("target_weight_effective") or 0.0)
        act["weight_to_cash"] = 0.0     # ← 变异：抹掉**有符号**差额
        post = _p6_violations(mut, plan_rows, plan_total, "[D8变异]")
        post += _p7_violations(mut, plan_rows, plan_total, "[D8变异]",
                               prices, plan_price_by_code)
        d8_ok = ((not pre) and bool(post)
                 and any("weight_to_cash" in v for v in post))
        d8_detail = (synth_note + "席位 " + code
                     + "｜原样违例 " + str(len(pre)) + " 条（应 0）"
                     + "｜变异后违例 " + str(len(post)) + " 条：" + str(post[:2])
                     + "｜有符号差额应为 目标权重" + ("%.8f" % w) + "-落地权重"
                     + ("%.8f" % twe) + "=" + ("%.8f" % (w - twe)))
    except Exception as exc:  # noqa: BLE001
        d8_ok = False
        d8_detail = "负例构造异常：" + repr(exc)
    checker.add("D8", "真负例（变异）：门槛拦下行的 weight_to_cash 被改成 0"
                      "（而非**有符号**差额）⇒ P6 必须报错",
                d8_ok, d8_detail)

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
