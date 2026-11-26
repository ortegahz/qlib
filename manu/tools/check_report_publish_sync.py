#!/usr/bin/env python3
"""只读检查「**交付面报告**」与「**快照真值报告**」是否**逐字节一致**。

【为什么需要（本工具的诞生原因：2026-11-26 实测事故）】
`manu/etf_rotation/daily/main.py` 落盘时**把同一份 markdown 写两处**：
① `<RESULTS_ROOT>/reports/report_<YYYYMMDD>.md`（**交付面** ⇒ 用户读的那份）；
② `<快照>/report_<YYYYMMDD>.md`（**快照才是真值**，源码注释原文「快照里也留一份」）。
🔴 而 `manu/tools/regen_snapshot_derived.py`（**就地确定性重生成**）**只重写快照内**的派生件
⇒ **快照里的报告更新了，交付面那份没更新** ⇒ 两个面向出现**内容分叉**。

🔴 **实测后果（2026-11-25 那轮，11-26 修复后）**：
交付面那份仍是**修复前**的计划 ⇒ 写着「**买入 512890 2,800 股 @1.2550（3,514.00 元）**」
（**TA 从未点名的标的**）＋「**512800 维持**（🛡️ TA 一票否决 ⇒ 不卖）」；
而快照真值已修成「**买入 563020 4,300 股 @1.2290**」＋「**卖出 512800 2,000 股**」。
⇒ **人读的那份会引导下错单** ⇒ 这类分叉**必须机器化拦截**，不能靠人记得刷新。

【判定口径（🔴 判红只针对「**当前交付日**」—— 也就是用户即将照着下单的那一份）】
1. **交付快照** ＝ 快照目录里**同时**有 `final_rebalance_plan.json` **且**有 `execution_card_*.json`
   ⇒ 只有交付快照才代表「一份真的交付」。
   🔴 **stage-1/2 的半成品快照**（只刷了数据/打分，没有计划件与执行卡）**不参与判定**
   ⇒ 否则会把「最新快照」错认成「最新交付」，**制造假红**（2026-11-26 实测踩过：
   `snapshots/20261118_121403` 是 asof=11-17 的数据刷新半成品，若拿它当准，
   会把**真的一致**（11-17 发布面与真交付快照 `20261117_221846` 逐字节相同）报成不一致）。
2. **同一 asof 有多个交付快照** ⇒ 取 **`finalized_at` 最新**者（并列时取目录名更晚者）。
3. **当前交付日** ＝ 所有交付快照里 **asof 最大**者 ⇒ 🔴 **只有这一天的分叉判红**
   （它才是用户即将照着下单的那份）；**更早的交付日只披露、不判红**
   （历史发布面可能被后续重跑/重定稿取代 ⇒ 按「历史只增不改」**不回改**）。
4. 缺陷口径（**仅当前交付日**）：
   - **A 交付面与快照真值不一致**（md5 不同）⇒ 判红；
   - **B 交付快照内缺报告副本** ⇒ 判红；
   - **C 交付面缺报告**（快照有、交付面没有）⇒ 判红；
   - **D 交付面有报告但找不到对应交付快照** ⇒ **只披露、不判红**（历史/失败报告不在此列）。

【只读】
本脚本**只读**：不改、不写、不删任何被检查的文件。

【用法】
    python3 manu/tools/check_report_publish_sync.py
    python3 manu/tools/check_report_publish_sync.py --results-root <结果根> --json-out <落点>.json

【退出码】
    0 = 无缺陷；1 = 有缺陷；2 = 用法/输入错误（结果根不存在等）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_RESULTS_ROOT = "/mnt/data/siping/runs/etf_rotation_results"

#: 交付面报告文件名（只认「正式报告」；`report_<日期>_failed.md` 不匹配 ⇒ 天然豁免）
REPORT_NAME_RE = re.compile(r"^report_(\d{8})\.md$")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


@dataclass
class Defect:
    code: str
    detail: str


@dataclass
class Result:
    results_root: str
    current_asof: str = ""
    checked: int = 0
    defects: list[Defect] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.defects

    def as_dict(self) -> dict:
        return {
            "results_root": self.results_root,
            "current_asof": self.current_asof,
            "checked_pairs": self.checked,
            "defects": [{"code": d.code, "detail": d.detail} for d in self.defects],
            "notes": list(self.notes),
            "ok": self.ok,
        }


def _is_delivery_snapshot(snap: Path) -> bool:
    """**交付快照** ＝ 同时有计划件与执行卡。"""
    if not (snap / "final_rebalance_plan.json").is_file():
        return False
    return any(snap.glob("execution_card_*.json"))


def _collect_snapshots(snapshots_dir: Path) -> tuple[dict[str, tuple[Path, str]], list[str]]:
    """返回（每个 asof 的最新**交付**快照 ``(快照目录, asof)``, 半成品快照说明列表）。"""
    deliveries: dict[str, tuple[str, Path, str]] = {}
    partials: list[str] = []
    if not snapshots_dir.is_dir():
        return {}, partials
    for snap in sorted(p for p in snapshots_dir.iterdir() if p.is_dir()):
        manifest = snap / "snapshot_manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        asof = str(data.get("asof") or "")
        if not asof:
            continue
        key = asof.replace("-", "")
        if not _is_delivery_snapshot(snap):
            partials.append(f"{snap.name}（asof={asof}，无计划件/执行卡 ⇒ 半成品快照，不参与判定）")
            continue
        finalized = str(data.get("finalized_at") or "")
        current = deliveries.get(key)
        if current is None or (finalized, snap.name) > (current[0], current[1].name):
            deliveries[key] = (finalized, snap, asof)
    return {k: (v[1], v[2]) for k, v in deliveries.items()}, partials


def analyze(results_root: str) -> Result:
    result = Result(results_root=results_root)
    root = Path(results_root)
    reports_dir = root / "reports"
    snapshots_dir = root / "snapshots"
    if not root.is_dir():
        raise FileNotFoundError(f"结果根不存在：{root}")

    published: dict[str, Path] = {}
    if not reports_dir.is_dir():
        result.notes.append(f"ℹ️ 交付面目录不存在：{reports_dir} ⇒ 全部交付日都判「交付面缺报告」")
    else:
        for path in sorted(reports_dir.glob("report_*.md")):
            match = REPORT_NAME_RE.match(path.name)
            if match:
                published[match.group(1)] = path

    deliveries, partials = _collect_snapshots(snapshots_dir)
    for note in partials:
        result.notes.append(f"ℹ️ {note}")

    if not deliveries:
        result.notes.append("ℹ️ 没有找到任何交付快照（无计划件＋执行卡）⇒ 无可判定对")
    else:
        current_key = max(deliveries)
        result.current_asof = deliveries[current_key][1]

    for key, (snap, _asof) in sorted(deliveries.items()):  # noqa: B007
        snap_report = snap / f"report_{key}.md"
        pub = published.get(key)
        is_current = key == current_key
        if not snap_report.is_file():
            detail = f"交付快照内缺报告副本：{snap_report}"
            (result.defects if is_current else result.notes).append(
                Defect("B", detail) if is_current else f"ℹ️ {detail}"
            )
            continue
        if pub is None:
            detail = (f"交付面缺报告：{reports_dir / f'report_{key}.md'}"
                      f"（交付快照 {snap.name} 有该日真值报告）")
            if is_current:
                result.defects.append(Defect("C", detail))
            else:
                result.notes.append(f"ℹ️ {detail}")
            continue
        pub_md5, snap_md5 = _md5(pub), _md5(snap_report)
        if pub_md5 != snap_md5:
            detail = (f"交付面与快照真值不一致：交付面 {pub}（md5={pub_md5}）"
                      f" ≠ 快照 {snap_report}（md5={snap_md5}）")
            if is_current:
                result.defects.append(Defect("A", detail))
            else:
                result.notes.append(
                    f"ℹ️ 历史交付日分叉（**不判红**：只披露；历史不回改）：{detail}"
                )
            continue
        result.checked += 1

    for key, pub in sorted(published.items()):
        if key not in deliveries:
            result.notes.append(f"ℹ️ 交付面有报告但找不到对应交付快照（不判红）：{pub}")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查交付面报告与快照真值报告是否逐字节一致")
    parser.add_argument("--results-root", default=DEFAULT_RESULTS_ROOT, help="结果根（默认服务器结果根）")
    parser.add_argument("--json-out", default="", help="JSON 落点路径（可省略）")
    args = parser.parse_args(argv)

    try:
        result = analyze(args.results_root)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[ERROR] 读取失败：{exc}", file=sys.stderr)
        return 2

    for note in result.notes:
        print(note)
    for defect in result.defects:
        print(f"[FAIL] {defect.code} {defect.detail}")
    print(
        f"合计：当前交付日 {result.current_asof or '—'}｜判定一致 {result.checked} 对"
        f"｜缺陷 {len(result.defects)} 个｜结果根 {result.results_root}"
    )

    if args.json_out:
        payload = result.as_dict()
        payload["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
