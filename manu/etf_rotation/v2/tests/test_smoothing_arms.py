"""🔴 V2 决策③：三臂（EWMA 半衰期自算 / 原生慢变量 / V1 现状对照）的钉死测试。

【为什么要有这些断言】
实盘持有周期 = **20 个交易日**，而 V1 的下游 EWMA（``alpha=0.20``）记忆只有
**3.11 个交易日** ⇒ 口径不匹配 ⇒ V2 必须在 val 段比完三臂并**定死一个**。
本文件钉死：

  ① **alpha 必须由半衰期自算**（``alpha = 1 - 0.5**(1/half_life)``），
     **不许写死字面量**（0.04516 / 0.03406 只是**自证值**）；
     且 ``effective_half_life_days`` 与它是**互逆**的（往返一致）；
  ② **臂 C 与 V1 ``score_smoothing.py`` 同参数逐值一致**（只读 import V1 ⇒ 交叉校验，
     防止两边口径漂移）；
  ③ **脉冲抗扰动**：前 4 天平稳、第 5 天脉冲 ×3 ⇒ 平滑后显著低于瞬态值
     且**未冲上第一**；
  ④ **冷启动不抛异常**；**全历史缺失** ⇒ 退化为瞬态分并输出
     ``WARNING: Fallback to transient score``；
  ⑤ **变异反证**：把实现源码**在内存里**改坏 ⇒ 对应断言**必须转红**
     （⛔ **绝不落盘改真源码** —— 真源码是主 agent 的写集）。

【V1 只读边界】
本文件是**唯一**允许 import V1 业务模块的地方（只读交叉校验）；
``v2/smoothing.py`` 本身**不 import** V1 ⇒ V2 硬隔离。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

#: `manu/etf_rotation/v2/` 目录（``smoothing`` 用扁平导入 ``config_v2`` ⇒ 必须进 sys.path）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import smoothing as SM  # noqa: E402

#: 被测模块源码（**只读**）
SMOOTHING_SOURCE = _V2_DIR / "smoothing.py"

#: V1 冻结基线目录（**只读**）
_V1_DIR = _V2_DIR.parent


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。

    ⛔ **绝不落盘改真源码**（真源码是主 agent 的写集）。
    锚点原文必须存在于源码里（不存在就报错 ⇒ 源码变了要同步改测试，不许悄悄放过）。
    """
    text = SMOOTHING_SOURCE.read_text(encoding="utf-8")
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


def _import_v1_score_smoothing():
    """🔴 **只读** import V1 的 ``score_smoothing.py``（V1 冻结基线 ⇒ 绝不修改）。"""
    if str(_V1_DIR) not in sys.path:
        sys.path.insert(0, str(_V1_DIR))
    import score_smoothing as v1  # noqa: PLC0415

    return v1


