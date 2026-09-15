"""行业映射一次性建库（解 B2：板块约束/中性化的前提）。

用 astock-data 的 get_a_stock_info（东财 push2 单票接口，正常可用）逐只取行业，
存成 {symbol: industry}。东财自身有 1s 节流（em_get），5000 只 ≈ 90 分钟，**拉一次长期用**。
幂等：已存在的 symbol 跳过，所以可以中断后续跑。
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path(os.environ.get("INDUSTRY_MAP", "/data/industry_map.json"))
BASE = os.environ.get("ASTOCK_MCP", "http://astock-data:50052/mcp")
KEY = os.environ.get("MCP_LICENSE_KEY", "")


def call(tool, args, timeout=30):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if KEY:
        req.add_header("X-License-Key", KEY)
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
    for line in raw.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
            break
    r = json.loads(raw)
    txt = "".join(c.get("text", "") for c in (r.get("result") or {}).get("content") or [])
    return json.loads(txt)


def main():
    inst = Path(os.environ.get("UNIVERSE_FILE", "/app/.qlib/qlib_data/cn_data/instruments/all.txt"))
    syms = []
    for line in inst.read_text().splitlines():
        s = line.split("\t")[0].strip().upper()
        if len(s) == 8 and s[:2] in ("SH", "SZ"):
            syms.append(s)
    m = {}
    if OUT.exists():
        m = json.loads(OUT.read_text())
    todo = [s for s in syms if s not in m]
    print("  总 %d 只，已有 %d，待拉 %d" % (len(syms), len(m), len(todo)), flush=True)
    for i, s in enumerate(todo, 1):
        try:
            d = call("get_a_stock_info", {"symbol": s})
            ind = (d or {}).get("industry") or (d or {}).get("行业") or ""
            m[s] = ind
        except Exception as e:  # noqa: BLE001
            m[s] = ""
            print("  %s 失败: %s" % (s, e), flush=True)
        if i % 50 == 0:
            OUT.write_text(json.dumps(m, ensure_ascii=False))
            print("  进度 %d/%d" % (i, len(todo)), flush=True)
    OUT.write_text(json.dumps(m, ensure_ascii=False))
    got = sum(1 for v in m.values() if v)
    print("  完成：%d 只，其中拿到行业 %d 只 → %s" % (len(m), got, OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
