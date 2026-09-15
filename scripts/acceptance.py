"""SPINE §6 真实数据验收：**用被否掉的东西验收闸门**。

对 h5 跑今晚已验证的 4 个内置信号，全部必须 reject，并打印三个数供与今晚实测对照：
  今晚实测（不重叠 5 日窗口）：rev20 +0.0273/t1.03 · rev60 +0.0229/0.79 ·
  ma60dev +0.0310/1.06 · composite +0.0360/1.26；composite 组合净超额 −6.3%~−12.4%/年。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spine import dataset, gate  # noqa: E402

SPEC = {
    "universe": "trade",
    "train_end": "2024-12-31",
    "test": {"start": "2025-01-01", "end": "2026-07-15"},
    "windows": {"step_days": 5},
    "cost_model": {"round_trip_bps": 20, "hold_days": 5},
    "turnover_cap_annual": 6.0,
    "topn": 30, "universe_size": 120, "min_windows": 40,
    "lookback_days": 140, "baseline": "static_equal_weight_universe",
}


def main():
    h5 = os.environ.get("WB_H5", "/app/data/factor_mining/daily_pv_all.h5")
    close, vol = dataset.load_h5(h5, start="2024-01-01")
    print("  数据: %d 日 × %d 只" % (close.shape[0], close.shape[1]), flush=True)
    rows, bad = [], 0
    for name, fn in gate.SIGNALS.items():
        v = gate.evaluate(fn, SPEC, close, vol, batch_n=1, run_id="acc-" + name)
        print("  [%s] K=%d net_ic=%+.4f t_ic=%+.2f | 净超额=%+.2f%%/年 t_excess=%+.2f "
              "换手=%.1f → %s" % (
                  name, v["k_windows"], v["net_ic"], v["t_ic"],
                  100 * v["net_excess_annual"], v["t_excess"], v["turnover_annual"], v["decision"]),
              flush=True)
        if v["decision"] != "reject":
            bad += 1
        rows.append({"name": name, **{k: v[k] for k in (
            "k_windows", "net_ic", "t_ic", "net_excess_annual", "t_excess",
            "turnover_annual", "decision")}, "reasons": v["reasons"]})
    print(json.dumps({"all_rejected": bad == 0, "rows": rows}, ensure_ascii=False, indent=2))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
