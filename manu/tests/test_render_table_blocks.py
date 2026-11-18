"""🔴 2026-11-10 新增：交付 md 的「表块结构」回归（渲染器 `run_tradingagents._tail`）。

事故来源（2026-11-09 17:49 那一轮交付 md，服务器 `probe_llm_stage_repro` P7/D4 红灯）：

  LLM 原文里两张 markdown 表之间是一个**结构分隔块** `"\n### 新目标及候选\n"`（12 字符）；
  旧 `_tail()` 把它当**普通散文** ⇒ 散文预算超支时**整块丢弃** ⇒ 后果有两条：

    ① 分隔块里的**标题整段消失**（交付件丢内容 ⇒ 与 M-N「交付件丢表」同类）；
    ② **两张表首尾相接**（中间一个空行都没有）⇒ 在 markdown 渲染器里两张表会被当成
       **一张表**（第二张表的表头变成第一张表的数据行）⇒ 用户读的纪要里
       第二张表**根本没有渲染出来**（探针 P6 逐字命中、P7 定位失败 ⇒ 红灯）。

本文件钉死四件事（自带夹具，不依赖服务器产物）：

  ① 两张表之间的**分隔块不许整块丢**（预算再紧也要保留头部）；
  ② 渲染后两张表必须是**两个独立表块**（不许首尾相接）；
  ③ 每张表**前面必须有空行**（GFM：表格不能打断段落 ⇒ 否则那张表不渲染成表格）；
  ④ **变异反证**：把「分隔块整块丢弃」「表前不留空行」两个变异点改回去，
     上面三条断言必须**转红**（证明本测试有牙，不是空跑）。

夹具按**真实事故件的结构**搭（7 个块）：
    散文0 | 表X | **大散文**（吃光散文预算）| 表A | **分隔标题** | 表B | 散文1
    ⇒ 预算按「离中段最近优先」消耗 ⇒ 先轮到中间那段大散文 ⇒ 再轮到分隔标题时
      预算已见底 ⇒ 旧口径把它丢掉（真实事故就是这么发生的）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

#: `manu/etf_rotation/` 目录（渲染器在它下面的 `llm/` 里）
_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETF_DIR))

#: `run_tradingagents.py` 在 `manu/etf_rotation/llm/` 下（不在 `sys.path` 的默认位置）
_RTA_PATH = _ETF_DIR / "llm" / "run_tradingagents.py"


def _load_rta():
    spec = importlib.util.spec_from_file_location("_rta_under_test", _RTA_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RTA = _load_rta()

#: markdown 表格行（以竖线开头，允许前导空白）
_TABLE_ROW_RE = re.compile(r"^\s*\|")

#: 夹具里两张表的表头（必须与事实源逐字一致，否则「粘连」与「独立」看不出来）
_TABLE_A_HEAD = "| 代码 | 当前仓位 | 方向 | 目标权重 | 理由 |"
_TABLE_B_HEAD = "| 代码 | 方向 | 目标权重 | 参考金额 | 理由 |"
#: 夹具里夹在两张表之间的**分隔块标题**
_SEPARATOR_HEADING = "### 新目标及候选"
#: 渲染预算（要小于夹具里那段大散文 ⇒ 预算必然被吃光）
_LIMIT = 2000


def _fixture() -> str:
    """自带夹具：复刻真实事故件的结构（见模块 docstring 的 7 块结构图）。"""
    table_x = "\n".join([
        "| 指标 | 512800 | 515450 |",
        "|---|---:|---:|",
        "| 最终分 | **0.9760** | 0.5412 |",
    ])
    table_a = "\n".join([
        _TABLE_A_HEAD,
        "|---|---:|---|---:|---|",
        "| **512800** | 7,700份 | 持有 | 20% | 动量最强 |",
        "| **512690** | 2,000份 | 卖出全部 | 0% | 强制退出 |",
    ])
    table_b = "\n".join([
        _TABLE_B_HEAD,
        "|---|---:|---:|---:|---|",
        "| **515450** | 条件性小仓买入 | 10% | 约2,985元 | 红利同族最优 |",
        "| **563020** | 不买 | 0% | 0元 | 未入选 |",
    ])
    lead = "".join(f"前置论证第 {i} 段：用来把散文预算撑满，模拟真实交付件的长正文。\n\n"
                   for i in range(1, 20))
    # 🔴 中段大散文：**长度必须 ≥ 渲染预算**（按 `_LIMIT` 构造，不写死段数）
    #    ⇒ 它先被裁（`_head_tail`）并把预算吃光
    #    ⇒ 后面轮到分隔标题时预算已见底（旧口径就是在这里把标题丢掉的）。
    middle = ""
    while len(middle) < _LIMIT + 500:
        middle += "中段论证：这段必须比渲染预算长，才能把散文预算吃光。\n\n"
    middle += "### 当前持仓\n"
    tail = "".join(f"后置论证第 {i} 段：同样用来撑满散文预算。\n\n" for i in range(1, 20))
    return (
        f"{lead}"
        f"{table_x}\n\n"
        f"{middle}"
        f"{table_a}\n\n"
        f"{_SEPARATOR_HEADING}\n\n"
        f"{table_b}\n\n"
        f"{tail}"
    )


def _table_blocks(text: str) -> list[list[str]]:
    """按「连续的 `|` 行」切表块（与探针 `probe_llm_stage_repro` 同口径）。"""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    buf: list[str] = []
    for line in lines:
        if _TABLE_ROW_RE.match(line):
            buf.append(line)
        elif buf:
            blocks.append(buf)
            buf = []
    if buf:
        blocks.append(buf)
    return blocks


def _old_tail(text: str, limit: int) -> str:
    """修复**前**的 `_tail()` 原样复刻（变异反证用的「坏实现」）。

    与现实现的两点差异（正是本次事故的两个成因）：
      · 分隔块被当**普通散文** ⇒ 预算不足时**整块丢弃**（⇒ 两张表粘连 + 标题消失）；
      · 表格前**不补空行**（⇒ 表格紧跟散文行 ⇒ GFM 里不渲染成表格）。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    blocks = RTA._split_table_blocks(text)
    if not any(is_table for is_table, _ in blocks):
        return RTA._head_tail(text, limit)
    budget = limit
    used = 0
    kept: dict[int, str] = {}
    prose_idx = [i for i, (is_table, _) in enumerate(blocks) if not is_table]
    prose_len = {i: len(blocks[i][1]) for i in prose_idx}
    for i in sorted(prose_idx, key=lambda i: (abs((len(blocks) - 1) / 2.0 - i), i)):
        length = prose_len[i]
        if used + length <= budget:
            used += length
            kept[i] = blocks[i][1]
            continue
        remaining = budget - used
        if remaining >= RTA._MIN_PROSE_KEEP:
            kept[i] = RTA._head_tail(blocks[i][1], remaining)
            used += len(kept[i])
        else:
            continue  # ← 旧口径：直接丢弃（分隔块就这样没了）
    out: list[str] = []
    marker_done = False
    for i, (is_table, block) in enumerate(blocks):
        if is_table:
            out.append(block)
            continue
        if i in kept:
            out.append(kept[i])
            continue
        if not marker_done:
            out.append("...[中段省略]...")
            marker_done = True
    return "\n".join(out)


