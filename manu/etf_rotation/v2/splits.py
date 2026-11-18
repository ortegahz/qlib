"""🔴 V2 时间切分 + **embargo 硬隔离**（单一事实源）。

红线（用户规格 + 主机续跑指令，两处一致）：
  * 训练名义段 ``2016-01-01 ~ 2021-12-31``，**末段按 embargo 裁剪**；
  * 验证段 ``2022-01-01 ~ 2023-12-31``（**起点不裁**）；
  * 测试段 ``2024-01-01 ~ 2026-11-03``（**起点不动**，单盲只跑一次）；
  * 训练末样本与验证集开头之间留 **≥ 20 个交易日 embargo 隔离带**；
  * `fit_end_time` **必须**同步收窄到裁剪后的训练末日（不许仍写 2021-12-31）。

为什么隔离带取 25 而不是 20（诚实披露，不藏）：
  V2 标签退出窗口是 **t+15 ~ t+25**（末端 = t+25）⇒ 只裁 20 个交易日时，
  最后一个训练样本（≈2021-12-02）的标签窗口会伸到 **2022-01-10**，
  与验证集**价格窗口**重叠 6 个交易日 ⇒ **结构性泄露**。
  ⇒ `effective_embargo = max(EMBARGO_DAYS, LABEL_HORIZON_DAYS) = 25`；
  25 ≥ 20 ⇒ 仍满足「训练末样本 ≤ 2021-12-31 前第 20 个交易日」这条上界断言。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import config_v2 as C


# ----------------------------------------------------------------- 交易日历
@lru_cache(maxsize=8)
def trading_calendar(start: str = "", end: str = "") -> tuple[_dt.date, ...]:
    """交易日历（来自 qlib ``cn_data/calendars/day.txt``，**只读**、不联网）。

    ⚠️ 日历文件的末日 = 数据集实际覆盖末日（当前 2026-11-16）；
       标签窗口超出日历末端的样本会被 `DropnaLabel` 丢掉（标准行为，不特殊处理）。
    """
    path = Path(C.CALENDAR_FILE)
    if not path.is_file():
        raise FileNotFoundError(f"交易日历不存在：{path}")
    out: list[_dt.date] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            day = _dt.date.fromisoformat(line)
            if start and day < _dt.date.fromisoformat(start):
                continue
            if end and day > _dt.date.fromisoformat(end):
                continue
            out.append(day)
    return tuple(out)


def _d(text: str) -> _dt.date:
    """把 ISO 字符串转成 ``date``（口径统一，避免到处散落解析）。"""
    return _dt.date.fromisoformat(text)


def trading_days_before(day: _dt.date, n: int) -> _dt.date:
    """`day` 之前第 `n` 个**交易日**（不含 `day` 本身）。

    例：`trading_days_before(2021-12-31, 1)` = 2021-12-30；`..., 20` = 2021-12-02。
    """
    if n <= 0:
        raise ValueError(f"n 必须 ≥ 1（n={n}）")
    cal = trading_calendar()
    prior = [d for d in cal if d < day]
    if len(prior) < n:
        raise ValueError(f"日历里 {day} 之前不足 {n} 个交易日")
    return prior[-n]


def label_window_end(sample_day: _dt.date, horizon: int | None = None) -> _dt.date:
    """样本日 `sample_day` 的**标签窗口结束日**（= t+horizon 的那个交易日）。

    `horizon` 默认取 `config_v2.LABEL_HORIZON_DAYS`（= 25）。
    """
    h = int(horizon if horizon is not None else C.LABEL_HORIZON_DAYS)
    cal = trading_calendar()
    idx = None
    for i, d in enumerate(cal):
        if d == sample_day:
            idx = i
            break
    if idx is None:
        raise ValueError(f"{sample_day} 不在交易日历里")
    end_idx = idx + h
    if end_idx >= len(cal):
        # 🔴 标签窗口超出日历末端 ⇒ 该样本**没有标签**（会被 DropnaLabel 丢掉）
        return cal[-1]
    return cal[end_idx]


def effective_embargo_days() -> int:
    """实际 embargo（交易日）= max(规格下限, 标签窗口末端)。

    🔴 规格下限 = `config_v2.EMBARGO_DAYS`（20）；
       标签窗口末端 = `config_v2.LABEL_HORIZON_DAYS`（25）⇒ 实际 25。
    """
    return max(int(C.EMBARGO_DAYS), int(C.LABEL_HORIZON_DAYS))


# ----------------------------------------------------------------- 切分规格
@dataclass(frozen=True)
class SplitSpec:
    """V2 的切分规格（**训练末样本日已被 embargo 裁剪**）。"""

    start_time: str
    train_start: str
    train_end_raw: str          # 名义训练末日（2021-12-31）
    train_end: str              # **裁剪后**的训练末日（embargo 之后）
    valid_start: str
    valid_end: str
    test_start: str
    test_end: str
    fit_start: str
    fit_end: str                # = train_end（不许仍写 2021-12-31）
    embargo_days: int

    def as_dict(self) -> dict:
        """把切分规格转成**可 JSON 序列化**的字典（写进 manifest / 报告）。"""
        return {
            "start_time": self.start_time,
            "train_start": self.train_start,
            "train_end_raw": self.train_end_raw,
            "train_end": self.train_end,
            "valid_start": self.valid_start,
            "valid_end": self.valid_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "fit_start": self.fit_start,
            "fit_end": self.fit_end,
            "embargo_days": self.embargo_days,
        }


def build_split(embargo_days: int | None = None) -> SplitSpec:
    """按真实交易日历构造切分规格（embargo 裁剪训练末段）。

    `embargo_days=None` ⇒ 用 `effective_embargo_days()`（25，见模块说明）。
    显式传 20 ⇒ 复现主机规格的**字面**口径（此时标签窗口会跨界，
    `assert_no_label_overlap` 会**如实报红**，不掩盖）。
    """
    emb = effective_embargo_days() if embargo_days is None else int(embargo_days)
    train_end_raw = _d(C.TRAIN[1])
    train_end = trading_days_before(train_end_raw, emb)
    return SplitSpec(
        start_time=C.START_TIME,
        train_start=C.TRAIN[0],
        train_end_raw=train_end_raw.isoformat(),
        train_end=train_end.isoformat(),
        valid_start=C.VALID[0],
        valid_end=C.VALID[1],
        test_start=C.TEST[0],
        test_end=C.TEST[1],
        fit_start=C.FIT_START,
        fit_end=train_end.isoformat(),
        embargo_days=emb,
    )


def assert_no_label_overlap(spec: SplitSpec) -> None:
    """🔴 硬断言：训练末样本的**标签窗口结束日**必须 **< 验证集首样本日**。

    这条断言是 embargo 的**目的**（防止 20 日前瞻标签跨段泄露）；
    去掉 embargo ⇒ 训练末样本回到 2021-12-31 ⇒ 标签窗口伸进验证段 ⇒ **必须报红**。
    """
    last_train = _d(spec.train_end)
    val_start = _d(spec.valid_start)
    lbl_end = label_window_end(last_train)
    if lbl_end >= val_start:
        raise AssertionError(
            f"🔴 embargo 失效：训练末样本 {last_train} 的标签窗口结束日 {lbl_end} "
            f"**不早于**验证集首样本日 {val_start} ⇒ 标签跨界泄露"
            f"（embargo={spec.embargo_days} 交易日）"
        )
    if last_train > trading_days_before(_d(spec.train_end_raw), int(C.EMBARGO_DAYS)):
        raise AssertionError(
            f"🔴 embargo 不足：训练末样本 {last_train} 晚于"
            f"「{spec.train_end_raw} 前第 {C.EMBARGO_DAYS} 个交易日」"
        )


def assert_split_discipline(spec: SplitSpec) -> None:
    """全套切分红线断言（顺序性 + embargo + fit 窗落在训练段内）。"""
    assert _d(spec.train_start) < _d(spec.train_end) <= _d(spec.train_end_raw), "训练段顺序异常"
    assert _d(spec.train_end) < _d(spec.valid_start), "训练段与验证段必须不相交"
    assert _d(spec.valid_start) <= _d(spec.valid_end) < _d(spec.test_start), "验证/测试段顺序异常"
    assert _d(spec.fit_start) >= _d(spec.train_start), "fit_start 必须落在训练段内"
    assert _d(spec.fit_end) <= _d(spec.train_end), "fit_end 必须落在（裁剪后的）训练段内"
    assert _d(spec.fit_end) == _d(spec.train_end), "fit_end 必须与裁剪后的训练末日一致"
    assert_no_label_overlap(spec)


def split() -> SplitSpec:
    """默认切分（embargo = 25）+ 全套断言。"""
    spec = build_split()
    assert_split_discipline(spec)
    return spec


def embargo_summary() -> dict:
    """给报告用的口径摘要（**两个数都报**，不许只报好看的）。"""
    spec = split()
    return {
        "embargo_days_spec_min": int(C.EMBARGO_DAYS),
        "embargo_days_effective": spec.embargo_days,
        "label_horizon_days": int(C.LABEL_HORIZON_DAYS),
        "label_exit_window": list(C.LABEL_EXIT_WINDOW),
        "train_end_raw": spec.train_end_raw,
        "train_start": spec.train_start,
        "train_end": spec.train_end,
        # 🔴 2026-11-18 夜班补：这两个字段**原来根本没暴露** ⇒ 成绩单把缺失值印成了 Python 的
        #    `None`（违反本层「缺失写『—』」铁律）。这里补上**已有**的 spec 值（纯披露，不改计算）。
        "fit_start": spec.fit_start,
        "fit_end": spec.fit_end,
        "last_train_label_window_end": label_window_end(_d(spec.train_end)).isoformat(),
        "valid_start": spec.valid_start,
        "no_label_overlap": True,
        "note": (
            "规格下限 20 个交易日；因 V2 标签退出窗口末端为 t+25，"
            "为满足「训练末样本标签窗口结束日 < 验证集首样本日期」的硬断言，"
            "实际隔离带取 25 个交易日（≈2021-11-26）。"
        ),
    }


# ----------------------------------------------------------------- 5 折滚动前推
@dataclass(frozen=True)
class FoldSpec:
    """滚动前推（expanding window）的一折切分规格。

    🔴 与 :class:`SplitSpec` 的字段**同名同义** ⇒ ``train_s.py`` 可直接复用
       （``train_start`` / ``train_end`` / ``fit_start`` / ``fit_end`` /
       ``test_start`` / ``test_end``）；区别只在：
         * ``fold_id``   —— 折号（1~5）；
         * ``valid_*`` / ``test_*`` 都指向**该折的验证年**（= holdout）；
         * ``train_end`` = 「标签窗口结束日 < 验证段首日」的**最后一个**交易日
           （由 :func:`build_fold_spec` 从验证段起点**倒推** embargo）。
    """

    fold_id: int
    start_time: str
    train_start: str
    train_end_raw: str          # 名义训练末日（该折训练段日历年末）
    train_end: str              # **embargo 裁剪后**的训练末日
    valid_start: str
    valid_end: str
    test_start: str             # = valid_start（该折 holdout 就是验证年）
    test_end: str               # = valid_end
    fit_start: str
    fit_end: str                # = train_end
    embargo_days: int

    def as_dict(self) -> dict:
        """转成可 JSON 序列化的字典（写进 manifest / 报告）。"""
        return {
            "fold_id": self.fold_id,
            "start_time": self.start_time,
            "train_start": self.train_start,
            "train_end_raw": self.train_end_raw,
            "train_end": self.train_end,
            "valid_start": self.valid_start,
            "valid_end": self.valid_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "fit_start": self.fit_start,
            "fit_end": self.fit_end,
            "embargo_days": self.embargo_days,
        }


def build_fold_spec(fold_id: int, embargo_days: int | None = None) -> FoldSpec:
    """构造第 ``fold_id`` 折的切分规格（**expanding window**）。

    🔴 **embargo 从验证段起点倒推**（不是从名义训练末日往前数）：
       「训练末样本的标签窗口结束日（t+25）必须 < 验证段首日」⇒
       ``train_end = 验证段首日之前第 (embargo+1) 个交易日``。
       （若沿用「名义末日往前数 25 天」，折 1 会算到 2018-11-27 而验证段从
       2019-01-01 开始 —— 中间只有 ~23 个交易日 ⇒ 标签窗口仍会跨界 ⇒ 断言报红。）

    参数
    ----
    fold_id : 1~5（见 :data:`config_v2.WALK_FORWARD_FOLDS`）。
    embargo_days : None ⇒ 用 :func:`effective_embargo_days()`（25）。
    """
    table = {int(f[0]): f for f in C.WALK_FORWARD_FOLDS}
    fid = int(fold_id)
    if fid not in table:
        raise ValueError(
            f"🔴 折号非法：{fold_id}（合法 = {[int(f[0]) for f in C.WALK_FORWARD_FOLDS]}）"
        )
    _, train_end_raw, valid_start, valid_end = table[fid]
    emb = effective_embargo_days() if embargo_days is None else int(embargo_days)
    train_end = trading_days_before(_d(valid_start), emb + 1)
    return FoldSpec(
        fold_id=fid,
        start_time=C.START_TIME,
        train_start=C.WALK_FORWARD_TRAIN_START,
        train_end_raw=train_end_raw,
        train_end=train_end.isoformat(),
        valid_start=valid_start,
        valid_end=valid_end,
        test_start=valid_start,
        test_end=valid_end,
        fit_start=C.WALK_FORWARD_TRAIN_START,
        fit_end=train_end.isoformat(),
        embargo_days=emb,
    )


def assert_fold_discipline(spec: FoldSpec) -> None:
    """🔴 折级切分红线断言（顺序性 + embargo + **绝不触碰测试段**）。"""
    if _d(spec.train_start) >= _d(spec.train_end):
        raise AssertionError(f"🔴 折 {spec.fold_id}：训练段顺序异常")
    if _d(spec.train_end) > _d(spec.train_end_raw):
        raise AssertionError(
            f"🔴 折 {spec.fold_id}：训练末日 {spec.train_end} 晚于名义末日 {spec.train_end_raw}"
        )
    if _d(spec.train_end) >= _d(spec.valid_start):
        raise AssertionError(f"🔴 折 {spec.fold_id}：训练段与验证段相交")
    if _d(spec.valid_start) > _d(spec.valid_end):
        raise AssertionError(f"🔴 折 {spec.fold_id}：验证段倒挂")
    # 🔴🔴 **绝不触碰测试段**（单盲已消耗，禁止回头选参）
    if _d(spec.valid_end) >= _d(C.TEST[0]):
        raise AssertionError(
            f"🔴 折 {spec.fold_id}：验证段末日 {spec.valid_end} 触碰测试段"
            f"（{C.TEST[0]} 起）⇒ 滚动前推**只许**在 2019~2023 做"
        )
    if _d(spec.fit_start) < _d(spec.train_start):
        raise AssertionError(f"🔴 折 {spec.fold_id}：fit_start 落在训练段之外")
    if _d(spec.fit_end) != _d(spec.train_end):
        raise AssertionError(f"🔴 折 {spec.fold_id}：fit_end 必须等于裁剪后的训练末日")
    # ---- 标签窗口不跨界 ----
    lbl_end = label_window_end(_d(spec.train_end))
    if lbl_end >= _d(spec.valid_start):
        raise AssertionError(
            f"🔴 折 {spec.fold_id} embargo 失效：训练末样本 {spec.train_end} 的标签窗口"
            f"结束日 {lbl_end} **不早于**验证段首日 {spec.valid_start} ⇒ 标签跨界泄露"
        )
    # ---- embargo 上界：训练末样本必须 ≤ 名义末日之前第 EMBARGO_DAYS 个交易日 ----
    upper = trading_days_before(_d(spec.train_end_raw), int(C.EMBARGO_DAYS))
    if _d(spec.train_end) > upper:
        raise AssertionError(
            f"🔴 折 {spec.fold_id} embargo 不足：训练末样本 {spec.train_end} 晚于"
            f"「{spec.train_end_raw} 前第 {C.EMBARGO_DAYS} 个交易日」（{upper}）"
        )


def assert_fold_only(fold_id: int) -> FoldSpec:
    """构造 + 断言第 ``fold_id`` 折（**唯一入口** ⇒ 漏断言会直接报错）。"""
    spec = build_fold_spec(fold_id)
    assert_fold_discipline(spec)
    return spec


def walk_forward_summary() -> dict:
    """5 折滚动前推的口径摘要（写进报告；**两个偏差都披露**）。"""
    folds = []
    for fid, train_end_raw, valid_start, valid_end in C.WALK_FORWARD_FOLDS:
        spec = build_fold_spec(fid)
        folds.append({
            **spec.as_dict(),
            "last_train_label_window_end": label_window_end(_d(spec.train_end)).isoformat(),
            "n_valid_days": len([d for d in trading_calendar() if valid_start <= d.isoformat() <= valid_end]),
        })
    return {
        "mode": "expanding_window_walk_forward",
        "n_folds": len(folds),
        "folds": folds,
        "oos_window": list(C.WALK_FORWARD_OOS_WINDOW),
        "embargo_days_spec_min": int(C.EMBARGO_DAYS),
        "embargo_days_effective": effective_embargo_days(),
        "label_horizon_days": int(C.LABEL_HORIZON_DAYS),
        "train_start": C.WALK_FORWARD_TRAIN_START,
        "deviation_train_start": (
            "🔴 用户规格写「2015 ~ 2018 起训」；本工程训练段起点 = 2016-01-01"
            "（数据本身从 2016 年起）⇒ 折 1 训练段少 1 年"
        ),
        "locked_params": {
            "rebalance_every_n_days": C.WALK_FORWARD_CADENCE,
            "top_k": C.WALK_FORWARD_TOPK_LOCKED,
            "hold_buffer_grid": list(C.WALK_FORWARD_BUFFER_GRID),
        },
        "test_window_sealed": list(C.TEST),
        "note": (
            "5 折 OOS 拼成 2019–2023 的纯样本外验证段；测试段（2024–2026）单盲已消耗，"
            "**任何新候选参数都不得回头用它验收**（需新盲测窗口或用户显式授权）。"
        ),
    }
