"""🔴 V2 特征清单（Alpha158）与特征消融（对齐①）的回归测试。

**事故背景（为什么要钉这些）**：V2 的对齐①要求「Label = 未来 20 交易日超额收益」
并把「5 日内超短线反转特征」列为**消融对象**。这条消融如果**名不副实**
（削掉了别的特征、或根本没削掉），对照实验的结论就整个作废 ⇒ 必须钉死：

  ① 基线清单里**必须有** ``ROC5``（Alpha158 的 ``Ref($close,5)/$close``
     = 5 日收益的**相反数** ⇒ 高 ROC5 才是超短线反转信号）；
  ② 消融 ``ROC5`` ⇒ 它**真的不在**清单里，且特征总数**恰好少 1**；
  ③ ``drop`` 里写错特征名 ⇒ **当场报错**（静默忽略 ⇒ 消融实验悄悄变成别的实验）；
  ④ 消融 ROC 族 ⇒ **不许**牵连同窗口的 ``MA5`` / ``STD5``
     （口径已在 ``config_v2.ABLATION_GROUPS`` 定死，模块只负责按名精确剔除）。

【变异反证】每条关键判定都配一个「源码 → 内存改 → tmp_path → import 副本」的
变异测试（沿用本仓库既有写法）⇒ 改坏源码里的关键判定 ⇒ 对应断言**必须转红**。
⛔ **绝不落盘改真源码**（真源码是主 agent 的写集）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

#: `manu/etf_rotation/v2/` 目录（``features`` 用扁平导入 ``config_v2`` ⇒ 必须进 sys.path）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import features  # noqa: E402

#: 被测模块源码（**只读**）
FEATURES_SOURCE = _V2_DIR / "features.py"


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。

    锚点原文必须存在于源码里（不存在就报错 ⇒ 源码变了要同步改测试，不许悄悄放过）。
    """
    text = FEATURES_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- ① 基线


class TestAlpha158Baseline:
    """基线清单 = qlib 上游 Alpha158（**只读 import**，不手抄）。"""

    def test_ROC5必须在基线清单里(self) -> None:
        mapping = dict(features.alpha158_feature_config())
        assert "ROC5" in mapping, (
            f"🔴 基线清单里没有 ROC5 ⇒「5 日内超短线反转」消融实验无从谈起；"
            f"实得前若干名 {list(mapping)[:8]}"
        )

    def test_ROC5的表达式必须是5日价格比(self) -> None:
        expr = dict(features.alpha158_feature_config())["ROC5"]
        assert expr.replace(" ", "") == features.ROC5_EXPRESSION, (
            f"🔴 ROC5 表达式变成了 {expr!r}（应为 {features.ROC5_EXPRESSION!r}）"
            "⇒ 消融口径（5 日反转）已失效"
        )

    def test_基线特征数与qlib上游一致(self) -> None:
        cfg = features.alpha158_feature_config()
        assert len(cfg) == 158, f"🔴 Alpha158 基线应为 158 个特征，实得 {len(cfg)}"
        assert len(dict(cfg)) == len(cfg), "🔴 特征名有重复 ⇒ 消融会误伤"


# --------------------------------------------------------------------- ② 消融


class TestDrop:
    """``drop`` = 按特征名**精确剔除**。"""

    def test_drop_ROC5后_它不在清单里且总数少1(self) -> None:
        base = features.alpha158_feature_config()
        dropped = features.alpha158_feature_config(("ROC5",))
        assert len(dropped) == len(base) - 1, (
            f"🔴 消融 ROC5 后特征数应为 {len(base) - 1}，实得 {len(dropped)}"
        )
        assert "ROC5" not in dict(dropped), "🔴 ROC5 还在清单里 ⇒ 消融没生效"

    def test_drop不存在的特征必须报错(self) -> None:
        with pytest.raises(ValueError):
            features.alpha158_feature_config(("NOT_A_REAL_FEATURE",))

    def test_ROC族消融不影响MA5与STD5(self) -> None:
        """🔴 同窗口的 MA / STD 族**不许**被 ROC 消融牵连（口径在 config 定死）。"""
        base = dict(features.alpha158_feature_config())
        after = dict(features.alpha158_feature_config(("ROC5", "ROC10", "ROC20")))
        for name in ("MA5", "STD5"):
            assert name in after, f"🔴 ROC 族消融把 {name} 也削掉了 ⇒ 口径串了"
            assert after[name] == base[name], f"🔴 {name} 的表达式被改了"

    def test_config里的消融组名都真实存在(self) -> None:
        """消融组（config_v2.ABLATION_GROUPS）里的名字必须在清单里
        ⇒ config 与本模块的口径不会悄悄脱节。"""
        import config_v2 as C  # noqa: PLC0415

        names = {name for name, _ in features.alpha158_feature_config()}
        for group, drop in C.ABLATION_GROUPS.items():
            missing = [name for name in drop if name not in names]
            assert not missing, f"🔴 消融组 {group} 里的 {missing} 不在 Alpha158 清单中"


