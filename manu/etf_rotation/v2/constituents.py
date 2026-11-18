"""🔴 V2 成分股 / 权重口径（对齐②）。

**这是 V2，与 V1 无关**：本模块**只读** ``etf_holdings.csv``，
不 import、不改写 V1 的 ``engine/weights.py`` / ``qlib_scores.py``
（V1 冻结为 baseline）。V1 的口径在这里用**本地等价实现**复刻（见下）。

🔴 **本模块存在的唯一理由 = 把结构性前视说清楚（不许用沉默掩盖）**：

  * akshare ``fund_etf_holdings_tx``（腾讯实际持仓）与 ``fund_etf_pcf``
    （每日申购赎回清单）**都没有历史 PCF 接口**（实测：``fund_etf_pcf`` 只接受
    ``symbol``，无日期参数）⇒ **历史成分股变更不可观测**；
  * 本地 ``etf_holdings.csv`` 只有 **一个**快照日（2026-11-04，见
    ``config_v2.HOLDINGS_SNAPSHOT_DATE``）⇒ 回测只能用 2026 的权重**倒推历史**；
  * ⇒ ``lookahead_bias_bound`` 只给**代理量**，``is_true_history`` **恒为 False**；
    **禁止编造历史快照**（编出来就是铁律六「禁编数字」的刑事犯）。

口径（与 V1 ``engine/weights.py`` **逐位对齐**，V2 不允许漂移）：

  * ``coverage`` = **已打分成分股权重 / 该 ETF 总权重**（V1 同式）
    ⇒ 权重口径的覆盖率，不是「成分股只数占比」；
  * 成分股权重取自 ``weight_pct``（**市值占比 %**），**只保留 > 0 的行**；
  * 股票代码统一成 **qlib instrument**（``SZ300857``）后才与打分池比对
    （V1 的 ``to_qlib_instrument`` 逻辑在此**本地复刻**，因为 V2 禁止 import V1）；
  * 🔴 覆盖率下限 ``min_coverage = 0.5`` 仍以 ``config_v2.MIN_COVERAGE`` 为准
    （本模块不判定入池，只算覆盖率）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:  # 扁平导入（v2 目录进 sys.path，与 labels.py / splits.py 同口径）
    import config_v2 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v2 as C


#: 成分股表必备列（少一列 ⇒ 直接报错，不许「少读一列照样跑」）
REQUIRED_COLUMNS = ("etf_code", "stock_code", "weight_pct", "source", "fetch_date")


@dataclass(frozen=True)
class ConstituentsSnapshot:
    """一份成分股 / 权重快照（**只读**）。"""

    snapshot_date: str
    #: etf_code -> {stock_code: weight_pct}（键是**表里的原始成分代码**）
    weights: dict[str, dict[str, float]]
    #: source(tx/pcf) -> 只数（成分行数）
    source_counts: dict[str, int]


# ----------------------------------------------------------------- 代码归一
def _to_qlib_instrument(code: str) -> str | None:
    """把成分股代码转成 qlib instrument（``SZ300857``）；认不出 ⇒ ``None``。

    🔴 **与 V1 ``engine/weights.py::to_qlib_instrument`` 同口径的本地复刻**
    （V2 禁止 import V1 ⇒ 只能复刻；口径不许漂移）：

      * ``"600000.SH"`` → ``"SH600000"``；``"300857.SZ"`` → ``"SZ300857"``；
      * ``"09988.BJ"`` → ``"BJ09988"``（港股走这条 ⇒ 在 qlib 里查无此票 ⇒ 永不被打分）；
      * ``"SHAU"`` / ``"Au9999"``（黄金 ETF 的商品行）⇒ **None**
        ⇒ 它们**永不计入已打分权重**，但**照旧计入总权重**（与 V1 一致）。
    """
    if code is None:
        return None
    code = str(code).strip().upper()
    if not code:
        return None
    if "." in code:
        head, _, tail = code.partition(".")
        if head.isalpha():          # 已经是 qlib 形式
            return code
        if tail.isalpha():
            if tail == "BJ":
                return f"BJ{head}"
            return f"{tail}{head}"
        code = head
    code = code.zfill(6)
    if not code.isdigit():
        return None
    if code.startswith(("60", "68", "51", "52", "56", "58", "9")):
        return f"SH{code}"
    if code.startswith(("0", "3")):
        return f"SZ{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return None


def _etf_key(code: str) -> str:
    """ETF 代码归一（6 位数字字符串，与 V1 的 ``zfill(6)`` 同口径）。"""
    text = str(code).strip()
    if text.isdigit():
        return text.zfill(6)
    return text


# ----------------------------------------------------------------- 读表
def _read_holdings_frame(path: str | Path | None = None) -> pd.DataFrame:
    """读成分股表（**只读**）⇒ 原始 DataFrame；表空 / 缺列 ⇒ 直接报错。"""
    p = Path(path) if path is not None else Path(C.HOLDINGS_CSV)
    if not p.exists():
        raise FileNotFoundError(f"🔴 成分股表不存在：{p}")
    try:
        df = pd.read_csv(
            p,
            dtype={"etf_code": str, "stock_code": str, "source": str, "fetch_date": str},
        )
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"🔴 成分股表是空文件：{p}") from exc
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"🔴 成分股表缺列 {missing}（现有列：{list(df.columns)}）")
    if df.empty:
        raise ValueError(f"🔴 成分股表没有任何数据行：{p}")
    return df


def _positive_weight_rows(df: pd.DataFrame) -> pd.DataFrame:
    """只保留 ``weight_pct > 0`` 的行（权重 ≤ 0 / 缺失 ⇒ 剔掉）。"""
    weights = pd.to_numeric(df["weight_pct"], errors="coerce")
    return df.loc[weights > 0].copy()


def read_holdings_frame(path: str | Path | None = None) -> pd.DataFrame:
    """成分股表的**原始长表**（公开只读入口）。

    🔴 为什么需要它：``load_holdings()`` 返回的是 ``ConstituentsSnapshot``（**已归一**的
    ``{ETF: {成分: 权重}}``），**不保留** ``etf_name`` 等明细列；
    而 ETF 层的 ``build_weight_map(holdings)`` 要的是**原始长表**（它自己负责
    正权重过滤与 qlib instrument 归一）⇒ 调用方需要这个入口才能把两段拼起来。

    🔴 **只读**：本函数不改文件、不做归一化（归一化在调用方做，口径才唯一）。
    """
    return _read_holdings_frame(path)


def snapshot_frame(snapshot: "ConstituentsSnapshot",
                   path: str | Path | None = None) -> pd.DataFrame:
    """把 ``ConstituentsSnapshot`` 对应的**那一份**原始长表取出来（按快照日过滤）。

    🔴 为什么要按日过滤：成分股表是 **append-only 累积**的（见 ``holdings_archive.py``）
    ⇒ 同一张表里可能有多天的 ``fetch_date``；
    **回测只能用「那一天真正可得的快照」** ⇒ 不许把多天混进同一次计算。
    """
    df = read_holdings_frame(path)
    dates = df["fetch_date"].astype(str).str.strip()
    return df.loc[dates == str(snapshot.snapshot_date)].copy()

def available_snapshot_dates(path: str | Path | None = None) -> list[str]:
    """读成分股表的 ``fetch_date`` 去重排序（**升序**）。

    🔴 真实数据当前**只有 1 个**快照日（2026-11-04）⇒ 返回 ``["2026-11-04"]``；
    这是「历史成分不可观测」的**事实证据**，不是解析错误。
    """
    df = _read_holdings_frame(path)
    dates = sorted({str(v).strip() for v in df["fetch_date"] if str(v).strip()})
    if not dates:
        raise ValueError("🔴 成分股表里没有任何 fetch_date ⇒ 快照日不可判定")
    return dates


def load_holdings(path: str | Path | None = None,
                  snapshot_date: str | None = None) -> ConstituentsSnapshot:
    """加载成分股 / 权重。

    参数
    ----
    path : 成分股 CSV（默认 ``config_v2.HOLDINGS_CSV``，**只读**）
    snapshot_date : 指定快照日；``None`` ⇒ 取**最新**那个快照日

    返回
    ----
    :class:`ConstituentsSnapshot`

    🔴 **一条都读不到 ⇒ 直接 raise**（不许返回空 dict 蒙混过去：
    空快照会让覆盖率变成 0/0，悄悄把整池 ETF 打成「无成分」）。
    """
    df = _read_holdings_frame(path)
    df = _positive_weight_rows(df)
    if df.empty:
        raise ValueError("🔴 成分股表里没有 ``weight_pct > 0`` 的行 ⇒ 快照不可用")
    df["fetch_date"] = df["fetch_date"].astype(str).str.strip()
    df["etf_code"] = df["etf_code"].map(_etf_key)
    df["stock_code"] = df["stock_code"].astype(str).str.strip()
    dates = sorted(set(df["fetch_date"]))
    if snapshot_date is None:
        chosen = dates[-1]          # 最新快照
    else:
        chosen = str(snapshot_date).strip()
        if chosen not in dates:
            raise ValueError(f"🔴 快照日 {chosen} 不存在（可用：{dates}）")
    sub = df.loc[df["fetch_date"] == chosen]
    if sub.empty:
        raise ValueError(f"🔴 快照日 {chosen} 没有任何成分行")
    weights: dict[str, dict[str, float]] = {}
    for etf_code, grp in sub.groupby("etf_code", sort=True):
        # 同 ETF 同股票多行 ⇒ 权重求和（不同来源重复持有时不能重复计权重）
        agg = grp.groupby("stock_code", sort=True)["weight_pct"].sum()
        weights[str(etf_code)] = {str(code): float(weight) for code, weight in agg.items()}
    source_counts = {
        str(source): int(n) for source, n in sub["source"].value_counts().items()
    }
    return ConstituentsSnapshot(
        snapshot_date=chosen,
        weights=weights,
        source_counts=source_counts,
    )


# ----------------------------------------------------------------- 覆盖率
def coverage(etf_code: str, snapshot: ConstituentsSnapshot, scored_codes: set[str]) -> float:
    """已打分权重 / 总权重（**V1 同口径**，阈值 ``config_v2.MIN_COVERAGE`` = 0.5）。

    * ``scored_codes``：打分池里的 qlib instrument（``SZ300857``），
      也接受表里的**原始代码**（``300857.SZ``）⇒ 两种写法都能对上；
    * 大小写不敏感（qlib ``instruments/all.txt`` 用大写 ``SZ300857``，
      特征目录用小写 ``sz300857`` ⇒ 不做归一会「明明打过分却算成没覆盖」）；
    * ETF 不在快照里 ⇒ 返回 ``0.0``（= 无成分可穿透 ⇒ 不入池，与 V1 同义）；
    * 总权重 ≤ 0 ⇒ 返回 ``0.0``。
    """
    etf = _etf_key(etf_code)
    holdings = snapshot.weights.get(etf)
    if not holdings:
        return 0.0
    total = float(sum(holdings.values()))
    if total <= 0:
        return 0.0
    scored_norm = {str(code).strip().upper() for code in scored_codes}
    if not scored_norm:
        return 0.0
    scored_weight = 0.0
    for code, weight in holdings.items():
        if code.strip().upper() in scored_norm:
            scored_weight += float(weight)
            continue
        instrument = _to_qlib_instrument(code)
        if instrument is not None and instrument.upper() in scored_norm:
            scored_weight += float(weight)
    return scored_weight / total


# ----------------------------------------------------------------- 前视偏差代理量
def lookahead_bias_bound(snapshot: ConstituentsSnapshot, n_candidates: int = 5) -> dict:
    """量化「用 2026 快照倒推历史」的偏差**代理上界**（口径如实写进返回值）。

    返回
    ----
    dict：``snapshot_date``、``n_etfs``、``max_weight_pct``、
    ``top_weight_concentration``、``proxy_note``、``is_true_history``。

    🔴 **``is_true_history`` 恒为 ``False``**：本地只有**一个**快照日
    （2026-11-04），akshare 两个接口都没有历史 PCF ⇒ 历史成分变更**不可观测**
    ⇒ 这里的数**是代理量，不是真实历史误差**，禁止解读成「已回溯真实历史成分」。

    代理口径：
      * ``max_weight_pct`` = 快照内**单只成分股**的最大权重（%）
        ⇒ 「单只成分股口径漂移」对 ETF 穿透分的最大影响幅度；
      * ``top_weight_concentration`` = 逐 ETF「权重前 ``n_candidates`` 大成分股」
        占该 ETF 总权重之比，再取**全体 ETF 的最大值**
        ⇒ 成分集中度上界（集中度越高 ⇒ 用 2026 权重倒推历史越不可信）。
    """
    n = max(int(n_candidates), 1)
    max_weight = 0.0
    concentration = 0.0
    for holdings in snapshot.weights.values():
        if not holdings:
            continue
        total = float(sum(holdings.values()))
        max_weight = max(max_weight, float(max(holdings.values())))
        if total <= 0:
            continue
        top = sorted((float(w) for w in holdings.values()), reverse=True)[:n]
        concentration = max(concentration, sum(top) / total)
    return {
        "snapshot_date": snapshot.snapshot_date,
        "n_etfs": len(snapshot.weights),
        "max_weight_pct": float(max_weight),
        "top_weight_concentration": float(concentration),
        "proxy_note": (
            f"🔴 代理口径（非真实历史）：本地只有 **1 个**成分快照日"
            f"（{snapshot.snapshot_date}），akshare `fund_etf_holdings_tx` / "
            f"`fund_etf_pcf` 均无历史 PCF 接口 ⇒ 历史成分变更**不可观测**。"
            f"max_weight_pct = 单只成分股最大权重（%）；"
            f"top_weight_concentration = 前 {n} 大成分股权重占比在全体 ETF 上的最大值。"
            f"**is_true_history 恒为 False** ⇒ 不得解读为「已回溯真实历史成分」。"
        ),
        "is_true_history": False,
    }


def caliber_note() -> str:
    """成分股口径披露（进报告用）。

    🔴 文本由 ``config_v2.CONSTITUENTS_CALIBER_NOTE`` **单一持有**
    （本模块只转述 ⇒ 口径文案不会在两个文件里各写一份而漂移）。
    """
    return str(C.CONSTITUENTS_CALIBER_NOTE)
