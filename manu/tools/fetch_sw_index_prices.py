#!/usr/bin/env python3
"""🔴 抓取**申万一级行业指数**日线（长周期指数宇宙外推检验的输入）。

【为什么要有它】
  V2「ETF 穿透评分」的**完整管线**回测不到 2015：成分股权重档案只有 2020 年以后、
  qlib 个股特征从 2016 起 ⇒ 本脚本只负责**指数宇宙**这一层的数据底座
  （把 V2 的 **ETF 层决策规则**搬到行业指数上跑，见 ``v2_longhorizon_index_probe.py``）。

【数据源】
  ``akshare.index_hist_sw(symbol=<6位代码>, period="day")`` ⇒ 源站 ``swsresearch.com``
  （🔴 **不走东财**：东财 ``index_zh_a_hist`` 实测已被限流 ``Push2RateLimitError``）。
  返回列：``['代码','日期','收盘','开盘','最高','最低','成交量','成交额']``。

【区间】
  默认 **2014-01-01 ~ 2023-12-31**：2014 只做**预热**（动量 120 日 + MA60 + 历史长度闸门），
  🔴 **严禁**取 2024 之后的任何数据（那是 V2 的单盲封存段）⇒ 抓回来的全历史**一律截断**到 ``--end``。

【输出】
  * 长表 CSV：``date,code,name,open,high,low,close,volume,amount``
  * manifest JSON：每只代码的行数 / 起止日 / 失败代码 / 耗时 / akshare 版本 / 抓取时间戳。

【稳健性】
  逐代码 try/except + 重试 ⇒ **单只失败不许中断整批**；失败代码**必须**记进 manifest
  （🔴 不许静默丢）。

用法（服务器 · ak env）::

    /home/huangzhe/anaconda3/envs/ak/bin/python -u manu/tools/fetch_sw_index_prices.py \
        --out-dir /mnt/data/siping/datasets/manu/etf_rotation/datasets/idx_sw_2015_2023
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

#: 🔴 **申万一级行业指数宇宙（31 只）** —— 代码与名称逐字照抄申万官方分类（2021 版）。
#: ⚠️ 申万 2021 版对行业分类做过调整（如「采掘」拆成「煤炭」/「石油石化」）
#:    ⇒ 抓到的是**申万官方历史序列（含事后口径重述）** ⇒ 报告必须披露这一偏差。
SW_L1_UNIVERSE: dict[str, str] = {
    "801010": "农林牧渔",
    "801030": "基础化工",
    "801040": "钢铁",
    "801050": "有色金属",
    "801080": "电子",
    "801110": "家用电器",
    "801120": "食品饮料",
    "801130": "纺织服饰",
    "801140": "轻工制造",
    "801150": "医药生物",
    "801160": "公用事业",
    "801170": "交通运输",
    "801180": "房地产",
    "801200": "商贸零售",
    "801210": "社会服务",
    "801230": "综合",
    "801710": "建筑材料",
    "801720": "建筑装饰",
    "801730": "电力设备",
    "801740": "国防军工",
    "801750": "计算机",
    "801760": "传媒",
    "801770": "通信",
    "801780": "银行",
    "801790": "非银金融",
    "801880": "汽车",
    "801890": "机械设备",
    "801950": "煤炭",
    "801960": "石油石化",
    "801970": "环保",
    "801980": "美容护理",
}

#: 区间默认值（🔴 2024 之后一律不取）
DEFAULT_START = "2014-01-01"
DEFAULT_END = "2023-12-31"

#: 申万返回列 → 输出列
COLMAP: dict[str, str] = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}

#: 失败重试的退避秒数
BACKOFF: tuple[int, ...] = (2, 5)

#: 申万成交额 / 成交量单位（源站口径：成交额=亿元、成交量=亿股）
UNIT_NOTE = "申万指数源站口径：成交额单位=亿元、成交量单位=亿股（与 ETF 面板的「元/手」不可直接比）"


def log(msg: str) -> None:
    """带时间戳的进度输出（stdout，flush ⇒ 被杀也能看到进度）。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """原子落盘（tmp + rename）⇒ 中途被杀不留半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(payload: dict, path: Path) -> None:
    """原子落盘 JSON（manifest）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def fetch_one(ak, code: str, start: str, end: str, retries: int) -> tuple[pd.DataFrame | None, str]:
    """抓单只指数 ⇒ ``(长表, 错误信息)``；成功时错误信息为空串。

    🔴 失败**不抛异常**（逐代码隔离 ⇒ 单只失败不许中断整批）。
    """
    last_err = ""
    for attempt in range(retries + 1):
        try:
            hist = ak.index_hist_sw(symbol=code, period="day")
            if hist is None or len(hist) == 0:
                return None, "空返回（源站没有该代码的历史）"
            out = pd.DataFrame()
            for src, dst in COLMAP.items():
                if src not in hist.columns:
                    return None, f"源站返回列缺 {src}（实得 {list(hist.columns)}）"
                out[dst] = (
                    pd.to_datetime(hist[src], errors="coerce").dt.strftime("%Y-%m-%d")
                    if dst == "date"
                    else pd.to_numeric(hist[src], errors="coerce")
                )
            out["code"] = str(code)
            out = out[["date", "code", "open", "high", "low", "close", "volume", "amount"]]
            # 🔴 **截断到请求区间**：源站只给全历史 ⇒ 2024+ 的数据必须在这里被切掉。
            out = out[(out["date"] >= start) & (out["date"] <= end)]
            out = out.dropna(subset=["date", "close"])
            out = out.drop_duplicates(subset=["date"], keep="last")
            return out.sort_values("date").reset_index(drop=True), ""
        except Exception as exc:  # noqa: BLE001 —— 网络/解析异常都要重试
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt >= retries:
                break
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    return None, last_err or "未知错误"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数。"""
    ap = argparse.ArgumentParser(description="抓取申万一级行业指数日线（2014-2023 预热+评估）")
    ap.add_argument("--out-dir", required=True, help="输出目录（CSV + manifest 落这里）")
    ap.add_argument("--prices-name", default="sw_index_prices.csv", help="长表 CSV 文件名")
    ap.add_argument("--manifest-name", default="manifest.json", help="manifest JSON 文件名")
    ap.add_argument("--start", default=DEFAULT_START, help="起始日（含），默认 2014-01-01（预热）")
    ap.add_argument("--end", default=DEFAULT_END, help="结束日（含），默认 2023-12-31（🔴 2024+ 不取）")
    ap.add_argument("--sleep-min", type=float, default=0.6, help="每次请求后的最小间隔秒（源站限流）")
    ap.add_argument("--sleep-max", type=float, default=1.2, help="每次请求后的最大间隔秒")
    ap.add_argument("--max-retries", type=int, default=2, help="单只代码的重试次数")
    ap.add_argument("--skip-existing", action="store_true",
                    help="已有 CSV 且 manifest 齐备时跳过已抓到的代码（幂等）")
    ap.add_argument("--codes", default="", help="只抓这些代码（逗号分隔；默认=全宇宙 31 只）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """主流程：抓取 ⇒ 落盘 ⇒ 写 manifest。"""
    args = parse_args(argv)

    import akshare as ak  # 延迟导入（ak env）

    out_dir = Path(args.out_dir).expanduser()
    prices_path = out_dir / args.prices_name
    manifest_path = out_dir / args.manifest_name

    universe: dict[str, str] = dict(SW_L1_UNIVERSE)
    if args.codes.strip():
        wanted = [c.strip() for c in args.codes.split(",") if c.strip()]
        universe = {c: SW_L1_UNIVERSE.get(c, c) for c in wanted}

    existing: dict[str, pd.DataFrame] = {}
    if args.skip_existing and prices_path.exists():
        try:
            old = pd.read_csv(prices_path, dtype={"code": str})
            old["code"] = old["code"].astype(str).str.zfill(6)
            for code, grp in old.groupby("code", sort=True):
                existing[str(code)] = grp.copy()
            log(f"♻️ --skip-existing：已有 CSV 含 {len(existing)} 只代码的既有数据")
        except Exception as exc:  # noqa: BLE001 —— 既有文件坏了就当没有
            log(f"⚠️ 既有 CSV 读不出来（{type(exc).__name__}: {exc}）⇒ 全部重抓")
            existing = {}

    log(f"机器={platform.node()} python={sys.version.split()[0]} akshare={ak.__version__} "
        f"待处理 {len(universe)} 只 区间={args.start}~{args.end}")

    frames: list[pd.DataFrame] = []
    per_code: dict[str, dict] = {}
    failed: list[dict] = []
    skipped: list[str] = []
    fetched: list[str] = []
    t0 = time.time()

    for i, (code, name) in enumerate(universe.items(), 1):
        if code in existing and len(existing[code]):
            skipped.append(code)
            frames.append(existing[code])
            grp = existing[code]
            per_code[code] = {
                "name": name,
                "rows": int(len(grp)),
                "date_min": str(grp["date"].min()),
                "date_max": str(grp["date"].max()),
                "status": "skipped_existing",
            }
            continue

        df, err = fetch_one(ak, code, args.start, args.end, int(args.max_retries))
        if df is None:
            failed.append({"code": code, "name": name, "error": err})
            per_code[code] = {"name": name, "rows": 0, "date_min": None, "date_max": None,
                              "status": "failed", "error": err}
            log(f"  ❌ {code} {name} 抓取失败：{err}")
        else:
            fetched.append(code)
            frames.append(df)
            per_code[code] = {
                "name": name,
                "rows": int(len(df)),
                "date_min": str(df["date"].min()) if len(df) else None,
                "date_max": str(df["date"].max()) if len(df) else None,
                "status": "ok" if len(df) else "empty_in_window",
            }
            log(f"  ✅ {code} {name} {len(df)} 行 "
                f"{per_code[code]['date_min']}~{per_code[code]['date_max']}")
        if i < len(universe):
            time.sleep(random.uniform(float(args.sleep_min), float(args.sleep_max)))

    final = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "code", "open", "high", "low", "close", "volume", "amount"])
    if len(final):
        final["code"] = final["code"].astype(str).str.zfill(6)
        final["name"] = final["code"].map(lambda c: universe.get(str(c), ""))
        final = (final[["date", "code", "name", "open", "high", "low", "close", "volume", "amount"]]
                 .drop_duplicates(subset=["date", "code"], keep="last")
                 .sort_values(["code", "date"], kind="stable")
                 .reset_index(drop=True))
    atomic_write_csv(final, prices_path)

    manifest = {
        "source": "akshare.index_hist_sw",
        "source_site": "swsresearch.com",
        "akshare_version": ak.__version__,
        "python_version": sys.version.split()[0],
        "host": platform.node(),
        "generated_at": datetime.now().isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "start": args.start,
        "end": args.end,
        "universe_size": len(universe),
        "n_codes_ok": len(fetched) + len(skipped),
        "n_codes_failed": len(failed),
        "failed_codes": failed,
        "skipped_existing_codes": skipped,
        "fetched_codes": fetched,
        "n_rows": int(len(final)),
        "n_codes_with_data": int(final["code"].nunique()) if len(final) else 0,
        "date_min": str(final["date"].min()) if len(final) else None,
        "date_max": str(final["date"].max()) if len(final) else None,
        "prices_csv": str(prices_path),
        "per_code": per_code,
        "notes": [
            UNIT_NOTE,
            "行业指数是**价格指数**（不含分红再投）⇒ 与 ETF 宇宙（qfq 含分红）收益不可直接比大小。",
            "申万 2021 版行业分类有调整（如「采掘」→「煤炭」/「石油石化」）⇒ "
            "抓到的是申万官方历史序列（含事后口径重述）⇒ 存在分类口径偏差。",
            f"区间截断到 {args.end}（2024+ 为 V2 单盲封存段，本数据集一律不取）。",
        ],
    }
    atomic_write_json(manifest, manifest_path)

    log(f"✅ 完成：{manifest['n_rows']} 行 / {manifest['n_codes_with_data']} 只有数据 / "
        f"失败 {len(failed)} 只 ⇒ {prices_path}")
    if failed:
        log(f"⚠️ 失败代码（已记进 manifest，报告必须披露）："
            f"{[f['code'] for f in failed]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