def test_separator_between_tables_is_not_dropped() -> None:
    """① 两张表之间的分隔块（标题）不许整块丢 —— 预算再紧也要保留头部。"""
    out = RTA._tail(_fixture(), _LIMIT)
    assert _SEPARATOR_HEADING in out, (
        "两张表之间的分隔标题被整块丢弃了（交付件丢内容）；"
        f"实得输出长度={len(out)}"
    )


def test_two_tables_stay_two_blocks() -> None:
    """② 两张表必须渲染成**两个独立表块**（不许首尾相接粘成一张）。"""
    out = RTA._tail(_fixture(), _LIMIT)
    blocks = _table_blocks(out)
    heads = [b[0] for b in blocks]
    assert _TABLE_A_HEAD in heads, f"第一张表不见了；实得表块头={heads}"
    assert _TABLE_B_HEAD in heads, f"第二张表不见了（被粘到第一张表里）；实得表块头={heads}"
    for block in blocks:
        if block[0] == _TABLE_A_HEAD:
            assert len(block) == 4, f"第一张表被腰斩/粘连；实得行数={len(block)}"
        if block[0] == _TABLE_B_HEAD:
            assert len(block) == 4, f"第二张表被腰斩/粘连；实得行数={len(block)}"


def test_every_table_is_preceded_by_blank_line() -> None:
    """③ 每张表**块的第一行**前面必须有空行（GFM：表格不能打断段落 ⇒ 否则不渲染）。"""
    out = RTA._tail(_fixture(), _LIMIT)
    lines = out.splitlines()
    starts = []
    for index, line in enumerate(lines):
        if not _TABLE_ROW_RE.match(line):
            continue
        if index == 0 or _TABLE_ROW_RE.match(lines[index - 1]):
            continue  # 表块的第一行才检查（表内第二行当然紧跟表头）
        starts.append(index)
    assert starts, "夹具里一张表都没渲染出来（自检失败）"
    for index in starts:
        previous = lines[index - 1]
        assert previous.strip() == "", (
            f"第 {index + 1} 行的表格紧跟在非空行 {previous[:40]!r} 后面 ⇒ "
            "markdown 里这张表不会渲染成表格（表格不能打断段落）"
        )


