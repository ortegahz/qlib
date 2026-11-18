"""可复现性探针：同一输入 + 同一代码 + ``PYTHONHASHSEED=0`` ⇒ 产物必须一致。

【用法】
    export PYTHONHASHSEED=0
    python manu/etf_rotation/probes/probe_reproducibility.py \
        --baseline <落点根>/runs \
        --rerun   <落点根>/runs/_repro_check \
        --out     <落点根>/runs/probe_reproducibility.json

【比什么】
- JSON：深比对，**只剔除**声明的易变键（默认 ``meta.generated_at`` / ``generated_at``）；
  其余任何差异都要报出「路径 + 基线值 + 重跑值」⇒ 差异即真问题，不许静默。
- MD：剔除「生成时间」行后逐行比对，输出首个不一致行号。
- CSV：逐行逐字段比对。

【路径归一化】
重跑目录与基线目录**位置不同** ⇒ 产物里记录的输入文件路径必然不同（同一份文件、
只是换了存放位置）。``--path-map 重跑目录=基线目录`` 把这类字符串差异归一化，
并单独统计为 ``path_only``（位置差异，**不是**计算差异）；归一化后仍有差异 ⇒ 真问题。

【复跑目录该放什么（2026-11-04 21:5x 实测踩坑；2026-11-05 00:0x 再修正）】
复跑目录必须只放「**外部输入**」：
    stock_scores.csv / stock_prices.csv（符号链接）、
    tradingagents_decision.md + .json（LLM 产出 = 外部输入）
**必须**放 ``ta_context_meta.json`` / ``ta_research_context.md``（**逐字节复制**）：
🔴 2026-11-05 05:5x（M-N 收口）**更正本段旧说明**（旧说明写在「链路第一环按报告 md
原始 sha1 判」的时代，M-G/M-F4 之后已改成**报告内容指纹** ⇒ 路径类易变项被剔除 ⇒
放三件套**不会**造成假阳性 stale）：
    · **不放** ⇒ sha1 链**缺件** ⇒ `final_rebalance_plan._llm_staleness` 走
      「缺件 ⇒ 无法校验 ⇒ stale=true」（M-F2 硬化）⇒ 计划里多出
      `llm_stale.stale=true` 与各条 `llm_conflicts[].stale` ⇒ 探针报「12 处实质差异」
      ⇒ **那是复跑目录放错导致的，不是不可复现**（本工程实测：放对 ⇒ 6/6 一致）。
    · **放了** ⇒ 第一环比「报告 JSON 内容指纹」（已剔除 ``meta.inputs`` 里的路径）
      ⇒ 与基线**逐位相同** ⇒ 不判 stale；第二环比「决策 json 里的 context_sha1
      vs ta_research_context.md 的 sha1」⇒ 逐字节复制即一致。
⇒ 另：命令行加 ``--path-map <复跑目录>=<基线目录>``（否则 md 里的路径字符串差异会被算成实质差异）。

🔴🔴 **也不要**放 ``etf_rotation_backtest*.json``（2026-11-05 00:0x 实测新坑）：
旧注释说「必须 cp -p 复刻隐式输入」，那是 **F3 修好之前**的状态。现在
``analyze.py::_load_backtest`` 在输出目录扫不到回测产物时会**回退到产物根目录**
⇒ 复跑目录里**不放**副本，报告引用的仍是官方路径 ⇒ 报告**内容指纹**与基线逐位相同。
反过来（把回测产物 cp 进复跑目录）的实测后果：
    回测路径被写进 ``risk_disclosure[0].detail`` ⇒ 内容指纹 8387ead7 → d28f8de7
    ⇒ sha1 链第一环判「LLM 意见已过期」⇒ 计划里多出 ``llm_conflicts[].stale``
    ⇒ 探针报 **4/6 一致、2 处实质差异**（final_rebalance_plan.json / .md）
⇒ **那不是不可复现，是流程放错**（详见 manu/memory/etf_rotation_20261104.md §35.14）。

【判定】
- ``identical``：全部产物在剔除易变键后**逐字节/逐值一致** ⇒ 可复现成立。
- ``differs``：存在实质差异 ⇒ 不可复现，差异清单即待修问题。

本探针**只读**，不修改任何产物。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

#: 默认剔除的易变键（JSON 路径，点号分隔）
DEFAULT_VOLATILE_JSON_KEYS = ("meta.generated_at", "generated_at")
#: MD 中含这些子串的行视为「生成时间」行，剔除后比对
DEFAULT_VOLATILE_MD_PATTERNS = ("生成时间", "generated_at")

JSON_ARTIFACTS = ("etf_analysis_report.json", "rebalance_recommendation.json",
                  "final_rebalance_plan.json")
MD_ARTIFACTS = ("etf_analysis_report.md", "final_rebalance_plan.md")
CSV_ARTIFACTS = ("rebalance_recommendation.csv",)


def _sha1_bytes(data: bytes) -> str:
    """字节流的 sha1（十六进制）。"""
    return hashlib.sha1(data).hexdigest()


def _sha1_file(path: Path) -> str:
    """文件的 sha1（十六进制）；文件不存在返回 None。"""
    if not path.exists():
        return None
    return _sha1_bytes(path.read_bytes())


def _drop_key(obj: Any, dotted: str) -> Any:
    """按点号路径删除一个键（就地返回新对象，不改动原对象）。"""
    parts = dotted.split(".")
    if isinstance(obj, dict) and len(parts) == 1:
        return {k: v for k, v in obj.items() if k != parts[0]}
    if isinstance(obj, dict) and parts:
        head, rest = parts[0], ".".join(parts[1:])
        if head in obj:
            new = dict(obj)
            new[head] = _drop_key(obj[head], rest)
            return new
    return obj


def _norm_obj(obj: Any, path_map: tuple[tuple[str, str], ...]) -> Any:
    """把字符串里的「重跑目录」替换成「基线目录」（递归；只动字符串，不动数值）。"""
    if isinstance(obj, str):
        for src, dst in path_map:
            obj = obj.replace(src, dst)
        return obj
    if isinstance(obj, dict):
        return {k: _norm_obj(v, path_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm_obj(v, path_map) for v in obj]
    return obj


def _norm_text(text: str, path_map: tuple[tuple[str, str], ...]) -> str:
    """文本版路径归一化。"""
    for src, dst in path_map:
        text = text.replace(src, dst)
    return text


def _diff_json(a: Any, b: Any, path: str = "$", out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """深比对两个 JSON 结构，返回差异清单（路径 / 基线值 / 重跑值）。"""
    if out is None:
        out = []
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append({"path": path, "baseline": repr(a)[:200], "rerun": repr(b)[:200]})
        return out
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append({"path": f"{path}.{key}", "baseline": "（缺）", "rerun": repr(b[key])[:200]})
            elif key not in b:
                out.append({"path": f"{path}.{key}", "baseline": repr(a[key])[:200], "rerun": "（缺）"})
            else:
                _diff_json(a[key], b[key], f"{path}.{key}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append({"path": f"{path}[len]", "baseline": len(a), "rerun": len(b)})
            return out
        for idx, (x, y) in enumerate(zip(a, b)):
            _diff_json(x, y, f"{path}[{idx}]", out)
    else:
        if a != b:
            out.append({"path": path, "baseline": repr(a)[:200], "rerun": repr(b)[:200]})
    return out


def _strip_md(lines: list[str], patterns: tuple[str, ...]) -> list[str]:
    """剔除含易变子串的行（生成时间等）。"""
    return [ln for ln in lines if not any(p in ln for p in patterns)]


def _diff_lines(a: list[str], b: list[str], label: str, max_show: int = 10) -> dict[str, Any]:
    """逐行比对两个文本，返回差异统计。"""
    if len(a) != len(b):
        first = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
        return {"label": label, "identical": False,
                "detail": f"行数不同：基线 {len(a)} 行 vs 重跑 {len(b)} 行；首个不同行号 {first + 1}",
                "n_diff_lines": abs(len(a) - len(b)),
                "samples": [{"line": first + 1, "baseline": a[first] if first < len(a) else "（缺）",
                             "rerun": b[first] if first < len(b) else "（缺）"}]}
    diffs = [(i, a[i], b[i]) for i in range(len(a)) if a[i] != b[i]]
    return {
        "label": label,
        "identical": not diffs,
        "detail": "逐行一致" if not diffs else f"{len(diffs)} 行不同",
        "n_diff_lines": len(diffs),
        "samples": [{"line": i + 1, "baseline": x, "rerun": y} for i, x, y in diffs[:max_show]],
    }


def compare_dirs(baseline: Path, rerun: Path,
                 volatile_json_keys: tuple[str, ...] = DEFAULT_VOLATILE_JSON_KEYS,
                 volatile_md_patterns: tuple[str, ...] = DEFAULT_VOLATILE_MD_PATTERNS,
                 path_map: tuple[tuple[str, str], ...] = ()) -> dict[str, Any]:
    """比对两个产物目录，返回结构化结论。"""
    results: list[dict[str, Any]] = []

    for name in JSON_ARTIFACTS:
        b_path, r_path = baseline / name, rerun / name
        item: dict[str, Any] = {"artifact": name, "kind": "json"}
        if not b_path.exists() or not r_path.exists():
            item.update({"identical": False,
                         "detail": f"文件缺失：基线存在={b_path.exists()} 重跑存在={r_path.exists()}"})
            results.append(item)
            continue
        b_obj = json.loads(b_path.read_text(encoding="utf-8"))
        r_obj = json.loads(r_path.read_text(encoding="utf-8"))
        b_norm, r_norm = b_obj, r_obj
        for key in volatile_json_keys:
            b_norm = _drop_key(b_norm, key)
            r_norm = _drop_key(r_norm, key)
        diffs_raw = _diff_json(b_norm, r_norm)
        diffs = _diff_json(_norm_obj(b_norm, path_map), _norm_obj(r_norm, path_map))
        item.update({
            "identical": not diffs,
            "detail": "剔除易变键后完全一致" if not diffs else f"{len(diffs)} 处实质差异",
            "n_diffs": len(diffs),
            "n_diffs_raw": len(diffs_raw),
            "n_path_only": len(diffs_raw) - len(diffs),
            "diffs": diffs[:20],
            "diffs_raw": diffs_raw[:20],
            "sha1_baseline": _sha1_file(b_path),
            "sha1_rerun": _sha1_file(r_path),
            "sha1_equal_raw": _sha1_file(b_path) == _sha1_file(r_path),
        })
        results.append(item)

    for name in MD_ARTIFACTS:
        b_path, r_path = baseline / name, rerun / name
        if not b_path.exists() or not r_path.exists():
            results.append({"artifact": name, "kind": "md", "identical": False,
                            "detail": f"文件缺失：基线存在={b_path.exists()} 重跑存在={r_path.exists()}"})
            continue
        b_lines = _strip_md(b_path.read_text(encoding="utf-8").splitlines(), volatile_md_patterns)
        r_lines = _strip_md(r_path.read_text(encoding="utf-8").splitlines(), volatile_md_patterns)
        item = _diff_lines(b_lines, r_lines, name)
        n_diff_lines_raw = item["n_diff_lines"]
        b_norm = _strip_md(_norm_text(b_path.read_text(encoding="utf-8"), path_map).splitlines(),
                           volatile_md_patterns)
        r_norm = _strip_md(_norm_text(r_path.read_text(encoding="utf-8"), path_map).splitlines(),
                           volatile_md_patterns)
        item_norm = _diff_lines(b_norm, r_norm, name)
        item["identical"] = item_norm["identical"]
        item["detail"] = item_norm["detail"]
        item["n_diff_lines"] = item_norm["n_diff_lines"]
        item["n_diff_lines_raw"] = n_diff_lines_raw
        item["samples"] = item_norm["samples"]
        item["n_path_only"] = n_diff_lines_raw - item_norm["n_diff_lines"]
        item.update({"artifact": name, "kind": "md",
                     "sha1_baseline": _sha1_file(b_path), "sha1_rerun": _sha1_file(r_path),
                     "sha1_equal_raw": _sha1_file(b_path) == _sha1_file(r_path)})
        results.append(item)

    for name in CSV_ARTIFACTS:
        b_path, r_path = baseline / name, rerun / name
        if not b_path.exists() or not r_path.exists():
            results.append({"artifact": name, "kind": "csv", "identical": False,
                            "detail": f"文件缺失：基线存在={b_path.exists()} 重跑存在={r_path.exists()}"})
            continue
        b_lines = b_path.read_text(encoding="utf-8-sig").splitlines()
        r_lines = r_path.read_text(encoding="utf-8-sig").splitlines()
        item = _diff_lines(b_lines, r_lines, name)
        n_diff_lines_raw = item["n_diff_lines"]
        b_norm = _norm_text(b_path.read_text(encoding="utf-8-sig"), path_map).splitlines()
        r_norm = _norm_text(r_path.read_text(encoding="utf-8-sig"), path_map).splitlines()
        item_norm = _diff_lines(b_norm, r_norm, name)
        item["identical"] = item_norm["identical"]
        item["detail"] = item_norm["detail"]
        item["n_diff_lines"] = item_norm["n_diff_lines"]
        item["n_diff_lines_raw"] = n_diff_lines_raw
        item["samples"] = item_norm["samples"]
        item["n_path_only"] = n_diff_lines_raw - item_norm["n_diff_lines"]
        item.update({"artifact": name, "kind": "csv",
                     "sha1_baseline": _sha1_file(b_path), "sha1_rerun": _sha1_file(r_path),
                     "sha1_equal_raw": _sha1_file(b_path) == _sha1_file(r_path)})
        results.append(item)

    n_same = sum(1 for r in results if r.get("identical"))
    return {
        "baseline_dir": str(baseline),
        "rerun_dir": str(rerun),
        "volatile_json_keys": list(volatile_json_keys),
        "volatile_md_patterns": list(volatile_md_patterns),
        "path_map": [list(pair) for pair in path_map],
        "n_artifacts": len(results),
        "n_identical": n_same,
        "identical": n_same == len(results),
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    """命令行参数。"""
    p = argparse.ArgumentParser(description="产物可复现性探针（只读）")
    p.add_argument("--baseline", required=True, help="基线产物目录")
    p.add_argument("--rerun", required=True, help="重跑产物目录")
    p.add_argument("--out", default=None, help="结论 JSON 输出路径")
    p.add_argument("--path-map", action="append", default=[],
                   help="路径归一化，形如 重跑目录=基线目录（可重复）；"
                        "归一化后仍有的差异才算实质差异")
    return p.parse_args()


def main() -> int:
    """主流程：比对 → 打印中文结论 → 落盘。"""
    args = parse_args()
    baseline, rerun = Path(args.baseline), Path(args.rerun)
    for d in (baseline, rerun):
        if not d.is_dir():
            raise SystemExit(f"[FATAL] 目录不存在：{d}")
    path_map: list[tuple[str, str]] = []
    for raw_item in args.path_map:
        if "=" not in raw_item:
            raise SystemExit(f"[FATAL] --path-map 格式应为 重跑目录=基线目录，收到 {raw_item!r}")
        src, dst = raw_item.split("=", 1)
        path_map.append((src, dst))
    verdict = compare_dirs(baseline, rerun, path_map=tuple(path_map))
    print("=" * 78)
    print(f"可复现性探针：基线 {baseline} ｜ 重跑 {rerun}")
    print(f"易变键（已剔除）：JSON {verdict['volatile_json_keys']} ｜ MD 含 {verdict['volatile_md_patterns']}")
    if path_map:
        print(f"路径归一化：{path_map}（只动字符串路径；归一化后仍有差异才算实质差异）")
    print("-" * 78)
    for r in verdict["results"]:
        flag = "✅" if r.get("identical") else "❌"
        raw = "（原始 sha1 亦相同）" if r.get("sha1_equal_raw") else "（原始 sha1 不同）"
        print(f"{flag} {r['artifact']:<32} {r.get('detail', '')} {raw}")
        for s in r.get("samples", [])[:3]:
            print(f"      行 {s.get('line')}: 基线={str(s.get('baseline'))[:80]!r} 重跑={str(s.get('rerun'))[:80]!r}")
        for d in r.get("diffs", [])[:3]:
            print(f"      {d['path']}: 基线={d['baseline']} 重跑={d['rerun']}")
    print("-" * 78)
    print(f"结论：{verdict['n_identical']}/{verdict['n_artifacts']} 个产物一致 ⇒ "
          f"{'可复现成立' if verdict['identical'] else '存在实质差异（不可复现）'}")
    print("=" * 78)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[INFO] 结论已写出 {out_path}")
    return 0 if verdict["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
