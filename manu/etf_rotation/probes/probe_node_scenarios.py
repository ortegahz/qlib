#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""节点日「分歧情景」重算闸门探针（M-S7 新增）。

【用途】
把「用户拍板 N 项分歧 ⇒ 12-01 当天到底买多少股」这条链变成**可执行的闸门**：
两个脚本常驻、**默认路径零回归**（规则层产物不被污染）、情景 A/A/A ≡ 规则层、
2^N 情景聚合件结构完整、现金非负、整手合法、**独立重算**现金流与破单席标记、幂等、
以及三个**真负例**（非法情景字母 / 未知代码 / 篡改现金 ⇒ 必须 FAIL）。

🔴 **分歧项个数 / 情景数一律不写死**：N 与期望情景数（2^N）都从**当前产物 payload**
    动态取（「全 A」组合标签、负例用的分歧码也都按产物里的顺序动态取）
    ⇒ 分歧项换人时探针不用改，也不会与新产物错配。

【为什么需要】
M-S7 之前，`reprice_node_day.py` 只会重算**纯规则层**清单 ⇒ 用户拍完板后
**没有任何工具**能算出「我选的那个方案在节点日价格下买多少股」⇒ 12-01 当天只能手算。
本探针保证这条链**不会悄悄坏掉**，且**默认（规则层）路径一个字节都没变**。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_node_scenarios.py \\
        --run-root <落点根>/runs
    （退出码 0 = 全过，1 = 有 FAIL，2 = 环境错）

【断言清单】
P1  两个脚本在仓库里常驻（存在 / 非空）
P2  默认路径零回归：默认产物**无** `meta.scenario`、动作行**无** `scenario_override`
P3  情景 A/A/A 与默认运行**逐条一致**（股数 / 目标金额 / 动作；🔴 2026-11-13 起
    **按原始浮点值比较**，容差只放浮点表示误差 —— 旧口径拿「默认的原始权重」比
    「聚合件的 4 位小数百分数（展示值）」⇒ 同一笔权重也因打印精度不同被判红）
P4  聚合件结构完整（情景数 = 2^N / notes 非空 / 3 条断言全 PASS / disclaimer 非空）
P5  全部情景期末现金**均不为负**且 `feasible`
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

#: 🔴 2026-11-09 修（critic 复核轮）：探针的**金额阈值 / 整手 / 起始现金**一律从
#:    **单一事实源**取（`account.py` 的账户口径 + `final_rebalance_plan.LOT` 的整手），
#:    **不许写死**（旧值：整手 100、单席上限 5,970.934 元、现金 15,518.87 元）⇒
#:    总资产 / 单席权重一变，探针与产品**静默分叉**（判红判绿都可能是错的）。
sys.path.insert(0, str(REPO_ROOT / "manu" / "etf_rotation"))
import config as C  # noqa: E402  （落点常量单一事实源）
import account as A  # noqa: E402


def _resolve_lot() -> int:
    """整手份数：真源 = `final_rebalance_plan.LOT`（兜底 100，与 `probe_node_reprice` 同款）。"""
    try:
        from final_rebalance_plan import LOT as _LOT  # noqa: PLC0415
        return int(_LOT)
    except Exception:  # noqa: BLE001
        return 100


#: 整手（ETF 最小交易单位）
LOT = _resolve_lot()
#: 单席上限（账户级 20%）= 总资产 × 单席权重 —— **兜底**口径（`account.py` 研究链旧账户）
#: 🔴 2026-11-10：这两个常量（5970.934 / 15518.87）是 **2026-11-04 的 5 只旧账户**口径。
#:    本轮该探针跑的是 canonical 根（口径自洽），但 canonical 根一旦换上
#:    **按新账户生成**的计划就会与产品（`reprice_node_day.py` 用的是
#:    **计划件口径**的 `total_assets × 单席权重`）错位 ⇒ 误判。
#: ⇒ 口径改成：**优先从计划件 `meta.total_assets` / `meta.cash` 派生**，
#:   取不到再回落这两个旧常量（见 `_account_basis`）。
SLOT_LIMIT = float(A.TOTAL_ASSETS) * float(A.TARGET_POSITION_WEIGHT)
#: 账户起始现金 —— **兜底**口径（`account.py` 研究链旧账户）
CASH0 = float(A.CASH)


