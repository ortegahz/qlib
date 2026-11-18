#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信号日可行性口径（**纯函数**）—— 「这个信号日，个股层(S)到底打不打得出来」。

【为什么要有这个模块（2026-11-05 06:0x 实测）】
ETF 行情与个股行情是**两套独立数据源**，刷新节奏不一样：

* ETF 行情 `etf_prices.csv` 用 akshare 能随时往前补（2026-11-05 时已到 2026-11-04，
  下一个调仓节点 **2026-12-01** 当天就能补到）；
* 个股行情在 **qlib 数据集**里，`calendars/day.txt` 只到**最后一个有行情的交易日**
  （当前 **2026-11-03**）⇒ 要往前推必须**重建 qlib 数据集**（**重活，需用户当轮授权**）。

⇒ 于是产生一个**致命错配**：12-01 那天 ETF 行情刷好了、`analyze.py --asof 2026-12-01`
也传得进去，但 **S 层压根没有 12-01 的截面** ⇒ E = W × S 的 **S 不可跳过**（铁律 §十四.2）
⇒ 报告出不来。更糟的是 `qlib_scores.py --end 2026-12-01` 会被**静默截断**到
`config.SCORE_END_TIME=2026-11-03`（日历查询窗写死）⇒ 用户以为「已经重新打过分了」，
其实**还是 11-03 那份分数** ⇒ 一小时算力白花，且**全程没有任何提示**。

🔴 **第二重错配（更要命）：打分日是「相位栅格」，不是任意交易日。**
`qlib_scores.rebalance_dates` 的口径是「从打分窗起点算，每 N 个交易日取一个，
且**永远包含最后一个交易日**」（相位 = `(i % N) == N-1`，i 是交易日历下标）。
以当前日历（起点 2023-06-01，N=10）实测，相位日是
**2026-11-04 / 2026-11-18 / 2026-12-02 / 2026-12-16 / 2026-12-30**。
⇒ **2026-12-01 不在相位上** ⇒ 即使重建了 qlib 数据集，`--asof 2026-12-01` **仍然出不了报告**
（除非数据集恰好停在 12-01，触发「末日例外」）。
⇒ 所以「12-01 那天跑一次 `--asof 2026-12-01`」这个操作**在数据层面就不成立**，
必须由机器判定并给出**可行的信号日**，而不是让人踩坑。

【本模块提供什么】
* `read_calendar_dates` / `calendar_max_date`：读交易日历（`day.txt`，一行一个 ``YYYY-MM-DD``）；
* `score_grid_dates`：**唯一事实源**的打分日栅格口径（`qlib_scores.rebalance_dates` 改为调用它）；
* `check_signal_feasibility`：信号日可行性判定（打分期 / 出报告期共用）。

【纪律】
* **纯计算**：不联网、不写文件、不依赖 qlib 运行时与 pandas（日期一律由调用方传入）；
* 每个函数**中文 docstring**；4 空格；snake_case；
* 读不到必需输入 ⇒ 返回 ``None`` / 抛中文异常，**不许编数字**（铁律 §十四.5）。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

#: 判定级别：**block** = 硬拦（拒绝继续）；**disclose** = 只披露（不拦，但要写进产物）
LEVEL_BLOCK = "block"
LEVEL_DISCLOSE = "disclose"


