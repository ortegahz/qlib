#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""节点日「分歧情景」重算闸门探针（M-S7 新增）。

【用途】
把「用户拍板 3 项分歧 ⇒ 12-01 当天到底买多少股」这条链变成**可执行的闸门**：
两个脚本常驻、**默认路径零回归**（规则层产物不被污染）、情景 A/A/A ≡ 规则层、
8 情景聚合件结构完整、现金非负、整手合法、**独立重算**现金流与破单席标记、幂等、
以及三个**真负例**（非法情景字母 / 未知代码 / 篡改现金 ⇒ 必须 FAIL）。

【为什么需要】
M-S7 之前，`reprice_node_day.py` 只会重算**纯规则层**清单 ⇒ 用户拍完板后
**没有任何工具**能算出「我选的那个方案在节点日价格下买多少股」⇒ 12-01 当天只能手算。
本探针保证这条链**不会悄悄坏掉**，且**默认（规则层）路径一个字节都没变**。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_node_scenarios.py \\
        --run-root /mnt/data/siping/runs/etf_rotation
    （退出码 0 = 全过，1 = 有 FAIL，2 = 环境错）

【断言清单】
P1  两个脚本在仓库里常驻（存在 / 非空）
P2  默认路径零回归：默认产物**无** `meta.scenario`、动作行**无** `scenario_override`
P3  情景 A/A/A 与默认运行**逐条一致**（股数 / 权重 / 动作）
P4  聚合件结构完整（8 情景 / notes 非空 / 3 条断言全 PASS / disclaimer 非空）
P5  8 情景期末现金**均不为负**且 `feasible`
P6  所有落地股数都是**整手**（100 股）
P7  **独立重算**：逐情景从 trades 手推现金恒等式 == 产物 `cash_end`（容差 0.01 元）
P8  **独立重算**：破单席标记完整（落地金额 > 单席上限 ⇔ `slot_breach`）
P9  幂等：聚合件两次独立运行 sha1 **逐字节**相同
P10 情景覆盖语义：选项 B 的落地股数与 `llm_final_decision.targets` 的 lots/target_pct 一致
D1  真负例：情景字母非法（X）⇒ 退出码 ≠ 0 且中文报错
D2  真负例：情景代码不在计划件里 ⇒ 退出码 ≠ 0 且中文报错
D3  真负例：篡改聚合件的 `cash_end` ⇒ P7 的独立重算必须 FAIL（证明闸门有牙）
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
while not (REPO_ROOT / "manu" / "etf_rotation" / "reprice_node_day.py").is_file():
    if REPO_ROOT.parent == REPO_ROOT:
        break
    REPO_ROOT = REPO_ROOT.parent

REPRICE = REPO_ROOT / "manu" / "etf_rotation" / "reprice_node_day.py"
BATCH = REPO_ROOT / "manu" / "etf_rotation" / "make_node_scenarios.py"
#: 整手（ETF 最小交易单位）
LOT = 100
#: 单席上限（账户口径：20% × 29,854.67）
SLOT_LIMIT = 5970.934
#: 账户起始现金
CASH0 = 15518.87


