"""🔴 2026-11-27 新增（S5）：交付 md 的**表格单元格竖线转义** ＋ P7 的 GFM 口径回归。

【事故来源】
2026-11-26 17:18 那一轮交付 md（服务器 ``probe_llm_stage_repro`` P7 红灯：
「表块行内 `|` 数量不一致 [3, 5]」）：LLM 在单元格正文里写了**绝对值记号**
``|均值| 0.13%`` ⇒ 该行有 **5 个**竖线，同表其它行只有 **3 个** ⇒
  ① markdown 渲染器按 5 列切 ⇒ 多出来的列**整列丢弃**
     ⇒ 用户读到的纪要里这一行的后半段**根本没显示**；
  ② P7 判「块内竖线数不一致」红灯。

【修法】
渲染层（``llm/run_tradingagents.py``）把**单元格内部**的竖线转义成 ``\\|``
（GFM 规定的「正文竖线」写法，渲染出来仍然是 ``|``）⇒ 块内各行列数一致；
探针 P7 改按**未转义**竖线数判定（与 markdown 渲染器的切列口径一致）。

【本文件钉死六件事（自带夹具，不依赖服务器产物）】
  ① 真实事故行转义后，块内各行**未转义**竖线数一致；
  ② 转义**只改编码、不改内容**：把 ``\\|`` 还原成 ``|`` 后与原文逐字相同；
  ③ 修复**幂等**（再修一次不动）；
  ④ **只转义、不造数**：竖线**少于**规范列数的行（真·残行）**原样保留**；
  ⑤ 渲染出口（``_render_md``）已规范化；
  ⑥ **变异反证**（本文件有牙的证明）：
     a. 旧口径（未转义）的 md ⇒ P7 必须 FAIL；
     b. 修复后的 md ⇒ P7 必须 PASS；
     c. 注入一个**未转义**的多余竖线 ⇒ P7 必须 FAIL（证明 P7 没被改瞎）；
     d. 把一行**腰斩**（少一个列分隔符）⇒ P7 必须 FAIL（真残行仍被抓）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

#: `manu/etf_rotation/`（镜像位置 `manu/tests/` 下本变量 = `manu/` ⇒ 两边都能跑）
ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(ETF_DIR) not in sys.path:
    sys.path.insert(0, str(ETF_DIR))


def _load_module(name: str, path: pathlib.Path):
    """按**路径**加载模块（渲染器与探针都不在 ``sys.path`` 的默认位置）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载不了模块：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RTA = _load_module("s5_rta_under_test", ETF_DIR / "llm" / "run_tradingagents.py")
PROBE = _load_module("s5_probe_under_test", ETF_DIR / "probes" / "probe_llm_stage_repro.py")

#: 🔴 **真实事故行**（2026-11-26 交付 md 第 169 行，逐字照抄，不许改写）
ACCIDENT_ROW = (
    "| **ETF 自身风险（med）** | 19 只候选折价中位 0.09%、区间 [−0.27%, +0.27%]、"
    "|均值| 0.13%；60 日年化波动 10.09%~29.72%（中位 15.44%）；跟踪误差代理中位 2.52%。"
    "熊说得对：**跟踪偏离不是零成本**。 |"
)
#: 同一张表里的**正常行**（2 列 ⇒ 3 个竖线）
NORMAL_ROW = "| **换手与成本（med）** | 本轮换手是建仓级而非调仓级。 |"
NORMAL_ROW_2 = "| **策略容量（med）** | 小资金可执行，放大到千万级才非线性。 |"
HEADER_ROW = "| 风险条目 | 论证 |"
SEPARATOR_ROW = "|---|---|"

#: 一张完整的 2 列表（表头 + 分隔行 + 3 行数据 ⇒ 满足 P7 的「行数 ≥ 5」）
#:    其中 1 行是事故行 ⇒ 旧口径下竖线数 = {3, 5}
BROKEN_TABLE = "\n".join(
    [HEADER_ROW, SEPARATOR_ROW, NORMAL_ROW, NORMAL_ROW_2, ACCIDENT_ROW]
)


def _unescaped(line: str) -> int:
    """未转义竖线数（**独立实现**，不复用被测模块的私有函数 ⇒ 期望值不抄被检对象）。"""
    count = 0
    backslashes = 0
    for char in line:
        if char == "\\":
            backslashes += 1
            continue
        if char == "|" and backslashes % 2 == 0:
            count += 1
        backslashes = 0
    return count


def _p7(md_text: str, source_block: list[str]):
    """跑探针 P7（真调用，不复刻逻辑）⇒ 返回 (是否通过, 细节)。"""
    return PROBE._check_table_integrity(md_text, {"多头": [source_block]}, [])


# --------------------------------------------------------------------------- ① ② ③ ④
def test_事故行转义后_块内未转义竖线数一致():
    """① 真实事故行转义后，块内每行**未转义**竖线数必须一致（= 规范列数 3）。"""
    fixed = RTA._repair_table_pipes(BROKEN_TABLE)
    counts = [_unescaped(line) for line in fixed.split("\n")]
    assert counts == [3, 3, 3, 3, 3], f"块内竖线数仍不一致：{counts}"
    # 旧口径（原始文本）确实是不一致的 ⇒ 说明这条夹具真的复刻了事故
    assert sorted({_unescaped(line) for line in BROKEN_TABLE.split("\n")}) == [3, 5]


