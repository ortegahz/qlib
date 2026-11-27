"""🔴 2026-11-27 新增（P6 · Design B'）：交付 md 与交付 json 的**竖线转义等价容忍**。

【事故来源】
2026-11-26 17:18 那一轮交付：LLM 在表格单元格正文里写了绝对值记号 ``|均值| 0.13%``
⇒ 渲染器（S5）把**单元格内**的竖线转义成 ``\\|``（GFM 语义：``\\|`` 是正文，不是列分隔符）
⇒ 交付 md 与交付 json 的**事实源**在那一行上**不再逐字相同**
⇒ P6（旧口径：严格逐字比对）判红「整块消失」⇒ **假红**。

【修法】
P6 比对前把 **md 侧**的 ``\\|`` 还原成 ``|``（``_unescape_pipes``，**不复用渲染器函数**
⇒ 不许自证）⇒ 只容忍**编码差异**，**不容忍任何内容差异**。

【本文件钉死七件事（自带夹具，不依赖服务器产物）】
  ① 转义还原的**往返**一致（还原后再还原 = 原文）；
  ② 转义还原**不碰**其它字符（只动 ``\\|``）；
  ③ 反斜杠按 **GFM 奇偶规则**处理（``\\|`` 还原、``\\\\|`` 不还原）；
  ④ 等价容忍：新口径 P6 对「md 转义 / json 裸竖线」必须 **PASS**；
  ⑤ **必要性**：旧严格口径对同一输入必须 **FAIL**（否则这条放宽是多余的）；
  ⑥ **变异反证**（本文件有牙的证明）：
     a. 改一个**非竖线**字符 ⇒ P6 必须 FAIL；
     b. 删掉整块 ⇒ P6 必须 FAIL；
     c. 腰斩掉一行 ⇒ P6 必须 FAIL；
     d. 多插一行 ⇒ P6 必须 FAIL；
     e. D5 变异器（改坏正文一个字）⇒ P6 必须 FAIL；
  ⑦ **不许自证**：断言期望值全部**硬编码**在本文件，不取自被检模块的自述。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

#: `manu/etf_rotation/`（镜像位置 `manu/tests/` 下本变量 = `manu/` ⇒ 两边都能跑）
ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
#: 🔴 `import config` 必须命中**活链** config（`manu/etf_rotation/config.py`）。
#:    镜像位 `manu/tests/` 下 `manu/` 在 sys.path 里 ⇒ `import config` 会命中
#:    `manu/config.py`（**V1 冻结件**）⇒ 它要加载 `<repo>/experiments/ashare_topk_selection/config.py`
#:    （**只在服务器有**）⇒ 本机与服务器行为分叉 ⇒ 假红。
#:    ⇒ 显式把**活链目录**插到 sys.path 最前 ⇒ 两侧行为一致。
_LIVE_CONFIG_DIR = (
    ETF_DIR / "etf_rotation"
    if (ETF_DIR / "etf_rotation" / "config.py").is_file()
    else ETF_DIR
)
#: 先登记 `manu/` ⇒ 探针自己**不会**再把它插到最前（探针只在缺失时才插）
if str(ETF_DIR) not in sys.path:
    sys.path.insert(0, str(ETF_DIR))
if str(_LIVE_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_LIVE_CONFIG_DIR))


def _load_module(name: str, path: pathlib.Path):
    """按**路径**加载模块（探针不在 ``sys.path`` 的默认位置）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载不了模块：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = _load_module("p6_probe_under_test", ETF_DIR / "probes" / "probe_llm_stage_repro.py")

BULL = "多头"

#: **事实源**（交付 json 的原文）：单元格正文里有裸竖线
FACT_ROWS = ["| 指标 | 数值 |", "|---|---|", "| A | x|y| z |"]
#: **展示件**（交付 md）：同一张表，单元格正文里的竖线被渲染器转义成 ``\|``
MD_ROWS = ["| 指标 | 数值 |", "|---|---|", "| A | x\\|y\\| z |"]
#: 交付 md 的完整文本（表块前后各留一行正文 ⇒ 证明不是「整篇就一张表」）
MD_TEXT = "\n".join(["## 多空辩论", ""] + MD_ROWS + ["", "以上。"])


def _p6(md_text: str, fact_rows: list[str]) -> tuple[bool, str]:
    """跑探针 P6（真调用，不复刻逻辑）⇒ 返回 (是否通过, 细节)。"""
    return PROBE._check_debate_tables_present(md_text, {BULL: [fact_rows]}, [])


# --------------------------------------------------------------------------- ① ② ③
def test_转义还原_往返一致():
    """① 还原后的文本再还原一次必须**逐字不变**（还原是幂等的）。"""
    once = PROBE._unescape_pipes(MD_ROWS[2])
    assert once == FACT_ROWS[2], f"还原结果不对：{once!r}"
    assert PROBE._unescape_pipes(once) == once


def test_转义还原_不碰其它字符():
    """② 还原**只**动 ``\\|``；没有转义的行必须**逐字不变**。"""
    for row in (FACT_ROWS[0], FACT_ROWS[1], "## 普通正文", "a|b"):
        assert PROBE._unescape_pipes(row) == row, f"不该被改的行被改了：{row!r}"


