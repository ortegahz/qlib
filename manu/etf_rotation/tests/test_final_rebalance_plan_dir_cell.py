"""🔴 2026-11-10 新增：两个**真缺陷**的回归（LLM 方向「后置否定」 + 前视审计排序）。

事故来源（2026-11-10 上午服务器实跑，asof=2026-11-09，产物快照
``<结果根>/snapshots/20261110_120420``）：

  ① **LLM 方向解析不认「后置否定」**（BLOCKING）
     LLM 空头表的方向格写「**买：否**」（同行目标权重 **0%**）⇒ 旧口径只认
     **前缀**否定（不作为 / 不作 / 不予以 / 不当 / 不打算 / 不计划）⇒ 方向被判成
     「买」（POS）⇒ 与目标 0% **自相矛盾** ⇒ 交付件自带守卫
     「LLM 方向-权重语义自洽」报 **4 条异常**
     （159207 / 512170 / 515450 / 515650，四条全是**空头视图**的 ``direction='买'``）。

  ② **前视审计的排序把 None 排到了最前**（真缺陷）
     ``lookahead._tightest_realistic`` 的排序键把 ``margin_realistic is None`` 的行
     打成 ``(0, 0.0)``、把真值打成 ``(1, -mr)`` ⇒ **None 反而排最前**
     ⇒ 与它自己 docstring「None 排最后」**正好相反**
     ⇒ 本轮报告把「现实档余量最大」口径表首行点成**余量不可得**的那一对，
     而真正余量最大的是 −0.079164（512800/515020）
     ⇒ 报告内部自相矛盾（正文一处说「不可得」、另一处说「余量 −0.0792」）
     ⇒ 闸门 L17 失败。

本文件钉死两件事（自带夹具，**不依赖服务器产物**）：

  ① **后置否定**（「买：否」「买:否」「买(否)」「买/否」「是否买：否」「买，不」「买 - 否」）
     ⇒ 方向必须是「**不买**」（ZERO 类 ⇒ 目标必须 0%，与原文一致）；
     且**不许**误伤既有口径（前缀否定、部分卖出、真买入句）。
  ② **None 排最后** ⇒ 排序后第一行必须是余量最大的那一行，
     且必须等于 ``max_same_theme_margin_realistic``（= 闸门 L17 的口径）。

另附**变异反证**：把两处修复分别改回旧口径 ⇒ 对应断言必须**转红**（证明本测试有牙）。
"""

from __future__ import annotations

import pathlib
import sys

import pandas as pd
import pytest

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import analyze as AN  # noqa: E402
import final_rebalance_plan as FRP  # noqa: E402
import lookahead as LA  # noqa: E402


