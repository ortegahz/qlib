"""🔴 V3 训练池（对齐②）+ **时变**规模档（对齐③·规模侧）。

**为什么单独一个文件**：V3 的「训练池」口径与 V2 **完全不同**（V2 = csi300），
所以 V3 必须有**自己的**训练池 owner ⇒ 训练池 / ΣW / 规模档三件事都收在这里。

口径（用户 2026-11-26 目标文档「对齐②」）

  * 训练池 = **候选 ETF 的成分股并集**（口径名 ``etf_constituent_union``）
    ⇒ 下游 ``E = W × S`` 里的 ``W``（ETF 成分权重）只对 ETF 成分股有意义；
  * 每个成分股带 **ΣW**（该股在全部候选 ETF 里的权重和）⇒ 样本加权口径
    （🔴 默认 ``config_v3.SAMPLE_WEIGHT_MODE = "sum_w"`` ⇒ 权重 = max(ΣW, 0)，
    **直接按 ΣW 加权**、不开方 ⇒ 与「按个股在 ETF 池中的总权重 ΣW 做样本加权」字面一致）；
  * 🔴 **规模档是时变的**：qlib 的 ``instruments/csi*.txt`` 每行带**日期区间**
    （``SH600000\t2005-04-08\t2026-11-20``）⇒ 同一只票在不同年份可能换档
    ⇒ 规模中性化必须用**区间判定**，不许用「今天的档位」倒推历史。

🔴 硬约束

  * **fail-closed**：输入文件缺失 / 池太小 ⇒ 直接 raise，不许静默降级；
  * **代码口径与 V2 逐位一致**：:func:`to_qlib_instrument` 是 V2
    ``v2/constituents.py::_to_qlib_instrument`` 的**本地等价实现**
    （V3 **不 import** V2；守卫测试里对拍）；
  * **import 期零 IO**：所有路径只作参数默认值，模块加载时不读盘、不 import qlib。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

try:  # 扁平导入（v3 目录进 sys.path，与 v2 同口径）
    import config_v3 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v3 as C


#: 候选池代码列的候选列名（🔴 容忍列名差异 ⇒ 依次尝试）
ETF_CODE_COLUMNS: tuple[str, ...] = ("etf_code", "code", "fund_code", "symbol")
#: 候选池标志列（🔴 与每日链 `daily/fetch_data.py::_pool_codes` 同口径：先 fetch_candidate）
UNIVERSE_FLAG_COLUMNS: tuple[str, ...] = ("fetch_candidate", "in_universe")
#: 🔴 规模档取值（**数字越小 = 档位越严** ⇒ 同时命中多档时取最严）
TIER_CSI300 = 0
TIER_CSI500 = 1
TIER_CSI1000 = 2
TIER_OTHER = 3
#: 规模档名 → 哑变量列名（🔴 drop 掉 tier3 基准档 ⇒ 只剩 3 列）
TIER_DUMMY_COLUMNS: tuple[str, ...] = ("tier_csi300", "tier_csi500", "tier_csi1000")
#: 规模档名 → 档位取值（顺序 = 严格程度：csi300 最严）
TIER_FILES_ORDER: tuple[str, ...] = ("csi300", "csi500", "csi1000")
#: 候选池默认路径（与 `config_v3` 同源；**只作参数默认值**）
ETF_UNIVERSE_CSV = C.ETF_UNIVERSE_CSV
#: 成分股表默认路径（同上）
HOLDINGS_CSV = C.HOLDINGS_CSV


# ----------------------------------------------------------------- 小工具
def _zfill6(code: str) -> str:
    """ETF 代码归一（纯数字 ⇒ 补齐 6 位；其余原样保留）。"""
    text = str(code).strip()
    if text.isdigit():
        return text.zfill(6)
    return text


def _pick_column(columns: pd.Index, candidates: tuple[str, ...]) -> str | None:
    """在候选列名里挑第一个存在的（🔴 容忍列名差异）。"""
    for name in candidates:
        if name in columns:
            return name
    return None


# ----------------------------------------------------------------- 读表
def load_etf_universe(path: str | Path = ETF_UNIVERSE_CSV) -> list[str]:
    """读候选 ETF 池 ⇒ ETF 代码列表（**升序、去重、字符串**）。

    🔴 **列名容忍**：依次尝试 ``etf_code`` / ``code`` / ``fund_code`` / ``symbol``；
    🔴 **文件不存在 ⇒ raise FileNotFoundError**（fail-closed，不许静默返回空列表）；
    🔴 **候选池口径与每日链同源**（``daily/fetch_data.py::_pool_codes``）：
       有 ``fetch_candidate`` 列就用它，没有就退到 ``in_universe``，都没有就用全表
       ⇒ 口径不漂移（本地实测：``etf_universe.csv`` 的代码列叫 ``code``）。

    返回：``["158041", "510300", ...]``（6 位数字字符串）。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"🔴 候选 ETF 池不存在：{p}")
    df = pd.read_csv(p, dtype=str)
    column = _pick_column(df.columns, ETF_CODE_COLUMNS)
    if column is None:
        raise ValueError(
            f"🔴 候选 ETF 池缺少代码列（候选名 {ETF_CODE_COLUMNS}）：{p}"
        )
    series = df[column].astype("string").str.strip()
    series = series[series.notna() & (series != "")]
    for flag in UNIVERSE_FLAG_COLUMNS:
        if flag in df.columns:
            mask = (
                df[flag].astype("string").str.strip().str.lower().isin(("true", "1", "yes"))
            )
            sub = series[mask.reindex(series.index, fill_value=False)]
            if len(sub) > 0:
                series = sub
                break
    codes = sorted({_zfill6(code) for code in series.tolist()})
    if not codes:
        raise ValueError(f"🔴 候选 ETF 池读不出任何代码：{p}")
    return codes


