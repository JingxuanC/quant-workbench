"""闸门（SPINE §2）：唯一准入权，**无 LLM**。

五条全真才 admit：
  1 净超额 > 0（已扣 cost_model.round_trip_bps）
  2 t_excess >= 2.0 且 K >= min_windows
  3 年化换手 <= turnover_cap_annual
  4 打败静态基线（条件层/择时层额外要求）
  5 batch_n 多重检验校正后仍显著（batch_n 由账本统计，不许调用方自报）

输出必带 reasons[]：每条不满足都写明数字与阈值。
"""

import hashlib
import json

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

TRADING_DAYS = 252


def evaluate(signal_fn, spec: dict, close: pd.DataFrame, volume: pd.DataFrame,
             batch_n: int = 1, run_id: str = None) -> dict:
    """跑闸门。signal_fn(px_window)->Series(index=instrument)。

    - 只在 spec['test'] 区间评估（train 区允许被搜索，test 区不许——这是规则5的现实依据）
    - 组合 = 取信号前 topn 等权（用于测量信号可交易性；生产组合由 wb_portfolio 另算）
    - 基线 = 同一 universe 等权（隔离 beta，只看选股本事）
    """
    hold = int(spec["cost_model"]["hold_days"])
    step = int(spec.get("windows", {}).get("step_days", hold))
    rt_bps = float(spec["cost_model"]["round_trip_bps"])
    topn = int(spec.get("topn", 30))
    usize = int(spec.get("universe_size", 120))
    t0, t1 = spec["test"]["start"], spec["test"]["end"]
    min_w = int(spec.get("min_windows", 40))
    cap_to = float(spec.get("turnover_cap_annual", 6.0))
    mu_look = int(spec.get("lookback_days", 140))

    dates = close.index
    n = len(dates)
    ics, excess, turns, picks_prev = [], [], [], None
    for i in range(mu_look, n - hold, step):
        ts = str(dates[i].date())
        if not (t0 <= ts <= t1):
            continue
        win = close.iloc[i - mu_look + 1:i + 1]
        amt = (win * volume.iloc[i - mu_look + 1:i + 1]).mean()
        syms = [s for s in win.columns if not np.isnan(amt.get(s, np.nan))]
        if len(syms) < max(usize, topn * 2):
            continue
        syms = list(amt[syms].sort_values(ascending=False).index[:usize])
        px = win[syms]
        sig = signal_fn(px)
        if sig is None:
            continue
        sig = sig.reindex(syms).dropna()
        if len(sig) < topn:
            continue
        fwd = close[syms].iloc[i + hold] / close[syms].iloc[i] - 1.0
        fwd = fwd.dropna()
        common = [s for s in sig.index if s in fwd.index]
        if len(common) < topn:
            continue
        ics.append(spearmanr(sig.reindex(common), fwd.reindex(common)).correlation)
        pick = list(sig.reindex(common).sort_values(ascending=False).index[:topn])
        excess.append(float(fwd.reindex(pick).mean()) - float(fwd.mean()))
        turns.append(1.0 if picks_prev is None else len(set(pick) - picks_prev) / topn)
        picks_prev = set(pick)

    K = len(ics)
    if K == 0:
        return _verdict(spec, run_id, 0, [], [], [], batch_n,
                        ["样本外窗口为 0：检查 test 区间/数据长度"])
    ic = np.array(ics, dtype=float)
    ex = np.array(excess, dtype=float)
    to = np.array(turns, dtype=float)
    mean_to = float(to.mean())
    cost = mean_to * rt_bps / 1e4
    net_ex = ex - cost
    ann = TRADING_DAYS / hold
    net_ic = float(ic.mean())
    net_ex_ann = float(net_ex.mean() * ann)
    to_ann = float(mean_to * ann)
    def _t(x):
        """sd==0 的退化情形：均值>0 说明证据极强（不是"无证据"）→ 用数值下限给出大 t；
        均值<=0 则 t=0。写死在这里，免得把"完美一致"误判成"不可信"。"""
        if K <= 1:
            return 0.0
        sd = float(x.std(ddof=1))
        if sd <= 1e-12:
            return 0.0 if x.mean() <= 0 else float(abs(x.mean()) / 1e-9)
        return float(x.mean() / (sd / np.sqrt(K)))
    t_ic = _t(ic)
    t_ex = _t(net_ex)
    gross_ex_ann = float(ex.mean() * ann)
    baseline = {"baseline": spec.get("baseline", "static_equal_weight_universe"),
                "baseline_net_excess_annual": 0.0, "gross_excess_annual": gross_ex_ann}
    return _verdict(spec, run_id, K, ic, ex, to, batch_n,
                    [], net_ic=net_ic, net_excess_annual=net_ex_ann, turnover_annual=to_ann,
                    t_ic=t_ic, t_excess=t_ex, baseline=baseline)