# --------------------------------------------------------------------------- ① 方向格
class TestClassifyDirCell:
    """``final_rebalance_plan._classify_dir_cell``：后置否定 + 既有口径回归。"""

    # ---- 后置否定（本轮事故的真凶）----
    def test_postfix_negation_is_not_buy(self) -> None:
        """① 「买：否」这类写法 ⇒ 方向 = 「不买」（**不许**返回空串，也不许判成「买」）。"""
        for cell in ("买：否", "买:否", "买(否)", "买/否", "是否买：否", "买，不", "买 - 否"):
            direction = FRP._classify_dir_cell(cell)
            assert direction == "不买", f"{cell!r} 必须判成「不买」，实得 {direction!r}"
            assert direction, f"{cell!r} 不许返回空串（守卫会把「未分类方向词」判 FAIL）"
            assert direction in FRP._ZERO_DIR_WORDS, (
                f"{cell!r} 的方向必须属 ZERO 类 ⇒ 目标权重必须 0%（与原文一致）"
            )

    def test_postfix_negation_with_markdown_emphasis(self) -> None:
        """① 真实事故件的方向格带 markdown 强调（「**买：否**」）⇒ 同样判「不买」。"""
        assert FRP._classify_dir_cell("**买：否**") == "不买"

    def test_postfix_negation_with_trailing_remark(self) -> None:
        """① 「买：否（估值偏高）」⇒ 仍是「不买」（括号备注里没有方向词）。"""
        assert FRP._classify_dir_cell("买：否（估值偏高）") == "不买"

    # ---- 既有口径回归（不许被新规则改坏）----
    def test_plain_buy_still_buy(self) -> None:
        """① 回归：「买」⇒ 「买」（POS）。"""
        assert FRP._classify_dir_cell("买") == "买"
        assert FRP._classify_dir_cell("买入") == "买入"

    def test_prefix_negation_still_not_buy(self) -> None:
        """① 回归：既有的**前缀**否定不许被改坏。"""
        assert FRP._classify_dir_cell("不买") == "不买"
        assert FRP._classify_dir_cell("不作为防守仓买入") == "不买"

    def test_partial_sell_still_reduce(self) -> None:
        """① 回归：「卖出约 2,600 股」= **部分卖出**（减），最高优先级，不许被新规则命中。"""
        assert FRP._classify_dir_cell("卖出约 2,600 股") == "减"

    def test_reduce_and_liquidation(self) -> None:
        """① 回归：「减」⇒ 「减」；「清仓」⇒ ZERO 类（守卫口径下与「卖」等价）。"""
        assert FRP._classify_dir_cell("减") == "减"
        direction = FRP._classify_dir_cell("清仓")
        assert direction in ("卖", "清仓"), f"「清仓」必须落进 ZERO 类，实得 {direction!r}"
        assert direction in FRP._ZERO_DIR_WORDS, "「清仓」必须属 ZERO 方向词（⇒ 目标必须 0%）"

    def test_true_buy_sentence_is_not_misjudged(self) -> None:
        """① 精确性闸门：「买入，不做波段」「买入（不追高）」是**真买入** ⇒ 不许判成「不买」。"""
        assert FRP._classify_dir_cell("买入，不做波段") == "买入"
        assert FRP._classify_dir_cell("买入（不追高）") == "买入"

    def test_old_impl_would_misjudge(self) -> None:
        """① 变异反证：旧口径（只认前缀否定 + 最左长词）会把「买：否」判成「买」⇒ 本测试有牙。"""
        cell = "**买：否**"
        old_direction = max(
            FRP._LLM_DIR_CELL.finditer(cell),
            key=lambda _m: (len(_m.group(1)), -_m.start()),
        ).group(1)
        assert old_direction == "买", "旧口径没复现事故 ⇒ 夹具不敏感"
        assert FRP._classify_dir_cell(cell) == "不买"


