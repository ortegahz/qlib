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

🔴 2026-11-11 12:05 实测事故 ⇒ 本探针再加一条**产物级**断言：
  C.「TA 否决名单零误伤」—— **交付件** `final_rebalance_plan.json` 的
     `ta_screening.rejected` 里**不得出现**同一份交付件里 TA 明确要买的标的
     （买/增持/加仓/建仓/buy）。
     事故实录：交付件 `ta_screening.rejected` = ['515450','512170','563020','159207']，
     而**同一份** `tradingagents_decision.md` 第 47/78/191 行明写
     「| 买 | 515450 | 红利低波50ETF南方 | 16% | …」「515450 **买**（16%…）」
     ⇒ **TA 明确要买 515450，却被当成一票否决** ⇒ 误伤
     ⇒ 5 个席位里 515450 被换成别的标的（512800/515650/512890/516620/512010）
     ⇒ TA 要的红利低波 515450 从计划里消失。
     根因在**散文否决解析的归属**（`llm_final._prose_hits` 把「不建仓」挂到了
     「与 515450 相关性…」里的 515450 上）—— **解析逻辑不在本探针的职责里**；
     本探针只负责把「误伤」变成一条**机器可判、每次跑链都会执行**的闸门。

🔴 2026-11-11 12:28 实测：**闸门自己也会假绿**。交付件 `20261111_122825` 的
   `ta_screening.rejected` 里 515450 是**研究经理层**的否决，而**同一份**决策 md 的
   `## 六、最终交易决定（原始输出）` 段落下明写 `- **515450：条件性买入12%**`
   （层级 **40** 的买侧证据）⇒ 旧口径只看**表格行** ⇒ 看不见 ⇒ 闸门判 `pass`
   ⇒ 假绿 ⇒ 判据补两处：**层级继承祖先标题**（`_md_row_ranks`）
   + **权威段落的列表行也算证据**（`_md_list_directions`）。

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
import re
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
#: 🆕 2026-11-11：**产物级**闸门「TA 否决名单零误伤」的断言名
CHECK_TA_REJECT_ZERO_MISHIT = "TA 否决名单零误伤（TA 明确要买的标的不得被否决）"

#: 方向词前面可能带的修饰（「小买」「重买」…）⇒ 剥掉修饰后再回词表匹配。
#:   🔴 「不买」「不建仓」**不受影响**：它们在 ZERO 词表里精确命中，先被返回了。
_DIR_MODIFIERS = ("小", "重", "轻", "大", "继续", "分批", "试探", "少量")

#: 表格行里「方向列」与「代码列」的相邻搜索窗口（±2 格）
_DIR_WINDOW = 2

#: 散文（列表行）证据的**最低权威层级**：观点层（多头/空头，层级 0）不算裁决。
_PROSE_MIN_RANK = 10

#: 散文证据里「方向词」与代码之间的最大间隔（字）：方向词必须落在**代码之后** ≤ 12 字内
#: （「代码 → 方向」的裁决写法）；代码**前面**的方向词不许回挂（那是「散文归属错位」）。
_PROSE_DIR_WINDOW = 12

#: 方向词在**代码前面**时，只认**紧挨着**的（`买入 515450` / `买入：515450`）；
#: 离得远的方向词（「后续加仓的唯一条件：512170 …」）**不许**回挂。
_PROSE_DIR_BEFORE_WINDOW = 2

#: 否定标记（**不是方向词表**，只判「这个方向被否定」）：
#: 代码与方向词之间出现它 ⇒ 该方向被否定 ⇒ 不作买侧证据
#: （「563020 不应建仓」「563020 不值得建仓20%」⇒ 不是买侧裁决）。
_NEGATION_MARK = "不"

#: markdown 列表行标记：`- ` / `* ` / `N. ` / `N、`
_LIST_MARKER_RE = re.compile(r"^(?:[-*]\s|\d+[.、]\s|\d+、)")

#: 列表行内的**分句符** ⇒ 方向词只算**同句**里那个标的的
#: （「买入 A 5%、试探买入 B 2%」⇒ B 的方向不许挂到 A 头上）。
_LIST_SEGMENT_SPLIT_RE = re.compile(r"[、，；。！？]")


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


def _norm_cell(value: object) -> str:
    """去掉 markdown 强调（`**`）/反引号/空白 ⇒ 只留正文。"""
    text = "" if value is None else str(value)
    return text.replace("**", "").replace("`", "").strip()


