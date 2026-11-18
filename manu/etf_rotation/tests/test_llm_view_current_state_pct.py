"""🔴 2026-11-17 真机交付件 `snapshots/20261117_162113` 实证：**两个真缺陷**的回归。

事故现场（多头表 512800 行 · 理由列，原样截取）：

    | **512800** | **减** | 多头核心，但不追涨。**当前权重22.54%**，高于机械目标16%；…… |

⇒ **缺陷①（幽灵目标）**：`_backward_segment` 的「锚点截断」只保留锚点**之后**的文本
  ⇒「当前权重」被截成「」⇒ 语境守卫**看不见「当前」**
  ⇒ 理由列里的**账户现状权重 22.54%** 被当成**说话人的目标权重**
  ⇒ 交付件里出现「LLM 目标 22.54%」这个**幽灵数字**（LLM 从未这么说过）
  ⇒ 方向-权重语义守卫按「减 ⇒ 目标 22.54% ≥ 当前 22.20%」判 ⇒ **假红灯**。

⇒ **缺陷②（规则层的数被安到说话人头上）**：正文里的「**机械目标**16%」是**规则模板**
  给的数（表头口径早有先例：A14「机械目标」列不是说话人的目标）
  ⇒ 若把 16% 当成多头目标，方向「减」+ 目标 16% 在**当前权重降到 16% 以下**时
  必然触发「减 ⇒ 目标 < 当前」的**假冲突**（本轮 512800 卖到 7.00% 后，下一轮就踩中）。

本文件钉死（自带夹具，不依赖服务器产物、不联网）：

  ① 现状权重（当前/现有/目前/原有 + 权重/仓位/…）**不得**被读成目标权重；
  ② 规则层机械目标（机械目标/规则目标/原目标…）**不得**被读成说话人的目标；
  ③ 说话人**自己**写出的目标照常取（修法不许过头：「减至10%」⇒ 10%）；
  ④ 既有口径**不许**被改坏：「当前**目标** 20%」⇒ 20%、「目标收益 20%」⇒ 20%；
  ⑤ 真机研究经理行（当前22.54%明显超配…目标低于20%）**不受影响**（目标 20% / 方向 持有）；
  ⑥ **变异反证**：把两处护栏回退 ⇒ ① ② 必须**转红**（证明本测试有牙）。
"""

from __future__ import annotations

import pathlib
import re
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import final_rebalance_plan as FRP  # noqa: E402


# --------------------------------------------------------------------- 夹具

#: 🔴 真机多头表 512800 行（2026-11-17 asof=2026-11-17）
_BULL_ROW_REASON = (
    "多头核心，但不追涨。当前权重22.54%，高于机械目标16%；"
    "其与515450的120日相关性约0.841，保留更高银行权重会放大红利/金融风格集中度。"
    "减仓是恢复风险预算，不是看空清仓。"
)

#: 🔴 真机研究经理表 512800 行（同一份决策 md）
_RESEARCH_ROW_REASON = (
    "当前22.54%明显超配；E=-0.2022且组合因子集中，但正动量、MA60和排序稳健性支持保留小核心。"
    "目标低于20%，并按35%总权益上限下的规则层等权槽位执行。"
)

_MD_BULL = (
    "## 多头（Bull）\n\n"
    "### 四、逐标的调仓方向\n\n"
    "| 标的 | 方向 | 理由 |\n"
    "|---|---|---|\n"
    f"| **512800** | **减** | {_BULL_ROW_REASON} |\n"
)

_MD_RESEARCH = (
    "## 研究经理结论\n\n"
    "### 2026-12-01执行方向\n\n"
    "| 标的 | 方向 | 理由 |\n"
    "|---|---|---|\n"
    f"| **512800** | **减，完成后继续持有** | {_RESEARCH_ROW_REASON} |\n"
)

_ACCOUNT = {"512800": 22.201010335412313, "现金": 77.79898966458769}
_TOTAL_ASSETS = 30035.57


def _write(tmp_path: pathlib.Path, text: str) -> str:
    path = tmp_path / "tradingagents_decision.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _views(tmp_path: pathlib.Path, text: str) -> dict:
    return FRP._llm_target_views(_write(tmp_path, text),
                                 total_assets=_TOTAL_ASSETS,
                                 account_weights=_ACCOUNT)


