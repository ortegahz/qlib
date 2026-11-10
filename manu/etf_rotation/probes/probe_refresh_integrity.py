#!/usr/bin/env python
"""刷新链路 · 策略与输入完整性回归探针（M-S1，2026-11-05 05:4x）。

【为什么要有这道闸】
M-S1 实测出**五个刷新链路缺陷**（全部会让「到节点日刷新」变成**静默空转或静默损坏**）：

===== ============================== ==========================================
编号  缺陷                            实测后果
===== ============================== ==========================================
F1    `fetch_etf_prices.py` 默认区间  ``DEFAULT_END="20261104"`` **硬编码**
      结束日钉死                    ⇒ 换个日子跑也抓不到新日期 ⇒ 0 新行
F2    ``--resume`` 按「code 在文件   ⇒ 到节点日**全部跳过** ⇒ 0 新行
      里就跳过」                      ⇒ 新鲜度预检照样 die，用户看不出原因
F2'   `topup_etf_prices.py` 同上     ⇒ 159929 永远补不到新数据
F2''  `fetch_etf_holdings.py` 同上   ⇒ W 成分快照永远停在旧日期
F3    `fetch_etf_universe.py` 的     ⇒ 流动性 / 规模口径永远用旧快照
      spot 按「文件存在就跳过」
F5    预检给的刷新命令带 ``--out``   ⇒ 该参数**根本不存在** ⇒ 用户照抄 argparse
      （不存在）                      报错退出码 2 ⇒ 仍然卡死
F6    W 快照滞后只看 **max**         ⇒ 刷新部分失败造成「混合快照日期」时
                                     ⇒ 滞后天数被**低估**（披露偏乐观）
===== ============================== ==========================================

【口径】
* **硬拦 vs 只披露必须分清**（§十四.77）：I1 / I2 / I3 硬拦；I3b / I4 / I5 只披露。
* **必须带真负例**：只测「正例 PASS」等于没测（§十四.36）⇒ 本探针含 **8 个真负例**
  （P5 / P8 / P10 / P12 / D5 / D6 / D7 / D8）⇒ 逐个把数据改坏 ⇒ 断言必须**真的报出来**。
* **静态回归断言**（S 组）：M-S1 的修复**不许被改回去**（结束日不许再钉死、
  resume 不许再按存在性跳过、提示命令不许再引用不存在的参数）。

【副作用】**只读**：全部检查都在内存副本上做，**不写任何文件**、不联网。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_refresh_integrity.py \\
        [--run-root <落点根>/runs]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
if str(DATA_DIR) not in sys.path:
    sys.path.insert(0, str(DATA_DIR))

import config as C  # noqa: E402
from refresh_policy import (  # noqa: E402
    check_dataset_integrity,
    codes_needing_holdings_refresh,
    codes_needing_price_refresh,
    holdings_snapshot_dates,
    resolve_end_date,
    spot_snapshot_is_stale,
)

#: 本轮实测出的缺陷编号（写进探针输出，方便对着 memory 核）
KNOWN_DEFECTS = ("F1", "F2", "F2'", "F2''", "F3", "F5", "F6")


class Checker:
    """极简断言收集器：记录 (id, 名称, 是否通过, 细节)。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str) -> None:
        self.items.append((check_id, name, bool(ok), detail))


def _read(path: Path) -> pd.DataFrame:
    """读一张表；读不到 ⇒ 空表（让断言自己失败，不编数字）。"""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"code": str, "etf_code": str})
    except Exception:
        return pd.DataFrame()


def _ids(problems: list[dict]) -> set[str]:
    """从体检问题列表里取出编号集合。"""
    return {str(p.get("id")) for p in problems}