def test_反斜杠按GFM奇偶规则处理():
    """③ 1 个反斜杠 ⇒ 还原；2 个反斜杠 ⇒ **不**还原（字面反斜杠 + 列分隔符）。"""
    assert PROBE._unescape_pipes("a\\|b") == "a|b"
    assert PROBE._unescape_pipes("a\\\\|b") == "a\\\\|b"


# --------------------------------------------------------------------------- ④ ⑤
def test_等价容忍_转义后的md_P6必须判绿():
    """④ 交付 md（转义版） vs 交付 json（裸竖线）⇒ P6 必须 PASS。"""
    ok, detail = _p6(MD_TEXT, FACT_ROWS)
    assert ok, f"转义后的交付 md 仍被 P6 判红：{detail}"


def test_必要性_旧严格口径对同一输入必须判红():
    """⑤ 旧口径（严格逐字）必须 FAIL ⇒ 证明「等价容忍」这一放宽是**必要的**。"""
    assert PROBE._find_verbatim_block(MD_ROWS, FACT_ROWS) == [], \
        "旧严格口径竟然命中了 ⇒ 等价容忍是多余的 ⇒ 本次放宽没有理由"


# --------------------------------------------------------------------------- ⑥ 变异反证
def test_变异_改非竖线字符_P6必须判红():
    """⑥a **变异反证**：把正文里的一个字符改坏 ⇒ P6 必须 FAIL（容忍范围不许过宽）。"""
    mutated = MD_TEXT.replace("| A |", "| B |")
    assert mutated != MD_TEXT, "变异点没生效（替换串没命中）"
    ok, detail = _p6(mutated, FACT_ROWS)
    assert not ok, "改坏一个字符后 P6 竟然 PASS ⇒ P6 被改瞎了"
    assert "没有逐字出现" in detail, f"失败理由不对：{detail}"


def test_变异_删掉整块_P6必须判红():
    """⑥b **变异反证**：整块表从交付 md 里消失 ⇒ P6 必须 FAIL（M-N 事故本体）。"""
    mutated = "\n".join(["## 多空辩论", ""] + ["", "以上。"])
    ok, detail = _p6(mutated, FACT_ROWS)
    assert not ok, "整块消失后 P6 竟然 PASS ⇒ P6 抓不到 M-N 事故"
    assert "没有逐字出现" in detail, f"失败理由不对：{detail}"


def test_变异_腰斩掉一行_P6必须判红():
    """⑥c **变异反证**：表块被腰斩（少一行）⇒ P6 必须 FAIL。"""
    mutated = "\n".join(["## 多空辩论", ""] + [MD_ROWS[0], MD_ROWS[1]] + ["", "以上。"])
    ok, detail = _p6(mutated, FACT_ROWS)
    assert not ok, "腰斩掉一行后 P6 竟然 PASS ⇒ P6 抓不到残缺表块"


def test_变异_多插一行_P6必须判红():
    """⑥d **变异反证**：表块**中间**多插一行 ⇒ P6 必须 FAIL（逐字连续 ⇒ 行序/行数都得对）。"""
    mutated = "\n".join(["## 多空辩论", ""] + MD_ROWS[:2] + ["| B | 9 |"] + MD_ROWS[2:] + ["", "以上。"])
    ok, detail = _p6(mutated, FACT_ROWS)
    assert not ok, "多插一行后 P6 竟然 PASS ⇒ P6 只比了前缀"


def test_变异_D5变异器_P6必须判红():
    """⑥e **变异反证**：探针自带的 D5 变异器（改坏正文一个字）⇒ P6 必须 FAIL。"""
    mutated = PROBE._mutate_corrupt_cell_char(MD_TEXT, {BULL: [FACT_ROWS]}, BULL)
    assert mutated != MD_TEXT, "D5 变异没生效"
    ok, detail = _p6(mutated, FACT_ROWS)
    assert not ok, "D5 变异后 P6 竟然 PASS ⇒ D5 负例没有牙"


# --------------------------------------------------------------------------- ⑦
def test_不许自证_探针未调用渲染器函数():
    """⑦ P6 的还原函数**不得**调用渲染器 `_repair_table_pipes`（不许自证）。

    🔴 用 **AST 级**断言（不 import 渲染器 ⇒ 镜像位 `manu/tests/` 也能跑；
       且能区分「代码调用」与「文档里提到」⇒ 只查代码里的引用）。
    """
    src = (ETF_DIR / "probes" / "probe_llm_stage_repro.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    referenced = {
        (node.attr if isinstance(node, ast.Attribute) else node.id)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    assert "_repair_table_pipes" not in referenced, \
        "探针调用了渲染器的 `_repair_table_pipes` ⇒ 断言与被检对象同源 ⇒ 断言失去牙齿"
    # 还原方向：**md 的 \\| ⇒ |**（与渲染器「| ⇒ \\|」方向相反）
    assert PROBE._unescape_pipes("a\\|b") == "a|b"