def _classify_direction(value: object) -> str | None:
    """把方向字段 / 表格方向格归一化成词表方向词；不是方向词 ⇒ `None`（**不是**买侧）。

    口径（闭集，单一事实源 = `verify_artifacts.LLM_DIRECTION_*` ⇒ 与解析器、语义守卫同表）：
      · 精确命中词表 ⇒ 原样返回；
      · 带修饰前缀（「小买」「重买」）⇒ 剥掉修饰再命中；
      · ASCII 词（Buy / Overweight…）大小写不敏感。

    🔴 **不做前缀匹配**：「买，但仅在数据复核通过后」这种**带条件的半句**是
       熊方的附条件意见，**不是 TA 的裁决** ⇒ 不许当成买侧证据（否则闸门会误报）。
    """
    text = _norm_cell(value)
    if not text:
        return None
    if text in VA.LLM_DIRECTION_ALL:
        return text
    for modifier in _DIR_MODIFIERS:
        if text.startswith(modifier):
            rest = text[len(modifier):]
            if rest in VA.LLM_DIRECTION_ALL:
                return rest
            break
    if text.isascii():
        for word in VA.LLM_DIRECTION_ALL:
            if word.isascii() and word.lower() == text.lower():
                return word
    return None


#: 🔴 2026-11-11（根因修复 · 「unjudged ⇒ na ⇒ 假绿」）：方向**格**允许带**补充说明**。
#:
#: 真实交付件（快照 `20261111_163156`）里 TA 的否决表长这样：
#:     | **515020** | **不买，否决同族替代** | 与512800高度重合… |
#: 旧口径要求方向格**整格精确等于**词表词 ⇒ 这类格子被判「**没有方向证据**」
#: ⇒ 该代码进 `unjudged` ⇒ 闸门判「不适用(na)」⇒ **na ≠ 通过**（闸门自己就是这么写的）
#: ⇒ 但验收脚本把 na 记成 PASS ⇒ **假绿**（2026-11-11 审计 P1-1 实测命中）。
#:
#: ⇒ 修法（**只放开非买侧**）：按分隔符切段后，**只认 ZERO/NEUTRAL/REDUCE** 词段的段。
#: **买侧词一律不放开** —— 「买，但仅在数据复核通过后」是**附条件**意见，
#: 放开会凭空造出买侧证据 ⇒ 把真否决误判成误伤（见 `_classify_direction` 的说明）。
#: ⇒ 本修正只把「判不了」变成「**判得了、且判为非买侧**」，**不会**新增任何误伤判定。
_CELL_SEGMENT_SPLIT_RE = re.compile(r"[，,；;。/、|｜]+")


def _classify_cell_direction(value: object) -> str | None:
    """方向**格**的方向词（容忍补充说明）⇒ 方向词 | None。

    与 `_classify_direction` 的唯一区别：**非买侧**方向词允许「整格前缀 + 补充说明」。
    买侧词仍必须**整格精确命中**（附条件买 ⇒ 不算买侧证据）。
    """
    text = _norm_cell(value)
    if not text:
        return None
    direct = _classify_direction(text)
    if direct:
        return direct
    for segment in _CELL_SEGMENT_SPLIT_RE.split(text):
        segment = segment.strip()
        if not segment:
            continue
        word = _classify_direction(segment)
        if word and word not in VA.LLM_DIRECTION_POS:
            return word
    return None


def _is_buy_side(direction: str | None) -> bool:
    """方向是不是**买侧**（买 / 买入 / 增持 / 加仓 / 建仓 / Buy / Overweight…）。"""
    return bool(direction) and direction in VA.LLM_DIRECTION_POS


#: 🔴 2026-11-11（判据换版 · **权威层级**）：「TA 要买」这件事**分层级**。
#:
#: 真实决策件里**研究经理的提案表**和**Trader 的最终裁决**可以**互相矛盾**
#: —— 那正是「一票否决 + 顺延」这套机制的**设计意图**：
#: 研究经理提案买 159207（4%），Trader 在交易计划里写「不建议保留 4% 象征仓」
#: ⇒ **159207 是真否决**，不是误伤。
#:
#: ⇒ 判据必须是：**买侧证据的权威层级 ≥ 否决的权威层级** 才算误伤
#: （同一层自相矛盾 ⇒ 归属错位；更高层要买 ⇒ 低层的否决本就不该生效；
#:   更低层要买 ⇒ 已被更高层否决 ⇒ 正常）。
#:
#: 层级（低 → 高）：
#:   0  观点（多头/空头/激进派/保守派/中立派）—— 没有否决权，也不构成「要买」
#:   10 研究经理结论（提案层）
#:   20 辩论裁决 / 风险裁决
#:   30 Trader 交易计划
#:   40 最终交易决定（原始输出）
_AUTHORITY_RANKS: tuple[tuple[str, int], ...] = (
    ("最终交易决定", 40),
    ("Trader", 30),
    ("辩论裁决", 20),
    ("风险裁决", 20),
    ("研究经理", 10),
    ("Research Manager", 10),
)