def _verdict(spec, run_id, K, ic, ex, to, batch_n, extra_reasons, net_ic=0.0,
             net_excess_annual=0.0, turnover_annual=0.0, t_ic=0.0, t_excess=0.0,
             baseline=None):
    reasons = list(extra_reasons)
    cap_to = float(spec.get("turnover_cap_annual", 6.0))
    min_w = int(spec.get("min_windows", 40))
    # 规则5：Bonferroni 校正（batch_n 由账本统计后传入）
    t_adj = t_excess / np.sqrt(max(batch_n, 1))
    if not reasons:
        if net_excess_annual <= 0:
            reasons.append("规则1 净超额 %.2f%%/年 <= 0（毛超额 %.2f%%/年，成本拖累 %.2f%%/年）"
                           % (100 * net_excess_annual,
                              100 * baseline["gross_excess_annual"],
                              100 * (baseline["gross_excess_annual"] - net_excess_annual)))
        if t_excess < 2.0:
            reasons.append("规则2 t_excess=%.2f < 2.0（t_ic=%.2f）" % (t_excess, t_ic))
        if K < min_w:
            reasons.append("规则2 样本外窗口 K=%d < %d" % (K, min_w))
        if turnover_annual > cap_to:
            reasons.append("规则3 年化换手 %.1f > 上限 %.1f" % (turnover_annual, cap_to))
        if t_adj < 2.0:
            reasons.append("规则5 batch_n=%d 校正后 t=%.2f < 2.0" % (batch_n, t_adj))
    decision = "admit" if not reasons else "reject"
    ih = hashlib.sha256(json.dumps({"spec": spec, "batch_n": batch_n},
                                   sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"run_id": run_id or "run-" + ih[:6], "inputs_hash": ih, "k_windows": K,
            "net_ic": round(net_ic, 6), "net_excess_annual": round(net_excess_annual, 6),
            "turnover_annual": round(turnover_annual, 4), "t_ic": round(t_ic, 4),
            "t_excess": round(t_excess, 4), "t_excess_adj": round(float(t_adj), 4),
            "batch_n": batch_n, "baseline": baseline, "decision": decision,
            "reasons": reasons}


# ── 内置信号（今晚已验证的面板片段，作为验收基准）──
def sig_rev20(px):
    return -(px.iloc[-1] / px.iloc[-21] - 1.0) if len(px) > 21 else None


def sig_rev60(px):
    return -(px.iloc[-1] / px.iloc[-61] - 1.0) if len(px) > 61 else None


def sig_ma60dev(px):
    return -(px.iloc[-1] / px.iloc[-60:].mean() - 1.0) if len(px) >= 60 else None


def sig_composite(px):
    parts = [f(px) for f in (sig_rev20, sig_rev60, sig_ma60dev)]
    parts = [p for p in parts if p is not None]
    if len(parts) < 2:
        return None
    zs = [(p - p.mean()) / (p.std() + 1e-12) for p in parts]
    return sum(zs) / len(zs)


SIGNALS = {"rev20": sig_rev20, "rev60": sig_rev60, "ma60dev": sig_ma60dev,
           "composite": sig_composite}
