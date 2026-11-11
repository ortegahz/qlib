#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM（TradingAgents）阶段可复现闸门探针。

【用途】
把「LLM 决策阶段的可复现性」与「交付件的辩论表格完整性」变成**可执行的闸门**：
脚本在 git 仓库里常驻、指纹已登记、交付 json 口径正确、研究上下文未过期、
**零 LLM 调用**即可逐字节重渲出交付 md、多空两方的每张表块都能在交付 md 里**逐字**找到、
每张表块内部自洽（行内 `|` 数量一致 / 行数下限）、run-root 零污染。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_llm_stage_repro.py \
        --run-root <落点根>/runs
    （省略 `--run-root` 时取 `config.ETF_RUN_ROOT`；退出码 0 = 全过，1 = 有 FAIL）

【比什么】
* 比「git 仓库里的 LLM 脚本」与「指纹文件 `_ms5_md5.txt` 的登记」⇒ md5 漂移即 FAIL；
* 比「交付件 `tradingagents_decision.md`」与「用交付 json 零 LLM 重渲出来的 md」⇒ sha1 必须逐字节相同；
* 比「交付 json 里多头 / 空头的表块（事实源）」与「交付 md 里同一批表块（展示件）」⇒ **逐字**对得上
  （少一块 / 被腰斩 ⇒ FAIL）；再**逐表块**校验块内自洽（行内 `|` 数量一致 / 行数 ≥ 5）。
  🔴 **不比**「多头 vs 空头」的表结构是否相同 —— 两家分析师的表本来就允许不同。

【为什么需要】
2026-11-05 的「M-N 事故」：`tradingagents_decision.md` 里**空头表整表消失**、多头表被腰斩成残行，
而当时的闸门**全绿**（没覆盖展示件这一层）⇒ 展示件不再等于事实源。
rules.md §十四.82 要求「可复现件必须落在 git 仓库里」；
本探针把「脚本在仓库里 + 指纹登记 + 渲染可复现 + 表格完整」四件事钉成硬闸门，
并用**真负例**（D1/D2/D3/D4）证明这些断言**有牙**（改坏交付件 ⇒ 对应断言必须 FAIL）。

【断言清单】
P1  LLM 脚本在 git 仓库里常驻（存在 / 非空 / 是文件）
P2  指纹文件登记了这两个脚本，且 md5 与当前文件一致（漂移 ⇒ FAIL）
P3  交付决策 json 结构与模型口径正确（status / provider / llm / effort / max_tokens）
P4  交付件引用的研究上下文未过期（meta.context_sha1 == 上下文 md 的实际 sha1）
P5  渲染可复现：`--render-only` 零 LLM 调用重渲的 md 与交付 md **逐字节**相同
P6  多头 / 空头两方的**每张表块**都能在交付 md 里**逐字**找到（整块消失 / 被腰斩 ⇒ FAIL）
P7  **逐表块**完整：块内行内 `|` 数量一致 / 行数 ≥ 5（**不**跨侧比较表结构）
P8  run-root 零污染：既有文件 sha1 全未变且无新增文件
D1  真负例：交付 md 副本里空头侧表块整块消失 ⇒ P6（与 P7）必须 FAIL
D2  真负例：交付 md 副本里多头侧表块被腰斩成残行 ⇒ P7 必须 FAIL
D3  真负例：交付 json 副本的 meta.context_sha1 被改坏 ⇒ P4 必须 FAIL
D4  真负例：交付 md 副本里多头侧表块整块消失 ⇒ P6（与 P7）必须 FAIL
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 仓库根：探针位于 `<repo>/manu/etf_rotation/probes/` ⇒ 按约定从 probes 上溯两层（`parents[2]`），
# 再上溯到**含 `manu/etf_rotation/llm` 的目录**——指纹文件里的相对路径以它为基准
# （形如 `manu/etf_rotation/llm/run_tradingagents.py`）。
REPO_ROOT = Path(__file__).resolve().parents[2]
while not (REPO_ROOT / "manu" / "etf_rotation" / "llm").is_dir():
    if REPO_ROOT.parent == REPO_ROOT:
        break
    REPO_ROOT = REPO_ROOT.parent

_ETF_DIR = Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import config as C  # noqa: E402

#: LLM 阶段脚本（相对仓库根的路径 —— 与指纹文件里的登记格式一致）
LLM_SCRIPTS = (
    "manu/etf_rotation/llm/run_tradingagents.py",
    "manu/etf_rotation/llm/build_ta_context.py",
)
#: 渲染器脚本（相对仓库根的路径）
RENDER_SCRIPT = "manu/etf_rotation/llm/run_tradingagents.py"

#: 探针自己的临时目录（**必须在 run-root 之外**：产物根只放交付件，见 rules.md）
TMP_ROOT = C.WORK_ROOT / "_probe_llm_stage_tmp"

