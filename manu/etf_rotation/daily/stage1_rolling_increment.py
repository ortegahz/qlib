"""stage1_rolling_increment.py —— 阶段 1 的「自建增量日」兜底（Rolling Increment）。

【为什么需要它（2026-11-05 实测）】
------------------------------------------------------------------------------
阶段 1 的滚动快照换库（`stage1_dataset.py`）只能拿到**上游 `chenditc/investment_data`
的 `qlib_bin.tar.gz` 快照**。实测（2026-11-05 15:32 下载 + 解压三份暂存快照）：

    上游快照 `calendars/day.txt` 末日 = **2026-11-03**（6501 个交易日 / 6174 标的）
    三个暂存快照的日历**逐位相同** ⇒ **不是换库链路坏了，是上游数据本身滞后 2 天**

⇒ 于是 S 层只能打到 11-03 ⇒ 调仓报告里那句
「打分日 = 2026-11-03 ≠ 信号日 2026-11-05」就是这么来的。
（`stage1_dataset` 的行为是**正确的**：日历没推进 ⇒ `swap_skipped` ⇒ 不碰生产数据集。）

【本模块做什么】
------------------------------------------------------------------------------
在**换库之后**，若基座仍覆盖不到信号日，就用 AkShare 的**全市场当日快照**自建缺失
交易日，**派生**出一个可用的 qlib provider 目录（`cn_data_live`）⇒ S 层就能打到信号日。

【三条硬约束（违反即事故）】
------------------------------------------------------------------------------
1. 🔴 **不碰上游基座**：`cn_data` 是上游换库管理的**基准产物**（铁律〇之二：只可调用 /
   读取 / 派生到新目录）⇒ 增量只写 `cn_data_live`（**派生目录**）。
   派生用**硬链接复制**（`cp -al`）⇒ 几乎不占额外空间、基座一个字节都不改。
2. 🔴 **不污染换库相位闸门**：自建日只加在**派生目录**的日历尾部 ⇒ 上游基座的日历前缀
   逐位不变 ⇒ 以后照样能干净换库。
   （若把自建日写进基座，`stage1_dataset.compare_calendars` 的前缀闸门会**永远拒绝换库**。）
3. 🔴 **默认 dry-run**：真写要显式 `--apply`（铁律一：禁擅自启动重活；全市场写盘是重活）。

【qlib bin 格式（源码核验：`qlib/data/storage/file_storage.py:299-379`）】
------------------------------------------------------------------------------
`features/<symbol>/<field>.day.bin` = 小端 float32 数组：
* **第 1 个 float32 = 起始日历下标**（不是值！）；
* 其后每个 float32 对应**一个日历日**（缺失写 NaN）；
* 日历下标 = `start + (元素下标 - 1)` ⇒ **跳过一个标的不会造成错位**（它的 bin 短一截，
  qlib 读出来就是 NaN）；但**中间缺口必须补 NaN**，否则后面的值会整体错位。

【🔴 cn_data 字段口径（2026-11-05 服务器实测，5 标的 × 4 日逐位核对）】
------------------------------------------------------------------------------
| 字段 | 口径 | 实测依据 |
|:--|:--|:--|
| `close/open/high/low` | **后复权价** = 原始价 × `factor` | 002142：`close/factor` = 34.98/35.17/34.01/34.06 ≡ 东财原始收盘 |
| `factor` | 后复权因子（**锚点与上游一致**，比例与锚点无关） | 沿用上一交易日即可（误差 ≤1e-4 相对） |
| `volume` | **原始成交量(手) ÷ factor** | 600000：`volume/原始手数` = 1/factor，逐位吻合 |
| `amount` | **成交额(元) ÷ 1000**（千元） | 600000 逐位吻合 |
| `vwap` | 后复权成交均价（满足 `volume × close × 100 = 成交额(元)`） | 浦发实测自洽 |
| `change` | **原始涨跌幅**（`raw_close/raw_prev_close − 1`，除权日也是原始口径） | 002142 除权日逐位吻合 |

⇒ **写盘配方**（`raw` = 未复权，`f_new` = 当日因子）：
```
open/high/low/close = raw × f_new
volume              = raw_volume_lots / f_new
amount              = raw_amount_yuan / 1000
vwap                = (raw_amount_yuan / (raw_volume_lots × 100)) × f_new
change              = raw_close / raw_prev_close − 1
factor              = f_new
```
🔴 **除权除息股必须修正 factor**，否则会凭空造出一次 ≈ 股息率 的假涨幅
（实测 002142 在 2026-11-05：沿用陈旧 factor ⇒ 假 +0.3837%，正确值 −0.7969%，差 1.18pp）。
修法：`f_new = f_prev × hfq(t)/hfq(t−1)`（比例与复权锚点无关），`hfq` 取
`ak.stock_zh_a_hist(adjust="hfq")`。当日除权名单用东财 `RPT_SHAREBONUS_DET`（1 个请求）。

【基准守卫（宁可不写，也不写脏数据）】
------------------------------------------------------------------------------
用 **D−1 的 qlib 后复权收盘价 ÷ factor** 还原**原始收盘价**，与 AkShare 的
**原始前收盘**比：`r = raw_prev_qlib / raw_prev_ak`，`|r − 1| > 容差` ⇒ **剔除**
（口径不一致 / 数据异常）；`volume == 0` ⇒ 判为停牌；qlib 里没有该标的 ⇒ **不新建标的**。

【数据源与现实边界（2026-11-05 实测）】
------------------------------------------------------------------------------
* `ak.stock_zh_a_spot_em()`：**1 次调用 = 59 个 HTTP 请求 / 67.2s**，拿到全市场
  **5927 只** A 股的当日快照（字段齐备：最新价/今开/最高/最低/昨收/成交量(手)/成交额(元)/涨跌幅）。
* 🔴 **快照只能补「当天」**：没有「一次请求取全市场某个历史交易日」的接口
  （逐标的接口全是单标的；腾讯 fqkline 多标的直接 `param error`；EM 批量接口只给实时快照）
  ⇒ 本模块对历史日**直接失败并披露**（宁可不写）。
  ⇒ **历史日靠本地快照档案**（`snapshot_archive/`）：每次抓到的快照都落盘存档，
  以后补缺口时可以回填。
* 停牌/退市整理期在快照里是**整行行情为空**（不是 volume=0）⇒ 落到 NaN 判据里。
* 🔴 `昨收` 是**除权除息后的前收盘参考价**（不是原始昨收）⇒ 除权股的原始前收盘
  必须从原始 K 线取。

【台账】
------------------------------------------------------------------------------
每次自建交易日写一条 JSONL 到 `DC.QLIB_INCREMENT_LEDGER`：
日期、来源、抓取时刻、写入 / 剔除 / 停牌 / 不在库 计数、基座与派生日历末日。
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import daily_config as DC

#: 派生目录（上游基座的**派生**，铁律〇之二：只可调用/读取/派生到新目录）
QLIB_CN_DATA_LIVE = Path(
    os.environ.get(
        "ETF_ROTATION_QLIB_CN_DATA_LIVE", str(DC.QLIB_DATASET_ROOT / "cn_data_live")
    )
)

#: 增量台账（JSONL）
QLIB_INCREMENT_LEDGER = Path(
    os.environ.get(
        "ETF_ROTATION_QLIB_INCREMENT_LEDGER",
        str(DC.QLIB_DATASET_ROOT / "_increment_ledger.jsonl"),
    )
)

#: 全市场快照档案（每天一份 ⇒ 以后可回填历史缺口）
SNAPSHOT_ARCHIVE = Path(
    os.environ.get(
        "ETF_ROTATION_SNAPSHOT_ARCHIVE",
        str(DC.QLIB_DATASET_ROOT / "snapshot_archive"),
    )
)

#: 🔴 **收盘护栏**：A 股收盘 15:00 ⇒ 收盘后 5 分钟才允许把「今天」写成日线
#:    （盘中快照是**实时价**，写成日线 = 静默写脏数据 ⇒ 后面所有 Alpha 特征全歪）。
MARKET_CLOSE = "15:00"
SESSION_SETTLE_MINUTES = 5


def assert_session_closed(day: str, now: datetime.datetime | None = None) -> None:
    """🔴 **未收盘 ⇒ 拒绝写入该交易日**（fail-closed）。

    盘中/盘前抓到的快照是**实时价**，不是日线收盘价 ⇒ 写进 qlib 二进制库会让
    当天的 `close/volume/vwap` 全错 ⇒ Alpha158 特征静默失真 ⇒ 分数不可信。
    ⇒ 只允许写**已收盘**的交易日（收盘后再留 5 分钟给数据源结算）。
    """
    now = now or datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if day != today:
        return
    hh, mm = (int(x) for x in MARKET_CLOSE.split(":"))
    close_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    settle = close_at + datetime.timedelta(minutes=SESSION_SETTLE_MINUTES)
    if now < settle:
        raise RuntimeError(
            f"🔴 **{day} 尚未收盘**（现在 {now.strftime('%H:%M:%S')}，收盘 {MARKET_CLOSE}"
            f" ＋ 结算缓冲 {SESSION_SETTLE_MINUTES} 分钟）⇒ **拒绝把盘中快照写成日线**"
            f"（宁可不写，也不写脏数据）"
        )


#: 基准守卫容差：`|raw_prev_qlib / raw_prev_ak − 1|` 超过它 ⇒ 剔除该标的
BASIS_TOLERANCE = float(os.environ.get("ETF_ROTATION_INCREMENT_BASIS_TOL", "0.02"))

#: cn_data 的字段 bin（写哪些字段 ⇒ 与上游保持一致）
#: 🔴 `adjclose` 在 cn_data 里存在但口径不明 ⇒ 增量只写 **NaN 占位**（保持长度对齐，不编数字）。
QLIB_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "vwap",
    "factor",
    "change",
    "adjclose",
)


# =============================================================================
# 1. qlib bin 读写
# =============================================================================
def read_qlib_bin(path: Path) -> tuple[int, np.ndarray]:
    """读 qlib 的 float32 bin ⇒ `(起始日历下标, 值数组)`。

    🔴 qlib 的 bin 布局：**第 1 个 float32 是「起始日历下标」**，其后才是各交易日的值
    （缺失写 NaN）⇒ 不能按「第 0 个是值」解析。
    """
    if not path.exists():
        return 0, np.empty(0, dtype="<f4")
    arr = np.fromfile(path, dtype="<f4")
    if arr.size == 0:
        return 0, np.empty(0, dtype="<f4")
    return int(arr[0]), arr[1:].copy()


def cow_break(path: Path) -> bool:
    """断开硬链接（**写之前必须做**）。

    派生目录是基座的**硬链接副本** ⇒ 直接追加写会**改到基座的文件**（同一个 inode）！
    ⇒ 先把内容复制成独立 inode 再写。返回 `True` 表示确实断开了链接。
    """
    if not path.exists():
        return False
    if path.stat().st_nlink <= 1:
        return False
    tmp = path.with_name(path.name + ".cowtmp")
    shutil.copyfile(path, tmp)
    os.replace(tmp, path)
    return True


def write_qlib_bin_append(
    path: Path, start: int, values: np.ndarray, target_index: int, value: float
) -> str:
    """把 `value` 写到 bin 的第 `target_index` 个日历位置。

    对齐 `scripts/dump_bin.py` 的 UPDATE_MODE：
    * `slot == len(values)` ⇒ **追加** float32（O(1)）；
    * `slot > len(values)` ⇒ 中间缺口**补 NaN** 后追加（**保持日历对齐**，否则整体错位）；
    * `slot < len(values)` ⇒ 该日已在 bin 里 ⇒ 不写（返回 `"already"`）。
    """
    slot = int(target_index) - int(start)
    if slot < 0:
        raise ValueError(f"目标日历下标 {target_index} 小于 bin 起始下标 {start} ⇒ 不写")
    if slot < len(values):
        return "already"
    cow_break(path)
    if slot == len(values):
        with path.open("ab") as fp:
            np.array([value], dtype="<f4").tofile(fp)
        return "appended"
    vals = values.astype("<f4", copy=True)
    # 🔴 padding 个数必须是 `slot - len(vals) + 1`（补到 slot+1 长），
    #    写成 `slot - len(vals)` 会短一格 ⇒ `vals[slot]` 越界（off-by-one）。
    vals = np.concatenate([vals, np.full(slot - len(vals) + 1, np.nan, dtype="<f4")])
    vals[slot] = value
    tmp = path.with_name(path.name + ".tmp")
    np.hstack([np.array([start], dtype="<f4"), vals]).astype("<f4").tofile(tmp)
    tmp.replace(path)
    return "padded"


# =============================================================================
# 2. 目录 / 日历 / 标的清单
# =============================================================================
def read_calendar(provider_dir: Path) -> list[str]:
    """读 qlib 日历（`calendars/day.txt`，一行一个 `YYYY-MM-DD`）。"""
    path = Path(provider_dir) / "calendars" / "day.txt"
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_calendar(provider_dir: Path, days: list[str]) -> list[str]:
    """把 `days` 追加到派生目录的日历（已存在 ⇒ 不重复追加；**必须升序**）。"""
    path = Path(provider_dir) / "calendars" / "day.txt"
    existing = read_calendar(provider_dir)
    new_days = sorted({d for d in days if d not in set(existing)})
    if not new_days:
        return []
    cow_break(path)
    with path.open("a", encoding="utf-8") as fp:
        for day in new_days:
            fp.write(day + "\n")
    return new_days


def code_dirs(provider_dir: Path) -> dict[str, Path]:
    """`{6 位代码: features/<symbol> 目录}`（qlib 的目录名是 `sh/sz/bj` + 代码）。"""
    out: dict[str, Path] = {}
    feats = Path(provider_dir) / "features"
    if not feats.is_dir():
        return out
    for item in feats.iterdir():
        if not item.is_dir():
            continue
        name = item.name.lower()
        if len(name) >= 8 and name[-6:].isdigit():
            out[name[-6:]] = item
    return out


def update_instrument_end(provider_dir: Path, symbol: str, end: str) -> bool:
    """把 `instruments/all.txt` 里该标的的结束日推到 `end`。

    🔴 **必须改**：qlib 按 `all.txt` 的 `[start, end]` 决定取数区间 ⇒ 不改 ⇒ 新交易日
    的特征根本不会被取到（打分日当天会是空的）。
    不在文件里 ⇒ 返回 `False`（**不新建标的**：新建要造全套 bin + 改 all.txt，风险太高）。
    """
    path = Path(provider_dir) / "instruments" / "all.txt"
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    for i, line in enumerate(lines):
        parts = line.split("\t")
        if parts and parts[0].strip().upper() == symbol.upper() and len(parts) >= 3:
            if parts[2].strip() < end:
                parts[2] = end
                lines[i] = "\t".join(parts)
                changed = True
    if changed:
        cow_break(path)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


# =============================================================================
# 3. 长度自洽巡检
# =============================================================================
def audit_bins(provider_dir: Path, sample: int | None = None) -> dict:
    """巡检派生目录的 bin 长度自洽性（**只读**）。

    判据（qlib 的读语义）：bin 的**覆盖末日** = `start + len(values) - 1`
    **不得超过**日历末日；**中间缺口是致命的**（值会整体前移）⇒ 本函数统计覆盖末日分布。

    返回 `{"calendar_len","n_instruments","n_bins","over_cal_max","n_over","missing_fields"}`。
    """
    provider_dir = Path(provider_dir)
    cal = read_calendar(provider_dir)
    cal_max = len(cal) - 1
    dirs = code_dirs(provider_dir)
    items = list(dirs.items())
    if sample:
        items = items[:sample]
    n_bins = 0
    n_over = 0
    over_cal_max = 0
    missing_fields: dict[str, list[str]] = {}
    for code, cdir in items:
        for field in QLIB_FIELDS:
            path = cdir / f"{field}.day.bin"
            if not path.exists():
                missing_fields.setdefault(field, []).append(code)
                continue
            start, values = read_qlib_bin(path)
            n_bins += 1
            covered = start + len(values) - 1
            if covered > cal_max:
                n_over += 1
                over_cal_max = max(over_cal_max, covered)
    return {
        "calendar_len": len(cal),
        "n_instruments": len(items),
        "n_bins": n_bins,
        "over_cal_max": over_cal_max,
        "n_over": n_over,
        "missing_fields": {k: len(v) for k, v in missing_fields.items()},
    }


# =============================================================================
# 4. 取数（ak env 子进程；父进程是 ql env，两边依赖互不污染）
# =============================================================================
#: 在 **ak env** 里跑：抓「全市场 A 股当日快照」⇒ 规范化 CSV
#:
#: 输出列（**全部是未复权口径**）：
#:   code,name,date,open,high,low,close,prev_close,volume_lots,amount_yuan,pct_chg,factor_mult
#: `prev_close` = **原始前收盘**（除权股从原始 K 线取，不用快照的除权参考价）
#: `factor_mult` = 当日因子倍数（除权股 = `hfq(t)/hfq(t−1)`；其余 = 1.0）
_AK_WORKER = r'''
"""全市场 A 股当日快照（akshare `stock_zh_a_spot_em`）⇒ 规范化 CSV。

