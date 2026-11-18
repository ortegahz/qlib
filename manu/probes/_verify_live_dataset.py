"""派生数据集（cn_data_live）验收：**只读**。

四项验收：
A1 派生目录的日历末日 == 目标交易日（2026-11-05）
A2 **qlib 真的能读回新交易日**（D.features ⇒ 不是「文件写进去了」而是「qlib 读得到」）
A3 bin 长度自洽（覆盖末日不得超过日历末日 ⇒ 不存在日历错位）
A4 **打分栅格末日例外生效** ⇒ S 层会打到目标交易日（`score_grid` 的末位 == 目标日）

运行（服务器，ql env）：
    /home/huangzhe/anaconda3/envs/ql/bin/python _verify_live_dataset.py --day 2026-11-05
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path("/tmp/pycharm_project_658d3fc4")
sys.path.insert(0, str(REPO / "manu" / "etf_rotation" / "daily"))
import daily_config as DC  # noqa: E402  （落点常量单一事实源）
import stage1_rolling_increment as SRI  # noqa: E402

LIVE = SRI.QLIB_CN_DATA_LIVE
BASE = DC.QLIB_CN_DATA


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-05")
    ap.add_argument("--live", default=str(LIVE))
    ap.add_argument("--sample", type=int, default=400, help="bin 自洽巡检抽样数（0=全量）")
    args = ap.parse_args()
    day = args.day
    live = Path(args.live)
    rc = 0

    print("=" * 72)
    print(f"A1 日历末日：基座 {SRI.read_calendar(BASE)[-1]} ⇒ 派生 {SRI.read_calendar(live)[-1] if live.exists() else '（派生目录不存在）'}")
    ok1 = live.exists() and SRI.read_calendar(live)[-1] == day
    print(f"   ⇒ {'✅' if ok1 else '❌'} 派生日历末日 == {day}")
    rc |= 0 if ok1 else 1

    print("\nA2 qlib 读回新交易日（D.features）")
    try:
        import qlib
        from qlib.constant import REG_CN
        from qlib.data import D

        qlib.init(provider_uri=str(live), region=REG_CN)
        probes = ["sh600000", "sz000001", "bj920000", "sz002142"]
        frame = D.features(probes, ["$close", "$volume", "$factor"], start_time=day, end_time=day)
        print(frame.to_string())
        ok2 = len(frame) > 0 and frame["$close"].notna().any()
        print(f"   ⇒ {'✅' if ok2 else '❌'} qlib 读得到 {day} 的个股特征")
        rc |= 0 if ok2 else 1
    except Exception as exc:  # noqa: BLE001
        print(f"   ❌ 读回失败：{type(exc).__name__}: {exc}")
        rc |= 1

    print("\nA3 bin 长度自洽巡检（覆盖末日不得超过日历末日）")
    audit = SRI.audit_bins(live, sample=args.sample or None)
    print(f"   {audit}")
    ok3 = audit["n_over"] == 0
    print(f"   ⇒ {'✅' if ok3 else '❌'} 超日历末日的 bin：{audit['n_over']} 条")
    rc |= 0 if ok3 else 1

    print("\nA4 打分栅格（末日例外 ⇒ S 层会打到目标日）")
    sys.path.insert(0, str(REPO / "manu" / "etf_rotation" / "daily"))
    import run_qlib_score as R

    provider, reason = R.select_provider(day)
    grid = R.score_grid(end=day)
    ok4 = bool(grid) and grid[-1] == day
    print(f"   provider = {provider}（{reason}）")
    print(f"   栅格末 5 个：{grid[-5:]}")
    print(f"   ⇒ {'✅' if ok4 else '❌'} 栅格末位 == {day}（⇒ `qlib_scores --end {day}` 会打到该日）")
    rc |= 0 if ok4 else 1

    print("=" * 72)
    print(f"验收结果：{'✅ 全通过' if rc == 0 else '❌ 有未通过项'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