# --------------------------------------------------------------------- ① 现状权重

class TestCurrentStateWeightIsNotTarget:
    """「当前/现有/… + 权重/仓位/…」= **账户现状**，不是说话人的目标。"""

    def test_真机多头行_不得抽出22_54(self, tmp_path: pathlib.Path) -> None:
        views = _views(tmp_path, _MD_BULL)
        for view in views.get("512800") or []:
            assert view["target_pct"] != 22.54, (
                f"🔴「当前权重22.54%」是**账户现状**，被当成目标权重了"
                f"（target_pct={view['target_pct']!r}）⇒ 交付件出现幽灵数字"
            )

    def test_真机多头行_判不出自身目标_整行丢弃(self, tmp_path: pathlib.Path) -> None:
        """真机多头行**没有自己的目标**（只有现状 22.54% 与规则层的 16%）⇒ 不猜。"""
        views = _views(tmp_path, _MD_BULL)
        assert "512800" not in views, (
            f"🔴多头行既无自身目标、又只剩现状/机械目标 ⇒ 该行应被丢弃（不猜）；"
            f"实得 {views.get('512800')!r}"
        )

    def test_同型变体_都不得当成目标(self, tmp_path: pathlib.Path) -> None:
        for phrase in ("当前权重22.54%", "现有权重22.54%", "目前仓位22.54%",
                       "原有仓位22.54%", "当前持仓比例22.54%"):
            md = (
                "## 多头（Bull）\n\n"
                "| 标的 | 方向 | 理由 |\n|---|---|---|\n"
                f"| **512800** | **减** | {phrase}，明显超配。 |\n"
            )
            views = _views(tmp_path, md)
            for view in views.get("512800") or []:
                assert view["target_pct"] != 22.54, (
                    f"🔴「{phrase}」被当成目标权重（target_pct={view['target_pct']!r}）"
                )

    def test_说话人自己的目标照常取(self, tmp_path: pathlib.Path) -> None:
        """③ 修法不许过头：现状之外**说话人自己**写出的落点照常取。"""
        # 形态①：方向格自带落点（「方向格自带百分比」口径）
        md_dir = (
            "## 多头（Bull）\n\n"
            "| 标的 | 方向 | 理由 |\n|---|---|---|\n"
            "| **512800** | **减至10%** | 当前权重22.54%，明显超配。 |\n"
        )
        views = _views(tmp_path, md_dir)
        assert views.get("512800"), "夹具没解析出 512800 ⇒ 断言不适用"
        assert views["512800"][0]["target_pct"] == 10.0, (
            f"方向格自带落点必须取到 10%；实得 {views['512800'][0]['target_pct']!r}"
        )
        # 形态②：理由格里有**目标锚点** + 说话人自己的落点
        md_reason = (
            "## 多头（Bull）\n\n"
            "| 标的 | 方向 | 理由 |\n|---|---|---|\n"
            "| **512800** | **减** | 当前权重22.54%，目标减至10%。 |\n"
        )
        views2 = _views(tmp_path, md_reason)
        assert views2.get("512800"), "夹具没解析出 512800 ⇒ 断言不适用"
        assert views2["512800"][0]["target_pct"] == 10.0, (
            f"理由格里的自身落点必须取到 10%；实得 {views2['512800'][0]['target_pct']!r}"
        )


# --------------------------------------------------------------------- ② 规则层的机械目标

