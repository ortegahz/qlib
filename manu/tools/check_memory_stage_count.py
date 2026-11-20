#!/usr/bin/env python3
"""只读检查「闸门阶段数 / 位次」这类**计数类口径**是否与**单一事实源**一致。

为什么需要（`rules.md §十四.127`）
    计数类口径（阶段数、位次、条数…）是**最容易悄悄漂移**的一类事实：
    脚本自己加了阶段、注释忘了改、memory 抄错了数 ⇒ **没有任何报错**，只会**默默误导**。
    本轮实测四类真缺陷：
      ① `run_gates.sh` 头注释「26 个探针阶段」/ 段内「27 个探针阶段」⇒ 实测 `run_stage` = **28**；
      ② memory 四层把「26 阶段」当现口径 ⇒ 实测 **28**；
      ③ memory 写「26 阶段绿 / 4 阶段红」⇒ 同一轮日志实测 **24 绿 / 4 红 / 1 跳过**（抄错）；
      ④ 位次漂移：`probe_rolling_increment`「第 20 阶段」实测第 **22** 位（另有 3 处）。

判定口径
    * **单一事实源**：`manu/etf_rotation/run_gates.sh` 的 `run_stage` 调用数 ＝ 阶段总数 `N`；
      阶段顺序 ＝ `run_stage` 的**调用顺序**（＝**执行位次**，唯一可自证的口径）。
    * **检查 A（现口径数字等式）**：memory 四层里**当前口径型**的阶段数陈述必须 == `N`；
      「汇总条目数」必须 == `N + 1`（`--skip-repro` 模式下 repro 段记 SKIPPED，仍占一条）。
    * **检查 B（位次自洽）**：凡标了「执行位次」的位次陈述，其数字必须 == 该阶段的实测位次；
      标了位次却**点不出阶段名** ⇒ 缺陷（不可校验的位次等于没标）。
    * **检查 C（脚本自述）**：`run_gates.sh` **头注释**声明的阶段总数必须 == `N`。
    * **检查 D（标签不许写死）**：`manu/tools/*.sh` 不许出现写死的「全量闸门（N 阶段）」标签
      （必须**动态派生**），否则脚本加阶段时标签会悄悄过期。
    * **检查 E（历史不许篡改）**：一组**历史锚点**（历史阶段数的原文片段）必须**逐字还在**
      ⇒ 防止「为了让闸门变绿而删历史」（同 `rules.md §十四.124`）。
    * **检查 F（可选，日志对照）**：给了 `--gates-log` 时，把 memory 里的
      「N 阶段 ＝ x 绿 / y 红 / z 跳过」与**该日志的实测条目**逐项对照。

用法
    python3 manu/tools/check_memory_stage_count.py --project-dir .
    python3 manu/tools/check_memory_stage_count.py --project-dir . \
        --gates-log /mnt/data/siping/runs/etf_rotation_results/_gates_full_20261119v2acct_r2.log

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 用法/输入错误（文件读不到等）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_658d3fc4"

RUN_GATES_REL = "manu/etf_rotation/run_gates.sh"
TOOLS_DIR_REL = "manu/tools"

MEMORY_FILES = (
    "manu/memory/memory.md",
    "manu/memory/memory_compact.md",
    "manu/memory/.slim_memory.md",
    "manu/memory/rules.md",
)

#: 「执行位次」标注的两种写法（`rules.md §十四.127` 铁律二）
POS_PATTERNS = (
    re.compile(r"执行位次第\s*(\d+)\s*位"),
    re.compile(r"第\s*(\d+)\s*位\s*[（(]\s*执行位次"),
)

#: 检查 A：**现口径**型阶段数锚点。
#:   kind = "TOTAL"          ⇒ 必须 == `N`（阶段总数）
#:   kind = "TOTAL_SKIPREPRO" ⇒ 必须 == `N + 1`（`--skip-repro` 的汇总条目数）
#:   kind = "COMPOSITE"      ⇒ (总数, 绿, 红, 跳过)：总数 == `N`，其余三个只在给了
#:                              `--gates-log` 时对照日志核对（否则只校验总数）。
TOTAL_ANCHORS = (
    ("manu/memory/.slim_memory.md", re.compile(r"现口径：(\d+) 阶段"), "TOTAL", "精简档·全量闸门小节标题"),
    ("manu/memory/.slim_memory.md", re.compile(r"\*\*全量 (\d+) 阶段闸门本地跑不了\*\*"), "TOTAL", "精简档·已知遗留"),
    ("manu/memory/memory_compact.md", re.compile(r"\*\*全量 (\d+) 阶段闸门本地跑不了\*\*"), "TOTAL", "速览卡·已知遗留"),
    ("manu/memory/memory_compact.md", re.compile(r"复跑 (\d+) 阶段闸门"), "TOTAL", "速览卡·配套工具口径"),
    ("manu/memory/memory.md", re.compile(r"\*\*全量 (\d+) 阶段闸门本地跑不了\*\*"), "TOTAL", "主档·已知遗留"),
    ("manu/memory/memory.md", re.compile(r"汇总 \*\*(\d+)\*\* 条阶段条目"), "TOTAL_SKIPREPRO", "主档·汇总条目数"),
    ("manu/memory/memory.md", re.compile(r"脚本里现有 \*\*(\d+) 个 `run_stage` 阶段"), "TOTAL", "主档·闸门层现状"),
    ("manu/memory/memory.md", re.compile(r"# (\d+) 阶段全量闸门"), "TOTAL", "主档·命令速查"),
    ("manu/memory/memory_compact.md", re.compile(r"脚本现有 \*\*(\d+) 个 `run_stage` 阶段"), "TOTAL", "速览卡·闸门层现状"),
    ("manu/memory/memory_compact.md", re.compile(r"现有 \*\*(\d+) 个 `run_stage` 阶段"), "TOTAL", "速览卡·新口径"),
    ("manu/memory/.slim_memory.md", re.compile(r"现有 \*\*(\d+) 个 `run_stage` 阶段"), "TOTAL", "精简档·新口径"),
)

#: 「N 阶段 ＝ x 绿 / y 红 / z 跳过」复合口径（三层各一处，指同一轮实跑日志）
COMPOSITE_ANCHORS = (
    ("manu/memory/memory.md", re.compile(r"(\d+) 阶段 ＝ (\d+) 绿 / (\d+) 红 / (\d+) 跳过")),
    ("manu/memory/memory_compact.md", re.compile(r"(\d+) 阶段 ＝ (\d+) 绿 / (\d+) 红 / (\d+) 跳过")),
    ("manu/memory/.slim_memory.md", re.compile(r"(\d+) 阶段 ＝ (\d+) 绿 / (\d+) 红 / (\d+) 跳过")),
)

#: 检查 E：**历史锚点**（历史事实的原文片段）必须逐字还在。
#:   （这些是「当时的数字」；订正它们 ＝ 篡改事实源 ⇒ 只能保留，不能改）
HISTORICAL_ANCHORS = (
    ("manu/memory/memory.md", "自动化 **16 步流水线 + 21 阶段闸门**", "2026-11-05 M-R 轮的当时口径"),
    ("manu/memory/memory.md", "昨夜服务器全量闸门 **26 阶段**红了 **3** 个", "2026-11-10 当时的红阶段数"),
    ("manu/memory/memory_compact.md", "前 3 个来自昨夜服务器全量闸门 **26 阶段**红了 **3** 个", "2026-11-10 当时的红阶段数"),
    ("manu/memory/memory_compact.md", "全量闸门 **24 阶段** ⇒ 现口径 **28 阶段**（2026-11-10 当时口径 26 阶段）", "2026-11-09 收口时的口径跃迁（历史数 26 须内联保留）"),
    ("manu/memory/memory_compact.md", "🔴 **新口径**：**28 阶段**、断言 **PASS 行数 590**（**2026-11-10 当时口径 26 阶段**", "2026-11-10 当时的新口径（历史数 26 须内联保留）"),
    ("manu/memory/.slim_memory.md", "全量闸门 **24 阶段 23 绿 1 红** ⇒ 现口径 **28 阶段**（2026-11-10 当时口径 26 阶段）", "2026-11-10 当时的口径跃迁（历史数 26 须内联保留）"),
    ("manu/memory/.slim_memory.md", "🔴 **新口径**：**28 阶段**、断言 **PASS 行数 590**（**2026-11-10 当时口径 26 阶段**", "2026-11-10 当时的新口径（历史数 26 须内联保留）"),
    ("manu/memory/memory_compact.md", "现有 **28 个 `run_stage` 阶段 ＋ 1 个 repro 段**〔**2026-11-10 当时口径 26 阶段**〕", "速览卡·新口径的历史数须内联保留"),
    ("manu/memory/.slim_memory.md", "现有 **28 个 `run_stage` 阶段 ＋ 1 个 repro 段**〔**2026-11-10 当时口径 26 阶段**〕", "精简档·新口径的历史数须内联保留"),
    ("manu/memory/.slim_memory.md", "（昨夜全量闸门 **26 阶段**红 **3** 个", "2026-11-10 当时的红阶段数"),
)

#: 检查 B 要扫的文件（位次陈述可能出现的地方）
POSITION_SCAN_FILES = (
    "manu/etf_rotation/run_gates.sh",
    "manu/run_gates.sh",
    "manu/memory/memory.md",
    "manu/memory/memory_compact.md",
    "manu/memory/.slim_memory.md",
    "manu/memory/rules.md",
    "manu/docs/snapshot_architecture.md",
    "manu/etf_rotation/daily/README.md",
    "manu/daily/README.md",
)

#: 检查 D：`manu/tools/*.sh` 里不许写死的标签
HARDCODE_LABEL = re.compile(r"全量闸门（(\d+) 阶段）")

#: 检查 C：`run_gates.sh` 头注释里声明的阶段总数（头 12 行内）
HEADER_SCAN_LINES = 12
HEADER_TOTAL = re.compile(r"(\d+)\s*个阶段")

#: `run_stage` 调用行
RUN_STAGE = re.compile(r"^run_stage\s+(\S+)")


class InputError(Exception):
    """输入/用法错误 ⇒ 退出码 2。"""


@dataclass
class Defect:
    """一条缺陷。"""

    file: str
    line_no: int
    check: str
    reason: str
    preview: str

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line_no": self.line_no,
            "check": self.check,
            "reason": self.reason,
            "preview": self.preview,
        }


@dataclass
class GatesFacts:
    """从 `run_gates.sh` 与（可选）闸门日志里**实测**出来的口径。"""

    total: int
    probe_count: int
    stage_names: list[str] = field(default_factory=list)

    def position_of(self, name: str) -> int | None:
        """某阶段的**执行位次**（1 起）；名字不在清单里 ⇒ `None`。"""
        if name in self.stage_names:
            return self.stage_names.index(name) + 1
        return None


@dataclass
class LogFacts:
    """从闸门日志里实测出来的条目口径。"""

    entries: int
    green: int
    red: int
    skipped: int
    not_applicable: int


def read_text(path: str) -> str:
    """读文件；读不到 ⇒ 中文异常（⇒ 退出码 2）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"读不到待检查文件：{path}（{exc}）") from exc


