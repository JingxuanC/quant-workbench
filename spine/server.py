"""workbench-mcp：wb_* 的唯一入口（SPINE §1）。

零第三方依赖（stdlib + spine）。形状与 hub 现有 MCP 服务一致：
  GET  /health  /tools
  POST /mcp  {"method":"tools/list"} / {"method":"tools/call","params":{"name":..,"arguments":..}}

铁律（写在服务里，不靠调用方自觉）：
  · 写工具缺 run_id/inputs_hash → 拒绝（spine.db 强制）
  · wb_admit 只认人类主体（spine.db HUMAN_PRINCIPALS）**且**需 human_token
  · batch_n 由账本统计，调用方传了也不采信
"""

import json
import os
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spine import db, gate, ui  # noqa: E402

DB_PATH = os.environ.get("WB_DB", "/data/workbench.db")
H5_PATH = os.environ.get("WB_H5", "/data/daily_pv_all.h5")
SERVER_VERSION = "1.0.0"

# ── 人类专属工具的身份校验 ─────────────────────────────────────────────
# SPINE §4「只有人类主体可 approve」。2026-09-15 修一个**可利用的漏洞**：
# 此前 db.admit 只校验 `approved_by` —— 那是**调用方自己传的字符串**。
# 实测（公网 /wb/mcp，服务此前无任何鉴权）：
#     approved_by='intruder' → 被拒
#     approved_by='hjx'      → **通过主体校验**，只因 eval_run 不存在才没落库
# 即凑出真实的 (factor_id, eval_run_id) 就能从公网批准因子。
# 现在人类专属工具必须同时提供 WB_HUMAN_TOKEN（只有人类控制台持有）；
# 未配置该环境变量时**一律拒绝**（fail-closed），不退化成无鉴权。
HUMAN_TOOLS = {"wb_admit", "wb_reject", "wb_retire"}
HUMAN_TOKEN = os.environ.get("WB_HUMAN_TOKEN", "")


def _require_human_token(name, args):
    if not HUMAN_TOKEN:
        raise PermissionError(
            "%s 已禁用：服务端未配置 WB_HUMAN_TOKEN（fail-closed，不接受无鉴权的人类操作）" % name)
    got = str((args or {}).get("human_token") or "")
    if not got or not secrets.compare_digest(got, HUMAN_TOKEN):
        raise PermissionError(
            "%s 需要有效的 human_token；调用方自报的 approved_by/rejected_by 不可作为身份凭据" % name)


_close_vol = None
# 每线程一个 sqlite 连接；h5 懒加载用锁保护（避免并发重复加载 400MB）
_local = threading.local()
_data_lock = threading.Lock()


def conn():
    """**每线程一个** sqlite 连接。

    2026-09-15 修一个让服务无法多客户端使用的 bug：此前是模块级单例连接，
    而 `ThreadingHTTPServer` 每个请求开一个线程 → 第二个并发请求必然炸：

        ProgrammingError: SQLite objects created in a thread can only be used
        in that same thread.

    后果：workbench 只能"单线程碰运气"地用，"注册进 hub 供多个 agent 调用"
    根本不可能（同一连接被不同线程复用必抛）。db.connect 已开 WAL，
    多连接并发读写是安全的。
    """
    c = getattr(_local, "conn", None)
    if c is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = db.connect(DB_PATH)
        _local.conn = c
    return c


def _data():
    global _close_vol
    if _close_vol is None:
        with _data_lock:
            if _close_vol is None:
                from spine import dataset  # noqa: PLC0415
                _close_vol = dataset.load_h5(
                    H5_PATH, start=os.environ.get("WB_START", "2024-01-01"))
    return _close_vol


TOOLS = {
    "wb_hypothesis": ("登记假设（agent 产出）", ["text", "mechanism"], ["source", "expected_ic",
                     "expected_turnover", "proposed_by", "run_id", "inputs_hash"]),
    "wb_factor": ("登记因子表达式", ["hypothesis_id", "name", "code"], ["run_id", "inputs_hash"]),
    "wb_gate_evaluate": ("闸门评估（唯一准入权·无 LLM）", ["factor_id", "spec"],
                         ["signal_name", "run_id", "inputs_hash"]),
    "wb_admit": ("批准准入（仅人类主体）", ["factor_id", "eval_run_id", "approved_by"], ["note"]),
    "wb_reject": ("否决待批因子（连原因入台账）", ["factor_id", "eval_run_id", "rejected_by"],
                  ["reason"]),
    "wb_retire": ("退役因子", ["factor_id", "reason"], ["retired_by"]),
    "wb_registry": ("因子动物园", [], ["status"]),
    "wb_report": ("账本概览", [], ["kind"]),
}


