#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「分歧组合矩阵」产物闸门探针（M-S6 新增）。

【用途】
把 `manu/etf_rotation/make_decision_scenarios.py` 生成的 2^N 行情景矩阵
（`decision_scenarios_<label>.json/md`，N = 分歧项个数）变成**可执行的闸门**：
脚本在仓库里常驻、生成器跑得通、**两次运行逐字节相同**（幂等）、
2^N 行情景与 5 条自洽断言齐全、A/A/A 情景现金与 `final_rebalance_plan.json` 对齐、
与 `decision_options_<label>.json` 逐项对账、且与已交付产物**逐字节一致**（漂移即 FAIL）。

🔴 **分歧项个数 / 情景数一律不写死**：N 与期望情景数（2^N）都从**交付件 payload**
    动态取（「全 A」组合标签也按产物里的分歧码顺序动态拼）⇒ 分歧项换人时
    探针不用改，也不会与新产物错配。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_decision_scenarios.py \
        --run-root <落点根>/runs
    （退出码 0 = 全过，1 = 有 FAIL，2 = 环境错）

【为什么需要】
`decision_options_<label>.md` 只给「每项各自选 A 还是 B」，**缺「多项一起选」时的账户后果**
（期末现金 / 权益 / 破单席 / 可执行性）。本探针保证那份矩阵
① 不会被静默改坏、② 不会与 `final_rebalance_plan.json` 脱节、③ 不会与 options 表脱节。

【断言清单】
P1  生成器脚本在仓库里常驻（存在 / 非空 / 是文件）
P2  生成器跑通：退出码 0 且日志里 5 条「断言 N: PASS」齐全
P3  幂等：两次独立运行产出的 json / md **逐字节**相同
P4  产物结构完整：必备顶层键齐全、情景数 = 2^N、5 条断言全 PASS、notes / disclaimer 非空
P5  A/A/A 情景期末现金 == `final_rebalance_plan.json` 的 `cash_flow.cash_end`（容差 0.01 元）
P6  **独立重算**：全部情景的现金 / 权益 / 总资产由 plan.json 手推，与产物逐项一致（容差 0.01 元）；
    分歧标的的落地股数也**从上游输入推导**（选项 A = 规则层 `target_shares`；
    选项 B = LLM 最终裁决的方向 / 保留手数 / 目标权重），**不写死常量**；
    且现金均不为负、情景间总资产最大偏差 ≤ 全部费用之和
P7  与 `decision_options_<label>.json` 的分歧项选项（股数 / 金额）逐项一致
P8  与**已交付**的 `decision_scenarios_<label>.json/md` 逐字节一致（漂移检测）
P9  run-root 零污染：既有文件 sha1 全未变、无新增/消失（临时目录在 run-root 之外）
D1  真负例：把生成结果副本的 A/A/A 现金改坏 ⇒ P5/P6 的独立复算必须 FAIL（证明闸门有牙）
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: 仓库根：探针位于 `<repo>/manu/etf_rotation/probes/` ⇒ 上溯三层到 `<repo>`
#: （parents[0]=probes、parents[1]=etf_rotation、parents[2]=manu、parents[3]=<repo>）
REPO_ROOT = Path(__file__).resolve().parents[3]
while not (REPO_ROOT / "manu" / "etf_rotation" / "make_decision_scenarios.py").is_file():
    if REPO_ROOT.parent == REPO_ROOT:
        break
    REPO_ROOT = REPO_ROOT.parent
GENERATOR = REPO_ROOT / "manu" / "etf_rotation" / "make_decision_scenarios.py"

sys.path.insert(0, str(REPO_ROOT / "manu" / "etf_rotation"))
import config as C  # noqa: E402  （落点常量单一事实源）

#: 成本口径（与 `final_rebalance_plan.json` 的 cash_flow 同源）
FEE_RATE_ONE_WAY = 0.00065
LOT_SIZE = 100
#: LLM 最终裁决里「落地 0 股」的方向词（卖 / 不买 / 不建仓 / 清仓）
ZERO_DIRECTIONS = {"卖", "不买", "不建仓", "清仓"}