#: `llm_targets[code].who` ⇒ 层级（研究经理=提案层；多头/空头=观点层）
_WHO_RANKS: dict[str, int] = {
    "research": 10,
    "研究经理": 10,
    "bull": 0,
    "bear": 0,
    "多头": 0,
    "空头": 0,
}


def _authority_rank(section: object) -> int:
    """章节标题 ⇒ 权威层级（认不出来 ⇒ 0 = 观点层/未知）。"""
    text = _norm_cell(section)
    if not text:
        return 0
    for marker, rank in _AUTHORITY_RANKS:
        if marker in text:
            return rank
    return 0


def _md_row_ranks(decision_md: str) -> dict[int, int]:
    """每一行所属章节的权威层级 ⇒ `{行号: 层级}`。

    🔴 2026-11-11（假绿修复 · 缺陷 1）：**按 markdown 标题栈语义**算层级 ——
    一个标题在遇到**同级或更高级**的下一个标题之前一直「开着」
    ⇒ 某行的层级 = **所有仍然开着的祖先标题**里 `_authority_rank` 的**最大值**
    （含该行自己所处的标题）。

    旧实现只取「该行之前最后一个标题」的层级 ⇒ **子标题**下的行拿不到祖先的层级
    （`## 六、最终交易决定（原始输出）` 下面再开一级小节 ⇒ 层级被降到 0
    ⇒ 层级 40 的买侧证据看不见 ⇒ 闸门**假绿**）。
    """
    stack: list[tuple[int, int]] = []  # (标题级别, 权威层级)
    out: dict[int, int] = {}
    for line_no, line in enumerate((decision_md or "").splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _authority_rank(m.group(2).strip())))
            continue
        out[line_no] = max((rank for _level, rank in stack), default=0)
    return out


def _plan_direction_ranks(plan: dict, code: str) -> dict[str, int]:
    """计划件里各来源的权威层级 ⇒ `{来源标签: 层级}`。"""
    out: dict[str, int] = {}
    targets = plan.get("llm_targets") or {}
    row = targets.get(code)
    if isinstance(row, dict):
        who = _norm_cell(row.get("who"))
        out["plan.llm_targets"] = _WHO_RANKS.get(who, 10)
    final_targets = (plan.get("llm_final_decision") or {}).get("targets") or {}
    if isinstance(final_targets.get(code), dict):
        out["plan.llm_final_decision.targets"] = 40
    return out


def _md_direction_ranks(decision_md: str, code: str) -> dict[str, int]:
    """决策 md 各行来源的权威层级 ⇒ `{来源标签: 层级}`。"""
    row_ranks = _md_row_ranks(decision_md)
    out: dict[str, int] = {}
    for label, _direction in _md_directions(decision_md, code):
        out[label] = row_ranks.get(int(label.rsplit("L", 1)[-1]), 0)
    return out


def _rejected_source_sections(plan: dict) -> dict[str, str]:
    """否决名单里每只代码的**否决出处章节** ⇒ `{代码: 章节标题}`。"""
    screening = plan.get("ta_screening") or {}
    out: dict[str, str] = {}
    for row in screening.get("rejected") or []:
        if not isinstance(row, dict):
            continue
        code = _norm_cell(row.get("code"))
        if code:
            out[code] = _norm_cell(row.get("source_section"))
    return out


def _rejected_codes(plan: dict) -> list[str]:
    """交付件 `ta_screening.rejected` 里的代码列表。

    兼容两种形态：**字典行**（`{"code": …, "name": …, "reason": …}`，生产形态）
    与**裸字符串**（`["515450", …]`）。
    """
    screening = plan.get("ta_screening") or {}
    codes: list[str] = []
    for row in screening.get("rejected") or []:
        code = _norm_cell(row.get("code")) if isinstance(row, dict) else _norm_cell(row)
        if code and code not in codes:
            codes.append(code)
    return codes


