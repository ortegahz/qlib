#!/usr/bin/env python3
"""只读检查「**交付面报告**」与「**快照真值报告**」是否**逐字节一致**。

【为什么需要（本工具的诞生原因：2026-11-26 实测事故）】
`manu/etf_rotation/daily/main.py` 落盘时**把同一份 markdown 写两处**：
① `<RESULTS_ROOT>/reports/report_<YYYYMMDD>.md`（**交付面** ⇒ 用户读的那份）；
② `<快照>/report_<YYYYMMDD>.md`（**快照才是真值**，注释原文「快照里也留一份」）。
🔴 而 `manu/tools/regen_snapshot_derived.py`（**就地确定性重生成**）**只重写快照内**的派生件
⇒ **快照里的报告更新了，交付面那份没更新** ⇒ 两个面向出现**内容分叉**。

🔴 **实测后果（2026-11-25 那轮，11-26 修复后）**：
交付面那份仍是**修复前**的计划 ⇒ 写着「**买入 512890 2,800 股 @1.2550（3,514.00 元）**」
（**TA 从未点名的标的**）＋「**512800 维持**（🛡️ TA 一票否决 ⇒ 不卖）」；
而快照真值已修成「**买入 563020 4,300 股 @1.2290**」＋「**卖出 512800 2,000 股**」。
⇒ **人读的那份会引导下错单** ⇒ 这类分叉**必须机器化拦截**，不能靠人记得刷新。

【判定口径】
1. 交付面报告 = `<RESULTS_ROOT>/reports/report_<YYYYMMDD>.md`（**只看顶层**；
   `_superseded/` 下的是**历史作废件**，按「历史只增不改」不参与判定）。
2. 快照真值   = `<RESULTS_ROOT>/snapshots/<run_id>/report_<YYYYMMDD>.md`
   ⇒ 同一 asof 有多个快照时，取 **`finalized_at` 最新**的那个（⇒ 以最后收口的那次为准）。
3. 🔴 **必须逐字节一致**（不是「内容大致相同」）⇒ 用 md5 判。
4. 缺陷口径：
   - **A 交付面与快照真值不一致**（md5 不同）⇒ 判红；
   - **B 快照内缺报告副本** ⇒ 判红（快照是自洽单元，缺件即不完整）；
   - **C 交付面缺报告**（快照有、交付面没有）⇒ 判红；
   - **D 交付面有报告但找不到对应快照** ⇒ **只披露、不判红**（历史报告 / 失败报告不在此列）。

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
    checked: int = 0
    defects: list[Defect] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.defects

    def as_dict(self) -> dict:
        return {
            "results_root": self.results_root,
            "checked_pairs": self.checked,
            "defects": [{"code": d.code, "detail": d.detail} for d in self.defects],
            "notes": list(self.notes),
            "ok": self.ok,
        }


def _latest_snapshots(snapshots_dir: Path) -> dict[str, tuple[str, Path, str]]:
    """每个 asof（YYYYMMDD）取 ``finalized_at`` 最新的那个快照。"""
    latest: dict[str, tuple[str, Path, str]] = {}
    if not snapshots_dir.is_dir():
        return latest
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
        finalized = str(data.get("finalized_at") or "")
        key = asof.replace("-", "")
        current = latest.get(key)
        if current is None or finalized > current[0]:
            latest[key] = (finalized, snap, asof)
    return latest


def analyze(results_root: str) -> Result:
    result = Result(results_root=results_root)
    root = Path(results_root)
    reports_dir = root / "reports"
    snapshots_dir = root / "snapshots"
    if not root.is_dir():
        raise FileNotFoundError(f"结果根不存在：{root}")
    published: dict[str, Path] = {}
    if not reports_dir.is_dir():
        # 🔴 交付面目录整体缺失 **不许**当成「无可判定对」⇒ 一律当「交付面缺报告」（缺陷 C）
        result.notes.append(f"ℹ️ 交付面目录不存在：{reports_dir} ⇒ 全部快照报告都判「交付面缺报告」")
    else:
        for path in sorted(reports_dir.glob("report_*.md")):
            match = REPORT_NAME_RE.match(path.name)
            if match:
                published[match.group(1)] = path

    latest = _latest_snapshots(snapshots_dir)

    for key, (finalized, snap, asof) in sorted(latest.items()):
        snap_report = snap / f"report_{key}.md"
        pub = published.get(key)
        if not snap_report.is_file():
            result.defects.append(
                Defect("B", f"快照内缺报告副本：{snap_report}（asof={asof}）")
            )
            continue
        if pub is None:
            result.defects.append(
                Defect("C", f"交付面缺报告：{reports_dir / f'report_{key}.md'}"
                            f"（快照 {snap.name} 有 asof={asof} 的真值报告）")
            )
            continue
        result.checked += 1
        pub_md5, snap_md5 = _md5(pub), _md5(snap_report)
        if pub_md5 != snap_md5:
            result.defects.append(
                Defect(
                    "A",
                    f"交付面与快照真值不一致：交付面 {pub}（md5={pub_md5}）"
                    f" ≠ 快照 {snap_report}（md5={snap_md5}）",
                )
            )

    for key, pub in sorted(published.items()):
        if key not in latest:
            result.notes.append(f"ℹ️ 交付面有报告但找不到对应快照（不判红）：{pub}")

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
        f"合计：判定 {result.checked} 对｜缺陷 {len(result.defects)} 个"
        f"｜结果根 {result.results_root}"
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
