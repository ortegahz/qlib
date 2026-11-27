"""🔴 V3 时间切分 + **embargo 硬隔离**（V3 自己的实现，🔴 **不 import V2/splits**）。

**为什么 V3 要自己写一遍**：V3 的隔离契约是「**不 import、不改写** V2 的任何文件」，
所以切分必须自带一份；🔴 但**红线一模一样**（抄的是红线，不是代码）：

  * 训练名义段 ``2016-01-01 ~ 2021-12-31``（5 折时末段逐年推进）；
  * 验证段 ``2022-01-01 ~ 2023-12-31``（**起点不裁**）；
  * 🔴 **单盲封存段** ``2024-01-01 ~ 2026-11-03``（``config_v3.BLIND_SEALED_WINDOW``）
    —— V3 的对照实验**一律不许触碰**（V2 已消耗）；
  * 训练末样本与验证段首日之间留 **≥ 25 个交易日 embargo**：
    标签退出窗口是 ``t+15 ~ t+25``（末端 = t+25）⇒ embargo 必须 ≥ 25
    ⇒ ``effective_embargo = max(EMBARGO_DAYS, LABEL_HORIZON_DAYS) = 25``。

🔴 **embargo 裁剪必须基于真实交易日历**（qlib ``calendars/day.txt``）——
**不许用自然日**（自然日会把 25 个交易日裁成 ~17 个交易日 ⇒ 标签窗口照样跨界泄露）。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

try:  # 扁平导入（v3 目录进 sys.path，与 v2 同口径）
    import config_v3 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v3 as C


#: 交易日历默认路径（**只作参数默认值**；🔴 模块加载时不读盘）
CALENDAR_FILE = C.CALENDAR_FILE


# ----------------------------------------------------------------- 交易日历
@lru_cache(maxsize=8)
def _calendar_index(calendar_file: str) -> pd.DatetimeIndex:
    """交易日历（qlib ``calendars/day.txt``，**只读**、不联网）⇒ 升序 DatetimeIndex。

    🔴 文件不存在 ⇒ raise FileNotFoundError（fail-closed：不许拿自然日凑数）。
    """
    path = Path(calendar_file)
    if not path.is_file():
        raise FileNotFoundError(f"🔴 交易日历不存在：{path}")
    days: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if text:
                days.append(text)
    if not days:
        raise ValueError(f"🔴 交易日历是空文件：{path}")
    return pd.DatetimeIndex(pd.to_datetime(days))


def trading_days_before(date: str, n: int, calendar_file=CALENDAR_FILE) -> str:
    """``date`` 之前第 ``n`` 个**交易日**（不含 ``date`` 本身）⇒ ISO 字符串。

    * 例（真实日历）：``trading_days_before("2021-12-31", 1)`` = ``"2021-12-30"``；
    * 🔴 **不许用自然日代替**（自然日 ≠ 交易日 ⇒ embargo 会缩水）；
    * 🔴 日历里不足 ``n`` 个交易日 ⇒ raise（不许静默截断）。
    """
    if int(n) <= 0:
        raise ValueError(f"🔴 n 必须 ≥ 1（{n}）")
    calendar = _calendar_index(str(calendar_file))
    index = int(calendar.searchsorted(pd.Timestamp(date), side="left"))
    position = index - int(n)
    if position < 0:
        raise ValueError(f"🔴 交易日历里 {date} 之前不足 {n} 个交易日")
    return calendar[position].date().isoformat()


def _trading_days_after(date: str, n: int, calendar_file=CALENDAR_FILE) -> str | None:
    """``date`` 之后第 ``n`` 个**交易日**（不含 ``date`` 本身）；超出日历末端 ⇒ ``None``。

    🔴 用来算「训练末样本的**标签窗口结束日**」（= t + ``LABEL_HORIZON_DAYS``）。
    """
    if int(n) <= 0:
        raise ValueError(f"🔴 n 必须 ≥ 1（{n}）")
    calendar = _calendar_index(str(calendar_file))
    index = int(calendar.searchsorted(pd.Timestamp(date), side="right"))
    position = index + int(n) - 1
    if position >= len(calendar):
        return None
    return calendar[position].date().isoformat()


# ----------------------------------------------------------------- embargo
def effective_embargo_days() -> int:
    """实际 embargo（交易日）= max(``EMBARGO_DAYS``, ``LABEL_HORIZON_DAYS``) = **25**。

    🔴 规格下限 = 20；标签窗口末端 = 25（``t+15 ~ t+25``）⇒ 只裁 20 个交易日时，
    最后一个训练样本的标签窗口会伸进验证段 ⇒ **结构性泄露** ⇒ 取 25。
    """
    return max(int(C.EMBARGO_DAYS), int(C.LABEL_HORIZON_DAYS))


# ----------------------------------------------------------------- 切分规格
@dataclass(frozen=True)
class FoldSpec:
    """一折切分规格（**训练末样本日已被 embargo 裁剪**）。"""

    fold_id: int | None
    train_start: str
    train_end: str              # 🔴 **embargo 裁剪后**的训练末日
    train_end_raw: str          # 名义训练末日（未裁剪）
    valid_start: str
    valid_end: str
    fit_start: str              # fit 段必须**落在训练段内**
    fit_end: str                # = train_end（不许仍写名义末日）
    embargo_days: int

    def as_dict(self) -> dict:
        """转成**可 JSON 序列化**的字典（写进 manifest / 报告）。"""
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "train_end_raw": self.train_end_raw,
            "valid_start": self.valid_start,
            "valid_end": self.valid_end,
            "fit_start": self.fit_start,
            "fit_end": self.fit_end,
            "embargo_days": self.embargo_days,
        }


def build_fold_spec(fold_id: int) -> FoldSpec:
    """按 ``config_v3.WALK_FORWARD_FOLDS`` 构造第 ``fold_id`` 折。

    * 训练段 = ``[2016-01-01, 名义末日]``（**expanding window**）；
    * 🔴 实际训练末日 = **名义末日往前 embargo 个交易日**（基于**真实交易日历**，
      不许用自然日）⇒ 等价于「验证段首日往前 ``embargo + 1`` 个交易日」；
    * ``fit_end`` = 裁剪后的训练末日（不许仍写名义末日）。
    """
    table = {int(entry[0]): entry for entry in C.WALK_FORWARD_FOLDS}
    fid = int(fold_id)
    if fid not in table:
        raise ValueError(
            f"🔴 折号非法：{fold_id}（合法 = {[int(e[0]) for e in C.WALK_FORWARD_FOLDS]}）"
        )
    _, train_end_raw, valid_start, valid_end = table[fid]
    embargo = effective_embargo_days()
    train_end = trading_days_before(valid_start, embargo + 1, calendar_file=CALENDAR_FILE)
    return FoldSpec(
        fold_id=fid,
        train_start=str(C.TRAIN[0]),
        train_end=train_end,
        train_end_raw=str(train_end_raw),
        valid_start=str(valid_start),
        valid_end=str(valid_end),
        fit_start=str(C.FIT_START),
        fit_end=train_end,
        embargo_days=embargo,
    )


def assert_fold_discipline(spec: FoldSpec) -> None:
    """🔴 折级切分红线断言（**4 条**，违反即 raise）。

    ① **折号递增、折间验证年递增**：折号必须在 ``config_v3.WALK_FORWARD_FOLDS`` 里，
       且本折的验证段与配置**逐字一致**；折号越大 ⇒ 验证年必须**越晚**且**不重叠**；
    ② ``train_end < valid_start``（训练段与验证段不许相交）；
    ③ **训练末样本的标签窗口末端 < valid_start**（embargo 真的够 ⇒ 标签不跨界泄露）；
    ④ 🔴 **绝不触碰单盲封存段**（``valid_end`` 与 ``train_end`` 都必须早于
       ``config_v3.BLIND_SEALED_WINDOW[0]``）。
    """
    if spec is None:
        raise AssertionError("🔴 折规格为空")
    train_end = pd.Timestamp(spec.train_end)
    valid_start = pd.Timestamp(spec.valid_start)
    valid_end = pd.Timestamp(spec.valid_end)
    # ---- ① 折号递增、折间验证年递增 ----
    if spec.fold_id is not None:
        table = {int(entry[0]): entry for entry in C.WALK_FORWARD_FOLDS}
        if int(spec.fold_id) not in table:
            raise AssertionError(f"🔴 折号非法：{spec.fold_id}")
        entry = table[int(spec.fold_id)]
        if (spec.valid_start, spec.valid_end) != (str(entry[2]), str(entry[3])):
            raise AssertionError(
                f"🔴 折 {spec.fold_id} 的验证段 {spec.valid_start}~{spec.valid_end} "
                f"与 config_v3.WALK_FORWARD_FOLDS（{entry[2]}~{entry[3]}）不一致"
            )
        earlier = [key for key in table if key < int(spec.fold_id)]
        if earlier:
            previous = table[max(earlier)]
            if valid_start <= pd.Timestamp(previous[2]):
                raise AssertionError(
                    f"🔴 折 {spec.fold_id} 的验证年没有递增"
                    f"（上一折验证段首日 {previous[2]}）"
                )
            if valid_start <= pd.Timestamp(previous[3]):
                raise AssertionError(
                    f"🔴 折 {spec.fold_id} 的验证段与上一折（{previous[2]}~{previous[3]}）重叠"
                )
    # ---- ② 训练段与验证段不相交 ----
    if train_end >= valid_start:
        raise AssertionError(
            f"🔴 折 {spec.fold_id}：训练末日 {spec.train_end} 不早于验证段首日 "
            f"{spec.valid_start} ⇒ 训练段与验证段相交"
        )
    # ---- ③ 训练末样本的标签窗口末端 < 验证段首日 ----
    label_end = _trading_days_after(spec.train_end, int(C.LABEL_HORIZON_DAYS), CALENDAR_FILE)
    if label_end is None:
        raise AssertionError(
            f"🔴 折 {spec.fold_id}：训练末日 {spec.train_end} 的标签窗口末端"
            f"超出交易日历 ⇒ 无法证明 embargo 足够（fail-closed）"
        )
    if pd.Timestamp(label_end) >= valid_start:
        raise AssertionError(
            f"🔴 折 {spec.fold_id} embargo 失效：训练末样本 {spec.train_end} 的标签窗口"
            f"结束日 {label_end} 不早于验证段首日 {spec.valid_start} ⇒ 标签跨界泄露"
        )
    # ---- ④ 绝不触碰单盲封存段 ----
    sealed_start = pd.Timestamp(C.BLIND_SEALED_WINDOW[0])
    if valid_end >= sealed_start:
        raise AssertionError(
            f"🔴 折 {spec.fold_id}：验证段末日 {spec.valid_end} 触碰单盲封存段"
            f"（{C.BLIND_SEALED_WINDOW[0]} 起）⇒ V3 一律不许回头用它验收"
        )
    if train_end >= sealed_start:
        raise AssertionError(
            f"🔴 折 {spec.fold_id}：训练末日 {spec.train_end} 触碰单盲封存段"
            f"（{C.BLIND_SEALED_WINDOW[0]} 起）"
        )


def split() -> FoldSpec:
    """默认切分（train = 2016~2021 / valid = 2022~2023，训练末日已 embargo 裁剪）。

    🔴 返回前**必过** :func:`assert_fold_discipline`（fail-closed：
    切分本身不合规 ⇒ 不许拿到一个「看起来能用」的规格）。
    """
    embargo = effective_embargo_days()
    train_end_raw = str(C.TRAIN[1])
    train_end = trading_days_before(str(C.VALID[0]), embargo + 1, calendar_file=CALENDAR_FILE)
    spec = FoldSpec(
        fold_id=None,
        train_start=str(C.TRAIN[0]),
        train_end=train_end,
        train_end_raw=train_end_raw,
        valid_start=str(C.VALID[0]),
        valid_end=str(C.VALID[1]),
        fit_start=str(C.FIT_START),
        fit_end=train_end,
        embargo_days=embargo,
    )
    assert_fold_discipline(spec)
    return spec