class TestRuleLayerMechanicalTargetIsNotSpeakerTarget:
    """「机械目标X%」= **规则层**的数（表头口径 A14 已有先例）⇒ 不是说话人的目标。"""

    def test_真机机械目标16_不得成为多头目标(self, tmp_path: pathlib.Path) -> None:
        views = _views(tmp_path, _MD_BULL)
        for view in views.get("512800") or []:
            assert view["target_pct"] != 16.0, (
                f"🔴「高于机械目标16%」是**规则层**的数，被当成多头目标了"
                f"（target_pct={view['target_pct']!r}）"
            )

    def test_机械目标_不得与减方向构成假冲突(self, tmp_path: pathlib.Path) -> None:
        """🔴 下一轮的现实场景：512800 已卖到 7.00%（< 机械目标 16%）
        ⇒ 若把 16% 当成「减」的目标 ⇒ 「减 ⇒ 目标 < 当前」必然违反 ⇒ 假红灯。"""
        account = {"512800": 7.0, "现金": 93.0}
        md = (
            "## 多头（Bull）\n\n"
            "| 标的 | 方向 | 理由 |\n|---|---|---|\n"
            "| **512800** | **减** | 当前权重7.00%，低于机械目标16%，继续减。 |\n"
        )
        path = _write(tmp_path, md)
        views = FRP._llm_target_views(path, total_assets=_TOTAL_ASSETS,
                                      account_weights=account)
        plan = {"llm_targets": {"512800": views["512800"][0]}} if views.get("512800") else {}
        from verify_artifacts import _check_llm_direction_semantics  # noqa: PLC0415
        ok, detail = _check_llm_direction_semantics(plan)
        assert ok, f"🔴「机械目标16% + 方向减」造成假冲突：{detail}"


# --------------------------------------------------------------------- ④ 既有口径不许被改坏

class TestExistingCalibersUnchanged:
    def test_当前目标20_仍是目标(self) -> None:
        assert FRP._extract_target_pct("当前目标 20%") == 20.0, (
            "「当前**目标** 20%」的限定词修饰的是「目标」⇒ 20% 仍是合法目标"
        )

    def test_目标收益20_仍是目标(self) -> None:
        assert FRP._extract_target_pct("目标收益 20%") == 20.0, (
            "「目标收益 20%」是合法目标（reviewer A2 实测口径）"
        )

    def test_当前目标20_经锚点取数仍是20(self) -> None:
        assert FRP._extract_target_pct("当前目标 20%", anchor_required=True) == 20.0


# --------------------------------------------------------------------- ⑤ 真机研究经理行不受影响

def test_真机研究经理行_目标20_方向持有_不受影响(tmp_path: pathlib.Path) -> None:
    views = _views(tmp_path, _MD_RESEARCH)
    assert views.get("512800"), "夹具没解析出 512800 ⇒ 断言不适用"
    view = views["512800"][0]
    assert view["target_pct"] == 20.0, (
        f"真机研究经理行的目标必须仍是 20%；实得 {view['target_pct']!r}"
    )
    assert view["direction"] == "持有", (
        f"真机研究经理行的方向必须仍是「持有」；实得 {view['direction']!r}"
    )


# --------------------------------------------------------------------- ⑥ 变异反证

def test_变异反证_回退现状护栏_必须转红(tmp_path: pathlib.Path) -> None:
    """把「现状限定词」护栏回退（永不匹配）⇒ 幽灵目标 22.54% 必须复活 ⇒ 本测试有牙。"""
    original = FRP._CURRENT_STATE_PHRASE_RE
    try:
        FRP._CURRENT_STATE_PHRASE_RE = re.compile(r"(?!x)x")
        views = _views(tmp_path, _MD_BULL)
        got = [v["target_pct"] for v in (views.get("512800") or [])]
        assert 22.54 in got, (
            f"变异后 22.54% 仍未复活 ⇒ 这条断言没有牙（got={got!r}）"
        )
    finally:
        FRP._CURRENT_STATE_PHRASE_RE = original


def test_变异反证_回退机械目标护栏_必须转红(tmp_path: pathlib.Path) -> None:
    """把「规则层机械目标」护栏回退（词表清空）⇒ 16% 必须复活 ⇒ 本测试有牙。"""
    original = FRP._RULE_LAYER_NUM_LABELS
    try:
        FRP._RULE_LAYER_NUM_LABELS = ()
        views = _views(tmp_path, _MD_BULL)
        got = [v["target_pct"] for v in (views.get("512800") or [])]
        assert 16.0 in got, (
            f"变异后 16% 仍未复活 ⇒ 这条断言没有牙（got={got!r}）"
        )
    finally:
        FRP._RULE_LAYER_NUM_LABELS = original
