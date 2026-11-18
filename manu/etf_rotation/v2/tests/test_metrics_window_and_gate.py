"""🔴 V2 「指标窗口 + 换手目标闸门」的回归测试。

**事故背景（为什么要钉这些）**：审计实测发现两处口径脱钩：

  ① **指标窗口与 val 声明脱钩** —— val 段超参搜索的目标函数把**整段历史净值**
     （2020-01-02 → 2026-11-10）交给 ``metrics.summarize()`` 求年化
     ⇒ 指标里**含测试段收益**（违反红线）。
  ② **年化换手被低估** —— ``annualized_turnover = total_turnover / years``，
     而 ``years`` 由**传入 nav 的长度**决定 ⇒ nav 是全史、换手只有 val 段时
     **年化换手被低估约 3.4 倍**（实测：objective 报 4.99×，val 段真值 17.13×）
     ⇒ 用户要求的「换手 ≤ 4×」目标**直接失效**。

本文件把两道防线钉死：

  * ``metrics.slice_window()`` ⇒ 净值 / 换手 / 基准**同时**切到同一窗口
    （杜绝「分子 val / 分母全史」）；
  * ``metrics.check_turnover_target()`` ⇒ 换手目标**硬闸门**
    （``annualized_turnover`` 算不出来 ⇒ **不许**当成达标）。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 改坏关键判定 ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。

🔴 本文件**只用自造小夹具** ⇒ 不读真实行情、不联网、不跑重量级 qlib 取数。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

#: `manu/etf_rotation/v2/` 目录（``metrics`` 用扁平导入 ``config_v2``）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import metrics  # noqa: E402

#: 被测模块源码（**只读**）
METRICS_SOURCE = _V2_DIR / "metrics.py"


# --------------------------------------------------------------------- 夹具
#: 全史净值（模拟「nav 给全史」的旧错法）：窗口前 2 天 + 窗口内 4 天 + 窗口后 2 天
_DATES = pd.to_datetime([
    "2021-12-30", "2021-12-31",                  # 窗口前
    "2022-01-04", "2022-01-05", "2022-01-06",    # 窗口内
    "2022-12-30",                                # 窗口内
    "2023-01-03", "2023-06-01",                  # 窗口后
])
_NAV_VALUES = [1.0, 1.01, 1.02, 1.03, 1.05, 1.20, 1.21, 1.30]
_BENCH_VALUES = [1.0, 1.005, 1.01, 1.02, 1.03, 1.10, 1.12, 1.15]

#: 换手（``backtest_v2`` 的 ``turnover_by_date`` 同形：``dict[str, float]``，
#: 键 = ``%Y-%m-%d``，只有调仓日才有值）—— 同样跨了窗口前后
_TURNOVER = {
    "2021-12-30": 0.5,
    "2022-01-04": 1.0,
    "2022-01-05": 0.0,
    "2022-12-30": 2.0,
    "2023-01-03": 0.7,
    "2023-06-01": 0.9,
}

#: 模拟 val 段窗口（与 ``config_v2.VALID`` 同形）
_START, _END = "2022-01-01", "2022-12-31"

#: 窗口内换手之和（= 1.0 + 0.0 + 2.0）
_TURN_SUM_IN_WINDOW = 3.0


def _nav() -> pd.Series:
    return pd.Series(_NAV_VALUES, index=_DATES, name="nav")


def _bench() -> pd.Series:
    return pd.Series(_BENCH_VALUES, index=_DATES, name="bench")


def _turnover_dict() -> dict:
    return dict(_TURNOVER)


# ----------------------------------------------------------------- 断言助手
def _assert_same_window(nav_s, turn_s, bench_s, start, end) -> None:
    """「三者同窗」的**核心断言**（正常测试与变异反证共用 ⇒ 变异时**必须转红**）。

    🔴 这一条就是本次修复的牙齿：净值 / 换手 / 基准**必须同时**落在 ``[start, end]``。
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    assert isinstance(nav_s, pd.Series) and len(nav_s) > 0, "🔴 切片后净值不能为空"
    assert nav_s.index.min() >= start_ts, (
        f"🔴 净值早于窗口起点 {start}：{nav_s.index.min()}"
    )
    assert nav_s.index.max() <= end_ts, (
        f"🔴 净值晚于窗口终点 {end}：{nav_s.index.max()}"
    )

    assert isinstance(bench_s, pd.Series) and len(bench_s) > 0, "🔴 切片后基准不能为空"
    assert bench_s.index.min() >= start_ts, (
        f"🔴 基准早于窗口起点 {start}：{bench_s.index.min()}"
    )
    assert bench_s.index.max() <= end_ts, (
        f"🔴 基准晚于窗口终点 {end}：{bench_s.index.max()}"
    )

    assert isinstance(turn_s, pd.Series) and len(turn_s) > 0, "🔴 切片后换手不能为空"
    turn_dates = pd.to_datetime(pd.Index(list(turn_s.index)))
    assert turn_dates.min() >= start_ts, (
        f"🔴 换手日期跑出窗口（早于 {start}）：{turn_dates.min()}"
    )
    assert turn_dates.max() <= end_ts, (
        f"🔴 换手日期跑出窗口（晚于 {end}）：{turn_dates.max()}"
    )
    assert float(turn_s.sum()) == pytest.approx(_TURN_SUM_IN_WINDOW, abs=1e-12), (
        f"🔴 换手总和必须是窗口内的 {_TURN_SUM_IN_WINDOW}；实得 {float(turn_s.sum())!r}"
    )


