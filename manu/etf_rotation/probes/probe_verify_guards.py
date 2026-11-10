#!/usr/bin/env python
"""守卫自检探针：`verify_artifacts.py` 的两条新断言**必须真的会拦人**。

【为什么需要】
2026-11-04 夜 reviewer 指出：自检只查「冲突表是否为空」，
**不查表里的数字是否与 LLM 原文一致** ⇒ 一条「非空但方向相反」的**假冲突**
能一路 PASS 到交付（当轮 BLOCKING 事故的根因）。
为此给自检加了两条断言：

  A.「LLM 方向-权重语义自洽」—— **与解析机制无关**的纯语义守卫
     （方向 ∈ 不买/清仓/卖出/退出 ⇒ 目标权重必须 0%；方向 ∈ 买/加仓/增持 ⇒ 必须 > 0%）；
  B.「冲突表数字可追溯（回放 LLM 原文）」—— 用生产解析器重放原文，
     逐条比对 llm_target_pct / llm_direction / llm_who。

🔴 **只加断言是不够的**：断言写错了、或写死了「永远 PASS」，
那就等于没加。本探针用**故意做坏的交付件**去撞这两条断言，
要求它们**真的报 FAIL**；同时用原样交付件要求它们**真的报 PASS**。
⇒ 守卫本身也有回归测试。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_verify_guards.py \
        --run-root <落点根>/runs

【副作用】
- 只在 `--run-root` 同级的临时目录里写文件（默认 `<run-root>/_probe_guards`），
  **不碰正式产物**；结束即自动清理。
- 只读正式产物，不修改。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from pathlib import Path

#: 研究链根（探针目录不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ETF_ROOT = Path(__file__).resolve().parents[1]
if str(_ETF_ROOT) not in sys.path:
    sys.path.insert(0, str(_ETF_ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）
import verify_artifacts as VA  # noqa: E402  （方向词表 = 生产语义守卫的**单一事实源**）

PY_BIN = "/home/huangzhe/anaconda3/envs/ql/bin/python"
VERIFY = "manu/etf_rotation/verify_artifacts.py"
#: 需要拷进临时目录的输入（决策件算**外部输入**：LLM 产出，不是本工程算出来的）
NEEDED = (
    "etf_analysis_report.json", "etf_analysis_report.md",
    "rebalance_recommendation.json", "rebalance_recommendation.csv",
    "final_rebalance_plan.json", "final_rebalance_plan.md",
    "tradingagents_decision.md", "tradingagents_decision.json",
    # 🔴 2026-11-04 夜实测修（M-F）：必须把「上下文三件套」也拷进来 ⇒
    #    sha1 链**才可校验**。旧实现没拷 ⇒ 链不可校验 ⇒ 那条断言过去只能靠
    #    「无法校验也判 PASS」的空档才过（现已修成「无法校验 ⇒ FAIL」）。
    "ta_context_meta.json", "ta_research_context.md",
    "etf_rotation_backtest_v4_prefer1.json",
    # 🔴 2026-11-10：`verify_artifacts.py` 的账户断言改成读**交付件同目录**的
    #    账户事实源（`account_context.json` 优先、`account_snapshot.csv` 回落）
    #    ⇒ 临时目录里**缺**这两个件 ⇒ 那边会因事实源缺失而 fail-closed 判 FAIL
    #    ⇒ 探针的每一条用例都会跟着假红。
    #    ⇒ 这两个件**存在才拷**（旧账户口径的产物根可能没有）。
    "account_context.json", "account_snapshot.csv",
)
#: 两条新断言在自检输出里的名字
CHECK_SEMANTIC = "LLM 方向-权重语义自洽"
CHECK_TRACEABLE = "冲突表数字可追溯（回放 LLM 原文）"


def _run_verify(run_root: Path, project_dir: Path) -> tuple[int, dict[str, bool]]:
    """跑一次自检，返回 (退出码, {断言名: 是否 PASS})。"""
    proc = subprocess.run(
        [PY_BIN, VERIFY, "--run-root", str(run_root)],
        capture_output=True, text=True, cwd=str(project_dir),
    )
    verdicts: dict[str, bool] = {}
    lines = proc.stdout.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[PASS]"):
            verdicts[stripped[6:].strip()] = True
        elif stripped.startswith("[FAIL]"):
            verdicts[stripped[6:].strip()] = False
    return proc.returncode, verdicts


#: 语义守卫的方向词表：**直接取生产守卫的常量**（单一事实源）⇒
#:   探针与生产**不可能**再各自漂一套词表（2026-11-10：探针曾自己抄一份，
#:   覆盖不到生产新增的「清 / Sell」等词 ⇒ 漏检）。
#: ZERO    = 目标权重必须 = 0%；REDUCE = 目标权重必须 < 当前权重。
_ZERO_DIRECTIONS = tuple(sorted(VA.LLM_DIRECTION_ZERO))
_REDUCE_DIRECTIONS = tuple(sorted(VA.LLM_DIRECTION_REDUCE))


def _mutate_baseline(plan: dict) -> list[tuple[str, dict]]:
    """构造若干「故意做坏」的交付件变体。

    🔴 2026-11-09（Mode C v2 独立审计实证）修：**负例不许硬编码生产数据的偶然形态**。
       旧实现把变异目标**写死**：N1/N4 改代码 `563020`、N5 改数值 `25.0`
       ⇒ 2026-11-09 这一轮决策里 `563020` **不在冲突表**（它不在规则层动作里）、
         `512800` 的规则层权重**本来就是 25.0**
       ⇒ 变异**空转**（改了等于没改）⇒ 守卫根本没被触发
       ⇒ 三条负例全部「守卫生效」地 PASS（实测 N1/N4/N5 三红）。
       ⇒ 修法：**动态挑行**（挑「ZERO 方向 + 目标 0%」的行）+ **自带兜底夹具**，
         且外层会**断言变异确实改动了交付件**（改了等于没改 ⇒ 负例无牙 ⇒ 判 FAIL）。
    """
    variants: list[tuple[str, dict]] = []

    # N1：BLOCKING 类假冲突 —— 「不买/卖」却配 9.11% 目标权重（= 理由栏的浮盈被当成权重）
    bad = json.loads(json.dumps(plan))
    _hit = 0
    for conflict in bad.get("llm_conflicts") or []:
        if str(conflict.get("llm_direction") or "") not in _ZERO_DIRECTIONS:
            continue
        conflict["llm_target_pct"] = 9.11
        for view in conflict.get("all_divergent_views") or []:
            if str(view.get("direction") or "") in _ZERO_DIRECTIONS:
                view["target_pct"] = 9.11
        _hit += 1
    if not _hit and bad.get("llm_conflicts"):
        # 兜底夹具：自造一条「不买 + 9.11%」（**不依赖生产数据恰好有某个代码**）
        bad["llm_conflicts"][0]["llm_direction"] = "不买"
        bad["llm_conflicts"][0]["llm_target_pct"] = 9.11
        _hit = 1
    variants.append(("N1 「不买」配 9.11% 目标权重（假冲突）", bad))

    # N2：REDUCE 方向那一行的目标改成 **≥ 当前权重**（说减却不减 ⇒ 自相矛盾）。
    # 🔴 2026-11-05 01:3x（M-K 收尾）：旧注释写「「减」方向不受语义守卫约束」——
    #    那**不是设计，是 bug**：旧实现把「当前权重」伪造成**目标值**
    #    ⇒ 「减 ⇒ 目标 < 当前」这一档**恒不成立** ⇒ 语义守卫对「减」方向完全失效。
    # 🔴 2026-11-10（闸门假红修复）：旧实现把变异目标**写死** `512800` + `25.0`
    #    ⇒ 本轮 512800 的冲突方向是「持有」（NEUTRAL ⇒ **不设约束**）
    #    ⇒ 变异落在**无约束维度** ⇒ 语义守卫仍 PASS ⇒ 负例**空转**
    #    ⇒ 期望「语义守卫 FAIL」落空 ⇒ 闸门假红（实测 N2 红）。
    #    ⇒ 修法：**动态挑 REDUCE 方向的行**（挑不到就用**兜底夹具**自造一条）
    #      ⇒ REDUCE 这一档**永远有人看守**，不再依赖生产数据碰巧有某个代码/方向。
    bad = json.loads(json.dumps(plan))
    _hit = 0
    for conflict in bad.get("llm_conflicts") or []:
        if str(conflict.get("llm_direction") or "") not in _REDUCE_DIRECTIONS:
            continue
        _cur = float(conflict.get("llm_current_pct") or 0.0)
        conflict["llm_target_pct"] = _cur  # 「减」却不减到当前以下 ⇒ 自相矛盾
        for view in conflict.get("all_divergent_views") or []:
            if str(view.get("direction") or "") in _REDUCE_DIRECTIONS:
                view["target_pct"] = _cur
        _hit += 1
    if not _hit and bad.get("llm_conflicts"):
        # 兜底夹具：自造一条「减仓 + 目标 = 当前」⇒ 必被 REDUCE 档拦下
        # （**不依赖生产数据恰好有 REDUCE 行**，否则该档会悄悄失去看守）。
        bad["llm_conflicts"][0].update({
            "llm_direction": "减仓", "llm_current_pct": 20.0, "llm_target_pct": 20.0,
        })
        _hit = 1
    variants.append(("N2 REDUCE 方向 ⇒ 目标 ≥ 当前权重（说减却不减）", bad))

    # N3：凭空插入一条 LLM 原文里根本没有的冲突
    bad = json.loads(json.dumps(plan))
    bad["llm_conflicts"].append({
        "code": "510300", "name": "沪深300ETF", "rule_target_pct": 20.0,
        "llm_target_pct": 5.0, "llm_direction": "减", "llm_who": "bear",
        "llm_reason": "凭空捏造", "all_divergent_views": [],
    })
    variants.append(("N3 凭空插入原文里不存在的冲突", bad))

    # N4：方向被改写（「不买」→「买」，但目标权重还是 0% ⇒ 语义矛盾）
    bad = json.loads(json.dumps(plan))
    _hit = 0
    for conflict in bad.get("llm_conflicts") or []:
        if str(conflict.get("llm_direction") or "") not in _ZERO_DIRECTIONS:
            continue
        conflict["llm_direction"] = "买"
        conflict["llm_target_pct"] = 0.0
        _hit += 1
    if not _hit and bad.get("llm_conflicts"):
        bad["llm_conflicts"][0]["llm_direction"] = "买"
        bad["llm_conflicts"][0]["llm_target_pct"] = 0.0
        _hit = 1
    variants.append(("N4 方向「不买」→「买」（目标 0%）", bad))

    # N5：规则层权重被改（与 actions 不自洽）
    #    🔴 2026-11-09 修：旧实现**写死 25.0** ⇒ 本轮 512800 的规则层权重**本来就是 25.0**
    #    ⇒ 变异空转 ⇒ 可追溯守卫看不到任何改动 ⇒ 负例无牙。
    #    ⇒ 修法：**在现值上加 5pp**（保证与 actions 重建值**必然不同**）。
    bad = json.loads(json.dumps(plan))
    for conflict in bad.get("llm_conflicts") or []:
        try:
            _cur = float(conflict.get("rule_target_pct"))
        except (TypeError, ValueError):
            _cur = 0.0
        conflict["rule_target_pct"] = _cur + 5.0
    variants.append(("N5 规则层权重 +5pp（与 actions 不一致）", bad))

    # N6：无 LLM 意见（--skip-ta 形态）⇒ 两条新断言**按设计**跳过
    bad = json.loads(json.dumps(plan))
    bad["llm_conflicts"] = []
    bad["llm_targets"] = {}
    bad["tradingagents_view"] = ""
    bad["meta"]["source_decision"] = ""
    variants.append(("N6 无 LLM 意见（--skip-ta 形态）", bad))
    return variants


def main() -> int:
    parser = argparse.ArgumentParser(description="verify_artifacts 两条新断言的守卫自检")
    parser.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    parser.add_argument("--project-dir", default="/tmp/pycharm_project_658d3fc4")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（默认结束即删）")
    args = parser.parse_args()

    run_root = Path(args.run_root)
    project_dir = Path(args.project_dir)
    # 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出）：
    #    scratch 建在**官方产物目录里面** ⇒ 崩了留残留 + 密钥扫描会扫到 + 并发跑会撞。
    #    ⇒ 改到 run-root 的**同级**目录（仍在持久盘上，不写 /tmp）。
    scratch = run_root.parent / "_probe_guards"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    for name in NEEDED:
        source = run_root / name
        if source.exists():
            shutil.copy2(source, scratch / name)

    # 🔴 2026-11-05 01:3x（M-K 收尾）：缺 LLM 决策层 ⇒ 本探针**不适用**（不是「通过」）。
    #    旧实现直接 `read_text` ⇒ 抛 traceback（退出码 1）⇒ 被流水线当成「守卫失效」误杀。
    #    ⇒ 改成：**先判适用性**，不适用 ⇒ 退出码 **2**（与 0=通过 / 1=失败 区分开）。
    _missing_inputs = [
        _n for _n in ("final_rebalance_plan.json", "tradingagents_decision.md",
                       "tradingagents_decision.json")
        if not (scratch / _n).exists()
    ]
    if _missing_inputs:
        print("=" * 78)
        print("[N/A] 守卫自检探针**不适用**：本 run-root 缺 "
              f"{_missing_inputs} ⇒ 本轮没有 LLM 决策层，"
              "「方向-权重语义自洽 / 冲突表可追溯」这两条守卫**无从校验**。")
        print("      **不适用 ≠ 通过**（流水线按退出码 2 显式跳过并明说）。")
        print("=" * 78)
        shutil.rmtree(scratch, ignore_errors=True)
        return 2

    baseline_plan = json.loads((scratch / "final_rebalance_plan.json").read_text(encoding="utf-8"))
    plan_path = scratch / "final_rebalance_plan.json"

    # 🔴 2026-11-09 修（Mode C v2 独立审计后补）：**适用性判据只查「决策文件在不在」是漏判**。
    #    生产交付件是 `run_pipeline.sh --skip-ta` 跑出来的（`run_pipeline.sh:190` 把
    #    `DECISION_ARG` 指向哨兵 `.no_llm_decision.md`）⇒ 计划里
    #    `llm_final_decision.available=False`、`llm_targets={}`；
    #    而产物目录里**躺着上一轮遗留的决策 md/json** ⇒ 老判据以为「有 LLM 层」
    #    ⇒ 探针进 N9/N10 去取 `llm_targets["512800"]` ⇒ **KeyError 崩溃**（退出码 1）
    #    ⇒ 被流水线当成「守卫已失效」误杀。
    #    ⇒ 修法：适用性判据补「**计划里必须真嵌了 LLM 层**」⇒ 缺层 ⇒ 退出码 **2**
    #      （沿用本探针既有的「不适用 ≠ 通过」语义，**不是**记成 PASS）。
    _llm_final = baseline_plan.get("llm_final_decision") or {}
    _llm_targets = baseline_plan.get("llm_targets") or {}
    if not (_llm_final.get("available") is True or _llm_targets):
        print("=" * 78)
        print("[N/A] 守卫自检探针**不适用**：交付件里**没有 LLM 决策层**"
              f"（llm_final_decision.available={_llm_final.get('available')!r}、"
              f"llm_targets {len(_llm_targets)} 条）"
              "⇒ 「方向-权重语义自洽 / 冲突表可追溯」这两条守卫**无从校验**。")
        print("      **不适用 ≠ 通过**（流水线/闸门按退出码 2 显式跳过并明说）。")
        print("=" * 78)
        shutil.rmtree(scratch, ignore_errors=True)
        return 2

    def _pick_target_code(plan: dict) -> str | None:
        """🔴 2026-11-09 修：N9/N10 原先**写死** `llm_targets["512800"]`
        ⇒ 计划里没有 512800 的目标（LLM 裁决没提它）时直接 KeyError 崩溃。
        ⇒ 改成「从 llm_targets 里挑一个**真有非零目标**的代码」（优先 512800）。
        """
        _targets = plan.get("llm_targets") or {}
        for _code in ("512800", *sorted(_targets)):
            _row = _targets.get(_code)
            if isinstance(_row, dict) and (_row.get("target_pct") or 0):
                return _code
        return None
    lines: list[str] = []
    failures = 0

    def _record(ok: bool, tag: str, detail: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {tag}")
        lines.append(f"        {detail}")

    # ---- P0 阳性对照：原样交付件 ⇒ 两条新断言都必须 PASS，且整体退出码 0 ----
    exit_code, verdicts = _run_verify(scratch, project_dir)
    ok = (
        exit_code == 0
        and verdicts.get(CHECK_SEMANTIC) is True
        and verdicts.get(CHECK_TRACEABLE) is True
    )
    _record(ok, "P0 阳性对照：原样交付件 ⇒ 两条新断言都 PASS、退出码 0",
            f"exit={exit_code}｜语义守卫={verdicts.get(CHECK_SEMANTIC)}｜可追溯守卫={verdicts.get(CHECK_TRACEABLE)}")

    # ---- P0b 基线前置：冲突行必须带 llm_current_pct ----
    # 🔴 2026-11-05 01:4x（reviewer 指出）：N2 的语义断言依赖「当前权重」；
    #    若基线是**旧产物**（没有 llm_current_pct）⇒ 变异后语义守卫仍 PASS
    #    ⇒ 探针会误报「守卫已失效」⇒ 先把这条误诊挡在前面。
    _base_conflicts = baseline_plan.get("llm_conflicts") or []
    _n_with_cur = sum(
        1 for c in _base_conflicts
        if isinstance(c, dict) and c.get("llm_current_pct") is not None
    )
    _record(bool(_base_conflicts) and _n_with_cur == len(_base_conflicts),
            "P0b 基线前置：每条冲突行都必须带 llm_current_pct（否则 N2 的语义断言无从生效）",
            f"冲突 {len(_base_conflicts)} 条｜带 llm_current_pct 的 {_n_with_cur} 条")

    # ---- N1~N6：故意做坏 ⇒ 对应断言必须 FAIL ----
    # 期望**哪些断言要报 FAIL**（语义守卫, 可追溯守卫, 是否校验全局退出码）
    # 🔴 2026-11-10：加第三维「是否校验 exit」——
    #    `:305`（旧行号）把两条守卫与**全局**退出码耦合 ⇒ **N6 永远不可能 PASS**：
    #    N6 期望两条守卫都 PASS（--skip-ta 形态 ⇒ 按设计跳过），
    #    但只要基线报告里**另一条无关断言**（实测 = 持仓诊断）FAIL，
    #    整体退出码就仍是 1 ⇒ N6 被那条**与本用例无关**的红灯连坐。
    #    ⇒ 只对 N6 关掉 exit 校验；**期望某条守卫 FAIL 时仍必须 exit ≠ 0**
    #      （那半边保留牙 ⇒ 负例不许空转）。
    expectations = {
        "N1": (True, True, True),
        "N2": (True, True, True),
        "N3": (False, True, True),
        "N4": (True, True, True),
        "N5": (False, True, True),
        # --skip-ta 形态 ⇒ 两条断言按设计都 PASS ⇒ **不校验全局退出码**
        "N6": (False, False, False),
    }
    _base_json = json.dumps(baseline_plan, ensure_ascii=False, sort_keys=True)
    for tag, variant in _mutate_baseline(baseline_plan):
        key = tag.split(" ", 1)[0]
        # 🔴 2026-11-09：负例必须**真的改坏了交付件**；改了等于没改 ⇒ 负例**无牙**
        #    ⇒ 判 FAIL（而不是「守卫生效 ⇒ 探针坏了」这种误报）。
        mutated = json.dumps(variant, ensure_ascii=False, sort_keys=True) != _base_json
        plan_path.write_text(json.dumps(variant, ensure_ascii=False, indent=2), encoding="utf-8")
        exit_code, verdicts = _run_verify(scratch, project_dir)
        want_semantic, want_traceable, check_exit = expectations[key]
        got_semantic = verdicts.get(CHECK_SEMANTIC)
        got_traceable = verdicts.get(CHECK_TRACEABLE)
        ok = (
            mutated
            and got_semantic is (not want_semantic)
            and got_traceable is (not want_traceable)
            and (not check_exit or (exit_code != 0) is (want_semantic or want_traceable))
        )
        _record(ok, f"{tag} ⇒ 期望 语义守卫{'FAIL' if want_semantic else 'PASS'}"
                    f" / 可追溯守卫{'FAIL' if want_traceable else 'PASS'}"
                    + ("" if check_exit else "（**不校验全局退出码**：--skip-ta 形态下"
                       "全局退出码受**无关断言**连坐）"),
                f"exit={exit_code}｜语义守卫={got_semantic}｜可追溯守卫={got_traceable}"
                f"｜变异{'已生效' if mutated else '**未生效（负例无牙）**'}")

    # ---- G1 守卫词表必须**覆盖**解析器能产出的全部方向词（从正则里抽，不靠手抄）----
    # 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出）：
    #    旧守卫只有 ZERO/POS 两档 ⇒ 解析器能产出的另外 **18 个**方向词（含全部英文）
    #    **完全不受约束** ⇒ 「Sell 配 26.96%」实测 PASS（异常 0 条）。
    #    ⇒ 守卫改成五档 + 「未分类即 FAIL」；本断言保证**以后再给解析器加词也不会漏**。
    import re as _re
    sys.path.insert(0, str(project_dir / "manu" / "etf_rotation"))
    import final_rebalance_plan as _frp
    import verify_artifacts as _va

    def _parser_direction_words() -> list[str]:
        """从解析器 `_LLM_DIR_CELL` 正则的备选串里抽全部方向词（不手抄 ⇒ 不会与代码脱节）。"""
        _g = _re.search(r"\(([^()]*)\)", _frp._LLM_DIR_CELL.pattern, _re.S)
        if not _g:
            return []
        return [w for w in _g.group(1).split("|") if w]

    _words = _parser_direction_words()
    _missing = [w for w in _words if w not in _va.LLM_DIRECTION_ALL]
    ok = len(_words) >= 25 and not _missing
    _record(ok, "G1 守卫词表 ⊇ 解析器可产出的全部方向词（漏一个 ⇒ 那个方向的假冲突能 PASS）",
            f"解析器方向词 {len(_words)} 个｜守卫已分类 {len(_va.LLM_DIRECTION_ALL)} 个"
            f"｜未分类={_missing or '无'}（期望 ≥25 且未分类为空）")

    # ---- N7 决策 json **损坏** ⇒ 不许冒充「--skip-ta 无决策」 ----
    dec_json = scratch / "tradingagents_decision.json"
    dec_backup = dec_json.read_text(encoding="utf-8")
    dec_json.write_text(dec_backup[: len(dec_backup) // 2], encoding="utf-8")
    exit_code, verdicts = _run_verify(scratch, project_dir)
    dec_ok = verdicts.get("TradingAgents 决策状态") is False and exit_code != 0
    _record(dec_ok, "N7 决策 json 截断（损坏）⇒ 「TradingAgents 决策状态」必须 FAIL",
            f"exit={exit_code}｜决策状态判定={verdicts.get('TradingAgents 决策状态')}"
            "（期望 False：损坏 ≠ 不存在）")
    dec_json.write_text(dec_backup, encoding="utf-8")

    # ---- N8 上下文三件套缺失 ⇒ sha1 链**无法校验** ⇒ 必须 FAIL（不许报「新鲜」）----
    meta_path = scratch / "ta_context_meta.json"
    meta_backup = meta_path.read_text(encoding="utf-8")
    meta_path.unlink()
    exit_code, verdicts = _run_verify(scratch, project_dir)
    chain_ok = verdicts.get("sha1 链新鲜（报告→context→决策）") is False and exit_code != 0
    _record(chain_ok, "N8 删掉 ta_context_meta.json ⇒ sha1 链无法校验 ⇒ 必须 FAIL",
            f"exit={exit_code}｜sha1 链判定={verdicts.get('sha1 链新鲜（报告→context→决策）')}"
            "（期望 False：不确定 ≠ 确定新鲜）")
    meta_path.write_text(meta_backup, encoding="utf-8")

    # ---- N9/N10 英文方向词 / 未分类方向词 ⇒ 语义守卫必须 FAIL ----
    # 🔴 2026-11-09 修：取码**动态化**（原先写死 `llm_targets["512800"]` ⇒ 计划里没这个
    #    代码就 KeyError 崩溃；现在「从 llm_targets 里挑一个真有非零目标的代码」）。
    #    若**一个都没有** ⇒ 无从构造变异 ⇒ 显式判红并**整块跳过** N9/N10
    #    （不许拿「未变异的计划」去跑负例 ⇒ 那会把负例变成假阳性）。
    _probe_code = _pick_target_code(baseline_plan)
    if _probe_code is None:
        _record(False, "N9/N10 前置：llm_targets 里必须有带非零 target_pct 的代码（否则无从构造变异）",
                f"llm_targets {len(baseline_plan.get('llm_targets') or {})} 条"
                "｜期望 ≥1 条带非零 target_pct")
        _record(False, "N9 英文方向词 Sell 配 26.96% ⇒ 语义守卫必须 FAIL（旧实现 PASS ⇒ 漏检）",
                "**未执行**：前置不满足（无可变异的 llm_targets 条目）⇒ 不得记 PASS")
        _record(False, "N10 未分类方向词 ⇒ 语义守卫必须 FAIL（无法校验 ≠ 可以放过）",
                "**未执行**：前置不满足（无可变异的 llm_targets 条目）⇒ 不得记 PASS")
    else:
        # ---- N9 英文方向词（Sell）配非零目标 ⇒ 语义守卫必须 FAIL ----
        bad_en = json.loads(json.dumps(baseline_plan))
        _v = bad_en["llm_targets"][_probe_code]
        _v["direction"] = "Sell"
        _v["target_pct"] = 26.96
        plan_path.write_text(json.dumps(bad_en, ensure_ascii=False, indent=2), encoding="utf-8")
        exit_code, verdicts = _run_verify(scratch, project_dir)
        en_ok = verdicts.get(CHECK_SEMANTIC) is False and exit_code != 0
        _record(en_ok, f"N9 英文方向词 Sell 配 26.96% ⇒ 语义守卫必须 FAIL（旧实现 PASS ⇒ 漏检）"
                      f"（变异代码 {_probe_code}）",
                f"exit={exit_code}｜语义守卫={verdicts.get(CHECK_SEMANTIC)}"
                "（期望 False：Sell 属 ZERO ⇒ 目标必须 0%）")

        # ---- N10 未分类方向词 ⇒ 语义守卫必须 FAIL（不许静默放过）----
        bad_unk = json.loads(json.dumps(baseline_plan))
        bad_unk["llm_targets"][_probe_code]["direction"] = "减持到三成"
        plan_path.write_text(json.dumps(bad_unk, ensure_ascii=False, indent=2), encoding="utf-8")
        exit_code, verdicts = _run_verify(scratch, project_dir)
        unk_ok = verdicts.get(CHECK_SEMANTIC) is False and exit_code != 0
        _record(unk_ok, f"N10 未分类方向词 ⇒ 语义守卫必须 FAIL（无法校验 ≠ 可以放过）（变异代码 {_probe_code}）",
                f"exit={exit_code}｜语义守卫={verdicts.get(CHECK_SEMANTIC)}（期望 False）")

    # ---- N11/N12：F4（M-G）**成对对照实验**（守卫是安全件 ⇒ 两头都要测）----
    # 🔴 2026-11-04 夜实测修（M-G / F4，专题档 §21.4）：
    #    第一环改按「报告**内容指纹**」判（报告 JSON 去掉易变 meta 后的规范化 sha1）。
    #    ⇒ 必须同时证明两件事：
    #      ① **仅元数据变化**（模拟报告重生成：md 的「生成时间」行变 + json 的易变 meta 变）
    #         ⇒ 链**仍新鲜**（旧实现会误报 stale ⇒ 那正是 F4 要消灭的**假阳性**）；
    #      ② **实质变化**（信号日 asof 改了）⇒ 链**必须 stale**（守卫**不许被改松**）。
    #    ⇒ 只测①不测② = 把守卫改松了；只测②不测① = 白改（F4 的价值不在②）。
    _CHECK_CHAIN = "sha1 链新鲜（报告→context→决策）"
    report_json_path = scratch / "etf_analysis_report.json"
    report_md_path = scratch / "etf_analysis_report.md"
    report_json_backup = report_json_path.read_text(encoding="utf-8")
    report_md_backup = report_md_path.read_text(encoding="utf-8")
    base_report = json.loads(report_json_backup)
    # 🔴 N9/N10 把 plan 改坏了且**没还原** ⇒ 这里必须先还原成基线，
    #    否则 N11 的「exit==0」会被**上一条用例的残留**顶掉（实测过一次：链判定 True 但 exit=1）。
    plan_path.write_text(json.dumps(baseline_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # ① 仅元数据变化：md 的「生成时间/执行机/Python 版本」行换新 + json 的易变 meta 换新
    meta_only = json.loads(json.dumps(base_report))
    meta_only["meta"]["generated_at"] = "2099-01-01T00:00:00+08:00"
    meta_only["meta"]["host"] = "another-host"
    meta_only["meta"]["script"] = "/somewhere/else/analyze.py"
    meta_only["meta"]["python_version"] = "3.99.0"
    meta_only["meta"]["inputs"] = {"whatever": {"mtime_local": "2099-01-01"}}
    report_json_path.write_text(json.dumps(meta_only, ensure_ascii=False), encoding="utf-8")
    report_md_path.write_text(
        report_md_backup.replace("2026-11-04T20:55:30+08:00", "2099-01-01T00:00:00+08:00")
        .replace("执行机 test-Super-Server", "执行机 another-host")
        .replace("Python 3.12.16", "Python 3.99.0"),
        encoding="utf-8")
    exit_code, verdicts = _run_verify(scratch, project_dir)
    md_only_ok = verdicts.get(_CHECK_CHAIN) is True and exit_code == 0
    _record(md_only_ok,
            "N11 仅元数据变化（模拟报告重生成：md 生成时间行 + json 易变 meta）⇒ 链**仍新鲜**"
            "（F4 的假阳性已消除）",
            f"exit={exit_code}｜sha1 链判定={verdicts.get(_CHECK_CHAIN)}"
            "（期望 True：元数据进不了 LLM 上下文 ⇒ 旧决策依然成立；"
            "旧实现按 raw md sha1 判 ⇒ 这里会误报 stale）")

    # ② 实质变化：信号日改了 ⇒ 内容指纹必须变 ⇒ 链必须 stale
    plan_path.write_text(json.dumps(baseline_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    substantive = json.loads(json.dumps(base_report))
    substantive["meta"]["asof"] = "2026-11-04"
    report_json_path.write_text(json.dumps(substantive, ensure_ascii=False), encoding="utf-8")
    exit_code, verdicts = _run_verify(scratch, project_dir)
    sub_ok = verdicts.get(_CHECK_CHAIN) is False and exit_code != 0
    _record(sub_ok,
            "N12 实质变化（信号日 asof 改了）⇒ 链**必须 stale**（F4 不许把守卫改松）",
            f"exit={exit_code}｜sha1 链判定={verdicts.get(_CHECK_CHAIN)}"
            "（期望 False：内容变了 ⇒ 旧决策不适用）")
    report_json_path.write_text(report_json_backup, encoding="utf-8")
    report_md_path.write_text(report_md_backup, encoding="utf-8")

    # ---- 还原正式产物，结束时清理临时目录 ----
    plan_path.write_text(json.dumps(baseline_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)

    text = "\n".join(lines)
    print("=" * 78)
    print("verify_artifacts 守卫自检（断言必须真的会拦人）")
    print(f"基线：{run_root}｜临时目录：{scratch}{'（已保留）' if args.keep else '（已清理）'}")
    print("-" * 78)
    print(text)
    print("-" * 78)
    print(f"合计 {len(lines) // 2} 项：PASS {len(lines) // 2 - failures} / FAIL {failures}")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