#: 快照根目录名（架构迁移后：交付件在 `<canonical>/snapshots/<stamp>/`，指纹表留在 canonical 根）
SNAPSHOT_DIR_NAME = "snapshots"
#: 交付件 / 指纹文件名
DECISION_JSON = "tradingagents_decision.json"
DECISION_MD = "tradingagents_decision.md"
CONTEXT_MD = "ta_research_context.md"
FINGERPRINT_FILE = "_ms5_md5.txt"

#: 辩论双方（**只当展示用的标签**，不参与任何匹配）⇒ (侧名, 交付 json 里的正文字段名)
BULL_SIDE = "多头（Bull）"
BEAR_SIDE = "空头（Bear）"
DEBATE_SIDES = ((BULL_SIDE, "bull_history"), (BEAR_SIDE, "bear_history"))

#: meta 必备口径（交付决策 json）
EXPECTED_META = {
    "provider": "minimax-cn",
    "deep_think_llm": "MiniMax-M3.1-Flash",
    "reasoning_effort": "max",
}
EXPECTED_MAX_TOKENS = "32768"

#: 表格块最小行数（表头 + 分隔行 + ≥3 行数据）
MIN_TABLE_ROWS = 5

#: markdown 表格行（以竖线开头，**允许前导空白**）
_TABLE_ROW_RE = re.compile(r"^\s*\|")
#: 渲染器的「零 LLM 调用」早退分支 / dotenv 导入（行首锚定 ⇒ 注释里提到也不算）
_RENDER_ONLY_RE = re.compile(r"^\s*if args\.render_only:", re.MULTILINE)
_DOTENV_RE = re.compile(r"^\s*from dotenv import load_dotenv", re.MULTILINE)


class Checker:
    """断言收集器：逐条记录 (id, 名称, 是否通过, 细节)。"""

    def __init__(self) -> None:
        """初始化空的断言列表。"""
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        """记录一条断言结果。"""
        self.items.append((check_id, name, bool(ok), detail))

    def failed(self) -> list[tuple[str, str, bool, str]]:
        """挑出未通过的断言。"""
        return [it for it in self.items if not it[2]]


# --------------------------------------------------------------------------- 基础工具
def _sha1_bytes(data: bytes) -> str:
    """字节流的 sha1（十六进制）。"""
    return hashlib.sha1(data).hexdigest()


def _sha1_file(path: Path) -> str:
    """文件的 sha1（十六进制）。"""
    return _sha1_bytes(path.read_bytes())