def _numbers_in(text: str) -> list[float]:
    """从报错信息里抽出所有数字（⇒ 断言不依赖具体格式化格式）。"""
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", text)]


def _assert_mentions(message: str, *values: float) -> None:
    """断言报错信息里**写清了**给定的数值（实测值 / 目标值 / 超标幅度）。"""
    numbers = _numbers_in(message)
    for value in values:
        assert any(abs(n - value) < 1e-6 for n in numbers), (
            f"🔴 报错信息里必须写清数值 {value}；实得 {message!r}（解析到 {numbers!r}）"
        )


def _assert_not_counted_as_pass(result: dict) -> None:
    """「算不出来 ⇒ 不许当成达标」的**核心断言**（变异反证共用）。"""
    assert result["ok"] is False, (
        f"🔴 annualized_turnover 缺失 / NaN 时不许当成达标；实得 {result!r}"
    )


def _assert_over_target(result: dict, measured: float, target: float) -> None:
    """「超标 ⇒ ok=False 且信息写清实测/目标/超标幅度」的**核心断言**（变异反证共用）。"""
    assert result["ok"] is False, (
        f"🔴 年化换手 {measured} > 目标 {target} 时必须 ok=False；实得 {result!r}"
    )
    _assert_mentions(result["message"], measured, target, measured - target)


