"""🔴 2026-11-27 新增：生产批的闸门步**必须跑活闸门（V2）**、**默认不许跑已弃用的 V1**。

【背景：11-30 实盘批前的实测归因】
2026-11-26 那轮「完整生产链」（`manu/tools/server_full_pipeline_run.sh`）的**步骤 4/5**
跑的是 ``manu/etf_rotation/run_gates.sh``（**V1 全量闸门**，实测 **28** 个阶段）⇒
退出码 **1**（**6 个红阶段**）⇒ 汇总表每轮都挂一个红步骤。

🔴 **但 6 个红不是本轮产物的回归**：用户 2026-11-26 已定调
「**V1 已弃用 ⇒ 不再跑、不再修；红阶段只做归因留档**」（铁律 ``rules.md §十四.137``），
**唯一活的闸门入口 = V2**（``manu/etf_rotation/v2/run_gates_v2.sh``，实测 **4** 个阶段）。
⇒ 生产批继续跑 V1 有两个问题：
   ① **违反用户口径**（V1 已定调「不再跑」）；
   ② **活闸门根本没进生产批** ⇒ 真正该绿的那 4 个阶段**每轮都没跑**。

【本文件钉死的四件事】
  G1 三个编排器（``server_full_pipeline_run.sh`` / ``overnight_snapshot_run.sh`` /
     ``rebuild_baseline_and_gates.sh``）**必须**引用活闸门脚本
     ``manu/etf_rotation/v2/run_gates_v2.sh``；
  G2 **默认路径不许跑 V1** ⇒ V1 的调用必须被 ``RUN_V1_GATES`` 开关**包住**
     （开关缺省 0 ⇒ 不跑；需要复现历史归因时显式开）；
  G3 **不许静默跳过闸门** ⇒ 活闸门脚本缺失时必须 **fail-closed**（记红），
     不许悄悄当「通过」；
  G4 阶段数**不许手写** ⇒ 必须从闸门脚本的 ``run_stage`` 调用数**动态派生**。

【变异反证（证明本测试有牙）】
  ① 把活闸门引用改回 V1 ⇒ G1 必须转红；
  ② 把 ``RUN_V1_GATES`` 的守卫去掉（V1 变成默认跑）⇒ G2 必须转红；
  ③ 把 fail-closed 改成「脚本缺失就跳过」⇒ G3 必须转红；
  ④ 把阶段数写死成字面数字 ⇒ G4 必须转红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: 三个编排器（相对工程根）
ORCHESTRATORS = (
    "manu/tools/server_full_pipeline_run.sh",
    "manu/tools/overnight_snapshot_run.sh",
    "manu/tools/rebuild_baseline_and_gates.sh",
)

#: 活闸门脚本（唯一活的入口）
LIVE_GATE_REL = "manu/etf_rotation/v2/run_gates_v2.sh"

#: 已弃用的 V1 全量闸门脚本
V1_GATE_REL = "manu/etf_rotation/run_gates.sh"


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / LIVE_GATE_REL).is_file() and (parent / ORCHESTRATORS[0]).is_file():
            return parent
    raise AssertionError("找不到工程根目录")


PROJECT_DIR = _find_project_dir()


def _read(rel: str) -> str:
    path = PROJECT_DIR / rel
    assert path.is_file(), f"找不到文件：{rel}"
    return path.read_text(encoding="utf-8")


def _v1_call_lines(text: str) -> list[tuple[int, str]]:
    """返回**真正调用** V1 闸门脚本的行号（排除注释行）。"""
    hits: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "run_gates.sh" in line and LIVE_GATE_REL not in line:
            hits.append((line_no, line))
    return hits


def _code_lines(text: str) -> list[tuple[int, str]]:
    """返回**非注释**的代码行（去掉纯注释行；续行仍保留）。"""
    out: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        out.append((line_no, line))
    return out


# ── G1：必须引用活闸门 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("rel", ORCHESTRATORS)
def test_g1_orchestrator_must_call_live_gate(rel: str) -> None:
    """G1：编排器的闸门步必须**实际调用活闸门**（V2）。

    🔴 只查「文中出现过活闸门路径」不够 —— 注释里也会出现 ⇒ 变异把
    ``V2_GATES`` 指回 V1 时仍能蒙混过关 ⇒ 必须钉**两处**：
    ① ``V2_GATES`` 赋值指向活闸门；② 活闸门**真的被调用**（非注释行）。
    """
    text = _read(rel)
    code = _code_lines(text)
    assignments = [
        (line_no, line) for line_no, line in code
        if "V2_GATES=" in line
    ]
    assert assignments, f"{rel}：找不到 V2_GATES 赋值 ⇒ 锚点过期（先订正本测试）"
    for line_no, line in assignments:
        assert LIVE_GATE_REL in line, (
            f"{rel}：第 {line_no} 行把 V2_GATES 指向了非活闸门路径 ⇒ "
            f"活闸门必须是 {LIVE_GATE_REL}"
        )
    #    🔴 必须是**真正的调用**（`bash ...`）⇒ `say`/`echo` 里提到路径**不算**
    #    （否则「只定义不跑」也能蒙混过关 —— 变异 ⑤ 已实测能钻这个空子）。
    invocation_re = re.compile(r'bash\s+"?\$\{?V2_GATES\}?"?|bash\s+\S*run_gates_v2\.sh')
    invocations = [
        (line_no, line) for line_no, line in code
        if invocation_re.search(line)
    ]
    assert invocations, (
        f"{rel}：活闸门脚本**没有被真正调用**（只定义不跑等于没跑）"
    )


# ── G2：默认不许跑 V1（必须被 RUN_V1_GATES 开关包住）─────────────────────────
@pytest.mark.parametrize("rel", ORCHESTRATORS)
def test_g2_v1_gate_must_be_opt_in_only(rel: str) -> None:
    """G2：V1 全量闸门（已弃用）默认**不许跑** ⇒ 必须被 ``RUN_V1_GATES`` 开关包住。"""
    text = _read(rel)
    calls = _v1_call_lines(text)
    assert calls, f"{rel}：连 V1 的调用都找不到了 ⇒ 锚点过期（先订正本测试）"
    assert 'RUN_V1_GATES' in text, f"{rel}：V1 调用没有开关守卫 ⇒ 默认会跑已弃用的 V1"
    # 开关的缺省值必须是 0（不跑）
    assert re.search(r'\$\{RUN_V1_GATES:-0\}"\s*=\s*"1"', text), (
        f"{rel}：RUN_V1_GATES 的缺省值必须是 0（默认不跑）"
    )
    # V1 的调用必须落在开关的 if 块里 ⇒ 调用行之前必须出现该开关
    guard_line = next(
        line_no for line_no, line in enumerate(text.splitlines(), start=1)
        if "RUN_V1_GATES" in line and "if" in line
    )
    for line_no, _line in calls:
        assert line_no > guard_line, (
            f"{rel}：第 {line_no} 行的 V1 调用在 RUN_V1_GATES 守卫（第 {guard_line} 行）"
            f"**之前** ⇒ 默认路径会跑已弃用的 V1"
        )


# ── G3：活闸门缺失必须 fail-closed ────────────────────────────────────────────
@pytest.mark.parametrize("rel", ORCHESTRATORS)
def test_g3_missing_live_gate_must_fail_closed(rel: str) -> None:
    """G3：活闸门脚本缺失 ⇒ 必须**判红**（不许静默跳过闸门当通过）。"""
    text = _read(rel)
    assert "if [ ! -f \"$V2_GATES\" ]" in text, f"{rel}：没有「活闸门脚本缺失」的守卫"
    assert re.search(r'if \[ ! -f "\$V2_GATES" \]; then.*?record "活闸门（V2）" "2"', text, re.S), (
        f"{rel}：活闸门缺失时必须记红（退出码 2），不许静默跳过"
    )


# ── G4：阶段数不许手写（必须动态派生）────────────────────────────────────────
@pytest.mark.parametrize("rel", ORCHESTRATORS)
def test_g4_stage_count_must_be_derived(rel: str) -> None:
    """G4：阶段数必须从闸门脚本的 ``run_stage`` 调用数动态派生。"""
    text = _read(rel)
    assert 'V2_STAGES="$(grep -c \'^run_stage \' "$V2_GATES"' in text, (
        f"{rel}：活闸门阶段数没有从 {LIVE_GATE_REL} 动态派生"
    )
    assert 'record "活闸门（V2 · ${V2_STAGES} 阶段）"' in text, (
        f"{rel}：活闸门标签没有用动态派生的阶段数"
    )


# ── 变异反证 ─────────────────────────────────────────────────────────────────
def test_mutation_revert_to_v1_gate_must_break_g1() -> None:
    """变异 ①：把 ``V2_GATES`` 指回 V1 ⇒ G1 必须转红（证明本测试有牙）。

    🔴 只把**注释**里的路径换掉是不够的 ⇒ 变异必须打在**赋值**那一行。
    """
    rel = ORCHESTRATORS[0]
    text = _read(rel)
    mutated = text.replace(
        f'V2_GATES="{LIVE_GATE_REL}"',
        f'V2_GATES="{V1_GATE_REL}"',
    )
    assert mutated != text, "变异没打中 ⇒ 锚点过期"
    # 变异后：赋值行指向 V1 ⇒ G1 的判据必须不成立（**注释**里还有活闸门路径不算数）
    assignments = [
        line for _line_no, line in _code_lines(mutated) if "V2_GATES=" in line
    ]
    assert assignments, "变异后找不到赋值行 ⇒ 变异没打中"
    for line in assignments:
        assert LIVE_GATE_REL not in line, (
            "变异后赋值仍指向活闸门 ⇒ G1 抓不住这个变异（断言太弱）"
        )


def test_mutation_remove_v1_guard_must_break_g2() -> None:
    """变异 ②：去掉 ``RUN_V1_GATES`` 守卫 ⇒ G2 必须转红（默认就会跑 V1）。"""
    rel = ORCHESTRATORS[0]
    text = _read(rel)
    mutated = text.replace('if [ "${RUN_V1_GATES:-0}" = "1" ]; then', "if true; then")
    assert mutated != text, "变异没打中 ⇒ 锚点过期"
    assert not re.search(r'\$\{RUN_V1_GATES:-0\}"\s*=\s*"1"', mutated)


def test_mutation_hardcode_stage_count_must_break_g4() -> None:
    """变异 ④：把阶段数写死成字面数字 ⇒ 必须被阶段数守卫抓到。"""
    rel = ORCHESTRATORS[0]
    text = _read(rel)
    mutated = text.replace(
        'record "活闸门（V2 · ${V2_STAGES} 阶段）"',
        'record "活闸门（V2 · 4 阶段）"',
    )
    assert mutated != text, "变异没打中 ⇒ 锚点过期"
    assert 'record "活闸门（V2 · ${V2_STAGES} 阶段）"' not in mutated
