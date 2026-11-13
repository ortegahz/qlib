"""🔴 2026-11-13 新增：探针 A20⑥「零方向**代码级归属**」+ A20⑤「手数**代码级**要求」回归。

事故来源（2026-11-13 服务器实跑，生产链「解析器回归探针（82 项）」红 2 项）：

    [FAIL] A20⑤ 每个**买入类**最终裁决都必须能回放手数（数据侧）+ 解析器必须读得出手数（能力侧）
            买入类目标 1 个｜回放问题 ['515450: 手数=None（必须是 ≥1 的整数）']
    [FAIL] A20⑥ 决策 md 里写明「清仓/不买」的代码 ⇒ 计划里必须零目标 + 零方向（+ 部分卖出≠清仓）
            最终裁决章节：表格方向列 0 个代码｜散文子句 5 个代码｜合计 5 个
            违例 ['512800: 原文写明零方向，但计划目标=16.0（必须 0.0）']｜部分卖出方向违例 []

⇒ **产品侧行为是对的**：
   512800 是「**减仓**至 16%」（不是清仓）——真机原句把「卖出**四只持仓**」和
   「将 **512800 降至 12–16%**」写在**同一个子句**里 ⇒ 子句级零方向词把 512800 **连坐**；
   515450 是「**条件性卫星仓，最高 8%**」——用**权重**表达、**没有手数**
   ⇒ 计划里 `lots=None` 是**正确**的（计划按 权重×价格÷手数 自己算）。
⇒ 红的是**探针口径**，不是产品 ⇒ 本文件钉死探针的**新口径**，且**不许把断言改弱**。

本文件钉死（自带夹具，**不依赖** `/tmp` 那个真机文件 ⇒ 原句**内联**）：

  ① **A20⑥ 代码级归属**：真机原句里 512800 被「降至 12–16%」**点名** ⇒ **减仓**、
     **不是**零方向；而同段真·清仓的 4 只**仍然**必须在零方向集合里（不许整子句连坐豁免）。
  ② **A20⑥ 成对负例**：「卖出 512800 并将 515450 降至 10%」⇒ 512800 **仍判零方向**、
     515450 不判（⇒ 「降至」只允许**代码级**豁免，**不是**整子句豁免）。
  ③ **A20⑥ 护栏边界**：明写「清仓 900001」的代码**永远**判零方向（不许被「降至」放过）。
  ④ **A20⑤ 权重表达**：原文只给**权重**（最高 8%）⇒ 计划 `lots=None` **合法**（真机 515450）。
  ⑤ **A20⑤ 成对负例**：原文写了「买 1 手」⇒ 计划 `lots=None` / `lots=2` **必须红**。
  ⑥ **A20⑤ 能力侧不许动**：合成夹具「买 1 手」/「最多一手」⇒ 解析器必须抽出 `lots == 1`。
  ⑦ **变异反证**：把两处修复分别摘掉 ⇒ 对应断言必须转红（证明本测试有牙）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETF_DIR))

_PLP_PATH = _ETF_DIR / "probes" / "probe_llm_parser.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLP = _load("_plp_under_test", _PLP_PATH)
LLF = PLP.LF  # noqa: N815 —— 探针自己 import 的解析器（能力侧夹具要用同一个）

#: 真机交付件（`snapshots/20261113_092926`）最终裁决章里的**原句**（内联 ⇒ 不依赖 /tmp）：
#:    「卖出」的对象是「四只持仓」，512800 被「降至 12–16%」绑定 ⇒ 512800 是**减仓**。
_REAL_SENTENCE = (
    "【防守槽位全部留现金】规则目标本身 0% 防守资产；511990 一手约 10,000 元 ≈ 总资产 33.5%，"
    "既无法按 16% 标准槽位执行，又受严格 e_covered 口径限制（不可穿透资产不得旁路），"
    "且本陪审团不得推荐池外标的。现金 15,518.87 元（51.98%）在卖出四只持仓并将 512800 "
    "降至 12–16% 后静态约 76–84%，已是有效防御与流动性缓冲。"
)

#: 真机最终裁决 Executive Summary 里的**真·清仓**那几句（内联）
_REAL_SELL_SENTENCE = (
    "159929、513330、513770 按 MA60 强制退出规则卖出，512690 卖出"
)

#: 真机最终裁决 Executive Summary 里的**权重表达**买入（内联）
_REAL_WEIGHT_BUY_SENTENCE = "515450 仅作条件性卫星仓，最高 8%"


def _md(body: str) -> str:
    """拼一份带「最终交易决定」章节的决策 md（探针与解析器都靠这个标题定位章节）。"""
    return f"## 六、最终交易决定\n\n{body}\n"


def _zero(body: str) -> set[str]:
    """决策 md 正文 ⇒ 零方向代码集合。"""
    codes, _note = PLP._md_zero_direction_codes(_md(body))
    return codes


# --------------------------------------------------------------------- ① 真夹具
def test_real_sentence_reduced_code_is_not_zero_direction() -> None:
    """① 真机原句：512800 被「降至 12–16%」点名 ⇒ **减仓**，不是零方向。"""
    codes = _zero(_REAL_SENTENCE)
    assert "512800" not in codes, f"512800 又被误判成零方向：{sorted(codes)}"


def test_real_sentence_real_sells_are_still_zero_direction() -> None:
    """① 同段的真·清仓 4 只**必须**仍在零方向集合里（不许把整个子句连坐豁免）。"""
    codes = _zero(_REAL_SELL_SENTENCE + "。" + _REAL_SENTENCE)
    for code in ("159929", "513330", "513770", "512690"):
        assert code in codes, f"真·清仓漏判：{code} 不在 {sorted(codes)}"
    assert "512800" not in codes, f"512800 又被误判成零方向：{sorted(codes)}"


# --------------------------------------------------------------------- ② 成对负例
def test_sold_code_in_same_clause_still_zero_direction() -> None:
    """② 「卖出 512800 并将 515450 降至 10%」⇒ 512800 **仍判零方向**，515450 不判。

    🔴 这是「**代码级**豁免」的牙：把「降至」当**整子句**豁免 ⇒ 512800 被放过
       ⇒ 真·清仓漏检（假绿）⇒ 本断言必须转红。
    """
    codes = _zero("卖出 512800 并将 515450 降至 10%")
    assert "512800" in codes, f"同子句里的真·卖出被放过（假绿）：{sorted(codes)}"
    assert "515450" not in codes, f"被「降至」点名的代码不该判零方向：{sorted(codes)}"


def test_clear_out_is_not_shielded_by_reduce_word() -> None:
    """③ 明写「清仓 900001」⇒ **永远**判零方向（「降至」不许把它放过）。"""
    codes = _zero("清仓 900001 并将组合权益降至 10%")
    assert "900001" in codes, f"真·清仓被「降至」放过（假绿）：{sorted(codes)}"


def test_clear_out_with_colon_is_not_shielded_by_reduce_word() -> None:
    """③ 「清仓：900001 并将组合权益降至 10%」⇒ 900001 **仍判零方向**（冒号也算直接对象）。"""
    codes = _zero("清仓：900001 并将组合权益降至 10%")
    assert "900001" in codes, f"真·清仓被「降至」放过（假绿）：{sorted(codes)}"


def test_reduce_binding_is_code_level_not_clause_level() -> None:
    """③ 同一子句里两只代码：只有被「降至」**点名**的那只被豁免。"""
    codes = _zero("卖出 900001、900002 并将 900002 降至 10%")
    assert "900001" in codes, f"没被点名的代码被连坐豁免（假绿）：{sorted(codes)}"
    assert "900002" not in codes, f"被点名的代码不该判零方向：{sorted(codes)}"


# --------------------------------------------------------------------- ④ 权重表达
def test_weight_expressed_buy_needs_no_lots() -> None:
    """④ 原文用**权重**表达（最高 8%）⇒ 计划 `lots=None` **合法**（真机 515450）。"""
    body = "900005 仅作条件性卫星仓，最高 8%。"
    targets = {"900005": {"direction": "条件买入", "lots": None}}
    assert PLP._md_lots_violations(_md(body), targets) == [], "权重表达的买入被误判成缺手数"


def test_real_weight_expressed_buy_needs_no_lots() -> None:
    """④ 真机原句（515450 卫星仓）⇒ `lots=None` 合法（这正是 2026-11-13 的假红现场）。"""
    targets = {"515450": {"direction": "条件买入", "lots": None}}
    assert PLP._md_lots_violations(_md(_REAL_WEIGHT_BUY_SENTENCE), targets) == []


# --------------------------------------------------------------------- ⑤ 成对负例
def test_original_lots_must_be_replayed() -> None:
    """⑤ 原文写了「买 1 手」⇒ 计划必须回放 **1 手**（漏抽 / 抽错都要红）。"""
    body = "对 900001 买 1 手。"
    assert PLP._md_lots_violations(_md(body),
                                    {"900001": {"direction": "买", "lots": 1}}) == []
    assert PLP._md_lots_violations(_md(body),
                                    {"900001": {"direction": "买", "lots": None}}), \
        "原文写了 1 手、计划却是 None ⇒ 必须红（手数漏抽）"
    assert PLP._md_lots_violations(_md(body),
                                    {"900001": {"direction": "买", "lots": 2}}), \
        "原文写了 1 手、计划却是 2 手 ⇒ 必须红（手数抽错）"


def test_max_lots_expression_must_be_replayed() -> None:
    """⑤ 「最多一手」（上界语义）同样是**手数** ⇒ 计划必须回放 1 手。"""
    body = "对 900002 条件买入最多一手。"
    assert PLP._md_lots_violations(_md(body),
                                    {"900002": {"direction": "条件买入", "lots": 1}}) == []
    assert PLP._md_lots_violations(_md(body),
                                    {"900002": {"direction": "条件买入", "lots": None}}), \
        "原文写了「最多一手」、计划却是 None ⇒ 必须红"


def test_share_amount_is_not_lots() -> None:
    """⑤ 「一手约 10,000 元」是**金额**、不是手数 ⇒ 不许要求计划有手数。

    🔴 照抄解析器口径：解析器的 `lots` 只来自「买 N 手」/「最多 N 手」
       ⇒ 「N 股/份」走**股数**通道 ⇒ 要求 `lots` 会变成**永久假红**。
    """
    body = "900003 一手约 10,000 元，约占总资产 33.5%。"
    targets = {"900003": {"direction": "条件买入", "lots": None}}
    assert PLP._md_lots_violations(_md(body), targets) == []


def test_non_lots_phrasing_is_not_a_lot_count() -> None:
    """⑤ 「三手货币 ETF」不是手数 ⇒ 不许要求计划有手数（裸数字匹配会误命中）。"""
    body = "900004 为三手货币 ETF，不做配置。"
    targets = {"900004": {"direction": "条件买入", "lots": None}}
    assert PLP._md_lots_violations(_md(body), targets) == []


# --------------------------------------------------------------------- ⑥ 能力侧
def test_capability_side_still_reads_lots() -> None:
    """⑥ 能力侧不许动：解析器必须从「买 1 手」/「最多一手」抽出 `lots == 1`。"""
    syn = ("## 六、最终交易决定\n\n**Executive Summary**: "
           "对 900001 买 1 手；对 900002 条件买入最多一手。\n")
    result = LLF.extract_final_decision(syn)
    targets = result.get("targets") or {}
    assert (targets.get("900001") or {}).get("lots") == 1, \
        f"解析器读不出「买 1 手」：{(targets.get('900001') or {}).get('lots')!r}"
    assert (targets.get("900002") or {}).get("lots") == 1, \
        f"解析器读不出「最多一手」：{(targets.get('900002') or {}).get('lots')!r}"


# --------------------------------------------------------------------- ⑦ 变异反证
def test_reduce_binding_is_load_bearing() -> None:
    """⑦ 把「代码级减仓绑定」摘掉 ⇒ ① 的断言必须转红（证明本测试有牙）。"""
    old = PLP._md_clause_reduced_codes
    try:
        PLP._md_clause_reduced_codes = lambda clause: set()
        codes = _zero(_REAL_SENTENCE)
        assert "512800" in codes, "摘掉代码级归属后 512800 仍未进零方向集合 ⇒ 夹具不敏感"
    finally:
        PLP._md_clause_reduced_codes = old


def test_lots_requirement_is_load_bearing() -> None:
    """⑦ 把「代码级手数要求」摘掉 ⇒ ⑤ 的负例必须转绿（证明负例有牙）。"""
    old = PLP._md_code_lots
    try:
        PLP._md_code_lots = lambda md_text: {}
        body = "对 900001 买 1 手。"
        targets = {"900001": {"direction": "买", "lots": None}}
        assert PLP._md_lots_violations(_md(body), targets) == [], \
            "摘掉代码级手数要求后负例仍然红 ⇒ 夹具不敏感"
    finally:
        PLP._md_code_lots = old