用法：python _ak_worker.py <YYYY-MM-DD> <out.csv>
- 1 次 spot 调用（59 个分页请求，实测 67.2s）+ 1 次分红表 + 除权股逐标的 K 线（~13 次）
- 输出列：code,name,date,open,high,low,close,prev_close,volume_lots,amount_yuan,pct_chg,factor_mult
"""
import json
import sys
import time

import akshare as ak
import pandas as pd

day, out_csv = sys.argv[1], sys.argv[2]
prev_day = sys.argv[3] if len(sys.argv) > 3 else ""
t0 = time.time()

COLS = {
    "代码": "code",
    "名称": "name",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "最新价": "close",
    "昨收": "prev_ref",
    "成交量": "volume_lots",
    "成交额": "amount_yuan",
    "涨跌幅": "pct_chg",
}

df = ak.stock_zh_a_spot_em()
missing = [c for c in COLS if c not in df.columns]
if missing:
    raise SystemExit(f"spot_em 缺列 {missing}；实际列 {list(df.columns)}")
out = df[list(COLS)].rename(columns=COLS).copy()
out["code"] = out["code"].astype(str).str.extract(r"(\d{6})", expand=False)
out = out[out["code"].notna()].copy()
out["date"] = day
for col in ["open", "high", "low", "close", "prev_ref", "volume_lots", "amount_yuan", "pct_chg"]:
    out[col] = pd.to_numeric(out[col], errors="coerce")
# 🔴 停牌/退市整理期 = 整行行情为空 ⇒ 剔除（不是 volume=0）
out = out.dropna(subset=["close", "volume_lots", "amount_yuan"]).copy()

# ---- 原始前收盘：非除权股 = 昨收（= 原始前收盘）；除权股要单独取 ----
out["prev_close"] = out["prev_ref"]
out["factor_mult"] = 1.0

# ---- 当日除权除息名单（东财 datacenter，1 个请求）----
exdiv = []
try:
    import requests

    r = requests.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
        params={
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "pageSize": "50",
            "pageNumber": "1",
            "sortColumns": "EX_DIVIDEND_DATE",
            "sortTypes": "-1",
            "filter": f"(EX_DIVIDEND_DATE='{day}')",
        },
    )
    j = r.json()
    for item in (j.get("result") or {}).get("data") or []:
        exdiv.append(str(item.get("SECURITY_CODE", "")).zfill(6))
except Exception as exc:  # noqa: BLE001
    print(f"[warn] 分红表查询失败（不阻断）：{type(exc).__name__}: {exc}", file=sys.stderr)

# ---- 除权股：取原始 + 后复权 K 线 ⇒ 原始前收盘 + 因子倍数 ----
if exdiv and prev_day:
    for code in exdiv:
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=prev_day.replace("-", ""),
                end_date=day.replace("-", ""),
                adjust="",
            )
            hfq = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=prev_day.replace("-", ""),
                end_date=day.replace("-", ""),
                adjust="hfq",
            )
            raw = raw.sort_values("日期")
            hfq = hfq.sort_values("日期")
            if len(raw) < 2 or len(hfq) < 2:
                continue
            mult = float(hfq["收盘"].iloc[-1]) / float(hfq["收盘"].iloc[-2])
            mask = out["code"] == code
            if not mask.any():
                continue
            out.loc[mask, "prev_close"] = float(raw["收盘"].iloc[-2])
            out.loc[mask, "factor_mult"] = mult
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {code} 除权修正失败：{type(exc).__name__}: {exc}", file=sys.stderr)

out = out[
    [
        "code", "name", "date", "open", "high", "low", "close",
        "prev_close", "volume_lots", "amount_yuan", "pct_chg", "factor_mult",
    ]
]
out.to_csv(out_csv, index=False, encoding="utf-8-sig")
print(
    json.dumps(
        {
            "rows": int(len(out)),
            "exdiv_codes": exdiv,
            "elapsed_sec": round(time.time() - t0, 1),
        },
        ensure_ascii=False,
    )
)
'''


#: 在 **ak env** 里跑：批量**历史**日线（搜狐 `hisHq`，**8 只/请求**）⇒ 每交易日一份 CSV
#:
#: 🔴 **为什么需要这条通道**：实时快照（`stock_zh_a_spot_em`）**只能补当天** ⇒ 上游若滞后
#: **2 个交易日以上**，中间那些天补不上 ⇒ 「当周/当月增量」就落不了地。
#: 实测（2026-11-05）：搜狐 `q.stock.sohu.com/hisHq` 支持 **8 只代码一个请求**、
#: **一次请求覆盖整段区间** ⇒ 全市场 5575 只可延展标的 ≈ **698 个请求**（几分钟），
#: 且口径与东财**逐位吻合**（600000 @11-03：开 10.06 / 收 10.09 / 量 977550 手 /
#: 额 97865.83 **万元** ≡ 东财 9.786583e8 元；920000 北交所同样吻合）。
#: ⇒ 搜狐口径：**不复权原始价**、成交量 = **手**、成交额 = **万元**（⇒ ×10000 换成元）。
#: 🔴 搜狐**不覆盖**已退市的老北交所代码（43xxxx/83xxxx/87xxxx）—— 但那些标的的
#: `instruments/all.txt` 末日停在 2025-09-30 ⇒ **本来就不可能延展** ⇒ 不必抓。
_AK_HISTORY_WORKER = r'''
"""全市场**历史**日线批量抓取（搜狐 hisHq，8 只/请求）⇒ 每交易日一份规范化 CSV。