class Checker:
    """断言收集器：记录 (id, 名称, 是否通过, 详情)。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((check_id, name, bool(ok), detail))


def sha1_of(path: Path) -> str:
    """算文件 sha1（读不到就抛异常，不静默跳过）。"""
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    return hashlib.sha1(path.read_bytes()).hexdigest()


def load_json(path: Path, label: str) -> dict:
    """读 JSON 对象；不是合法对象就抛中文异常（不许 fillna(0) 蒙混）。"""
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 统一转成中文错误
        raise ValueError(f"{label}不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象：{path}")
    return payload


def divergence_codes_of(payload: dict) -> list[str]:
    """从 `decision_scenarios_*.json` 的 payload 取**分歧码集合**（按产物里的顺序）。

    🔴 取值优先级：`meta.divergence_codes` → 顶层 `divergence_items` 的键 →
       各情景 `choices` 的键（按首次出现顺序）。
       **不写死任何具体代码、不写死分歧项个数** ⇒ 分歧项换人时探针不用改。
    """
    meta_codes = (payload.get("meta") or {}).get("divergence_codes")
    if isinstance(meta_codes, (list, tuple)) and meta_codes:
        return [str(code) for code in meta_codes]
    items = payload.get("divergence_items")
    if isinstance(items, dict) and items:
        return [str(code) for code in items]
    codes: list[str] = []
    for item in payload.get("scenarios") or []:
        for code in (item.get("choices") or {}):
            if str(code) not in codes:
                codes.append(str(code))
    return codes


def all_a_label(codes: list[str]) -> str:
    """「全 A」组合标签：按产物里的分歧码顺序拼接（**不写死任何具体代码**）。"""
    return ",".join(f"{code}=A" for code in codes)


def all_a_scenario(scenarios: list[dict], expect_label: str = "") -> dict | None:
    """找「全部选项都是 A」的那个情景（2^N 组合里唯一）。

    先按**动态标签**（`expect_label`，按产物里的分歧码顺序拼）匹配；
    标签形态对不上时退回**语义匹配**（choices 里全是 A）⇒ 既不依赖写死的标签串，
    也不依赖标签的书写格式。
    """
    if expect_label:
        for item in scenarios or []:
            if str(item.get("label") or "") == expect_label:
                return item
    for item in scenarios or []:
        choices = dict(item.get("choices") or {})
        if choices and all(letter == "A" for letter in choices.values()):
            return item
    return None


def recompute_scenario(plan: dict, choices: dict) -> tuple[float, float, float, float]:
    """独立重算一个情景的（期末现金, 期末权益, 总资产, 全部费用）。

    🔴 本函数**不复用** `make_decision_scenarios.py` 的任何函数，
    直接从 `final_rebalance_plan.json` 的 `actions` 手推（卖出计费、买入计费，单边费率 0.00065）。
    🔴 分歧标的的落地股数也**从上游输入推导**（见 `option_a_shares` / `option_b_shares`），
    **不写死常量** ⇒ 否则等于把交付产物里的答案抄成常量再回读校验（自证）。
    """
    cash = float(plan["cash_flow"]["cash_begin"])
    equity = 0.0
    fees = 0.0
    for action in plan["actions"]:
        code = str(action["code"])
        current = int(action.get("current_shares", 0))
        price = float(action["price"])
        if code in choices:
            letter = choices[code]
            if letter == "A":
                target = option_a_shares(action)
            elif letter == "B":
                target = option_b_shares(plan, action)
            else:
                raise ValueError(f"未知选项字母：{letter}（{code}）")
        else:
            target = int(action.get("target_shares", 0))
        sell = max(0, current - target)
        buy = max(0, target - current)
        if sell:
            amount = sell * price
            fee = amount * FEE_RATE_ONE_WAY
            cash += amount - fee
            fees += fee
        if buy:
            amount = buy * price
            fee = amount * FEE_RATE_ONE_WAY
            cash -= amount + fee
            fees += fee
        if target > 0:
            equity += target * price
    return cash, equity, cash + equity, fees


def option_a_shares(action: dict) -> int:
    """选项 A（采纳规则层）的落地股数 = `actions[code].target_shares`。

    🔴 规则层**已经含「整手买不起 ⇒ 0 股」的口径**（货基类一手 ≈ 1 万元 > 单席上限
    ≈ 5,971 元 ⇒ `target_shares = 0`），这里只做转录，不另设常量。
    """
    return int(action["target_shares"])


def option_b_shares(plan: dict, action: dict) -> int:
    """选项 B（采纳 LLM 最终裁决）的落地股数 = 从 `llm_final_decision.targets[code]` **推导**。

    * 方向 ∈ {卖, 不买, 不建仓, 清仓} ⇒ **0 股**；
    * **有目标权重（`target_pct`）⇒ 按目标权重落地**（`floor(目标金额 ÷ 价格 ÷ 手数) × 手数`）
      —— 与 `make_decision_options` / `make_decision_scenarios` **同口径**；
    * 没有目标权重、方向 = 减 且原文给了保留手数（`shares_keep`）⇒ **保留该股数**；
    * 方向 = 买（或方向未知）⇒ 目标权重 × 账户总资产 ⇒ 折**整手**股数
      （`floor(目标金额 ÷ 价格 ÷ 手数) × 手数`）；
    * 通用兜底：**算出来的股数若对应金额 < 一手成本（价格 × 手数）⇒ 整手买不起 ⇒ 0 股**。

    🔴 2026-11-11 口径修正（实测 2026-11-03 那一轮决策 md 实证）：
       原文：「保留3,400份、卖出4,300份；保留市值约2,971.60元、权重约9.95%」
       —— 这 3,400 份是**按参考价 0.874 元测算**的**示例**，而计划落地用的是**执行价**；
       旧口径（`减` + `shares_keep` ⇒ 用股数）⇒ 3,400 × 0.88 = 2,992 元
       = 总资产的 **10.02%** ⇒ **突破原文自己写的硬上限 10%**；
       而按**目标权重 10%** 向下取整到整手 = 3,300 份 = 2,904 元 = **9.73%** ⇒ 不破上限。
       ⇒ **目标权重是 LLM 的主口径，股数只是参考价下的示例** ⇒ 目标权重优先。
       （生成器 `make_decision_scenarios` / `make_decision_options` 本来就是这个口径
         ⇒ 旧口径会让独立重算与产物**必然对不上** ⇒ P6 假红。）
    """
    code = str(action["code"])
    price = float(action["price"])
    final = (plan.get("llm_final_decision") or {}).get("targets", {}).get(code)
    if not isinstance(final, dict):
        raise ValueError(f"llm_final_decision.targets 缺 {code} 的条目（抽不出 ⇒ 报错，不许默认）")
    direction = str(final.get("direction") or "").strip()
    target_pct = final.get("target_pct")
    if direction in ZERO_DIRECTIONS:
        shares = 0
    elif target_pct is not None:
        # 🔴 目标权重优先（口径见 docstring）：LLM 的主口径是「降至 X%」，
        #    原文股数只是**参考价**下的示例 ⇒ 用执行价落地时会漂移（可能破硬上限）。
        shares = shares_from_target_pct(float(target_pct), price, plan_total_assets(plan))
    elif direction == "减" and final.get("shares_keep") is not None:
        shares = int(final["shares_keep"])
    else:
        raise ValueError(f"llm_final_decision.targets[{code}] 无方向也无 target_pct（抽不出 ⇒ 报错）")
    one_lot_cost = price * LOT_SIZE
    if shares > 0 and shares * price < one_lot_cost - 1e-9:
        shares = 0
    return shares


def shares_from_target_pct(target_pct: float, price: float, total_assets: float) -> int:
    """目标权重 → 整手股数：`floor(目标金额 ÷ 价格 ÷ 手数) × 手数`。"""
    target_amount = total_assets * float(target_pct) / 100.0
    return int(math.floor(target_amount / price / LOT_SIZE) * LOT_SIZE)


def plan_total_assets(plan: dict) -> float:
    """账户总资产（把目标权重折成股数的分母）——取自 plan 自身的 `meta`。"""
    value = (plan.get("meta") or {}).get("total_assets")
    if value is None:
        raise ValueError("final_rebalance_plan.json 的 meta 缺 total_assets（无法把目标权重折成股数）")
    return float(value)


def derived_option_shares(plan: dict, codes: list[str]) -> dict[str, dict[str, int]]:
    """给出每个分歧标的的 A / B 落地股数（**当场推导**，不是查表）。

    写进 P6 的 detail ⇒ 人工一眼能看出「推导口径」与「产物数字」是否同源。
    """
    out: dict[str, dict[str, int]] = {}
    for action in plan.get("actions") or []:
        code = str(action["code"])
        if code not in codes:
            continue
        out[code] = {"A": option_a_shares(action), "B": option_b_shares(plan, action)}
    return out

def run_checks(run_root: Path) -> list[tuple[str, str, bool, str]]:
    """跑全部断言，返回断言列表。"""
    checker = Checker()
    #: run-root 文件 sha1 快照（P9 零污染比对用；`_` 开头的闸门日志不计）
    before_sha1 = {p.name: sha1_of(p) for p in sorted(run_root.iterdir()) if p.is_file()}
    label = "20261105"
    plan_path = run_root / "final_rebalance_plan.json"
    options_path = run_root / f"decision_options_{label}.json"
    scenarios_path = run_root / f"decision_scenarios_{label}.json"
    scenarios_md_path = run_root / f"decision_scenarios_{label}.md"

    # --- P1 生成器常驻 ---
    p1_ok = GENERATOR.is_file() and GENERATOR.stat().st_size > 0
    checker.add("P1", "生成器脚本在仓库里常驻（存在 / 非空）", p1_ok,
                f"{GENERATOR}" if p1_ok else f"缺失或为空：{GENERATOR}")

    plan = load_json(plan_path, "final_rebalance_plan.json")
    options = load_json(options_path, f"decision_options_{label}.json")
    delivered = load_json(scenarios_path, f"decision_scenarios_{label}.json")
    #: 分歧码集合 / 期望情景数：**从交付件 payload 动态取**（N 个分歧项 ⇒ 2^N 情景）
    #: 🔴 不写死 8 / 16 / 3 / 4 ⇒ 分歧项换人时探针不用改，也不会与新产物错配。
    ref_codes = divergence_codes_of(delivered)
    expected_scen = 2 ** len(ref_codes)
    aaa_label = all_a_label(ref_codes)

    tmp_parent = run_root.parent
    if not str(tmp_parent).startswith("/mnt/data/"):
        tmp_parent = C.WORK_ROOT
    tmp_root = tmp_parent / "_scen_probe_tmp"

    def run_generator(out_dir: Path) -> tuple[int, str]:
        """把生成器跑到指定目录，返回 (退出码, 合并日志)。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), "--run-root", str(run_root), "--out-dir", str(out_dir)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    try:
        shutil.rmtree(tmp_root, ignore_errors=True)
        rc1, log1 = run_generator(tmp_root / "run1")

        # --- P2 生成器跑通 + 5 条断言 PASS ---
        #: 只认「断言 N: PASS」形态的行；末尾汇总行「5 条断言全部 PASS ⇒ 退出码 0」不算一条断言
        pass_lines = [ln for ln in log1.splitlines()
                      if re.search(r"断言\s*\d+\s*[:：]\s*PASS", ln)]
        p2_ok = rc1 == 0 and len(pass_lines) == 5
        checker.add("P2", "生成器跑通：退出码 0 且 5 条自洽断言全 PASS", p2_ok,
                    f"rc={rc1}，断言 PASS 行 {len(pass_lines)} 条" if p2_ok
                    else f"rc={rc1}，断言 PASS 行 {len(pass_lines)} 条（期望 5）")

        # --- P3 幂等 ---
        rc2, _ = run_generator(tmp_root / "run2")
        p3_detail = ""
        p3_ok = False
        if rc2 == 0:
            diffs = []
            for name in (f"decision_scenarios_{label}.json", f"decision_scenarios_{label}.md"):
                a = tmp_root / "run1" / name
                b = tmp_root / "run2" / name
                if not (a.is_file() and b.is_file()):
                    diffs.append(f"{name} 缺失")
                    continue
                if sha1_of(a) != sha1_of(b):
                    diffs.append(f"{name} sha1 不同")
            p3_ok = not diffs
            p3_detail = "两次运行 json / md sha1 逐字节相同" if p3_ok else "；".join(diffs)
        else:
            p3_detail = f"第二次运行 rc={rc2}"
        checker.add("P3", "幂等：两次独立运行产出逐字节相同", p3_ok, p3_detail)

        fresh_path = tmp_root / "run1" / f"decision_scenarios_{label}.json"
        fresh_md_path = tmp_root / "run1" / f"decision_scenarios_{label}.md"
        fresh = load_json(fresh_path, f"decision_scenarios_{label}.json（新鲜生成）") if fresh_path.is_file() else {}

        # --- P4 结构完整 ---
        need_top = ["meta", "account", "divergence_items", "scenarios", "assertions", "notes", "disclaimer"]
        p4_problems = []
        for key in need_top:
            if key not in fresh:
                p4_problems.append(f"缺顶层键 {key}")
        scen_list = fresh.get("scenarios", [])
        #: 新鲜产物里的分歧码集合（逐情景 choices 的键去重）
        fresh_codes = sorted({c for item in scen_list for c in (item.get("choices") or {})})
        if not ref_codes:
            p4_problems.append("交付件里推不出分歧码集合（无法确定期望情景数 2^N）")
        else:
            if len(scen_list) != expected_scen:
                p4_problems.append(
                    f"情景数 {len(scen_list)} ≠ 2^{len(ref_codes)} = {expected_scen}")
            if fresh_codes != sorted(ref_codes):
                p4_problems.append(
                    f"新鲜产物的分歧码集合 {fresh_codes} ≠ 交付件 {sorted(ref_codes)}")
        assertions = fresh.get("assertions", [])
        if len(assertions) != 5:
            p4_problems.append(f"断言数 {len(assertions)} ≠ 5")
        for item in assertions:
            if item.get("status") != "PASS":
                p4_problems.append(f"断言 {item.get('id')} 状态 {item.get('status')}")
        if not fresh.get("notes"):
            p4_problems.append("notes 为空")
        if not fresh.get("disclaimer"):
            p4_problems.append("disclaimer 为空")
        p4_ok = not p4_problems
        checker.add("P4", "产物结构完整（必备键 / 情景数 = 2^N / 5 断言全 PASS / notes / disclaimer）",
                    p4_ok, "；".join(p4_problems) if p4_problems
                    else f"顶层键 {len(need_top)} 项、情景 {len(scen_list)} 个"
                         f"（= 2^{len(ref_codes)} = {expected_scen}）、"
                         f"断言 {len(assertions)} 条全 PASS")

        # --- P5 A/A/A 现金 == plan.cash_flow.cash_end ---
        # 🔴 修（2026-11-09 独立审计实证）：原来**写死**标签
        #    「全 A」组合标签串 ⇒ 分歧标的集合一变（本轮 LLM 裁决不同 ⇒
        #    分歧项就换人）就「找不到 A/A/A 情景」⇒ **假红**。
        #    ⇒ 改成**动态找「全部选项都是 A」的情景**（2^N 情景 ⇒ 唯一）
        #      ⇒ 断言强度不变（仍要求 A/A/A 现金 == plan.cash_flow.cash_end），
        #        但不再依赖「分歧恰好是这几只」。
        aaa = all_a_scenario(scen_list, aaa_label)
        p5_ok = False
        p5_detail = "找不到 A/A/A 情景（全部选项都是 A 的那一个）"
        if aaa is not None:
            expect = float(plan["cash_flow"]["cash_end"])
            diff = abs(float(aaa["cash_end"]) - expect)
            p5_ok = diff < 0.01
            p5_detail = (f"情景「{aaa.get('label')}」期末现金 {float(aaa['cash_end']):.6f} "
                         f"vs plan {expect:.6f}｜差 {diff:.10f} 元（容差 0.01）")
        checker.add("P5", "A/A/A 情景期末现金 == final_rebalance_plan.json 的 cash_flow.cash_end", p5_ok, p5_detail)

        # --- P6 独立重算（现金 / 权益 / 总资产）+ 非负现金 + 费用上界 ---
        # 🔴 2026-11-09 修：情景为空时 P6/P7 会「零违例 ⇒ PASS」而 detail 谎报
        #    「全部情景手推全对」⇒ 空洞通过。⇒ 空情景必须 FAIL。
        _scen_missing = ([] if scen_list else
                         ["新鲜生成的场景集合为空（生成器未产出 ⇒ 一条情景都没核对）"])
        p6_problems = list(_scen_missing)
        totals = []
        fee_sum = 0.0
        for item in scen_list:
            choices = dict(item.get("choices", {}))
            cash, equity, total, fees = recompute_scenario(plan, choices)
            fee_sum += fees
            totals.append(total)
            if abs(cash - float(item["cash_end"])) >= 0.01:
                p6_problems.append(f"{item['label']} 现金差 {cash - float(item['cash_end']):.6f}")
            if abs(equity - float(item["equity_amount"])) >= 0.01:
                p6_problems.append(f"{item['label']} 权益差 {equity - float(item['equity_amount']):.6f}")
            if abs(total - float(item["total_assets"])) >= 0.01:
                p6_problems.append(f"{item['label']} 总资产差 {total - float(item['total_assets']):.6f}")
            if float(item["cash_end"]) < 0:
                p6_problems.append(f"{item['label']} 现金为负")
        spread = (max(totals) - min(totals)) if totals else 0.0
        if spread > fee_sum + 1e-9:
            p6_problems.append(f"情景总资产最大偏差 {spread:.6f} > 费用之和 {fee_sum:.6f}")
        p6_ok = not p6_problems
        #: 分歧标的的落地股数（当场从 plan 推导）⇒ 写进 detail 供人工核对推导口径
        derived = derived_option_shares(plan, fresh_codes)
        shares_desc = "；".join(
            f"{code} A={derived[code]['A']}/B={derived[code]['B']}"
            for code in fresh_codes if code in derived)
        checker.add("P6", "独立重算：全部情景的现金/权益/总资产与产物一致（且现金非负、总资产偏差 ≤ 费用之和）",
                    p6_ok, "；".join(p6_problems) if p6_problems
                    else f"{len(scen_list)} 情景手推全对（选项股数由 plan 的 actions / llm_final_decision 推导："
                         f"{shares_desc}）｜总资产最大偏差 {spread:.6f} ≤ 费用之和 {fee_sum:.6f}")

        # --- P7 与 decision_options 逐项一致 ---
        p7_problems = list(_scen_missing)
        per_code = {p["code"]: p for p in options.get("options", {}).get("per_code_options", [])}
        for item in scen_list:
            choices = dict(item.get("choices", {}))
            positions = {p["code"]: p for p in item.get("positions", [])}
            for code, letter in choices.items():
                if code not in per_code:
                    p7_problems.append(f"decision_options 缺 {code}")
                    continue
                opt = per_code[code]["option_a"] if letter == "A" else per_code[code]["option_b"]
                pos = positions.get(code, {})
                if int(pos.get("shares", 0)) != int(opt["target_shares"]):
                    p7_problems.append(f"{item['label']}/{code} 股数差 {pos.get('shares', 0)} vs {opt['target_shares']}")
                if abs(float(pos.get("amount", 0.0)) - float(opt["landing_amount"])) >= 0.01:
                    p7_problems.append(f"{item['label']}/{code} 金额差 {pos.get('amount', 0.0)} vs {opt['landing_amount']}")
        p7_ok = not p7_problems
        checker.add("P7", "与 decision_options 的分歧项选项（股数 / 金额）逐项一致", p7_ok,
                    "；".join(p7_problems) if p7_problems
                    else f"{len(per_code)} 个分歧项 × 2 选项 × {len(scen_list)} 情景全部对齐")

        # --- P8 与已交付产物逐字节一致（漂移检测）---
        p8_problems = []
        # 🔴 交付件自身也要自洽：N 个分歧项 ⇒ 2^N 个情景（交付件坏了要单独报出来）
        if ref_codes and len(delivered.get("scenarios") or []) != expected_scen:
            p8_problems.append(
                f"交付件情景数 {len(delivered.get('scenarios') or [])} ≠ "
                f"2^{len(ref_codes)} = {expected_scen}")
        for path, name in ((fresh_path, "json"), (fresh_md_path, "md")):
            target = scenarios_path if name == "json" else scenarios_md_path
            if not target.is_file():
                p8_problems.append(f"交付件缺失 {target.name}")
                continue
            # 🔴 2026-11-09 修：新鲜生成件**不存在**时，旧实现直接 `sha1_of(path)` ⇒
            #    FileNotFoundError ⇒ 冒泡到顶层 `except` ⇒ 闸门只报 rc=2
            #    「无法完成」，把「生成器没产出」这个真因盖掉了。
            #    ⇒ 修法：缺件**判 FAIL**（漂移检测照样成立：缺件比字节漂移更严重）。
            if not path.is_file():
                p8_problems.append(f"新鲜生成结果缺失 {path.name}（生成器未产出 ⇒ 交付件无法核对）")
                continue
            if sha1_of(path) != sha1_of(target):
                p8_problems.append(f"{target.name} 与新鲜生成结果不一致（漂移）")
        p8_ok = not p8_problems
        checker.add("P8", "与已交付的 decision_scenarios_*.json/md 逐字节一致（漂移检测）", p8_ok,
                    "；".join(p8_problems) if p8_problems
                    else "json / md 的 sha1 与新鲜生成完全相同")

        # --- P9 run-root 零污染 ---
        p9_problems = []
        expected_new = {scenarios_path.name, scenarios_md_path.name}
        for name, digest in before_sha1.items():
            if name.startswith("_") or name in expected_new:
                continue
            path = run_root / name
            if not path.is_file():
                p9_problems.append(f"run-root 文件消失：{name}")
            elif sha1_of(path) != digest:
                p9_problems.append(f"run-root 文件被改写：{name}")
        now_names = {p.name for p in run_root.iterdir() if p.is_file()}
        for name in sorted(now_names - set(before_sha1)):
            if name.startswith("_") or name in expected_new:
                continue
            p9_problems.append(f"run-root 新增文件：{name}")
        p9_ok = not p9_problems
        checker.add("P9", "run-root 零污染（临时目录在 run-root 之外，既有文件 sha1 全未变、无新增/消失）",
                    p9_ok, "；".join(p9_problems) if p9_problems
                    else f"{len(before_sha1)} 个既有文件 sha1 全未变、无新增/消失（临时目录在 run-root 之外）")

        # --- D1 真负例：改坏 A/A/A 现金 ⇒ 独立复算必须 FAIL ---
        d1_ok = False
        d1_detail = "负例未能触发"
        if fresh:
            mutated = copy.deepcopy(fresh)
            #: 负例目标 =「全 A」情景（标签按产物里的分歧码顺序动态拼；找不到就退语义匹配）
            target = all_a_scenario(mutated.get("scenarios", []), aaa_label)
            if target is None:
                d1_detail = "找不到「全部选项都是 A」的情景 ⇒ 负例无法构造（闸门无牙）"
            else:
                target["cash_end"] = float(target["cash_end"]) + 100.0
                detected = False
                for item in mutated.get("scenarios", []):
                    choices = dict(item.get("choices", {}))
                    cash, _equity, _total, _fees = recompute_scenario(plan, choices)
                    if abs(cash - float(item["cash_end"])) >= 0.01:
                        detected = True
                        break
                d1_ok = detected
                d1_detail = (f"改坏「{target.get('label')}」后独立复算检出偏差 "
                             f"{cash - float(item['cash_end']):.6f} 元 ⇒ 闸门会 FAIL"
                             if detected else "改坏后独立复算**未**检出 ⇒ 闸门无牙")
        checker.add("D1", "真负例：A/A/A 现金改坏 +100 元 ⇒ 独立复算必须 FAIL（证明闸门有牙）",
                    d1_ok, d1_detail)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return checker.items


def main() -> int:
    """命令行入口：``--run-root`` 指向产物根。"""
    ap = argparse.ArgumentParser(description="分歧组合矩阵产物闸门")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()
    try:
        items = run_checks(Path(args.run_root))
    except Exception as exc:  # noqa: BLE001 —— 环境/输入错误统一退出码 2
        print(f"[ERROR] 闸门无法完成：{exc}", file=sys.stderr)
        return 2
    failed = [it for it in items if not it[2]]
    for check_id, name, ok, detail in items:
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", check_id, name)
              + (("｜" + detail) if detail else ""))
    print("合计 %d 项：PASS %d / FAIL %d" % (len(items), len(items) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
