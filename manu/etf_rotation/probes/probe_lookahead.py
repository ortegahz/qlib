#!/usr/bin/env python
"""前视审计闸门（**只读**，不写任何文件、不联网、不跑 LLM）。

【为什么需要（2026-11-05 02:0x 实测，专题档 §39）】
M-L 把「无未来函数」从**口头承诺**变成**可复算的台账**：

* 🔴 本链**唯一无法修掉**的前视是 **W 成分快照**：``etf_holdings.csv`` 的
  ``fetch_date = 2026-11-04`` 晚于信号日 ``asof = 2026-11-03``（+1 天），
  而 akshare 的 ``fund_etf_holdings_tx`` / ``fund_etf_pcf`` 只给当前快照、取不到历史 PCF
  ⇒ **asof 当日的真实成分集不可观测** ⇒ 只能**量化 + 披露**，不能靠改代码消除。
* 🔴 关键口径：**文件含未来行 ≠ 前视**。判据是「**实际被消费**的那部分数据的日期 > asof」。
  例如 ``etf_prices.csv`` 文件最晚 2026-11-04，但特征层严格 ``date <= asof``
  ⇒ 实际消费到 2026-11-03 ⇒ **不是前视**。
* ⇒ 本闸门守的是三件事：
  ① 台账**恰好 1 项**决策前视，且**必须**是 W 快照（不许漏、也不许凑数）；
  ② E 上界与**独立重算**逐只一致（不信任产物自述）；
  ③ **余量 ≤ 0 ⇒ 名单稳健**的结论与文案必须**自洽**（不许「余量≤0 却写可能换」）。

【本探针做什么】
**不信任产物自述**：E 上界、池内 σ(E)、台账条数都**独立重算**（本文件自带一份**另写**的
实现，从原始 CSV 起算）再与报告比对；并用**负例**证明断言**真的会拦人**
（把产物改坏 ⇒ 对应断言必须 FAIL）。

🔴 **负例纪律（rules.md §十四.68）**：负例必须**真的改到被断言的对象上**。
本探针的被断言对象是 ``dict`` ⇒ 负例用 ``copy.deepcopy`` 后**原地改字段**再喂回
``evaluate()``（dict 是引用语义 ⇒ 改动一定生效），**不做**「字符串 replace 不回写」那种假负例。

【用法】
    PYTHONHASHSEED=0 /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_lookahead.py

    # 断言对象 = 本轮快照内那一份（**数据根目录也跟着切到快照** ⇒ 快照自包含）
    ... probe_lookahead.py --snapshot-root <落点根>/runs/snapshots

🔴 **数据根目录口径（2026-11-10）**：``stock_scores.csv`` / ``stock_prices.csv``
从**同一个**目录读 —— 基线口径 = canonical ``RUN_ROOT``（根目录），
快照口径 = 快照目录（**不许**回落 canonical：断言对象与数据不同源 = 假绿）。
快照里缺这两个件 ⇒ 退出码 **2「不适用」**（≠ 通过）。

【副作用】除 **MD 级真负例**外**零写入**（只读官方产物与原始 CSV）。
MD 级真负例（§十四.68）会把报告 md 复制一份到 ``<落点根>/runs/`` 下的临时目录、
**真的改坏披露**后用子进程跑一遍本探针（必须非零退出），**跑完立刻删掉临时目录**
⇒ 全程**不写 /tmp**。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from pathlib import Path
import numpy as np
import pandas as pd
#: 研究链根（探针目录不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ETF_ROOT = Path(__file__).resolve().parents[1]
if str(_ETF_ROOT) not in sys.path:
    sys.path.insert(0, str(_ETF_ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）


RUN_ROOT = C.ETF_RUN_ROOT
ETF_DATA = C.ETF_DATA_ROOT
QLIB_DATA = Path(C.PROVIDER_URI)

#: 🔴 **数据根目录**里必须**真实存在**的两个件（个股分数 / 个股日线）。
#:    快照自包含契约（`probe_snapshot_contract.CONTRACT_ARTIFACTS` 16 件）里就有这两个
#:    ⇒ 快照口径下**必须**从快照目录读（快照自包含），
#:    **不许**回落根目录 canonical 再报「通过」。
DATA_ROOT_FILES = ("stock_scores.csv", "stock_prices.csv")

#: E 上界复算容差（绝对值）
TOL_E = 1e-6
#: 「小权重成分」的权重上限（%），必须与报告里 small_weight_cap 一致
SMALL_WEIGHT_CAP = 5.0
#: 🔴 2026-11-10：决策前视项的**期望条数**不再写死（原 `EXPECTED_N_LOOKAHEAD = 1`
#:    是 asof=2026-11-03 那轮的形态）⇒ 改成**事实源派生**
#:    （W 快照日 > asof ⇒ 1 项，否则 0 项）⇒ 见 `expected_lookahead_count()`。
#: 唯一允许的决策前视项（名字必须含它）
EXPECTED_LOOKAHEAD_KEY = "etf_holdings.csv"
#: 「口径① 最终分差最近（|final_gap| 最小 ⇒ 最容易翻）」在 MD 里的标记词
CALIBER_CLOSEST_MARK = "最终分差最近"
#: 🔴 **绝对不许**用来称呼某一对的措辞（把「极端档余量最大」叫成「最接近/最紧」＝旧披露 bug）
FORBIDDEN_CLOSE_WORDS = ("最接近", "最紧")
#: asof=2026-11-03 的**实测**金标（**2026-11-11 第四次重新实测**；来源
#: ``lookahead_audit.flip_test.same_theme_closest_by_gap`` 第一名）：
#: |final_gap| 最小的一对 = 512800 ↔ 512820，分差 +0.029595 / 现实档翻转阈值 0.187930
#: / 现实档余量 −0.158335（**最容易翻** ⇒ 不许被写成别的对）。
#: 🔴 2026-11-11 **第四次金标换值**（不是断言增删）：上一版金标是
#:    final_gap **0.024412** / thr **0.177043** / margin **−0.152631**。
#:    **换值根因 = 排序口径变了**（按架构文档 §二.2 / §二.4 对齐）：
#:      ① EWMA **alpha 0.35 ⇒ 0.20**（`config.SMOOTHING_ALPHA`；半衰期 1.61 ⇒ 3.10 个交易日）
#:      ② 入场闸门 **MA20 ⇒ MA60**（`config.ENTRY_MA_FILTER_WINDOW`）
#:      ③ 单标的权重上限 **40% ⇒ 20%**（`config.MAX_SINGLE_WEIGHT`）
#:    ⇒ 池内 `final_score`（= w_alpha·z_alpha + w_mom·z_mom）随平滑分改变 ⇒
#:      **分差与翻转阈值同时变化**（这次 thr 也从 0.177043 变成 0.187930）。
#:    ⇒ **不是**报告算错了 —— 复核证据：
#:      ① 5 条目仍按 |final_gap| 升序排列且首条即最小；
#:      ② 恒等式 `margin_realistic = final_gap − swap_threshold_realistic` 仍成立
#:         （0.029595 − 0.187930 = −0.158335）；
#:      ③ 报告 md「八之四」段落独立写着同一对与同一组数字（交叉印证，非自说自话）。
#:    ⇒ **重测触发条件**不变 = 任何**改到池内 `final_score`** 的输入刷新
#:      （价格 / 交易日历 / W 快照 / 平滑观测集 / 个股分数 / **平滑参数** / **均线闸门**）。
#:    ⇒ 旧值**保留在下面注释里作证据**（不许无声改掉 ⇒ 下次漂移能对比）。
#: 🔴 2026-11-11 **金标换值**（不是断言增删）：上一版金标是
#:    final_gap **0.024434** / thr 0.177043 / margin **−0.152609**。
#:    **换值根因 = 输入刷新（价格 / 交易日历）⇒ 动量侧（`z_mom`）变了**，
#:    实测依据 = OLD（`generated_at=2026-11-10T10:11`）↔ NEW（`generated_at=2026-11-11T10:52`）
#:    两份**真实产物**对拍：
#:      · `e_score` / `smoothed_score` / `z_alpha` **逐位未变** ⇒ **E 侧与平滑观测集没变**；
#:      · 变的是**动量侧**（`z_mom`）；`1.6934904 → 1.6934020` 是 **516310** 的 `momentum_score`
#:        （🔴 **不是** 512820 —— 旧注释这里写错过，已改）；
#:      · 真因 = **价格 / 交易日历输入刷新**：`etf_prices` 2026-11-04→2026-11-10、
#:        `qlib_calendar` 2026-11-03→2026-11-09；而 `etf_holdings`（W 快照）/
#:        `stock_prices` / `stock_scores` **逐位未变**；
#:      · `z_mom` 是**全池截面 z** ⇒ 动量分一动，**5 条 gap 全部跟着动**
#:        （|final_gap| 升序：0.024434/0.062584/0.065616/0.262260/0.564080
#:          → 0.024412/0.062542/0.065580/0.275613/0.577386）
#:        ⇒ 🔴 「只有第 1 条动、其余 4 条逐位未变」的说法**是错的**
#:          （那是拿旧值反推的自我循环；此处已按真实产物对拍更正）；
#:      · **5 条翻转阈值逐位不变**（含 0.177043）⇒ 变的是分差，不是阈值口径。
#:    ⇒ 已**排除**的两条路径（都**不是**根因）：
#:      ① 「留 20% 现金」（`CASH_RESERVE_RATIO` 只进 `equity_budget` 的**目标权重与披露**，
#:         `final_score = w_alpha·z_alpha + w_mom·z_mom` 与权重无关）；
#:         席位口径实际是 **5×20% → 5×16%（席位数不变**，`TARGET_POSITIONS = 5`、
#:         `config.TARGET_N_HOLDINGS = 5` 都没动**）**
#:         ⇒ 🔴 旧注释「席位由 4×20% 改成 5×16%」与代码事实不符，已改；
#:      ② `config.ANALYZE_TOP_N` 15→100（只切 `candidates` 与 `la_bounds` 的样本代码，
#:         **不碰** `chain["pool"]` 与 `final_score`；本轮池两次都是 15 只）。
#:    **不是**报告算错了 —— 复核证据：① 5 条目按 |final_gap| 升序排列且首条即最小
#:    （0.024412 < 0.062542 < 0.065580 < 0.275613 < 0.577386）；
#:    ② 5 条目全部满足恒等式 `margin_realistic = final_gap − swap_threshold_realistic`
#:    （0.024412 − 0.177043 = −0.152631）；
#:    ③ 翻转阈值 0.177043 **未变**（⇒ 变的是分差，不是阈值口径）。
#:    ⇒ **重测触发条件** = 任何**改到池内 `final_score`** 的输入刷新
#:      （价格 / 交易日历 / W 快照 / 平滑观测集 / 个股分数）；
#:      **不是**现金预留比例，**也不是** top_n。
#:    ⇒ 旧值**保留在下面注释里作证据**（不许无声改掉 ⇒ 下次漂移能对比）。
#: 🔴 2026-11-09 **金标换值**（不是断言增删）：上一版金标是 159547 ↔ 512890
#:    （gap −0.023897 / thr 0.404878 / margin −0.428775，同日**平滑前**实测）。
#:    换值根因 = **排序口径变了**（2026-11-09 新增「穿透分时间平滑」：
#:    排序改用 **5 交易日 EWMA 平滑分**（α=0.35）⇒ 入选名单与名次跟着变
#:    ⇒ 同一 asof 下「同主题最近的一对」跟着变），
#:    **不是**报告算错了 —— 复核证据：① 5 条目按 |final_gap| 升序排列且首条即最小
#:    （0.024434 < 0.062584 < 0.065616 < 0.262260 < 0.564080
#:      —— 🔴 **2026-11-09 当时**的产物数字；2026-11-11 的输入刷新已把它改成
#:      0.024412 / 0.062542 / 0.065580 / 0.275613 / 0.577386 ⇒ **现值见上面 2026-11-11 段**）；
#:    ② 5 条目全部满足恒等式 `margin_realistic = final_gap − swap_threshold_realistic`；
#:    ③ 报告 md「八之四」段落独立写着同一对与同一组数字（交叉印证，非自说自话）。
#:    ⇒ 旧值**保留在下面注释里作证据**（不许无声改掉 ⇒ 下次漂移能对比）。
GOLD_ASOF = "2026-11-03"
GOLD_CLOSEST = {
    "selected": "512800",
    "challenger": "512820",
    "final_gap": 0.029595,
    "swap_threshold_realistic": 0.187930,
    "margin_realistic": -0.158335,
}
#: 数值比对容差（绝对值；报告里的数字都是 6 位小数的 round() 结果）
TOL_NUM = 1e-9
#: MD 负例用的临时目录**必须**在持久盘上（rules.md §〇之二：cache/产物不落 /tmp）
NEG_TMP_PARENT = C.WORK_ROOT
#: 子进程跑 MD 负例时的环境变量守卫（防递归：子进程不再做 MD 负例）
ENV_SKIP_MD_NEG = "ETF_PROBE_LOOKAHEAD_SKIP_MD_NEG"


# --------------------------------------------------------------------------- #
# MD 抽取助手（供 L17~L20 用）
# --------------------------------------------------------------------------- #

def md_section(md_text: str, heading: str) -> str:
    """取 MD 里某个 ``## 标题`` 到下一个同级标题之间的正文（取不到 ⇒ 空串）。

    🔴 **按节取**，不许整篇扫：前视审计段与风险披露段各自只该说自己的口径。
    """
    out: list[str] = []
    inside = False
    for ln in md_text.splitlines():
        if ln.startswith(heading):
            inside = True
            continue
        if inside and ln.startswith("## "):
            break
        if inside:
            out.append(ln)
    return "\n".join(out)


def _caliber_first_line(section: str, pair: dict, mark: str,
                        known_codes: set[str]) -> tuple[bool, str]:
    """取 ``section`` 里**第一条**带配对代码且含 ``mark`` 的行，核对它点名的是不是 ``pair``。

    🔴 取「**第一条**」而不是「任意一条」：口径①的列表会把同一对排在不同名次上，
    只看「任意一条」会被后面的名次蒙混过关 ⇒ 口径①的**头一条**披露必须是
    |final_gap| 最小的那一对。
    返回 ``(是否命中, 详情)``；三个数字（分差 / 翻转阈值 / 余量）一个都不能少。
    """
    target = {str(pair.get("selected") or ""), str(pair.get("challenger") or "")}
    num_txt = [str(n) for n in (pair.get("final_gap"),
                                pair.get("swap_threshold_realistic"),
                                pair.get("margin_realistic")) if n is not None]
    first = None
    for ln in section.splitlines():
        if mark not in ln:
            continue
        if not any(c and c in ln for c in known_codes):
            continue
        first = ln
        break
    if first is None:
        return False, f"没有一条同时含「{mark}」和配对代码的行"
    named = {c for c in known_codes if c in first}
    if not target.issubset(named):
        return False, (f"第一条「{mark}」的行点名的是 {sorted(named)}，"
                       f"不是 |final_gap| 最小的那对 {sorted(target)}：{first.strip()[:110]}")
    miss = [t for t in num_txt if t not in first]
    if miss:
        return False, f"第一条「{mark}」的行缺数字 {miss}：{first.strip()[:110]}"
    return True, first.strip()[:150]

# --------------------------------------------------------------------------- #
# 独立实现（**不复用** lookahead.py，避免「自己证明自己」）
# --------------------------------------------------------------------------- #

def _instrument(stock_code: str, exchange: str | None) -> str | None:
    """``300857.SZ`` + ``SZ`` ⇒ ``SZ300857``（独立实现，优先用交易所列）。"""
    code = str(stock_code or "").strip().upper()
    if not code:
        return None
    head, _, tail = code.partition(".")
    if head.isalpha():                       # 已是 SH600000 形式
        return head
    digits = head.zfill(6)
    if not digits.isdigit():
        return None
    ex = str(exchange or "").strip().upper()
    if ex in ("SH", "SZ", "BJ"):
        return f"{ex}{digits}"
    if tail in ("SH", "SZ", "BJ"):            # 后缀式 600000.SH
        return f"{tail}{digits}"
    return None


def independent_e_bounds(asof: pd.Timestamp, codes: list[str] | None = None,
                         data_root: Path | None = None) -> dict:
    """独立重算 E 与「单只成分股进出」的 E 误差上界（两档）。

    返回 ``{code: {e_score, n_scored, delta_e_extreme, delta_e_small_weight}}``。

    🔴 ``data_root``：**个股分数**（``stock_scores.csv``）所在目录。
       缺省 = ``RUN_ROOT``（canonical ⇒ 基线行为逐字节不变）；
       快照口径 = 快照目录（快照自包含 ⇒ 断言对象与数据同源）。
    """
    asof = pd.Timestamp(asof).normalize()
    root = Path(data_root) if data_root is not None else RUN_ROOT
    h = pd.read_csv(ETF_DATA / "etf_holdings.csv",
                    dtype={"etf_code": str, "stock_code": str})
    h["etf_code"] = h["etf_code"].astype(str).str.zfill(6)
    h["weight_pct"] = pd.to_numeric(h["weight_pct"], errors="coerce")
    h["instrument"] = [_instrument(c, e) for c, e in
                       zip(h["stock_code"].to_numpy(), h["stock_exchange"].to_numpy())]

    sc = pd.read_csv(root / "stock_scores.csv", dtype={"instrument": str})
    sc["date"] = pd.to_datetime(sc["date"])
    sc = sc[sc["date"] == asof].copy()
    if sc.empty:
        raise ValueError(f"{asof.date()} 个股分数为空，无法复算 E 上界")
    mu = float(sc["score"].mean())
    sd = float(sc["score"].std(ddof=0))
    if not np.isfinite(sd) or sd == 0:
        raise ValueError(f"{asof.date()} 个股分数方差为 0（退化）⇒ 拒绝复算")
    sc["z"] = (sc["score"] - mu) / sd
    zmap = dict(zip(sc["instrument"].astype(str), sc["z"]))

    want = {str(c).zfill(6) for c in codes} if codes else None
    out: dict[str, dict] = {}
    for code, g in h.groupby("etf_code", sort=True):
        if want is not None and code not in want:
            continue
        pairs = [(float(w), zmap.get(str(inst))) for w, inst in
                 zip(g["weight_pct"].to_numpy(), g["instrument"].to_numpy())
                 if inst is not None and str(inst) in zmap and np.isfinite(w)]
        if not pairs:
            continue
        w = np.array([p[0] for p in pairs], dtype=float)
        z = np.array([p[1] for p in pairs], dtype=float)
        if w.sum() <= 0:
            continue
        e = float(np.average(z, weights=w))
        with np.errstate(divide="ignore", invalid="ignore"):
            delta = np.abs(w * (z - e) / (w.sum() - w))
        delta = np.where(np.isfinite(delta), delta, np.nan)
        small_mask = w <= SMALL_WEIGHT_CAP
        out[str(code)] = {
            "e_score": e,
            "n_scored": int(len(w)),
            "delta_e_extreme": (float(np.nanmax(delta))
                                if np.isfinite(delta).any() else None),
            "delta_e_small_weight": (float(np.nanmax(delta[small_mask]))
                                     if small_mask.any()
                                     and np.isfinite(delta[small_mask]).any() else None),
        }
    return out


def independent_ledger(asof: pd.Timestamp, data_root: Path | None = None) -> dict:
    """独立重算「决策前视」项：逐个带日期输入按**各自实际消费规则**取有效日期。

    🔴 这是「独立于 lookahead.py」的第二份实现 ⇒ 两边一致 ⇒ 台账可信。

    🔴 ``data_root``：**个股分数 / 个股日线**所在目录。
       缺省 = ``RUN_ROOT``（canonical ⇒ 基线行为逐字节不变）；
       快照口径 = 快照目录（快照自包含 ⇒ 断言对象与数据同源）。
       🔴 注意：``etf_holdings.csv`` / ``etf_prices.csv`` / ``etf_universe.csv``
          / ``trade_calendar.csv`` 属于**数据集**（``ETF_DATA``），
          **不在**快照契约 16 件里 ⇒ 仍读 canonical 数据集目录（口径不变）。
    """
    asof = pd.Timestamp(asof).normalize()
    root = Path(data_root) if data_root is not None else RUN_ROOT
    asof_str = asof.strftime("%Y-%m-%d")
    items: list[dict] = []
    non_decision: list[dict] = []

    # (1) W 成分快照：**整表按 fetch_date 消费** ⇒ 有效日期 = max(fetch_date)
    h = pd.read_csv(ETF_DATA / "etf_holdings.csv", dtype={"etf_code": str},
                    usecols=["fetch_date"])
    fd = h["fetch_date"].astype(str).str.slice(0, 10)
    w_eff = sorted(fd.dropna().unique())
    w_eff = w_eff[-1] if w_eff else None
    if w_eff and pd.Timestamp(w_eff) > asof:
        items.append({"name": "etf_holdings.csv(W 成分快照)", "effective_date": w_eff})

    # (2) ETF 日线：特征层只取 date<=asof ⇒ 有效日期 = max(date<=asof)
    p = pd.read_csv(ETF_DATA / "etf_prices.csv", usecols=["date"])
    p["date"] = p["date"].astype(str).str.slice(0, 10)
    peff = p.loc[p["date"] <= asof_str, "date"].max() if len(p) else None
    if peff and pd.Timestamp(peff) > asof:
        items.append({"name": "etf_prices.csv", "effective_date": peff})

    # (3) 个股分数 / 个股日线：直接按 asof 消费
    for name, path in (("stock_scores.csv", root / "stock_scores.csv"),
                       ("stock_prices.csv", root / "stock_prices.csv")):
        if not path.exists():
            continue
        d = pd.read_csv(path, usecols=["date"], dtype=str)
        eff = d["date"].astype(str).str.slice(0, 10).max()
        if eff and pd.Timestamp(eff) > asof:
            items.append({"name": name, "effective_date": eff})

    # (4) 候选池：静态属性（无时间维度）⇒ **不可判为前视**；兜底成交额单列
    u = pd.read_csv(ETF_DATA / "etf_universe.csv")
    u_fetch = sorted(u["fetch_date"].astype(str).str.slice(0, 10).dropna().unique())
    u_fetch = u_fetch[-1] if u_fetch else None
    non_decision.append({"name": "etf_universe.csv(静态属性)",
                         "file_date": u_fetch,
                         "note": "静态属性无时间维度 ⇒ 不是前视"})

    # (5) 日历 / 回测产物：**设计上**就在 asof 之后，且不参与打分
    cal = ETF_DATA / "trade_calendar.csv"
    if cal.exists():
        d = pd.read_csv(cal, dtype=str)
        col = "date" if "date" in d.columns else d.columns[0]
        non_decision.append({"name": "trade_calendar.csv",
                             "file_date": d[col].astype(str).str.slice(0, 10).max(),
                             "note": "日历类输入，设计上就在 asof 之后 ⇒ 不算决策前视"})

    return {
        "asof": asof_str,
        "n_decision_lookahead": len(items),
        "items": items,
        "non_decision": non_decision,
        "w_snapshot_date": w_eff,
    }


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #

def expected_lookahead_count(indep: dict, asof: str) -> int:
    """**事实源派生**的期望决策前视条数：W 快照日 > asof ⇒ 1 项，否则 0 项。

    🔴 2026-11-10（实测红灯）：旧实现把期望值**写死**成 1（`EXPECTED_N_LOOKAHEAD`），
       那是 **asof=2026-11-03** 那轮的形态（W 快照 2026-11-04 > asof ⇒ 1 项）。
       本轮 asof=2026-11-09、W 成分快照只有 **2026-11-04**（早于 asof 5 天）
       ⇒ 决策前视**合法地变成 0 项**（报告 `n_look_ahead=0` / `blocking=[]`，
       探针自己的独立重算也是 0 ⇒ L9 PASS）⇒ 事实源没坏，是断言写死了旧形态。
    ⇒ 期望值改成**从事实源派生**（不许放宽成永真）：W 快照日 > asof ⇒ 1，否则 0。
    """
    w_eff = str(indep.get("w_snapshot_date") or "")
    return 1 if (w_eff and w_eff > str(asof or "")) else 0


def evaluate(report: dict, md_text: str, indep: dict) -> list[tuple[str, bool, str]]:
    """对给定报告跑**全部**断言，返回 [(名称, 是否通过, 详情), ...]。

    🔴 本函数**只依赖入参** ⇒ 负例可以直接喂「改坏的报告」，不必重跑流水线。
    """
    out: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, bool(ok), detail))

    la = report.get("lookahead_audit") or {}
    inputs = la.get("inputs") or {}
    entries = inputs.get("entries") or []
    wsnap = la.get("w_snapshot") or {}
    eb = la.get("e_bound") or {}
    per_etf = eb.get("per_etf") or []
    flip = la.get("flip_test") or {}
    disc = {d.get("id"): d for d in (report.get("risk_disclosure") or [])}

    # ---- L1 前视审计段存在且四件套齐全 ----
    add("L1 报告必须带 lookahead_audit 段（inputs/w_snapshot/e_bound/flip_test 四件套齐全）",
        bool(la) and all(la.get(k) for k in ("inputs", "w_snapshot", "e_bound", "flip_test")),
        f"keys={sorted(la.keys())}")

    # ---- L2 MD 必须有「八之四、前视审计」小节 ----
    add("L2 报告 MD 必须有「八之四、前视审计」小节",
        "八之四、前视审计" in md_text,
        "命中" if "八之四、前视审计" in md_text else "未命中")

    # ---- L3 台账条数：必须等于**事实源派生**的期望值 ----
    n_la = inputs.get("n_look_ahead")
    _w_eff = str(indep.get("w_snapshot_date") or "")
    _asof = str(inputs.get("asof") or "")
    _expect = expected_lookahead_count(indep, _asof)
    add("L3 输入日期台账：决策前视条数必须等于**事实源派生**的期望值"
        "（W 快照日 > asof ⇒ 1 项，否则 0 项；多一项 ⇒ 链路有未披露的前视）",
        n_la == _expect,
        f"报告 n_look_ahead={n_la}｜期望={_expect}｜"
        f"W 快照日={_w_eff or '无'} vs asof={_asof}｜"
        f"独立重算={indep.get('n_decision_lookahead')}")

    # ---- L4 决策前视项：期望 1 项时必须是**唯一的** W 成分快照；期望 0 项时 blocking 必须为空 ----
    blocking = inputs.get("blocking") or []
    if _expect == 1:
        ok4 = (
            len(blocking) == 1
            and EXPECTED_LOOKAHEAD_KEY in str(blocking[0].get("name", ""))
            and str(blocking[0].get("effective_date", "")) > str(inputs.get("asof", ""))
        )
        detail4 = (f"期望 1 项｜blocking={[(b.get('name'), b.get('effective_date')) for b in blocking]}"
                   f"｜asof={inputs.get('asof')}")
    else:
        #: 期望 0 项时：**多报一项同样是误披露** ⇒ `blocking` 必须**恰好为空**
        #: （不许写成「随便」/「不为空也行」）。
        ok4 = (len(blocking) == 0)
        detail4 = (f"期望 0 项（W 快照日={_w_eff or '无'} ≤ asof={inputs.get('asof')}）"
                   f"｜blocking={[(b.get('name'), b.get('effective_date')) for b in blocking]}"
                   f"（必须为空）")
    add("L4 决策前视项：期望 1 项时必须是**唯一的** W 成分快照（etf_holdings.csv）"
        "且有效日 > asof；期望 0 项时 blocking 必须为空",
        ok4, detail4)

    # ---- L5 台账里**没有第二条**被标成前视 ----
    n_flag = sum(1 for e in entries if e.get("look_ahead") is True)
    add("L5 台账逐条口径自洽：标成前视（look_ahead=True）的条数 == n_look_ahead",
        n_flag == (n_la if isinstance(n_la, int) else -1),
        f"逐条 look_ahead=True 的条数={n_flag}｜n_look_ahead={n_la}")

    # ---- L6 非前视项：实际消费日 ≤ asof（文件晚不算前视） ----
    # 🔴 唯一的合法例外是「avg_amount_20d 兜底」项：它的快照日晚于 asof，
    #    但**实测惰性**（用到兜底且通过流动性过滤的 ETF = 0 只 ⇒ 未来成交额没进过决策）
    #    ⇒ 例外的合法性由 L7 单独把关，这里只检查「例外**唯一**且**就是兜底项**」。
    asof_str = str(inputs.get("asof") or "")
    over = [e for e in entries
            if e.get("role") == "decision" and e.get("look_ahead") is not True
            and e.get("effective_date") and str(e["effective_date"]) > asof_str]
    # 唯一被承认的例外：**avg_amount_20d 兜底**（流动性过滤的兜底成交额）
    #   —— 其合法性完全依赖「实测惰性」（L7：用到兜底且过流动性过滤的 ETF = 0 只）
    bad_eff = [e.get("name") for e in over if "avg_amount_20d" not in str(e.get("name", ""))]
    only_fallback = len(over) == 1 and "avg_amount_20d" in str(over[0].get("name", "")) \
        if over else True
    inert_ok = (inputs.get("liquidity_fallback") or {}).get("inert") is True
    add("L6 非前视项的**实际消费日**必须 ≤ asof；唯一例外是**已实测惰性**的 avg_amount_20d 兜底项",
        (not bad_eff) and only_fallback and inert_ok,
        f"越界项={bad_eff}｜越界项总数={len(over)}｜"
        f"越界项名={[e.get('name') for e in over]}｜兜底惰性={inert_ok}"
        + ("" if only_fallback else "（⇒ 例外不止兜底项！）"))

    # ---- L7 成交额兜底必须**惰性**（用到兜底且通过流动性过滤的只数必须为 0） ----
    fb = inputs.get("liquidity_fallback") or {}
    ok7 = fb.get("n_need_fallback_pass_liq") == 0 and fb.get("inert") is True
    add("L7 候选池 avg_amount_20d 兜底必须**惰性**（用到未来成交额且过流动性过滤 = 0 只）",
        ok7,
        f"n_need_fallback={fb.get('n_need_fallback')}｜"
        f"n_need_fallback_pass_liq={fb.get('n_need_fallback_pass_liq')}｜"
        f"inert={fb.get('inert')}")

    # ---- L8 W 快照：只有 1 个快照日、**不可修**、不可观测理由非空 ----
    # 🔴 2026-11-10（实测红灯）：旧实现把「`snapshot_date > asof`」也写进了合取
    #    ⇒ 本轮（W 快照 2026-11-04 **早于** asof 2026-11-09）合法地**不成立**
    #    ⇒ L8 假红。事实源没坏：W 快照只有 1 个快照日、不可修、给了不可观测理由
    #    —— 这些**才是**「结构性不可修」的判据。
    # ⇒ 修法：**删掉**「snapshot_date > asof」这一条合取，
    #   其余（`n_snapshot_dates == 1`、`fixable is False`、`unobservable_reason` 非空）
    #   **全部保留**；detail 里显式打出「是否晚于 asof」，避免再误判。
    _wsnap_date = str(wsnap.get("snapshot_date") or "")
    _wsnap_late = bool(_wsnap_date) and _wsnap_date > str(inputs.get("asof", ""))
    ok8 = (
        wsnap.get("snapshot_date") is not None
        and wsnap.get("n_snapshot_dates") == 1
        and wsnap.get("fixable") is False
        and bool(str(wsnap.get("unobservable_reason") or "").strip())
    )
    add("L8 W 快照必须被标成**结构性不可修**（只有 1 个快照日、不可修、给了不可观测理由）",
        ok8,
        f"snapshot_date={wsnap.get('snapshot_date')}｜asof={inputs.get('asof')}｜"
        f"晚于 asof={_wsnap_late}｜gap_days={wsnap.get('gap_days')}｜"
        f"n_snapshot_dates={wsnap.get('n_snapshot_dates')}｜"
        f"fixable={wsnap.get('fixable')}")

    # ---- L9 独立重算：台账条数与身份一致 ----
    indep_names = sorted(i["name"] for i in indep.get("items") or [])
    rep_names = sorted(str(b.get("name")) for b in blocking)
    ok9 = (indep.get("n_decision_lookahead") == n_la
           and len(indep_names) == len(rep_names)
           and all(EXPECTED_LOOKAHEAD_KEY in n for n in indep_names))
    add("L9 决策前视条数与身份：与**独立重算**一致（且必须是 W 快照）",
        ok9,
        f"独立重算={indep_names}｜报告 blocking={rep_names}")

    # ---- L10 E 上界与独立重算逐只一致 ----
    indep_bounds = indep.get("e_bounds") or {}
    devs: list[tuple[str, float]] = []
    for row in per_etf:
        code = str(row.get("code"))
        ib = indep_bounds.get(code)
        if not ib:
            devs.append((code, float("inf")))
            continue
        for field in ("e_score", "delta_e_extreme", "delta_e_small_weight"):
            rv = row.get(field)
            if rv is None or ib.get(field) is None:
                continue
            d = abs(float(rv) - float(ib[field]))
            if d > TOL_E:
                devs.append((f"{code}.{field}", d))
    add("L10 E 值与两档误差上界：与**独立重算**逐只一致（容差 1e-6）",
        not devs,
        f"逐只最大偏差={max((d for _, d in devs), default=0.0):.2e}｜越界={devs[:5]}"
        if devs else f"复算 {len(per_etf)} 只全部一致")

    # ---- L11 上界最大值与独立重算的最大值一致 ----
    def _max_of(d: dict, key: str) -> float | None:
        vals = [v[key] for v in d.values() if v.get(key) is not None]
        return max(vals) if vals else None

    mr_rep, mr_ind = eb.get("max_delta_e_realistic"), _max_of(indep_bounds, "delta_e_small_weight")
    me_rep, me_ind = eb.get("max_delta_e_extreme"), _max_of(indep_bounds, "delta_e_extreme")
    ok11 = (mr_rep is not None and mr_ind is not None and abs(float(mr_rep) - mr_ind) <= TOL_E
            and me_rep is not None and me_ind is not None and abs(float(me_rep) - me_ind) <= TOL_E)
    add("L11 E 上界的**最大值**（现实/极端）必须与独立重算一致",
        ok11,
        f"现实 报告={mr_rep}｜独立={mr_ind}｜极端 报告={me_rep}｜独立={me_ind}")

    # ---- L12 排名稳健性：同主题换产品的余量 ≤ 0（现实 & 极端） ----
    ms_r = flip.get("max_same_theme_margin_realistic")
    ms_e = flip.get("max_same_theme_margin_extreme")
    ok12 = (ms_r is not None and float(ms_r) <= 0
            and ms_e is not None and float(ms_e) <= 0)
    add("L12 排名稳健性：**同主题换产品**的最大余量 ≤ 0（现实档 & 极端档）⇒ 名单不变",
        ok12,
        f"现实档余量={ms_r}｜极端档余量={ms_e}")

    # ---- L13 换主题：无第三主题（余量 None）或余量 ≤ 0 ----
    mc_r = flip.get("max_cross_theme_margin_realistic")
    mc_e = flip.get("max_cross_theme_margin_extreme")
    ok13 = ((mc_r is None or float(mc_r) <= 0) and (mc_e is None or float(mc_e) <= 0))
    add("L13 排名稳健性：**换主题**要么池内无第三主题（余量 None），要么余量 ≤ 0",
        ok13,
        f"现实档余量={mc_r}｜极端档余量={mc_e}｜主题构成={flip.get('theme_composition')}")

    # ---- L14 结论文案必须与余量**符号自洽**（余量≤0 却写「可能被改」= 违规） ----
    concl = str(flip.get("conclusion") or "")
    gaps = flip.get("same_theme_gaps") or []
    worst = max((float(g.get("margin_realistic")) for g in gaps
                 if g.get("margin_realistic") is not None), default=None)
    if worst is None:
        ok14 = True
        detail = "无同主题配对 ⇒ 无需判定文案"
    else:
        ok14 = (worst <= 0 and "可能被改" not in concl) or (worst > 0 and "可能被改" in concl)
        detail = f"最危险同主题余量={worst}｜结论文案含「可能被改」={'可能被改' in concl}"
    add("L14 结论文案必须与余量**符号自洽**（余量 ≤ 0 时不许写「可能被改」）",
        ok14, detail)

    # ---- L15 风险披露 w_snapshot_bias 必须带**实测数字**（不许写「不可量化」） ----
    wsb = disc.get("w_snapshot_bias") or {}
    det = str(wsb.get("detail") or "")
    need_nums = [eb.get("max_delta_e_realistic"), eb.get("max_delta_e_extreme")]
    has_digits = any(ch.isdigit() for ch in det)
    no_vague = ("不可量化" not in det) and ("无法量化" not in det)
    ok15 = has_digits and no_vague and det != ""
    add("L15 风险披露 w_snapshot_bias 必须带**实测数字**且不许再写「不可量化」",
        ok15,
        f"E 上界={need_nums}｜披露含数字={has_digits}｜含「不可量化」={'不可量化' in det}")

    # ---- L16 风险披露必须覆盖 7 类必需要项（口径含前视/换手/容量…） ----
    need_ids = ("in_sample_selection_bias", "current_drawdown_position", "w_snapshot_bias",
                "pool_attribute_limits", "turnover_and_cost", "non_lookthrough_etf",
                "strategy_capacity")
    missing = [i for i in need_ids if i not in disc]
    add("L16 风险披露必须覆盖 7 类必需要项（样本内偏差/回撤/W 快照/池属性/换手成本/不可穿透/容量）",
        not missing,
        f"缺失={missing}" if missing else f"已覆盖 {len(need_ids)} 类")

    # ---- L17 同主题三口径表：齐全 + 排序口径自洽 + 与极值自洽 ----
    # 🔴 reviewer 实测的旧 bug：披露把「极端档余量最大」的一对叫成「最接近的一对」。
    #    修法 = 报告里必须**同时**给三张口径表，且三张表互相自洽（不许互相矛盾）。
    closest_tbl = flip.get("same_theme_closest_by_gap") or []
    real_tbl = flip.get("same_theme_tightest_realistic") or []
    ext_tbl = flip.get("same_theme_tightest_extreme") or []
    chal_tbl = flip.get("same_theme_challengers") or []
    l17_problems: list[str] = []
    for nm, tbl in (("same_theme_closest_by_gap", closest_tbl),
                    ("same_theme_tightest_realistic", real_tbl),
                    ("same_theme_tightest_extreme", ext_tbl)):
        if not tbl:
            l17_problems.append(f"{nm} 缺失/为空")
    if closest_tbl:
        mags = [abs(float(r.get("final_gap"))) for r in closest_tbl
                if r.get("final_gap") is not None]
        if mags != sorted(mags):
            l17_problems.append(f"closest 未按 |final_gap| 升序：{mags}")
    if real_tbl:
        mrs = [r.get("margin_realistic") for r in real_tbl]
        vals = [float(v) for v in mrs if v is not None]
        if vals != sorted(vals, reverse=True):
            l17_problems.append(f"tightest_realistic 未按 margin_realistic 降序：{vals}")
        # 🔴 2026-11-10：旧写法 `mrs[-1] is not None and any(v is None for v in mrs[:-1])`
        #    **只在末位非 None 时才判** ⇒ 全表皆 None 时反而**不报** ⇒ 漏检。
        #    ⇒ 改成「**任何 None 都不许出现在非 None 行之前**」（全表皆 None 也不算违规：
        #    没有非 None 行 ⇒ 没有「None 排在非 None 之前」的情形）。
        _seen_none = False
        for _v in mrs:
            if _v is None:
                _seen_none = True
            elif _seen_none:
                l17_problems.append("tightest_realistic 的 None 没排在最后")
                break
    if ext_tbl:
        mes = [float(r.get("margin_extreme")) for r in ext_tbl if r.get("margin_extreme") is not None]
        if mes != sorted(mes, reverse=True):
            l17_problems.append(f"tightest_extreme 未按 margin_extreme 降序：{mes}")
    if ext_tbl and chal_tbl and ext_tbl != chal_tbl:
        l17_problems.append("tightest_extreme 与 same_theme_challengers 口径不一致（应同为极端档）")
    ms_r = flip.get("max_same_theme_margin_realistic")
    ms_e = flip.get("max_same_theme_margin_extreme")
    if real_tbl and ms_r is not None:
        got = real_tbl[0].get("margin_realistic")
        if got is None or abs(float(got) - float(ms_r)) > TOL_NUM:
            l17_problems.append(f"tightest_realistic[0].margin_realistic={got} ≠ 极值 {ms_r}")
    if ext_tbl and ms_e is not None:
        got = ext_tbl[0].get("margin_extreme")
        if got is None or abs(float(got) - float(ms_e)) > TOL_NUM:
            l17_problems.append(f"tightest_extreme[0].margin_extreme={got} ≠ 极值 {ms_e}")
    if closest_tbl and ms_r is not None:
        got = closest_tbl[0].get("margin_realistic")
        if got is not None and float(got) > float(ms_r) + TOL_NUM:
            l17_problems.append(f"closest[0].margin_realistic={got} > 全池极值 {ms_r}（自相矛盾）")
    if closest_tbl:
        c0 = closest_tbl[0]
        known = {(str(r.get("selected")), str(r.get("challenger")))
                 for r in (flip.get("same_theme_gaps") or [])}
        if (str(c0.get("selected")), str(c0.get("challenger"))) not in known:
            l17_problems.append("closest[0] 不在 same_theme_gaps 里（报告自相矛盾）")
    # 🔴 跨表交叉校验：口径①必须是 |final_gap| 最小的那一对、②③必须真的是余量最大的
    #    （用 same_theme_gaps 这张**另一张表**当参照 ⇒ 不靠表自己证明自己）
    gap_rows = flip.get("same_theme_gaps") or []
    if closest_tbl and gap_rows:
        m0 = abs(float(closest_tbl[0].get("final_gap")))
        gmin = min(abs(float(r.get("final_gap"))) for r in gap_rows
                   if r.get("final_gap") is not None)
        if m0 > gmin + TOL_NUM:
            l17_problems.append(
                f"closest[0].|final_gap|={m0} > same_theme_gaps 里最小的 {gmin}"
                f"（口径①点名的不是最接近的那一对）")
    if real_tbl and gap_rows:
        r0 = real_tbl[0].get("margin_realistic")
        rmax = max((float(r.get("margin_realistic")) for r in gap_rows
                    if r.get("margin_realistic") is not None), default=None)
        if r0 is not None and rmax is not None and float(r0) < rmax - TOL_NUM:
            l17_problems.append(
                f"tightest_realistic[0].margin_realistic={r0} < same_theme_gaps 里最大的 {rmax}")
    if ext_tbl and gap_rows:
        e0 = ext_tbl[0].get("margin_extreme")
        emax = max((float(r.get("margin_extreme")) for r in gap_rows
                    if r.get("margin_extreme") is not None), default=None)
        if e0 is not None and emax is not None and float(e0) < emax - TOL_NUM:
            l17_problems.append(
                f"tightest_extreme[0].margin_extreme={e0} < same_theme_gaps 里最大的 {emax}")
    if l17_problems:
        det17 = "；".join(l17_problems)
    elif closest_tbl and real_tbl and ext_tbl:
        det17 = (f"closest[0]={closest_tbl[0].get('selected')}↔{closest_tbl[0].get('challenger')}｜"
                 f"tightest_realistic[0]={real_tbl[0].get('selected')}↔{real_tbl[0].get('challenger')}｜"
                 f"tightest_extreme[0]={ext_tbl[0].get('selected')}↔{ext_tbl[0].get('challenger')}")
    else:
        det17 = "口径表不全"
    add("L17 同主题三口径表齐全且自洽（口径①|final_gap|最小 / ②现实档余量最大 / ③极端档余量最大）",
        not l17_problems, det17)

    # ---- L18 MD 必须点名「最终分差最近的一对」（口径①）并带全三个数字 ----
    # 报告里出现过的所有配对代码（用于在 MD 里认出「哪一行点了名」）
    known_codes: set[str] = set()
    for tbl in (flip.get("same_theme_gaps") or [], closest_tbl, real_tbl, ext_tbl, chal_tbl):
        for row in tbl:
            for fld in ("selected", "challenger"):
                if row.get(fld):
                    known_codes.add(str(row[fld]))
    c0 = closest_tbl[0] if closest_tbl else None
    sec_flip = md_section(md_text, "## 八之四、前视审计")
    sec_disc = md_section(md_text, "## 九、风险披露")
    if c0:
        ok_flip, det_flip = _caliber_first_line(sec_flip, c0, CALIBER_CLOSEST_MARK, known_codes)
        ok_disc, det_disc = _caliber_first_line(sec_disc, c0, CALIBER_CLOSEST_MARK, known_codes)
        add("L18 MD 披露（八之四 + 风险披露）第一条口径①必须点名**最终分差最近的一对**并带全 分差/阈值/余量",
            ok_flip and ok_disc,
            f"八之四：{det_flip}｜风险披露：{det_disc}")
    else:
        add("L18 MD 披露（八之四 + 风险披露）必须点名**最终分差最近的一对**并带全 分差/阈值/余量",
            False, "报告里没有 same_theme_closest_by_gap ⇒ 无法核对披露点名的是哪一对")

    # ---- L19 措辞闸门：极端档余量最大的一对**不许**被叫成「最接近 / 最紧」 ----
    ext0 = ext_tbl[0] if ext_tbl else (chal_tbl[0] if chal_tbl else None)
    close_codes = {str(c0.get("selected")), str(c0.get("challenger"))} if c0 else set()
    bad_lines: list[str] = []
    for ln in md_text.splitlines():
        if not any(w in ln for w in FORBIDDEN_CLOSE_WORDS):
            continue
        named = {c for c in known_codes if c in ln}
        if not named:
            bad_lines.append(f"未点名任何配对：{ln.strip()[:90]}")
        elif not (named & close_codes):
            bad_lines.append(f"点了名 {sorted(named)} 但它不是 |final_gap| 最小的那对：{ln.strip()[:90]}")
    add("L19 极端档余量最大的一对**不许**被称作「最接近 / 最紧」（口径必须分清）",
        not bad_lines,
        "；".join(bad_lines) if bad_lines else
        (f"极端档那一对 = {ext0.get('selected')}↔{ext0.get('challenger')}，未被误称"
         if ext0 else "报告里没有极端档口径表"))

    # ---- L20 asof=2026-11-03 的**实测金标**：|final_gap| 最小的一对必须是 512800 ↔ 512820 ----
    asof_str = str(inputs.get("asof") or "")
    if asof_str != GOLD_ASOF:
        add("L20 金标：|final_gap| 最小的一对 = 512800 ↔ 512820（分差/阈值/余量一致）",
            True,
            f"本条金标只覆盖 asof={GOLD_ASOF}（当前 asof={asof_str}）⇒ 未套用金标，"
            f"降级为 L17/L18 的口径自洽核对")
    elif c0 is None:
        add("L20 金标：|final_gap| 最小的一对 = 512800 ↔ 512820（分差/阈值/余量一致）",
            False, "报告里没有 same_theme_closest_by_gap ⇒ 金标无法核对")
    else:
        diffs: list[str] = []
        for fld in ("selected", "challenger"):
            if str(c0.get(fld)) != GOLD_CLOSEST[fld]:
                diffs.append(f"{fld}={c0.get(fld)} ≠ {GOLD_CLOSEST[fld]}")
        for fld in ("final_gap", "swap_threshold_realistic", "margin_realistic"):
            v = c0.get(fld)
            if v is None or abs(float(v) - float(GOLD_CLOSEST[fld])) > TOL_NUM:
                diffs.append(f"{fld}={v} ≠ {GOLD_CLOSEST[fld]}")
        add("L20 金标：|final_gap| 最小的一对 = 512800 ↔ 512820（分差/阈值/余量一致）",
            not diffs,
            "；".join(diffs) if diffs else
            f"{c0.get('selected')}↔{c0.get('challenger')}｜gap={c0.get('final_gap')}｜"
            f"thr_real={c0.get('swap_threshold_realistic')}｜margin_real={c0.get('margin_realistic')}")

    return out


def corrupt_md(md_text: str, closest: dict, ext: dict) -> str:
    """把披露**真的改坏**（rules.md §十四.68：负例必须改到被断言的对象上）。

    改两处（都改在**被断言的对象**＝MD 正文上，不是改在旁路副本上）：

    ① 「最终分差最近」的那一处点名 ⇒ 换成**极端档余量最大**的那一对及其数字
       （= 点名成错误的对）；
    ② 极端档那一对的说法 ⇒ 改成「最接近」
       （= 把极端档余量最大者叫成「最接近」，正是 reviewer 实测的旧披露 bug）。
    """
    out: list[str] = []
    for ln in md_text.splitlines(keepends=True):
        if ("极端档余量最大" in ln
                and str(ext.get("selected")) in ln
                and str(ext.get("challenger")) in ln):
            ln = ln.replace("极端档余量最大", "最接近的一对")
        if (CALIBER_CLOSEST_MARK in ln
                and str(closest.get("selected")) in ln
                and str(closest.get("challenger")) in ln):
            for src, dst in (
                (str(closest.get("selected")), str(ext.get("selected"))),
                (str(closest.get("challenger")), str(ext.get("challenger"))),
                (str(closest.get("final_gap")), str(ext.get("final_gap"))),
                (str(closest.get("swap_threshold_realistic")),
                 str(ext.get("swap_threshold_realistic"))),
                (str(closest.get("margin_realistic")), str(ext.get("margin_realistic"))),
            ):
                if src:
                    ln = ln.replace(src, dst)
            for fld in ("selected_name", "challenger_name"):
                src = str(closest.get(fld) or "")
                dst = str(ext.get(fld) or "")
                if src and dst:
                    ln = ln.replace(src, dst)
        out.append(ln)
    return "".join(out)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _find_snapshot(root: Path) -> Path | None:
    """根目录下**最新**一个「有 manifest **且**有报告」的快照目录（没有 ⇒ None）。

    🔴 与 `probe_snapshot_lookahead.find_snapshot` **同口径**：
    必须同时要 manifest 与报告 —— 只看 manifest 会把「最新的一次 dry-run」
    当成断言对象（dry-run 快照有 manifest、无报告）⇒ 明明有真快照却判「不适用」。
    """
    if not root.is_dir():
        return None
    candidates = [
        p for p in sorted(root.iterdir())
        if p.is_dir()
        and (p / "snapshot_manifest.json").is_file()
        and (p / "etf_analysis_report.json").is_file()
    ]
    return candidates[-1] if candidates else None


def main() -> int:
    ap = argparse.ArgumentParser(description="前视审计闸门（只读）")
    ap.add_argument("--report", default=str(RUN_ROOT / "etf_analysis_report.json"))
    ap.add_argument("--md", default=str(RUN_ROOT / "etf_analysis_report.md"))
    # 🔴 2026-11-09（正本 §三第 2 条 / 独立审计 P1-8）：断言对象必须能指向
    #    **本轮快照内的那一份报告**（快照制的意义就在这儿）。
    #    ⇒ 给了 `--snapshot-root` / `--snapshot` ⇒ 优先断言快照内那一份；
    #      找不到真快照 ⇒ **退出码 2「不适用」**（≠ 通过），
    #      **不许**悄悄回落成根目录那份还报「通过」。
    #    ⇒ 不给这两个参数 ⇒ 仍断言根目录那份（**基线一致性护栏**，保留不动）。
    ap.add_argument("--snapshot-root", default="",
                    help="快照根目录（给了就优先断言快照内那一份；无真快照 ⇒ rc2 不适用）")
    ap.add_argument("--snapshot", default="", help="直接指定快照目录")
    # 🔴 2026-11-10：**数据根目录**（个股分数 / 个股日线）显式化。
    #    缺省 = canonical ``RUN_ROOT``（基线路径逐字节不变）；
    #    快照口径下由本探针**自动**切到快照目录（快照自包含），
    #    本参数只给「子进程复跑同口径」用（例如 N10 MD 级真负例）。
    ap.add_argument("--data-root", default="",
                    help="个股分数/个股日线所在目录（缺省 = canonical RUN_ROOT）")
    args = ap.parse_args()

    report_path = Path(args.report)
    md_path = Path(args.md)
    #: **数据根目录**（个股分数 / 个股日线）：缺省 canonical；快照口径 ⇒ 快照目录
    data_root = RUN_ROOT
    if args.snapshot or args.snapshot_root:
        snap = (Path(args.snapshot) if args.snapshot
                else _find_snapshot(Path(args.snapshot_root)))
        if snap is None or not snap.is_dir():
            print(f"[不适用] 找不到真实快照（root={args.snapshot_root}）"
                  "⇒ **本阶段不适用**（≠ 通过）")
            return 2
        cand_json = snap / "etf_analysis_report.json"
        cand_md = snap / "etf_analysis_report.md"
        if not (cand_json.is_file() and cand_md.is_file()):
            print(f"[不适用] 快照 {snap} 里没有报告（json/md 缺一）"
                  "⇒ **本阶段不适用**（≠ 通过）")
            return 2
        report_path, md_path = cand_json, cand_md
        print(f"[快照口径] 断言对象 = 本轮快照内那一份：{snap}")
        # 🔴 2026-11-10：数据根目录**跟着断言对象一起切到快照**（快照自包含）。
        #    🔴 缺件 ⇒ 判「不适用」（rc2，≠ 通过），
        #       **不许**回落 canonical 再报「通过」（回落 = 断言对象与数据不同源 = 假绿）。
        missing = [n for n in DATA_ROOT_FILES if not (snap / n).is_file()]
        if missing:
            print(f"[不适用] 快照 {snap} 里缺 {'、'.join(missing)}"
                  " ⇒ **本阶段不适用**（≠ 通过）；"
                  "**不许**回落根目录 canonical 再报「通过」")
            return 2
        data_root = snap
        print(f"[快照口径] 数据根目录 = 同一快照（个股分数/个股日线自包含）：{data_root}")
    elif args.data_root:
        # 🔴 显式数据根目录（**只在不给快照参数时**生效 ⇒ 快照优先级更高）
        data_root = Path(args.data_root)
        missing = [n for n in DATA_ROOT_FILES if not (data_root / n).is_file()]
        if missing:
            print(f"[不适用] 数据根目录 {data_root} 里缺 {'、'.join(missing)}"
                  " ⇒ **本阶段不适用**（≠ 通过）")
            return 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    md_text = md_path.read_text(encoding="utf-8")

    asof = pd.Timestamp((report.get("meta") or {}).get("asof") or
                        ((report.get("lookahead_audit") or {}).get("inputs") or {}).get("asof"))
    indep = independent_ledger(asof, data_root=data_root)
    la = report.get("lookahead_audit") or {}
    codes = [str(r.get("code")) for r in ((la.get("e_bound") or {}).get("per_etf") or [])]
    indep["e_bounds"] = independent_e_bounds(asof, codes, data_root=data_root)

    results = evaluate(report, md_text, indep)
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print("=" * 78)
    print("前视审计闸门 · 独立复算 + 负例自检")
    print(f"信号日 asof = {asof.date()}｜独立重算决策前视 "
          f"{indep['n_decision_lookahead']} 项：{[i['name'] for i in indep['items']]}")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"       └─ {detail}")
    print("-" * 78)
    print(f"正向断言：{len(results) - n_fail}/{len(results)} PASS")

    # ---- 负例：证明这些断言真的会拦人（§十四.68：负例必须真的改到被断言的对象）----
    def _mutate(fn):
        bad = copy.deepcopy(report)
        fn(bad)
        return bad

    def _set_path(d: dict, path: list, value) -> None:
        cur = d
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value

    def _mutate_wsnap_detail(d: dict) -> None:
        """把 risk_disclosure 里 w_snapshot_bias 的 detail 改回「不可量化」（负例）。"""
        for item in d.get("risk_disclosure") or []:
            if isinstance(item, dict) and item.get("id") == "w_snapshot_bias":
                item["detail"] = "偏差不可量化"
                return
        raise AssertionError("risk_disclosure 里找不到 w_snapshot_bias（负例无法生效）")

    def _mutate_closest_to_extreme(d: dict) -> None:
        """把「口径①最终分差最近」的第一名改成**极端档余量最大**的那一对（负例）。"""
        fl = ((d.get("lookahead_audit") or {}).get("flip_test")) or {}
        ext = (fl.get("same_theme_tightest_extreme") or [])[0]
        tbl = fl.get("same_theme_closest_by_gap") or []
        if not ext or not tbl:
            raise AssertionError("报告里没有三口径表（负例无法生效）")
        tbl[0] = copy.deepcopy(ext)

    #: 🔴 2026-11-10：L3 的期望值改成**事实源派生**（W 快照日 > asof ⇒ 1，否则 0）
    #:    ⇒ 负例 N1 **不能**再写死「改成 0」（快照口径下期望本来就是 0 ⇒ 改了等于没改
    #:    ⇒ 负例空转）。⇒ 负例改成「改成 **≠ 期望值** 的另一个值」
    #:    ⇒ 基线口径（期望 1 ⇒ 改成 2）与快照口径（期望 0 ⇒ 改成 1）**都必然改坏**。
    _rep_inputs = ((report.get("lookahead_audit") or {}).get("inputs") or {})
    _expect_base = expected_lookahead_count(indep, str(_rep_inputs.get("asof") or ""))
    _n_la_bad = _expect_base + 1

    negs = [
        (f"N1 台账把决策前视条数改成 {_n_la_bad}（≠ 事实源派生的期望 {_expect_base}）"
         f" ⇒ L3/L5 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "inputs", "n_look_ahead"], _n_la_bad), "L3"),
        ("N2 台账把前视项换成 etf_prices.csv ⇒ L4 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "inputs", "blocking"],
                             [{"name": "etf_prices.csv", "effective_date": "2026-11-04"}]), "L4"),
        ("N3 把 W 快照标成可修（fixable=True）⇒ L8 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "w_snapshot", "fixable"], True), "L8"),
        ("N4 篡改某只 ETF 的 E 上界 ⇒ L10 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "e_bound", "per_etf", 0, "delta_e_extreme"],
                             0.9), "L10"),
        ("N5 把同主题余量改成 +0.5 ⇒ L12/L14 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "flip_test",
                                 "max_same_theme_margin_realistic"], 0.5), "L12"),
        ("N6 把极端档余量改成 +0.5 ⇒ L12 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "flip_test",
                                 "max_same_theme_margin_extreme"], 0.5), "L12"),
        ("N7 把兜底改成「用过未来成交额且通过过滤」⇒ L7 必须 FAIL",
         lambda d: _set_path(d, ["lookahead_audit", "inputs", "liquidity_fallback"],
                             {"n_need_fallback": 3, "n_need_fallback_pass_liq": 1,
                              "inert": False, "note": "负例"}), "L7"),
        ("N8 把披露改回「不可量化」⇒ L15 必须 FAIL",
         lambda d: _mutate_wsnap_detail(d), "L15"),
        ("N9 把「口径①最终分差最近」的第一名改成极端档余量最大的那一对 ⇒ L17/L18/L20 必须 FAIL",
         lambda d: _mutate_closest_to_extreme(d), ("L17", "L18", "L20")),
    ]
    neg_fail = 0
    print("-" * 78)
    print("负例自检（每个负例都必须让对应断言 FAIL）：")
    for desc, fn, expect_prefix in negs:
        bad = _mutate(fn)
        res = evaluate(bad, md_text, indep)
        prefixes = (expect_prefix,) if isinstance(expect_prefix, str) else tuple(expect_prefix)
        hit = [name for name, ok, _ in res if not ok and name.startswith(prefixes)]
        ok = bool(hit)
        if not ok:
            neg_fail += 1
        print(f"[{'PASS' if ok else 'FAIL'}] 负例：{desc}")
        print(f"       └─ 实际 FAIL 的断言：{hit or '（无 ⇒ 该负例无效！）'}")
    n_checks = len(results)
    n_negs = len(negs)
    n_negs_ok = len(negs) - neg_fail

    # ---- N10 真负例（**MD 级**）：复制 md 到持久盘临时目录 + 真的改坏 ⇒ 探针必须非零退出 ----
    # 🔴 §十四.68：负例必须**真的改到被断言的对象**上 ⇒ 这里改的是 **MD 正文**（披露本体），
    #    不是改一个旁路副本；改完**用子进程真跑一遍本探针**，必须非零退出。
    md_neg_pass = True
    md_neg_detail = ""
    if os.environ.get(ENV_SKIP_MD_NEG):
        print(f"[SKIP] 负例：N10 MD 级真负例（子进程守卫 {ENV_SKIP_MD_NEG} 已设置，避免递归）")
        md_neg_detail = "子进程守卫已设置 ⇒ 本条未在本进程执行"
    else:
        flip0 = ((report.get("lookahead_audit") or {}).get("flip_test")) or {}
        closest0 = (flip0.get("same_theme_closest_by_gap") or [None])[0]
        ext0 = (flip0.get("same_theme_tightest_extreme") or [None])[0]
        if not closest0 or not ext0:
            md_neg_pass = False
            md_neg_detail = "报告里没有三口径表 ⇒ 负例无法构造"
            print("[FAIL] 负例：N10 MD 级真负例（报告缺三口径表 ⇒ 构造不出来）")
        else:
            NEG_TMP_PARENT.mkdir(parents=True, exist_ok=True)
            tmpdir = Path(tempfile.mkdtemp(prefix="_probe_lookahead_neg_",
                                           dir=str(NEG_TMP_PARENT)))
            try:
                bad_md = tmpdir / "etf_analysis_report_broken.md"
                bad_md.write_text(corrupt_md(md_text, closest0, ext0), encoding="utf-8")
                # (a) 进程内：改坏后 L18/L19 必须 FAIL（证明断言真的在被断言对象上）
                res_bad = evaluate(report, bad_md.read_text(encoding="utf-8"), indep)
                hit = [n for n, ok, _ in res_bad if not ok and n.startswith(("L18", "L19"))]
                inproc_ok = bool(hit)
                # (b) 子进程：真跑一遍本探针 ⇒ 必须非零退出
                env = dict(os.environ)
                env[ENV_SKIP_MD_NEG] = "1"
                # 🔴 子进程必须与本进程**同源**：断言对象 = 本次的 report_path
                #    （快照口径下 = 快照内那一份，**不是** canonical），
                #    数据根目录 = 同一个 data_root。
                cmd = [sys.executable, str(Path(__file__).resolve()),
                       "--report", str(report_path), "--md", str(bad_md)]
                if Path(data_root) != RUN_ROOT:
                    cmd += ["--data-root", str(data_root)]
                proc = subprocess.run(
                    cmd,
                    capture_output=True, text=True, env=env,
                )
                sub_ok = proc.returncode != 0
                md_neg_pass = inproc_ok and sub_ok
                md_neg_detail = (
                    f"进程内 FAIL 的断言={hit}｜子进程退出码={proc.returncode}"
                    f"（0=通过 ⇒ 断言没拦住 ⇒ 负例无效）｜临时目录={tmpdir}"
                )
                print(f"[{'PASS' if md_neg_pass else 'FAIL'}] 负例：N10 把披露改坏"
                      f"（点名成错误的对 + 把极端档余量最大者叫「最接近」）⇒ 探针必须 FAIL")
                print(f"       └─ {md_neg_detail}")
                if not sub_ok:
                    print("       └─ 子进程输出尾部：" + proc.stdout.strip()[-800:])
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                print(f"       └─ 临时目录已清理：{tmpdir}（存在={tmpdir.exists()}）")
    n_negs += 1
    if md_neg_pass:
        n_negs_ok += 1
    print("-" * 78)
    print(f"合计：正向 {n_checks - n_fail}/{n_checks} PASS｜负例 "
          f"{n_negs_ok}/{n_negs} 有效")
    total = n_checks + n_negs
    passed = (n_checks - n_fail) + n_negs_ok
    print(f"闸门总计：{passed}/{total} PASS")
    if n_fail or neg_fail or not md_neg_pass:
        print("🔴 前视审计闸门 FAIL ⇒ 产物不可交付")
        return 1
    print("✅ 前视审计闸门全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