def _md5_file(path: Path) -> str:
    """文件的 md5（十六进制，用于与指纹清单比对）。"""
    return hashlib.md5(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    """run-root 顶层文件的「文件名 -> sha1」快照（用于证明探针没动既有交付件）。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.iterdir()):
        if p.is_file():
            out[p.name] = _sha1_file(p)
    return out


def _require_file(path: Path, what: str) -> Path:
    """取必需输入文件；缺失 ⇒ 抛**中文**异常（不许默认值蒙混、不许静默跳过）。"""
    if not path.exists():
        raise FileNotFoundError(
            f"{what}不存在：{path}（LLM 阶段必须先产出该交付件，闸门不许默认放行）")
    if not path.is_file():
        raise FileNotFoundError(f"{what}不是文件：{path}（闸门只认普通文件）")
    return path


def _join_lines(lines: list[str], original: str) -> str:
    """把行列表拼回文本（保留原文末尾换行语义）。"""
    text = "\n".join(lines)
    if original.endswith("\n"):
        text += "\n"
    return text


# --------------------------------------------------------------------------- md 结构解析
# 🔴 为什么**不**按 md 的标题层级切「多头小节 / 空头小节」（2026-11-05 重写的根因）：
#    交付 md 是 `run_tradingagents._render_md()` 机械渲染出来的，骨架固定
#    （`## 三、多空辩论要点` / `### 多头（Bull）` / `### 空头（Bear）` / `### 辩论裁决` / `## 四、风险辩论`），
#    但**多头 / 空头正文里嵌着 LLM 报告自带的 `##` / `###` 标题**
#    （多头体内有 `## 一、…`…`## 三、…` 与 `### 1. …`…`### 4. …`；空头体内有 `## 一、…`…`## 五、…`）
#    ⇒ 「下一个 `###` 就是小节末」这种写法必然在 LLM 自带标题处误切
#    （旧实现切在第 159 行就是撞上多头体内的 `### 1.`）⇒ 误判「多头小节内没有表块」。
#    ⇒ **小节边界不能来自 md 自己的标题层级**。
#    改用「事实源（交付 json）× 展示件（交付 md）」交叉校验：
#    json 的 `investment_debate.bull_history` / `bear_history` 是多头 / 空头分析师的**原始输出**，
#    渲染器 `_tail()` **保证表格整块保留**（只裁散文块）⇒ 每张表块必须在 md 里**逐字连续出现**。
def _table_block_spans(lines: list[str]) -> list[tuple[int, int]]:
    """全文里所有 markdown 表块的行区间 ``[(块首行号, 块尾行号+1), ...]``。

    表块定义 = **连续的以 `|` 开头的行** —— 与渲染器 `run_tradingagents._split_table_blocks`
    的切块口径完全一致 ⇒ 渲染器「表格整块保留」这条保证在本探针里能逐字对齐。
    """
    spans: list[tuple[int, int]] = []
    cur_start = None
    for i, line in enumerate(lines):
        if _TABLE_ROW_RE.match(line):
            if cur_start is None:
                cur_start = i
        elif cur_start is not None:
            spans.append((cur_start, i))
            cur_start = None
    if cur_start is not None:
        spans.append((cur_start, len(lines)))
    return spans


def _table_blocks(text: str) -> list[list[str]]:
    """文本里所有 markdown 表块（每个表块 = 连续的、以 `|` 开头的行）。"""
    lines = text.splitlines()
    return [lines[s:e] for s, e in _table_block_spans(lines)]


def _find_verbatim_block(lines: list[str], block: list[str]) -> list[int]:
    """`block` 在 `lines` 里**逐字连续**出现的起始行号列表（0 基；没命中 ⇒ 空列表）。"""
    n = len(block)
    if n <= 0 or n > len(lines):
        return []
    hits: list[int] = []
    for i in range(len(lines) - n + 1):
        if lines[i:i + n] == block:
            hits.append(i)
    return hits


def _locate_debate_blocks(
    lines: list[str],
    side_blocks: dict[str, list[list[str]]],
) -> dict[str, list[tuple[int, int] | None]]:
    """把交付 md 里的表块与「各侧的事实源表块」一一对应 ⇒ 每侧给出 md 行区间列表。

    🔴 对应规则**只认「表头行逐字相同 + 行数相同」**，不拿数据行做定位：
       数据行正是被检对象（残行要由 P7 抓）⇒ 若用数据行定位，一行被腰斩就会让整块
       定位失败 ⇒ 把「内容坏了」误报成「表不见了」，P7 就抓不到残行。
    同表头出现多张表时按「逐行相同的行数最多」择优；每个 md 表块只归属一张事实源表块。
    """
    md_spans = _table_block_spans(lines)
    md_rows = {span: lines[span[0]:span[1]] for span in md_spans}
    candidates: list[tuple[int, int, str, int, tuple[int, int]]] = []
    for side, blocks in side_blocks.items():
        for bi, block in enumerate(blocks):
            if not block:
                continue
            for span in md_spans:
                rows = md_rows[span]
                if len(rows) != len(block) or rows[0] != block[0]:
                    continue
                score = sum(1 for a, b in zip(rows, block) if a == b)
                candidates.append((score, len(candidates), side, bi, span))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    located: dict[str, list[tuple[int, int] | None]] = {
        side: [None] * len(blocks) for side, blocks in side_blocks.items()}
    used: set[tuple[int, int]] = set()
    for _score, _order, side, bi, span in candidates:
        if located[side][bi] is not None or span in used:
            continue
        located[side][bi] = span
        used.add(span)
    return located


def _debate_side_blocks(payload: dict) -> tuple[dict[str, list[list[str]]], list[str]]:
    """从交付决策 json 抽出多头 / 空头两侧的表块集合（**事实源**）⇒ (各侧表块, 问题清单)。

    🔴 事实源 = 交付 json：`investment_debate.bull_history` / `bear_history` 是多头 / 空头
       分析师的**原始输出**；渲染器 `_tail()` 保证表格整块保留 ⇒ 展示件（md）里必须能逐字找到。
       （json 是机器可解析的事实源，md 只是给人看的展示件 —— M-N 事故正是展示件丢了表。）
    """
    problems: list[str] = []
    debate = payload.get("investment_debate")
    if not isinstance(debate, dict):
        return {}, ["交付决策 json 缺少 investment_debate 段（不是对象）"]
    side_blocks: dict[str, list[list[str]]] = {}
    for side, field in DEBATE_SIDES:
        raw = debate.get(field)
        if not isinstance(raw, str):
            problems.append(f"investment_debate.{field} 缺失或不是字符串（{side} 侧无正文）")
            continue
        blocks = _table_blocks(raw)
        side_blocks[side] = blocks
        if not blocks:
            problems.append(f"{side} 侧（json.investment_debate.{field}）"
                            "没有任何 markdown 表块（展示件无从谈起）")
    return side_blocks, problems


# --------------------------------------------------------------------------- 断言实现
def _check_repo_scripts(repo_root: Path) -> tuple[bool, str]:
    """P1：LLM 脚本在 git 仓库里常驻（存在 / 非空 / 是文件）。"""
    problems: list[str] = []
    notes: list[str] = []
    for rel in LLM_SCRIPTS:
        path = repo_root / rel
        if not path.exists():
            problems.append(f"{rel} 不存在")
        elif not path.is_file():
            problems.append(f"{rel} 不是文件")
        elif path.stat().st_size <= 0:
            problems.append(f"{rel} 是空文件")
        else:
            notes.append(f"{rel}（{path.stat().st_size} 字节）")
    detail = "；".join(problems) if problems else "；".join(notes)
    return (not problems), detail


def _parse_fingerprint(path: Path) -> dict[str, str]:
    """解析指纹文件：``<md5>  <相对仓库根的路径>`` 每行一条；跳过 `#` 开头与空行。"""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        out[parts[1]] = parts[0].lower()
    return out


def _resolve_fingerprint_path(run_root: Path, explicit: str = "") -> Path:
    """指纹表落点解析（🔴 架构迁移后指纹表**不在快照里**）。

    指纹表 ``_ms5_md5.txt`` 是 **canonical 根**的「代码状态登记表」
    （md5 段登记仓库脚本、sha1 段登记交付件基线）⇒ 它**不是**某一次运行的交付件
    ⇒ 不在快照的 16 交付件清单里。
    断言对象从 canonical 迁到「最新完整快照」之后，若只认 ``<run-root>/_ms5_md5.txt``
    ⇒ P2 会以「指纹文件不存在」误红。

    解析优先级：
    ① ``--fingerprint-table`` 显式指定；
    ② ``<run-root>/_ms5_md5.txt``（旧口径：run-root 本身就是 canonical 根）；
    ③ run-root 在 ``<canonical>/snapshots/<stamp>/`` 下 ⇒ ``<canonical>/_ms5_md5.txt``；
    ④ 兜底：``<run-root>/../_ms5_md5.txt``。
    """
    if explicit:
        return Path(explicit)
    direct = run_root / FINGERPRINT_FILE
    if direct.is_file():
        return direct
    candidates: list[Path] = []
    if run_root.parent.name == SNAPSHOT_DIR_NAME:
        candidates.append(run_root.parent.parent / FINGERPRINT_FILE)
    candidates.append(run_root.parent / FINGERPRINT_FILE)
    for cand in candidates:
        if cand.is_file():
            return cand
    return direct


def _check_fingerprint(run_root: Path, repo_root: Path, explicit: str = "") -> tuple[bool, str]:
    """P2：指纹文件登记了这两个 LLM 脚本，且 md5 与当前文件一致（漂移 ⇒ FAIL）。"""
    fp_path = _resolve_fingerprint_path(run_root, explicit)
    if not fp_path.is_file():
        return False, (f"指纹文件不存在：{fp_path}"
                       f"（指纹表是**canonical 根**的代码状态登记表，"
                       f"不在快照里；可用 --fingerprint-table 显式指定）")
    try:
        registry = _parse_fingerprint(fp_path)
    except OSError as exc:
        return False, f"指纹文件读不出来：{fp_path}（{exc}）"
    problems: list[str] = []
    notes: list[str] = []
    for rel in LLM_SCRIPTS:
        if rel not in registry:
            problems.append(f"{rel} 未登记进指纹文件（{fp_path.name}）")
            continue
        actual = _md5_file(repo_root / rel)
        declared = registry[rel]
        if declared != actual:
            problems.append(f"{rel} 指纹漂移：登记 {declared} ≠ 当前文件 {actual}")
        else:
            notes.append(f"{rel} md5={actual}")
    detail = ("；".join(problems) if problems
              else f"指纹表 {fp_path}｜" + "；".join(notes))
    return (not problems), detail


def _load_decision(path: Path) -> tuple[dict, dict]:
    """读交付决策 json ⇒ (payload, meta)；解析失败 ⇒ 抛**中文**异常。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"交付决策 json 解析失败：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"交付决策 json 结构不对（不是对象）：{path}")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise ValueError(f"交付决策 json 缺少 meta 段（不是对象）：{path}")
    return payload, meta


def _check_decision_json(payload: dict, meta: dict) -> tuple[bool, str]:
    """P3：交付决策 json 的结构与模型口径。"""
    problems: list[str] = []
    notes: list[str] = []
    status = payload.get("status")
    if status != "ok":
        problems.append(f"status={status!r} ≠ 'ok'")
    else:
        notes.append("status=ok")
    for key, expect in EXPECTED_META.items():
        got = meta.get(key)
        if got != expect:
            problems.append(f"meta.{key}={got!r} ≠ {expect!r}")
        else:
            notes.append(f"meta.{key}={expect}")
    max_tokens = meta.get("max_tokens")
    if max_tokens is None:
        problems.append("meta.max_tokens 缺失")
    elif str(max_tokens) != EXPECTED_MAX_TOKENS:
        problems.append(f"meta.max_tokens={max_tokens!r} → str()={str(max_tokens)!r} "
                        f"≠ {EXPECTED_MAX_TOKENS!r}")
    else:
        notes.append(f"meta.max_tokens={EXPECTED_MAX_TOKENS}")
    detail = "；".join(problems) if problems else "；".join(notes)
    return (not problems), detail


def _check_context_sha1(meta: dict, context_path: Path) -> tuple[bool, str]:
    """P4：交付件引用的研究上下文未过期（context_sha1 == 上下文 md 的实际 sha1）。"""
    declared = meta.get("context_sha1")
    if declared is None:
        return False, "meta.context_sha1 缺失"
    actual = _sha1_file(context_path)
    if str(declared).strip().lower() != actual:
        return False, f"声明 {declared} ≠ 实际 {actual}（上下文已过期/被换过）"
    return True, f"context_sha1={actual}"


def _render_only_md(run_root: Path, out_md: Path) -> subprocess.CompletedProcess:
    """零 LLM 调用重渲 md（`--render-only` 分支先于 dotenv import 就 return ⇒ 纯 stdlib）。"""
    cmd = [
        sys.executable,
        str(REPO_ROOT / RENDER_SCRIPT),
        "--render-only", "1",
        "--run-root", str(run_root),
        "--out-json", str(run_root / DECISION_JSON),
        "--out-md", str(out_md),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=str(REPO_ROOT))


def _check_render_only(run_root: Path, delivered_md: Path) -> tuple[bool, str]:
    """P5：零 LLM 调用重渲的 md 与交付 md 逐字节相同。"""
    render_md = TMP_ROOT / "render.md"
    if render_md.exists():
        render_md.unlink()
    proc = _render_only_md(run_root, render_md)
    problems: list[str] = []
    notes: list[str] = []
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
        problems.append("重渲退出码=" + str(proc.returncode)
                        + ("｜stderr=" + " / ".join(stderr_tail) if stderr_tail else ""))
        return False, "；".join(problems)
    if not render_md.is_file():
        return False, f"重渲没有产出 md：{render_md}"
    got = _sha1_file(render_md)
    expect = _sha1_file(delivered_md)
    if got != expect:
        problems.append(f"重渲 md sha1={got} ≠ 交付 md sha1={expect}（渲染不可复现）")
    else:
        notes.append(f"重渲 sha1={got} == 交付 sha1（逐字节相同）")
    # 🔴 「零 LLM 调用」的结构性保证：render-only 的早退必须**先于** dotenv import
    #    （否则重渲会读 .env / 打真接口 ⇒ 闸门自己就花钱）。
    src = (REPO_ROOT / RENDER_SCRIPT).read_text(encoding="utf-8")
    render_m = _RENDER_ONLY_RE.search(src)
    dotenv_m = _DOTENV_RE.search(src)
    if render_m is None or dotenv_m is None or render_m.start() > dotenv_m.start():
        problems.append("run_tradingagents.py 里 render-only 的早退不再早于 dotenv import"
                        "（重渲可能触发真实 LLM 调用）")
    else:
        notes.append("render-only 早退先于 dotenv import ⇒ 零 LLM 调用")
    return (not problems), "；".join(problems) if problems else "；".join(notes)


def _check_debate_tables_present(md_text: str, side_blocks: dict[str, list[list[str]]],
                                 json_problems: list[str]) -> tuple[bool, str]:
    """P6：多头 / 空头两侧的**每一张表块**都能在交付 md 里**逐字**找到。

    🔴 口径（2026-11-05 重写）：**不再**按 md 的标题层级切「小节」
       （多头 / 空头正文里嵌着 LLM 自带的 `##` / `###` 标题 ⇒ 任何层级切法都会误切）；
       改用「事实源（交付 json）× 展示件（交付 md）」交叉校验：
       json 里每张表块（渲染器保证整块保留）必须在 md 里**逐字连续出现**，
       **少一块就 FAIL**（这正是 M-N「空头表整表消失」事故）。
    """
    problems = list(json_problems)
    notes: list[str] = []
    lines = md_text.splitlines()
    for side, blocks in side_blocks.items():
        for bi, block in enumerate(blocks):
            hits = _find_verbatim_block(lines, block)
            if hits:
                notes.append(f"{side} 第 {bi + 1} 张表块（{len(block)} 行）在 md 第 "
                             + str(hits[0] + 1) + " 行逐字命中")
            else:
                problems.append(f"{side} 第 {bi + 1} 张表块（{len(block)} 行）在交付 md 里"
                                "没有逐字出现（整块消失 / 被腰斩 ⇒ M-N 事故）")
    detail = "；".join(problems) if problems else "；".join(notes)
    return (not problems), detail


def _check_table_integrity(md_text: str, side_blocks: dict[str, list[list[str]]],
                           json_problems: list[str]) -> tuple[bool, str]:
    """P7：**逐表块**校验 —— ① 同一表块内所有行行内 `|` 数量一致；② 表块行数 ≥ MIN_TABLE_ROWS。

    🔴 口径（2026-11-05 重写）：**删掉**跨侧等式（「多头与空头表块数必须相等」/
       「逐块行数必须相等」/「表头 `|` 数必须相等」）—— 两家分析师的表结构本来就允许不同
       （多头 2 张、空头 1 张是**正常**的交付形态，不是缺陷）⇒ 留着这条等式等于把
       「本轮观察到的表数量」当金标（违反 rules.md §十四.91 item 6「断言不许抄现状」）。
       剩下的「表块内自洽」（不出现残行 / 空表）才是真正该拦的东西。
    """
    problems = list(json_problems)
    notes: list[str] = []
    lines = md_text.splitlines()
    located = _locate_debate_blocks(lines, side_blocks)
    for side, blocks in side_blocks.items():
        spans = located.get(side) or []
        for bi, _block in enumerate(blocks):
            span = spans[bi] if bi < len(spans) else None
            if span is None:
                problems.append(f"{side} 第 {bi + 1} 张表块在交付 md 里整块消失"
                                "（找不到表头与行数都相同的表块）")
                continue
            rows = lines[span[0]:span[1]]
            pipes = sorted({row.count("|") for row in rows})
            if len(pipes) > 1:
                problems.append(f"{side} 第 {bi + 1} 张表块行内 `|` 数量不一致："
                                + str(pipes) + "（残行 ⇒ 被腰斩）")
            if len(rows) < MIN_TABLE_ROWS:
                problems.append(f"{side} 第 {bi + 1} 张表块只有 {len(rows)} 行"
                                f"（< {MIN_TABLE_ROWS}：表头 + 分隔行 + ≥3 行数据）")
            if len(pipes) == 1:
                notes.append(f"{side} 第 {bi + 1} 张表块：{len(rows)} 行 / 每行 "
                             + str(pipes[0]) + " 个 `|`")
    detail = "；".join(problems) if problems else "；".join(notes)
    return (not problems), detail


# --------------------------------------------------------------------------- 真负例（变异）
def _mutate_drop_side_tables(md_text: str, side_blocks: dict[str, list[list[str]]],
                             side: str) -> str:
    """负例 D1 / D4：把某一侧（多头 / 空头）的表块整块删掉 ⇒ 返回**新** md 字符串（不改原字符串）。

    🔴 负例按**新边界**构造：某一侧 = 交付 json 的 `investment_debate.bull_history` /
       `bear_history` 里的表块（事实源）⇒ 先在交付 md 里定位到它们，再整块删掉 ⇒ P6 必须 FAIL。
    """
    lines = md_text.splitlines()
    side_bullets = side_blocks.get(side) or []
    if not side_bullets:
        raise ValueError(f"{side} 侧（事实源）里没有表块 ⇒ 负例无法构造")
    located = _locate_debate_blocks(lines, side_blocks)
    spans = located.get(side) or []
    drop: set[int] = set()
    _dropped_spans: list[tuple[int, int]] = []
    for bi in range(len(side_bullets)):
        span = spans[bi] if bi < len(spans) else None
        if span is None:
            raise ValueError(f"{side} 侧第 {bi + 1} 张表块在交付 md 里定位不到"
                             " ⇒ 负例无法构造")
        drop.update(range(span[0], span[1]))
        _dropped_spans.append(span)
    # 🔴 2026-11-11 修（**D4 负例失效 ⇒ 闸门假红**）：
    #    同一张表在交付 md 里可能**出现不止一次**（多头表在「多空辩论要点」与
    #    「Trader 交易计划」里各出现一次）⇒ 只删**定位到的那一份**时，
    #    P7 仍能在 md 里找到另一张「表头逐字相同 + 行数相同」的表 ⇒
    #    P7 **真空 PASS**（它只检查「找到的表块」自不自洽）⇒
    #    D4 要求的「P6 **与** P7 必须同时 FAIL」永远不成立 ⇒ 负例**没有牙**
    #    （实测 2026-11-11：Pass10 / Pass12 都红在 D4）。
    #    ⇒ 修法：把**同签名（表头逐字 + 行数）**的所有 md 表块**一起删掉**
    #      ⇒ 真正模拟「这一侧的表块**整块**消失」⇒ P7 必 FAIL ⇒ 负例恢复牙齿。
    _sigs = set()
    for _span in _dropped_spans:
        _rows = lines[_span[0]:_span[1]]
        if _rows:
            _sigs.add((_rows[0], len(_rows)))
    if _sigs:
        for _span in _table_block_spans(lines):
            _rows = lines[_span[0]:_span[1]]
            if ((_rows[0] if _rows else ""), len(_rows)) in _sigs:
                drop.update(range(_span[0], _span[1]))
    kept = [line for i, line in enumerate(lines) if i not in drop]
    return _join_lines(kept, md_text)


def _mutate_halve_bull_row(md_text: str, side_blocks: dict[str, list[list[str]]]) -> str:
    """负例 D2：把**多头表**某一行腰斩（砍掉后半截 ⇒ 该行 `|` 数量变少）⇒ 返回**新** md 字符串。

    🔴 负例按**新边界**构造：多头侧 = 交付 json 的 `investment_debate.bull_history` 里
       的表块（事实源）⇒ 先在交付 md 里定位到它们，再腰斩其中一行（数据行优先）。
       腰斩后该表块仍可被定位（表头 + 行数没变）⇒ P7 必须以「块内行内 `|` 数量不一致」抓它。
    """
    lines = md_text.splitlines()
    bull_blocks = side_blocks.get(BULL_SIDE) or []
    if not bull_blocks:
        raise ValueError("多头侧（json.investment_debate.bull_history）里没有表块"
                         " ⇒ D2 负例无法构造")
    located = _locate_debate_blocks(lines, side_blocks)
    spans = located.get(BULL_SIDE) or []
    target: tuple[int, int] | None = None
    for bi in range(len(bull_blocks)):
        span = spans[bi] if bi < len(spans) else None
        if span is not None and span[1] - span[0] >= 3:
            target = span
            break
    if target is None:
        raise ValueError("多头侧表块在交付 md 里定位不到（或不足 3 行）⇒ D2 负例无法构造")
    block_start, block_end = target
    candidates = [block_start + 2] + [i for i in range(block_start, block_end)
                                      if i != block_start + 2]
    new_lines = list(lines)
    for row_idx in candidates:
        if row_idx < block_start or row_idx >= block_end:
            continue
        original = lines[row_idx]
        if original.count("|") < 2:
            continue
        halved = original[: max(1, len(original) // 2)]
        if halved.count("|") == original.count("|"):
            # 极端情况：前半截没切到 `|` ⇒ 砍掉最后一个 `|` 及其之后的内容
            pos = original.rfind("|")
            halved = original[:pos]
        if halved.count("|") < original.count("|"):
            new_lines[row_idx] = halved
            return _join_lines(new_lines, md_text)
    raise ValueError("多头表行的腰斩变异没生效 ⇒ D2 负例无效")


def _mutate_context_sha1(payload: dict) -> dict:
    """负例 D3：把决策 json 副本的 meta.context_sha1 改成错误值 ⇒ 返回**新** payload。"""
    mutated = copy.deepcopy(payload)
    mutated["meta"]["context_sha1"] = "0" * 40
    return mutated


# --------------------------------------------------------------------------- 主流程
def run_checks(run_root: Path, repo_root: Path, fingerprint_table: str = "") -> list[tuple[str, str, bool, str]]:
    """跑全部断言 ⇒ 返回 (id, 名称, 是否通过, 细节) 列表。"""
    checker = Checker()

    # --- 必需输入：读不到 ⇒ 抛中文异常退出（不许默认值蒙混） ---
    decision_json_path = _require_file(run_root / DECISION_JSON, "交付决策 json")
    decision_md_path = _require_file(run_root / DECISION_MD, "交付决策 md")
    context_md_path = _require_file(run_root / CONTEXT_MD, "研究上下文 md")
    md_text = decision_md_path.read_text(encoding="utf-8")
    payload, meta = _load_decision(decision_json_path)
    # 多空两侧的表块集合 = **事实源**（交付 json 侧）；展示件（md）侧靠内容交叉校验
    side_blocks, json_problems = _debate_side_blocks(payload)

    # run-root 零污染基线（探针运行前的快照）
    before = _snapshot(run_root)

    # --- 临时目录：必须在 run-root 之外 ---
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    ok, detail = _check_repo_scripts(repo_root)
    checker.add("P1", "LLM 脚本在 git 仓库里常驻（存在 / 非空 / 是文件）", ok, detail)

    ok, detail = _check_fingerprint(run_root, repo_root, fingerprint_table)
    checker.add("P2", "指纹文件登记了两个 LLM 脚本且 md5 与当前文件一致（漂移 ⇒ FAIL）",
                ok, detail)

    ok, detail = _check_decision_json(payload, meta)
    checker.add("P3", "交付决策 json 结构与模型口径正确", ok, detail)

    ok, detail = _check_context_sha1(meta, context_md_path)
    checker.add("P4", "研究上下文未过期（meta.context_sha1 == 上下文 md 实际 sha1）", ok, detail)

    ok, detail = _check_render_only(run_root, decision_md_path)
    checker.add("P5", "渲染可复现：零 LLM 调用重渲的 md 与交付 md 逐字节相同", ok, detail)

    ok, detail = _check_debate_tables_present(md_text, side_blocks, json_problems)
    checker.add("P6", "多头 / 空头两侧的每张表块都能在交付 md 里逐字找到（整块消失 ⇒ FAIL）",
                ok, detail)

    ok, detail = _check_table_integrity(md_text, side_blocks, json_problems)
    checker.add("P7", "逐表块完整：块内行内 `|` 数量一致 / 行数 ≥ 5（不跨侧比较表结构）",
                ok, detail)

    # --- 真负例：只操作**副本**，以「返回新值」的方式传给被断言的函数 ---
    d1_detail = ""
    d1_ok = False
    try:
        mutated_md = _mutate_drop_side_tables(md_text, side_blocks, BEAR_SIDE)
        p6_ok, p6_detail = _check_debate_tables_present(mutated_md, side_blocks, json_problems)
        p7_ok, p7_detail = _check_table_integrity(mutated_md, side_blocks, json_problems)
        violated: list[str] = []
        if not p6_ok:
            violated.append("P6：" + p6_detail)
        if not p7_ok:
            violated.append("P7：" + p7_detail)
        d1_ok = (not p6_ok) and (not p7_ok)
        d1_detail = "变异后违例 " + str(len(violated)) + " 条：" + "；".join(violated)
    except Exception as exc:  # noqa: BLE001
        d1_detail = "负例构造异常：" + repr(exc)
    checker.add("D1", "真负例：交付 md 副本里空头侧表块整块消失 ⇒ P6（与 P7）必须 FAIL",
                d1_ok, d1_detail)

    d2_detail = ""
    d2_ok = False
    try:
        mutated_md = _mutate_halve_bull_row(md_text, side_blocks)
        p7_ok, p7_detail = _check_table_integrity(mutated_md, side_blocks, json_problems)
        p6_ok, p6_detail = _check_debate_tables_present(mutated_md, side_blocks, json_problems)
        violated: list[str] = []
        if not p7_ok:
            violated.append("P7：" + p7_detail)
        if not p6_ok:
            violated.append("P6：" + p6_detail)
        note = "" if not p6_ok else "（P6 仍 PASS：整块还在，残行由 P7 抓）"
        d2_ok = not p7_ok
        d2_detail = ("变异后违例 " + str(len(violated)) + " 条：" + "；".join(violated) + note)
    except Exception as exc:  # noqa: BLE001
        d2_detail = "负例构造异常：" + repr(exc)
    checker.add("D2", "真负例：交付 md 副本里多头侧表块被腰斩成残行 ⇒ P7 必须 FAIL",
                d2_ok, d2_detail)

    d3_detail = ""
    d3_ok = False
    try:
        mutated_payload = _mutate_context_sha1(payload)
        p4_ok, p4_detail = _check_context_sha1(mutated_payload["meta"], context_md_path)
        violated: list[str] = [] if p4_ok else ["P4：" + p4_detail]
        d3_ok = not p4_ok
        d3_detail = "变异后违例 " + str(len(violated)) + " 条：" + "；".join(violated)
    except Exception as exc:  # noqa: BLE001
        d3_detail = "负例构造异常：" + repr(exc)
    checker.add("D3", "真负例：交付 json 副本的 meta.context_sha1 被改坏 ⇒ P4 必须 FAIL",
                d3_ok, d3_detail)

    d4_detail = ""
    d4_ok = False
    try:
        mutated_md = _mutate_drop_side_tables(md_text, side_blocks, BULL_SIDE)
        p6_ok, p6_detail = _check_debate_tables_present(mutated_md, side_blocks, json_problems)
        p7_ok, p7_detail = _check_table_integrity(mutated_md, side_blocks, json_problems)
        violated: list[str] = []
        if not p6_ok:
            violated.append("P6：" + p6_detail)
        if not p7_ok:
            violated.append("P7：" + p7_detail)
        d4_ok = (not p6_ok) and (not p7_ok)
        d4_detail = "变异后违例 " + str(len(violated)) + " 条：" + "；".join(violated)
    except Exception as exc:  # noqa: BLE001
        d4_detail = "负例构造异常：" + repr(exc)
    checker.add("D4", "真负例：交付 md 副本里多头侧表块整块消失 ⇒ P6（与 P7）必须 FAIL",
                d4_ok, d4_detail)

    # --- P8：run-root 零污染（探针跑完再比） ---
    after = _snapshot(run_root)
    changed = sorted(name for name, digest in before.items() if after.get(name) != digest)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    problems = []
    if changed:
        problems.append("既有文件被改写：" + ",".join(changed))
    if added:
        problems.append("run-root 新增文件：" + ",".join(added))
    if removed:
        problems.append("run-root 文件消失：" + ",".join(removed))
    checker.add("P8", "run-root 零污染：既有文件 sha1 全未变且无新增文件",
                not problems, "；".join(problems) if problems
                else f"{len(before)} 个既有文件 sha1 全未变、无新增/消失（临时目录在 run-root 之外）")

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return checker.items


def main() -> int:
    """命令行入口：``--run-root`` 指向产物根。"""
    ap = argparse.ArgumentParser(description="LLM（TradingAgents）阶段可复现闸门")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    ap.add_argument("--fingerprint-table", default="",
                    help="指纹表路径（缺省：run-root 下 → canonical 根；指纹表不在快照里）")
    args = ap.parse_args()

    items = run_checks(Path(args.run_root), REPO_ROOT, args.fingerprint_table)
    failed = [it for it in items if not it[2]]
    for check_id, name, ok, detail in items:
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", check_id, name)
              + (("｜" + detail) if detail else ""))
    print("合计 %d 项：PASS %d / FAIL %d" % (len(items), len(items) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