# --------------------------------------------------------------------------- ② 排序
class TestTightestRealisticOrdering:
    """``lookahead.rank_flip_robustness``：现实档余量表**None 排最后**。

    🔴 夹具：4 只 ETF、**同一个主题**（⇒ 4 对同主题配对）：

      * ``515450`` **没有 W 记录** ⇒ 该档上界不可得 ⇒ 它参与的配对
        ``margin_realistic is None``（**不可得 ≠ 0**）；
      * ``515020`` 的现实档余量最大（唯一最大值）⇒ 排序后**必须**排第一；
      * ``512800`` 的**极端档**余量被刻意做成**恰好 0.0**（真值，不是不可得）
        ⇒ 旧口径的 ``or -1e9`` 真值兜底会把它踢到最后（口径反了）。
    """

    #: 现实档上界（``delta_e_small_weight``）—— 512800 取 0 ⇒ 阈值只剩对手那一半
    _REAL_BOUNDS = {"512800": 0.0, "512820": 0.01, "515020": 0.02}

    @staticmethod
    def _pool() -> pd.DataFrame:
        return pd.DataFrame([
            {"code": "512800", "name": "银行ETF华宝", "theme_id": "银行",
             "alpha_score": 0.40, "final_score": 0.40, "selected": True},
            {"code": "512820", "name": "银行ETF", "theme_id": "银行",
             "alpha_score": 0.28, "final_score": 0.39, "selected": True},
            {"code": "515020", "name": "红利低波", "theme_id": "银行",
             "alpha_score": 0.20, "final_score": 0.50, "selected": False},
            # 515450 **无 W 记录** ⇒ margin_* = None（该档上界不可得）
            {"code": "515450", "name": "红利低波50ETF", "theme_id": "银行",
             "alpha_score": 0.10, "final_score": 0.30, "selected": False},
        ])

    @classmethod
    def _bounds(cls) -> pd.DataFrame:
        """构造上界表：让 (512800, 515020) 的**极端档余量恰好 = 0.0**（真值，不是不可得）。"""
        pool = cls._pool()
        sigma = float(pd.to_numeric(pool["alpha_score"]).std(ddof=0))
        w_alpha = 0.5
        gap = 0.50 - 0.40  # final_gap(512800 → 515020) = +0.10
        rows = []
        for code, real_bound, extreme_bound in (
            # 515020 的极端档上界取「让阈值恰好等于分差」的值 ⇒ 512800 那一对余量 = 0.0
            ("512800", cls._REAL_BOUNDS["512800"], 0.0),
            ("512820", cls._REAL_BOUNDS["512820"], 0.30),
            ("515020", cls._REAL_BOUNDS["515020"], gap * sigma / w_alpha),
        ):
            rows.append({
                "code": code,
                "delta_e_small_weight": real_bound,
                "delta_e_extreme": extreme_bound,
            })
        return pd.DataFrame(rows)

    @classmethod
    def _flip(cls) -> dict:
        return LA.rank_flip_robustness(
            cls._pool(), cls._bounds(), alpha_weight=0.5, momentum_weight=0.5,
        )

    def test_none_rows_are_sorted_to_the_end(self) -> None:
        """② 排序后 ``None`` **全部在末尾**，且第一行是余量最大的那一行。"""
        flip = self._flip()
        table = flip["same_theme_tightest_realistic"]
        gaps = flip["same_theme_gaps"]
        margins = [row.get("margin_realistic") for row in table]
        assert margins, "现实档余量表不许为空"
        # None 不许出现在真值前面
        seen_none = False
        for margin in margins:
            if margin is None:
                seen_none = True
            else:
                assert not seen_none, f"None 之后又出现真值 ⇒ 排序错：{margins}"
        # 真值按降序
        values = [float(m) for m in margins if m is not None]
        assert values == sorted(values, reverse=True), f"未按 margin_realistic 降序：{values}"
        # 第一行 = 余量最大的那一行（且与报告里的极值一致 = 闸门 L17 的口径）
        expected = max(
            float(row["margin_realistic"]) for row in gaps
            if row.get("margin_realistic") is not None
        )
        assert margins[0] is not None, f"第一行不许是余量不可得的行：{margins}"
        assert float(margins[0]) == pytest.approx(expected, abs=1e-9), (
            f"第一行余量 {margins[0]} ≠ 池内最大余量 {expected}"
        )
        assert float(margins[0]) == pytest.approx(
            flip["max_same_theme_margin_realistic"], abs=1e-9
        ), (
            f"第一行余量 {margins[0]} ≠ max_same_theme_margin_realistic "
            f"{flip['max_same_theme_margin_realistic']}"
        )
        assert (table[0]["selected"], table[0]["challenger"]) == ("512800", "515020")

    def test_none_rows_are_not_dropped(self) -> None:
        """② ``None`` 的行**不许被悄悄丢掉**（只是排到末尾）。"""
        flip = self._flip()
        table = flip["same_theme_tightest_realistic"]
        n_none_gaps = sum(1 for row in flip["same_theme_gaps"]
                           if row.get("margin_realistic") is None)
        n_none_table = sum(1 for row in table if row.get("margin_realistic") is None)
        assert n_none_gaps == 2, f"夹具没造出 2 对余量不可得的配对：{flip['same_theme_gaps']}"
        assert n_none_table == n_none_gaps, (
            f"余量不可得的行被丢了：表里 {n_none_table} 行，池里 {n_none_gaps} 行"
        )

    def test_extreme_table_keeps_zero_margin_as_a_real_value(self) -> None:
        """② 极端档：余量**恰好 0.0** 是真值（不许被真值兜底踢到最后）。"""
        flip = self._flip()
        table = flip["same_theme_tightest_extreme"]
        values = [float(row["margin_extreme"]) for row in table
                  if row.get("margin_extreme") is not None]
        assert values == sorted(values, reverse=True), f"未按 margin_extreme 降序：{values}"
        assert values[0] == pytest.approx(0.0, abs=1e-9), (
            f"夹具没造出余量恰好 0.0 的配对 ⇒ 夹具不敏感：{values}"
        )

    def test_old_sort_key_would_put_none_first(self) -> None:
        """② 变异反证：旧排序键（None → ``(0, 0.0)``）把不可得的行排到**最前** ⇒ 本测试有牙。"""
        gaps = self._flip()["same_theme_gaps"]
        old_sorted = sorted(
            gaps,
            key=lambda r: (0, 0.0) if r.get("margin_realistic") is None
            else (1, -float(r["margin_realistic"])),
        )
        assert old_sorted[0].get("margin_realistic") is None, "旧口径没复现事故 ⇒ 夹具不敏感"
        # 新口径：第一行是余量最大的那一行
        new_sorted = sorted(
            gaps,
            key=lambda r: (1, 0.0) if r.get("margin_realistic") is None
            else (0, -float(r["margin_realistic"])),
        )
        assert new_sorted[0].get("margin_realistic") is not None
        assert (new_sorted[0]["selected"], new_sorted[0]["challenger"]) == ("512800", "515020")


