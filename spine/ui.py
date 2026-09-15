"""可视化工作台：单页看板 + 只读 API（挂在 workbench-mcp 上，零构建）。

设计约束（重要）：**看板没有"手工放行"按钮**。批准只能走 wb_admit（人类主体口径），
否则 UI 就成了绕过闸门的后门——那会把整个脊柱的意义抹掉。
"""

API_OVERVIEW = """
SELECT 'hypothesis' k, COUNT(*) n FROM hypothesis
UNION ALL SELECT 'factor', COUNT(*) FROM factor
UNION ALL SELECT 'eval_run', COUNT(*) FROM eval_run
UNION ALL SELECT 'admitted', COUNT(*) FROM admission
UNION ALL SELECT 'rejected', COUNT(*) FROM eval_run WHERE decision='reject'
"""

# 待批 = 闸门判 admit 但还没有 admission 记录（即 SPINE §4 的 pending_approval）
API_PENDING = """
SELECT f.id factor_id, f.name, h.text, h.mechanism, e.id eval_run_id,
       e.net_ic, e.t_excess, e.turnover_annual, e.net_excess_annual, e.k_windows,
       e.reasons_json, e.created_at
FROM factor f JOIN eval_run e ON e.factor_id = f.id
LEFT JOIN hypothesis h ON h.id = f.hypothesis_id
WHERE e.decision = 'admit'
  AND e.id NOT IN (SELECT eval_run_id FROM admission)
GROUP BY f.id HAVING e.id = MAX(e.id)
ORDER BY e.t_excess DESC
"""

API_REGISTRY = """
SELECT f.id factor_id, f.name, r.status, r.retired_reason,
       e.net_ic, e.t_excess, e.turnover_annual, e.net_excess_annual, e.decision, e.k_windows
FROM factor f
LEFT JOIN registry r ON r.factor_id = f.id
LEFT JOIN eval_run e ON e.factor_id = f.id
GROUP BY f.id HAVING e.id = MAX(e.id) OR e.id IS NULL
ORDER BY (r.status IS NULL), e.t_excess DESC
"""

# 墓碑墙 = 被否决的假设 + 原因（防 agent 重复提同一类垃圾）
API_TOMBSTONE = """
SELECT f.id factor_id, f.name, h.text, h.mechanism, e.decision,
       e.net_ic, e.t_excess, e.turnover_annual, e.net_excess_annual, e.reasons_json
FROM eval_run e JOIN factor f ON f.id = e.factor_id
LEFT JOIN hypothesis h ON h.id = f.hypothesis_id
WHERE e.decision = 'reject'
ORDER BY e.id DESC LIMIT 50
"""

HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>量化工作台</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--dim:#8b93a7;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
header{padding:14px 20px;border-bottom:1px solid #262b36;display:flex;align-items:center;gap:16px;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:600}
nav{display:flex;gap:4px;margin-left:auto}
nav button{background:none;border:1px solid transparent;color:var(--dim);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
nav button.on{color:var(--fg);background:var(--card);border-color:#2c3340}
main{padding:18px 20px;max-width:1280px;margin:0 auto}
.card{background:var(--card);border:1px solid #232935;border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card h2{font-size:13px;margin:0 0 10px;color:var(--dim);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.kpis{display:flex;gap:12px;flex-wrap:wrap}
.kpi{flex:1;min-width:120px;background:var(--card);border:1px solid #232935;border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:22px;font-weight:600}
.kpi span{color:var(--dim);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #212734}
th{color:var(--dim);font-weight:500;font-size:12px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--ok)}.neg{color:var(--bad)}.dim{color:var(--dim)}
.reason{color:var(--warn);font-size:12px}
.empty{color:var(--dim);padding:18px 0;text-align:center}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;border:1px solid #2c3340;color:var(--dim)}
.pill.admitted{color:var(--ok);border-color:#1c3a24}.pill.retired{color:var(--bad);border-color:#3a1f1f}
footer{color:var(--dim);font-size:12px;text-align:center;padding:20px}
</style></head><body>
<header><h1>量化工作台</h1><nav id="nav"></nav></header>
<main id="main"></main><footer id="foot"></footer>
<script>
const TABS=[["overview","概览"],["pending","闸门待批"],["registry","因子动物园"],["tombstone","墓碑墙"]];
let cur="overview", DATA={};
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function pct(v,d=2){return v==null?"—":(100*v).toFixed(d)+"%"};
function num(v,d=3){return v==null?"—":Number(v).toFixed(d)};
function cls(v){return v==null?"":(v>0?"pos":v<0?"neg":"")}
function reasons(j){try{return (JSON.parse(j)||[]).map(esc).join(" · ")}catch(e){return esc(j)}}
function tab(){
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.t===cur));
  let h="";
  if(cur==="overview"){
    h='<div class="card"><h2>账本</h2><div class="kpis">'+(DATA.overview||[]).map(r=>
        '<div class="kpi"><b>'+r.n+'</b><span>'+{hypothesis:"假设",factor:"因子",eval_run:"评估",admitted:"已准入",rejected:"已否决"}[r.k]||r.k+'</span></div>').join("")+'</div></div>';
    h+='<div class="card"><h2>最近判定</h2>'+evalTable(DATA.recent||[],false)+'</div>';
  }
  if(cur==="pending"){
    h='<div class="card"><h2>闸门待批（批准只能在这里，且需人类主体）</h2>';
    h += (DATA.pending||[]).length? (DATA.pending||[]).map(r=>
      '<div style="border-top:1px solid #212734;padding:10px 0"><b>'+esc(r.name)+'</b> <span class="dim">#'+r.factor_id+' · '+
      esc(r.text||"")+'</span><br><span class="dim">机制: '+esc(r.mechanism||"—")+'</span><br>'+
      'net_ic '+num(r.net_ic)+' · t_excess <b class="'+cls(r.t_excess)+'">'+num(r.t_excess)+'</b> · 换手 '+r.turnover_annual.toFixed(1)+
      ' · 净超额 <b class="'+cls(r.net_excess_annual)+'">'+pct(r.net_excess_annual)+'</b></div>').join("")
      : '<div class="empty">没有待批因子（空的是对的——闸门到现在否决了所有候选）</div>';
    h+='</div>';
  }
  if(cur==="registry"){h='<div class="card"><h2>因子动物园</h2>'+regTable(DATA.registry||[])+'</div>';}
  if(cur==="tombstone"){h='<div class="card"><h2>墓碑墙 · 被否决的假设与原因（防止重复提同一类）</h2>'+
    ((DATA.tombstone||[]).length? (DATA.tombstone||[]).map(r=>
      '<div style="border-top:1px solid #212734;padding:9px 0"><b>'+esc(r.name)+'</b> <span class="dim">'+esc(r.text||"")+'</span><br>'+
      '<span class="reason">'+reasons(r.reasons_json)+'</span></div>').join("")
      : '<div class="empty">暂无墓碑</div>')+'</div>';}
  document.getElementById("main").innerHTML=h;
}
function evalTable(rows,showName){
  if(!rows.length) return '<div class="empty">暂无</div>';
  let h='<table><tr><th>因子</th><th class="num">net_ic</th><th class="num">t_excess</th><th class="num">换手/年</th><th class="num">净超额/年</th><th>判定</th></tr>';
  rows.forEach(r=>{h+='<tr><td>'+(showName===false?(r.factor_id||""):esc(r.name))+'</td><td class="num">'+num(r.net_ic)+
    '</td><td class="num '+cls(r.t_excess)+'">'+num(r.t_excess)+'</td><td class="num">'+num(r.turnover_annual,1)+
    '</td><td class="num '+cls(r.net_excess_annual)+'">'+pct(r.net_excess_annual)+'</td><td>'+
    (r.decision==="admit"?'<span class="pill">admit</span>':'<span class="pill retired">reject</span>')+'</td></tr>';});
  return h+'</table>';
}
function regTable(rows){
  if(!rows.length) return '<div class="empty">因子动物园是空的（真话）</div>';
  let h='<table><tr><th>因子</th><th>状态</th><th class="num">net_ic</th><th class="num">t_excess</th><th class="num">换手/年</th><th>退役原因 / 判定</th></tr>';
  rows.forEach(r=>{h+='<tr><td>'+esc(r.name)+'</td><td><span class="pill '+(r.status||"")+'">'+esc(r.status||"候选")+
    '</span></td><td class="num">'+num(r.net_ic)+'</td><td class="num '+cls(r.t_excess)+'">'+num(r.t_excess)+
    '</td><td class="num">'+num(r.turnover_annual,1)+'</td><td class="dim">'+esc(r.retired_reason||r.decision||"—")+'</td></tr>';});
  return h+'</table>';
}
async function load(){
  try{const r=await fetch("api/report");DATA=await r.json();
    document.getElementById("foot").textContent="更新于 "+new Date().toLocaleTimeString()+" · 数据源 workbench-mcp 账本";
    tab();
  }catch(e){document.getElementById("main").innerHTML='<div class="card">加载失败: '+esc(e)+'</div>';}
}
document.getElementById("nav").innerHTML=TABS.map(t=>'<button data-t="'+t[0]+'">'+t[1]+'</button>').join("");
document.getElementById("nav").addEventListener("click",e=>{if(e.target.dataset.t){cur=e.target.dataset.t;tab();}});
load();setInterval(load,30000);
</script></body></html>
"""