def _account_basis(plan: dict | None) -> tuple[float, float, float, str]:
    """账户口径（总资产 / 现金 / 单席上限）—— **优先从计划件 `meta` 派生**，取不到回落旧常量。

    🔴 2026-11-10：`SLOT_LIMIT` / `CASH0` 写死旧账户常量 ⇒ canonical 根换上
       按新账户生成的计划后，探针的单席上限与产品（`reprice_node_day.py`
       用**计划件口径**的总资产 × 单席权重）就会错位 ⇒ P8 误判。
    ⇒ 现在：**计划件 meta 优先**，旧常量兜底；返回值第四项是**口径说明**
       （必须打进探针输出 ⇒ 不许悄悄换口径）。

    返回：(总资产, 现金, 单席上限, 口径说明)。
    """
    meta = (plan or {}).get("meta") or {}
    total = meta.get("total_assets")
    cash = meta.get("cash")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and float(total) > 0:
        total_assets = float(total)
        total_src = "计划件 meta.total_assets"
        slot_limit = total_assets * float(A.TARGET_POSITION_WEIGHT)
    else:
        total_assets = float(A.TOTAL_ASSETS)
        total_src = "account.py 常量（回落）"
        slot_limit = SLOT_LIMIT
    if isinstance(cash, (int, float)) and not isinstance(cash, bool):
        cash_value = float(cash)
        cash_src = "计划件 meta.cash"
    else:
        cash_value = CASH0
        cash_src = "account.py 常量（回落）"
    return (total_assets, cash_value, slot_limit,
            "总资产 %.2f（%s）｜现金 %.2f（%s）｜单席上限 %.2f（= 总资产 × %.0f%%）"
            % (total_assets, total_src, cash_value, cash_src, slot_limit,
               float(A.TARGET_POSITION_WEIGHT) * 100))


def _d4_empty_position_negative(agg: dict, slot_limit: float) -> tuple[bool, str]:
    """D4 真负例：给一个**空 positions** 的情景硬塞一条破单席 ⇒ 独立重算必须检出。

    🔴 2026-11-10（实测红灯）：本轮**不存在**空仓情景（分歧项只有 3 个，
       且 512800 是固定持有 ⇒ 任何组合至少留 1 只持仓）⇒ 旧实现「找既有空仓情景」
       找不到 ⇒ 负例**没能触发**（探针无牙）。
    ⇒ 修法：**自造一只** —— 深拷贝一个已有情景、`positions` 置空（**键存在但为空**）、
       label 追加「（合成空仓）」⇒ 塞进 `mutated["scenarios"]`；
       随后照旧给它塞一条凭空 `slot_breaches` ⇒ 独立重算（宇宙 = ∅）必然与产物
       对不上 ⇒ 检出 ⇒ 闸门会 FAIL。

    返回：(负例是否触发, 细节)。
    """
    if not agg:
        return False, "负例未能触发（聚合件为空）"
    mutated = copy.deepcopy(agg)
    empty_scen = next(
        (s for s in mutated.get("scenarios", [])
         if "positions" in s and not (s.get("positions") or [])), None)
    synth = False
    if empty_scen is None:
        src_scen = next((s for s in mutated.get("scenarios", [])), None)
        if src_scen is None:
            return False, "负例未能触发（聚合件里一个情景都没有）"
        empty_scen = copy.deepcopy(src_scen)
        empty_scen["positions"] = []
        empty_scen["label"] = str(empty_scen.get("label") or "") + "（合成空仓）"
        mutated.setdefault("scenarios", []).append(empty_scen)
        synth = True
    empty_scen["slot_breaches"] = [
        {"code": "999999", "name": "凭空破单席",
         "amount": 999999.0, "limit": slot_limit}]
    detected = False
    for s in mutated.get("scenarios", []):
        if "positions" not in s:
            continue
        claimed = {b["code"] for b in s.get("slot_breaches", [])}
        positions = s.get("positions") or []
        recomputed = {
            p["code"] for p in positions
            if float(p.get("target_shares") or 0) * float(p.get("price") or 0.0)
            > slot_limit + 0.005}
        if claimed != recomputed:
            detected = True
            break
    if not detected:
        return False, "改坏后独立重算**未**检出 ⇒ 闸门无牙"
    detail = "空仓情景里凭空塞的破单席被独立重算检出 ⇒ 闸门会 FAIL"
    if synth:
        detail += "｜本轮无空仓情景 ⇒ 负例**自造**了一只「" + str(empty_scen.get("label")) + "」"
    return True, detail


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