def test_转义只改编码不改内容():
    """② 把 ``\\|`` 还原成 ``|`` 后，交付 md 必须与原始行**逐字相同**（不许改内容）。"""
    fixed = RTA._repair_table_pipes(BROKEN_TABLE)
    restored = fixed.replace("\\|", "|")
    assert restored == BROKEN_TABLE


def test_修复幂等():
    """③ 已修复的块再修一次必须**逐字节不变**（不许重复加反斜杠）。"""
    once = RTA._repair_table_pipes(BROKEN_TABLE)
    assert RTA._repair_table_pipes(once) == once


def test_竖线少于规范列数的行原样保留_不造数():
    """④ 真·残行（竖线**少于**规范列数）**原样保留** ⇒ 本函数绝不编造单元格。"""
    broken = "\n".join([
        HEADER_ROW,
        SEPARATOR_ROW,
        "| **被腰斩的行** | 只剩半截 |",
        NORMAL_ROW,
        NORMAL_ROW_2,
    ])
    fixed = RTA._repair_table_pipes(broken)
    assert "| **被腰斩的行** | 只剩半截 |" in fixed
    assert fixed.count("|") == broken.count("|")


# --------------------------------------------------------------------------- ⑤
def test_渲染出口已规范化():
    """⑤ ``_render_md`` 的产出里，表块各行**未转义**竖线数必须一致。"""
    payload = {
        "meta": {"ticker": "ETF", "date": "2026-11-26", "analysts": ["a"],
                 "deep_think_llm": "m", "provider": "p", "reasoning_effort": "max",
                 "max_tokens": 32768, "context_path": "ctx.md", "context_chars": 10},
        "investment_debate": {"bull_history": BROKEN_TABLE, "bear_history": ""},
        "risk_debate": {},
        "investment_plan": "",
        "trader_investment_plan": "",
        "final_trade_decision": "",
        "signal": "hold",
        "elapsed_seconds": 1.0,
    }
    md = RTA._render_md(payload)
    blocks: list[list[str]] = []
    buf: list[str] = []
    for line in md.split("\n"):
        if line.lstrip().startswith("|"):
            buf.append(line)
            continue
        if buf:
            blocks.append(buf)
            buf = []
    if buf:
        blocks.append(buf)
    assert blocks, "渲染结果里没有表块 ⇒ 夹具失效"
    for block in blocks:
        counts = {_unescaped(row) for row in block}
        assert len(counts) == 1, f"渲染出口仍存在竖线数不一致的表块：{sorted(counts)}"


# --------------------------------------------------------------------------- ⑥ 变异反证
def test_变异反证_旧口径未转义的md_P7必须判红():
    """⑥a **变异反证**：未转义的旧交付 md ⇒ P7 必须 FAIL（否则 P7 抓不到原事故）。"""
    ok, detail = _p7(BROKEN_TABLE, BROKEN_TABLE.split("\n"))
    assert not ok, "未转义 md 竟然过了 P7 ⇒ P7 没牙"
    assert "数量不一致" in detail, f"失败理由不是「竖线数不一致」：{detail}"


def test_变异反证_修复后的md_P7必须判绿():
    """⑥b 变异反证的对偶：修复后的交付 md ⇒ P7 必须 PASS。"""
    fixed = RTA._repair_table_pipes(BROKEN_TABLE)
    ok, detail = _p7(fixed, BROKEN_TABLE.split("\n"))
    assert ok, f"修复后的 md 仍被 P7 判红：{detail}"


def test_变异反证_注入未转义竖线_P7必须判红():
    """⑥c **变异反证**：往单元格塞一个**未转义**竖线 ⇒ P7 必须 FAIL。

    🔴 这条是「P7 没被改瞎」的证明：P7 改成按未转义数算之后，
       **未转义**的多余竖线依然必须被抓（否则就是被"放宽"了）。
    """
    fixed = RTA._repair_table_pipes(BROKEN_TABLE)
    mutated = fixed.replace("本轮换手是建仓级", "|本轮换手是建仓级")
    assert mutated != fixed, "变异点没生效（替换串没命中）"
    ok, detail = _p7(mutated, BROKEN_TABLE.split("\n"))
    assert not ok, "注入未转义竖线后 P7 竟然 PASS ⇒ P7 被改瞎了"
    assert "数量不一致" in detail, f"失败理由不是「竖线数不一致」：{detail}"


def test_变异反证_腰斩成残行_P7必须判红():
    """⑥d **变异反证**：把一行腰斩（少一个列分隔符）⇒ P7 必须 FAIL（真残行仍被抓）。"""
    fixed = RTA._repair_table_pipes(BROKEN_TABLE)
    cut = fixed.replace(NORMAL_ROW, "| **换手与成本（med）** | 本轮换手是建仓级")
    assert cut != fixed, "变异点没生效"
    ok, detail = _p7(cut, BROKEN_TABLE.split("\n"))
    assert not ok, "腰斩成残行后 P7 竟然 PASS ⇒ P7 抓不到真残行"