def test_mutant_old_tail_fails_the_same_assertions() -> None:
    """④ 变异反证：把两个修复点改回去 ⇒ ① ② ③ 必须**同时转红**（本测试有牙）。"""
    #: 📌 变异点清单（实测：变异 1、2 转红；变异 3 是**等价变异**，不是缺陷）：
    #:   ① 去掉「分隔块不许整块丢」守卫 ⇒ 断言① 转红；
    #:   ② 去掉「表前补空行」⇒ 断言③ 转红；
    #:   ③ 把「分隔块」判定放宽成「任意内部散文块」⇒ **不会**转红，
    #:      因为 `_split_table_blocks` 的块类型**严格交替**（散文/表格/散文/表格…）
    #:      ⇒ 任何**内部**散文块的两侧必然都是表格 ⇒ 两个判定式**恒等**。
    broken = _old_tail(_fixture(), _LIMIT)

    # 变异点 A：分隔块被整块丢弃 ⇒ 标题消失（① 转红）
    assert _SEPARATOR_HEADING not in broken, (
        "变异体没有复现「分隔标题消失」⇒ 夹具不敏感，本测试是空跑"
    )
    # 变异点 B：两张表首尾相接 ⇒ 粘成一块（② 转红）
    broken_heads = [b[0] for b in _table_blocks(broken)]
    assert _TABLE_A_HEAD in broken_heads
    assert _TABLE_B_HEAD not in broken_heads, (
        "变异体没有复现「两张表粘连」⇒ 夹具不敏感，本测试是空跑"
    )
    # 变异点 C：表格前不留空行 ⇒ 表格紧跟散文行（③ 转红）
    broken_lines = broken.splitlines()
    glued = [
        index + 1 for index, line in enumerate(broken_lines)
        if _TABLE_ROW_RE.match(line) and index > 0
        and not _TABLE_ROW_RE.match(broken_lines[index - 1])
        and broken_lines[index - 1].strip() != ""
    ]
    assert glued, "变异体没有复现「表格紧跟散文行」⇒ 夹具不敏感，本测试是空跑"