def divergence_codes(scenarios: list[dict]) -> list[str]:
    """从情景列表的 `choices` 键取**分歧码集合**（按首次出现顺序 ⇒ 产物里的顺序）。

    🔴 **不写死任何具体代码、不写死分歧项个数** ⇒ 分歧项换人时探针不用改。
    """
    codes: list[str] = []
    for item in scenarios or []:
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


#: 🔴 2026-11-13（探针真空假红 · P3）：金额比较容差（元）。
#:    **只**放**浮点表示误差**（1e-9 量级）⇒ 真实差异照样被拦下：
#:    权重差 0.001（≈ 30 元）≫ 1e-9 ⇒ 必被报出来。
#:    🔴 **不许**把它放大到能掩盖真实差异（也不许拿「展示用舍入值」当判据）。
P3_AMOUNT_TOL = 1e-9


def _p3_default_signature(payload: dict) -> dict[str, dict]:
    """默认（规则层）产物的每条动作 ⇒ `{代码: {股数, 变动, 动作, 目标金额}}`。"""
    out: dict[str, dict] = {}
    for row in payload.get("actions") or []:
        out[str(row.get("code"))] = {
            "target_shares": row.get("target_shares"),
            "delta_shares": row.get("delta_shares"),
            "action": row.get("action"),
            "target_amount": row.get("target_amount"),
        }
    return out


def _p3_aaa_signature(scenario: dict) -> dict[str, dict]:
    """「全 A」情景的每笔交易 ⇒ `{代码: {股数, 变动, 动作, 目标金额}}`（字段与默认侧同构）。"""
    out: dict[str, dict] = {}
    for row in scenario.get("trades") or []:
        out[str(row.get("code"))] = {
            "target_shares": row.get("target_shares"),
            "delta_shares": row.get("delta_shares"),
            "action": row.get("action"),
            "target_amount": row.get("target_amount"),
        }
    return out


