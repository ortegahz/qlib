#!/usr/bin/env python3
"""probe_snapshot_lookahead.py —— **直接断言「本轮运行生成的快照目录」**的结构有效性与无未来函数。

对应用户架构正本 §三 第 2 条的**剩余项**（见 `manu/docs/snapshot_architecture.md §12.2`）：
把「断言对象」从**产物根里的旧件**（`<落点根>/runs/etf_analysis_report.json`）
换成**当前运行生成的快照目录**（`snapshots/<YYYYMMDD_HHMMSS>/`）。

🔴 **为什么这条探针存在（不是重复 probe_lookahead）**：
`probe_lookahead` 断言的是**研究链基线**（固定 asof=2026-11-03 的产物根件）⇒ 它守的是
「基线没被人改坏」；本探针断言的是**快照自包含 + 快照内没有未来函数**⇒ 它守的是
「每天那次真跑出来的目录本身是干净的」。**两者守的不是同一件事**。

正例断言（全部对着快照里的真文件）：
  A1 报告内部三处 asof 必须一致（report.meta / lookahead_audit / inputs），
     **且** 分析日 ≤ 业务日（report.meta.asof ≤ manifest.asof）
     🔴 manifest.asof = **业务日**（用户给的 --asof）｜report.meta.asof = **分析日**
     （数据链实际算到的交易日）⇒ 两者**本就不必相等**（例如业务日 11-05 / 分析日 11-03），
        真正的不变式是「报告内部自洽 ＋ 分析日不晚于业务日」。
  A2 决策前视只能是 **0 项或 1 项**，且台账逐条自洽（🔴 **不许**写死「恰 1 项」——
     W 快照日晚于 asof 时才有 1 项；asof 推过快照日就是 0 项 ⇒ 写死会误红）
  A3 决策前视**有则唯一且必是** W 成分快照（etf_holdings.csv，有效日 > asof）；无则 blocking 必须为空
  A4 台账自洽：逐条 look_ahead=True 的条数 == n_look_ahead
  A5 非前视决策项的**实际消费日** ≤ asof（唯一例外 = **已实测惰性**的 avg_amount_20d 兜底）
  A6 W 快照**若晚于 asof** ⇒ 必须被标成**结构性不可修**（只有 1 个快照日 + 给了不可观测理由）；
     不晚于 asof ⇒ 根本不构成前视（不算违规）
  A7 风险披露含 `w_snapshot_bias` 且带**实测数字**（不许写「不可量化」）
  A8 快照**自包含**：manifest 登记的件都在、无符号链接、报告就在快照里
  A9 快照内**行情文件的最晚日期 ≤ asof**（快照里没有未来行）

负例自检（每个负例都必须让对应断言转红 ⇒ 断言不许是「空过」）：
  N1 把 w_snapshot.fixable 改成 True      ⇒ A6 必须 FAIL
  N2 把唯一前视项的 effective_date 改成 ≤ asof ⇒ A3 必须 FAIL
  N3 把 manifest.asof 改成与报告不一致      ⇒ A1 必须 FAIL
  N4 删掉 w_snapshot_bias 风险披露           ⇒ A7 必须 FAIL
  N5 把 n_look_ahead 改成 0                  ⇒ A2/A4 必须 FAIL

退出码：0 = 全过；1 = 有断言失败；**2 = 不适用**（找不到真实快照 ⇒ **不算「通过」**）。

用法：
    python manu/etf_rotation/probes/probe_snapshot_lookahead.py \
        [--snapshot /path/to/snapshots/<run_id>] [--snapshot-root /path/to/snapshots]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DAILY = _HERE.parent / "daily"
for _p in (_DAILY,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import daily_config as DC  # noqa: E402  （落点常量单一事实源）

REPORT_NAME = "etf_analysis_report.json"
MANIFEST_NAME = "snapshot_manifest.json"
DEFAULT_ROOT = Path(
    os.environ.get("ETF_ROTATION_SNAPSHOT_ROOT",
                   str(DC.SNAPSHOT_ROOT))
)
#: 唯一被承认的「非前视项越界」例外：成交额兜底（其合法性靠**实测惰性**兜住）
FALLBACK_KEY = "avg_amount_20d"
#: 唯一允许的决策前视项：W 成分快照（结构性不可观测）
LOOKAHEAD_KEY = "etf_holdings.csv"

_RESULTS: list[tuple[str, bool, str]] = []
_QUIET = False


def add(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _RESULTS.append((name, ok, detail))
    if not _QUIET:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


# =============================================================================
# 断言（分「只读 dict」与「读快照文件」两半 ⇒ 负例自检可以只改 dict）
# =============================================================================
def evaluate_dicts(manifest: dict, report: dict) -> dict[str, bool]:
    la = report.get("lookahead_audit") or {}
    inputs = la.get("inputs") or {}
    entries = inputs.get("entries") or []
    blocking = inputs.get("blocking") or []
    wsnap = la.get("w_snapshot") or {}
    meta = report.get("meta") or {}

    asof_manifest = str(manifest.get("asof") or "")
    asof_report = str(meta.get("asof") or "")
    asof_la = str(la.get("asof") or "")
    asof_inputs = str(inputs.get("asof") or "")

    res: dict[str, bool] = {}
    res["A1"] = (
        bool(asof_manifest)
        and bool(asof_report)
        and asof_report == asof_la == asof_inputs
        and asof_report <= asof_manifest
    )
    add("A1 报告内部三处 asof 一致（report.meta / 前视审计 / inputs），且分析日 ≤ 业务日（manifest.asof）",
        res["A1"],
        f"业务日(manifest)={asof_manifest}｜分析日(report.meta)={asof_report}｜"
        f"la={asof_la}｜inputs={asof_inputs}")

    n_la = inputs.get("n_look_ahead")
    n_flag = sum(1 for e in entries if e.get("look_ahead") is True)
    res["A2"] = n_la in (0, 1) and n_flag == n_la
    add("A2 决策前视只能是 **0 项或 1 项**，且台账逐条自洽（n_look_ahead == 逐条 True 的条数）",
        res["A2"], f"n_look_ahead={n_la}｜逐条=True 的条数={n_flag}")

    if n_la == 1:
        res["A3"] = (
            len(blocking) == 1
            and LOOKAHEAD_KEY in str(blocking[0].get("name", ""))
            and str(blocking[0].get("effective_date", "")) > asof_inputs
        )
        detail = f"blocking={[(b.get('name'), b.get('effective_date')) for b in blocking]}｜asof={asof_inputs}"
    else:
        res["A3"] = len(blocking) == 0
        detail = f"n_look_ahead=0 ⇒ blocking 必须为空｜实际={blocking}"
    add("A3 决策前视**有则唯一且必是** W 成分快照（有效日 > asof）；无则 blocking 为空",
        res["A3"], detail)

    res["A4"] = n_flag == (n_la if isinstance(n_la, int) else -1)
    add("A4 台账自洽：逐条 look_ahead=True 的条数 == n_look_ahead", res["A4"],
        f"逐条=True 的条数={n_flag}｜n_look_ahead={n_la}")

    over = [e for e in entries
            if e.get("role") == "decision" and e.get("look_ahead") is not True
            and e.get("effective_date") and str(e["effective_date"]) > asof_inputs]
    bad_eff = [e.get("name") for e in over if FALLBACK_KEY not in str(e.get("name", ""))]
    only_fallback = (len(over) == 1 and FALLBACK_KEY in str(over[0].get("name", ""))) if over else True
    inert_ok = (inputs.get("liquidity_fallback") or {}).get("inert") is True
    res["A5"] = (not bad_eff) and only_fallback and inert_ok
    add("A5 非前视决策项的**实际消费日** ≤ asof（唯一例外 = 已实测惰性的成交额兜底）",
        res["A5"],
        f"越界项={bad_eff}｜越界项总数={len(over)}｜兜底惰性={inert_ok}")

    snap_date = wsnap.get("snapshot_date")
    if snap_date is not None and str(snap_date) > asof_inputs:
        res["A6"] = (
            wsnap.get("n_snapshot_dates") == 1
            and wsnap.get("fixable") is False
            and bool(str(wsnap.get("unobservable_reason") or "").strip())
        )
        detail = (f"快照日晚于 asof ⇒ 必须结构性不可修｜snapshot_date={snap_date}｜"
                  f"n_snapshot_dates={wsnap.get('n_snapshot_dates')}｜fixable={wsnap.get('fixable')}")
    else:
        res["A6"] = snap_date is not None
        detail = f"快照日={snap_date} ≤ asof={asof_inputs} ⇒ 不构成前视（不算违规）"
    add("A6 W 快照**若晚于 asof** ⇒ 必须被标成结构性不可修（只有 1 个快照日 + 不可观测理由）",
        res["A6"], detail)

    bias = [x for x in (report.get("risk_disclosure") or []) if x.get("id") == "w_snapshot_bias"]
    text = json.dumps(bias, ensure_ascii=False)
    res["A7"] = bool(bias) and "不可量化" not in text and len(text) > 60
    add("A7 风险披露含 `w_snapshot_bias` 且带**实测数字**（不许写「不可量化」）",
        res["A7"], f"条数={len(bias)}｜文本长度={len(text)}")

    return res


def evaluate_files(snapshot: Path, manifest: dict) -> dict[str, bool]:
    res: dict[str, bool] = {}
    artifacts = manifest.get("artifacts") or []
    names = [str(a.get("name") or "") for a in artifacts]
    missing = [n for n in names if n and not (snapshot / n).exists()]
    symlinks = [p for p in snapshot.rglob("*") if p.is_symlink()]
    res["A8"] = bool(names) and not missing and not symlinks \
        and (snapshot / REPORT_NAME).is_file()
    add("A8 快照**自包含**：manifest 登记的件都在、无符号链接、报告就在快照里",
        res["A8"],
        f"登记件={len(names)}｜缺失={missing[:5]}｜符号链接={len(symlinks)}")

    # A9：快照里的**行情**文件最晚日期 ≤ asof（快照里不许藏未来行）
    asof = str(manifest.get("asof") or "")
    later: list[str] = []
    checked: list[str] = []
    for csv_path in sorted(snapshot.glob("*.csv")):
        try:
            import pandas as pd  # noqa: PLC0415 —— 只在真要用时才 import
            df = pd.read_csv(csv_path, usecols=["date"])
        except Exception:  # noqa: BLE001 —— 没有 date 列 / 读不动 ⇒ 不算这一项
            continue
        if df.empty:
            continue
        checked.append(csv_path.name)
        max_date = str(df["date"].max())[:10]
        if max_date > asof:
            later.append(f"{csv_path.name}:{max_date}>{asof}")
    res["A9"] = not later
    add("A9 快照内行情文件的最晚日期 ≤ asof（快照里没有未来行）",
        res["A9"], f"已查={checked}｜越界={later}")
    return res


# =============================================================================
# 负例自检（自带夹具：改坏 dict ⇒ 对应断言必须转红）
# =============================================================================
def _set_path(obj: dict, path: list[str], value) -> None:
    cur = obj
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = value


def negative_selfcheck(manifest: dict, report: dict) -> list[tuple[str, bool, str]]:
    """每个负例都必须让对应断言转红（否则断言是「空过」）。"""
    out: list[tuple[str, bool, str]] = []
    # 🔴 负例自检会重复跑断言 ⇒ 必须**隔离**：① 静默（不刷屏）② 不污染正向汇总表
    global _QUIET
    saved_results = list(_RESULTS)
    saved_quiet = _QUIET
    _QUIET = True

    def _run(name: str, expected: list[str], mutate) -> None:
        m = copy.deepcopy(manifest)
        r = copy.deepcopy(report)
        mutate(m, r)
        _RESULTS.clear()
        res = evaluate_dicts(m, r)
        failed = [k for k, v in res.items() if not v]
        ok = all(k in failed for k in expected) and bool(failed)
        out.append((name, ok, f"转红断言={failed}｜期望至少 {expected}"))
        print(f"  [{'PASS' if ok else 'FAIL'}] 负例：{name} —— 转红={failed}｜期望 {expected}")

    def _n1(m, r):
        # 🔴 同时把快照日推到 asof 之后 ⇒ 保证「W 快照晚于 asof」这条分支**被走到**
        #    （否则在「asof 已推过快照日」的新快照上，这个负例会空过 ⇒ 断言无效）
        asof = str(r["lookahead_audit"]["inputs"]["asof"])
        _set_path(r, ["lookahead_audit", "w_snapshot", "snapshot_date"], "2099-01-01")
        _set_path(r, ["lookahead_audit", "w_snapshot", "fixable"], True)
        _set_path(r, ["lookahead_audit", "inputs", "n_look_ahead"], 1)
        _set_path(r, ["lookahead_audit", "inputs", "blocking"],
                  [{"name": LOOKAHEAD_KEY, "effective_date": "2099-01-01"}])
        _ = asof

    _run("N1 把 W 快照标成「可修」（并推到 asof 之后）⇒ A6 必须 FAIL", ["A6"], _n1)

    def _n2(m, r):
        _set_path(r, ["lookahead_audit", "inputs", "n_look_ahead"], 1)
        _set_path(r, ["lookahead_audit", "inputs", "blocking"], [
            {"name": LOOKAHEAD_KEY, "effective_date": str(r["lookahead_audit"]["inputs"]["asof"])}
        ])

    _run("N2 把唯一前视项的 effective_date 改成 ≤ asof ⇒ A3 必须 FAIL", ["A3"], _n2)

    _run("N3 把业务日改成早于分析日（manifest.asof=2000-01-01）⇒ A1 必须 FAIL",
         ["A1"], lambda m, r: m.__setitem__("asof", "2000-01-01"))

    def _n4(m, r):
        rd = [x for x in (r.get("risk_disclosure") or []) if x.get("id") != "w_snapshot_bias"]
        r["risk_disclosure"] = rd

    _run("N4 删掉 w_snapshot_bias 风险披露 ⇒ A7 必须 FAIL", ["A7"], _n4)

    _run("N5 把 n_look_ahead 改成 2（不可能的条数）⇒ A2/A4 必须 FAIL",
         ["A2", "A4"], lambda m, r: _set_path(r, ["lookahead_audit", "inputs", "n_look_ahead"], 2))

    _RESULTS.clear()
    _RESULTS.extend(saved_results)
    _QUIET = saved_quiet
    return out


def find_snapshot(root: Path) -> Path | None:
    """根目录下**最新**一个「有 manifest **且**有报告」的快照目录（没有 ⇒ None）。

    🔴 为什么必须同时要报告：``--dry-run`` 也会建快照（manifest 在、**报告不在**）
    ⇒ 只看 manifest 会把「最新的一次 dry-run」当成断言对象 ⇒ 明明有真快照却判「不适用」。
    ⇒ 选取口径 = **最新一个自带报告的快照**（dry-run 快照自动跳过）。
    """
    if not root.is_dir():
        return None
    candidates = [p for p in sorted(root.iterdir())
                  if p.is_dir() and (p / MANIFEST_NAME).is_file()
                  and (p / REPORT_NAME).is_file()]
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="快照结构 + 无未来函数闸门（断言对象 = 本轮快照）")
    ap.add_argument("--snapshot", default="", help="快照目录（缺省 = 取 root 下最新一个）")
    ap.add_argument("--snapshot-root", default=str(DEFAULT_ROOT), help="快照根目录")
    args = ap.parse_args(argv)

    snapshot = Path(args.snapshot) if args.snapshot else find_snapshot(Path(args.snapshot_root))
    if snapshot is None or not snapshot.is_dir():
        print(f"[不适用] 找不到真实快照（root={args.snapshot_root}）"
              "⇒ **本阶段不适用**（≠ 通过）")
        return 2
    manifest_path = snapshot / MANIFEST_NAME
    report_path = snapshot / REPORT_NAME
    if not manifest_path.is_file() or not report_path.is_file():
        print(f"[不适用] 快照 {snapshot} 缺 manifest 或报告 ⇒ 本阶段不适用（≠ 通过）")
        return 2

    print(f"== 快照结构 + 无未来函数闸门 ==")
    print(f"快照：{snapshot}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))

    evaluate_dicts(manifest, report)
    evaluate_files(snapshot, manifest)

    print("-- 负例自检（每个负例都必须让对应断言转红）--")
    negatives = negative_selfcheck(manifest, report)

    n_pass = sum(1 for _, ok, _ in _RESULTS if ok)
    n_fail = len(_RESULTS) - n_pass
    n_neg_ok = sum(1 for _, ok, _ in negatives if ok)
    print(f"== 结果：正向 {n_pass}/{len(_RESULTS)} PASS｜负例 {n_neg_ok}/{len(negatives)} 有效 ==")
    if n_fail or n_neg_ok != len(negatives):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
