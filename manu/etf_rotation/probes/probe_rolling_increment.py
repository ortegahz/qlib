#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 1「自建增量日」（Rolling Increment）闸门探针。

【用途】
把「上游数据集滞后 ⇒ S 层打不到信号日 ⇒ 用全市场快照自建缺失交易日」这条链变成
**可执行的闸门**：qlib bin 的日历对齐、**基座零改动**（硬链接 copy-on-write）、
守卫剔除不造成错位、缺口补 NaN、**写盘配方口径**、fail-closed 等不变量，
全部用**合成小数据集**验证（**不碰生产数据集、不联网**）。

【口径（2026-11-05 服务器实测）】
`close/open/high/low` 是**后复权价** = 原始价 × `factor`；`volume` = 原始成交量(手) ÷ factor；
`amount` = 成交额(元) ÷ 1000；`vwap` = 成交额(元)/(成交量(股)) × factor；`change` = 原始涨跌幅。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_rolling_increment.py
    （退出码 0 = 全过，1 = 有 FAIL，2 = 环境错）

【断言清单】
I1  追加新交易日：bin 长度 +1，且第 `start+slot` 个 float32 == 写入值
I2  **基座零改动**：构建派生目录后，基座 bin / 日历 / all.txt 逐字节不变
I3  **守卫剔除不错位**：被剔除的标的 bin 长度不变（qlib 读出 NaN），其余 +1
I4  **中间缺口补 NaN**：目标日与 bin 末日差 2 天 ⇒ 长度 +2 且缺口位是 NaN（不错位）
I5  派生目录日历追加新交易日且**升序**；基座日历不变
I6  `instruments/all.txt` 只对**成功写入**的标的推进 end；基座 all.txt 不变
I7  **写盘配方**：close = 原始价 × factor；volume = 原始手数 ÷ factor；
    amount = 成交额 ÷ 1000；vwap = 成交额/(手数×100) × factor；change = 原始涨跌幅
I8  **基准守卫**：qlib 原始前收盘 vs akshare 原始前收盘偏离 > 容差 ⇒ 剔除
H1  **批量历史源解析**：搜狐列序映射 + 成交额**万元 → 元** + 北交所 9 列响应
H2  **交易日历解析**：`YYYY-MM-DD`（带横线）必须能识别（🔴 `isdigit()` 会静默清空日历）
H3  **可延展标的清单**：只含 `instruments/all.txt` 末日 == 基座末日的标的
G1  **打分栅格口径同源**：每日链 `score_grid` == 研究链口径的独立重算（切片起点 = `SCORE_START_TIME`）
G2  **变异反证**：旧口径（从日历首日起算相位）必须与同源口径不同 ⇒ 闸门有牙，防相位漂移回归
D1  **真负例**：缺中间交易日的 bar ⇒ fail-closed 抛错，且派生目录**不落盘**
D2  **真负例**：dry-run ⇒ 派生目录**完全不存在**（磁盘零改动）
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
while not (REPO_ROOT / "manu" / "etf_rotation" / "daily" / "stage1_rolling_increment.py").is_file():
    if REPO_ROOT.parent == REPO_ROOT:
        break
    REPO_ROOT = REPO_ROOT.parent

DAILY_DIR = REPO_ROOT / "manu" / "etf_rotation" / "daily"
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import stage1_rolling_increment as SRI  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """打一条断言结果。

    🔴 **必须带 `[PASS]` / `[FAIL]` 字面量**：`run_gates.sh` 的断言计数口径是
    `grep -c '\[PASS\]'` ⇒ 只打 emoji 的话**本探针的项不会被算进闸门总数**
    （闸门会绿灯，但 headline 少算本阶段 ⇒ 账实不符）。
    """
    (PASS if ok else FAIL).append(name)
    mark = "[PASS]" if ok else "[FAIL]"
    emoji = "✅" if ok else "❌"
    print(f"  {mark} {emoji} {name}" + (f" —— {detail}" if detail else ""))