def _p3_compare(default_sig: dict[str, dict], aaa_sig: dict[str, dict],
                total_assets: float) -> tuple[list[str], set[str]]:
    """默认（规则层） vs 「全 A」情景逐条比对 ⇒ `(差异描述, 共同代码集)`。

    🔴 2026-11-13（探针真空假红）**口径换版**：旧口径把
       「默认产物的**原始权重** `target_weight`」与「聚合件的 **4 位小数百分数**
       `target_weight_pct`（= `make_node_scenarios.py` 的 `round(w*100, 4)`，
       **展示用**的舍入值）」放在一起比（还各自 `round(..., 10)`）
       ⇒ 同一笔权重也会因**打印精度**不同而被判红
       （实测 512800：默认 0.0533333333 vs A/A/A 0.053333 ⇒ 假红）。
    ⇒ 正确口径：**按原始浮点值比较**，容差只放**浮点表示误差**（`P3_AMOUNT_TOL`）：
       · 股数 / 变动股数 / 动作 ⇒ **逐位相等**；
       · 目标**金额**（两边都存 `round(target_amount, 6)` ⇒ **逐位相同**的原始浮点值）
         ⇒ 差 > `P3_AMOUNT_TOL` 才算不一致（0.001 权重 ≈ 30 元 ⇒ 必被拦下）。
       ⇒ **不许**把「展示用舍入值」当判据（那会把真实差异一起放过）。
    """
    problems: list[str] = []
    common = set(default_sig) & set(aaa_sig)
    for code in sorted(common):
        left = default_sig[code]
        right = aaa_sig[code]
        for field in ("target_shares", "delta_shares", "action"):
            if left.get(field) != right.get(field):
                problems.append(
                    f"{code}: {field} 默认 {left.get(field)!r} vs A/A/A {right.get(field)!r}")
        left_amount = left.get("target_amount")
        right_amount = right.get("target_amount")
        if left_amount is None or right_amount is None:
            if left_amount != right_amount:
                problems.append(
                    f"{code}: 目标金额缺失 默认 {left_amount!r} vs A/A/A {right_amount!r}")
            continue
        diff = abs(float(left_amount) - float(right_amount))
        if diff > P3_AMOUNT_TOL:
            weight_pp = (diff / total_assets * 100.0) if total_assets else float("nan")
            problems.append(
                f"{code}: 目标金额 默认 {float(left_amount):.6f} 元 vs "
                f"A/A/A {float(right_amount):.6f} 元（差 {diff:.6f} 元 ≈ {weight_pp:.6f}pp 权重）")
    for code in sorted(set(aaa_sig) - set(default_sig)):
        problems.append(f"{code}: A/A/A 多出交易项")
    return problems, common


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="节点日分歧情景重算闸门")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
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
    #: 🔴 2026-11-10：账户口径**优先从计划件派生**（旧常量只作兜底）⇒ P7/P8/D3/D4
    #:    与产品（`reprice_node_day.py` 用计划件口径的总资产）同源。
    _total_assets, cash0, slot_limit, _basis_note = _account_basis(plan)
    print("账户口径：" + _basis_note)
    combos = [(str(s["label"]), {str(k): str(v) for k, v in s["choices"].items()})
              for s in src.get("scenarios", [])]
    #: 上游情景组合文件里的分歧码集合（按文件里的顺序 ⇒ 也是「全 A」组合标签的顺序）
    src_codes = divergence_codes(src.get("scenarios") or [])

    tmp_parent = run_root.parent
    if not str(tmp_parent).startswith("/mnt/data/"):
        tmp_parent = C.WORK_ROOT
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
        #: 分歧码集合 / 期望情景数：**从当前产物 payload 动态取**（N 个分歧项 ⇒ 2^N 情景）
        #: 🔴 不写死 8 / 16 / 3 / 4 ⇒ 分歧项换人时探针不用改，也不会与新产物错配。
        agg_codes = divergence_codes(agg.get("scenarios") or [])
        div_codes = agg_codes or src_codes
        expected_n = 2 ** len(div_codes)
        aaa_label = all_a_label(src_codes or div_codes)

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
        aaa = all_a_scenario(agg.get("scenarios", []), aaa_label)
        common: set[str] = set()
        if aaa is None:
            p3_problems.append(f"聚合件里找不到「全 A」情景（期望标签 {aaa_label}）")
        else:
            # 🔴 2026-11-13（探针真空假红）：**按原始浮点值比较**（见 `_p3_compare` 的口径）
            #   —— 旧口径拿「默认的原始权重」比「聚合件的 4 位小数百分数（展示值）」
            #   ⇒ 同一笔权重也因打印精度不同被判红（0.0533333333 vs 0.053333）。
            default_sig = _p3_default_signature(default_payload)
            aaa_sig = _p3_aaa_signature(aaa)
            # 默认产物里的「零变动」条目在聚合件里被剔除了 ⇒ 只比共同的（有变动的）条目
            p3_problems, common = _p3_compare(default_sig, aaa_sig, _total_assets)
        p3_ok = not p3_problems
        checker.add("P3", "情景 A/A/A 与默认（规则层）逐条一致（股数 / 目标金额 / 动作）", p3_ok,
                    "；".join(p3_problems) if p3_problems
                    else f"共同 {len(common)} 条交易项的股数/目标金额/动作全一致"
                         f"（目标金额 = 权重 × 总资产，容差 {P3_AMOUNT_TOL} 元）")

        # --- P4 聚合件结构完整 ---
        p4_problems = []
        scen = agg.get("scenarios", [])
        if not div_codes:
            p4_problems.append("推不出分歧码集合（无法确定期望情景数 2^N）")
        else:
            if len(scen) != expected_n:
                p4_problems.append(f"情景数 {len(scen)} ≠ 2^{len(div_codes)} = {expected_n}")
            if len(combos) != expected_n:
                p4_problems.append(f"来源情景数 {len(combos)} ≠ 2^{len(div_codes)} = {expected_n}")
            if agg_codes and src_codes and sorted(agg_codes) != sorted(src_codes):
                p4_problems.append(
                    f"聚合件的分歧码集合 {sorted(agg_codes)} ≠ 上游 {sorted(src_codes)}")
        if not agg.get("notes"):
            p4_problems.append("notes 为空")
        if not agg.get("disclaimer"):
            p4_problems.append("disclaimer 为空")
        for a in agg.get("assertions", []):
            if a.get("status") != "PASS":
                p4_problems.append(f"聚合件断言 {a.get('id')} 状态 {a.get('status')}")
        p4_ok = not p4_problems
        checker.add("P4", "聚合件结构完整（情景数 = 2^N / notes / disclaimer / 断言全 PASS）", p4_ok,
                    "；".join(p4_problems) if p4_problems
                    else f"情景 {len(scen)} 个（= 2^{len(div_codes)} = {expected_n}）、"
                         f"来源 {len(combos)} 个、notes {len(agg.get('notes', []))} 条、"
                         f"断言 {len(agg.get('assertions', []))} 条全 PASS")

        # 🔴 修（2026-11-09 独立审计实证）：**空情景集合会制造「空洞 PASS」** ——
        #    P5~P8/P10 都是「逐情景遍历 ⇒ 没找到违例 ⇒ PASS」的结构 ⇒ 情景为 0 条时
        #    它们会「零违例通过」，而 detail 还会谎报「全部情景手推全对」。
        #    （实测：上游生成器崩掉时聚合件为空 ⇒ P5 的 min() 直接抛 ValueError ⇒
        #    整份断言报告丢失、闸门只能报 rc=2，看不出真因。）
        #    ⇒ 修法：情景为空 ⇒ 这些断言**必须 FAIL**，并把「上游没产出」写进 detail。
        _scen_missing = ([] if scen else
                         ["聚合件里没有情景（上游生成器未产出 ⇒ 一条情景都没覆盖）"])

        # --- P5 现金非负 + feasible ---
        p5_problems = list(_scen_missing)
        for s in scen:
            if float(s.get("cash_end") or 0.0) < -0.005:
                p5_problems.append(f"{s.get('label')} 现金 {s.get('cash_end')}")
            if not s.get("feasible", True):
                p5_problems.append(f"{s.get('label')} 不可行")
        p5_ok = not p5_problems
        checker.add("P5", "全部情景期末现金均不为负且可行", p5_ok,
                    "；".join(p5_problems) if p5_problems
                    else f"最小期末现金 "
                         f"{min((float(s['cash_end']) for s in scen), default=0.0):.2f} 元"
                         f"（{len(scen)} 个情景）")

        # --- P6 整手 ---
        p6_problems = list(_scen_missing)
        for s in scen:
            for t in s.get("trades", []):
                if int(t.get("target_shares") or 0) % LOT != 0:
                    p6_problems.append(f"{s.get('label')}/{t.get('code')} {t.get('target_shares')}")
        p6_ok = not p6_problems
        checker.add("P6", f"所有落地股数都是整手（{LOT} 股）", p6_ok,
                    "；".join(p6_problems) if p6_problems
                    else f"共 {sum(len(s.get('trades', [])) for s in scen)} 条交易逐条校验通过")

        # --- P7 独立重算现金恒等式 ---
        p7_problems = list(_scen_missing)
        for s in scen:
            cash = float(s.get("cash_begin") or cash0)
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
                    else f"{len(scen)} 个情景手推全对")

        # --- P8 破单席标记完整性（独立重算）---
        p8_problems = list(_scen_missing)
        for s in scen:
            claimed = {b["code"] for b in s.get("slot_breaches", [])}
            #: 🔴 宇宙 = 聚合件的**落地持仓全集** `positions`（含「本轮不动」的仓位）；
            #:    用 `trades`（变动股数 ≠ 0）当宇宙 ⇒ 会漏掉「已经在仓、目标仍超单席上限」的席位
            #:    ⇒ 口径对不上（产品按全集判、探针按变动集判）⇒ 假红。
            #:    度量 = **落地金额**（落地股数 × 节点日价）—— **不信产品给的金额字段**，
            #:    自己用 `target_shares × price` 算 ⇒ 与 `reprice_node_day.py` 的
            #:    `slot_breach` 判据、`make_decision_scenarios.py` 的破单席清单同口径。
            #: 🔴 2026-11-10 修（实测红灯）：**「positions 键缺失」与「positions 为空」是两回事**。
            #:    实测：全 B 情景（`512010=B,512800=B,515450=B,515650=B`）是**合法的空仓**
            #:    （现金 29,845.35 元 = 99.97%，落地持仓 0 只）⇒ `positions` 键**存在**但**是空数组**。
            #:    旧实现用 `s.get("positions") or []` ⇒ 空数组被当成「键缺失」⇒ 误判 FAIL（假红）。
            #:    正确口径：**键缺失** = 产物结构不完整（⇒ FAIL）；
            #:    **键存在但为空** = 合法空仓 ⇒ 独立重算的宇宙是 ∅ ⇒ 破单席也必须为空（⇒ 照常校验）。
            if "positions" not in s:
                p8_problems.append(
                    f"{s.get('label')} 聚合件里**没有 positions 键**（落地持仓全集）"
                    "⇒ 产物结构不完整 ⇒ 无法独立重算破单席")
                continue
            positions = s.get("positions") or []
            recomputed = {p["code"] for p in positions
                          if float(p.get("target_shares") or 0) * float(p.get("price") or 0.0)
                          > slot_limit + 0.005}
            if claimed != recomputed:
                p8_problems.append(f"{s.get('label')} 重算 {recomputed} vs 产物 {claimed}")
        p8_ok = not p8_problems
        checker.add("P8", "独立重算：破单席标记完整（落地金额 > 单席上限 ⇔ 标记）", p8_ok,
                    "；".join(p8_problems) if p8_problems
                    else f"{len(scen)} 个情景的破单席标记与独立重算全一致")

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
        p10_problems = list(_scen_missing)
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
        # 🔴 负例必须用「当前产物里的**真实分歧码** + 非法字母」：
        #    写死的旧代码若已不在分歧表里，报错原因会从「选项字母非法」变成「代码不在计划件」，
        #    负例就名不副实（测的东西换了）⇒ 取当前产物里的第一个分歧码。
        d1_code = div_codes[0] if div_codes else ""
        d1_ok = False
        d1_detail = "推不出分歧码集合 ⇒ 负例无法构造"
        if d1_code:
            rc_d1, out_d1 = run([sys.executable, str(REPRICE), "--node-date", "2026-11-04",
                                 "--out-dir", str(tmp_root / "d1"), "--scenario", f"{d1_code}=X"],
                                REPO_ROOT)
            d1_ok = rc_d1 != 0 and ("X" in out_d1 and "非法" in out_d1)
            d1_detail = (f"退出码 {rc_d1}（规格 {d1_code}=X）" if d1_ok
                         else f"退出码 {rc_d1}，输出：{out_d1.strip()[-200:]}")
        checker.add("D1", "真负例：情景字母非法（X）⇒ 退出码 ≠ 0 且中文报错", d1_ok, d1_detail)

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
                cash = float(s.get("cash_begin") or cash0)
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

        # --- D4 空仓情景（positions = []）里塞一条破单席 ⇒ P8 必须 FAIL（证明闸门有牙）---
        #: 🔴 2026-11-10：P8 改成「positions 键缺失 ⇒ FAIL；键存在但为空 ⇒ 照常校验」之后，
        #:    **必须**补一条负例钉死「空仓情景不许白过」——
        #:    给一个**空 positions** 的情景硬塞一条 `slot_breaches` ⇒ 独立重算（宇宙 = ∅）
        #:    必然与产物对不上 ⇒ 探针必须 FAIL。
        d4_ok, d4_detail = _d4_empty_position_negative(agg, slot_limit)
        checker.add("D4",
                    "真负例：空仓情景（positions 为空）里塞破单席 ⇒ 独立重算必须 FAIL（证明闸门有牙）",
                    d4_ok, d4_detail)

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