def _plan_directions(plan: dict, code: str) -> list[tuple[str, str]]:
    """计划件里该代码的 LLM 方向证据 ⇒ `[(来源, 方向词)]`。

    两个来源（**都是交付件自带的解析结果**，本闸门不重新解析原文）：
      · `llm_targets[code].direction`                —— 研究经理 / 基准调仓表的裁决；
      · `llm_final_decision.targets[code].direction` —— Trader 的**最终裁决**。
    """
    found: list[tuple[str, str]] = []
    for source, holder in (
        ("plan.llm_targets", plan.get("llm_targets") or {}),
        ("plan.llm_final_decision.targets",
         (plan.get("llm_final_decision") or {}).get("targets") or {}),
    ):
        row = holder.get(code)
        if not isinstance(row, dict):
            continue
        direction = _classify_direction(row.get("direction"))
        if direction:
            found.append((source, direction))
    return found


def _md_table_rows(decision_md: str) -> list[tuple[int, list[str]]]:
    """决策 md 的**表格行** ⇒ `[(行号, 单元格列表)]`（跳过表头分隔行）。"""
    rows: list[tuple[int, list[str]]] = []
    for line_no, line in enumerate((decision_md or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped.strip("|"))]
        if not cells or all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells):
            continue
        rows.append((line_no, cells))
    return rows


def _md_table_directions(decision_md: str, code: str) -> list[tuple[str, str]]:
    """决策 md **表格行**里该代码的方向证据 ⇒ `[(来源, 方向词)]`。

    口径（三条都是刻意收紧的）：
      · **只认表格行** —— 2026-11-11 事故的根因正是**散文归属错位**
        （`llm_final._prose_hits` 把「不建仓」挂到了「与 515450 相关性…」里的
        515450 上）⇒ 拿散文当证据 = 重犯同一个错；
      · **只认「纯代码格」** —— 该格**整个**就是一个 6 位代码 ⇒「与 515450 相关性 0.917」
        这种顺带提到代码的格子**不算**代码格（否则会把别的行的方向挂到它头上）；
      · 方向格 = 代码格**相邻**的格子（方向列在代码列**前**或**后**两种版式都见过）。
    """
    found: list[tuple[str, str]] = []
    for line_no, cells in _md_table_rows(decision_md):
        for index, cell in enumerate(cells):
            if _norm_cell(cell) != code:
                continue
            for offset in range(-_DIR_WINDOW, _DIR_WINDOW + 1):
                if offset == 0:
                    continue
                pos = index + offset
                if not 0 <= pos < len(cells):
                    continue
                direction = _classify_cell_direction(cells[pos])
                if direction:
                    found.append((f"md:L{line_no}", direction))
            break
    return found


def _direction_tokens(text: str) -> list[tuple[int, int, str]]:
    """文本里的方向词 ⇒ `[(起点, 终点, 词)]`（**闭集** = `verify_artifacts.LLM_DIRECTION_ALL`）。

    🔴 **最长优先**：否则「不买」会被拆成「买」⇒ 真否决被当成买侧证据
    （2026-11-11 实测：`- 159207、563020、511990：不买` 会变成「买」⇒ 误伤）。
    """
    words = sorted(VA.LLM_DIRECTION_ALL, key=len, reverse=True)
    out: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        for word in words:
            end = index + len(word)
            if end > len(text):
                continue
            chunk = text[index:end]
            if chunk == word or (word.isascii() and chunk.lower() == word.lower()):
                out.append((index, end, word))
                index = end
                break
        else:
            index += 1
    return out


def _md_list_rows(decision_md: str) -> list[tuple[int, str]]:
    """决策 md 的**列表行** ⇒ `[(行号, 归一化正文)]`（`- ` / `* ` / `N. ` / `N、`）。"""
    rows: list[tuple[int, str]] = []
    for line_no, line in enumerate((decision_md or "").splitlines(), start=1):
        text = _norm_cell(line)
        if not _LIST_MARKER_RE.match(text):
            continue
        rows.append((line_no, text))
    return rows