用法：python _ak_hist_worker.py <out_dir> <start YYYY-MM-DD> <end YYYY-MM-DD> <codes_file>
    python _ak_hist_worker.py <out_dir> <start> <end> <codes_file> <trade_calendar.csv>
输出：<out_dir>/bars_<YYYYMMDD>.csv
      列：code,name,date,open,high,low,close,prev_close,volume_lots,amount_yuan,pct_chg,factor_mult
"""
import json
import os
import re
import sys
import time

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SOHU_URL = "https://q.stock.sohu.com/hisHq"
BATCH = 8
SLEEP = 0.15
PAGE_RE = re.compile(r"callback\((.*)\)\s*$", re.S)


def parse_item(item):
    """搜狐一条记录 ⇒ 规范化行列表。

    列序（实测）：[日期, 开, 收, 涨跌额, 涨跌幅, 低, 高, 量(手), 额(万元), 换手率, 总股本]
    （北交所只有前 9 列 ⇒ 按位置取前 9 个，后面可缺）
    """
    code = re.sub(r"^cn_", "", str(item.get("code") or ""))
    out = []
    for row in item.get("hq") or []:
        if len(row) < 9:
            continue
        try:
            pct = float(str(row[4]).replace("%", "") or "nan")
        except ValueError:
            pct = float("nan")
        try:
            out.append(
                {
                    "code": code,
                    "date": str(row[0])[:10],
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "low": float(row[5]),
                    "high": float(row[6]),
                    "volume_lots": float(row[7]),
                    "amount_yuan": float(row[8]) * 10000.0,   # 万元 → 元
                    "pct_chg": pct,
                }
            )
        except (TypeError, ValueError):
            continue
    return out


def fetch_range(codes, start, end):
    """批量抓 ⇒ 全部行（一次请求 8 只，一次请求覆盖整段区间）。"""
    rows = []
    n_req = 0
    n_fail = 0
    for i in range(0, len(codes), BATCH):
        chunk = codes[i : i + BATCH]
        # 🔴 搜狐的 start/end 必须是 **无横线** 的 `YYYYMMDD`（传 `2026-10-14` 会静默返回空）
        url = (
            SOHU_URL
            + "?code="
            + ",".join("cn_" + c for c in chunk)
            + f"&start={start.replace('-', '')}&end={end.replace('-', '')}"
            + "&stat=1&order=D&period=d&rt=jsonp"
        )
        data = []
        try:
            resp = requests.get(url, headers=HEADERS, timeout=25)
            m = PAGE_RE.search(resp.text)
            data = json.loads(m.group(1)) if m else []
        except Exception as exc:  # noqa: BLE001 - 单批失败不阻断整轮
            n_fail += 1
            print(f"[warn] 批次 {i // BATCH} 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        n_req += 1
        for item in data:
            rows.extend(parse_item(item))
        time.sleep(SLEEP)
    return rows, n_req, n_fail


def exdiv_codes(day):
    """当日除权除息名单（东财 datacenter，1 个请求）。"""
    try:
        resp = requests.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            timeout=30,
            headers=HEADERS,
            params={
                "reportName": "RPT_SHAREBONUS_DET",
                "columns": "ALL",
                "source": "WEB",
                "client": "WEB",
                "pageSize": "50",
                "pageNumber": "1",
                "sortColumns": "EX_DIVIDEND_DATE",
                "sortTypes": "-1",
                "filter": f"(EX_DIVIDEND_DATE='{day}')",
            },
        )
        j = resp.json()
        return [
            str(it.get("SECURITY_CODE", "")).zfill(6)
            for it in (j.get("result") or {}).get("data") or []
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 分红表失败 {day}: {exc}", file=sys.stderr)
        return []


def factor_mults(codes, prev_day, day):
    """除权股的因子倍数 = hfq(day)/hfq(prev_day)（**比例与复权锚点无关**）。"""
    import akshare as ak

    out = {}
    for code in codes:
        try:
            hfq = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=prev_day.replace("-", ""),
                end_date=day.replace("-", ""),
                adjust="hfq",
            )
            hfq = hfq.sort_values("日期")
            if len(hfq) < 2:
                continue
            out[code] = float(hfq["收盘"].iloc[-1]) / float(hfq["收盘"].iloc[-2])
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {code} hfq 取数失败：{exc}", file=sys.stderr)
    return out


def trade_days(calendar_csv, start, end):
    """交易日序列（本地交易日历为事实源）。

    🔴 日期是 `YYYY-MM-DD`（**带横线**）⇒ 不能用 `str.isdigit()` 判格式（那会静默清空
    整个日历 ⇒ 上一交易日全变 None ⇒ 全部标的被守卫剔除）。用正则判。
    """
    import csv

    days = []
    with open(calendar_csv, encoding="utf-8") as fp:
        for row in csv.reader(fp):
            if not row:
                continue
            v = row[0].strip()[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) and start <= v <= end:
                days.append(v)
    return sorted(set(days))


def main():
    out_dir, start, end, codes_file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    calendar_csv = sys.argv[5] if len(sys.argv) > 5 else ""
    os.makedirs(out_dir, exist_ok=True)
    codes = [ln.strip() for ln in open(codes_file, encoding="utf-8") if ln.strip()]

    t0 = time.time()
    rows, n_req, n_fail = fetch_range(codes, start, end)
    print(f"[INFO] 搜狐批量：{len(codes)} 只 / {n_req} 个请求（失败批次 {n_fail}）"
          f" / {len(rows)} 行 / {time.time() - t0:.1f}s", file=sys.stderr)

    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    # 市场交易日序列 ⇒ 上一交易日（**基准守卫要的是「市场」的上一交易日，不是该股的前一行**）
    cal = trade_days(calendar_csv, start, end) if calendar_csv else sorted(by_date)
    prev_of = {}
    for i, day in enumerate(cal):
        prev_of[day] = cal[i - 1] if i > 0 else None

    import csv as _csv

    written = []
    for day in sorted(by_date):
        prev_day = prev_of.get(day)
        frame_rows = []
        # 该股在**上一交易日**的收盘（没有 ⇒ NaN ⇒ 守卫会剔除该股）
        prev_close = {}
        for r in by_date.get(prev_day, []) if prev_day else []:
            prev_close[r["code"]] = r["close"]
        for r in by_date[day]:
            frame_rows.append(
                {
                    "code": r["code"],
                    "name": "",
                    "date": day,
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "prev_close": prev_close.get(r["code"], float("nan")),
                    "volume_lots": r["volume_lots"],
                    "amount_yuan": r["amount_yuan"],
                    "pct_chg": r["pct_chg"],
                    "factor_mult": 1.0,
                }
            )
        # 除权修正：当日除权名单 ⇒ 因子倍数（不修正就会凭空造出 ≈ 股息率 的假涨幅）
        if prev_day:
            cohort = [c for c in exdiv_codes(day) if any(x["code"] == c for x in frame_rows)]
            if cohort:
                mults = factor_mults(cohort, prev_day, day)
                for x in frame_rows:
                    if x["code"] in mults:
                        x["factor_mult"] = mults[x["code"]]
        out_path = os.path.join(out_dir, "bars_" + day.replace("-", "") + ".csv")
        with open(out_path, "w", encoding="utf-8-sig", newline="") as fp:
            w = _csv.DictWriter(
                fp,
                fieldnames=[
                    "code", "name", "date", "open", "high", "low", "close",
                    "prev_close", "volume_lots", "amount_yuan", "pct_chg", "factor_mult",
                ],
            )
            w.writeheader()
            w.writerows(frame_rows)
        written.append((day, len(frame_rows)))
        print(f"[INFO] {day}: {len(frame_rows)} 行 → {out_path}", file=sys.stderr)

    print(json.dumps({"dates": written, "requests": n_req, "elapsed_sec": round(time.time() - t0, 1)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
'''


def fetch_market_snapshot(
    day: str,
    out_csv: str | Path,
    ak_python: str | None = None,
    prev_day: str | None = None,
    timeout: int = 900,
) -> dict:
    """抓「全市场 A 股 `day` 当日快照」⇒ 规范化 CSV（**在 ak env 子进程里跑**）。

    ⚠️ `stock_zh_a_spot_em` 是**实时快照** ⇒ 只能拿到**当天**（收盘后 = 当日收盘价）。
    🔴 **硬闸门**：`day` 不是「ak env 所在时区的今天」且本地档案里也没有 ⇒ **直接抛错**。
       否则会把**今天的行情写进历史日**（静默错位 ⇒ Alpha158 全体失真）。
    """
    ak_python = ak_python or DC.AK_PY
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    worker = Path(out_csv).with_suffix(".worker.py")
    worker.write_text(_AK_WORKER, encoding="utf-8")
    try:
        proc = subprocess.run(
            [ak_python, str(worker), day, str(out_csv), str(prev_day or "")],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        worker.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"akshare 快照抓取失败 rc={proc.returncode}: {(proc.stderr or '')[-600:]}"
        )
    meta: dict = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                meta = json.loads(line)
            except json.JSONDecodeError:
                continue
    meta["path"] = str(out_csv)
    meta["day"] = day
    return meta


def archive_snapshot_path(day: str) -> Path:
    """全市场快照档案的落盘路径（每天一份）。"""
    return SNAPSHOT_ARCHIVE / f"snapshot_{day.replace('-', '')}.csv"


def archive_snapshot(day: str, csv_path: str | Path) -> Path:
    """把当日快照存进档案（⇒ 以后可以回填历史缺口）。"""
    src = Path(csv_path)
    dst = archive_snapshot_path(day)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def load_archived_snapshot(day: str) -> Path | None:
    """读档案里的某日快照；没有 ⇒ `None`（不编数据）。"""
    path = archive_snapshot_path(day)
    return path if path.exists() else None


# =============================================================================
# 4b. 多日历史通道（批量源）+ 取数编排
# =============================================================================
def extendable_codes(base_dir: Path) -> list[str]:
    """**可延展**标的的 6 位代码清单（`instruments/all.txt` 末日 == 基座日历末日）。

    🔴 其余标的的 bin 末日更早 ⇒ 新交易日**根本写不进去**（守卫也会剔掉它们）
    ⇒ **不必去抓**（实测：6174 个目录里只有 **5575** 个可延展 ⇒ 少抓 599 个）。
    """
    base_dir = Path(base_dir)
    all_txt = base_dir / "instruments" / "all.txt"
    cal = read_calendar(base_dir)
    if not all_txt.exists() or not cal:
        return []
    base_max = cal[-1]
    out: list[str] = []
    for line in all_txt.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == base_max:
            symbol = parts[0].strip().upper()
            if len(symbol) >= 8 and symbol[-6:].isdigit():
                out.append(symbol[-6:])
    return sorted(set(out))


def fetch_missing_bars(
    days: list[str],
    *,
    base_dir: Path,
    out_dir: Path,
    ak_python: str | None = None,
    today: str | None = None,
    timeout: int = 5400,
) -> dict[str, pd.DataFrame]:
    """取缺失交易日的全市场 bar ⇒ `{day: DataFrame}`（**取不到就不给**，调用方 fail-closed）。

    通道：
    * **当天** ⇒ `stock_zh_a_spot_em`（1 次调用 / 59 个分页请求 / ~67 秒）；
    * **历史日** ⇒ 搜狐 `hisHq` 批量（**8 只/请求**，一次请求覆盖整段区间 ⇒ 全市场约 698 个请求）。

    每次抓到的**逐日**结果都落进 `snapshot_archive/` ⇒ 下次补缺口可直接回填。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for _d in days:
        assert_session_closed(_d)
    today = today or _today_in_ak_env(ak_python)
    frames: dict[str, pd.DataFrame] = {}
    stats: dict[str, dict] = {}

    history_days = [d for d in days if d != today]
    spot_days = [d for d in days if d == today]

    # ---- 历史日：一条批量通道覆盖整段区间 ----
    if history_days:
        need = [d for d in history_days if load_archived_snapshot(d) is None]
        if need:
            codes = extendable_codes(base_dir)
            if not codes:
                raise RuntimeError(f"读不到可延展标的清单 ⇒ 历史通道放弃（base={base_dir}）")
            codes_file = out_dir / "_codes.txt"
            codes_file.write_text("\n".join(codes) + "\n", encoding="utf-8")
            start = _shift_days(min(need), -20)
            worker = out_dir / "_ak_hist_worker.py"
            worker.write_text(_AK_HISTORY_WORKER, encoding="utf-8")
            cmd = [
                ak_python or DC.AK_PY,
                str(worker),
                str(out_dir),
                start,
                max(need),
                str(codes_file),
                str(DC.TRADE_CALENDAR_CSV),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            worker.unlink(missing_ok=True)
            codes_file.unlink(missing_ok=True)
            for line in (proc.stderr or "").splitlines():
                if line.startswith("[INFO]") or line.startswith("[warn]"):
                    print(line)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"历史批量通道失败 rc={proc.returncode}: {(proc.stderr or '')[-600:]}"
                )
            # 🔴 **归档**：工作目录里的逐日产物 ⇒ 进快照档案（以后可直接回填）
            for day in need:
                produced = out_dir / f"bars_{day.replace('-', '')}.csv"
                if produced.exists():
                    archive_snapshot(day, produced)
        for day in history_days:
            path = load_archived_snapshot(day)
            if path is None:
                stats[day] = {"source": "history", "rows": 0, "note": "历史通道没产出该日"}
                continue
            frame = pd.read_csv(path, dtype={"code": str})
            frame["code"] = frame["code"].astype(str).str.zfill(6)
            frame["date"] = frame["date"].astype(str).str.slice(0, 10)
            frames[day] = frame
            stats[day] = {"source": "history", "rows": int(len(frame))}

    # ---- 当天：实时快照（收盘后 = 当日收盘）----
    for day in spot_days:
        path = load_archived_snapshot(day)
        if path is None:
            csv = out_dir / f"bars_{day.replace('-', '')}.csv"
            # 🔴 **必须传 prev_day**：spot 通道的「昨收」是**除权调整后的参考价**
            #    ⇒ 除权股的原始前收盘要靠 EM 原始 K 线补 ⇒ 不传 prev_day 会**整段跳过**
            #    除权修正（实测会白白丢掉当日 2 只除权股）。
            fetch_market_snapshot(
                day, csv, ak_python=ak_python, prev_day=_prev_trade_day(day)
            )
            archive_snapshot(day, csv)
            path = csv
        frame = pd.read_csv(path, dtype={"code": str})
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["date"] = frame["date"].astype(str).str.slice(0, 10)
        frames[day] = frame
        stats[day] = {"source": "spot_em", "rows": int(len(frame))}

    return {"frames": frames, "stats": stats}


