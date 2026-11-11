"""TA（TradingAgents）= **合规陪审团**：3.5 重合度筛查必须**明确授权**一票否决权。

【背景】架构文档要求 TA 承担三项职责：
① **黑天鹅一票否决**（成分股突发财务/政策风险）；
② **主题过度集中预警**（前两名若底层资产高度重合 ⇒ TA 指出，并**强制规则层顺延
   挑选下一个不同主题的资产**）；
③ 极端行情下的仓位建议（Beta 避险）——由主 agent 负责，本测试不碰。

🔴 2026-11-11 实测缺陷（本次改造的起因）
`manu/etf_rotation/llm/build_ta_context.py` 的 3.5 小节写着「**只供你审查参考**、
**不改变规则层判定**」⇒ **TA 拿到表却被明确告知「只是参考」⇒ 事实上没有被授权
按重合度/主题集中行使一票否决权** ⇒ 职责 ② 落不了地；
`## 7. 任务` 也只问了三个问题，**没告诉** TA 它是陪审团、有哪些权力、
否决一只之后规则层会怎么处理。

本测试钉死（不许为了变绿放宽任何断言）：
① 3.5 小节**不再**出现否定授权措辞，且**出现**「否决 / 顺延 / 不同主题」等授权语义；
   规则层阈值（0.90 / 0.95）与更宽预警阈值（0.50 / 0.80）**一个都不许丢**；
② 3.5 表格的**表头与数据行**与**改写前**逐字一致（表格列与数字不许动）；
③ `## 7. 任务` 里必须讲清：陪审团定位 / 一票否决 / 不得推荐池外标的 /
   不得自行定股数 / 权益敞口方向性建议（写明数值与依据）/ 指向 3.5；
④ context 全文**不含 "None"**（prompt 终检铁律：None 会被 LLM 当成「上游退化」）；
⑤ **变异负例**：抽掉新增的授权引言 / 7.1 小节 ⇒ 对应断言**必须转红**（证明测试有牙）。

【「改写前 / 改写后」怎么比】
用 `ast` 在**内存里**把模块源码**改回改写前的样子**（3.5 引言换回旧文案 +
7.1 小节整块删掉）⇒ exec 出「旧实现」⇒ 同一夹具下渲染两遍上下文
⇒ 断言 **3.5 表格行逐字相同**（改写只许动引言文字与新增小节）。
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

_ETF_DIR = Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

#: 被测模块（真身路径）
_MODULE_PATH = _ETF_DIR / "llm" / "build_ta_context.py"

#: 真实产物（2026-11-11 真机跑批快照）—— 有则用真产物回归，没有就跳过
_REAL_SNAPSHOT = Path(
    "/media/manu/1TB-Volume/workspace/etf_rotation_20261110/server_runs_etf_rotation"
    "/snapshots/20261111_122825"
)

_ASOF = "2026-11-09"

#: 「改写前」的 3.5 引言（**逐字**取自改写前的实现）⇒ 变异负例 + 改写前后对比都用它
_OLD_OVERLAP_INTRO = (
    "> 规则层已按「**成分重叠 ≥ 0.90 或 120 日相关性 ≥ 0.95**」逐对拦截"
    "（冲突对不共存）；下表是**更宽口径**的预警"
    "（成分重叠 ≥ 0.50 或 120 日相关性 ≥ 0.80 或同族），"
    "**只供你审查参考**，**不改变规则层判定**。"
)

#: 改写前 3.5 表格的**表头 + 数据行**（金标准：改写只许动引言与新增小节，表格一字不许改）
_EXPECTED_TABLE_ROWS = [
    "| 代码A | 名称A | 代码B | 名称B | 成分重叠系数 | 120 日相关性 | 同族 | 规则层冲突对 |",
    "|:--|:--|:--|:--|--:|--:|:--:|:--:|",
    "| 512800 | 银行ETF华宝 | 515020 | 银行ETF华夏 | 0.617 | 0.912 | 是 | 否 |",
    "| 513330 | 恒生互联网ETF华夏 | 515450 | 红利低波50ETF南方 | 0.512 | 0.843 | 否 | 否 |",
    "| 512800 | 银行ETF华宝 | 513330 | 恒生互联网ETF华夏 | 0.944 | 0.271 | 否 | **是** |",
]

#: 合成夹具的重合度筛查结果（三个数字口径各不相同 ⇒ 表格里每一格都能被钉死）
_PAIRS = [
    {"a": "512800", "b": "515020", "a_name": "银行ETF华宝", "b_name": "银行ETF华夏",
     "weight_overlap": 0.617, "corr120": 0.912, "theme_a": "银行", "theme_b": "银行",
     "same_theme": True, "rule_conflict": False, "screen_flag": True},
    {"a": "513330", "b": "515450", "a_name": "恒生互联网ETF华夏", "b_name": "红利低波50ETF南方",
     "weight_overlap": 0.512, "corr120": 0.843, "theme_a": "港股互联网", "theme_b": "红利",
     "same_theme": False, "rule_conflict": False, "screen_flag": True},
    {"a": "512800", "b": "513330", "a_name": "银行ETF华宝", "b_name": "恒生互联网ETF华夏",
     "weight_overlap": 0.944, "corr120": 0.271, "theme_a": "银行", "theme_b": "港股互联网",
     "same_theme": False, "rule_conflict": True, "screen_flag": True},
]


# ---------------------------------------------------------------- 断言（可复用 ⇒ 变异负例能直接调）
def _assert_3_5_authorized(section: str) -> None:
    """3.5 小节必须**明确授权**一票否决权（否定授权措辞一律不许出现）。"""
    for bad in ("只供你审查参考", "供审查参考", "不改变规则层判定", "仅供审查"):
        assert bad not in section, f"3.5 小节仍是否定授权措辞：{bad!r}"
    for good in ("一票否决", "否决", "顺延", "不同主题"):
        assert good in section, f"3.5 小节缺少授权语义：{good!r}"
    # 🔴 事实不许丢：规则层硬拦阈值 + 更宽口径预警阈值
    for fact in ("0.90", "0.95", "0.50", "0.80"):
        assert fact in section, f"3.5 小节丢了阈值事实：{fact!r}"


def _assert_task_duties(section: str) -> None:
    """`## 7. 任务` 必须讲清：陪审团定位 / 一票否决 / 权力边界 / 仓位建议口径。"""
    assert "### 7.1" in section, "任务节里没有新增的 7.1 小节"
    for kw in ("合规陪审团", "不是操盘手", "一票否决", "候选池之外", "买多少股",
               "权益敞口", "数值与依据", "3.5"):
        assert kw in section, f"任务节缺少关键语义：{kw!r}"


# ---------------------------------------------------------------- 文本工具
def _section(text: str, start_prefix: str, end_prefix: str) -> str:
    """截出 context 里的一个小节（从 `start_prefix` 行到 `end_prefix` 行之前）。"""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(start_prefix)]
    assert starts, f"context 里找不到小节起始 {start_prefix!r}"
    i = starts[0]
    j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith(end_prefix)),
             len(lines))
    return "\n".join(lines[i:j])


def _table_rows(section: str) -> list[str]:
    """小节里的 markdown 表格行（表头 + 分隔行 + 全部数据行）。"""
    return [line for line in section.splitlines() if line.startswith("|")]


# ---------------------------------------------------------------- 源码手术（造「改写前」与「变异体」）
def _current_source() -> str:
    return _MODULE_PATH.read_text(encoding="utf-8")


def _build_body(tree: ast.Module) -> list[ast.stmt]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build":
            return node.body
    raise AssertionError("找不到 build()（实现变了 ⇒ 本测试要跟着改）")


def _add_literal(stmt: ast.stmt) -> str | None:
    """`add("字面量")` 形态的语句 ⇒ 返回字面量；其它形态 ⇒ None。"""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    if not isinstance(call.func, ast.Name) or call.func.id != "add" or not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _parse() -> ast.Module:
    return ast.parse(_current_source())


def _iter_stmt_lists(stmts: list[ast.stmt]):
    """遍历语句树里的**每一层**语句列表（含 `if` / `for` 的嵌套块）。"""
    for st in stmts:
        yield stmts
        for field in ("body", "orelse", "finalbody"):
            sub = getattr(st, field, None)
            if isinstance(sub, list):
                yield from _iter_stmt_lists(sub)


def _find_add(stmts: list[ast.stmt], prefix: str):
    """找 `add("prefix…")` 这条语句 ⇒ 返回 (所在列表, 下标)；找不到 ⇒ (None, None)。"""
    for lst in _iter_stmt_lists(stmts):
        for i, stmt in enumerate(lst):
            lit = _add_literal(stmt)
            if lit is not None and lit.startswith(prefix):
                return lst, i
    return None, None


def _replace_intro(body: list[ast.stmt]) -> None:
    """把 3.5 引言换回「改写前」的文案（**表格与其它小节一字不动**）。"""
    lst, at = _find_add(body, "> 规则层已按")
    assert lst is not None, "找不到 3.5 引言（实现变了 ⇒ 本测试要跟着改）"
    lst[at] = ast.Expr(value=ast.Call(
        func=ast.Name(id="add", ctx=ast.Load()),
        args=[ast.Constant(value=_OLD_OVERLAP_INTRO)], keywords=[]))


def _source_before_rewrite() -> str:
    """**改写前**的源码：3.5 引言换回旧文案 + 删掉 7.1 小节（其余一字不动）。"""
    tree = _parse()
    body = _build_body(tree)
    _replace_intro(body)
    _strip_task_duties(body)
    return ast.unparse(tree)


def _source_without_intro_authorization() -> str:
    """变异体 A：只把 3.5 引言换回旧文案（**表格与其它小节一字不动**）。"""
    tree = _parse()
    body = _build_body(tree)
    _replace_intro(body)
    return ast.unparse(tree)


def _strip_task_duties(body: list[ast.stmt]) -> None:
    """把 7.1 小节整块删掉（从 `### 7.1` 到最后一条「仓位建议」要点，含头含尾）。"""
    start = _find_add(body, "### 7.1")
    end = _find_add(body, "- **仓位建议")
    assert start[0] is not None and end[0] is not None, (
        "找不到 7.1 小节或它的结束锚点（实现变了 ⇒ 本测试要跟着改）"
    )
    assert start[0] is body and end[0] is body and start[1] < end[1], (
        "7.1 小节必须与任务节同级且首尾有序（实现变了 ⇒ 本测试要跟着改）"
    )
    del body[start[1]:end[1] + 1]


def _source_without_task_duties() -> str:
    """变异体 B：只删掉 7.1 小节（3.5 引言保持授权版）。"""
    tree = _parse()
    _strip_task_duties(_build_body(tree))
    return ast.unparse(tree)


def _exec_source(source: str, name: str) -> types.ModuleType:
    """把源码 exec 成模块对象（`__file__` 指向真身 ⇒ `import account/config/overlap` 正常）。"""
    module = types.ModuleType(name)
    module.__file__ = str(_MODULE_PATH)
    exec(compile(source, str(_MODULE_PATH), "exec"), module.__dict__)
    return module


# ---------------------------------------------------------------- 夹具
def _write_fixture(tmp_path: Path) -> Path:
    """合成一份「候选池里有银行 + 港股互联网 + 红利」的报告 + 调仓建议。"""
    report = {
        "meta": {"asof": _ASOF, "params": {"pool_mode": "e_covered"}},
        "stock_scores": {"n_stocks": 120, "unique_values": 118,
                         "degenerate_check": {"passed": True},
                         "mean": 0.0123, "std": 0.45},
        "rank_ic": {"horizons": {
            "5": {"status": "ok", "mean_rank_ic": 0.0481, "icir": 0.447,
                  "t_stat": 2.1, "n_cross_sections": 68},
            "20": {"status": "ok", "mean_rank_ic": 0.0311, "icir": 0.512,
                   "t_stat": 1.9, "n_cross_sections": 61},
        }},
        "filter_stats": {"stages": [
            {"stage": "filter_1_liquidity", "n_in": 1664, "n_out": 292},
            {"stage": "filter_2_3_dedup_and_theme", "n_in": 21, "n_out": 5},
        ]},
        "candidates": [
            {"code": "512800", "name": "银行ETF华宝", "theme_id": "银行",
             "final_score": 1.1594, "e_score": 0.4177, "mom120": 0.1288,
             "coverage": 1.0, "selected": True},
            {"code": "515020", "name": "银行ETF华夏", "theme_id": "银行",
             "final_score": 1.2025, "e_score": 0.4166, "mom120": 0.1408,
             "coverage": 1.0, "selected": False},
            {"code": "515450", "name": "红利低波50ETF南方", "theme_id": "红利",
             "final_score": 0.6576, "e_score": 0.3353, "mom120": 0.0572,
             "coverage": 1.0, "selected": True},
            {"code": "513330", "name": "恒生互联网ETF华夏", "theme_id": "港股互联网",
             "final_score": 0.5120, "e_score": 0.2110, "mom120": 0.0311,
             "coverage": 1.0, "selected": False},
        ],
        "next_rebalance": {"target_date": "2026-12-01", "trading_days_until": 15},
        "risk_disclosure": ["上游个股分数横截面存在退化的可能。"],
    }
    rec = {
        "pool_mode": "e_covered", "rebalance_every_n_days": 20,
        "defensive_mode": "开启", "brake_mode": "无",
        "relaxed_absolute_momentum_filter": "关闭",
        "holdings": [
            {"code": "512800", "name": "银行ETF华宝", "target_weight": 0.16,
             "final_score": 1.1594, "alpha_score": 0.4177, "mom120": 0.1288,
             "above_ma60": True, "coverage": 1.0, "theme_id": "银行",
             "is_defensive": False},
            {"code": "515450", "name": "红利低波50ETF南方", "target_weight": 0.16,
             "final_score": 0.6576, "alpha_score": 0.3353, "mom120": 0.0572,
             "above_ma60": True, "coverage": 1.0, "theme_id": "红利",
             "is_defensive": False},
        ],
        "trades": [
            {"action": "买入", "code": "515450", "name": "红利低波50ETF南方",
             "target_weight": 0.16, "current_weight": 0.0, "amount": 4776.0},
            {"action": "卖出", "code": "512800", "name": "银行ETF华宝",
             "target_weight": 0.16, "current_weight": 0.2254, "amount": -1851.29},
        ],
        "stats": {"n_entry_ma_reject": 131, "forced_exit_codes": []},
    }
    (tmp_path / "etf_analysis_report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "rebalance_recommendation.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "etf_analysis_report.md").write_text("# 报告\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def overlap_stub(monkeypatch):
    """把重合度计算**打桩**（本机没有成分权重表 ⇒ 不打桩 3.5 小节会整节省略）。"""
    import overlap as overlap_mod
    monkeypatch.setattr(
        overlap_mod, "load_overlap_inputs",
        lambda **kw: (pd.DataFrame({"etf_code": ["512800"]}),
                      pd.DataFrame({"date": [_ASOF], "code": ["512800"], "close": [0.87]})))
    monkeypatch.setattr(
        overlap_mod, "compute_overlap_pairs", lambda **kw: list(_PAIRS))


def _render(module: types.ModuleType, run_root: Path) -> str:
    text, _meta = module.build(
        run_root, run_root / "etf_analysis_report.md", None, None, None, None)
    return text


# ---------------------------------------------------------------- 用例
def test_3_5_小节必须明确授权一票否决权(tmp_path: Path, overlap_stub):
    """3.5 小节：否定授权措辞消失 ⇒ 换成「否决 / 顺延 / 不同主题」的授权语义。"""
    run_root = _write_fixture(tmp_path)
    text = _render(_exec_source(_current_source(), "btc_current"), run_root)
    _assert_3_5_authorized(_section(text, "## 3.5.", "## 4."))


def test_任务节必须讲清陪审团定位与权力(tmp_path: Path, overlap_stub):
    """`## 7. 任务` 里必须新增一小节：定位 / 一票否决 / 权力边界 / 仓位建议口径。"""
    run_root = _write_fixture(tmp_path)
    text = _render(_exec_source(_current_source(), "btc_current"), run_root)
    _assert_task_duties(_section(text, "## 7. 任务", "---"))


def test_context_全文不得出现空值字面量(tmp_path: Path, overlap_stub):
    """prompt 终检铁律：context 全文不许出现 "None"（会被 LLM 当成「上游数据缺失」）。"""
    run_root = _write_fixture(tmp_path)
    text = _render(_exec_source(_current_source(), "btc_current"), run_root)
    assert "None" not in text, "context 里出现了 None"


def test_3_5_表格行改写前后逐字一致(tmp_path: Path, overlap_stub):
    """改写只许动引言文字与新增小节 ⇒ 3.5 表格的表头与数据行**逐字不变**。"""
    run_root = _write_fixture(tmp_path)
    after = _render(_exec_source(_current_source(), "btc_after"), run_root)
    before = _render(_exec_source(_source_before_rewrite(), "btc_before"), run_root)
    rows_after = _table_rows(_section(after, "## 3.5.", "## 4."))
    rows_before = _table_rows(_section(before, "## 3.5.", "## 4."))
    # 🔴 对比不许**空过**：改写前的渲染必须真的还是旧文案（否定授权）
    assert "只供你审查参考" in _section(before, "## 3.5.", "## 4."), (
        "「改写前」的渲染不是旧文案 ⇒ 前后对比是空过"
    )
    assert rows_before == rows_after, "3.5 表格在改写前后不一致（表格列/数字不许动）"
    assert rows_after == _EXPECTED_TABLE_ROWS, "3.5 表格与改写前的金标准不一致"


def test_变异_抽掉授权引言_3_5断言必须转红(tmp_path: Path, overlap_stub):
    """变异负例 A：把 3.5 引言换回旧文案 ⇒ 授权断言必须转红（证明测试有牙）。"""
    run_root = _write_fixture(tmp_path)
    mutated = _render(
        _exec_source(_source_without_intro_authorization(), "btc_mut_intro"), run_root)
    section = _section(mutated, "## 3.5.", "## 4.")
    with pytest.raises(AssertionError):
        _assert_3_5_authorized(section)
    # 变异是**外科手术**：只动引言 ⇒ 任务节断言仍应通过
    _assert_task_duties(_section(mutated, "## 7. 任务", "---"))


def test_变异_抽掉7_1小节_任务断言必须转红(tmp_path: Path, overlap_stub):
    """变异负例 B：把 7.1 小节整块抽掉 ⇒ 任务节断言必须转红（证明测试有牙）。"""
    run_root = _write_fixture(tmp_path)
    mutated = _render(
        _exec_source(_source_without_task_duties(), "btc_mut_task"), run_root)
    with pytest.raises(AssertionError):
        _assert_task_duties(_section(mutated, "## 7. 任务", "---"))
    # 变异是**外科手术**：只动任务节 ⇒ 3.5 授权断言仍应通过
    _assert_3_5_authorized(_section(mutated, "## 3.5.", "## 4."))


@pytest.mark.skipif(not (_REAL_SNAPSHOT / "etf_analysis_report.json").exists(),
                    reason="真实产物不在本机（服务器侧产物）")
def test_真实产物_context_授权语义与空值字面量(tmp_path: Path, monkeypatch):
    """用**真机跑批产物**（报告 JSON + 调仓建议 JSON）回归一遍同一组断言。"""
    for name in ("etf_analysis_report.json", "rebalance_recommendation.json"):
        shutil.copy(_REAL_SNAPSHOT / name, tmp_path / name)
    (tmp_path / "etf_analysis_report.md").write_text("# 报告\n", encoding="utf-8")

    import overlap as overlap_mod

    def _pairs(*, codes, names, **kw):
        a, b = codes[0], codes[1]
        return [{"a": a, "b": b, "a_name": names.get(a, ""), "b_name": names.get(b, ""),
                 "weight_overlap": 0.617, "corr120": 0.912, "theme_a": "银行",
                 "theme_b": "银行", "same_theme": True, "rule_conflict": False,
                 "screen_flag": True}]

    monkeypatch.setattr(
        overlap_mod, "load_overlap_inputs",
        lambda **kw: (pd.DataFrame({"etf_code": ["512800"]}),
                      pd.DataFrame({"date": [_ASOF], "code": ["512800"], "close": [0.87]})))
    monkeypatch.setattr(overlap_mod, "compute_overlap_pairs", _pairs)

    text = _render(_exec_source(_current_source(), "btc_real"), tmp_path)
    assert "None" not in text, "真实产物渲染出的 context 里出现了 None"
    _assert_3_5_authorized(_section(text, "## 3.5.", "## 4."))
    _assert_task_duties(_section(text, "## 7. 任务", "---"))
