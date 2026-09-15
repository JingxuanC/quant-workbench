"""MCP 握手集成测试。

背景（2026-09-15 实测）：workbench 的 `/mcp` 最初只实现了 `tools/list` 与
`tools/call`，**没有 `initialize`**。它本身无状态，所以直接 POST `tools/list`
是通的 —— 很容易误判成"协议没问题"。但 mcphub 作为标准 MCP 客户端必须先
握手，于是：

    Failed to connect client for server workbench
    "MCP error -32601: Unknown method: initialize"

结果：workbench 的工具**永远不会出现在任何分组里**，`/hub/mcp/gate` 的
tools/list 返回 `[]`。这是"注册进 hub"的硬前提，所以在这里钉住。
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import spine.server as srv


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(port, body):
    req = urllib.request.Request("http://127.0.0.1:%d/mcp" % port,
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.status, resp.read().decode()


def test_initialize_returns_server_info(server):
    st, body = _post(server, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                              "params": {"protocolVersion": "2024-11-05",
                                         "capabilities": {},
                                         "clientInfo": {"name": "mcphub", "version": "1"}}})
    d = json.loads(body)
    assert st == 200 and "error" not in d, d
    r = d["result"]
    assert r["serverInfo"]["name"] == "workbench-mcp"
    assert r["protocolVersion"] == "2024-11-05"
    assert "tools" in r["capabilities"]


def test_initialize_echoes_other_protocol_versions(server):
    """客户端给什么版本就回什么，避免版本协商把连接掐掉。"""
    _, body = _post(server, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}})
    assert json.loads(body)["result"]["protocolVersion"] == "2025-06-18"


def test_notification_gets_202_and_no_body(server):
    """`notifications/initialized` 无 id，按 JSON-RPC **不得**有响应。"""
    st, body = _post(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202
    assert body.strip() == ""


def test_ping_and_tools_list(server):
    _, body = _post(server, {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
    assert json.loads(body)["result"] == {}
    _, body = _post(server, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    names = [t["name"] for t in json.loads(body)["result"]["tools"]]
    assert {"wb_hypothesis", "wb_factor", "wb_gate_evaluate",
            "wb_registry", "wb_report"} <= set(names)


def test_unknown_method_still_errors(server):
    _, body = _post(server, {"jsonrpc": "2.0", "id": 4, "method": "bogus", "params": {}})
    assert json.loads(body)["error"]["code"] == -32601


# ── 并发：每线程一个 sqlite 连接 ─────────────────────────────────────
# 2026-09-15 修：模块级单例连接 + ThreadingHTTPServer = 第二个并发请求必然抛
#   ProgrammingError: SQLite objects created in a thread can only be used in
#   that same thread
# 这让"注册进 hub 供多个 agent 调用"根本不可行。

@pytest.fixture()
def server_with_db(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "DB_PATH", str(tmp_path / "wb.db"))
    monkeypatch.setattr(srv, "_local", threading.local())   # 隔离上一条测试的连接
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _call(port, tool, args):
    _, body = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
    res = json.loads(body)["result"]
    return res, "".join(c.get("text", "") for c in res.get("content") or [])


def test_concurrent_requests_do_not_share_a_connection(server_with_db):
    """★ 8 个并发写请求必须全部成功（回归：单例连接时从第 2 个起就炸）。"""
    import concurrent.futures as cf

    def one(i):
        res, txt = _call(server_with_db, "wb_hypothesis",
                         {"text": "并发 %d" % i, "mechanism": "m",
                          "run_id": "r%d" % i, "inputs_hash": "ih%d" % i})
        return res.get("isError"), txt

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(one, range(8)))
    bad = [(e, t[:120]) for e, t in out if e or "hypothesis_id" not in t]
    assert not bad, "并发写入失败: %s" % bad


def test_concurrent_reads(server_with_db):
    import concurrent.futures as cf
    _call(server_with_db, "wb_hypothesis",
          {"text": "x", "mechanism": "m", "run_id": "r", "inputs_hash": "ih"})

    def one(_):
        res, txt = _call(server_with_db, "wb_report", {})
        return res.get("isError"), txt

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        out = list(ex.map(one, range(6)))
    assert all(not e for e, _ in out), out
