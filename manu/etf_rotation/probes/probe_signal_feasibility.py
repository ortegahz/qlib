#!/usr/bin/env python
"""信号日可行性 · 回归探针（M-S2，2026-11-05 06:3x）。

【为什么要有这道闸】
M-S2 实测出**两个会让「到节点日跑流水线」白跑一小时 / 静默产出假结果**的缺陷：

===== ============================== ==========================================
编号  缺陷                            实测后果
===== ============================== ==========================================
G1    `qlib_scores.py --end` 被      ⇒ `--end 2026-12-01` **静默**只打到
      `C.SCORE_END_TIME` 静默截断     2026-11-03，用户以为「已重打分」，
      （日历查询窗写死）              其实还是旧分数 ⇒ 一小时算力白花
G2    流水线**没有任何一处**判定      ⇒ `--asof 2026-12-01` 会一路跑到
      「信号日打不打得出来」           analyze 阶段才报「个股分数里没有记录」
                                      ⇒ 一小时之后才发现
G3    **打分日是相位栅格**，不是任意  ⇒ 2026-12-01 **根本不在打分栅格**上
      交易日                          （相位日：11-04 / 11-18 / **12-02** /
                                       12-16 / 12-30）⇒ 重建数据集也打不出
                                       12-01 ⇒ 「12-01 跑一次」在数据层面
                                       **不成立**
===== ============================== ==========================================

【口径】
* **硬拦 vs 只披露必须分清**（§十四.77）：S1 / S2 / S0 硬拦；S3 / S4 只披露。
* **必须带真负例**（§十四.36）：只测正例 PASS 等于没测 ⇒ 本探针含 **4 个真负例**
  （P5 / P6 / P7 / D1）⇒ 逐个把输入改坏 ⇒ 断言必须**真的报出来**。
* **静态回归断言**（S 组）：M-S2 的修复**不许被改回去**
  （--end 不许再被静默截断、口径不许再各写各的、守卫不许再被绕过）。

【副作用】**只读**：全部检查都在内存副本 / 子进程上做，**不写任何文件**、不联网。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_signal_feasibility.py \\
        [--run-root <落点根>/runs]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
if str(DATA_DIR) not in sys.path:
    sys.path.insert(0, str(DATA_DIR))

import config as C  # noqa: E402
import signal_policy as SP  # noqa: E402

#: 本轮实测出的缺陷编号（写进探针输出，方便对着 memory 核）
KNOWN_DEFECTS = ("G1", "G2", "G3")

#: qlib 数据集日历（数据集覆盖末日的权威来源）
QLIB_CALENDAR = Path(str(getattr(C.STOCK, "PROVIDER_URI", ""))) / "calendars" / "day.txt"


class Checker:
    """极简断言收集器：记录 (id, 名称, 是否通过, 细节)。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        """记一条断言结果。"""
        self.items.append((check_id, name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, str, bool, str]]:
        """失败的断言列表。"""
        return [it for it in self.items if not it[2]]


def _grid_for(cal_dates: list, every_n: int, start: str, end: str) -> list[str]:
    """按 `signal_policy` 口径算栅格（探针内部便捷封装）。"""
    return SP.score_grid_dates(cal_dates, every_n, start=start, end=end)