def _md_list_directions(decision_md: str, code: str) -> list[tuple[str, str]]:
    """决策 md **权威段落里的列表行**中该代码的方向证据 ⇒ `[(来源, 方向词)]`。

    🔴 2026-11-11（假绿修复 · 缺陷 2）：层级 40 的买侧证据写在**列表行**里
    （`- **515450：条件性买入12%**`）⇒ 旧实现只读表格行 ⇒ 看不见 ⇒ **闸门假绿**。
    但散文归属正是老事故根因 ⇒ **不许无差别放开散文**，口径逐条收紧：
      · **只认列表行** —— 普通散文段落**仍然不算证据**（保持上一轮设计意图）；
      · **层级必须 ≥ 10**（研究经理/辩论裁决/风险裁决/Trader/最终交易决定）——
        观点层（多头/空头，层级 0）没有否决权，也不构成裁决；
      · **同一句** —— 方向词只算**同句**里那个标的的
        （「买入 A 5%、试探买入 B 2%」会把 B 的方向挂到 A 头上 ⇒ 分句符切开）；
      · **方向词必须落在代码之后 ≤ 12 字内**（「代码 → 方向」的裁决写法）；
        方向词在代码**前面**时，只认**紧挨着**的（`买入 515450`）——
        离得远的方向词（「后续加仓的唯一条件：512170 …」）**不许**回挂
        （那正是「散文归属错位」）；
      · **同一标的** —— 代码与方向词之间不许再出现别的 6 位代码；
      · **否定不算方向** —— 代码与方向词之间出现否定标记（「不应建仓」）⇒ 跳过；
      · 方向词判定**复用** `_classify_direction`（闭集 = 生产语义守卫的词表）。
    """
    found: list[tuple[str, str]] = []
    row_ranks = _md_row_ranks(decision_md)
    pattern = re.compile(rf"(?<!\d){re.escape(code)}(?!\d)")
    for line_no, text in _md_list_rows(decision_md):
        if row_ranks.get(line_no, 0) < _PROSE_MIN_RANK:
            continue
        for segment in _LIST_SEGMENT_SPLIT_RE.split(text):
            matches = list(pattern.finditer(segment))
            if not matches:
                continue
            tokens = _direction_tokens(segment)
            for match in matches:
                for start, end, word in tokens:
                    if start >= match.end():
                        gap = segment[match.end():start]
                        scope = gap
                        if len(gap) > _PROSE_DIR_WINDOW:
                            continue
                    elif end <= match.start():
                        gap = segment[end:match.start()]
                        scope = segment[:start]
                        if len(gap) > _PROSE_DIR_BEFORE_WINDOW:
                            continue
                    else:
                        continue
                    if re.search(r"\d{6}", gap):
                        continue
                    if _NEGATION_MARK in scope:
                        continue
                    direction = _classify_direction(word)
                    if direction:
                        found.append((f"md:L{line_no}", direction))
    return found


def _md_directions(decision_md: str, code: str) -> list[tuple[str, str]]:
    """决策 md 里该代码的方向证据 ⇒ `[(来源, 方向词)]`（表格行 + 权威段落的列表行）。"""
    return _md_table_directions(decision_md, code) + _md_list_directions(decision_md, code)