# ----------------------------------------------------------------- 变异反证
def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**（不碰真源码）。"""
    text = METRICS_SOURCE.read_text(encoding="utf-8")
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


# --------------------------------------------------------------------- ① 同窗
class TestSliceWindowSameWindow:
    def test_净值换手基准三者同窗(self) -> None:
        """🔴 本次修复的核心：三者**同时**落在 ``[start, end]``。"""
        nav_s, turn_s, bench_s = metrics.slice_window(
            _nav(), _turnover_dict(), _bench(), start=_START, end=_END
        )
        _assert_same_window(nav_s, turn_s, bench_s, _START, _END)

    def test_窗口外的净值被切掉(self) -> None:
        nav_s, _, _ = metrics.slice_window(
            _nav(), _turnover_dict(), _bench(), start=_START, end=_END
        )
        assert len(nav_s) == 4, f"🔴 窗口内只有 4 个净值点；实得 {len(nav_s)}"
        assert pd.Timestamp("2021-12-30") not in nav_s.index, "🔴 窗口前的净值没被切掉"
        assert pd.Timestamp("2023-01-03") not in nav_s.index, "🔴 窗口后的净值没被切掉"

    def test_窗口外的换手被切掉(self) -> None:
        _, turn_s, _ = metrics.slice_window(
            _nav(), _turnover_dict(), _bench(), start=_START, end=_END
        )
        assert len(turn_s) == 3, f"🔴 窗口内只有 3 个换手点；实得 {len(turn_s)}"
        assert "2021-12-30" not in set(turn_s.index), "🔴 窗口前的换手没被切掉"
        assert "2023-01-03" not in set(turn_s.index), "🔴 窗口后的换手没被切掉"
        assert "2023-06-01" not in set(turn_s.index), "🔴 窗口后的换手没被切掉"

    def test_切片后年化换手不再被低估(self) -> None:
        """🔴 旧错法（nav 全史 + 换手仅窗口内）⇒ 年化换手被低估；同窗后即真值。

        同一段换手、同一个窗口：nav 给全史时年份数被拉长 ⇒ 年化换手被低估；
        净值与换手**同窗**后，年化换手只由窗口内数据决定。
        """
        nav_full, turn_full, _ = metrics.slice_window(_nav(), _turnover_dict())
        nav_win, turn_win, _ = metrics.slice_window(
            _nav(), _turnover_dict(), start=_START, end=_END
        )
        m_full = metrics.summarize(nav_full, turnover=turn_full, periods_per_year=252)
        m_win = metrics.summarize(nav_win, turnover=turn_win, periods_per_year=252)
        assert m_full["annualized_turnover"] < m_win["annualized_turnover"], (
            "🔴 nav 给全史时年化换手必须被低估（分母年份被拉长）"
        )
        assert m_win["annualized_turnover"] == pytest.approx(
            _TURN_SUM_IN_WINDOW / ((len(nav_win) - 1) / 252.0), abs=1e-12
        ), "🔴 同窗后的年化换手必须 = 窗口内换手 / 窗口内年份"


# --------------------------------------------------------------------- ② 边界
class TestSliceWindowBounds:
    def test_两侧为None时原样返回(self) -> None:
        """两侧都 ``None`` ⇒ 不切片、不改数值、不抛错。"""
        nav, bench, turn = _nav(), _bench(), _turnover_dict()
        nav_s, turn_s, bench_s = metrics.slice_window(nav, turn, bench)
        assert nav_s.equals(nav), "🔴 无窗口时净值必须原样返回"
        assert bench_s.equals(bench), "🔴 无窗口时基准必须原样返回"
        assert isinstance(turn_s, pd.Series), "🔴 无窗口时换手也必须统一成 Series"
        assert float(turn_s.sum()) == pytest.approx(sum(turn.values()), abs=1e-12), (
            "🔴 无窗口时换手总和必须不变"
        )
        assert set(turn_s.index) == set(turn.keys()), "🔴 无窗口时换手日期必须齐全"

    def test_只给起点时只砍右边界(self) -> None:
        nav_s, turn_s, bench_s = metrics.slice_window(
            _nav(), _turnover_dict(), _bench(), start=_START
        )
        assert nav_s.index.min() >= pd.Timestamp(_START)
        assert nav_s.index.max() == _DATES.max(), "🔴 只给起点 ⇒ 右边界不该被砍"
        assert set(turn_s.index) == {"2022-01-04", "2022-01-05", "2022-12-30",
                                    "2023-01-03", "2023-06-01"}

    def test_只给终点时只砍左边界(self) -> None:
        nav_s, turn_s, bench_s = metrics.slice_window(
            _nav(), _turnover_dict(), _bench(), end=_END
        )
        assert nav_s.index.max() <= pd.Timestamp(_END)
        assert nav_s.index.min() == _DATES.min(), "🔴 只给终点 ⇒ 左边界不该被砍"
        assert set(turn_s.index) == {"2021-12-30", "2022-01-04", "2022-01-05", "2022-12-30"}

    def test_窗口外的换手日期一个都不许剩(self) -> None:
        _, turn_s, _ = metrics.slice_window(
            _nav(), _turnover_dict(), start=_START, end=_END
        )
        turn_dates = pd.to_datetime(pd.Index(list(turn_s.index)))
        assert (turn_dates >= pd.Timestamp(_START)).all()
        assert (turn_dates <= pd.Timestamp(_END)).all()


# --------------------------------------------------------------------- ③ 输入形态
class TestTurnoverInputForms:
    def test_dict与Series输入结果一致(self) -> None:
        turn = _turnover_dict()
        _, turn_dict, _ = metrics.slice_window(
            _nav(), turn, _bench(), start=_START, end=_END
        )
        _, turn_series, _ = metrics.slice_window(
            _nav(), pd.Series(turn), _bench(), start=_START, end=_END
        )
        assert turn_dict.equals(turn_series), (
            f"🔴 dict 与 Series 两种输入必须给出一致结果；"
            f"dict={turn_dict.to_dict()!r} vs series={turn_series.to_dict()!r}"
        )

    def test_DatetimeIndex的换手也统一成日期字符串(self) -> None:
        turn = _turnover_dict()
        turn_dt = pd.Series(list(turn.values()), index=pd.to_datetime(list(turn.keys())))
        _, turn_out, _ = metrics.slice_window(
            _nav(), turn_dt, _bench(), start=_START, end=_END
        )
        _, turn_ref, _ = metrics.slice_window(
            _nav(), turn, _bench(), start=_START, end=_END
        )
        assert list(turn_out.index) == list(turn_ref.index), (
            f"🔴 DatetimeIndex 输入必须统一成日期字符串；"
            f"实得 {list(turn_out.index)!r}"
        )
        assert float(turn_out.sum()) == pytest.approx(_TURN_SUM_IN_WINDOW, abs=1e-12)

    def test_换手为None时输出None(self) -> None:
        nav_s, turn_s, bench_s = metrics.slice_window(
            _nav(), None, _bench(), start=_START, end=_END
        )
        assert turn_s is None, "🔴 换手为 None 时输出必须是 None"
        assert len(nav_s) == 4
        assert bench_s is not None

    def test_基准为None时输出None(self) -> None:
        nav_s, turn_s, bench_s = metrics.slice_window(
            _nav(), _turnover_dict(), None, start=_START, end=_END
        )
        assert bench_s is None, "🔴 基准为 None 时输出必须是 None"
        assert len(nav_s) == 4
        assert turn_s is not None


# --------------------------------------------------------------------- ④ 换手闸门
class TestCheckTurnoverTarget:
    def test_达标则ok为True(self) -> None:
        res = metrics.check_turnover_target({"annualized_turnover": 3.2}, 4.0, label="val 段")
        assert res["ok"] is True, f"🔴 3.2× ≤ 4.0× 必须达标；实得 {res!r}"
        assert res["annualized_turnover"] == pytest.approx(3.2, abs=1e-12)
        assert res["target"] == pytest.approx(4.0, abs=1e-12)
        assert "val 段" in res["message"], f"🔴 label 必须进报错信息；实得 {res['message']!r}"

    def test_超标则ok为False且信息写清实测目标超标幅度(self) -> None:
        res = metrics.check_turnover_target({"annualized_turnover": 17.13}, 4.0, label="val 段")
        _assert_over_target(res, 17.13, 4.0)
        assert "val 段" in res["message"]
        assert res["annualized_turnover"] == pytest.approx(17.13, abs=1e-12)

    def test_超标不抛错只返回False(self) -> None:
        """🔴 ``ok=False`` 时**不许抛错**（由调用方决定是拒绝冻结还是只记录）。"""
        res = metrics.check_turnover_target({"annualized_turnover": 99.0}, 4.0)
        assert res["ok"] is False

    def test_缺失时不算达标(self) -> None:
        _assert_not_counted_as_pass(metrics.check_turnover_target({}, 4.0))

    def test_NaN时不算达标(self) -> None:
        _assert_not_counted_as_pass(
            metrics.check_turnover_target({"annualized_turnover": float("nan")}, 4.0)
        )

    def test_None时不算达标(self) -> None:
        _assert_not_counted_as_pass(
            metrics.check_turnover_target({"annualized_turnover": None}, 4.0)
        )

    def test_算不出来时信息里写明目标(self) -> None:
        res = metrics.check_turnover_target({"annualized_turnover": float("nan")}, 4.0)
        _assert_mentions(res["message"], 4.0)
        assert res["annualized_turnover"] is None

    def test_目标为None时取config(self) -> None:
        res = metrics.check_turnover_target({"annualized_turnover": 1.0})
        assert res["target"] == pytest.approx(float(C.TURNOVER_TARGET), abs=1e-12), (
            f"🔴 target=None 必须取 config_v2.TURNOVER_TARGET；实得 {res['target']!r}"
        )
        assert res["ok"] is True

    def test_返回键固定(self) -> None:
        res = metrics.check_turnover_target({"annualized_turnover": 3.2}, 4.0)
        assert set(res) == {"ok", "annualized_turnover", "target", "message"}, (
            f"🔴 返回键必须固定；实得 {sorted(res)!r}"
        )


# --------------------------------------------------------------------- ⑤ 变异反证
def test_变异反证_去掉换手切片_三者同窗断言必须转红(tmp_path: Path) -> None:
    """把 ``slice_window`` 里的**换手切片**去掉 ⇒ 「三者同窗」断言**必须转红**。

    （这正是审计实测的旧错法：净值切了、换手没切 ⇒ 年化换手被低估。）
    """
    mutant = _mutated_module(
        tmp_path,
        [("turn_series = turn_series[_mask(_parse_turnover_keys(keys))]",
          "turn_series = turn_series")],
        "metrics_no_turn_slice",
    )
    nav_s, turn_s, bench_s = mutant.slice_window(
        _nav(), _turnover_dict(), _bench(), start=_START, end=_END
    )
    assert "2021-12-30" in set(turn_s.index), "🔴 变异体必须把窗口外的换手留下来"
    with pytest.raises(AssertionError):
        _assert_same_window(nav_s, turn_s, bench_s, _START, _END)
    # 真源码仍然同窗
    nav_s, turn_s, bench_s = metrics.slice_window(
        _nav(), _turnover_dict(), _bench(), start=_START, end=_END
    )
    _assert_same_window(nav_s, turn_s, bench_s, _START, _END)


def test_变异反证_去掉基准切片_三者同窗断言必须转红(tmp_path: Path) -> None:
    """把 ``slice_window`` 里的**基准切片**去掉 ⇒ 「三者同窗」断言**必须转红**。"""
    mutant = _mutated_module(
        tmp_path,
        [("bench_out = bench_s[_mask(bench_s.index)]", "bench_out = bench_s")],
        "metrics_no_bench_slice",
    )
    nav_s, turn_s, bench_s = mutant.slice_window(
        _nav(), _turnover_dict(), _bench(), start=_START, end=_END
    )
    assert pd.Timestamp("2023-01-03") in bench_s.index, "🔴 变异体必须把窗口外的基准留下来"
    with pytest.raises(AssertionError):
        _assert_same_window(nav_s, turn_s, bench_s, _START, _END)


def test_变异反证_NaN分支改成达标_不得当成达标断言必须转红(tmp_path: Path) -> None:
    """把 ``check_turnover_target`` 的 NaN 分支改成 ``ok=True`` ⇒ 断言**必须转红**。"""
    mutant = _mutated_module(
        tmp_path,
        [('            "ok": False,\n            "annualized_turnover": None,',
          '            "ok": True,\n            "annualized_turnover": None,')],
        "metrics_nan_ok",
    )
    with pytest.raises(AssertionError):
        _assert_not_counted_as_pass(
            mutant.check_turnover_target({"annualized_turnover": float("nan")}, 4.0)
        )
    # 真源码必须拦住
    _assert_not_counted_as_pass(
        metrics.check_turnover_target({"annualized_turnover": float("nan")}, 4.0)
    )


def test_变异反证_闸门比较被写松_超标断言必须转红(tmp_path: Path) -> None:
    """把闸门比较写成恒真 ⇒ 「超标 ⇒ ok=False」断言**必须转红**。"""
    mutant = _mutated_module(
        tmp_path,
        [("ok = bool(value <= target_value)", "ok = True")],
        "metrics_gate_always_ok",
    )
    with pytest.raises(AssertionError):
        _assert_over_target(
            mutant.check_turnover_target({"annualized_turnover": 17.13}, 4.0),
            17.13,
            4.0,
        )
    # 真源码必须拦住
    _assert_over_target(
        metrics.check_turnover_target({"annualized_turnover": 17.13}, 4.0), 17.13, 4.0
    )