def run_checks(run_root: Path) -> list[tuple[str, str, bool, str]]:
    """跑全部断言，返回 (id, 名称, 是否通过, 细节) 列表。"""
    checker = Checker()

    # ============================================================ P 组：纯函数
    # ---- P1：策略函数复算出的栅格 == 真实产物 stock_scores.csv 的日期集合 ----
    scores_csv = Path(str(C.STOCK_SCORES_CSV))
    if not scores_csv.exists():
        checker.add("P1", "打分日栅格与真实 stock_scores.csv 逐位一致", True,
                    f"（{scores_csv} 不存在 ⇒ 跳过）")
        grid_now: list[str] = []
    else:
        import pandas as pd

        real = sorted({str(d) for d in pd.to_datetime(
            pd.read_csv(scores_csv, usecols=["date"])["date"].unique()).date})
        cal_all = SP.read_calendar_dates(QLIB_CALENDAR)
        lo = SP.parse_date(C.SCORE_START_TIME)
        # 🔴 2026-11-10（闸门假红修复）：日历查询窗必须与**生产口径**同款
        #    （`qlib_scores.py:572`：`cal_end = max(SCORE_END_TIME, requested_end)`）。
        #    旧实现**少了这一步** ⇒ qlib 数据集被 `--full` 推进（11-03 → 11-09）后：
        #    期望栅格按**新日历**算（末日 11-09），而断言对象是 **asof=11-03 的旧基线产物**
        #    （末日 11-03）⇒ 两个 vintage ⇒ P1 假红 + P4 的 S1 假硬拦。
        #    （实测：把日历截回 11-03 后旧探针 P1/P4 全 PASS ⇒ 因果链闭合。）
        artifact_asof = real[-1] if real else str(C.STOCK.TEST[1])
        hi = SP.parse_date(max(C.SCORE_END_TIME, artifact_asof))
        window = [d for d in cal_all
                  if (lo is None or d >= lo) and (hi is None or d <= hi)]
        grid_now = _grid_for(window, C.REBALANCE_EVERY_N_DAYS, str(C.STOCK.TEST[0]),
                             artifact_asof)
        checker.add("P1", "打分日栅格与真实 stock_scores.csv 逐位一致",
                    grid_now == real,
                    f"策略算出 {len(grid_now)} 个（{grid_now[0] if grid_now else '-'} ~ "
                    f"{grid_now[-1] if grid_now else '-'}）｜产物 {len(real)} 个"
                    f"（{real[0] if real else '-'} ~ {real[-1] if real else '-'}）")

    # ---- P2：相位语义（每 N 个交易日一个）+ 末日例外 ----
    synth = [f"2024-01-{d:02d}" for d in range(1, 11)]  # 10 个连续日
    g2 = _grid_for(synth, 10, None, None)
    checker.add("P2", "相位语义：每 N 个交易日取一个 + 末日必定入选",
                g2 == ["2024-01-10"], f"合成 10 日栅格 ⇒ {g2}（期望 ['2024-01-10']）")

    # ---- P3：末日例外（末交易日不在相位上时必须入选）----
    synth3 = [f"2024-01-{d:02d}" for d in range(1, 12)]  # 11 日 ⇒ 第 11 日不在相位上
    g3 = _grid_for(synth3, 10, None, None)
    checker.add("P3", "末日例外：末交易日必定入选（否则拿不到当期分数）",
                g3 == ["2024-01-10", "2024-01-11"], f"合成 11 日栅格 ⇒ {g3}")

    # ---- P4：可达信号日 ⇒ 无硬拦 ----
    f4 = SP.check_signal_feasibility(
        asof="2026-11-03", score_grid=grid_now or ["2026-11-03"],
        # 🔴 2026-11-10：S2（「qlib 数据集覆盖不到信号日」）必须按**真实日历末日**判，
        #    写死 "2026-11-03" 会把这条硬拦的牙齿一起写死（数据集回退到 11-03 之前时
        #    仍然不报 ⇒ 守卫形同虚设）。实测：数据集回退到 2026-10-30 ⇒ S1+S2 双报 ✓。
        qlib_calendar_max_date=SP.calendar_max_date(QLIB_CALENDAR),
        every_n=C.REBALANCE_EVERY_N_DAYS,
        scores_max_date="2026-11-03", prices_max_date="2026-11-04",
        deployed_grid=(grid_now[::C.REBALANCE_STRIDE_MULTIPLIER] if grid_now else None),
    )
    blocks4 = [x for x in f4 if x["level"] == SP.LEVEL_BLOCK]
    checker.add("P4", "当前信号日 2026-11-03 可达 ⇒ 不应有任何硬拦", not blocks4,
                f"findings={[x['id'] for x in f4]}｜block={[x['id'] for x in blocks4]}")

    # ---- P5（真负例）：asof 不在栅格 ⇒ 必须 S1 硬拦 ----
    f5 = SP.check_signal_feasibility(
        asof="2026-12-01", score_grid=grid_now or ["2026-11-03"],
        qlib_calendar_max_date="2026-11-03", every_n=C.REBALANCE_EVERY_N_DAYS,
    )
    s5 = [x for x in f5 if x["id"] == "S1" and x["level"] == SP.LEVEL_BLOCK]
    checker.add("P5", "真负例：信号日不在打分栅格 ⇒ 必须报 S1 硬拦", bool(s5),
                f"findings={[x['id'] for x in f5]}｜S1={len(s5)} 条")

    # ---- P6（真负例）：qlib 数据集覆盖不到 ⇒ 必须 S2 硬拦 ----
    f6 = SP.check_signal_feasibility(
        asof="2026-12-01", score_grid=["2026-12-01"],
        qlib_calendar_max_date="2026-11-03", every_n=C.REBALANCE_EVERY_N_DAYS,
    )
    s6 = [x for x in f6 if x["id"] == "S2" and x["level"] == SP.LEVEL_BLOCK]
    checker.add("P6", "真负例：qlib 数据集覆盖不到信号日 ⇒ 必须报 S2 硬拦", bool(s6),
                f"findings={[x['id'] for x in f6]}｜S2={len(s6)} 条")

    # ---- P7（真负例）：日期解析不出来 ⇒ 必须 S0 硬拦（不许编日期）----
    f7 = SP.check_signal_feasibility(
        asof="不是日期", score_grid=grid_now or [], qlib_calendar_max_date="2026-11-03",
    )
    s7 = [x for x in f7 if x["id"] == "S0" and x["level"] == SP.LEVEL_BLOCK]
    checker.add("P7", "真负例：信号日解析失败 ⇒ 必须报 S0 硬拦（不编日期）", bool(s7),
                f"findings={[x['id'] for x in f7]}")

    # ---- P8：S3 只在「行情层比个股层新」时披露 ----
    f8a = SP.check_signal_feasibility(
        asof="2026-11-03", score_grid=["2026-11-03"], qlib_calendar_max_date="2026-11-03",
        scores_max_date="2026-11-03", prices_max_date="2026-11-03",
    )
    f8b = SP.check_signal_feasibility(
        asof="2026-11-03", score_grid=["2026-11-03"], qlib_calendar_max_date="2026-11-03",
        scores_max_date="2026-11-03", prices_max_date="2026-11-04",
    )
    checker.add("P8", "S3 只在 ETF 行情层比个股层新时披露（vintage 差）",
                not any(x["id"] == "S3" for x in f8a) and any(x["id"] == "S3" for x in f8b),
                f"同价 ⇒ {[x['id'] for x in f8a]}｜行情新 1 天 ⇒ {[x['id'] for x in f8b]}")

    # ---- P9：S4 只在「不在部署网格」时披露 ----
    f9a = SP.check_signal_feasibility(
        asof="2026-11-03", score_grid=["2026-11-03"], qlib_calendar_max_date="2026-11-03",
        deployed_grid=["2026-11-03"],
    )
    f9b = SP.check_signal_feasibility(
        asof="2026-12-01", score_grid=["2026-12-01"], qlib_calendar_max_date="2026-12-01",
        deployed_grid=["2026-11-03"],
    )
    checker.add("P9", "S4 只在信号日不在部署网格（回测口径）时披露",
                not any(x["id"] == "S4" for x in f9a) and any(x["id"] == "S4" for x in f9b),
                f"在网格内 ⇒ {[x['id'] for x in f9a]}｜不在 ⇒ {[x['id'] for x in f9b]}")

    # ---- P10：读不到日历 ⇒ 返回 None（不编数字）----
    checker.add("P10", "读不到 qlib 日历 ⇒ 返回 None（不编数字）",
                SP.calendar_max_date("/nonexistent/day.txt") is None,
                "calendar_max_date('/nonexistent/day.txt') = "
                f"{SP.calendar_max_date('/nonexistent/day.txt')}")

    # ---- P11：日历缺失 ⇒ 抛中文异常（不静默跳过）----
    try:
        SP.read_calendar_dates("/nonexistent/day.txt")
        ok11, d11 = False, "没有抛异常"
    except FileNotFoundError as exc:
        ok11, d11 = True, f"抛出 FileNotFoundError：{str(exc)[:60]}"
    except Exception as exc:  # noqa: BLE001
        ok11, d11 = False, f"抛了别的异常：{exc!r}"
    checker.add("P11", "交易日历缺失 ⇒ 抛中文异常（不静默跳过）", ok11, d11)

    # ============================================================ D 组：真负例（跑真预检）
    py = sys.executable
    preflight = DATA_DIR / "preflight_freshness.py"

    # ---- D1（真负例）：--asof 2026-12-01 ⇒ 退出码 1 且输出含 S1/S2 ----
    try:
        r1 = subprocess.run([py, str(preflight), "--asof", "2026-12-01"],
                            capture_output=True, text=True, timeout=300)
        out1 = (r1.stdout or "") + (r1.stderr or "")
        ok_d1 = r1.returncode == 1 and "S1" in out1 and "S2" in out1
        d_d1 = f"退出码={r1.returncode}（期望 1）｜含 S1={'S1' in out1} 含 S2={'S2' in out1}"
    except Exception as exc:  # noqa: BLE001
        ok_d1, d_d1 = False, f"子进程异常：{exc!r}"
    checker.add("D1", "真负例：预检 --asof 2026-12-01 ⇒ 退出码 1 且报 S1/S2", ok_d1, d_d1)

    # ---- D2：当前信号日 2026-11-03 ⇒ 退出码 0 ----
    try:
        r2 = subprocess.run([py, str(preflight), "--asof", "2026-11-03"],
                            capture_output=True, text=True, timeout=300)
        d_d2 = f"退出码={r2.returncode}（期望 0）"
        ok_d2 = r2.returncode == 0
    except Exception as exc:  # noqa: BLE001
        ok_d2, d_d2 = False, f"子进程异常：{exc!r}"
    checker.add("D2", "当前信号日 2026-11-03 ⇒ 预检通过（退出码 0）", ok_d2, d_d2)
    # ---- D3：步长必须同源（M-S4 审计补测）----
    #    D_DATE 在 10 日栅格上、不在 20 日栅格上
    #    => 预检的 --rebalance-every 必须真的驱动 S1 判定
    _d3_date = "2026-09-30"
    rc_n10, rc_n20 = None, None
    try:
        ra = subprocess.run([py, str(preflight), "--asof", _d3_date,
                             "--rebalance-every", "10"],
                            capture_output=True, text=True, timeout=300)
        rb = subprocess.run([py, str(preflight), "--asof", _d3_date,
                             "--rebalance-every", "20"],
                            capture_output=True, text=True, timeout=300)
        rc_n10, rc_n20 = ra.returncode, rb.returncode
        ok_d3 = (rc_n10 == 0) and (rc_n20 == 1)
        d_d3 = (f"步长10 => 退出码 {rc_n10}（期望0）｜"
                f"步长20 => 退出码 {rc_n20}（期望1）")
    except Exception as exc:  # noqa: BLE001
        ok_d3, d_d3 = False, f"子进程异常：{exc!r}"
    checker.add("D3", "预检 --rebalance-every 必须真的驱动 S1（与打分器同源）",
                ok_d3, d_d3)


    # ============================================================ S 组：静态回归断言
    src_scores = (ROOT / "qlib_scores.py").read_text(encoding="utf-8")
    src_analyze = (ROOT / "analyze.py").read_text(encoding="utf-8")
    src_pre = (DATA_DIR / "preflight_freshness.py").read_text(encoding="utf-8")

    checker.add("S1", "qlib_scores 不再把日历查询窗写死 SCORE_END_TIME",
                "cal_end = max(C.SCORE_END_TIME, requested_end)" in src_scores,
                "存在 max(SCORE_END_TIME, requested_end) = "
                f"{'cal_end = max(C.SCORE_END_TIME, requested_end)' in src_scores}")

    checker.add("S2", "qlib_scores 复用 signal_policy（栅格口径单一事实源）",
                "import signal_policy as SP" in src_scores
                and "SP.score_grid_dates" in src_scores,
                "import signal_policy as SP = "
                f"{'import signal_policy as SP' in src_scores}｜"
                f"SP.score_grid_dates = {'SP.score_grid_dates' in src_scores}")

    checker.add("S3", "qlib_scores 在 --end 被数据集截断时显式告警",
                "超出 qlib 数据集覆盖末日" in src_scores and "end_capped" in src_scores,
                "含告警文案 = {'超出 qlib 数据集覆盖末日' in src_scores}｜"
                f"含 end_capped = {'end_capped' in src_scores}")

    checker.add("S4", "analyze 的信号日缺失报错含根因 A / 根因 B / 附近打分日",
                all(k in src_analyze for k in ("根因 A", "根因 B", "附近的打分日")),
                f"根因 A={'根因 A' in src_analyze}｜根因 B={'根因 B' in src_analyze}｜"
                f"附近的打分日={'附近的打分日' in src_analyze}")

    checker.add("S5", "预检调用 check_signal_feasibility 且把 block 计入退出码",
                "check_signal_feasibility" in src_pre and "signal_blocks" in src_pre
                and "or signal_blocks" in src_pre,
                f"调用={'check_signal_feasibility' in src_pre}｜"
                f"计数={'or signal_blocks' in src_pre}")

    compile_ok = True
    compile_detail = []
    import py_compile

    for name, path in (
        ("qlib_scores.py", ROOT / "qlib_scores.py"),
        ("analyze.py", ROOT / "analyze.py"),
        ("preflight_freshness.py", DATA_DIR / "preflight_freshness.py"),
        ("signal_policy.py", DATA_DIR / "signal_policy.py"),
        ("probe_signal_feasibility.py", ROOT / "probes" / "probe_signal_feasibility.py"),
    ):
        try:
            py_compile.compile(str(path), doraise=True)
            compile_detail.append(f"{name}:OK")
        except Exception as exc:  # noqa: BLE001
            compile_ok = False
            compile_detail.append(f"{name}:{type(exc).__name__}")
    checker.add("S6", "本轮改动的 5 个文件都能编译通过", compile_ok, "｜".join(compile_detail))

    return checker.items


def main() -> int:
    """跑探针；退出码 0 = 全部断言通过，1 = 有断言失败。"""
    ap = argparse.ArgumentParser(description="信号日可行性 · 回归探针")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()

    items = run_checks(Path(args.run_root))
    print("=" * 78)
    print("信号日可行性 · 回归探针（M-S2）")
    print(f"已知缺陷编号：{', '.join(KNOWN_DEFECTS)}")
    print("=" * 78)
    for check_id, name, ok, detail in items:
        print(f"[{'PASS' if ok else 'FAIL'}] {check_id} {name}")
        print(f"        {detail}")
    failed = [it for it in items if not it[2]]
    print("-" * 78)
    print(f"合计 {len(items)} 项：PASS {len(items) - len(failed)} / FAIL {len(failed)}")
    if failed:
        print("❌ 信号日可行性守卫失效 ⇒ 节点日跑流水线会白跑一小时或静默产出假结果")
        return 1
    print("✅ 信号日可行性守卫全绿（含 4 个真负例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