def measure_run_gates(script_text: str) -> GatesFacts:
    """从 `run_gates.sh` 文本实测阶段总数 / 探针数 / 执行顺序。"""
    names: list[str] = []
    for line in script_text.splitlines():
        matched = RUN_STAGE.match(line)
        if matched:
            names.append(matched.group(1))
    if not names:
        raise InputError("`run_gates.sh` 里一个 `run_stage` 调用都没找到 ⇒ 脚本口径不可解析")
    return GatesFacts(
        total=len(names),
        probe_count=sum(1 for name in names if name.startswith("probe_")),
        stage_names=names,
    )


def parse_gates_log(log_text: str) -> LogFacts:
    """从闸门日志实测条目数 / 绿 / 红 / 跳过 / 不适用。"""
    green = red = skipped = not_applicable = 0
    entries = 0
    for line in log_text.splitlines():
        if not line.startswith("STAGE "):
            continue
        entries += 1
        # STAGE 行形如 `STAGE <名字>=<rc> —— <描述>` ⇒ rc 取 `=` 后的**第一个空白分隔词**
        payload = line.split("=", 1)[-1].split()[0] if "=" in line else ""
        if payload == "0":
            green += 1
        elif payload == "SKIPPED":
            skipped += 1
        elif payload == "2":
            not_applicable += 1
        else:
            red += 1
    if entries == 0:
        raise InputError("闸门日志里一条 `STAGE ` 都没有 ⇒ 日志口径不可解析")
    return LogFacts(entries, green, red, skipped, not_applicable)


