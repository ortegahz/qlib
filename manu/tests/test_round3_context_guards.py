r"""🔴 2026-11-11（审计 **P1-2** / **P2-6**）：**两处 fail-open 护栏**的钉死测试。

① **P1-2 · 上下文等价校验 fail-open**（`daily/agents_rebalance.py::_carry_context_trio`）
   旧写法：``if Path(cur_context).is_file() and not _contexts_equivalent(...)``
   ⇒ **本轮快照的 context 文件不存在**时条件直接为假 ⇒ 跳过「本轮数据没变」的证明
   ⇒ 在**没有证明**的情况下把来源快照的 context 搬过来（**fail-open**）
   ⇒ 来源决策件可能是**别的数据**的 ⇒ sha1 链看似接上、实则结论过期。
   ⇒ 现在 **fail-closed**：本轮 context 缺失 ⇒ **不搬**（链保持断口 ⇒ 闸门红 ⇒ 需人工介入）。

② **P2-6 · `run_gates.sh` 的 config 读取在 ``cd "$REPO_ROOT"`` 之前**
   ``sys.path.insert(0,'manu/etf_rotation')`` 是**相对仓库根**的相对路径
   ⇒ CWD 不在仓库根（例如从 ``/tmp`` 执行本脚本）⇒ ``import config`` 失败
   ⇒ 旧写法 ``2>/dev/null || echo 100`` 把失败**静默兜底**成 100
   ⇒ 候选池口径漂移 ⇒ repro 段「重跑件 vs 基线」必红（**假红**，不是真缺陷）。
   ⇒ 现在：读取位置移到 ``cd "$REPO_ROOT"`` 之后；且**兜底不许静默**（WARNING 到 stderr + 日志）。

【变异反证】（审计要求「撤掉这层保护测试就转红」）
   · 把 P1-2 改回 ``if Path(cur_context).is_file() and not _contexts_equivalent(...)``
     ⇒ `test_carry_context_trio_refuses_when_current_context_missing` **转红**。
   · 把 P2-6 的读取移回 ``cd "$REPO_ROOT"`` 之前
     ⇒ `test_run_gates_reads_analyze_top_n_after_cd_repo_root` **转红**。
   · 把 P2-6 的兜底改回 ``2>/dev/null || echo 100``（静默）
     ⇒ `test_run_gates_analyze_top_n_fallback_is_not_silent` **转红**。
   （`test_run_gates_config_read_needs_repo_root_cwd` 是**行为级**旁证：
     它证明那条读取**必须**在仓库根 CWD 下才成立 ⇒ 位置是承重的。）

本文件自带夹具，不依赖服务器产物、不联网、不调 LLM。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

#: 仓库根（`manu/etf_rotation` 的上两级）
_REPO_ROOT = _ETF_DIR.parents[1]
_GATES_SH = _ETF_DIR / "run_gates.sh"
_ARB_PATH = _ETF_DIR / "daily" / "agents_rebalance.py"


def _load_agents_rebalance():
    daily_dir = _ETF_DIR / "daily"
    if str(daily_dir) not in sys.path:
        sys.path.insert(0, str(daily_dir))
    spec = importlib.util.spec_from_file_location("_arb_guards_under_test", daily_dir / "agents_rebalance.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha1(path: pathlib.Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _decision_json(snap: pathlib.Path, context_sha1: str) -> pathlib.Path:
    p = snap / "tradingagents_decision.json"
    p.write_text(
        json.dumps({"status": "ok", "meta": {"context_sha1": context_sha1}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


# ───────────────────── ① P1-2 · 上下文等价校验 fail-closed ─────────────────────


def test_carry_context_trio_refuses_when_current_context_missing(tmp_path, monkeypatch):
    """🔴 变异反证：**本轮 context 缺失 ⇒ 绝不搬来源 context**（fail-closed）。

    🔴 关键：把 ``_contexts_equivalent`` 强制成「恒真」⇒ 唯一的拦路虎只剩
       **「本轮 context 缺失」这一条守卫** ⇒ 撤掉守卫就必然变红（测试有牙）。
    """
    arb = _load_agents_rebalance()
    monkeypatch.setattr(arb, "_contexts_equivalent", lambda a, b: True)

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "ta_research_context.md").write_text("来源上下文（旧数据）\n", encoding="utf-8")
    dec = _decision_json(src, _sha1(src / "ta_research_context.md"))
    # 🔴 本轮快照**没有** ta_research_context.md（build_ta_context 失败/未产出）
    assert not (dst / "ta_research_context.md").exists()

    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)

    assert ok is False, f"本轮 context 缺失却仍然搬了来源 context：{why}"
    assert not (dst / "ta_research_context.md").exists(), "本轮 context 缺失时来源 context 被搬进来了"
    # 理由必须**可读**（会进 daily 链的 notes ⇒ ⚠️ WARNING）
    assert "缺" in why, why
    assert "无法证明" in why, why
    assert "fail-closed" in why, why


def test_carry_context_trio_refuses_when_current_context_not_equivalent(tmp_path):
    """🔴 变异反证：本轮 context **存在但内容不等价** ⇒ 仍然不搬（且不许覆盖本轮的）。"""
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "ta_research_context.md").write_text("上下文正文（旧数据）\n", encoding="utf-8")
    (dst / "ta_research_context.md").write_text("上下文正文（新数据）\n", encoding="utf-8")
    dec = _decision_json(src, _sha1(src / "ta_research_context.md"))

    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)

    assert ok is False, why
    assert "不等价" in why, why
    # 本轮现算的上下文**不许被来源的覆盖**
    assert (dst / "ta_research_context.md").read_text(encoding="utf-8") == "上下文正文（新数据）\n"


def test_carry_context_trio_copies_when_current_context_equivalent(tmp_path):
    """**正常路径不许变**（回归钉死）：本轮 context 存在且等价 ⇒ 照旧搬运。"""
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    (src / "ta_research_context.md").write_text(body.replace("<SNAP>", str(src)), encoding="utf-8")
    (dst / "ta_research_context.md").write_text(body.replace("<SNAP>", str(dst)), encoding="utf-8")
    dec = _decision_json(src, _sha1(src / "ta_research_context.md"))

    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)

    assert ok is True, why
    assert (dst / "ta_research_context.md").is_file()
    assert (dst / "ta_research_context.md").read_bytes() == (src / "ta_research_context.md").read_bytes()


def test_build_ta_context_runs_before_carry_context_trio():
    """🔴 不改变已交付快照行为：`build_ta_context` **先跑** ⇒ 本轮 context 必然存在
    ⇒ P1-2 新守卫在生产链上**不可达**（审计备注：失败即抛 `RuntimeError`）。"""
    tree = ast.parse(_ARB_PATH.read_text(encoding="utf-8"))
    build_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and n.value == "build_ta_context.py"
    ]
    carry_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "_carry_context_trio"
    ]
    assert build_lines, "daily 链里找不到 build_ta_context.py 的调用"
    assert carry_lines, "daily 链里找不到 _carry_context_trio 的调用"
    assert max(build_lines) < max(carry_lines), (
        f"build_ta_context.py（行 {max(build_lines)}）必须先于 "
        f"_carry_context_trio（行 {max(carry_lines)}）⇒ 否则本轮 context 可能缺失"
    )


# ───────────────────── ② P2-6 · run_gates.sh 的 config 读取位置 ─────────────────────


def _gates_text() -> str:
    return _GATES_SH.read_text(encoding="utf-8")


def _gates_lines() -> list[str]:
    return _gates_text().splitlines()


def _line_of(pattern: str, lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if re.search(pattern, line):
            return idx
    raise AssertionError(f"run_gates.sh 里找不到匹配 {pattern!r} 的行")


def test_run_gates_reads_analyze_top_n_after_cd_repo_root():
    """🔴 变异反证：`ANALYZE_TOP_N` 的读取位置**必须**在 `cd "$REPO_ROOT"` **之后**。"""
    lines = _gates_lines()
    i_cd = _line_of(r'^\s*cd "\$REPO_ROOT"', lines)
    i_read = _line_of(r'^\s*ANALYZE_TOP_N=', lines)
    assert i_read > i_cd, (
        f"ANALYZE_TOP_N 的读取在第 {i_read + 1} 行，早于 `cd \"$REPO_ROOT\"`（第 {i_cd + 1} 行）"
        "⇒ 从非仓库根 CWD 执行时 `import config` 必失败 ⇒ 口径漂移 ⇒ 闸门假红"
    )


def test_run_gates_analyze_top_n_fallback_is_not_silent():
    """🔴 变异反证：兜底值**不许静默**（读不到必须 WARNING 到 stderr + 日志）。"""
    lines = _gates_lines()
    i_read = _line_of(r'^\s*ANALYZE_TOP_N=', lines)
    read_line = lines[i_read]
    block = "\n".join(lines[i_read:i_read + 12])

    assert "2>/dev/null" not in read_line, f"ANALYZE_TOP_N 的读取仍在静默吞 stderr：{read_line}"
    assert "|| echo" not in read_line, f"ANALYZE_TOP_N 的读取仍在静默兜底：{read_line}"
    assert ">>\"$LOG\"" in read_line, f"ANALYZE_TOP_N 读取的 stderr 必须落进日志：{read_line}"
    assert "WARNING" in block, "兜底分支没有 WARNING ⇒ 口径漂移会静默发生"
    assert ">&2" in block, "兜底分支的 WARNING 必须打到 stderr（不许只落日志）"


def test_run_gates_config_read_needs_repo_root_cwd(tmp_path):
    """🔴 变异反证（行为级）：脚本里那条 config 读取是**仓库根相对**的
    ⇒ CWD 不在仓库根就**必然失败** ⇒ 读取位置（cd 之后）是**承重**的。"""
    lines = _gates_lines()
    i_read = _line_of(r'^\s*ANALYZE_TOP_N=', lines)
    m = re.search(r'-c "([^"]+)"', lines[i_read])
    assert m, f"解析不出 ANALYZE_TOP_N 的读取命令：{lines[i_read]}"
    code = m.group(1)
    assert "manu/etf_rotation" in code, f"config 读取应当用仓库根相对路径：{code}"

    # ① CWD 不在仓库根 ⇒ 必失败（旧位置下这条会被 `|| echo 100` 静默吞掉）
    bad = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=180)
    assert bad.returncode != 0, (
        "从非仓库根 CWD 居然读到了 config.ANALYZE_TOP_N ⇒ "
        "该读取已不依赖仓库根 ⇒ 本测试的前提失效（口径漂移风险回归）"
    )

    # ② CWD = 仓库根 ⇒ 必成功且打出候选池条数
    good = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO_ROOT),
                          capture_output=True, text=True, timeout=180)
    assert good.returncode == 0, f"在仓库根里读 config.ANALYZE_TOP_N 失败：{good.stderr[-800:]}"
    assert good.stdout.strip().isdigit(), f"读到的不是候选池条数：{good.stdout!r}"


def test_run_gates_shell_syntax_ok():
    """🔴 改了 shell 脚本 ⇒ 语法必须过（`bash -n`）。"""
    proc = subprocess.run(["bash", "-n", str(_GATES_SH)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"run_gates.sh 语法错：{proc.stderr}"