def _today_in_ak_env(ak_python: str | None = None) -> str:
    """ak env 所在时区的今天（`YYYY-MM-DD`）。"""
    py = ak_python or DC.AK_PY
    proc = subprocess.run(
        [py, "-c", "import datetime;print(datetime.datetime.now().astimezone().date().isoformat())"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = (proc.stdout or "").strip().splitlines()
    return lines[-1].strip() if lines else ""


def _shift_days(day: str, delta: int) -> str:
    """日期平移（自然日）。"""
    return (pd.Timestamp(day) + pd.Timedelta(days=delta)).strftime("%Y-%m-%d")


def _prev_trade_day(day: str) -> str:
    """`day` 的**上一个交易日**（本地交易日历为事实源；找不到 ⇒ 空串）。"""
    path = DC.TRADE_CALENDAR_CSV
    if not path.exists():
        return ""
    df = pd.read_csv(path, dtype=str)
    col = df.columns[0]
    days = sorted({str(v).strip()[:10] for v in df[col].dropna() if str(v).strip()})
    prior = [d for d in days if d < day]
    return prior[-1] if prior else ""


# =============================================================================
# 5. 写入计划（守卫）
# =============================================================================
def _to_float(value) -> float:
    """宽松转 float（读不出来 ⇒ NaN）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def plan_day(
    provider_dir: Path,
    day: str,
    bars: pd.DataFrame,
    cal: list[str],
    tolerance: float = BASIS_TOLERANCE,
    prev_override: dict[str, tuple[float, float]] | None = None,
) -> dict:
    """算某个交易日的写入计划（**只读，不写盘**）。

    `bars` 的列（**未复权口径**）：`code,name,date,open,high,low,close,prev_close,
    volume_lots,amount_yuan,pct_chg,factor_mult`。

    返回：`{"day","n_written","n_excluded","n_suspended","n_not_in_dataset","plans",...}`。
    """
    provider_dir = Path(provider_dir)
    dirs = code_dirs(provider_dir)
    plans: list[dict] = []
    excluded: list[dict] = []
    suspended: list[dict] = []
    not_in_dataset: list[str] = []

    if day not in cal:
        raise ValueError(f"目标日 {day} 不在日历里 ⇒ 无法定位上一交易日")
    target_index = cal.index(day)
    prev_pos = target_index - 1
    if prev_pos < 0:
        raise ValueError(f"目标日 {day} 是日历第一天 ⇒ 没有上一交易日可比对")

    for code, group in bars.groupby("code", sort=True):
        code = str(code).zfill(6)
        row = group.iloc[0]
        cdir = dirs.get(code)
        if cdir is None:
            not_in_dataset.append(code)
            continue

        close_start, close_vals = read_qlib_bin(cdir / "close.day.bin")
        if close_vals.size == 0:
            not_in_dataset.append(code)
            continue
        factor_start, factor_vals = read_qlib_bin(cdir / "factor.day.bin")

        # 🔴 **必须取「上一交易日」那格的值**（不是「最后一个有限值」）：
        #    若某标的昨天被守卫剔除（bin 里没有昨天），拿更早的值去比
        #    ⇒ 比的是多日收益 ⇒ 会把正常股票**级联剔除**。
        #    `prev_override` = dry-run 的**前一日模拟值**（{code: (close_hfq, factor)}）
        #    ⇒ 否则多日缺口在 dry-run 里会「第二天全被剔除」（因为前一天没真写盘）。
        pos = prev_pos - close_start
        qlib_prev_close_hfq = float("nan")
        f_prev = float("nan")
        if prev_override and code in prev_override:
            qlib_prev_close_hfq, f_prev = prev_override[code]
        else:
            if pos < 0 or pos >= len(close_vals) or not np.isfinite(close_vals[pos]):
                excluded.append(
                    {
                        "code": code,
                        "reason": (
                            f"上一交易日（{cal[prev_pos]}）没有收盘价 ⇒ "
                            f"无法做基准守卫 ⇒ **宁可不写**"
                        ),
                    }
                )
                continue
            qlib_prev_close_hfq = float(close_vals[pos])
            fpos = prev_pos - factor_start
            f_prev = float(factor_vals[fpos]) if (0 <= fpos < len(factor_vals)) else float("nan")
        if not np.isfinite(f_prev) or f_prev == 0.0:
            excluded.append({"code": code, "reason": "上一交易日没有 factor ⇒ 无法还原原始价 ⇒ 剔除"})
            continue

        raw_close = _to_float(row.get("close"))
        raw_prev = _to_float(row.get("prev_close"))
        raw_volume = _to_float(row.get("volume_lots"))
        raw_amount = _to_float(row.get("amount_yuan"))
        factor_mult = _to_float(row.get("factor_mult"))
        if not np.isfinite(factor_mult) or factor_mult <= 0:
            factor_mult = 1.0

        # ---- 停牌 / 无量 ⇒ 留 NaN（与上游缺数据的行为一致）----
        if not np.isfinite(raw_volume) or raw_volume <= 0:
            suspended.append({"code": code, "reason": f"成交量 {raw_volume} ⇒ 判为停牌/无成交"})
            continue
        if not np.isfinite(raw_close) or raw_close <= 0:
            excluded.append({"code": code, "reason": f"收盘价 {raw_close} 读不出来 ⇒ 剔除"})
            continue
        if not np.isfinite(raw_prev) or raw_prev <= 0:
            excluded.append({"code": code, "reason": f"原始前收盘 {raw_prev} 读不出来 ⇒ 剔除"})
            continue

        # ---- 基准守卫：qlib 的 D−1 **原始**收盘价 vs AkShare 的原始前收盘 ----
        raw_prev_qlib = qlib_prev_close_hfq / f_prev
        ratio = raw_prev_qlib / raw_prev
        drift = abs(ratio - 1.0)
        if drift > tolerance:
            excluded.append(
                {
                    "code": code,
                    "reason": (
                        f"基准偏离 {drift:.4%} > 容差 {tolerance:.2%}"
                        f"（qlib 原始 {raw_prev_qlib:.4f} vs ak 原始 {raw_prev:.4f}）"
                        f" ⇒ 口径不一致/数据异常 ⇒ **剔除、不写脏数据**"
                    ),
                    "qlib_raw_prev_close": round(raw_prev_qlib, 6),
                    "ak_raw_prev_close": round(raw_prev, 6),
                    "drift": round(drift, 6),
                }
            )
            continue

        # ---- 新值（后复权口径；见模块 docstring 的写盘配方）----
        f_new = f_prev * factor_mult
        values: dict[str, float] = {}
        for field in ("open", "high", "low", "close"):
            val = _to_float(row.get(field))
            values[field] = float(val * f_new) if np.isfinite(val) else float("nan")
        values["volume"] = float(raw_volume / f_new) if np.isfinite(raw_volume) else float("nan")
        values["amount"] = float(raw_amount / 1000.0) if np.isfinite(raw_amount) else float("nan")
        if np.isfinite(raw_amount) and np.isfinite(raw_volume) and raw_volume > 0:
            # cn_data 的 vwap = 成交额(元) / 成交量(股)，且要落在后复权空间
            values["vwap"] = float(raw_amount / (raw_volume * 100.0) * f_new)
        else:
            values["vwap"] = float("nan")
        values["change"] = float(raw_close / raw_prev - 1.0)
        values["factor"] = float(f_new)

        plans.append(
            {
                "code": code,
                "cdir": cdir,
                "values": values,
                "qlib_prev_close_hfq": round(qlib_prev_close_hfq, 6),
                "qlib_raw_prev_close": round(raw_prev_qlib, 6),
                "ak_raw_prev_close": round(raw_prev, 6),
                "drift": round(drift, 6),
                "f_prev": round(f_prev, 8),
                "f_new": round(f_new, 8),
                "prev_pos": prev_pos,
            }
        )

    return {
        "day": day,
        "n_written": len(plans),
        "n_excluded": len(excluded),
        "n_suspended": len(suspended),
        "n_not_in_dataset": len(not_in_dataset),
        "plans": plans,
        "excluded": excluded,
        "suspended": suspended,
        "not_in_dataset": not_in_dataset,
    }


def apply_day(
    provider_dir: Path,
    day: str,
    bars: pd.DataFrame,
    cal: list[str],
    *,
    dry_run: bool = True,
    tolerance: float = BASIS_TOLERANCE,
) -> dict:
    """把某个交易日的 bar 写进**派生目录**（`dry_run=True` ⇒ 只报告不写盘）。"""
    provider_dir = Path(provider_dir)
    plan = plan_day(provider_dir, day, bars, cal, tolerance=tolerance)
    if day not in cal:
        raise ValueError(f"目标日 {day} 不在日历里 ⇒ 先 append_calendar 再写")

    target_index = cal.index(day)
    written = 0
    errors: list[str] = []
    for item in plan["plans"]:
        cdir: Path = item["cdir"]
        try:
            for field in QLIB_FIELDS:
                path = cdir / f"{field}.day.bin"
                if not path.exists():
                    continue
                start, values = read_qlib_bin(path)
                write_qlib_bin_append(
                    path, start, values, target_index, float(item["values"].get(field, float("nan")))
                )
            if not dry_run:
                update_instrument_end(provider_dir, cdir.name.upper(), day)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单只写失败不许拖垮整轮
            errors.append(f"{item['code']} 写盘失败：{type(exc).__name__}: {exc}")

    result = dict(plan)
    result.pop("plans", None)
    result.update(
        {
            "dry_run": bool(dry_run),
            "written": written,
            "errors": errors,
            "target_index": target_index,
            "calendar_max": cal[-1] if cal else None,
        }
    )
    return result


# =============================================================================
# 6. 派生目录物化
# =============================================================================
def hardlink_copy(src: Path, dst: Path) -> None:
    """硬链接复制整棵目录（`cp -al`）⇒ 不占额外空间、基座零改动。

    失败（跨文件系统 / 无 `cp`）⇒ 退化为普通复制（慢，但语义相同）。
    """
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["cp", "-al", str(src), str(dst)], capture_output=True, text=True, timeout=1800
        )
        if proc.returncode == 0:
            return
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, copy_function=shutil.copy2)


def build_live_dataset(
    base_dir: Path,
    live_dir: Path,
    days: list[str],
    bars_by_day: dict[str, pd.DataFrame],
    *,
    dry_run: bool = True,
    tolerance: float = BASIS_TOLERANCE,
    ledger_path: Path | None = None,
    source: str = "spot_em(当天) + sohu_hisHq(历史批量)",
) -> dict:
    """构建派生数据集：`live = base + 自建交易日`。

    步骤：① 硬链接复制基座 → 派生目录；② 按日历顺序补齐缺失交易日；
    ③ 逐日写盘（守卫见 `plan_day`）；④ 写台账。

    🔴 **dry-run=True** ⇒ 派生目录**完全不落盘**（只做计划与统计）。
    """
    base_dir, live_dir = Path(base_dir), Path(live_dir)
    if not base_dir.exists():
        raise FileNotFoundError(f"基座数据集不存在：{base_dir}")
    base_cal = read_calendar(base_dir)
    if not base_cal:
        raise ValueError(f"基座日历读不出来：{base_dir / 'calendars' / 'day.txt'}")

    ordered_days = sorted({d for d in days})
    missing_days = [d for d in ordered_days if d not in set(base_cal)]
    notes: list[str] = []

    # 🔴 **预检先行**：所有待补交易日都必须有 bar ⇒ 不满足就**在任何落盘之前**失败
    #    （fail-closed ⇒ 磁盘零副作用；否则会先建出一个半成品派生目录）。
    for day in missing_days:
        bars = bars_by_day.get(day)
        if bars is None or getattr(bars, "empty", True):
            raise ValueError(
                f"缺 {day} 的全市场 bar ⇒ 拒绝构建（**fail-closed**："
                f"日历中间断档会让后面的值整体错位）"
            )

    plan_summary: list[dict] = []
    if not dry_run:
        hardlink_copy(base_dir, live_dir)
        cal = read_calendar(live_dir)
        for day in ordered_days:
            if day in set(cal):
                continue
            bars = bars_by_day.get(day)
            if bars is None or getattr(bars, "empty", True):
                raise ValueError(
                    f"缺 {day} 的全市场 bar ⇒ 拒绝构建（**fail-closed**："
                    f"日历中间断档会让后面的值整体错位）"
                )
            append_calendar(live_dir, [day])
            cal = read_calendar(live_dir)
            res = apply_day(live_dir, day, bars, cal, dry_run=False, tolerance=tolerance)
            plan_summary.append(
                {
                    k: v
                    for k, v in res.items()
                    if k not in ("excluded", "suspended", "not_in_dataset", "plans")
                }
            )
            notes.append(
                f"{day}: 写入 {res['written']} / 剔除 {res['n_excluded']} / "
                f"停牌 {res['n_suspended']} / 不在库 {res['n_not_in_dataset']}"
            )
        live_cal = read_calendar(live_dir)
    else:
        cal = list(base_cal)
        for day in ordered_days:
            if day not in set(cal):
                cal.append(day)
        cal = sorted(cal)
        # 🔴 dry-run **按序模拟**：把前一日「计划写入」的值当作下一日的上一交易日收盘
        #    ⇒ 多日缺口的计划才是**诚实**的（否则第二天会假性全被剔除）。
        prev_override: dict[str, tuple[float, float]] = {}
        for day in ordered_days:
            bars = bars_by_day.get(day)
            if bars is None or getattr(bars, "empty", True):
                raise ValueError(f"缺 {day} 的全市场 bar ⇒ 拒绝构建（fail-closed）")
            res = plan_day(
                live_dir if live_dir.exists() else base_dir,
                day,
                bars,
                cal,
                tolerance=tolerance,
                prev_override=prev_override or None,
            )
            prev_override = {
                item["code"]: (float(item["values"]["close"]), float(item["values"]["factor"]))
                for item in res["plans"]
            }
            plan_summary.append(
                {
                    k: v
                    for k, v in res.items()
                    if k not in ("excluded", "suspended", "not_in_dataset", "plans")
                }
            )
            notes.append(
                f"{day}（计划）: 写入 {res['n_written']} / 剔除 {res['n_excluded']} / "
                f"停牌 {res['n_suspended']} / 不在库 {res['n_not_in_dataset']}"
            )
            for item in (res.get("excluded") or [])[:5]:
                notes.append(f"    └ 剔除 {item.get('code')}：{item.get('reason')}")
        live_cal = cal

    entry = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_dir": str(base_dir),
        "live_dir": str(live_dir),
        "base_calendar_max": base_cal[-1],
        "live_calendar_max": live_cal[-1] if live_cal else None,
        "days_added": missing_days,
        "source": source,
        "tolerance": tolerance,
        "dry_run": bool(dry_run),
        "per_day": plan_summary,
        "notes": notes,
    }
    if not dry_run and ledger_path is not None:
        ledger_path = Path(ledger_path)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        entry["ledger"] = str(ledger_path)
    return entry


# =============================================================================
# 7. 交易日缺口
# =============================================================================
def missing_trade_days(base_max: str, target: str, calendar_csv: Path | None = None) -> list[str]:
    """`(base_max, target]` 之间的**交易日**清单（本地交易日历为事实源）。

    🔴 日历中间断档 ⇒ qlib 的 bin 会整体错位 ⇒ 缺口必须**逐日补齐**，不能只补最后一天。
    """
    path = Path(calendar_csv) if calendar_csv else DC.TRADE_CALENDAR_CSV
    if not path.exists():
        raise FileNotFoundError(f"交易日历缺失：{path}")
    df = pd.read_csv(path, dtype=str)
    col = df.columns[0]
    days = sorted({str(v).strip()[:10] for v in df[col].dropna() if str(v).strip()})
    return [d for d in days if base_max < d <= target]


# =============================================================================
# 8. CLI
# =============================================================================
def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="阶段 1 兜底：自建缺失交易日的全市场 bar ⇒ 派生 qlib provider 目录"
    )
    ap.add_argument("--day", required=True, help="目标交易日（YYYY-MM-DD）")
    ap.add_argument("--base-dir", default=str(DC.QLIB_CN_DATA), help="上游基座数据集目录")
    ap.add_argument("--live-dir", default=str(QLIB_CN_DATA_LIVE), help="派生目录")
    ap.add_argument("--bars-csv", default=None, help="已有 bar CSV（不给则现场抓/读档案）")
    ap.add_argument("--ak-python", default=None, help="akshare 解释器")
    ap.add_argument("--tolerance", type=float, default=BASIS_TOLERANCE, help="基准守卫容差")
    ap.add_argument("--apply", action="store_true", help="真写盘（默认 dry-run）")
    args = ap.parse_args(argv)
    dry_run = not args.apply

    base_dir = Path(args.base_dir)
    live_dir = Path(args.live_dir)
    base_cal = read_calendar(base_dir)
    base_max = base_cal[-1] if base_cal else None
    print(f"基座：{base_dir}（日历末日 {base_max}）")

    days = missing_trade_days(base_max, args.day) if base_max else [args.day]
    print(f"待补交易日：{days}")

    bars_by_day: dict[str, pd.DataFrame] = {}
    if args.bars_csv:
        for day in days:
            bars = pd.read_csv(args.bars_csv, dtype={"code": str})
            bars["code"] = bars["code"].astype(str).str.zfill(6)
            bars["date"] = bars["date"].astype(str).str.slice(0, 10)
            bars_by_day[day] = bars
            print(f"{day}: bar {len(bars)} 行（来自 {args.bars_csv}）")
    else:
        work_dir = DC.RESEARCH_RUN_ROOT / "_increment_bars"
        got = fetch_missing_bars(
            days, base_dir=base_dir, out_dir=work_dir, ak_python=args.ak_python
        )
        bars_by_day = got["frames"]
        for day, st in got["stats"].items():
            print(f"{day}: bar {st['rows']} 行（来源 {st['source']}）")

    entry = build_live_dataset(
        base_dir,
        live_dir,
        days,
        bars_by_day,
        dry_run=dry_run,
        tolerance=args.tolerance,
        ledger_path=QLIB_INCREMENT_LEDGER,
    )
    print(json.dumps(entry, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工自检
    sys.exit(main())
