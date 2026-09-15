"""闸门验收：既要能杀（无 edge），也要能放（有 edge），且可重放。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spine import db, gate  # noqa: E402

SPEC = {
    "universe": "trade",
    "test": {"start": "2025-01-01", "end": "2026-06-30"},
    "windows": {"step_days": 5},
    "cost_model": {"round_trip_bps": 20, "hold_days": 5},
    "turnover_cap_annual": 6.0,
    "topn": 30, "universe_size": 120, "min_windows": 10,
    "lookback_days": 140, "baseline": "static_equal_weight_universe",
}


def make_panel(n_inst=150, days=760, drift_spread=0.002, noise=0.01, seed=0):
    """每只票给一个恒定 drift + 日噪声：drift 高的票持续跑赢 → 动量信号可预测，
    但噪声让 t 值落在**真实量级**（否则零方差会让 Bonferroni 校正失去意义）。"""
    rng = np.random.default_rng(seed)
    drifts = np.linspace(-drift_spread, drift_spread, n_inst)
    rets = drifts[None, :] + rng.normal(0, noise, size=(days, n_inst))
    close = 100.0 * np.cumprod(1.0 + rets, axis=0)
    dates = pd.date_range("2024-01-02", periods=days, freq="B")
    cols = ["sh600%03d" % i for i in range(n_inst)]
    return (pd.DataFrame(close, index=dates, columns=cols),
            pd.DataFrame(np.full_like(close, 1e6), index=dates, columns=cols))


def sig_momentum(px):
    return px.iloc[-1] / px.iloc[-21] - 1.0        # 与恒定 drift 正相关，但换手高


def sig_stable_drift(px):
    """窗口收益 ≈ 恒定 drift：排序**稳定** → 低换手，且与未来收益强相关。
    用它当"有 edge"的夹具（sig_momentum 排序噪声大，t_excess 只有 1.7）。"""
    return px.iloc[-1] / px.iloc[0] - 1.0


def sig_random(px):
    rng = np.random.default_rng(hash(str(px.index[-1])) % 2**31)
    return pd.Series(rng.normal(size=len(px.columns)), index=px.columns)


def test_gate_rejects_no_edge_signal():
    close, vol = make_panel()
    v = gate.evaluate(sig_random, SPEC, close, vol, batch_n=1, run_id="r-rand")
    assert v["decision"] == "reject", v
    assert v["k_windows"] >= 10
    assert any("规则1" in r or "规则2" in r for r in v["reasons"]), v["reasons"]


def test_gate_admits_a_real_edge():
    """关键：闸门**能**放行——否则它只是个'一律拒绝'的空壳。"""
    close, vol = make_panel()
    v = gate.evaluate(sig_stable_drift, SPEC, close, vol, batch_n=1, run_id="r-edge")
    assert v["decision"] == "admit", v
    assert v["net_excess_annual"] > 0 and v["t_excess"] >= 2.0, v
    assert v["reasons"] == []


def test_gate_rejects_on_turnover_cap():
    """信号本身有 edge，但换手超标 → 必须被规则3拦下。"""
    close, vol = make_panel()

    def flip_flop(px):
        half = len(px.columns) // 2
        first = str(px.index[-1]) < "2026-01-01"     # 定期换一半，制造高换手
        cols = px.columns[:half] if first else px.columns[half:]
        s = pd.Series(np.nan, index=px.columns)
        s[cols] = 1.0
        return s

    spec = dict(SPEC, turnover_cap_annual=1.0)
    v = gate.evaluate(flip_flop, spec, close, vol, batch_n=1, run_id="r-to")
    assert v["decision"] == "reject"
    assert any("规则3" in r for r in v["reasons"]), v["reasons"]


def test_gate_applies_bonferroni_with_ledger_batch_n():
    """规则5：同一批试了 100 个假设 → 校正后必须更严。batch_n 由账本统计。"""
    close, vol = make_panel()
    v1 = gate.evaluate(sig_stable_drift, SPEC, close, vol, batch_n=1, run_id="r-b1")
    v100 = gate.evaluate(sig_stable_drift, SPEC, close, vol, batch_n=100, run_id="r-b100")
    assert v1["t_excess_adj"] > v100["t_excess_adj"], "batch_n 越大校正后 t 越小"
    assert v1["decision"] == "admit" and v100["decision"] == "reject", (v1, v100)
    assert any("规则5" in r for r in v100["reasons"]), v100["reasons"]


def test_verdict_is_reproducible_and_ledgered(tmp_path):
    """同输入 → 同 inputs_hash（可重放）；eval_run 落库并带 reasons。"""
    close, vol = make_panel()
    v1 = gate.evaluate(sig_stable_drift, SPEC, close, vol, batch_n=1, run_id="r-a")
    v2 = gate.evaluate(sig_stable_drift, SPEC, close, vol, batch_n=1, run_id="r-b")
    assert v1["inputs_hash"] == v2["inputs_hash"], "同输入必须同 hash（可重放）"
    assert v1["net_excess_annual"] == v2["net_excess_annual"]

    conn = db.connect(tmp_path / "wb.db")
    h = db.add_hypothesis(conn, "动量假设", "恒定漂移", run_id="r-a", ih=v1["inputs_hash"])
    fid, _ = db.add_factor(conn, h, "momentum", "code", run_id="r-a", ih=v1["inputs_hash"])
    eid = db.add_eval(conn, fid, SPEC, v1, run_id="r-a", ih=v1["inputs_hash"])
    row = conn.execute("SELECT * FROM eval_run WHERE id=?", (eid,)).fetchone()
    assert row["decision"] == "admit" and row["t_excess"] >= 2.0
    # 人批准 → registry 出现；agent 批准被拒
    db.admit(conn, fid, eid, approved_by="hjx", note="ok")
    assert conn.execute("SELECT status FROM registry WHERE factor_id=?", (fid,)).fetchone()["status"] == "admitted"
    try:
        db.add_eval(conn, fid, SPEC, {"decision": "admit"})   # 缺 run_id/inputs_hash → 必须拒
        raise AssertionError("缺 run_id 应当被拒")
    except ValueError:
        pass