class Checker:
    """断言收集器。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((check_id, name, bool(ok), detail))


def sha1_of(path: Path) -> str:
    """文件 sha1；读不到就抛异常（不静默跳过）。"""
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    return hashlib.sha1(path.read_bytes()).hexdigest()


def read_json(path: Path, label: str) -> dict:
    """读 JSON 对象；不合法 ⇒ 中文异常。"""
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label}不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象：{path}")
    return payload


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 合并输出)。"""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="节点日分歧情景重算闸门")
    ap.add_argument("--run-root", default="/mnt/data/siping/runs/etf_rotation")
    args = ap.parse_args()
    run_root = Path(args.run_root)

    checker = Checker()
    plan_path = run_root / "final_rebalance_plan.json"
    choices_path = run_root / "decision_scenarios_20261105.json"

    # --- P1 两个脚本常驻 ---
    p1_ok = (REPRICE.is_file() and REPRICE.stat().st_size > 0
             and BATCH.is_file() and BATCH.stat().st_size > 0)
    checker.add("P1", "两个脚本在仓库里常驻（reprice_node_day.py / make_node_scenarios.py）", p1_ok,
                f"{REPRICE}｜{BATCH}" if p1_ok else "缺失或为空")

    plan = read_json(plan_path, "计划件")
    src = read_json(choices_path, "情景组合文件")
    combos = [(str(s["label"]), {str(k): str(v) for k, v in s["choices"].items()})
              for s in src.get("scenarios", [])]

    tmp_parent = run_root.parent
    if not str(tmp_parent).startswith("/mnt/data/"):
        tmp_parent = Path("/mnt/data/siping/runs")
    tmp_root = tmp_parent / "_node_scen_probe_tmp"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    try:
        # --- 跑默认（规则层）与聚合件 ---
        rc_def, log_def = run([sys.executable, str(REPRICE), "--node-date", "2026-11-04",
                               "--out-dir", str(tmp_root / "def")], REPO_ROOT)
        default_payload = {}
        if rc_def == 0:
            default_payload = read_json(tmp_root / "def" / "node_reprice_20261104.json",
                                        "默认重算产物")
        rc_batch, log_batch = run([sys.executable, str(BATCH), "--node-date", "2026-11-04",
                                   "--out-dir", str(tmp_root / "agg")], REPO_ROOT)
        agg = {}
        if rc_batch == 0:
            agg = read_json(tmp_root / "agg" / "node_scenarios_20261104.json", "情景聚合件")

        # --- P2 默认路径零回归 ---
        p2_problems = []
        if rc_def != 0:
            p2_problems.append(f"默认重算退出码 {rc_def}")
        else:
            if default_payload.get("meta", {}).get("scenario") is not None:
                p2_problems.append("默认产物里出现了 meta.scenario")
            for a in default_payload.get("actions", []):
                if "scenario_override" in a:
                    p2_problems.append(f"默认动作行 {a.get('code')} 出现了 scenario_override")
                if "slot_breach" in a:
                    p2_problems.append(f"默认动作行 {a.get('code')} 出现了 slot_breach")
        p2_ok = not p2_problems
        checker.add("P2", "默认路径零回归：默认产物无 meta.scenario / 无 scenario_override / 无 slot_breach",
                    p2_ok, "；".join(p2_problems) if p2_problems
                    else f"{len(default_payload.get('actions', []))} 条动作行全是纯规则层字段")

        # --- P3 情景 A/A/A ≡ 默认 ---
        p3_problems = []
        aaa = None
        common: set[str] = set()
        for s in agg.get("scenarios", []):
            if str(s.get("label", "")) == "511880=A,512800=A,563020=A":
                aaa = s
                break
        if aaa is None:
            p3_problems.append("聚合件里找不到 A/A/A 情景")
        else:
            def sig(d: dict) -> dict:
                return {x["code"]: (x.get("target_shares"), x.get("delta_shares"),
                                    round(float(x.get("target_weight") or 0.0), 10),
                                    x.get("action")) for x in d.get("actions", [])}
            default_sig = sig(default_payload)
            aaa_sig = {t["code"]: (t.get("target_shares"), t.get("delta_shares"),
                                   round(float(t.get("target_weight_pct") or 0.0) / 100.0, 10),
                                   t.get("action")) for t in aaa.get("trades", [])}
            # 默认产物里的「零变动」条目在聚合件里被剔除了 ⇒ 只比共同的（有变动的）条目
            common = set(default_sig) & set(aaa_sig)
            for code in sorted(common):
                if default_sig[code] != aaa_sig[code]:
                    p3_problems.append(f"{code}: 默认 {default_sig[code]} vs A/A/A {aaa_sig[code]}")
            for code in sorted(set(aaa_sig) - set(default_sig)):
                p3_problems.append(f"{code}: A/A/A 多出交易项")
        p3_ok = not p3_problems
        checker.add("P3", "情景 A/A/A 与默认（规则层）逐条一致", p3_ok,
                    "；".join(p3_problems) if p3_problems
                    else f"共同 {len(common)} 条交易项的股数/权重/动作全一致")

        # --- P4 聚合件结构完整 ---
        p4_problems = []
        scen = agg.get("scenarios", [])
        if len(scen) != 8:
            p4_problems.append(f"情景数 {len(scen)} ≠ 8")
        if len(combos) != 8:
            p4_problems.append(f"来源情景数 {len(combos)} ≠ 8")
        if not agg.get("notes"):
            p4_problems.append("notes 为空")
        if not agg.get("disclaimer"):
            p4_problems.append("disclaimer 为空")
        for a in agg.get("assertions", []):
            if a.get("status") != "PASS":
                p4_problems.append(f"聚合件断言 {a.get('id')} 状态 {a.get('status')}")
        p4_ok = not p4_problems
        checker.add("P4", "聚合件结构完整（8 情景 / notes / disclaimer / 断言全 PASS）", p4_ok,
                    "；".join(p4_problems) if p4_problems
                    else f"情景 {len(scen)} 个、notes {len(agg.get('notes', []))} 条、"
                         f"断言 {len(agg.get('assertions', []))} 条全 PASS")

        # --- P5 现金非负 + feasible ---
        p5_problems = []
        for s in scen:
            if float(s.get("cash_end") or 0.0) < -0.005:
                p5_problems.append(f"{s.get('label')} 现金 {s.get('cash_end')}")
            if not s.get("feasible", True):
                p5_problems.append(f"{s.get('label')} 不可行")
        p5_ok = not p5_problems
        checker.add("P5", "8 情景期末现金均不为负且可行", p5_ok,
                    "；".join(p5_problems) if p5_problems
                    else f"最小期末现金 {min(float(s['cash_end']) for s in scen):.2f} 元")

        # --- P6 整手 ---
        p6_problems = []
        for s in scen:
            for t in s.get("trades", []):
                if int(t.get("target_shares") or 0) % LOT != 0:
                    p6_problems.append(f"{s.get('label')}/{t.get('code')} {t.get('target_shares')}")
        p6_ok = not p6_problems
        checker.add("P6", "所有落地股数都是整手（100 股）", p6_ok,
                    "；".join(p6_problems) if p6_problems
                    else f"共 {sum(len(s.get('trades', [])) for s in scen)} 条交易逐条校验通过")

        # --- P7 独立重算现金恒等式 ---
        p7_problems = []
        for s in scen:
            cash = float(s.get("cash_begin") or CASH0)
            for t in s.get("trades", []):
                amt = float(t.get("est_amount") or 0.0)
                fee = float(t.get("est_fee") or 0.0)
                if t.get("delta_shares", 0) > 0:
                    cash -= abs(amt) + fee
                else:
                    cash += abs(amt) - fee
            if abs(cash - float(s.get("cash_end") or 0.0)) >= 0.01:
                p7_problems.append(f"{s.get('label')} 手推 {cash:.6f} vs 产物 {s.get('cash_end')}")
        p7_ok = not p7_problems
        checker.add("P7", "独立重算：逐情景现金恒等式与产物一致（容差 0.01 元）", p7_ok,
                    "；".join(p7_problems) if p7_problems
                    else "8 个情景手推全对")

        # --- P8 破单席标记完整性（独立重算）---
        p8_problems = []
        for s in scen:
            claimed = {b["code"] for b in s.get("slot_breaches", [])}
            recomputed = {t["code"] for t in s.get("trades", [])
                          if float(t.get("target_amount") or 0.0) > SLOT_LIMIT + 0.005}
            if claimed != recomputed:
                p8_problems.append(f"{s.get('label')} 重算 {recomputed} vs 产物 {claimed}")
        p8_ok = not p8_problems
        checker.add("P8", "独立重算：破单席标记完整（落地金额 > 单席上限 ⇔ 标记）", p8_ok,
                    "；".join(p8_problems) if p8_problems
                    else "8 个情景的破单席标记与独立重算全一致")

        # --- P9 幂等 ---
        p9_detail = ""
        p9_ok = False
        if rc_batch == 0:
            rc2, _ = run([sys.executable, str(BATCH), "--node-date", "2026-11-04",
                          "--out-dir", str(tmp_root / "agg2")], REPO_ROOT)
            if rc2 == 0:
                same = all(
                    sha1_of(tmp_root / "agg" / name) == sha1_of(tmp_root / "agg2" / name)
                    for name in ("node_scenarios_20261104.json", "node_scenarios_20261104.md")
                )
                p9_ok = same
                p9_detail = "两次运行 json / md sha1 逐字节相同" if same else "两次运行 sha1 不同"
            else:
                p9_detail = f"第二次运行退出码 {rc2}"
        else:
            p9_detail = f"第一次运行退出码 {rc_batch}"
        checker.add("P9", "幂等：聚合件两次独立运行逐字节相同", p9_ok, p9_detail)

        # --- P10 情景覆盖语义（选项 B 按 llm_final_decision 落地）---
        p10_problems = []
        llm_targets = ((plan.get("llm_final_decision") or {}).get("targets") or {})
        for s in scen:
            choices = s.get("choices") or {}
            for code, letter in choices.items():
                if letter != "B":
                    continue
                tgt = llm_targets.get(code) or {}
                lots = tgt.get("lots")
                pct = tgt.get("target_pct")
                hit = next((t for t in s.get("trades", []) if t["code"] == code), None)
                if lots not in (None, ""):
                    want = int(lots) * LOT
                    if hit is None or int(hit.get("target_shares") or 0) != want:
                        p10_problems.append(
                            f"{s.get('label')}/{code} 期望 {want} 股（买 {lots} 手），实际 "
                            f"{hit.get('target_shares') if hit else '未建仓'}")
                elif pct is not None and float(pct) == 0:
                    if hit is not None and int(hit.get("target_shares") or 0) > 0:
                        p10_problems.append(f"{s.get('label')}/{code} 应不建仓，实际建了仓")
                else:
                    if hit is None:
                        p10_problems.append(f"{s.get('label')}/{code} 应建仓但没建")
                    else:
                        want_pct = float(pct or 0.0)
                        got = float(hit.get("target_weight_pct") or 0.0)
                        if abs(got - want_pct) > 0.05:
                            p10_problems.append(
                                f"{s.get('label')}/{code} 权重 {got:.4f}% vs 裁决 {want_pct:.4f}%")
        p10_ok = not p10_problems
        checker.add("P10", "情景覆盖语义：选项 B 的落地股数/权重与 llm_final_decision 一致", p10_ok,
                    "；".join(p10_problems) if p10_problems
                    else "B 选项逐项按 lots / target_pct 落地")

        # --- D1 非法情景字母 ---
        rc_d1, out_d1 = run([sys.executable, str(REPRICE), "--node-date", "2026-11-04",
                             "--out-dir", str(tmp_root / "d1"), "--scenario", "511880=X"],
                            REPO_ROOT)
        d1_ok = rc_d1 != 0 and ("X" in out_d1 and "非法" in out_d1)
        checker.add("D1", "真负例：情景字母非法（X）⇒ 退出码 ≠ 0 且中文报错", d1_ok,
                    f"退出码 {rc_d1}" if d1_ok else f"退出码 {rc_d1}，输出：{out_d1.strip()[-200:]}")

        # --- D2 未知代码 ---
        rc_d2, out_d2 = run([sys.executable, str(REPRICE), "--node-date", "2026-11-04",
                             "--out-dir", str(tmp_root / "d2"), "--scenario", "999999=B"],
                            REPO_ROOT)
        d2_ok = rc_d2 != 0 and "999999" in out_d2 and "不在计划件" in out_d2
        checker.add("D2", "真负例：情景代码不在计划件里 ⇒ 退出码 ≠ 0 且中文报错", d2_ok,
                    f"退出码 {rc_d2}" if d2_ok else f"退出码 {rc_d2}，输出：{out_d2.strip()[-200:]}")

        # --- D3 篡改聚合件现金 ⇒ 独立重算必须 FAIL ---
        d3_ok = False
        d3_detail = "负例未能触发"
        if agg:
            mutated = copy.deepcopy(agg)
            for s in mutated.get("scenarios", []):
                s["cash_end"] = float(s.get("cash_end") or 0.0) + 100.0
                break
            detected = False
            for s in mutated.get("scenarios", []):
                cash = float(s.get("cash_begin") or CASH0)
                for t in s.get("trades", []):
                    amt = float(t.get("est_amount") or 0.0)
                    fee = float(t.get("est_fee") or 0.0)
                    if t.get("delta_shares", 0) > 0:
                        cash -= abs(amt) + fee
                    else:
                        cash += abs(amt) - fee
                if abs(cash - float(s.get("cash_end") or 0.0)) >= 0.01:
                    detected = True
                    break
            d3_ok = detected
            d3_detail = ("改坏后独立重算检出偏差 ⇒ 闸门会 FAIL" if detected
                         else "改坏后独立重算**未**检出 ⇒ 闸门无牙")
        checker.add("D3", "真负例：篡改聚合件 cash_end ⇒ 独立重算必须 FAIL（证明闸门有牙）",
                    d3_ok, d3_detail)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    failed = [it for it in checker.items if not it[2]]
    for check_id, name, ok, detail in checker.items:
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", check_id, name)
              + (("｜" + detail) if detail else ""))
    print("合计 %d 项：PASS %d / FAIL %d" % (len(checker.items), len(checker.items) - len(failed),
                                             len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 闸门无法完成：{exc}", file=sys.stderr)
        raise SystemExit(2)