def load_holdings(path: str | Path = HOLDINGS_CSV) -> pd.DataFrame:
    """读成分股表 ⇒ DataFrame[etf_code, stock_code, weight_pct]（**weight_pct > 0 才留**）。

    🔴 **文件不存在 ⇒ raise FileNotFoundError**（fail-closed）；
    🔴 **负权重 / 零权重不是持仓** ⇒ 直接剔除（与 V2 同口径）；
    🔴 ``etf_code`` 统一补齐 6 位（与候选池对齐）。

    返回：列 = ``[etf_code, stock_code, weight_pct]``，行序保持原表顺序。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"🔴 成分股表不存在：{p}")
    df = pd.read_csv(p, dtype={"etf_code": str, "stock_code": str})
    missing = [c for c in ("etf_code", "stock_code", "weight_pct") if c not in df.columns]
    if missing:
        raise ValueError(f"🔴 成分股表缺列 {missing}：{p}")
    out = df.loc[:, ["etf_code", "stock_code", "weight_pct"]].copy()
    out["etf_code"] = out["etf_code"].astype("string").str.strip()
    out["stock_code"] = out["stock_code"].astype("string").str.strip()
    out["weight_pct"] = pd.to_numeric(out["weight_pct"], errors="coerce")
    keep = (
        out["weight_pct"].notna()
        & (out["weight_pct"] > 0)
        & out["etf_code"].notna()
        & (out["etf_code"] != "")
        & out["stock_code"].notna()
        & (out["stock_code"] != "")
    )
    out = out.loc[keep].reset_index(drop=True)
    out["etf_code"] = out["etf_code"].map(_zfill6)
    if out.empty:
        raise ValueError(f"🔴 成分股表里没有正权重的持仓行：{p}")
    return out


# ----------------------------------------------------------------- 代码归一
def to_qlib_instrument(code: str) -> str | None:
    """把成分股代码转成 qlib instrument（``SZ300857``）；认不出 ⇒ ``None``。

    🔴 **与 V2 ``v2/constituents.py::_to_qlib_instrument`` 逐位一致**
    （本地等价实现；V3 **不 import** V2，守卫测试里对拍）：

      * ``"300857.SZ"`` → ``"SZ300857"``；``"600000.SH"`` → ``"SH600000"``；
      * ``"09988.BJ"`` → ``"BJ09988"``（港股 ⇒ qlib 里查无此票 ⇒ 永不打分）；
      * ``"SHAU"`` / ``"Au9999"``（黄金 ETF 的商品行）⇒ ``None``
        ⇒ 它们**永不计入已打分权重**（与 V1/V2 一致）；
      * **无后缀的 6 位数字**（``"600000"``）⇒ 按前缀规则补出市场前缀
        （``"SH600000"``）——🔴 这与 V2 **完全一致**（别按「无后缀 ⇒ None」的口头
        说法改：那样就和 V2 的穿透比对不上 ⇒ 整个并集池口径漂移）。
    """
    if code is None:
        return None
    text = str(code).strip().upper()
    if not text:
        return None
    if "." in text:
        head, _, tail = text.partition(".")
        if head.isalpha():  # 已经是 qlib 形式
            return text
        if tail.isalpha():
            if tail == "BJ":
                return f"BJ{head}"
            return f"{tail}{head}"
        text = head
    text = text.zfill(6)
    if not text.isdigit():
        return None
    if text.startswith(("60", "68", "51", "52", "56", "58", "9")):
        return f"SH{text}"
    if text.startswith(("0", "3")):
        return f"SZ{text}"
    if text.startswith(("4", "8")):
        return f"BJ{text}"
    return None


def _instrument_or_blank(code: str) -> str:
    """qlib instrument；转换失败 ⇒ **空串**（原 code 仍保留在 ``stock_code`` 列）。"""
    instrument = to_qlib_instrument(code)
    return instrument if instrument is not None else ""


# ----------------------------------------------------------------- 并集 / ΣW
def build_etf_constituent_union(
    etf_codes: list[str],
    holdings: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """候选 ETF 的成分股**并集** ⇒ ``(union_df, sum_w)``。

    * ``union_df``：列 = ``[etf_code, stock_code, instrument, weight_pct]``
      （``instrument`` 用 qlib 口径；转换失败 ⇒ 保留原 code 且 ``instrument`` 置空）；
    * ``sum_w``：``{qlib_instrument: ΣW}``（该股在**所有候选 ETF** 里的权重和）
      ⇒ 样本加权用（``config_v3.SAMPLE_WEIGHT_MODE``）；
    * 🔴 **同一 (ETF, 股票) 多行 ⇒ 权重求和**（不同来源重复持有不能重复计权重，
      与 V2 同口径）；
    * 🔴 **``etf_codes`` 为空 ⇒ 用 holdings 里全部 ETF**，
      并在 ``union_df.attrs["used_fallback"]`` 标注（fail-closed 的反向：
      这里不是错，但**必须留痕**）。
    """
    if holdings is None or holdings.empty:
        raise ValueError("🔴 成分股表为空 ⇒ 无法构造并集")
    missing = [c for c in ("etf_code", "stock_code", "weight_pct") if c not in holdings.columns]
    if missing:
        raise ValueError(f"🔴 成分股表缺列 {missing}（构造并集失败）")
    frame = holdings.loc[:, ["etf_code", "stock_code", "weight_pct"]].copy()
    frame["etf_code"] = frame["etf_code"].astype(str).map(_zfill6)
    if etf_codes:
        wanted = {_zfill6(code) for code in etf_codes}
        sub = frame[frame["etf_code"].isin(wanted)]
        used_fallback = False
    else:
        sub = frame
        used_fallback = True
    if sub.empty:
        raise ValueError("🔴 候选 ETF 在成分股表里没有任何持仓 ⇒ 并集为空")
    agg = sub.groupby(["etf_code", "stock_code"], sort=True, as_index=False)["weight_pct"].sum()
    agg["instrument"] = [_instrument_or_blank(code) for code in agg["stock_code"].tolist()]
    union_df = agg.loc[:, ["etf_code", "stock_code", "instrument", "weight_pct"]].reset_index(
        drop=True
    )
    convertible = union_df["instrument"] != ""
    sum_w: dict[str, float] = {}
    if bool(convertible.any()):
        grouped = union_df.loc[convertible].groupby("instrument", sort=True)["weight_pct"].sum()
        sum_w = {str(key): float(value) for key, value in grouped.items()}
    union_df.attrs["used_fallback"] = used_fallback
    union_df.attrs["n_etfs"] = int(union_df["etf_code"].nunique())
    union_df.attrs["n_stocks"] = int(
        union_df.loc[convertible, "instrument"].nunique() if bool(convertible.any()) else 0
    )
    return union_df, sum_w


def build_universe(
    etf_codes: list[str],
    holdings: pd.DataFrame,
    mode: str | None = None,
    pit_dates: list[str] | None = None,
    provider_uri: str | None = None,
) -> dict:
    """构造 V3 训练池 ⇒ 口径字典。

    * ``mode='etf_constituent_union'``（``config_v3.UNIVERSE_MODE_FULL_UNION``）
      ⇒ 候选 ETF 成分股**全并集** + ΣW（留档对照口径）；
    * ``mode='etf_constituent_large'``（默认，``config_v3.UNIVERSE_MODE``）
      ⇒ **V3 主口径**：ETF 并集 ∩ 中证 800 ∪ 中证 1000（大中盘收敛版）；
    * ``provider_uri`` ⇒ PIT 面板日期轴的**provider 优先**日历（与训练/打分侧同源）；
    * ``mode='etf_constituent_large_pit'``（``config_v3.UNIVERSE_MODE_PIT``）
      ⇒ **逐日 PIT 口径**（新口径，🔴 **默认关闭**）：候选池与默认口径**同构**
        （同一批静态候选 ＋ 同一份 ΣW）⇒ **额外**多带一个**逐日成员面板**
        （键 = ``config_v3.PIT_MEMBERSHIP_KEY``）⇒ 训练样本按 **(日期, 标的)** 掩码；
      🔴 **打分池不受影响**（打分侧保持 ETF 全并集口径，不按日筛）；
    * ``mode='csi800'``（``config_v3.UNIVERSE_MODE_FALLBACK``）⇒ 解析 qlib
      ``instruments/csi800.txt``（**对照口径**，无 ΣW ⇒ 样本权重退化为均匀）；
    * 🔴 **``n_stocks < config_v3.MIN_UNIVERSE_SIZE`` ⇒ raise**（fail-closed）。

    返回 dict：``mode`` / ``instruments`` / ``sum_w`` / ``n_stocks`` /
    ``etf_codes`` / ``used_fallback`` / ``holdings_snapshot_date``
    （PIT 口径**额外**多一个 ``pit_membership`` 面板键 ⇒ 其余键与默认口径同构）。
    """
    chosen = str(mode or C.UNIVERSE_MODE)
    if chosen in (C.UNIVERSE_MODE, C.UNIVERSE_MODE_PIT):
        # 🔴 **V3 主口径** = UNIVERSE_MODE_LARGE（大中盘收敛版）
        #    （PIT 口径 = **同一批候选**（静态并集）＋ 同一份 ΣW；
        #     差别只在「逐日掩码」⇒ 由 ``pit_membership`` 面板承载）
        union_df, sum_w = build_etf_constituent_union(etf_codes, holdings)
        union_instruments = {code for code in union_df["instrument"].tolist() if code}
        large: set[str] = set()
        for _key, _path in dict(C.LARGE_CAP_INSTRUMENT_FILES).items():
            large |= set(read_instruments_file(_path))
        instruments = sorted(union_instruments & large)
        etf_list = sorted({str(code) for code in union_df["etf_code"].tolist()})
        used_fallback = False
    elif chosen == C.UNIVERSE_MODE_FULL_UNION:
        union_df, sum_w = build_etf_constituent_union(etf_codes, holdings)
        instruments = sorted({code for code in union_df["instrument"].tolist() if code})
        etf_list = sorted({str(code) for code in union_df["etf_code"].tolist()})
        used_fallback = bool(union_df.attrs.get("used_fallback", False))
    elif chosen == C.UNIVERSE_MODE_FALLBACK:
        instruments = read_instruments_file(C.QLIB_INSTRUMENTS_DIR / "csi800.txt")
        sum_w = {}
        etf_list = sorted({_zfill6(code) for code in (etf_codes or [])})
        used_fallback = True
    else:
        raise ValueError(
            f"🔴 未知训练池口径：{chosen}（合法 = {C.UNIVERSE_MODE} / {C.UNIVERSE_MODE_LARGE}"
            f" / {C.UNIVERSE_MODE_PIT} / {C.UNIVERSE_MODE_FALLBACK}）"
        )
    # 🔴 **ΣW 必须与池内标的严格一致**（否则被剔除的标的仍能查到样本权重 ⇒ 口径不一致）
    _instr_set = set(instruments)
    sum_w = {code: float(w) for code, w in (sum_w or {}).items() if code in _instr_set}
    n_stocks = len(instruments)
    if n_stocks < int(C.MIN_UNIVERSE_SIZE):
        raise ValueError(
            f"🔴 训练池只数 {n_stocks} < 最小 {int(C.MIN_UNIVERSE_SIZE)} ⇒ 池不可用（fail-closed）"
        )
    extra: dict = {}
    if chosen == C.UNIVERSE_MODE_PIT:
        # 🔴 **逐日 PIT 成员面板**（唯一的额外键 ⇒ 其余键与默认口径逐位同构）
        dates = ([str(d)[:10] for d in pit_dates] if pit_dates
                 else _pit_calendar_dates(C.PIT_PANEL_START, C.PIT_PANEL_END,
                                          provider_uri=provider_uri))
        extra[C.PIT_MEMBERSHIP_KEY] = build_pit_membership_panel(dates, instruments)
    return {
        "mode": chosen,
        "instruments": instruments,
        "sum_w": sum_w,
        "n_stocks": int(n_stocks),
        "etf_codes": etf_list,
        "used_fallback": used_fallback,
        "holdings_snapshot_date": str(C.HOLDINGS_SNAPSHOT_DATE),
        **extra,
    }


def write_instruments_file(path: Path, instruments: list[str]) -> Path:
    """写 qlib 可读的 instruments txt（每行一个代码，UTF-8）⇒ 返回落点。

    🔴 父目录不存在 ⇒ 自动创建；**覆盖写**（同一路径重跑幂等）。
    🔴 **原子写**（临时文件 + ``os.replace``）⇒ 折级并发训练时多个进程同时写
       同一个池文件也不会让对方读到**半截文件**（POSIX 原子替换）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    codes = [str(code).strip() for code in instruments if str(code).strip()]
    tmp = Path(str(target) + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        for code in codes:
            fh.write(f"{code}\n")
    os.replace(tmp, target)
    return target


# ----------------------------------------------------------------- 规模档
def read_instrument_intervals(path: str | Path) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """读 qlib instruments 文件 ⇒ ``[(code, start, end)]``。

    🔴 **格式**：`SH600000\t2005-04-08\t2026-11-20`（代码 + 起 + 止，**闭区间**）；
    只有代码没有区间 ⇒ 视作**长期有效**（qlib 的 ``all.txt`` 风格）；
    🔴 **同一代码可以有多段区间**（实测 ``csi500.txt`` 有）⇒ 这里**不合并**，
    区间判定由调用方做。

    🔴 **两列行（代码 + start）**：**不许**当成「长期有效」（那是**静默放大池子**）
       ⇒ 解析成「**自 start 起长期有效**」（``start`` ~ ``Timestamp.max``）
       ⇒ 区间**只减不增** ⇒ PIT 面板不会被悄悄撑大。
       （实测本仓 4 份成分表 csi300/csi500/csi800/csi1000 **全是 3 列** ⇒
       该分支对现役数据**零影响**；它防的是将来混进两列行的成分表。）
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"🔴 instruments 文件不存在：{p}")
    out: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.replace("\t", " ").split()
            if not parts:
                continue
            code = parts[0].strip().upper()
            if len(parts) >= 3:
                start = pd.Timestamp(parts[1])
                end = pd.Timestamp(parts[2])
            elif len(parts) == 2:
                # 🔴 两列行 ⇒ 「自 start 起长期有效」（**不是**从数据集起点就有效）
                start = pd.Timestamp(parts[1])
                end = pd.Timestamp.max
            else:
                start = pd.Timestamp.min
                end = pd.Timestamp.max
            out.append((code, start, end))
    return out


def read_instruments_file(path: str | Path) -> list[str]:
    """读 qlib instruments 文件 ⇒ **去重后的代码列表**（升序）。"""
    codes = {code for code, _, _ in read_instrument_intervals(path)}
    return sorted(codes)


def _closed_interval_slice(index: pd.DatetimeIndex,
                           start: pd.Timestamp,
                           end: pd.Timestamp) -> tuple[int, int]:
    """闭区间 ``[start, end]`` 在 ``index`` 上的下标切片 ``(lo, hi)``。

    🔴 **为什么必须归一单位**：pandas ≥3 里 ``pd.to_datetime(["2020-01-02"])``
       推断出的是 **us** 单位，而 ``pd.Timestamp.min`` / ``pd.Timestamp.max``
       是 **ns** ⇒ 直接 ``index.searchsorted(ts)`` 会抛
       ``ValueError: Cannot losslessly convert units``
       ⇒ 「单列行（长期有效）」与「两列行（自 start 起长期有效）」**都会崩**。
       ⇒ 这里先把端点**归一到 index 的单位**再二分。
    """
    unit = getattr(index, "unit", None)
    if unit is not None:
        start = start.as_unit(unit)
        end = end.as_unit(unit)
    lo = int(index.searchsorted(start, side="left"))
    hi = int(index.searchsorted(end, side="right"))
    return lo, hi

# ----------------------------------------------------------------- 逐日 PIT 成员面板
def _pit_calendar_dates(start: str, end: str,
                        calendar_file: str | Path | None = None,
                        provider_uri: str | None = None) -> list[str]:
    """PIT 面板的日期轴（交易日）⇒ **provider 优先、静态兜底**（与训练/打分侧同源）。

    🔴 **为什么必须 provider 优先**（I3）：``train_s._calendar_dates`` 是
       **provider 优先**（``<provider_uri>/calendars/day.txt`` → 静态兜底）⇒
       面板若只认静态 ``cn_data/calendars/day.txt`` ⇒ 两边日期轴可能不同源
       ⇒ 面板的日期轴与训练帧对不上 ⇒ 口径漂移（且「训练帧盖不住面板」时
       只会在训练中段报错，不会在建面板时发现）。

    🔴 日历文件缺失 ⇒ raise FileNotFoundError（fail-closed，不许拿自然日凑数）；
    日历存在但区间内没有交易日 ⇒ raise ValueError。
    """
    # 🔴 默认日历路径**在调用时**取 ``config_v3.CALENDAR_FILE``（不是定义时的默认值）
    #    ⇒ 守卫测试可以 monkeypatch 它 ⇒ 口径可测。
    base_calendar = C.CALENDAR_FILE if calendar_file is None else calendar_file
    candidates: list[Path] = []
    if provider_uri:
        candidates.append(Path(provider_uri) / "calendars" / "day.txt")
    candidates.append(Path(base_calendar))
    existing = [c for c in candidates if c.is_file()]
    if not existing:
        raise FileNotFoundError(
            f"🔴 交易日历不存在：{[str(c) for c in candidates]}"
        )
    for path in existing:
        days: list[str] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if text and str(start) <= text <= str(end):
                    days.append(text)
        if days:
            return days
    raise ValueError(
        f"🔴 交易日历在 {start} ~ {end} 内没有交易日：{[str(c) for c in existing]}"
    )


def build_pit_membership_panel(
    dates,
    instruments: list[str],
    files: dict[str, str | Path] | None = None,
) -> pd.DataFrame:
    """**逐日 PIT 成员面板**（``DataFrame[index=dates, columns=instruments]``，值 = bool）。

    * 成员判定 = **当日**是否落在 ``csi800 ∪ csi1000`` 成分表的**日期区间**内
      （``start <= date <= end``，闭区间）⇒ 同一只票不同年份可进可退；
    * 🔴 **必须按区间判定**（`read_instruments_file` 只取代码、**丢弃区间**
      ⇒ 会得到「历史上曾任成分」的静态并集 ⇒ 池从逐日的 ~1400 只膨胀到 **2635** 只）；
    * ``files`` 默认取 ``config_v3.LARGE_CAP_INSTRUMENT_FILES``（csi800 / csi1000）
      ⇒ 与静态口径**同一份成分表**，只差「按日判定」；
    * 🔴 成分表缺失 ⇒ raise FileNotFoundError（fail-closed）；
    * 🔴 **日期轴升序去重**（``searchsorted`` 要求升序 ⇒ 不排序会算错区间）。
    """
    index = pd.DatetimeIndex(pd.to_datetime(list(dates))).unique().sort_values()
    columns = [str(code) for code in instruments]
    if not columns:
        return pd.DataFrame(index=index, columns=columns, dtype=bool)
    cap_files = dict(C.LARGE_CAP_INSTRUMENT_FILES) if files is None else dict(files)
    positions = {code: idx for idx, code in enumerate(columns)}
    panel = np.zeros((len(index), len(columns)), dtype=bool)
    for path in cap_files.values():
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"🔴 大中盘成分表不存在：{p}（PIT 成员面板）")
        for code, start, end in read_instrument_intervals(p):
            col = positions.get(code)
            if col is None:
                continue
            # 🔴 **闭区间判定**（与 `size_tier_panel` 同一套 searchsorted 写法）
            lo, hi = _closed_interval_slice(index, start, end)
            if hi <= lo:
                continue
            panel[lo:hi, col] = True
    return pd.DataFrame(panel, index=index, columns=columns)


def size_tier_panel(
    instruments: list[str],
    dates,
    files: dict[str, str | Path] | None = None,
) -> pd.DataFrame:
    """**时变**规模档面板：``DataFrame[index=dates, columns=instruments]``。

    * 值 = ``0``(csi300) / ``1``(csi500) / ``2``(csi1000) / ``3``(其它或不在任何档)；
    * 🔴 **用 instruments 文件里的日期区间判定**（``start <= date <= end`` 才算命中）
      ⇒ 同一只票不同年份可能换档（本地实测 ``csi500.txt`` 同一代码有多段区间）；
    * 🔴 **档位最严优先**：同时在 csi300 与 csi500 ⇒ 记 ``0``；
    * ``files`` 默认取 ``config_v3.SIZE_TIER_INSTRUMENT_FILES``（键 = 档名）；
      🔴 **文件缺失 ⇒ raise FileNotFoundError**（fail-closed，不许静默当「其它档」）；
    * ``dates`` 传 ``pd.DatetimeIndex`` 或字符串列表都行；**空 instruments ⇒ 空面板**。
    """
    index = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    columns = [str(code) for code in instruments]
    if not columns:
        return pd.DataFrame(index=index, columns=columns, dtype="int8")
    tier_files = dict(C.SIZE_TIER_INSTRUMENT_FILES) if files is None else dict(files)
    positions = {code: idx for idx, code in enumerate(columns)}
    panel = np.full((len(index), len(columns)), TIER_OTHER, dtype="int8")
    for tier_value, tier_name in enumerate(TIER_FILES_ORDER):
        path = tier_files.get(tier_name)
        if path is None:
            continue
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"🔴 规模档成分表不存在：{p}（档位 {tier_name}）")
        for code, start, end in read_instrument_intervals(p):
            col = positions.get(code)
            if col is None:
                continue
            # 🔴 **闭区间判定**（start <= date <= end）；用 DatetimeIndex.searchsorted
            #    而不是整数纳秒比较（pandas 3 的 datetime 分辨率可能是 us/s ⇒ 单位会错位）
            lo, hi = _closed_interval_slice(index, start, end)
            if hi <= lo:
                continue
            block = panel[lo:hi, col]
            # 🔴 **最严优先**：已被更严的档位占住的位置**不覆盖**
            block[block == TIER_OTHER] = tier_value
            panel[lo:hi, col] = block
    return pd.DataFrame(panel, index=index, columns=columns)


def size_tier_dummies(tier_panel: pd.DataFrame) -> pd.DataFrame:
    """规模档哑变量（**drop 掉 tier3 基准档** ⇒ 3 列 dummy）。

    * 列 = ``tier_csi300`` / ``tier_csi500`` / ``tier_csi1000``（值 0/1）；
    * 🔴 返回**长表**（index = ``(date, instrument)``）⇒ 它可以直接当
      **截面回归的设计矩阵**用（每个「日期 × 个股」一行），与
      :func:`manu.etf_rotation.v3.neutralize.build_controls` 的口径一致；
    * 基准档（tier3 = 其它/不在任何档）不建 dummy ⇒ 避免与截距共线。
    """
    if tier_panel is None or tier_panel.empty:
        return pd.DataFrame(
            index=pd.MultiIndex.from_arrays([[], []], names=["date", "instrument"]),
            columns=list(TIER_DUMMY_COLUMNS),
        )
    dates = pd.DatetimeIndex(pd.to_datetime(tier_panel.index))
    instruments = [str(code) for code in tier_panel.columns]
    values = tier_panel.to_numpy()
    index = pd.MultiIndex.from_product([dates, instruments], names=["date", "instrument"])
    out = pd.DataFrame(index=index)
    for tier_value, column in (
        (TIER_CSI300, "tier_csi300"),
        (TIER_CSI500, "tier_csi500"),
        (TIER_CSI1000, "tier_csi1000"),
    ):
        out[column] = (values == tier_value).astype("float64").reshape(-1)
    return out