def call_tool(name, args):
    if name in HUMAN_TOOLS:
        _require_human_token(name, args)
    c = conn()
    if name == "wb_hypothesis":
        hid = db.add_hypothesis(c, args["text"], args["mechanism"],
                                source=args.get("source", ""), expected_ic=args.get("expected_ic"),
                                expected_turnover=args.get("expected_turnover"),
                                proposed_by=args.get("proposed_by", "agent"),
                                run_id=args.get("run_id"), ih=args.get("inputs_hash"))
        return {"hypothesis_id": hid, "status": "proposed", "batch_n": db.counts(c)["batch_n"]}
    if name == "wb_factor":
        fid, eh = db.add_factor(c, args["hypothesis_id"], args["name"], args["code"],
                                run_id=args.get("run_id"), ih=args.get("inputs_hash"))
        return {"factor_id": fid, "expr_hash": eh, "status": "implemented"}
    if name == "wb_gate_evaluate":
        sig = gate.SIGNALS.get(args.get("signal_name"))
        if sig is None:
            return {"error": "unknown signal_name=%r（当前内置: %s）"
                             % (args.get("signal_name"), list(gate.SIGNALS))}
        close, vol = _data()
        bn = db.counts(c)["batch_n"]          # 账本统计，不许自报
        v = gate.evaluate(sig, args["spec"], close, vol, batch_n=bn, run_id=args.get("run_id"))
        eid = db.add_eval(c, args["factor_id"], args["spec"], v,
                          run_id=args.get("run_id"), ih=args.get("inputs_hash"))
        v["eval_run_id"] = eid
        return v
    if name == "wb_admit":
        aid = db.admit(c, args["factor_id"], args["eval_run_id"], args["approved_by"],
                       note=args.get("note", ""))
        return {"admission_id": aid, "status": "admitted"}
    if name == "wb_reject":
        db.reject(c, args["factor_id"], args["eval_run_id"], args["rejected_by"],
                  reason=args.get("reason", ""))
        return {"status": "rejected", "factor_id": args["factor_id"]}
    if name == "wb_retire":
        db.retire(c, args["factor_id"], args["reason"], args.get("retired_by", "agent"))
        return {"status": "retired"}
    if name == "wb_registry":
        q = "SELECT * FROM registry"
        p = []
        if args.get("status"):
            q += " WHERE status=?"
            p.append(args["status"])
        return {"registry": [dict(r) for r in c.execute(q, p).fetchall()]}
    if name == "wb_ui_report":
        c2 = conn()
        agg = lambda q: [dict(r) for r in c2.execute(q).fetchall()]
        return {"overview": agg(ui.API_OVERVIEW), "pending": agg(ui.API_PENDING),
                "registry": agg(ui.API_REGISTRY), "tombstone": agg(ui.API_TOMBSTONE),
                "recent": agg("SELECT e.id,e.factor_id,f.name,e.net_ic,e.t_excess,"
                              "e.turnover_annual,e.net_excess_annual,e.decision,e.reasons_json "
                              "FROM eval_run e JOIN factor f ON f.id=e.factor_id "
                              "ORDER BY e.id DESC LIMIT 15")}
    if name == "wb_report":
        return {"counts": db.counts(c),
                "recent_evals": [dict(r) for r in c.execute(
                    "SELECT id,factor_id,decision,t_excess,net_excess_annual,turnover_annual,"
                    "k_windows,reasons_json FROM eval_run ORDER BY id DESC LIMIT 10").fetchall()]}
    return {"error": "unknown tool %s" % name}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False, default=str).encode())

    def do_GET(self):
        if self.path.startswith("/ui") or self.path in ("/", "/wb", "/wb/"):
            body = ui.HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if self.path.startswith("/api/report"):     # 看板数据（只读）
            try:
                return self._json(200, call_tool("wb_ui_report", {}))
            except Exception as e:  # noqa: BLE001
                return self._json(200, {"error": "%s: %s" % (type(e).__name__, e)})
        if self.path.startswith("/health"):
            return self._json(200, {"ok": True, "db": DB_PATH, "h5": H5_PATH})
        if self.path.startswith("/tools"):
            return self._json(200, [{"name": k, "description": v[0], "required": v[1]}
                                    for k, v in TOOLS.items()])
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        m = req.get("method")
        mid = req.get("id", 1)

        # ── JSON-RPC 通知（无 id）不产生响应 ──────────────────────────
        # 必须放在最前面：客户端（含 mcphub）在 initialize 之后会发
        # `notifications/initialized`，若按普通请求回一个 result，严格的
        # MCP 客户端会报错。
        if m and m.startswith("notifications/"):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # ── MCP 握手 ─────────────────────────────────────────────────
        # 没有这一段，mcphub 作为 MCP 客户端会连不上：
        #   Failed to connect client for server workbench
        #   "MCP error -32601: Unknown method: initialize"
        # 于是该 server 的工具永远不会出现在任何分组里（tools/list 返回 []）。
        # 注意：本服务**无状态**，所以 tools/list 不依赖 initialize 成功 ——
        # 直接 curl tools/list 是通的，很容易误判成"握手没问题"（2026-09-15 实测）。
        if m == "initialize":
            p = req.get("params") or {}
            return self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": p.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "workbench-mcp", "version": SERVER_VERSION},
            }})
        if m == "ping":
            return self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {}})

        if m == "tools/list":
            return self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": k, "description": v[0], "inputSchema": {"type": "object",
                 "properties": {a: {"type": "string"} for a in v[1] + v[2]},
                 "required": v[1]}} for k, v in TOOLS.items()]}})
        if m == "tools/call":
            p = req.get("params") or {}
            try:
                out = call_tool(p.get("name"), p.get("arguments") or {})
                is_err = isinstance(out, dict) and "error" in out
            except Exception as e:  # noqa: BLE001
                out = {"error": "%s: %s" % (type(e).__name__, e),
                       "trace": traceback.format_exc()[-400:]}
                is_err = True
            return self._json(200, {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                "isError": is_err}})
        return self._json(200, {"jsonrpc": "2.0", "id": mid,
                                "error": {"code": -32601, "message": "Unknown method: %s" % m}})


def serve(host="0.0.0.0", port=50062):
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "50062"))
    serve(os.environ.get("HOST", "0.0.0.0"), port)