def run_checks(run_root: Path) -> list[tuple[str, str, bool, str]]:
    """跑全部断言；返回 (id, 名称, 是否通过, 细节) 列表。"""
    checker = Checker()

    # ================= P 组：策略纯函数（正例 + 真负例） =================
    checker.add(
        "P1", "resolve_end_date 留空 ⇒ 解析成今天（不钉死）",
        resolve_end_date(None, today=date(2026, 11, 5)) == "20261105",
        f"got={resolve_end_date(None, today=date(2026, 11, 5))}",
    )
    checker.add(
        "P2", "resolve_end_date 留空 + 交易日历 ⇒ 取今天之前（含）的最近交易日",
        resolve_end_date(None, today=date(2026, 12, 1),
                         calendar_dates=["20261130", "20261201"]) == "20261201",
        f"got={resolve_end_date(None, today=date(2026, 12, 1), calendar_dates=['20261130', '20261201'])}",
    )
    checker.add(
        "P3", "resolve_end_date 今天非交易日 ⇒ 退回最近交易日",
        resolve_end_date(None, today=date(2026, 12, 1),
                         calendar_dates=["20261130"]) == "20261130",
        f"got={resolve_end_date(None, today=date(2026, 12, 1), calendar_dates=['20261130'])}",
    )
    checker.add(
        "P4", "resolve_end_date 显式值优先",
        resolve_end_date("20261104") == "20261104",
        f"got={resolve_end_date('20261104')}",
    )

    px_small = pd.DataFrame({
        "date": ["2026-11-04", "2026-12-01"],
        "code": ["111111", "222222"],
    })
    need, skip = codes_needing_price_refresh(px_small, ["111111", "222222"], "20261201")
    checker.add(
        "P5", "真负例：最新日落后的 code 必须**重抓**（旧口径会跳过 ⇒ 静默空转）",
        "111111" in need,
        f"need={need}｜skip={skip}",
    )
    checker.add(
        "P6", "最新日已达目标日的 code 才允许跳过",
        "222222" in skip and "222222" not in need,
        f"need={need}｜skip={skip}",
    )
    need3, skip3 = codes_needing_price_refresh(px_small, ["999999"], "20261201")
    checker.add(
        "P7", "文件里根本没有的 code ⇒ 必须抓",
        "999999" in need3 and "999999" not in skip3,
        f"need={need3}｜skip={skip3}",
    )

    h_small = pd.DataFrame({
        "etf_code": ["333333", "444444"],
        "fetch_date": ["2026-11-04", "2026-11-05"],
    })
    hneed, hskip = codes_needing_holdings_refresh(h_small, ["333333", "444444"], "20261105")
    checker.add(
        "P8", "真负例：快照日期早于今天的 code 必须重抓（旧口径会跳过 ⇒ W 快照永不更新）",
        "333333" in hneed,
        f"need={hneed}｜skip={hskip}",
    )
    checker.add(
        "P9", "快照已是今天的 code 才允许跳过",
        "444444" in hskip and "444444" not in hneed,
        f"need={hneed}｜skip={hskip}",
    )

    spot_small = pd.DataFrame({"数据日期": ["2026-11-04"], "更新时间": ["2026-11-04 15:00:00"]})
    stale, note = spot_snapshot_is_stale(spot_small, "20261105")
    checker.add("P10", "真负例：快照数据日期早于目标日 ⇒ 必须重拉", stale, f"stale={stale}｜{note}")
    spot_fresh = pd.DataFrame({"数据日期": ["2026-11-05"], "更新时间": ["2026-11-05 15:00:00"]})
    stale2, note2 = spot_snapshot_is_stale(spot_fresh, "20261105")
    checker.add("P11", "快照数据日期已达目标日 ⇒ 不重拉", not stale2, f"stale={stale2}｜{note2}")

    mixed = holdings_snapshot_dates(h_small)
    checker.add(
        "P12", "真负例：混合快照日期必须被识别（只看 max 会低估滞后）",
        bool(mixed["mixed"]) and mixed["min"] == "2026-11-04" and mixed["max"] == "2026-11-05"
        and mixed["n_stale"] == 1,
        f"mixed={mixed}",
    )
    uniform = holdings_snapshot_dates(pd.DataFrame({
        "etf_code": ["333333", "444444"], "fetch_date": ["2026-11-05", "2026-11-05"],
    }))
    checker.add(
        "P13", "日期一致的快照 ⇒ mixed=False",
        not uniform["mixed"] and uniform["n_stale"] == 0,
        f"mixed={uniform}",
    )

    # ================= D 组：真实数据集 + 真负例 =================
    prices = _read(Path(C.ETF_PRICES_CSV))
    universe = _read(Path(C.ETF_UNIVERSE_CSV))
    holdings = _read(Path(C.ETF_HOLDINGS_CSV))
    checker.add(
        "D0", "三张输入表都读得到",
        not prices.empty and not universe.empty and not holdings.empty,
        f"prices={len(prices)} 行｜universe={len(universe)} 行｜holdings={len(holdings)} 行",
    )
    probs = check_dataset_integrity(prices, universe, holdings, target_date="2026-11-03")
    blocks = [p for p in probs if p["level"] == "block"]
    checker.add(
        "D1", "真实数据集：**无硬拦项**（池内不缺行情 / 无重复行 / 历史未被截断）",
        not blocks,
        f"硬拦 {len(blocks)} 项｜披露 {len(probs) - len(blocks)} 项",
    )
    checker.add(
        "D2", "真实数据集：行情 (date, code) 无重复行",
        int(prices.duplicated(subset=["date", "code"]).sum()) == 0,
        f"重复行={int(prices.duplicated(subset=['date', 'code']).sum())}",
    )
    pool_mask = universe["in_universe"].astype(str).str.lower().isin(["true", "1"])
    pool = set(universe.loc[pool_mask, "code"].astype(str).str.zfill(6))
    have = set(prices["code"].astype(str).str.zfill(6))
    checker.add(
        "D3", "真实数据集：池内标的全部有行情",
        not (pool - have),
        f"池内 {len(pool)} 只｜缺行情 {len(pool - have)} 只",
    )

    # D5 真负例：删掉池内某只的行情 ⇒ 必须报 I1
    victim = sorted(pool & have)[0] if (pool & have) else None
    if victim:
        broken = prices[prices["code"].astype(str).str.zfill(6) != victim]
        probs5 = check_dataset_integrity(broken, universe, holdings, target_date="2026-11-03")
        checker.add(
            "D5", "真负例：删掉池内某只的行情 ⇒ 必须报 I1（缺行情）",
            "I1" in _ids(probs5) and "I1" not in _ids(probs),
            f"受害者={victim}｜命中={sorted(_ids(probs5))}",
        )
    else:
        checker.add("D5", "真负例：删掉池内某只的行情 ⇒ 必须报 I1", False, "池内没有可用的行情 code")

    # D6 真负例：造一条重复行 ⇒ 必须报 I2
    if not prices.empty:
        dup = pd.concat([prices, prices.head(1)], ignore_index=True)
        probs6 = check_dataset_integrity(dup, universe, holdings, target_date="2026-11-03")
        checker.add(
            "D6", "真负例：行情出现重复 (date, code) ⇒ 必须报 I2",
            "I2" in _ids(probs6) and "I2" not in _ids(probs),
            f"命中={sorted(_ids(probs6))}",
        )
    else:
        checker.add("D6", "真负例：行情出现重复 (date, code) ⇒ 必须报 I2", False, "行情表为空")

    # D7 真负例：把行情截断到最近 30 天 ⇒ 必须报 I3
    if not prices.empty:
        cutoff = (pd.Timestamp(prices["date"].max()) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        truncated = prices[prices["date"] >= cutoff]
        probs7 = check_dataset_integrity(truncated, universe, holdings, target_date="2026-11-03")
        checker.add(
            "D7", "真负例：行情历史被短窗口截断 ⇒ 必须报 I3（MA120 / mom120 会失效）",
            "I3" in _ids(probs7) and "I3" not in _ids(probs),
            f"截断到 {cutoff}｜命中={sorted(_ids(probs7))}",
        )
    else:
        checker.add("D7", "真负例：行情历史被短窗口截断 ⇒ 必须报 I3", False, "行情表为空")

    # D8 真负例：把 W 快照某只的 fetch_date 改成旧日期 ⇒ 必须报 I5
    if not holdings.empty:
        h_broken = holdings.copy()
        first_code = h_broken["etf_code"].astype(str).str.zfill(6).iloc[0]
        h_broken.loc[h_broken["etf_code"].astype(str).str.zfill(6) == first_code, "fetch_date"] = "2026-01-01"
        probs8 = check_dataset_integrity(prices, universe, h_broken, target_date="2026-11-03")
        checker.add(
            "D8", "真负例：W 快照出现混合日期 ⇒ 必须报 I5（只看 max 会低估滞后）",
            "I5" in _ids(probs8) and "I5" not in _ids(probs),
            f"受害者={first_code}｜命中={sorted(_ids(probs8))}",
        )
    else:
        checker.add("D8", "真负例：W 快照出现混合日期 ⇒ 必须报 I5", False, "成分表为空")

    # D9：I4 披露必须点名 A 股权益型缺口（159382）
    i4 = [p for p in probs if p["id"] == "I4"]
    equity = i4[0].get("equity_cn_missing", []) if i4 else []
    checker.add(
        "D9", "池内无成分的标的里，**A 股权益型数据缺口**必须被点名",
        bool(equity),
        f"equity_cn_missing={equity[:10]}",
    )

    # ================= S 组：静态回归断言（修复不许被改回去） =================
    src_prices = (DATA_DIR / "fetch_etf_prices.py").read_text(encoding="utf-8")
    src_topup = (DATA_DIR / "topup_etf_prices.py").read_text(encoding="utf-8")
    src_hold = (DATA_DIR / "fetch_etf_holdings.py").read_text(encoding="utf-8")
    src_uni = (DATA_DIR / "fetch_etf_universe.py").read_text(encoding="utf-8")
    src_pre = (DATA_DIR / "preflight_freshness.py").read_text(encoding="utf-8")

    hard_past = re.compile(r'DEFAULT_END\s*=\s*"(\d{8})"')
    for sid, src, name in (("S1", src_prices, "fetch_etf_prices.py"),
                           ("S2", src_topup, "topup_etf_prices.py")):
        m = hard_past.search(src)
        ok = m is None
        checker.add(
            sid, f"{name}：默认结束日**不得**硬编码成某一天（必须动态解析）",
            ok,
            f"硬编码={'无' if ok else m.group(1)}",
        )

    checker.add(
        "S3", "fetch_etf_prices.py：resume 必须按「最新日」判定（不得按存在性跳过）",
        "codes_needing_price_refresh" in src_prices
        and 'set(existing["code"].unique())' not in src_prices,
        f"调用策略函数={'codes_needing_price_refresh' in src_prices}"
        f"｜旧存在性跳过残留={'set(existing[\"code\"].unique())' in src_prices}",
    )
    checker.add(
        "S4", "fetch_etf_holdings.py：resume 必须按「快照日期」判定（不得按存在性跳过）",
        "codes_needing_holdings_refresh" in src_hold
        and "set(existing[\"etf_code\"].astype(str).unique())" not in src_hold,
        f"调用策略函数={'codes_needing_holdings_refresh' in src_hold}",
    )
    checker.add(
        "S5", "fetch_etf_universe.py：spot 必须按「数据日期」判新鲜度（不得按存在性跳过）",
        "spot_snapshot_is_stale" in src_uni and "spot 已存在，跳过" not in src_uni,
        f"调用策略函数={'spot_snapshot_is_stale' in src_uni}",
    )
    checker.add(
        "S6", "preflight_freshness.py：提示命令不许引用不存在的参数（--out 不存在）",
        "--out " not in src_pre and "fetch_etf_prices.py --resume" in src_pre,
        f"含'--out '={'--out ' in src_pre}",
    )
    compile_ok = True
    compile_detail = []
    for name in ("fetch_etf_prices.py", "fetch_etf_universe.py", "fetch_etf_holdings.py",
                 "topup_etf_prices.py", "refresh_policy.py", "preflight_freshness.py"):
        import py_compile
        try:
            py_compile.compile(str(DATA_DIR / name), doraise=True)
            compile_detail.append(f"{name}:OK")
        except Exception as exc:  # noqa: BLE001
            compile_ok = False
            compile_detail.append(f"{name}:{type(exc).__name__}")
    checker.add("S7", "六个刷新相关脚本都能编译通过", compile_ok, "｜".join(compile_detail))

    return checker.items


def main() -> int:
    """跑探针；退出码 0 = 全部断言通过，1 = 有断言失败。"""
    ap = argparse.ArgumentParser(description="刷新链路 · 策略与输入完整性回归探针")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()

    items = run_checks(Path(args.run_root))
    print("=" * 78)
    print("刷新链路 · 策略与输入完整性回归探针（M-S1）")
    print(f"已知缺陷编号：{', '.join(KNOWN_DEFECTS)}")
    print("=" * 78)
    for check_id, name, ok, detail in items:
        print(f"[{'PASS' if ok else 'FAIL'}] {check_id} {name}")
        print(f"        {detail}")
    failed = [it for it in items if not it[2]]
    print("-" * 78)
    print(f"合计 {len(items)} 项：PASS {len(items) - len(failed)} / FAIL {len(failed)}")
    if failed:
        print("❌ 刷新链路 / 输入完整性失效 ⇒ 节点日刷新会静默空转或静默损坏")
        return 1
    print("✅ 刷新链路与输入完整性全绿（含 8 个真负例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
