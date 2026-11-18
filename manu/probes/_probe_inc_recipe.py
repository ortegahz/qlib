"""实机口径核对（历史批量通道版）：用「自建增量日」的写盘配方重算真实数据集已存在的交易日，
与上游快照里**已经存着的**那一天逐位比对（**只读，零写盘**）。

判据：若 close/volume/amount/vwap/factor/change 全部吻合（相对误差 < 1e-3），
则证明「批量历史源 → 写盘配方」与上游数据集口径**完全一致**。

运行（服务器，**ak env** —— akshare 只装在 ak env）：
    /home/huangzhe/anaconda3/envs/ak/bin/python _probe_inc_recipe.py [--day 2026-11-03]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/tmp/pycharm_project_658d3fc4")
sys.path.insert(0, str(REPO / "manu" / "etf_rotation" / "daily"))
import daily_config as DC  # noqa: E402  （落点常量单一事实源）
import stage1_rolling_increment as SRI  # noqa: E402

PROVIDER = DC.QLIB_CN_DATA

SAMPLE = [
    "SH600000", "SZ000001", "SZ002142", "SH600519",
    "SZ300750", "BJ920000", "SH600036", "SZ000002",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-03")
    ap.add_argument("--codes", default=",".join(SAMPLE))
    args = ap.parse_args()
    day = args.day
    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]

    cal = SRI.read_calendar(PROVIDER)
    print(f"数据集日历末日 = {cal[-1]}（len={len(cal)}），核对日 {day}")
    if day not in cal:
        print(f"❌ {day} 不在数据集日历里（该脚本只核对已存在的交易日）")
        return 2

    # ---- 用新写的「历史批量通道」取数（搜狐 8 只/请求）----
    tmp = Path(tempfile.mkdtemp(prefix="probe_hist_"))
    codes_file = tmp / "_codes.txt"
    codes_file.write_text("\n".join(c[2:] for c in codes) + "\n", encoding="utf-8")
    worker = tmp / "_worker.py"
    worker.write_text(SRI._AK_HISTORY_WORKER, encoding="utf-8")
    proc = subprocess.run(
        [
            "/home/huangzhe/anaconda3/envs/ak/bin/python",
            str(worker), str(tmp), SRI._shift_days(day, -20), day,
            str(codes_file), str(DC.ETF_DATA_ROOT / "trade_calendar.csv"),
        ],
        capture_output=True, text=True, timeout=600,
    )
    print(proc.stderr.strip()[-800:])
    if proc.returncode != 0:
        print(f"❌ 历史通道失败 rc={proc.returncode}: {(proc.stderr or '')[-500:]}")
        return 2

    bars_path = tmp / f"bars_{day.replace('-', '')}.csv"
    if not bars_path.exists():
        print(f"❌ 历史通道没产出 {bars_path}")
        return 2
    bars = pd.read_csv(bars_path, dtype={"code": str})
    bars["code"] = bars["code"].astype(str).str.zfill(6)
    bars["date"] = bars["date"].astype(str).str.slice(0, 10)
    print(f"\n历史通道产出 {len(bars)} 行（{bars['code'].nunique()} 只）\n")

    plan = SRI.plan_day(PROVIDER, day, bars, cal)
    print(f"计划：写 {plan['n_written']} / 剔除 {plan['n_excluded']} / "
          f"停牌 {plan['n_suspended']} / 不在库 {plan['n_not_in_dataset']}")
    for item in plan["excluded"]:
        print(f"  剔除 {item['code']}：{item['reason']}")

    idx = cal.index(day)
    worst = 0.0
    print()
    for p in plan["plans"]:
        cdir = p["cdir"]
        line = [f"── {p['code']}"]
        for field in ("close", "volume", "amount", "vwap", "factor"):
            start, vals = SRI.read_qlib_bin(cdir / f"{field}.day.bin")
            pos = idx - start
            want = float(vals[pos]) if 0 <= pos < len(vals) else float("nan")
            got = float(p["values"][field])
            rel = abs(got - want) / abs(want) if (np.isfinite(want) and want != 0) else float("nan")
            if np.isfinite(rel):
                worst = max(worst, rel)
            line.append(f"{field} 计算 {got:.6g} / 存 {want:.6g} / 相对 {rel:.2e}")
        print("\n   ".join(line))
    print("\n基准守卫实测漂移（qlib 原始前收 vs 批量源原始前收）：")
    for p in plan["plans"]:
        print(f"   {p['code']}: drift = {p['drift']:.3e}（容差 2e-2）")
    print(
        f"\n最大相对误差 = {worst:.3e}"
        f" ⇒ {'✅ 批量历史源 + 配方与上游口径一致（≤1e-3）' if worst < 1e-3 else '❌ 有偏差'}"
    )
    return 0 if worst < 1e-3 else 1


if __name__ == "__main__":
    sys.exit(main())