def _dense_panel(n_days: int = 15, seed: int = 20261117) -> tuple[pd.DataFrame, pd.DataFrame]:
    """人造「日期 × ETF 代码」**稠密**穿透分面板（V1 交叉校验用）。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2022-01-03", periods=n_days)
    codes = ["510300", "510500", "159915"]
    panel = pd.DataFrame(rng.normal(size=(len(days), len(codes))), index=days, columns=codes)
    obs = panel.stack().reset_index()
    obs.columns = ["date", "code", "score"]
    return panel, obs


# --------------------------------------------------------------------- ① alpha 自算
class TestAlphaSelfComputation:
    """``alpha`` **必须由半衰期自算**（不许写死字面量）。"""

    def test_半衰期15天与20天的alpha必须自算(self) -> None:
        a15 = SM.alpha_from_half_life(15)
        a20 = SM.alpha_from_half_life(20)
        # 公式逐位（比 1e-6 更严：真值必须精确等于公式）
        assert a15 == pytest.approx(1.0 - 0.5 ** (1.0 / 15.0), abs=1e-12)
        assert a20 == pytest.approx(1.0 - 0.5 ** (1.0 / 20.0), abs=1e-12)
        # 用户原文的字面量（**5 位小数口径**）：0.04516 / 0.03406
        assert round(a15, 5) == 0.04516, (
            f"🔴 15 天半衰期的 alpha 应为 0.04516（5 位小数），实得 {a15!r}"
        )
        assert round(a20, 5) == 0.03406, (
            f"🔴 20 天半衰期的 alpha 应为 0.03406（5 位小数），实得 {a20!r}"
        )
        # 🔴 字面量是 **5 位小数** ⇒ 自身的舍入误差最大 5e-6（实测 1.6e-6 / 3.7e-6）
        #    ⇒ 对**字面量**只能给 5e-6；1e-6 的严格容差落在上面的**公式逐位**断言上。
        assert abs(a15 - 0.04516) < 5e-6
        assert abs(a20 - 0.03406) < 5e-6

    def test_非法半衰期必须raise(self) -> None:
        for bad in (0, 0.0, -1, -15.0, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                SM.alpha_from_half_life(bad)

    def test_half_life反算必须与自算互逆(self) -> None:
        for half_life in (3.11, 5, 10, 15, 20, 30, 60):
            alpha = SM.alpha_from_half_life(half_life)
            assert SM.effective_half_life_days(alpha) == pytest.approx(half_life, abs=1e-9), (
                f"🔴 半衰期 {half_life} 天往返不一致（反算得 "
                f"{SM.effective_half_life_days(alpha)!r}）"
            )

    def test_alpha越界必须raise(self) -> None:
        for bad in (0.0, 1.0, -0.1, 1.5, float("nan")):
            with pytest.raises(ValueError):
                SM.effective_half_life_days(bad)

    def test_臂A的alpha必须等于自算值(self) -> None:
        specs = SM.arm_specs()
        for half_life in C.ARM_A_HALF_LIFE_DAYS:
            arm_id = f"A_half_life_{int(half_life)}"
            spec = specs[arm_id]
            assert spec.method == "ewma"
            assert spec.alpha == pytest.approx(SM.alpha_from_half_life(half_life), abs=1e-12), (
                f"🔴 臂 {arm_id} 的 alpha 不是自算值（{spec.alpha!r}）"
            )
            assert spec.half_life_days == pytest.approx(float(half_life), abs=1e-12)

    def test_臂注册表必须与config完全一致(self) -> None:
        specs = SM.arm_specs()
        assert set(specs) == set(C.SMOOTHING_ARM_IDS), (
            f"🔴 臂注册表与 config_v2.SMOOTHING_ARM_IDS 不一致：{sorted(specs)}"
        )
        assert specs[C.ARM_B_ID].method == "none", (
            "🔴 臂 B 必须是「不做下游 EWMA」（原生慢变量直通）"
        )
        assert specs[C.ARM_B_ID].native_slow is True


# --------------------------------------------------------------------- ② 臂 C × V1 交叉校验
class TestArmCAgainstV1:
    """臂 C（V1 现状对照）与 **V1 实现**的数值一致性。"""

    def test_臂C参数必须与config一致(self) -> None:
        spec = SM.arm_spec("C_v1_current")
        assert spec.method == "ewma"
        assert spec.alpha == pytest.approx(float(C.ARM_C_ALPHA), abs=1e-12)
        assert spec.window == int(C.ARM_C_WINDOW)
        assert spec.history_span_days == int(C.ARM_C_HISTORY_SPAN_DAYS)

    def test_臂C参数必须与V1配置一致(self) -> None:
        """🔴 V1 与 V2 的「现状对照」参数不许漂移（config_v2 的注释就是这么要求的）。"""
        v1 = _import_v1_score_smoothing()
        assert float(C.ARM_C_ALPHA) == pytest.approx(float(v1.DEFAULT_SMOOTHING_ALPHA), abs=1e-12), (
            f"🔴 V2 的 ARM_C_ALPHA={C.ARM_C_ALPHA} 与 V1 的 "
            f"SMOOTHING_ALPHA={v1.DEFAULT_SMOOTHING_ALPHA} 不一致 ⇒ 两边漂移了"
        )
        assert int(C.ARM_C_WINDOW) == int(v1.DEFAULT_SMOOTHING_WINDOW)
        assert int(C.ARM_C_HISTORY_SPAN_DAYS) == int(v1.DEFAULT_HISTORY_SPAN_DAYS)
        assert SM.FALLBACK_WARNING == v1.FALLBACK_WARNING == "WARNING: Fallback to transient score"

    def test_臂C与V1同参数必须逐值一致(self) -> None:
        """🔴 **逐值**交叉校验：同一块人造面板上，臂 C 与 V1 的 EWMA 结果必须一致。"""
        v1 = _import_v1_score_smoothing()
        panel, obs = _dense_panel()
        days = list(panel.index)
        spec = SM.arm_spec("C_v1_current")          # 🔴 V2 侧用**臂注册表**的参数
        v1_alpha = float(v1.DEFAULT_SMOOTHING_ALPHA)  # 🔴 V1 侧用 **V1 自己的配置**
        worst = 0.0
        for k in range(1, len(days) + 1):
            axis = days[:k]
            v1_out = v1.smooth_panel(obs, axis, method="ewma",
                                     window=int(v1.DEFAULT_SMOOTHING_WINDOW), alpha=v1_alpha)
            res = SM.smooth_panel(panel, axis, method="ewma",
                                  alpha=float(spec.alpha), window=int(spec.window))
            last = res.panel.iloc[-1]
            for _, row in v1_out.iterrows():
                mine = float(last[str(row["code"])])
                worst = max(worst, abs(float(row["smoothed_score"]) - mine))
        assert worst <= 1e-12, (
            f"🔴 臂 C 与 V1 同参数结果不一致（最大绝对偏差 {worst:.3e}）⇒ 两边口径漂移了"
        )

    def test_臂C的记忆半衰期必须仍是3_11天(self) -> None:
        """🔴 臂 C 的「问题根源」不许被悄悄改掉（3.11 天 ≠ 20 天持有周期）。"""
        spec = SM.arm_spec("C_v1_current")
        assert spec.half_life_days == pytest.approx(3.1062837, abs=1e-6)
        assert spec.half_life_days < float(C.REBALANCE_EVERY_N_DAYS)


# --------------------------------------------------------------------- ③ 脉冲抗扰动
class TestPulseImmunity:
    """前 4 天平稳 + 第 5 天脉冲 ×3 ⇒ 平滑后显著低于瞬态值且**未冲上第一**。"""

    @staticmethod
    def _pulse_panel() -> pd.DataFrame:
        days = pd.bdate_range("2022-01-03", periods=5)
        return pd.DataFrame(
            {
                "510300": [1.0, 1.0, 1.0, 1.0, 3.0],   # 第 5 天脉冲 ×3
                "510500": [2.0] * 5,
                "159915": [3.0] * 5,
            },
            index=days,
        )

    @pytest.mark.parametrize("arm_id", ["A_half_life_15", "A_half_life_20", "C_v1_current"])
    def test_脉冲不得把排名冲上第一(self, arm_id: str) -> None:
        panel = self._pulse_panel()
        days = list(panel.index)
        res = SM.apply_arm(panel, days, arm_id)
        last = res.panel.iloc[-1]
        transient = panel.iloc[-1]
        assert float(transient["510300"]) == pytest.approx(3.0), (
            "🔴 人造脉冲本身没造对（瞬态值必须是 3.0）"
        )
        assert float(last["510300"]) < 1.5, (
            f"🔴 臂 {arm_id} 平滑后仍高达 {float(last['510300']):.4f} ⇒ 脉冲没被抑制住"
        )
        ranking = last.sort_values(ascending=False)
        assert ranking.index[0] != "510300", (
            f"🔴 臂 {arm_id} 下脉冲标的仍冲上第一（排序 {list(ranking.index)}）"
        )
        assert ranking.index[-1] == "510300", (
            f"🔴 臂 {arm_id} 下脉冲标的没垫底（排序 {list(ranking.index)}）"
        )

    def test_脉冲标的的平滑值必须显著低于瞬态值(self) -> None:
        panel = self._pulse_panel()
        res = SM.apply_arm(panel, list(panel.index), "A_half_life_15")
        last = float(res.panel.iloc[-1]["510300"])
        assert last < 0.6 * float(panel.iloc[-1]["510300"]), (
            f"🔴 平滑后 {last:.4f} 相对瞬态值 3.0 只降了 "
            f"{1 - last / 3.0:.1%} ⇒ 抗扰动不足"
        )


# --------------------------------------------------------------------- ④ 冷启动 / 回退
class TestColdStartAndFallback:
    def test_冷启动不得抛异常且首值即自身(self) -> None:
        days = pd.bdate_range("2022-01-03", periods=4)
        panel = pd.DataFrame({"510300": [1.5, 1.6, 1.7, 1.8]}, index=days)
        res = SM.smooth_panel(panel, days, method="ewma", alpha=0.2)
        assert np.isfinite(res.panel.iloc[0, 0]), "🔴 冷启动产生了 NaN"
        assert float(res.panel.iloc[0, 0]) == pytest.approx(1.5, abs=1e-12), (
            "🔴 冷启动必须「首个有效观测即自身」（min_periods=1）"
        )

    def test_历史不足时不得抛NaN(self) -> None:
        days = pd.bdate_range("2022-01-03", periods=10)
        panel = pd.DataFrame({"510300": [np.nan] * 9 + [2.0]}, index=days)
        res = SM.smooth_panel(panel, days, method="ewma", alpha=0.2)
        assert np.isfinite(res.panel.iloc[-1, 0]), "🔴 历史不足时抛了 NaN"
        assert float(res.panel.iloc[-1, 0]) == pytest.approx(2.0, abs=1e-12)

    def test_全历史缺失必须退化为瞬态并告警(self, capsys) -> None:
        days = pd.bdate_range("2022-01-03", periods=5)
        later = pd.bdate_range("2022-06-01", periods=3)
        panel = pd.DataFrame({"510300": [1.0, 2.0, 3.0]}, index=later)   # 观测全在轴之后
        res = SM.smooth_panel(panel, days, method="ewma", alpha=0.2)
        assert res.diagnostics["fallback_to_transient"] is True, (
            "🔴 全历史不可用时必须标记 fallback_to_transient"
        )
        assert res.diagnostics["warning"] == SM.FALLBACK_WARNING, (
            f"🔴 回退告警字面量不对：{res.diagnostics['warning']!r}"
        )
        assert SM.FALLBACK_WARNING in capsys.readouterr().out, (
            "🔴 回退时必须把 WARNING: Fallback to transient score 打到日志"
        )

    def test_单点观测的标的必须退化为瞬态分(self) -> None:
        """V1 同口径：观测点 ≤ 1 ⇒ 退化为瞬态分（不许编数字）。"""
        days = pd.bdate_range("2022-01-03", periods=6)
        panel = pd.DataFrame({"510300": [2.5] * 6, "510500": [np.nan] * 5 + [4.0]}, index=days)
        res = SM.smooth_panel(panel, days, method="ewma", alpha=0.2)
        assert float(res.panel.iloc[-1]["510500"]) == pytest.approx(4.0, abs=1e-12)
        assert res.diagnostics["n_codes_fallback_transient"] == 1


# --------------------------------------------------------------------- ⑤ 变异反证
class TestMutationProofs:
    """🔴 变异反证：改坏源码里的关键判定 ⇒ 对应断言**必须转红**。"""

    def test_变异反证_alpha改成常量_自算断言必须转红(self, tmp_path: Path) -> None:
        """把 ``alpha_from_half_life`` 改成恒返回 0.2 ⇒ 半衰期自算断言必须转红。"""
        mutant = _mutated_module(
            tmp_path,
            [("    return 1.0 - 0.5 ** (1.0 / half_life)\n", "    return 0.2\n")],
            "smoothing_const_alpha",
        )
        with pytest.raises(AssertionError):
            assert mutant.alpha_from_half_life(15) == pytest.approx(
                1.0 - 0.5 ** (1.0 / 15.0), abs=1e-12
            )

    def test_变异反证_臂C的alpha改成035_与V1交叉校验必须转红(self, tmp_path: Path) -> None:
        """把臂 C 的 alpha 改成 0.35 ⇒ 与 V1 的逐值交叉校验必须转红。"""
        v1 = _import_v1_score_smoothing()
        mutant = _mutated_module(
            tmp_path,
            [("        alpha=float(C.ARM_C_ALPHA),\n", "        alpha=0.35,\n")],
            "smoothing_arm_c_035",
        )
        panel, obs = _dense_panel()
        days = list(panel.index)
        v1_out = v1.smooth_panel(obs, days, method="ewma",
                                 window=int(C.ARM_C_WINDOW), alpha=float(C.ARM_C_ALPHA))
        res = mutant.smooth_panel(panel, days, method="ewma",
                                  alpha=mutant.arm_spec("C_v1_current").alpha,
                                  window=int(C.ARM_C_WINDOW))
        last = res.panel.iloc[-1]
        worst = 0.0
        for _, row in v1_out.iterrows():
            worst = max(worst, abs(float(row["smoothed_score"]) - float(last[str(row["code"])])))
        assert worst > 1e-6, (
            f"🔴 变异后臂 C 与 V1 仍然一致（偏差 {worst:.3e}）⇒ 交叉校验这条断言没有牙"
        )

    def test_变异反证_去掉fallback告警_回退断言必须转红(self, tmp_path: Path, capsys) -> None:
        """把全局回退的告警去掉 ⇒ 「必须输出 WARNING」这条断言必须转红。"""
        mutant = _mutated_module(
            tmp_path,
            [(
                '        diagnostics["warning"] = FALLBACK_WARNING\n'
                '        diagnostics["n_codes_fallback_transient"] = int(len(codes))\n'
                '        print(FALLBACK_WARNING, flush=True)\n'
                '        smoothed = locf.copy()\n',
                '        diagnostics["n_codes_fallback_transient"] = int(len(codes))\n'
                '        smoothed = locf.copy()\n',
            )],
            "smoothing_no_fallback",
        )
        days = pd.bdate_range("2022-01-03", periods=5)
        later = pd.bdate_range("2022-06-01", periods=3)
        panel = pd.DataFrame({"510300": [1.0, 2.0, 3.0]}, index=later)
        res = mutant.smooth_panel(panel, days, method="ewma", alpha=0.2)
        assert res.diagnostics["warning"] is None, (
            "🔴 变异后仍有告警 ⇒ 「必须输出 WARNING」这条断言没有牙"
        )
        assert mutant.FALLBACK_WARNING not in capsys.readouterr().out, (
            "🔴 变异后仍打了告警 ⇒ 「必须输出 WARNING」这条断言没有牙"
        )

    def test_变异反证_臂B改成EWMA_原生慢变量断言必须转红(self, tmp_path: Path) -> None:
        """把臂 B 改成 EWMA ⇒ 「臂 B 不做下游 EWMA」这条断言必须转红。"""
        mutant = _mutated_module(
            tmp_path,
            [(
                '    specs[C.ARM_B_ID] = SmoothingArm(\n'
                '        arm_id=C.ARM_B_ID,\n'
                '        method="none",\n',
                '    specs[C.ARM_B_ID] = SmoothingArm(\n'
                '        arm_id=C.ARM_B_ID,\n'
                '        method="ewma",\n',
            )],
            "smoothing_arm_b_ewma",
        )
        spec = mutant.arm_spec("B_native_slow")
        assert spec.method != "none", (
            "🔴 变异后臂 B 仍在做 EWMA ⇒ 「臂 B = 原生慢变量直通」这条断言没有牙"
        )