def _names_in_line(line: str, stage_names: Iterable[str]) -> list[tuple[int, str]]:
    """一行里出现的阶段名 ＝ [(列位置, 名字)]（同一位置取**最长**的名字：`probe_lookahead` 是 `probe_lookahead_snapshot` 的前缀）。"""
    hits: list[tuple[int, str]] = []
    for name in stage_names:
        start = line.find(name)
        while start != -1:
            hits.append((start, name))
            start = line.find(name, start + 1)
    best: dict[int, str] = {}
    for pos, name in hits:
        if pos not in best or len(name) > len(best[pos]):
            best[pos] = name
    return sorted((pos, name) for pos, name in best.items())


def find_stage_name(
    lines: list[str], index: int, stage_names: Iterable[str], anchor_pos: int
) -> tuple[str | None, str]:
    """定位位次陈述**指的是哪个阶段**。

    返回 `(阶段名, 说明)`；阶段名是 `None` ⇒ 不可校验（⇒ 报缺陷）。
    判据：
      ① **同一行**里有阶段名 ⇒ 取**距标注最近**的那一个（长 changelog 行里常有多个阶段名）；
      ② 同行没有 ⇒ 退到**上一行**（句子被折行的情况）；
      ③ 上一行也没有 ⇒ 退到**下一行**；
      ④ 退到邻行时若该行有**多个**阶段名 ⇒ **歧义**（不可校验 ⇒ 报缺陷）。
    """
    same_line = _names_in_line(lines[index], stage_names)
    if same_line:
        pos, name = min(same_line, key=lambda item: (abs(item[0] - anchor_pos), -len(item[1])))
        return name, "同行"
    for offset, label in ((-1, "上一行"), (1, "下一行")):
        neighbour = index + offset
        if 0 <= neighbour < len(lines):
            candidates = _names_in_line(lines[neighbour], stage_names)
            if len(candidates) == 1:
                return candidates[0][1], label
            if len(candidates) > 1:
                return None, f"{label}有多个阶段名（歧义）"
    return None, "同行与邻行都点不出阶段名"