def find_ta_reject_mis_hits(plan: dict, decision_md: str | None = None) -> dict:
    """**产物级**闸门：交付件 `ta_screening.rejected` 里**不得有 TA 明确要买的标的**。

    判据（闭集，两条证据**任意一条成立**，且**层级够高**才 FAIL）：
      A. 计划件里的 LLM 方向 —— `llm_targets[code].direction` /
         `llm_final_decision.targets[code].direction` ∈ 买侧（买/增持/加仓/建仓/buy…）；
      B. 决策 md **表格行**里该代码的方向格 ∈ 买侧；
      C. 决策 md **权威段落（层级 ≥ 10）里的列表行**里该代码的裁决 ∈ 买侧
         （`- **515450：条件性买入12%**` ⇒ 见 `_md_list_directions` 的逐条口径）。
    卖侧 / 不买 / 未持有方向**不算**误伤。
    两条证据**都拿不到** ⇒ 该代码**不适用（≠ 通过）** ⇒ 整个闸门判「不适用」。

    🔴 2026-11-11 判据换版（**权威层级**）：买侧证据必须**层级 ≥ 否决的层级**
    （见 `_AUTHORITY_RANKS`）才算误伤。理由：研究经理的**提案表**与 Trader 的
    **最终裁决**互相矛盾，是「一票否决 + 顺延」这套机制的**设计意图**
    （研究经理提案买 159207 4%，Trader 写「不建议保留 4% 象征仓」⇒ **真否决**）。
    ⇒ 只看「有没有出现过买侧证据」会把**真否决**也判红（2026-11-11 实测：
      159207 被误报）⇒ 闸门必须按层级比。

    🔴 2026-11-11 12:28（**假绿**修复）：交付件 `20261111_122825` 的
    `ta_screening.rejected` 含 515450（研究经理层否决），而**同一份**决策 md 的
    `## 六、最终交易决定（原始输出）` 段落下明写 `- **515450：条件性买入12%**`
    （层级 **40** 的买侧证据）⇒ 旧实现只看**表格行** ⇒ 看不见 ⇒ 闸门判 `pass`
    ⇒ **假绿**（真否决里混着 TA 明写要买的标的）。
    ⇒ 证据口径补上 C（**权威段落的列表行**），层级口径补上**祖先继承**
    （见 `_md_row_ranks`）。
    
    返回 `{"status": "pass"|"fail"|"na", "mis_hits": [...], "unjudged": [...],
    "rejected": [...], "detail": "..."}`。
    """
    screening = plan.get("ta_screening") or {}
    rejected = _rejected_codes(plan)
    if screening.get("enabled") is not True:
        return {
            "status": "na", "mis_hits": [], "unjudged": [], "rejected": rejected,
            "detail": f"交付件 ta_screening.enabled={screening.get('enabled')!r}"
                      " ⇒ 本轮没跑 TA 合规筛选 ⇒ 无从校验（**不适用 ≠ 通过**）",
        }
    mis_hits: list[dict] = []
    unjudged: list[str] = []
    veto_sections = _rejected_source_sections(plan)
    for code in rejected:
        evidence = _plan_directions(plan, code) + _md_directions(decision_md or "", code)
        buy_evidence = [(src, d) for src, d in evidence if _is_buy_side(d)]
        if not evidence:
            unjudged.append(code)
            continue
        if not buy_evidence:
            continue
        # 🔴 2026-11-11 判据换版：**按权威层级比**，不按「有没有买过」比。
        #    否决出处章节认不出来 ⇒ 层级 0 ⇒ 任何买侧证据都算矛盾（最保守）。
        veto_rank = _authority_rank(veto_sections.get(code, ""))
        ranks = _plan_direction_ranks(plan, code)
        ranks.update(_md_direction_ranks(decision_md or "", code))
        buy_rank = max((ranks.get(src, 0) for src, _d in buy_evidence), default=-1)
        if buy_rank >= veto_rank:
            mis_hits.append({
                "code": code,
                "evidence": buy_evidence,
                "veto_rank": veto_rank,
                "buy_rank": buy_rank,
                "veto_section": veto_sections.get(code, ""),
            })
    if mis_hits:
        hits = "、".join(
            f"{hit['code']}（" + "、".join(f"{src}={d}" for src, d in hit["evidence"]) + "）"
            for hit in mis_hits
        )
        return {
            "status": "fail", "mis_hits": mis_hits, "unjudged": unjudged,
            "rejected": rejected,
            "detail": f"误伤 {len(mis_hits)} 只：{hits}｜否决名单={rejected}"
                      "｜判据：TA 明确要买（买/增持/加仓/建仓/buy）的标的出现在 "
                      "ta_screening.rejected ⇒ **误伤**"
                      "（2026-11-11 事故：TA 明写「买」的 515450 被当成否决 ⇒ "
                      "5 席变成 512800/515650/512890/516620/512010，红利低波 515450 消失）",
        }
    if unjudged:
        return {
            "status": "na", "mis_hits": [], "unjudged": unjudged, "rejected": rejected,
            "detail": f"否决名单={rejected}｜**两条证据都拿不到**的代码：{unjudged}"
                      "（计划件没记方向、决策 md 表格行里也没有方向格）"
                      "⇒ **不适用 ≠ 通过**（无法校验 ≠ 可以放过）",
        }
    return {
        "status": "pass", "mis_hits": [], "unjudged": [], "rejected": rejected,
        "detail": f"否决名单={rejected or '（空）'}｜逐条取到 LLM 方向证据"
                  "（plan.llm_targets / plan.llm_final_decision.targets / 决策 md 表格行）"
                  "⇒ 无一只落在买侧 ⇒ 零误伤",
    }


def _drop_rejected_codes(plan: dict, codes: list[str]) -> dict:
    """把给定代码从交付件副本的 `ta_screening.rejected` 里摘掉（其余原样保留）。"""
    out = json.loads(json.dumps(plan, ensure_ascii=False))
    screening = out.get("ta_screening") or {}
    screening["rejected"] = [
        row for row in (screening.get("rejected") or [])
        if _norm_cell(row.get("code") if isinstance(row, dict) else row) not in codes
    ]
    out["ta_screening"] = screening
    return out


def _inject_rejected_code(plan: dict, code: str) -> dict:
    """把一个代码塞进交付件副本的 `ta_screening.rejected`（模拟「误伤被写进否决名单」）。"""
    out = json.loads(json.dumps(plan, ensure_ascii=False))
    screening = out.get("ta_screening") or {}
    rows = list(screening.get("rejected") or [])
    rows.append({
        "code": code, "name": "", "reason": "（变异反证：人为塞进否决名单）",
        "source_section": "变异",
    })
    screening["rejected"] = rows
    screening.setdefault("enabled", True)
    out["ta_screening"] = screening
    return out