# --------------------------------------------------------------------------- ③ 披露措辞
class TestWSnapshotGapWording:
    """``analyze._w_snapshot_gap_phrase``：gap 的**符号**决定措辞。

    🔴 本轮事故（服务器实跑 20261110_120420）：``gap_days = -5``（快照日 2026-11-04
       **早于**信号日 2026-11-09）⇒ 旧措辞无条件拼成「晚于信号日共 **-5 天**」
       ⇒ 与事实**正好相反**（W 快照已滞后，**不构成前视**）⇒ 交付件自相矛盾。
    """

    @staticmethod
    def _w(gap: int) -> dict:
        return {"snapshot_date": "2026-11-04", "asof": "2026-11-09", "gap_days": gap}

    def test_positive_gap_says_later(self) -> None:
        """③ gap>0 ⇒ 「晚于信号日共 N 天」。"""
        phrase = AN._w_snapshot_gap_phrase(self._w(3))
        assert "晚于信号日" in phrase and "共 **3 天**" in phrase, phrase
        assert "早于" not in phrase, phrase

    def test_zero_gap_says_equal(self) -> None:
        """③ gap==0 ⇒ 「等于信号日」。"""
        phrase = AN._w_snapshot_gap_phrase(self._w(0))
        assert "等于信号日" in phrase, phrase
        assert "晚于" not in phrase and "早于" not in phrase, phrase

    def test_negative_gap_says_earlier_and_not_lookahead(self) -> None:
        """③ gap<0 ⇒ 「早于信号日共 N 天（快照已滞后，不构成前视）」。"""
        phrase = AN._w_snapshot_gap_phrase(self._w(-5))
        assert "早于信号日" in phrase, phrase
        assert "共 **5 天**" in phrase, f"天数必须取绝对值：{phrase}"
        assert "-5" not in phrase, f"不许出现负天数：{phrase}"
        assert "不构成前视" in phrase, phrase
        assert "晚于" not in phrase, phrase

    def test_clause_marker_follows_sign(self) -> None:
        """③ 风险披露句：快照日**早于**信号日 ⇒ 不许再挂 🔴（不是前视）。"""
        la = {"w_snapshot": self._w(-5)}
        clause = AN._w_lookahead_clause(la)
        assert "⚪ 快照日" in clause, clause
        assert "🔴 快照日" not in clause, clause
        assert "早于信号日" in clause and "不构成前视" in clause, clause