def check_total_anchors(
    memory_texts: dict[str, str], facts: GatesFacts
) -> list[Defect]:
    """检查 A：现口径数字等式。"""
    defects: list[Defect] = []
    for rel, pattern, kind, desc in TOTAL_ANCHORS:
        text = memory_texts.get(rel, "")
        matches = list(pattern.finditer(text))
        if not matches:
            defects.append(
                Defect(rel, 0, "A-现口径锚点", f"锚点缺失：{desc}（模式 {pattern.pattern}）", "")
            )
            continue
        for matched in matches:
            value = int(matched.group(1))
            expected = facts.total + 1 if kind == "TOTAL_SKIPREPRO" else facts.total
            if value != expected:
                line_no = text[: matched.start()].count("\n") + 1
                defects.append(
                    Defect(
                        rel,
                        line_no,
                        "A-现口径锚点",
                        f"{desc}：标注 {value}，实测 {expected}（{kind}）",
                        _line_preview(text, line_no),
                    )
                )
    return defects


def check_composite_anchors(
    memory_texts: dict[str, str], facts: GatesFacts, log_facts: LogFacts | None
) -> list[Defect]:
    """检查 A'：复合口径「N 阶段 ＝ x 绿 / y 红 / z 跳过」。"""
    defects: list[Defect] = []
    for rel, pattern in COMPOSITE_ANCHORS:
        text = memory_texts.get(rel, "")
        matches = list(pattern.finditer(text))
        if not matches:
            defects.append(
                Defect(rel, 0, "A-复合口径", f"锚点缺失：模式 {pattern.pattern}", "")
            )
            continue
        for matched in matches:
            total, green, red, skipped = (int(g) for g in matched.groups())
            line_no = text[: matched.start()].count("\n") + 1
            preview = _line_preview(text, line_no)
            if total != facts.total:
                defects.append(
                    Defect(
                        rel,
                        line_no,
                        "A-复合口径",
                        f"阶段总数标注 {total}，实测 {facts.total}",
                        preview,
                    )
                )
            if log_facts is None:
                continue
            for label, claimed, actual in (
                ("绿", green, log_facts.green),
                ("红", red, log_facts.red),
                ("跳过", skipped, log_facts.skipped),
            ):
                if claimed != actual:
                    defects.append(
                        Defect(
                            rel,
                            line_no,
                            "F-日志对照",
                            f"{label}阶段数标注 {claimed}，日志实测 {actual}",
                            preview,
                        )
                    )
    return defects


