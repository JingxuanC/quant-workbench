"""账本（SPINE §3）：8 张表，admission 只追加，一切可重放。

铁律：写工具必须带 run_id + inputs_hash，否则拒绝——这是"可复现"的技术保证，
不是流程建议。
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypothesis (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, source TEXT, text TEXT,
  mechanism TEXT, expected_ic REAL, expected_turnover REAL, proposed_by TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS factor (
  id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id INTEGER, name TEXT, code TEXT,
  expr_hash TEXT, created_at REAL, status TEXT);
CREATE TABLE IF NOT EXISTS eval_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT, factor_id INTEGER, spec_json TEXT, inputs_hash TEXT,
  run_id TEXT, net_ic REAL, net_excess_annual REAL, turnover_annual REAL,
  t_ic REAL, t_excess REAL, k_windows INTEGER,
  baseline_json TEXT, decision TEXT, reasons_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS admission (
  id INTEGER PRIMARY KEY AUTOINCREMENT, factor_id INTEGER, eval_run_id INTEGER,
  decision TEXT, approved_by TEXT, approved_at REAL, note TEXT, supersedes INTEGER);
CREATE TABLE IF NOT EXISTS registry (
  factor_id INTEGER PRIMARY KEY, name TEXT, status TEXT, weight REAL,
  updated_at REAL, retired_reason TEXT);
CREATE TABLE IF NOT EXISTS portfolio (
  id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT, spec_json TEXT, weights_json TEXT,
  inputs_hash TEXT, run_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS order_list (
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio_id INTEGER, date TEXT, items_json TEXT,
  created_at REAL, pushed_at REAL);
CREATE TABLE IF NOT EXISTS attribution (
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio_id INTEGER, as_of TEXT,
  market_car REAL, sector_car REAL, alpha REAL, factor_ic_json TEXT,
  discipline_json TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_eval_factor ON eval_run(factor_id);
CREATE INDEX IF NOT EXISTS idx_factor_hyp ON factor(hypothesis_id);
"""

HUMAN_PRINCIPALS = {"hjx"}          # SPINE §4：只有人类主体可 approve


def connect(db_path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def inputs_hash(**parts) -> str:
    """因子代码 + 数据区间 + 成本模型 + 宇宙规则的哈希 → 同 hash 即同实验，可重放。"""
    blob = json.dumps({k: parts[k] for k in sorted(parts)}, ensure_ascii=False, sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def new_run_id(prefix: str = "run") -> str:
    return "%s-%s" % (prefix, hashlib.sha1(str(time.time()).encode()).hexdigest()[:8])


def _require_run(run_id, ih):
    if not run_id or not ih:
        raise ValueError("写工具必须带 run_id 与 inputs_hash（SPINE §3）")


def add_hypothesis(conn, text, mechanism, source="", expected_ic=None,
                   expected_turnover=None, proposed_by="agent", run_id=None, ih=None):
    _require_run(run_id, ih)
    cur = conn.execute(
        "INSERT INTO hypothesis(created_at,source,text,mechanism,expected_ic,"
        "expected_turnover,proposed_by,status) VALUES(?,?,?,?,?,?,?,'proposed')",
        (time.time(), source, text, mechanism, expected_ic, expected_turnover, proposed_by))
    conn.commit()
    return cur.lastrowid


def add_factor(conn, hypothesis_id, name, code, run_id=None, ih=None):
    _require_run(run_id, ih)
    eh = hashlib.sha256(code.encode()).hexdigest()[:16]
    cur = conn.execute(
        "INSERT INTO factor(hypothesis_id,name,code,expr_hash,created_at,status)"
        " VALUES(?,?,?,?,?,'implemented')",
        (hypothesis_id, name, code, eh, time.time()))
    conn.commit()
    return cur.lastrowid, eh


def add_eval(conn, factor_id, spec, verdict, run_id=None, ih=None):
    _require_run(run_id or verdict.get("run_id"), ih or verdict.get("inputs_hash"))
    cur = conn.execute(
        "INSERT INTO eval_run(factor_id,spec_json,inputs_hash,run_id,net_ic,net_excess_annual,"
        "turnover_annual,t_ic,t_excess,k_windows,baseline_json,decision,reasons_json,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (factor_id, json.dumps(spec, ensure_ascii=False, default=str), verdict["inputs_hash"],
         verdict["run_id"], verdict["net_ic"], verdict["net_excess_annual"],
         verdict["turnover_annual"], verdict["t_ic"], verdict["t_excess"],
         verdict["k_windows"], json.dumps(verdict.get("baseline"), ensure_ascii=False, default=str),
         verdict["decision"], json.dumps(verdict["reasons"], ensure_ascii=False), time.time()))
    conn.commit()
    return cur.lastrowid


def admit(conn, factor_id, eval_run_id, approved_by, note="", supersedes=None):
    """SPINE §4：只有人类主体能批准。agent 调这个会被拒。"""
    if approved_by not in HUMAN_PRINCIPALS:
        raise PermissionError("只有人类主体可批准（approved_by=%r）" % approved_by)
    row = conn.execute("SELECT decision FROM eval_run WHERE id=?", (eval_run_id,)).fetchone()
    if row is None:
        raise ValueError("eval_run %s 不存在" % eval_run_id)
    if row["decision"] != "admit":
        raise ValueError("eval_run 判定为 %s，不可准入" % row["decision"])
    cur = conn.execute(
        "INSERT INTO admission(factor_id,eval_run_id,decision,approved_by,approved_at,note,"
        "supersedes) VALUES(?,?,'admitted',?,?,?,?)",
        (factor_id, eval_run_id, approved_by, time.time(), note, supersedes))
    conn.execute("INSERT INTO registry(factor_id,name,status,weight,updated_at) "
                 "SELECT id,name,'admitted',0.0,? FROM factor WHERE id=? "
                 "ON CONFLICT(factor_id) DO UPDATE SET status='admitted',updated_at=excluded.updated_at",
                 (time.time(), factor_id))
    conn.commit()
    return cur.lastrowid


def reject(conn, factor_id, eval_run_id, rejected_by, reason=""):
    """否决一条待批因子：写一条 decision='rejected' 的 admission（只追加）。
    这样待批队列（按 eval_run 判重）会清掉它，但**台账保留**谁在何时因何否决。"""
    if not rejected_by:
        raise ValueError("否决必须记录 rejected_by")
    conn.execute("INSERT INTO admission(factor_id,eval_run_id,decision,approved_by,"
                 "approved_at,note) VALUES(?,?,'rejected',?,?,?)",
                 (factor_id, eval_run_id, rejected_by, time.time(), reason))
    conn.commit()


def retire(conn, factor_id, reason, retired_by="agent"):
    conn.execute("UPDATE registry SET status='retired',retired_reason=?,updated_at=? "
                 "WHERE factor_id=?", (reason, time.time(), factor_id))
    conn.commit()


def counts(conn) -> dict:
    out = {}
    for t in ("hypothesis", "factor", "eval_run", "admission"):
        out[t] = conn.execute("SELECT COUNT(*) c FROM %s" % t).fetchone()["c"]
    out["batch_n"] = out["hypothesis"]        # SPINE §2 规则5：batch_n 由账本统计，不许自报
    return out
