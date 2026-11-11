"""🔴 TA **合规筛选**的「否决」解析（`llm_final.extract_rejections`）回归。

背景：TradingAgents 承担**合规筛选**（审查 + 一票否决 + 顺延）⇒ 需要从
**决策原文**里抽出「TA 点名否决/剔除某只标的」的条目。

本文件钉死（**自带夹具**，不依赖服务器产物、不联网、不依赖 `manu/report/`）：

  ① 正常剔除句**必须**命中（含代码、动词、原文证据句、所在章节）；
  ② 表格行（`| 代码 | 名称 | 动作 |`）里的剔除**必须**命中，且名称取**下一格**；
  ③ 否定式（「不否决」）**不命中**；
  ④ 范围级表述（「所有红利类全部不考虑」）**不命中**；
  ⑤ 「一票否决权」措辞**不命中**（防止把「TA 有否决权」当成「否决某只标的」）；
  ⑥ 同一句里多个代码**都**命中；
  ⑦ **条件句 / 条件买入行 / 减仓行不算否决**（2026-11-11 真实夹具回归）；
  ⑧ **反向语义护栏是子句级**：「一票否决权…故剔除 512800」里的真否决**必须**命中；
  ⑨ **词表补齐**（回避 / 不配置 / 暂缓 / 0仓观察…）每个新词都有用例；
  ⑩ **名称列不许出垃圾**（`0%` / `、516320` / `和512170` / `未持有` ⇒ 空串）；
  ⑪ **真实决策件夹具回归**：`tests/fixtures/ta_decision_real_20261110.md`
      （2026-11-10 服务器实跑 asof 2026-11-09 的决策件节选）。
  ⑫ **变异负例**：把反向语义护栏 / 能力陈述规则清空 ⇒ 相关用例必须转红。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"
_FIXTURE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "ta_decision_real_20261110.md"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_reject_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> list[str]:
    return [r["code"] for r in LLF.extract_rejections(text)]


def _rows(text: str) -> list[dict]:
    return LLF.extract_rejections(text)


# ---------------------------------------------------------------- 夹具

_MD_REJECT = (
    "## 六、最终交易决定\n\n"
    "本轮建议剔除 512800 银行ETF华泰柏瑞，其余标的维持不变。\n"
)

_MD_TABLE = (
    "## 六、最终交易决定\n\n"
    "| 代码 | 名称 | 动作 |\n"
    "|:--|:--|:--|\n"
    "| 563020 | 红利低波ETF易方达 | 不选 |\n"
    "| 512800 | 银行ETF华宝 | 持有 |\n"
)

_MD_NEGATION = (
    "## 六、最终交易决定\n\n"
    "512800 继续持有，不否决该标的。\n"
)

_MD_SCOPE = (
    "## 六、最终交易决定\n\n"
    "本轮所有红利类标的全部不考虑，同类银行ETF也不选。\n"
)

_MD_VETO_POWER = (
    "## 六、最终交易决定\n\n"
    "TA 拥有一票否决权，可在必要时否决 512800 等任一标的，但本轮未行使该权力。\n"
)

_MD_MULTI = (
    "## 六、最终交易决定\n\n"
    "剔除 512800 与 512890，两只均不建仓。\n"
)


# ---------------------------------------------------------------- 既有行为

def test_正常剔除句命中():
    rows = _rows(_MD_REJECT)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["code"] == "512800"
    assert row["name"] == "银行ETF华泰柏瑞"
    assert "剔除" in row["reason"]
    assert len(row["reason"]) <= 200
    assert "最终交易决定" in row["source_section"]


def test_表格行剔除命中且名称取下一格():
    rows = _rows(_MD_TABLE)
    assert [r["code"] for r in rows] == ["563020"], rows
    assert rows[0]["name"] == "红利低波ETF易方达"


def test_否定式不命中():
    assert _codes(_MD_NEGATION) == []


def test_范围级表述不命中():
    assert _codes(_MD_SCOPE) == []


def test_否决权措辞不命中():
    assert _codes(_MD_VETO_POWER) == []


def test_同句多代码都命中():
    assert _codes(_MD_MULTI) == ["512800", "512890"]


def test_没有动词不命中():
    md = "## 六、最终交易决定\n\n512800 银行ETF华泰柏瑞维持 20% 权重不变。\n"
    assert _codes(md) == []


# ---------------------------------------------------------------- 既有语义必须保留

def test_既有保留_四句必须_clean():
    """这四句是历史金标语义：**一个字都不能改**。"""
    for md in (
        "## 六、最终交易决定\n\n本次不否决任何标的。\n",
        "## 六、最终交易决定\n\nTA 拥有一票否决权，可否决任意候选。\n",
        "## 六、最终交易决定\n\n若否决 512800，则顺延选择 515450。\n",
        "## 六、最终交易决定\n\n所有候选均通过审查。\n",
    ):
        assert _codes(md) == [], md


def test_既有保留_剔除多个必须命中两个():
    md = "## 六、最终交易决定\n\n剔除 516310、516320。\n"
    assert _codes(md) == ["516310", "516320"]


# ---------------------------------------------------------------- ⑦ 条件句不算否决

def test_条件买入_表格行不算否决():
    """🔴 真实决策件形态：`| 515450 | 当前不买；条件买入 | 重跑后最多10% |`（2026-11-09）。"""
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 最终动作 | 调整后安排 | 核心理由 |\n"
        "|:--|:--|:--|:--|\n"
        "| 515450 | 当前不买；条件买入 | 重跑后最多10% | E=+0.7243、最终分0.8995 |\n"
        "| 515650 | 回避 | 0% | E=-0.0254 |\n"
    )
    assert _codes(md) == ["515650"], _codes(md)


def test_条件买入_散文不算否决():
    md = (
        "## 六、最终交易决定\n\n"
        "515450当前不买，12月1日只有在E继续为正、mom120为正时才可条件买入，且最多10%。\n"
    )
    assert _codes(md) == [], _codes(md)


def test_条件买入_若则买入不算否决():
    md = "## 六、最终交易决定\n\n若E转正则买入 512800。\n"
    assert _codes(md) == [], _codes(md)


def test_条件买入_条件满足后买入不算否决():
    md = "## 六、最终交易决定\n\n512800 条件满足后再买入。\n"
    assert _codes(md) == [], _codes(md)


def test_条件买入_视情况买入不算否决():
    md = "## 六、最终交易决定\n\n视情况买入 515450。\n"
    assert _codes(md) == [], _codes(md)


def test_条件句_带若字的条件裁决不算否决():
    """条件句里的「取消买单」是**执行前置条件**，不是本轮否决。"""
    md = "## 六、最终交易决定\n\n若唯一值数不再满足条件，取消 515450、515650 的买单。\n"
    assert _codes(md) == [], _codes(md)


# ---------------------------------------------------------------- 表格行整行判定

def test_表格行_目标权重为正不算否决():
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 方向 | 基准目标权重 | 执行理由 |\n"
        "|:--|:--|--:|:--|\n"
        "| 512800 | 减 | 10%，硬上限12% | 保留最强相对信号 |\n"
    )
    assert _codes(md) == [], _codes(md)


def test_表格行_买入行不算否决():
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 方向 | 基准目标权重 | 执行理由 |\n"
        "|:--|:--|--:|:--|\n"
        "| 515450 | 买 | 8% | 只配置其16%标准槽位的50% |\n"
    )
    assert _codes(md) == [], _codes(md)


def test_表格行_零权重且明确剔除才算否决():
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 方向 | 基准目标权重 | 执行理由 |\n"
        "|:--|:--|--:|:--|\n"
        "| 515650 | 回避 | 0% | 信号不足以支持建仓 |\n"
    )
    assert _codes(md) == ["515650"], _codes(md)


def test_表格行_卖出不算否决():
    """卖出是**交易动作**，不是「否决某只候选」⇒ 不进否决名单。"""
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 方向 | 基准目标权重 | 执行理由 |\n"
        "|:--|:--|--:|:--|\n"
        "| 159929 | 卖 | 0% | 退出目标组合以释放风险预算 |\n"
    )
    assert _codes(md) == [], _codes(md)


# ---------------------------------------------------------------- ⑧ 反向护栏子句级

def test_反向护栏_子句级_同句真否决必须命中():
    """🔴 「一票否决权」只中和**它所在的那一句/那一行** ⇒ 同句里的真否决不能被吞。"""
    md = "## 六、最终交易决定\n\nTA 拥有一票否决权，故剔除 512800。\n"
    assert _codes(md) == ["512800"], _codes(md)


def test_反向护栏_能力陈述不算否决():
    md = "## 六、最终交易决定\n\nTA 拥有一票否决权，可在必要时否决 512800 等任一标的。\n"
    assert _codes(md) == [], _codes(md)


def test_反向护栏_否定式子句级():
    md = "## 六、最终交易决定\n\n不否决 512800。\n"
    assert _codes(md) == [], _codes(md)


# ---------------------------------------------------------------- ⑨ 词表补齐

def test_词表_回避():
    assert _codes("## 六、最终交易决定\n\n回避 512800。\n") == ["512800"]


def test_词表_不配置():
    assert _codes("## 六、最终交易决定\n\n不配置 512800。\n") == ["512800"]


def test_词表_不予配置():
    assert _codes("## 六、最终交易决定\n\n不予配置 512800。\n") == ["512800"]


def test_词表_暂缓():
    assert _codes("## 六、最终交易决定\n\n暂缓 512800。\n") == ["512800"]


def test_词表_放弃():
    assert _codes("## 六、最终交易决定\n\n放弃 512800。\n") == ["512800"]


def test_词表_不纳入():
    assert _codes("## 六、最终交易决定\n\n不纳入 512800。\n") == ["512800"]


def test_词表_退出候选():
    assert _codes("## 六、最终交易决定\n\n512800 退出候选。\n") == ["512800"]


def test_词表_不参与():
    assert _codes("## 六、最终交易决定\n\n本轮不参与 512800。\n") == ["512800"]


def test_词表_零仓观察():
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 方向 | 基准目标权重 | 执行理由 |\n"
        "|:--|:--|--:|:--|\n"
        "| 159207 | 持（0仓观察） | 0% | E仅0.0320 |\n"
    )
    assert _codes(md) == ["159207"], _codes(md)


# ---------------------------------------------------------------- ⑩ 名称不许出垃圾

def test_名称_表格行取不到返回空串():
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 当前权重 | 方向 | 调仓后目标 | 金额及理由 |\n"
        "|:--|--:|:--|--:|:--|\n"
        "| 512800 | 0% | 不买 | 0% | 走弱 |\n"
    )
    rows = _rows(md)
    assert rows and rows[0]["code"] == "512800", rows
    assert rows[0]["name"] == "", rows[0]["name"]


def test_名称_散文不得抽出顿号片段():
    rows = _rows("## 六、最终交易决定\n\n剔除 516310、516320。\n")
    assert [r["code"] for r in rows] == ["516310", "516320"]
    for r in rows:
        assert r["name"] == "", r["name"]


def test_名称_散文不得抽出含代码片段():
    rows = _rows("## 六、最终交易决定\n\n对515650和512170，当前不买。\n")
    for r in rows:
        assert r["name"] == "", r["name"]


def test_名称_不得出现未持有或百分比():
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 状态 | 方向 | 目标 | 理由 |\n"
        "|:--|:--|:--|--:|:--|\n"
        "| 512800 | 未持有 | 回避 | 0% | 走弱 |\n"
    )
    rows = _rows(md)
    assert rows and rows[0]["code"] == "512800", rows
    assert rows[0]["name"] == "", rows[0]["name"]


# ---------------------------------------------------------------- ⑪ 真实决策件夹具回归

def _fixture_body() -> str:
    raw = _FIXTURE_PATH.read_text(encoding="utf-8")
    match = re.search(r"```markdown\n(.*?)\n```", raw, re.S)
    assert match, "夹具缺少 ```markdown 围栏"
    return match.group(1)


def test_真实夹具_要买与要减的三只不得被判成否决():
    """🔴 2026-11-10 服务器实跑：`515450`/`515650`（TA 要买）、`512800`（要减）
    被误判成否决 ⇒ 计划里 5 席几乎被清空 ⇒ 闸门 8 个阶段转红。"""
    codes = _codes(_fixture_body())
    assert "515450" not in codes, codes
    assert "515650" not in codes, codes
    assert "512800" not in codes, codes


def test_真实夹具_真否决必须命中():
    codes = set(_codes(_fixture_body()))
    for code in ("159207", "512170", "563020", "511990"):
        assert code in codes, (code, sorted(codes))


def test_真实夹具_卖出不算否决():
    codes = set(_codes(_fixture_body()))
    for code in ("159929", "513330", "513770", "512690"):
        assert code not in codes, (code, sorted(codes))


def test_真实夹具_名称列不许出垃圾():
    for row in _rows(_fixture_body()):
        name = row["name"]
        if not name:
            continue
        assert name != "未持有", row
        assert "%" not in name, row
        assert not name[0].isdigit(), row
        assert not re.search(r"\d{6}", name), row
        assert name[0] not in "、和与及或", row


def test_真实夹具_命中项必须带证据句与章节():
    rows = _rows(_fixture_body())
    assert rows, "真实夹具里一条都没命中"
    for row in rows:
        assert row["reason"], row
        assert row["source_section"], row
        assert len(row["reason"]) <= 200, row


# ---------------------------------------------------------------- ⑫ 变异负例

def test_变异负例_清空反向语义护栏后否定式必须转红():
    """把护栏清空 ⇒ 「不否决 512800」变红 ⇒ 证明这条断言**有牙**。"""
    original = LLF._REJECT_NEGATIONS
    try:
        LLF._REJECT_NEGATIONS = ()
        assert _codes("## 六、最终交易决定\n\n不否决 512800。\n") != [], "变异后仍不命中 ⇒ 没有牙"
    finally:
        LLF._REJECT_NEGATIONS = original
    assert _codes("## 六、最终交易决定\n\n不否决 512800。\n") == []


def test_变异负例_清空能力陈述规则后否决权措辞必须转红():
    """把「能力陈述」规则清空 ⇒ 「可在必要时否决 512800 等任一标的」变红 ⇒ 有牙。"""
    original = LLF._REJECT_CAPABILITY
    try:
        LLF._REJECT_CAPABILITY = ()
        md = "## 六、最终交易决定\n\nTA 拥有一票否决权，可在必要时否决 512800 等任一标的。\n"
        assert _codes(md) != [], "变异后仍不命中 ⇒ 没有牙"
    finally:
        LLF._REJECT_CAPABILITY = original
    md = "## 六、最终交易决定\n\nTA 拥有一票否决权，可在必要时否决 512800 等任一标的。\n"
    assert _codes(md) == []


def test_变异负例_去掉买入意图判定后条件买入行必须转红():
    """去掉「买入意图」判定 ⇒ 条件买入行 / 条件买入句变红 ⇒ 有牙。"""
    original = LLF._BUY_INTENT_WORDS
    try:
        LLF._BUY_INTENT_WORDS = ()
        md = (
            "## 六、最终交易决定\n\n"
            "| 标的 | 最终动作 | 调整后安排 |\n"
            "|:--|:--|:--|\n"
            "| 515450 | 当前不买；条件买入 | 重跑后最多10% |\n"
        )
        assert _codes(md) != [], "变异后仍不命中 ⇒ 没有牙"
    finally:
        LLF._BUY_INTENT_WORDS = original


def test_条件句_条件里的否决动作不算否决():
    """🔴 复用既有 `_is_conditional` 的断言：条件句里的动作**不是本轮裁决**。

    去掉 `_is_conditional` 复用 ⇒ 本用例必须转红。
    """
    md = "## 六、最终交易决定\n\n若上游数据未通过，则不建仓 512800。\n"
    assert _codes(md) == [], _codes(md)


def test_条件买入_表格行整行判定_没有权重列也必须不否决():
    """🔴 「整行判定」断言：动作格里的「；」**不能**把「条件买入」切丢。

    把表格行改回「单元格级」判定 ⇒ 本用例必须转红。
    """
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 最终动作 |\n"
        "|:--|:--|\n"
        "| 515450 | 当前不买；条件买入 |\n"
    )
    assert _codes(md) == [], _codes(md)


def test_词表_暂缓建仓():
    assert _codes("## 六、最终交易决定\n\n暂缓建仓 563020。\n") == ["563020"]


def test_词表_不买():
    assert _codes("## 六、最终交易决定\n\n不买 511990。\n") == ["511990"]


def test_词表_不建仓():
    assert _codes("## 六、最终交易决定\n\n不建仓 563020。\n") == ["563020"]


# ---------------------------------------------------------------- ⑬ 禁止性反向语义（2026-11-11 复核补丁）

def test_禁止性反向语义_不得被判成否决不得触发误判():
    """🔴 实测事故：夹具头部说明文字
    「`512800`（减）、`515450`/`515650`（买）**不得**被判成否决」
    ⇒ 旧护栏只认连续的「不得否决」，中间隔了「被判成」⇒ **要买/要减的标的被误判成否决**。"""
    text = (
        "## 夹具说明\n\n"
        "`512800`（减）、`515450`/`515650`（买）**不得**被判成否决。\n"
    )
    assert _codes(text) == [], _codes(text)


def test_禁止性反向语义_不连续的否定词也要中和():
    for text in (
        "不得否决 512800。",
        "不可否决 512800。",
        "不应被否决 512800。",
        "这不构成对 512800 的否决。",
        "禁止剔除 512800。",
    ):
        assert _codes(text) == [], (text, _codes(text))


def test_禁止性反向语义_真否决仍必须命中():
    """加护栏不许把真否决一起豁免掉。"""
    for text in ("剔除 512800。", "本轮建议剔除 512800 银行ETF华泰柏瑞。"):
        assert _codes(text) == ["512800"], (text, _codes(text))


def test_变异负例_清空禁止性护栏后必须转红():
    """把禁止性正则清空 ⇒ 「不得被判成否决」变红 ⇒ 证明这条断言**有牙**。"""
    original = LLF._REJECT_NEGATION_RE
    try:
        import re as _re
        LLF._REJECT_NEGATION_RE = _re.compile(r"(?!x)x")
        text = (
            "## 夹具说明\n\n"
            "`512800`（减）、`515450`/`515650`（买）**不得**被判成否决。\n"
        )
        assert _codes(text) != [], "变异后仍不命中 ⇒ 没有牙"
    finally:
        LLF._REJECT_NEGATION_RE = original


def test_真实夹具_整文件解析必须与正文解析一致():
    """🔴 夹具头部的说明文字**不得污染**解析结果（说明文字里一旦出现代码 + 否决类动词就会污染）。"""
    raw = _FIXTURE_PATH.read_text(encoding="utf-8")
    assert set(_codes(raw)) == set(_codes(_fixture_body())), sorted(set(_codes(raw)))