def check_positions(
    project_dir: str, facts: GatesFacts, file_texts: dict[str, str]
) -> list[Defect]:
    """检查 B：执行位次自洽。"""
    defects: list[Defect] = []
    for rel in POSITION_SCAN_FILES:
        text = file_texts.get(rel)
        if text is None:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            claimed: int | None = None
            for pattern in POS_PATTERNS:
                found = pattern.search(line)
                if found:
                    claimed = int(found.group(1))
                    break
            if claimed is None:
                continue
            anchor_pos = max(pattern.search(line).start() for pattern in POS_PATTERNS if pattern.search(line))
            stage_name, how = find_stage_name(lines, index, facts.stage_names, anchor_pos)
            if stage_name is None:
                defects.append(
                    Defect(
                        rel,
                        index + 1,
                        "B-位次自洽",
                        f"标了「执行位次」却{how} ⇒ 位次不可校验（必须点名阶段）",
                        line.strip()[:80],
                    )
                )
                continue
            actual = facts.position_of(stage_name)
            if actual is not None and claimed != actual:
                defects.append(
                    Defect(
                        rel,
                        index + 1,
                        "B-位次自洽",
                        f"{stage_name} 标注执行位次第 {claimed} 位，实测第 {actual} 位",
                        line.strip()[:80],
                    )
                )
    return defects


def check_script_header(script_text: str, facts: GatesFacts) -> list[Defect]:
    """检查 C：脚本头注释自述的阶段总数。"""
    defects: list[Defect] = []
    head = script_text.splitlines()[:HEADER_SCAN_LINES]
    matched = [m for line in head for m in HEADER_TOTAL.finditer(line)]
    if not matched:
        defects.append(
            Defect(
                RUN_GATES_REL,
                0,
                "C-脚本自述",
                f"头注释没有声明阶段总数（实测 {facts.total}）",
                "",
            )
        )
        return defects
    for line_no, line in enumerate(head, start=1):
        for item in HEADER_TOTAL.finditer(line):
            value = int(item.group(1))
            if value != facts.total:
                defects.append(
                    Defect(
                        RUN_GATES_REL,
                        line_no,
                        "C-脚本自述",
                        f"头注释声明 {value} 个阶段，实测 {facts.total} 个",
                        line.strip()[:80],
                    )
                )
    return defects


