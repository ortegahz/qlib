"""`qlib_scores.py` 收尾统计量**呈现**的单测（离线：不连 qlib、不打分、不联网）。

【要防的回归】
2026-11-05 实测：`--mode infer` 只打 **1 个评分日**时，IC 系列统计量是 ``None``
（打分日之后没有未来收益 ⇒ 标签算不出来 ⇒ IC 算不出来），
旧代码直接 `f"{ic['ic']['mean']:.6f}"` ⇒
``TypeError: unsupported format string passed to NoneType.__format__``
⇒ `--mode infer` **非零退出** ⇒ 上游 `daily/agents_rebalance.py` 阶段 2 被判失败
⇒ 六阶段流水线中断。

【铁律六（禁编数字）】
算不出来的统计量**渲染「—」**：不许编、不许填 0、不许填 "None"。

【覆盖清单】
1. `fmt_stat`：`None` / `NaN` / `inf` → 「—」；正常值按格式串正常渲染；
2. 旧写法 `f"{None:.6f}"` 确实抛 `TypeError`（证明回归测试有效）；
3. 单日 + 标签全 NaN ⇒ IC 全 `None`，摘要行渲染「—」，**不含 0.000000 / None**；
4. 缺失原因说明里点明「仅 1 个评分日」与「标签窗口超出数据集末日」；
5. 只有 1 个**有效** IC 日 ⇒ `std` 必须是 `None`（不是编出来的 0.0），ICIR/t 缺失；
6. 2 个有效 IC 日 ⇒ mean/std/icir/t 齐备，且无缺失说明；
7. 源码级守卫：统计量键上**不许**直接套 `:.Nf` 格式（必须走 `fmt_stat`）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import qlib_scores as QS


def _scores(dates: list[str], n: int = 100, valid_label: bool = True) -> pd.DataFrame:
    """构造打分表；``valid_label=False`` ⇒ 标签（前瞻收益）全 NaN（末日无未来收益）。"""
    frames = []
    for day_idx, d in enumerate(dates):
        # 🔴 每天**不同种子**（同种子 ⇒ 两天截面逐位相同 ⇒ IC 恒等 ⇒ std=0 ⇒ ICIR 算不出来）
        rng = np.random.default_rng(1000 + day_idx)
        score = rng.normal(size=n)
        if valid_label:
            fwd_ret = score * (0.3 + 0.1 * day_idx) + rng.normal(size=n) * 0.1
        else:
            fwd_ret = np.full(n, np.nan)
        frames.append(
            pd.DataFrame(
                {
                    "date": pd.Timestamp(d),
                    "instrument": [f"{d}_{i:04d}" for i in range(n)],
                    "score": score,
                    "fwd_ret": fwd_ret,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


# ------------------------------------------------------------------ fmt_stat


def test_fmt_stat_missing_values_render_dash():
    assert QS.fmt_stat(None) == "—"
    assert QS.fmt_stat(float("nan")) == "—"
    assert QS.fmt_stat(float("inf")) == "—"
    assert QS.fmt_stat(float("-inf")) == "—"
    assert QS.fmt_stat(np.float64("nan")) == "—"
    assert QS.fmt_stat(None, ".4f") == "—"


def test_fmt_stat_normal_values_keep_format():
    assert QS.fmt_stat(0.0123456789) == "0.012346"
    assert QS.fmt_stat(0.5, ".4f") == "0.5000"
    assert QS.fmt_stat(1.23456, ".3f") == "1.235"
    assert QS.fmt_stat(5575, ".0f") == "5575"


def test_old_fstring_style_really_raises_typeerror():
    """旧代码写法必须仍然抛错 —— 证明上面那些用例守的是真回归。"""
    with pytest.raises(TypeError):
        f"{None:.6f}"  # noqa: F841  —— 旧代码的写法


# ------------------------------------------------------------------ 单日场景


def test_single_day_summary_line_renders_dash_and_does_not_crash():
    ic = QS.ic_stats(_scores(["2026-11-03"], valid_label=False))
    assert ic["ic"]["mean"] is None
    assert ic["ic"]["icir"] is None
    assert ic["ic"]["t"] is None
    assert ic["rank_ic"]["mean"] is None
    assert ic["coverage"]["n_dates"] == 1

    line = QS.format_ic_summary(ic)  # 旧代码在这里就炸了
    assert "—" in line
    assert "None" not in line
    assert "0.000000" not in line  # 铁律六：缺失不许填 0
    assert "2026-11-03" not in line  # 摘要行不含日期，日期在「IC 说明」里


def test_single_day_missing_reason_points_at_real_cause():
    dates = [pd.Timestamp("2026-11-03")]
    ic = QS.ic_stats(_scores(["2026-11-03"], valid_label=False))
    reason = QS.ic_stat_reason(ic, dates, dataset_max="2026-11-03")
    assert "仅 1 个评分日" in reason
    assert "2026-11-03" in reason
    assert "标签窗口" in reason
    assert "—" in reason


def test_label_horizon_days_matches_config_label():
    """标签前瞻天数从 config 解析（不写死常量）⇒ 与 ``Ref($vwap, -2)`` 一致。"""
    assert QS.label_horizon_days() == 2


# ------------------------------------------------------------------ 有效 IC 日


def test_one_valid_ic_day_std_is_none_not_fabricated_zero():
    ic = QS.ic_stats(_scores(["2026-11-03"], valid_label=True))
    assert ic["ic"]["n_days"] == 1
    assert ic["ic"]["mean"] is not None
    assert ic["ic"]["std"] is None  # 铁律六：ddof=1 单样本标准差不存在 ⇒ 不许写 0.0
    assert ic["ic"]["icir"] is None
    assert ic["ic"]["t"] is None
    reason = QS.ic_stat_reason(ic, [pd.Timestamp("2026-11-03")], dataset_max="2026-11-03")
    assert "有效 IC 日仅 1 个" in reason


def test_two_valid_ic_days_all_stats_present():
    ic = QS.ic_stats(_scores(["2026-11-03", "2026-10-20"], valid_label=True))
    assert ic["ic"]["n_days"] == 2
    for key in ("mean", "std", "icir", "t"):
        assert isinstance(ic["ic"][key], float), key
        assert isinstance(ic["rank_ic"][key], float), key
    assert QS.ic_stat_reason(ic, [], dataset_max="2026-11-03") == ""
    line = QS.format_ic_summary(ic)
    assert "—" not in line


def test_no_valid_day_branch_keeps_same_key_shape():
    """「一个有效 IC 日都没有」与「有一个」两条分支的 key 必须一致（下游别 KeyError）。"""
    empty = QS.ic_stats(_scores(["2026-11-03"], valid_label=False))
    one = QS.ic_stats(_scores(["2026-11-03"], valid_label=True))
    assert set(empty["ic"]) == set(one["ic"])
    assert empty["ic"]["icir_annualized"] is None


# ------------------------------------------------------------------ 源码级守卫


#: 可能缺失的统计量键（``None`` / ``NaN`` ⇒ 旧写法会炸或会印出 "None"）
_MISSING_KEY_RE = re.compile(
    r"""\[\s*['"](?:mean|icir|t|std|spearman|max_abs_diff)['"]\s*\]"""
)


def test_no_direct_interpolation_of_missing_prone_stat_keys():
    """f-string 里**不许直接插值**可能缺失的统计量 —— 必须走 `fmt_stat`（缺失渲染「—」）。

    用 AST 扫（不是正则扫源码文本）⇒ docstring 里引用的旧写法**不会**被误判成违规。
    """
    tree = ast.parse(Path(QS.__file__).read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue):
            continue
        value = node.value
        if isinstance(value, ast.Call) and ast.unparse(value.func).endswith("fmt_stat"):
            continue  # 已经走 fmt_stat ⇒ 合规
        rendered = ast.unparse(value)
        if _MISSING_KEY_RE.search(rendered):
            hits.append(rendered)
    assert not hits, f"这些统计量被直接插值进 f-string（缺失时会印出 None / 炸掉）：{hits}"


def test_format_specs_only_via_fmt_stat():
    """带 ``:.Nf`` 的插值一律不许直接作用在可能缺失的统计量键上。"""
    tree = ast.parse(Path(QS.__file__).read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue):
            continue
        if node.format_spec is None:
            continue
        spec = ast.unparse(node.format_spec)
        if not re.search(r":\.\d", spec):
            continue
        value = node.value
        if isinstance(value, ast.Call) and ast.unparse(value.func).endswith("fmt_stat"):
            continue
        rendered = ast.unparse(value)
        if _MISSING_KEY_RE.search(rendered):
            hits.append(rendered)
    assert not hits, f"这些统计量上仍直接套了格式串（会再炸一次）：{hits}"