def _write_bin(path: Path, start: int, values: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.hstack([np.array([start], dtype="<f4"), np.array(values, dtype="<f4")]).astype("<f4")
    arr.tofile(path)


def _make_base(root: Path, cal: list[str], instruments: dict[str, dict]) -> Path:
    base = root / "base"
    (base / "calendars").mkdir(parents=True, exist_ok=True)
    (base / "calendars" / "day.txt").write_text("\n".join(cal) + "\n", encoding="utf-8")
    (base / "instruments").mkdir(parents=True, exist_ok=True)
    lines = []
    for sym, spec in instruments.items():
        lines.append(f"{sym}\t{spec['start']}\t{spec['end']}")
        for field, values in spec["fields"].items():
            _write_bin(base / "features" / sym / f"{field}.day.bin", spec["bin_start"], values)
    (base / "instruments" / "all.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


#: sh600000：原始收盘 20/22/24，factor = 0.5 ⇒ 后复权收盘 10/11/12
INST_A = {
    "start": "2026-01-05",
    "end": "2026-01-07",
    "bin_start": 0,
    "fields": {
        "close": [10.0, 11.0, 12.0],
        "open": [9.8, 10.9, 11.8],
        "high": [10.2, 11.3, 12.4],
        "low": [9.6, 10.7, 11.6],
        # volume = 原始手数 ÷ factor（2000/0.5 = 4000）
        "volume": [4000.0, 4400.0, 4800.0],
        # amount = 成交额(元) ÷ 1000（20×2000×100/1000 = 4000 千元）
        "amount": [4000.0, 4840.0, 5760.0],
        # vwap = 成交额(元)/(手数×100) × factor（= 原始均价 × 0.5）
        "vwap": [10.0, 11.0, 12.0],
        "factor": [0.5, 0.5, 0.5],
        "change": [0.01, 0.1, 0.09],
    },
}
#: sh600002：factor = 0.8 ⇒ 后复权收盘 16/16.8/20 ⇒ 原始收盘 20/21/25
INST_B = {
    "start": "2026-01-05",
    "end": "2026-01-07",
    "bin_start": 0,
    "fields": {
        "close": [16.0, 16.8, 20.0],
        "open": [15.8, 16.7, 19.8],
        "high": [16.2, 17.1, 20.4],
        "low": [15.6, 16.5, 19.6],
        "volume": [2500.0, 2625.0, 2750.0],
        "amount": [5000.0, 5512.5, 6875.0],
        "vwap": [16.0, 16.8, 20.0],
        "factor": [0.8, 0.8, 0.8],
        "change": [0.01, 0.05, 0.19],
    },
}


def main() -> int:
    print("=== 阶段 1 自建增量日闸门（合成数据集，不联网、不碰生产目录）===")
    tmp = Path(tempfile.mkdtemp(prefix="probe_rolling_increment_"))
    try:
        cal = ["2026-01-05", "2026-01-06", "2026-01-07"]
        #: sh600003：末日更早（2026-01-06）⇒ **不可延展**（新交易日写不进去）
        INST_C = {
            "start": "2026-01-05",
            "end": "2026-01-06",
            "bin_start": 0,
            "fields": {"close": [30.0, 31.0], "factor": [0.9, 0.9]},
        }
        base = _make_base(
            tmp, cal, {"sh600000": INST_A, "sh600002": INST_B, "sh600003": INST_C}
        )
        base_before = _snapshot(base)
        live = tmp / "live"

        # ---- 2026-01-08 的全市场 bar（**未复权口径**）----
        #   sh600000：原始前收 24（= qlib 12/0.5）⇒ 基准一致；原始收盘 24.8
        #   sh600002：原始前收 20（= qlib 20/0.8）⇒ 但 qlib 原始前收 = 20/0.8 = 25 ⇒ 偏离 25% ⇒ 剔除
        bars = pd.DataFrame(
            [
                {
                    "code": "600000", "name": "浦发", "date": "2026-01-08",
                    "open": 24.0, "high": 25.0, "low": 23.9, "close": 24.8,
                    "prev_close": 24.0, "volume_lots": 3000.0, "amount_yuan": 7440000.0,
                    "pct_chg": 3.33, "factor_mult": 1.0,
                },
                {
                    "code": "600002", "name": "偏离股", "date": "2026-01-08",
                    "open": 24.0, "high": 25.4, "low": 23.9, "close": 24.8,
                    "prev_close": 20.0, "volume_lots": 2500.0, "amount_yuan": 6200000.0,
                    "pct_chg": 24.0, "factor_mult": 1.0,
                },
            ]
        )

        # ---- D2：dry-run ⇒ 派生目录完全不存在 ----
        entry_dry = SRI.build_live_dataset(
            base, live, ["2026-01-08"], {"2026-01-08": bars}, dry_run=True
        )
        check(
            "D2 dry-run 不落盘（派生目录不存在）",
            not live.exists(),
            f"live.exists={live.exists()}",
        )
        check(
            "D2 dry-run 计划里写 1 只、剔除 1 只",
            entry_dry["per_day"][0]["n_written"] == 1
            and entry_dry["per_day"][0]["n_excluded"] == 1,
            f"written={entry_dry['per_day'][0]['n_written']} "
            f"excluded={entry_dry['per_day'][0]['n_excluded']}",
        )

        # ---- 真构建 ----
        entry = SRI.build_live_dataset(
            base, live, ["2026-01-08"], {"2026-01-08": bars}, dry_run=False
        )
        check("构建成功", entry["live_calendar_max"] == "2026-01-08", str(entry["live_calendar_max"]))

        # ---- I1：bin 长度 +1 且值落位 ----
        start, vals = SRI.read_qlib_bin(live / "features" / "sh600000" / "close.day.bin")
        ok_i1 = start == 0 and len(vals) == 4 and abs(float(vals[3]) - 12.4) < 1e-3
        check(
            "I1 追加新交易日：bin 长度 +1 且值落位",
            ok_i1,
            f"start={start} len={len(vals)} last={vals[-1] if len(vals) else None}",
        )

        # ---- I7：写盘配方 ----
        _, v_close = SRI.read_qlib_bin(live / "features" / "sh600000" / "close.day.bin")
        _, v_vol = SRI.read_qlib_bin(live / "features" / "sh600000" / "volume.day.bin")
        _, v_amt = SRI.read_qlib_bin(live / "features" / "sh600000" / "amount.day.bin")
        _, v_vwap = SRI.read_qlib_bin(live / "features" / "sh600000" / "vwap.day.bin")
        _, v_chg = SRI.read_qlib_bin(live / "features" / "sh600000" / "change.day.bin")
        _, v_fac = SRI.read_qlib_bin(live / "features" / "sh600000" / "factor.day.bin")
        recipe = {
            "close=原始24.8×factor0.5": abs(float(v_close[3]) - 12.4) < 1e-3,
            "volume=原始3000手÷0.5": abs(float(v_vol[3]) - 6000.0) < 1e-2,
            "amount=7440000元÷1000": abs(float(v_amt[3]) - 7440.0) < 1e-2,
            "vwap=7440000/(3000×100)×0.5": abs(float(v_vwap[3]) - 12.4) < 1e-3,
            "change=24.8/24−1": abs(float(v_chg[3]) - (24.8 / 24.0 - 1.0)) < 1e-6,
            "factor=0.5×1.0": abs(float(v_fac[3]) - 0.5) < 1e-8,
        }
        check(
            "I7 写盘配方（后复权口径：close=raw×factor，volume=raw÷factor，amount=元÷1000）",
            all(recipe.values()),
            "; ".join(f"{k}={v}" for k, v in recipe.items() if not v) or "全部吻合",
        )

        # ---- I2：基座逐字节不变 ----
        base_after = _snapshot(base)
        changed = [k for k in base_before if base_before[k] != base_after.get(k)]
        added = [k for k in base_after if k not in base_before]
        check(
            "I2 基座零改动（bin/日历/all.txt 逐字节不变）",
            not changed and not added,
            f"changed={changed[:3]} added={added[:3]}",
        )

        # ---- I3：被剔除的标的 bin 长度不变（不错位）----
        _, vals2 = SRI.read_qlib_bin(live / "features" / "sh600002" / "close.day.bin")
        check(
            "I3 守卫剔除的标的不写盘（bin 长度不变 ⇒ qlib 读出 NaN）",
            len(vals2) == 3,
            f"len={len(vals2)}",
        )

        # ---- I8：基准守卫确实因偏离而剔除 ----
        excluded_reasons = entry["per_day"][0].get("n_excluded", 0)
        check(
            "I8 基准守卫：原始前收偏离 25% ⇒ 剔除该标的",
            int(excluded_reasons) == 1,
            f"n_excluded={excluded_reasons}",
        )

        # ---- I5：日历只加在派生目录，且升序 ----
        live_cal = SRI.read_calendar(live)
        base_cal = SRI.read_calendar(base)
        check(
            "I5 派生目录日历追加且升序；基座日历不变",
            live_cal == sorted(live_cal) and live_cal[-1] == "2026-01-08" and base_cal == cal,
            f"live={live_cal} base={base_cal}",
        )

        # ---- I6：all.txt 只对写成功的标的推进 end ----
        all_txt = (live / "instruments" / "all.txt").read_text(encoding="utf-8")
        rows = {
            ln.split("\t")[0].strip().upper(): ln.split("\t")[2]
            for ln in all_txt.splitlines()
            if ln.strip()
        }
        check(
            "I6 all.txt 只推进成功写入的标的",
            rows.get("SH600000") == "2026-01-08" and rows.get("SH600002") == "2026-01-07",
            f"{rows}",
        )

        # ---- I4：缺口补 NaN（**直接单测写盘原语**）----
        #   端到端场景里「跳过某日」会被守卫拦掉（宁可不写）⇒ 补 NaN 分支是**安全网**，
        #   这里直接构造一个「bin 比日历短 2 天」的写入来验证不错位。
        gap_dir = tmp / "gapunit"
        gap_dir.mkdir(parents=True, exist_ok=True)
        gap_bin = gap_dir / "close.day.bin"
        _write_bin(gap_bin, 0, [10.0, 11.0, 12.0])  # 覆盖日历下标 0/1/2
        gs, gv = SRI.read_qlib_bin(gap_bin)
        SRI.write_qlib_bin_append(gap_bin, gs, gv, 5, 99.0)  # 写到日历下标 5 ⇒ 缺口 3/4
        _, gv2 = SRI.read_qlib_bin(gap_bin)
        ok_i4 = (
            len(gv2) == 6
            and np.isnan(float(gv2[3]))
            and np.isnan(float(gv2[4]))
            and abs(float(gv2[5]) - 99.0) < 1e-6
        )
        check(
            "I4 缺口补 NaN（写第 6 格时 4/5 格是 NaN，不整体错位）",
            ok_i4,
            f"len={len(gv2)} vals={list(map(float, gv2))}",
        )

        # ---- I4b：端到端**拒绝**跨日跳写（上一交易日没有收盘价 ⇒ 不写）----
        base2 = _make_base(tmp / "gap", cal, {"sh600000": INST_A})
        live2 = tmp / "gap" / "live"
        bars2 = pd.DataFrame(
            [
                {
                    "code": "600000", "name": "浦发", "date": "2026-01-09",
                    "open": 26.0, "high": 27.2, "low": 25.8, "close": 26.4,
                    "prev_close": 24.8, "volume_lots": 3000.0, "amount_yuan": 7920000.0,
                    "pct_chg": 6.45, "factor_mult": 1.0,
                }
            ]
        )
        SRI.build_live_dataset(base2, live2, ["2026-01-09"], {"2026-01-09": bars2}, dry_run=False)
        _, v2c = SRI.read_qlib_bin(live2 / "features" / "sh600000" / "close.day.bin")
        check(
            "I4b 跨日跳写被拒（上一交易日无收盘价 ⇒ 宁可不写）",
            len(v2c) == 3,
            f"len={len(v2c)}",
        )

        # ---- H1：搜狐批量响应解析（**万元 → 元**、北交所 9 列也能解析）----
        ns: dict = {}
        exec(SRI._AK_HISTORY_WORKER, ns)  # noqa: S102 - 只取纯函数，不联网
        parse_item = ns["parse_item"]
        item = {
            "code": "cn_600000",
            "hq": [["2026-11-03", "10.06", "10.09", "0.03", "0.30%", "9.85", "10.18",
                    "977550", "97865.83", "0.29%", "186.00"]],
        }
        rows = parse_item(item)
        ok_h1 = (
            len(rows) == 1
            and abs(rows[0]["close"] - 10.09) < 1e-9
            and abs(rows[0]["volume_lots"] - 977550.0) < 1e-9
            and abs(rows[0]["amount_yuan"] - 97865.83 * 10000) < 1e-3
            and abs(rows[0]["low"] - 9.85) < 1e-9
            and abs(rows[0]["high"] - 10.18) < 1e-9
        )
        check(
            "H1 搜狐批量解析（列序映射 + 万元→元）",
            ok_h1,
            f"{rows[0] if rows else None}",
        )
        rows9 = parse_item(
            {"code": "cn_920000", "hq": [["2026-11-03", "11.19", "11.31", "0.12", "1.07%",
                                         "11.08", "11.66", "7330", "833.487"]]}
        )
        check(
            "H1b 北交所 9 列响应也能解析",
            len(rows9) == 1 and abs(rows9[0]["amount_yuan"] - 8334870.0) < 1e-3,
            f"{rows9}",
        )

        # ---- H2：交易日历解析（**带横线的日期**必须能识别）----
        cal_tmp = tmp / "trade_calendar.csv"
        cal_tmp.write_text("date\n2026-01-05\n2026-01-06\n2026-01-07\n", encoding="utf-8")
        td = ns["trade_days"](str(cal_tmp), "2026-01-05", "2026-01-07")
        check(
            "H2 交易日历解析（YYYY-MM-DD 带横线 ⇒ 不能用 isdigit 判）",
            td == ["2026-01-05", "2026-01-06", "2026-01-07"],
            f"{td}",
        )

        # ---- H3：可延展标的清单（只含末日 == 基座末日的标的）----
        live_codes = SRI.extendable_codes(base)
        check(
            "H3 可延展标的清单（末日更早的标的被排除）",
            live_codes == ["600000", "600002"],
            f"{live_codes}（期望 600000/600002；600003 末日更早 ⇒ 应排除）",
        )

        # ---- G1/G2：打分栅格**口径同源**（每日链 vs 研究链）------------------
        #   🔴 相位口径 `i % every_n == every_n - 1` 的 `i` 是**传入列表下标**
        #      ⇒ **切片起点决定相位**。研究链从 `SCORE_START_TIME` 起算；
        #      每日链若传「数据集全量日历（2000 起）」⇒ 两条链相位差 2 个交易日
        #      ⇒ 实测共同日期只剩 1 个 ⇒ 「缺分数」判定全错 ⇒ 误触发全量重打分。
        #   ⇒ 这里用**合成日历**独立重算研究链口径，并断言与 `score_grid()` 逐位一致；
        #   ⇒ 再断言「旧口径（从日历首日起算）」**必须不同** ⇒ 闸门有牙（防回归）。
        import datetime as _dt

        import run_qlib_score as RS  # noqa: E402

        cfg = RS._load_research_config()
        sp = RS._load_signal_policy()
        every_n = int(cfg.REBALANCE_EVERY_N_DAYS)
        # 🔴 合成日历**必须从数据集首日（2000-01-04）起**，与真实 `cn_data` 同构
        #    ⇒ 这样「旧口径（从日历首日起算相位）」才会真的复现那个**相位偏移**
        #    （若从 SCORE_START_TIME 起，旧口径与新口径会几乎重合 ⇒ 反证失效）。
        d0 = _dt.date(2000, 1, 4)
        synth_dates: list[str] = []
        cur = d0
        while len(synth_dates) < 7000:
            if cur.weekday() < 5:
                synth_dates.append(cur.isoformat())
            cur += _dt.timedelta(days=1)
        synth_cal = tmp / "synth_calendar.csv"
        synth_cal.write_text("date\n" + "\n".join(synth_dates) + "\n", encoding="utf-8")

        got = RS.score_grid(calendar_path=synth_cal, end="2026-11-03")

        def _research_recipe(dates: list[str]) -> list[str]:
            """按**研究链口径**独立重算：切片 [SCORE_START_TIME, cal_end] ⇒ 相位 ⇒ 末日例外。"""
            cal_end = max(str(cfg.SCORE_END_TIME), "2026-11-03")
            sliced = [d for d in dates if str(cfg.SCORE_START_TIME) <= d <= cal_end]
            sel = [d for i, d in enumerate(sliced) if i % every_n == every_n - 1]
            if not sel or sel[-1] != sliced[-1]:
                sel.append(sliced[-1])
            return sorted(set(sel))

        expect = _research_recipe(synth_dates)
        check(
            "G1 打分栅格口径同源（每日链 == 研究链重算，逐位一致）",
            got == expect,
            f"每日链 {len(got)} 个 / 研究链 {len(expect)} 个；末 3：{got[-3:]} vs {expect[-3:]}",
        )
        old_recipe = _research_recipe_old = None
        # 旧口径：从**日历首日**起算相位（2026-11-06 修复前的每日链口径）
        sel_old = [d for i, d in enumerate(synth_dates) if i % every_n == every_n - 1]
        if not sel_old or sel_old[-1] != synth_dates[-1]:
            sel_old.append(synth_dates[-1])
        old_recipe = sorted(set(sel_old))
        check(
            "G2 变异反证：旧口径（从日历首日起算相位）**必须与同源口径不同** ⇒ 闸门有牙",
            old_recipe != expect,
            f"旧口径 {len(old_recipe)} 个 / 同源 {len(expect)} 个 ⇒ 差异 {len(set(old_recipe) ^ set(expect))} 个日期",
        )

        # ---- D1：缺中间交易日的 bar ⇒ fail-closed，且不落盘 ----
        live3 = tmp / "failclosed" / "live"
        raised = False
        try:
            SRI.build_live_dataset(
                base2, live3, ["2026-01-08", "2026-01-09"], {}, dry_run=False
            )
        except ValueError:
            raised = True
        check("D1 缺中间交易日 ⇒ fail-closed 抛错", raised)
        check("D1 fail-closed 时派生目录不落盘", not live3.exists(), f"live3.exists={live3.exists()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    if FAIL:
        print("FAIL：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