def check_tool_labels(project_dir: str) -> list[Defect]:
    """检查 D：`manu/tools/*.sh` 不许写死「全量闸门（N 阶段）」标签。"""
    defects: list[Defect] = []
    tools_dir = Path(project_dir) / TOOLS_DIR_REL
    if not tools_dir.is_dir():
        raise InputError(f"找不到工具目录：{tools_dir}")
    for path in sorted(tools_dir.glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        for matched in HARDCODE_LABEL.finditer(text):
            line_no = text[: matched.start()].count("\n") + 1
            defects.append(
                Defect(
                    f"{TOOLS_DIR_REL}/{path.name}",
                    line_no,
                    "D-标签不许写死",
                    f"写死了「{matched.group(0)}」⇒ 必须从 `run_gates.sh` 动态派生",
                    _line_preview(text, line_no),
                )
            )
    return defects


def check_historical_anchors(memory_texts: dict[str, str]) -> list[Defect]:
    """检查 E：历史锚点必须逐字还在（防删历史 / 防篡改历史）。"""
    defects: list[Defect] = []
    for rel, snippet, desc in HISTORICAL_ANCHORS:
        text = memory_texts.get(rel)
        if text is None:
            defects.append(Defect(rel, 0, "E-历史不许篡改", f"文件缺失，无法核历史锚点：{desc}", ""))
            continue
        if snippet not in text:
            defects.append(
                Defect(rel, 0, "E-历史不许篡改", f"历史锚点不见了（被删或被改）：{desc}", snippet[:80])
            )
    return defects


def _line_preview(text: str, line_no: int) -> str:
    """取第 `line_no` 行的前 80 字符（行号越界 ⇒ 空串）。"""
    lines = text.splitlines()
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1].strip()[:80]
    return ""


def analyze(project_dir: str, gates_log: str | None = None) -> list[Defect]:
    """总入口：跑全部检查 ⇒ 缺陷列表（每个 `Defect` 有 `as_dict()`）。"""
    script_path = os.path.join(project_dir, RUN_GATES_REL)
    script_text = read_text(script_path)
    facts = measure_run_gates(script_text)

    memory_texts: dict[str, str] = {}
    for rel in MEMORY_FILES:
        memory_texts[rel] = read_text(os.path.join(project_dir, rel))

    file_texts: dict[str, str] = {}
    for rel in POSITION_SCAN_FILES:
        path = os.path.join(project_dir, rel)
        if os.path.isfile(path):
            file_texts[rel] = read_text(path)

    log_facts = None
    if gates_log:
        log_facts = parse_gates_log(read_text(gates_log))

    defects: list[Defect] = []
    defects += check_total_anchors(memory_texts, facts)
    defects += check_composite_anchors(memory_texts, facts, log_facts)
    defects += check_positions(project_dir, facts, file_texts)
    defects += check_script_header(script_text, facts)
    defects += check_tool_labels(project_dir)
    defects += check_historical_anchors(memory_texts)
    return defects


def build_payload(defects: list[Defect], project_dir: str, gates_log: str | None) -> dict:
    """结构化结果（供 `--json-out`）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "gates_log": gates_log,
        "summary": {"defect_count": len(defects)},
        "defects": [defect.as_dict() for defect in defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "只读检查「闸门阶段数 / 位次」这类计数类口径是否与单一事实源"
            "（run_gates.sh 的 run_stage 调用数）一致。"
        )
    )
    parser.add_argument(
        "--project-dir",
        default=DEFAULT_PROJECT_DIR,
        help=f"工程根目录（默认 {DEFAULT_PROJECT_DIR}）",
    )
    parser.add_argument(
        "--gates-log",
        default=None,
        help="可选：闸门日志路径；给了就把 memory 里的「绿/红/跳过」与日志实测逐项对照",
    )
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list = None) -> int:
    args = parse_args(argv)
    try:
        defects = analyze(args.project_dir, args.gates_log)
    except InputError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    for defect in defects:
        location = f"{defect.file}:{defect.line_no}" if defect.line_no else defect.file
        print(f"[DEFECT] {location} {defect.check}｜{defect.reason}｜{defect.preview}")
    print(f"合计：缺陷 {len(defects)} 个")

    if args.json_out:
        payload = build_payload(defects, args.project_dir, args.gates_log)
        try:
            parent = os.path.dirname(os.path.abspath(args.json_out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[ERROR] 写出 JSON 结果失败：{args.json_out}（{exc}）", file=sys.stderr)
            return 2

    return 1 if defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
