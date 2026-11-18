"""报告「**内容指纹**」：判断两次分析报告的**实质内容**是否相同（不含运行环境/生成时间）。

【为什么需要（2026-11-04 夜实测，专题档 §21.4 的 F4）】
链路第一环原按「报告 **md 的原始 sha1**」判新鲜。而报告 md 里含「生成时间」行
⇒ **任何一次重生成都改 sha1** ⇒ 第一环必断 ⇒ 交付件被误判成
「🔴 LLM 第二意见已过期（stale）」⇒ 修它只能靠**重跑 TA**（**929 秒 + 配额**）。

但实测（专题档 §21.1）：喂给 LLM 的 `research_context` 是由报告 **JSON** 生成的**压缩版**，
**不含**「生成时间」行与指纹表 ⇒ 元数据变化时 **context md sha1 不变**（实测 `b473009e` 不变）
⇒ 旧决策在**实质上依然成立** ⇒ 第一环此时是**假阳性**。

⇒ 修法：第一环改比「报告 JSON 去掉**易变 meta** 后的规范化 sha1」（= **内容指纹**）。

【易变字段：只去这些，不多去】
- `meta.generated_at`（生成时间）
- `meta.host`（跑在哪台机器）
- `meta.python_version`（解释器版本）
- `meta.script`（脚本绝对路径）
- `meta.inputs`（输入文件指纹：路径/大小/mtime 每次都可能不同）

🔴 **不去** `meta.asof` / `meta.params` / `meta.hash_seed`：
口径变了（信号日/池口径/α 权重/均线窗口）就是**实质变化** ⇒ **必须**判 stale。
⇒ 这条「去哪些、不去哪些」的清单就是 F4 的安全边界，改它等于改守卫口径。

【向后兼容】
旧格式的 `ta_context_meta.json` 没有 `report_content_sha1` ⇒ 调用方**回落** raw md sha1
⇒ 老产物照常校验（不因为本模块上线就变成「不可校验」）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

#: 报告 JSON 里**易变**的 meta 键（计算内容指纹时剔除）
VOLATILE_META_KEYS: tuple[str, ...] = (
    "generated_at",
    "host",
    "python_version",
    "script",
    "inputs",
)


def canonical_report_bytes(report: dict) -> bytes:
    """报告 JSON 的**规范化字节**（剔除易变 meta 键；键序固定 ⇒ 跨进程可比）。

    🔴 只做**浅拷贝 + 换掉 meta** ⇒ 不修改调用方传进来的 dict。
    """
    payload = dict(report)
    meta = payload.get("meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        for key in VOLATILE_META_KEYS:
            meta.pop(key, None)
        payload["meta"] = meta
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def report_content_sha1(report: dict) -> str:
    """报告 JSON 的内容指纹（sha1 十六进制）。"""
    return hashlib.sha1(canonical_report_bytes(report)).hexdigest()


def report_content_sha1_from_file(path: str | Path) -> str | None:
    """从文件算内容指纹；文件缺失/损坏 ⇒ 返回 ``None``（**不编数字**）。

    返回 ``None`` 的语义 = 「**算不出来**」⇒ 调用方必须按「无法校验」处理，
    **不许**当成「一致」。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        report = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(report, dict):
        return None
    return report_content_sha1(report)