# --------------------------------------------------------------------- ③ 消融报告


class TestAblationReport:
    """``ablation_report`` = **披露型**接口（不 raise，但一定点名缺失项）。"""

    def test_报告键固定(self) -> None:
        report = features.ablation_report()
        assert set(report) == {
            "dropped", "n_total", "n_kept", "dropped_found", "dropped_missing",
        }

    def test_未知名必须被点名(self) -> None:
        report = features.ablation_report(("ROC5", "NOT_A_REAL_FEATURE"))
        assert report["dropped_found"] == ["ROC5"], (
            f"🔴 报告没认出 ROC5：{report!r}"
        )
        assert report["dropped_missing"] == ["NOT_A_REAL_FEATURE"], (
            f"🔴 报告没点名缺失特征（静默忽略 ⇒ 消融实验名不副实）：{report!r}"
        )
        assert report["n_kept"] == report["n_total"] - 1


# --------------------------------------------------------------------- ④ 变异反证


def test_变异反证_剔除逻辑改成空操作_ROC5必须留得住(tmp_path: Path) -> None:
    """把「按名剔除」改成空操作 ⇒ ROC5 必须还在清单里 ⇒ 证明第②条断言有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("    drop_set = set(drop)\n", "    drop_set = set()\n")],
        "features_no_drop",
    )
    kept = dict(mutant.alpha158_feature_config(("ROC5",)))
    assert "ROC5" in kept, (
        f"🔴 变异后 ROC5 仍未复活 ⇒ 这条断言没有牙（kept={list(kept)[:5]}…）"
    )


def test_变异反证_ROC5断言被摘掉_上游少给ROC5也不报红(tmp_path: Path) -> None:
    """把 ROC5 断言摘掉 + 上游清单里没有 ROC5 ⇒ **不许**报 AssertionError
    ⇒ 证明第①条断言（而不是别的东西）在把关。"""
    mutant = _mutated_module(
        tmp_path,
        [(
            "    if C.REVERSAL_FEATURE not in mapping:\n"
            "        raise AssertionError(\n"
            "            f\"🔴 Alpha158 清单里没有消融锚点 {C.REVERSAL_FEATURE} ⇒ \"\n"
            "            f\"「5 日内超短线反转」消融实验无法执行（上游 Alpha158 已变？）\"\n"
            "        )\n"
            "    actual = mapping[C.REVERSAL_FEATURE].replace(\" \", \"\")\n"
            "    if actual != ROC5_EXPRESSION:\n"
            "        raise AssertionError(\n"
            "            f\"🔴 {C.REVERSAL_FEATURE} 的表达式变成了 {mapping[C.REVERSAL_FEATURE]!r}，\"\n"
            "            f\"与预期的 {ROC5_EXPRESSION!r} 不符 ⇒ 消融口径（5 日反转）已失效\"\n"
            "        )\n",
            "    pass\n",
        )],
        "features_no_roc5_guard",
    )

    class _FakeAlpha158:
        @staticmethod
        def get_feature_config():
            fields = ["$close/$open"]
            names = ["KMID"]
            return fields, names

    original = mutant.Alpha158DL
    mutant.Alpha158DL = _FakeAlpha158
    try:
        mutant.alpha158_feature_config()      # 变异后：不再报红
    finally:
        mutant.Alpha158DL = original

    # 真模块：同样的「上游少给 ROC5」必须**报红**
    original_real = features.Alpha158DL
    features.Alpha158DL = _FakeAlpha158
    try:
        with pytest.raises(AssertionError):
            features.alpha158_feature_config()
    finally:
        features.Alpha158DL = original_real
