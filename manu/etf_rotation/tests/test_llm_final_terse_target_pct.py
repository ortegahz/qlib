"""🔴 Trader 最终裁决「代码锚定的紧致写法」解析（2026-11-13 真缺陷）。

事故来源（真实交付件 `snapshots/20261112_181544`，2026-11-12 夜跑实测）::

    本轮最终采用**"`512800`减至20%、`515450`标准档20%、否决`515650`、
    其余弱仓退出、权益上限40%、约60%现金"**的方案

⇒ `llm_final_decision.targets[512800]` 与 `[515450]` 的 `target_pct` **抽不出**（None）
⇒ `make_decision_scenarios.resolve_divergence_options` 硬失败：

    RuntimeError: llm_final_decision.targets[515450]缺少必需字段 `target_pct`
                   （不许用默认值蒙混、不许静默跳过）

⇒ 重建链（`rebuild_baseline.sh` 步骤 1）转红 ⇒ 交付链第 1/5 步失败。

根因：旧口径**只认「权重名词（目标权重/权重/仓位/目标/风险预算）+ 动词 + 数字%」**
⇒ Trader 把方案压成「代码 + 目标设定词/档位 + 数字%」时**一个字都抽不出**。

修法（`llm_final._TERSE_TARGET_RE`）：新增**代码锚定**的紧致规则 ——
① 代码必须紧邻（中间只允许 markdown 记号与空白）；② 中间的词**只认闭集**
（目标设定动词 / 档位词）且**至少有一个**；③ **只补空**（`_set` 先到先得）。

本文件钉死（自带夹具，不依赖服务器产物、不联网）：

  ① 真实原文 ⇒ `512800`/`515450` 各抽到 **20%**，`unparsed_codes` **不再**有这两只；
  ② **只补空** ⇒ 严格规则已抽到的值**不被覆盖**；
  ③ **当前状态不是目标**：「512800当前权重7.90%」⇒ 抽不出（不许把现值当目标）；
  ④ **价格动词不是目标**：「512800涨到20%」⇒ 抽不出；
  ⑤ **组合层不进单只槽位**：「权益上限40%」⇒ 不得落到任何一只代码头上；
  ⑥ **既有夹具不许回归**：`fixtures/ta_decision_real_20261110.md` 的解析结果**一字不变**；
  ⑦ **变异负例**：把新规则关掉 ⇒ ① 必须转红（证明闸门有牙）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"
_FIXTURE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "ta_decision_real_20261110.md"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_terse_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _md(body: str) -> str:
    return f"## 六、最终交易决定\n\n{body}\n"


#: 真实原文（2026-11-12 那一轮 Trader 的最终裁决概要句）——一字不改。
_REAL = _md(
    "本轮最终采用**\"`512800`减至20%、`515450`标准档20%、否决`515650`、"
    "其余弱仓退出、权益上限40%、约60%现金\"**的方案"
)


def _targets(text: str) -> dict:
    return LLF.extract_final_decision(text).get("targets") or {}


def _pct(text: str, code: str):
    """该代码的 `target_pct`（**代码整只没出现**时也返回 None，不许 KeyError）。"""
    return (_targets(text).get(code) or {}).get("target_pct")


# ---------------------------------------------------------------- 真实原文

def test_真实原文_两只减仓股都抽到目标权重():
    """🔴 真缺陷现场：紧致写法里的数字必须抽出来（否则重建链硬失败）。"""
    tg = _targets(_REAL)
    assert tg["512800"]["target_pct"] == 20.0, tg.get("512800")
    assert tg["515450"]["target_pct"] == 20.0, tg.get("515450")


def test_真实原文_unparsed不再挂这两只():
    unparsed = LLF.extract_final_decision(_REAL).get("unparsed_codes") or []
    assert "512800" not in unparsed, unparsed
    assert "515450" not in unparsed, unparsed


def test_真实原文_来源标签必须指向真实出处():
    """🔴 回放纪律：来源标签不许写成编造的出处。"""
    src = _targets(_REAL)["515450"]["target_pct_source"] or ""
    assert "紧致写法" in src, src


# ---------------------------------------------------------------- 只补空

def test_只补空_严格规则已抽到的值不被覆盖():
    """「512800目标权重降至 12%」⇒ 12% 来自严格规则 ⇒ 新规则不许改成别的数。"""
    tg = _targets(_md("512800目标权重降至 12%。"))
    assert tg["512800"]["target_pct"] == 12.0, tg.get("512800")


# ---------------------------------------------------------------- 负例

def test_负例_当前状态不得当成目标权重():
    """「512800当前权重7.90%」⇒ 现值**不是**目标 ⇒ 抽不出（宁可不抽）。"""
    body = _md("512800当前权重7.90%，维持不动。")
    assert _pct(body, "512800") is None, _targets(body).get("512800")


def test_负例_价格动词不得当成目标权重():
    """「512800涨到20%」⇒ 价格，不是仓位 ⇒ 抽不出。"""
    body = _md("512800涨到20%再观察。")
    assert _pct(body, "512800") is None, _targets(body).get("512800")


def test_负例_组合层权益上限不得落到单只代码():
    """「权益上限40%」是组合层 ⇒ 不得进任何单只代码的槽位。"""
    body = _md("512800减仓，权益上限40%，约60%现金。")
    assert _pct(body, "512800") is None, _targets(body).get("512800")


def test_负例_裸数字不得当成目标权重():
    """「512800 20%」中间**没有**目标设定词 ⇒ 抽不出（不许放宽成模糊窗口）。"""
    body = _md("512800 20%。")
    assert _pct(body, "512800") is None, _targets(body).get("512800")


# ---------------------------------------------------------------- 既有夹具不许回归

def test_既有夹具_解析结果不许变():
    """🔴 `fixtures/ta_decision_real_20261110.md` 的解析结果必须与改动前**完全一致**。"""
    text = _FIXTURE_PATH.read_text(encoding="utf-8")
    before = {
        "159207", "159307", "159547", "511990", "512170", "512820", "512890",
        "515020", "515080", "515100", "515180", "516310", "563020",
    }
    assert {r["code"] for r in LLF.extract_rejections(text)} == before


# ---------------------------------------------------------------- 变异负例（闸门承重）

def test_变异_关掉紧致规则则真实原文必须重新抽不出():
    """变异：把新规则换成永不匹配 ⇒ ① 必须转红（证明这条规则确实承重）。"""
    saved = LLF._TERSE_TARGET_RE
    try:
        import re

        LLF._TERSE_TARGET_RE = re.compile(r"(?!x)x")
        tg = _targets(_REAL)
        assert tg["512800"]["target_pct"] is None, "关掉规则后 512800 必须重新抽不出"
        assert tg["515450"]["target_pct"] is None, "关掉规则后 515450 必须重新抽不出"
    finally:
        LLF._TERSE_TARGET_RE = saved


# ---------------------------------------------------------------- 否决/备选语境（独立审查 A-1）

def test_否决语境_被否决的标的不得拿到目标权重():
    """🔴 「`515650`调至20%的备选方案被否决。」⇒ 515650 是**被否决**的
    ⇒ 交付件不许给它编出「LLM 最终裁决目标权重 20%」。"""
    body = _md("`515650`调至20%的备选方案被否决。")
    assert _pct(body, "515650") is None, _targets(body).get("515650")


def test_否决语境_前置否决词也不许给目标():
    body = _md("否决`515650`给20%的方案。")
    assert _pct(body, "515650") is None, _targets(body).get("515650")


def test_否决语境_不许误伤别的标的():
    """🔴 同一个句子里「否决`515650`」**只管 515650** ⇒ 不许把 512800 一起毙掉。"""
    body = _md("否决`515650`、`512800`减至20%")
    assert _pct(body, "512800") == 20.0, _targets(body).get("512800")


def test_真实原文_否决后面的标的拿不到目标():
    """真实句子里「否决`515650`」在 512800/515450 **之后** ⇒ 那两只仍要抽到 20%。"""
    assert _pct(_REAL, "512800") == 20.0
    assert _pct(_REAL, "515450") == 20.0
    assert _pct(_REAL, "515650") is None


# ---------------------------------------------------------------- 真实 md 的其它写法（独立审查 A-2）

def test_冒号写法_必须抽得到():
    """真实 md 第 688 行的形态：「- `512800`：减至20%，保留核心底仓」。"""
    body = _md("- `512800`：减至20%，保留核心底仓")
    assert _pct(body, "512800") == 20.0, _targets(body).get("512800")


def test_档位词_标准槽位也要抽得到():
    """「512800减到标准槽位16%」（`rules.md §十四.98` 引用的形态）。"""
    body = _md("512800减到标准槽位16%、515450只给8%")
    assert _pct(body, "512800") == 16.0, _targets(body).get("512800")
    assert _pct(body, "515450") == 8.0, _targets(body).get("515450")


# ---------------------------------------------------------------- 严格规则全局优先（独立审查 B-1）

def test_跨句_严格口径必须赢过紧致口径():
    """🔴 「`512800`减至20%。512800目标权重降至12%。」⇒ 必须取 **12%**（严格口径）。

    若把紧致规则与严格规则**逐句交错** ⇒ 先出现的紧致句会赢（`_set` 先到先得）
    ⇒ 交付件会出现「宽松口径压过严格口径」。
    """
    body = _md("`512800`减至20%。512800目标权重降至12%。")
    assert _pct(body, "512800") == 12.0, _targets(body).get("512800")


# ---------------------------------------------------------------- 承重约束（独立审查 E-2）

def test_减至N份_不得当成百分比():
    """「512800减至5100份」⇒ 是**份数**，不是权重 ⇒ 目标权重必须抽不出。"""
    body = _md("512800减至5100份")
    assert _pct(body, "512800") is None, _targets(body).get("512800")


def test_七位数字_不得被切成六位代码():
    """「1512800减至20%」⇒ `1512800` 是 7 位 ⇒ 不许切成 `512800`。"""
    body = _md("1512800减至20%")
    assert "512800" not in _targets(body), _targets(body).keys()


# ---------------------------------------------------------------- 既有夹具不许回归（真金标）

#: `fixtures/ta_decision_real_20261110.md` 在**改动前**（`git show HEAD:`）的
#: `extract_final_decision` 解析结果（金标）⇒ 改一个字都必须被发现。
#: 🔴 2026-11-13（金标**变更**，有据）：本金标原先把夹具的**结构化决策表**
#:    （「标的 | 方向 | 基准目标权重 | 执行理由」）**整个忽略** ⇒
#:    512800 的 `target_pct=None`（表里明明写着「**10%，硬上限12%**」）、
#:    515450 / 515650 / 159207 / 512170 / 563020 … **连槽位都没有**
#:    ⇒ `unparsed_codes` 里挂着 6 只 ⇒ 「LLM 最终裁决抽不出数字」的假象。
#:    ⇒ **缺陷自证**：夹具自己的「最终交易决定」散文写的是
#:    「将当前22.54%的仓位降至**基准约10%**、硬上限10%」⇒ **10% 与表里的 10% 一致**
#:    ⇒ 旧金标固化的 `None` 与原文**矛盾**，是缺陷不是口径。
#:    ⇒ 变更后：**只多不多不少**（每个代码都从表里拿到了它自己的目标权重）
#:    ⇒ `unparsed_codes` 变空 ⇒ 「提到了但抽不出数字」的假象消失。
#:    ⚠️ 方向词「持」= 表里「持（0仓观察）」⇒ 目标权重 0%（0 仓观察）。
_FIXTURE_TARGETS_GOLD = {'159207': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '159307': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '159547': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '159929': {'cap_pct': None,
            'direction': '卖',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '511990': {'cap_pct': None,
            'direction': '不买',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '512170': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '512690': {'cap_pct': None,
            'direction': '卖',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '512800': {'cap_pct': None,
            'direction': '减',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 10.0},
 '512820': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '512890': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '513330': {'cap_pct': None,
            'direction': '卖',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '513770': {'cap_pct': None,
            'direction': '卖',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '515020': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '515080': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '515100': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '515180': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '515450': {'cap_pct': None,
            'direction': '买',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 8.0},
 '515650': {'cap_pct': None,
            'direction': '买',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 6.0},
 '516310': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0},
 '563020': {'cap_pct': None,
            'direction': '持',
            'floor_pct': None,
            'lots': None,
            'post_trade_pct': None,
            'target_pct': 0.0}}

_FIXTURE_UNPARSED_GOLD = []


def test_既有夹具_最终裁决解析结果不许变():
    """🔴 夹具的 `extract_final_decision`（**全字段** + unparsed）必须与改动前完全一致。"""
    text = _FIXTURE_PATH.read_text(encoding="utf-8")
    got = LLF.extract_final_decision(text)
    targets = got.get("targets") or {}
    slim = {
        code: {k: slot.get(k) for k in
               ("target_pct", "direction", "lots", "post_trade_pct", "cap_pct", "floor_pct")}
        for code, slot in targets.items()
    }
    assert slim == _FIXTURE_TARGETS_GOLD, {
        "新增": sorted(set(slim) - set(_FIXTURE_TARGETS_GOLD)),
        "丢失": sorted(set(_FIXTURE_TARGETS_GOLD) - set(slim)),
        "变动": {c: (slim.get(c), _FIXTURE_TARGETS_GOLD.get(c))
                 for c in set(slim) & set(_FIXTURE_TARGETS_GOLD)
                 if slim.get(c) != _FIXTURE_TARGETS_GOLD.get(c)},
    }
    assert (got.get("unparsed_codes") or []) == _FIXTURE_UNPARSED_GOLD, got.get("unparsed_codes")