def _pick_buy_side_code(plan: dict, excluded: list[str]) -> str | None:
    """从 `llm_targets` 里挑一个**买侧**代码（变异反证用；挑不到 ⇒ 返回 None）。"""
    for code, row in (plan.get("llm_targets") or {}).items():
        if code in excluded or not isinstance(row, dict):
            continue
        if _is_buy_side(_classify_direction(row.get("direction"))):
            return code
    return None


def _inject_buy_side_target(plan: dict, code: str) -> dict:
    """给交付件副本的 `llm_targets` 塞一条**买侧**方向（变异反证的兜底夹具）。"""
    out = json.loads(json.dumps(plan, ensure_ascii=False))
    targets = out.get("llm_targets") or {}
    targets[code] = {
        "direction": "买", "current_pct": 0.0, "target_pct": 10.0,
        "reason": "（变异反证：自造的买侧夹具）", "who": "research", "pnl_pcts": [],
    }
    out["llm_targets"] = targets
    return out


def _mutate_baseline(plan: dict) -> list[tuple[str, dict]]:
    """构造若干「故意做坏」的交付件变体。

    🔴（2026-11-09 独立审计实证）修：**负例不许硬编码生产数据的偶然形态**。
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
    _parent = run_root.parent
    #: 🔴 2026-11-11 修（真机实跑踩到）：scratch **不许**落在 `snapshots/` 里面。
    #:    那里放的是**不可变快照** ⇒ ① 探针崩了会留残留目录；
    #:    ② 更糟：残留目录里**也有** manifest + 报告两份文件
    #:    ⇒ `run_gates.sh` 的「最新完整快照」扫描会把它**选中当交付件根**
    #:    （`_` 的 ASCII 比数字大 ⇒ 倒序排序时它排第一）⇒ 闸门断言对象被指到临时目录。
    #:    ⇒ 统一放到**结果根**下（快照根的上一级）。
    _base = _parent.parent if _parent.name == "snapshots" else _parent
    scratch = _base / "_probe_guards"
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

    # 🔴 修（2026-11-09 独立审计实证）：**适用性判据只查「决策文件在不在」是漏判**。
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
    passes = 0
    not_applicable = 0

    def _record(ok: bool, tag: str, detail: str) -> None:
        nonlocal failures, passes
        if not ok:
            failures += 1
        else:
            passes += 1
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {tag}")
        lines.append(f"        {detail}")

    def _record_gate(verdict: dict, tag: str) -> None:
        """三值判定（pass / fail / na）的记录器。

        🔴 「不适用」**既不是通过也不是失败** ⇒ 单列一行 `[N/A]`，
           **不进 PASS 计数**（否则 run_gates.sh 的断言 PASS 计数会被灌水）。
        """
        nonlocal failures, passes, not_applicable
        status = verdict.get("status")
        if status == "fail":
            failures += 1
            lines.append(f"[FAIL] {tag}")
        elif status == "na":
            not_applicable += 1
            lines.append(f"[N/A] {tag}")
        else:
            passes += 1
            lines.append(f"[PASS] {tag}")
        lines.append(f"        {verdict.get('detail', '')}")

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

    # =============================================================================
    # 🔴 2026-11-11：**产物级**闸门「TA 否决名单零误伤」（断言对象 = 交付件本身）
    # =============================================================================
    # 事故实录（2026-11-11 12:05 实测）：交付件 `final_rebalance_plan.json` 的
    # `ta_screening.rejected` = ['515450','512170','563020','159207']，
    # 而**同一份交付件**的 `tradingagents_decision.md` 第 47/78/191 行明写
    # 「| 买 | 515450 | 红利低波50ETF南方 | 16% | …」「515450 **买**（16%…）」
    # ⇒ **TA 明确要买 515450，却被当成一票否决** ⇒ 误伤
    # ⇒ 5 个席位里 515450 被换成了别的标的（512800/515650/512890/516620/512010）
    # ⇒ TA 要的红利低波 515450 从计划里消失。
    # 根因在**散文否决解析的归属**（`llm_final._prose_hits` 把「不建仓」挂到了
    # 「与 515450 相关性…」里的 515450 上）—— 解析逻辑由主 agent 负责修；
    # **本探针只负责把「误伤」变成一条机器可判、每次跑链都会执行的闸门**。
    _decision_md_text = (scratch / "tradingagents_decision.md").read_text(encoding="utf-8")

    # ---- G-TA1 主断言：**未变异的交付件** ⇒ 否决名单里不得有买侧标的 ----
    _verdict = find_ta_reject_mis_hits(baseline_plan, _decision_md_text)
    _record_gate(
        _verdict,
        "G-TA1 产物级闸门：交付件 ta_screening.rejected 里**不得**有 TA 明确要买"
        "（买/增持/加仓/建仓/buy）的标的 ⇒ 2026-11-11 那次「TA 明写『买』的 515450 "
        "被当成否决 ⇒ 5 席被顺延成 512800/515650/512890/516620/512010」的误伤在这条被拦下",
    )

    # ---- G-TA1a 阳性对照：把买侧代码从否决名单摘掉 ⇒ 闸门必须 PASS（不许误报）----
    _mis_codes = [hit["code"] for hit in _verdict.get("mis_hits") or []]
    _clean_plan = _drop_rejected_codes(baseline_plan, _mis_codes)
    _clean_verdict = find_ta_reject_mis_hits(_clean_plan, _decision_md_text)
    _record_gate(
        _clean_verdict,
        "G-TA1a 阳性对照：把买侧代码从否决名单里摘掉（其余合法否决必须留着）"
        "⇒ 零误伤闸门必须 PASS（闸门不许把合法否决也判红）",
    )

    # ---- G-TA1b 变异负例：把一个买侧代码塞回否决名单 ⇒ 闸门必须 FAIL（闸门有牙）----
    _probe_buy_code = _pick_buy_side_code(_clean_plan, [])
    _mut_plan = _clean_plan
    if _probe_buy_code is None:
        # 兜底夹具：交付件里**一个买侧代码都没有** ⇒ 自造一条
        # （🔴 不许因为「无从构造」就把这条负例空转过去 ⇒ 闸门必须永远有牙）。
        _probe_buy_code = "599999"
        _mut_plan = _inject_buy_side_target(_mut_plan, _probe_buy_code)
    _mut_plan = _inject_rejected_code(_mut_plan, _probe_buy_code)
    _mut_verdict = find_ta_reject_mis_hits(_mut_plan, _decision_md_text)
    _record(
        _mut_verdict.get("status") == "fail",
        "G-TA1b 变异负例：把一个 llm_targets 买侧代码塞进 ta_screening.rejected"
        " ⇒ 零误伤闸门必须 FAIL（否则闸门没牙）",
        f"变异代码={_probe_buy_code}｜变异后判定={_mut_verdict.get('status')}（期望 fail）"
        f"｜{_mut_verdict.get('detail', '')}",
    )
    # 还原计划件（后面的 N1~N6 会各自写自己的变异体）
    plan_path.write_text(json.dumps(baseline_plan, ensure_ascii=False, indent=2), encoding="utf-8")

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
    #: 🔴 2026-11-11 修（真机实跑踩到）：`--skip-llm` 建的快照**本来就没有**这两个上下文文件
    #:    ⇒ 旧口径直接 `read_text()` ⇒ **FileNotFoundError 崩掉整个探针**
    #:    （守卫崩了 = 闸门红，而且红得没有信息量）⇒ 现在「存在才备份」，
    #:    断言本身**不变**（链必须判 FAIL：不确定 ≠ 确定新鲜）。
    meta_existed = meta_path.exists()
    meta_backup = meta_path.read_text(encoding="utf-8") if meta_existed else None
    meta_path.unlink(missing_ok=True)
    exit_code, verdicts = _run_verify(scratch, project_dir)
    chain_ok = verdicts.get("sha1 链新鲜（报告→context→决策）") is False and exit_code != 0
    _record(chain_ok, "N8 删掉 ta_context_meta.json ⇒ sha1 链无法校验 ⇒ 必须 FAIL",
            f"exit={exit_code}｜sha1 链判定={verdicts.get('sha1 链新鲜（报告→context→决策）')}"
            "（期望 False：不确定 ≠ 确定新鲜）"
            + ("｜（该交付件本来就没有上下文 meta ⇒ 变异前已缺失）" if not meta_existed else ""))
    if meta_existed:
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
    print(f"合计 {passes + failures} 项：PASS {passes} / FAIL {failures}"
          + (f" / 不适用 {not_applicable}" if not_applicable else ""))
    print("=" * 78)
    # 🔴 退出码契约：0=通过 / 1=失败 / **2=不适用**（不适用 ≠ 通过）
    if failures:
        return 1
    return 2 if not_applicable else 0


if __name__ == "__main__":
    raise SystemExit(main())