# ---------------------------------------------------------------- 日期工具
def parse_date(value: object) -> date | None:
    """把各种日期表示统一成 ``datetime.date``；解析不出来返回 ``None``（**不编日期**）。

    支持 ``date`` / ``datetime`` / ``"YYYY-MM-DD"`` / ``"YYYYMMDD"`` / ``"YYYY/MM/DD"``。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    text = text[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def fmt_date(value: object) -> str | None:
    """把日期表示统一成 ``"YYYY-MM-DD"``；解析不出来返回 ``None``。"""
    d = parse_date(value)
    return d.isoformat() if d is not None else None


def _days_between(earlier: object, later: object) -> int | None:
    """两个日期相差的自然日数（later - earlier）；任一解析不出来 ⇒ ``None``。"""
    a, b = parse_date(earlier), parse_date(later)
    if a is None or b is None:
        return None
    return (b - a).days


# ---------------------------------------------------------------- 日历读取
def read_calendar_dates(path: Path | str | None) -> list[date]:
    """读交易日历文件（一行一个 ``YYYY-MM-DD``）⇒ 升序去重的日期列表。

    文件缺失 / 读失败 ⇒ 抛中文异常（**不静默跳过**：日历是推导调仓节点的必需品）。
    行内容不是合法日期 ⇒ 跳过并计数（由调用方决定是否报错）。
    """
    if path is None:
        raise FileNotFoundError("交易日历路径为空 ⇒ 无法判定信号日可达性（不静默跳过）")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"交易日历缺失：`{p}` 不存在 ⇒ 无法判定信号日可达性"
            f"（服务器路径通常是 $ETF_ROTATION_OUTPUT_ROOT/datasets/qlib/cn_data/calendars/day.txt）"
        )
    out: set[date] = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        d = parse_date(line)
        if d is not None:
            out.add(d)
    if not out:
        raise ValueError(f"交易日历 `{p}` 里没有解析出任何合法日期 ⇒ 无法判定信号日可达性")
    return sorted(out)


def calendar_max_date(path: Path | str | None) -> str | None:
    """交易日历的**最大日期**（``"YYYY-MM-DD"``）；文件缺失 / 空 ⇒ ``None``。

    对 qlib 的 ``calendars/day.txt`` 而言，它就是**数据集覆盖末日**：
    个股行情只到这一天 ⇒ **再往后必须重建数据集**（重活，需用户当轮授权）。
    """
    try:
        dates = read_calendar_dates(path)
    except (FileNotFoundError, ValueError):
        return None
    return dates[-1].isoformat() if dates else None


# ---------------------------------------------------------------- 打分日栅格
def score_grid_dates(
    cal_dates: list[date] | list[str],
    every_n: int,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    """算**打分日栅格**（唯一事实源；``qlib_scores.rebalance_dates`` 改为调用本函数）。

    口径（与历史实现逐位一致，**不许**在这里改语义）：
    * 相位：交易日历下标 ``i`` 满足 ``i % every_n == every_n - 1``；
    * 再按 ``start <= d <= end`` 截断；
    * **末日例外**：若最后一个交易日不在结果里，**追加**它
      （否则「当期建议」拿不到分数 —— 这是历史行为，保留）。

    🔴 **M-S4 审计订正（2026-11-05 08:0x）**：历史实现的「末日例外」是
       ``sel.append(cal[-1])`` ⇒ 它**既不受 ``end`` 约束、也不受 ``start`` 约束**
       ⇒ 可能吐出一个**早于 ``start``** 的日期（实测：``start=2024-02-01``、末日
       ``2024-01-03`` ⇒ 返回 ``['2024-01-03']``）。这是**基线原样保留**的历史语义
       （不是本次改动引入的回归）⇒ 这里**逐位保留**、不擅自收紧，
       但调用方必须知道：**栅格可能比 ``[start, end]`` 更宽**。
    🔴 **空日历**：历史实现在空日历上抛裸 ``IndexError``（``parsed[-1]``）
       ⇒ M-S4 改为**返回空列表**（调用方据此判「无打分日」⇒ 硬拦），
       避免用裸异常掩盖真实原因。

    参数
    ----
    cal_dates : 交易日历（升序）
    every_n   : 相位步长（打分间隔，交易日）
    start/end : 截断边界（``None`` 表示不截断）

    返回
    ----
    升序去重的 ``"YYYY-MM-DD"`` 列表。
    """
    if every_n < 1:
        raise ValueError(f"打分步长 every_n 必须 ≥ 1，实际 {every_n}")
    parsed = [d for d in (parse_date(x) for x in cal_dates) if d is not None]
    parsed.sort()
    if not parsed:
        # 🔴 M-S4：空日历 ⇒ 返回空列表（不是裸 IndexError）
        #    （调用方 `check_signal_feasibility` 会据此报 S1 硬拦 ⇒ fail-closed）
        return []
    sel = [d for i, d in enumerate(parsed) if i % every_n == (every_n - 1) % every_n]
    lo = parse_date(start)
    hi = parse_date(end)
    if lo is not None:
        sel = [d for d in sel if d >= lo]
    if hi is not None:
        sel = [d for d in sel if d <= hi]
    if not sel or sel[-1] != parsed[-1]:
        # 🔴 历史口径：末日例外**不受 start / end 约束**（`qlib_scores.rebalance_dates` 原样）。
        #    这里保持一致（单一事实源），但调用方必须知道：
        #    **栅格可能包含早于 start、晚于 end 的末日**（见上方 docstring）。
        sel.append(parsed[-1])
    return sorted({d.isoformat() for d in sel})


# ---------------------------------------------------------------- 可行性判定
def check_signal_feasibility(
    *,
    asof: str,
    score_grid: list[str],
    qlib_calendar_max_date: str | None,
    every_n: int = 10,
    scores_max_date: str | None = None,
    prices_max_date: str | None = None,
    deployed_grid: list[str] | None = None,
) -> list[dict]:
    """信号日可行性判定 ⇒ 返回 findings 列表。

    参数
    ----
    asof                    : 目标信号日（``"YYYY-MM-DD"``）
    score_grid              : 本轮**将会**被打分的日期集合（``score_grid_dates`` 的输出）
    qlib_calendar_max_date  : qlib 数据集覆盖末日（``day.txt`` 的最大日期）
    every_n                 : 打分相位步长（交易日；只用于文案）
    scores_max_date         : 现有个股分数文件的末日（可为 ``None`` = 没有分数文件）
    prices_max_date         : ETF 行情末日（可为 ``None``）
    deployed_grid           : 部署网格（``score_grid`` 每 N 取一后的日期），可为 ``None``

    返回
    ----
    每项形如 ``{"id": "S1", "level": "block"|"disclose", "detail": "..."}``。
    """
    findings: list[dict] = []
    target = fmt_date(asof)
    if target is None:
        return [{
            "id": "S0",
            "level": LEVEL_BLOCK,
            "detail": f"信号日无法解析（asof={asof!r}）⇒ 拒绝继续（不编日期）",
        }]

    grid = sorted({fmt_date(d) or "" for d in score_grid} - {""})

    # ---- S1：asof 不在打分日栅格内 ⇒ 这个信号日**根本不会被打分** ----
    if target not in grid:
        prev = [d for d in grid if d < target]
        nxt = [d for d in grid if d > target]
        near = ", ".join((prev[-2:] if prev else []) + (nxt[:2] if nxt else [])) or "（无）"
        findings.append({
            "id": "S1",
            "level": LEVEL_BLOCK,
            "detail": (
                f"信号日 {target} **不在打分日栅格内** ⇒ qlib_scores 不会为这一天打分"
                f"（栅格 = 每 {every_n} 个交易日取一个的相位栅格 + 末日例外，"
                f"当前共 {len(grid)} 个打分日）⇒ `analyze.py --asof {target}` 必然报"
                f"「个股分数里没有记录」。\n"
                f"   附近的打分日：{near}\n"
                f"   处置（二选一）：\n"
                f"     ① 把 --asof 改成**栅格内的日期**（上面的「附近的打分日」）；\n"
                f"     ② 若必须用 {target}，须先让打分栅格覆盖它"
                f"（重建 qlib 数据集 **属重活，需用户当轮授权**）。"
            ),
        })

    # ---- S2：qlib 数据集覆盖不到 asof ⇒ S 层根本到不了这一天 ----
    if qlib_calendar_max_date is None:
        findings.append({
            "id": "S2",
            "level": LEVEL_BLOCK,
            "detail": (
                "读不到 qlib 数据集覆盖末日（`calendars/day.txt`）⇒ 无法判定信号日可达性"
                "⇒ 拒绝继续（不静默跳过：宁可停下，也不产出注定失败的报告）"
            ),
        })
    elif qlib_calendar_max_date < target:
        lag = _days_between(qlib_calendar_max_date, target)
        findings.append({
            "id": "S2",
            "level": LEVEL_BLOCK,
            "detail": (
                f"qlib 数据集只覆盖到 **{qlib_calendar_max_date}**，早于信号日 {target}"
                f"（差 **{lag}** 个自然日）⇒ 个股层(S)根本打不到这一天"
                f" ⇒ E=W×S 的 S 不可跳过（铁律 §十四.2）⇒ 拒绝继续。\n"
                f"   处置（二选一）：\n"
                f"     ① 重建 / 更新 qlib 数据集（**重活，需用户当轮授权**）后重跑 qlib_scores.py；\n"
                f"     ② 把 --asof 改成 ≤ {qlib_calendar_max_date} 且落在打分日栅格内的日期。"
            ),
        })

    # ---- S3：ETF 行情层比个股层新 ⇒ 两层 vintage 不一致（只披露） ----
    if prices_max_date and scores_max_date and prices_max_date > scores_max_date:
        lag = _days_between(scores_max_date, prices_max_date)
        findings.append({
            "id": "S3",
            "level": LEVEL_DISCLOSE,
            "detail": (
                f"ETF 行情已到 **{prices_max_date}**，而个股分数只到 **{scores_max_date}**"
                f"（两层 vintage 相差 **{lag}** 个自然日）⇒ 特征层按 `date <= asof` 截断"
                f" ⇒ **无前视**；但行情层已更新的信息**尚未进入 S 层** ⇒ "
                f"该信息差在本轮信号里不生效（只披露，不拦）。"
            ),
        })

    # ---- S4：asof 不在部署网格（回测口径）⇒ 与回测网格错位（只披露） ----
    if deployed_grid:
        dep = sorted({fmt_date(d) or "" for d in deployed_grid} - {""})
        if target not in dep:
            near = ", ".join(dep[-3:]) or "（无）"
            findings.append({
                "id": "S4",
                "level": LEVEL_DISCLOSE,
                "detail": (
                    f"信号日 {target} **不在部署网格（回测口径：打分日每 N 取一）**内"
                    f" ⇒ 本轮信号与回测网格**错位**（回测网格末尾：{near}）"
                    f" ⇒ 该点的回测数字**不能直接外推**（只披露，不拦）。"
                ),
            })
    return findings


def has_block(findings: list[dict]) -> bool:
    """findings 里有没有**硬拦**项（``level == 'block'``）。"""
    return any(f.get("level") == LEVEL_BLOCK for f in findings or [])
