# 工作台脊柱契约（SPINE）

> **每一行都是契约，不是实现建议。** 钉死这三份契约后，runtime/框架/前端都退化为**客户端**，
> 换谁都无所谓。今晚所有实测数字作为**验收基准**（§6），闸门必须能复现它们。

---

## 0. 定位（一句话）

**工作台 = 一个 MCP 服务（`wb_*`：账本 + 闸门 + registry + 组合 + 归因）+ 4 条 crontab + 角色配置。**

- 一切**能力**都已是 MCP：hub 111 工具（数据/因子/ML/kronos/事件）+ `causal-memory`（记忆）+ athena engine（执行/风控）
- 没有"runtime 选型"待决项：**能循环调 MCP 的任何东西都只是客户端**（DSH 会话 / cron / 看板 / athena）
- 服务内**唯二不许有 LLM** 的部件：**闸门**（§2）与**账本**（§3）

---

## 1. 工具契约（`wb_*`）

| 工具 | 签名 | 返回 |
|---|---|---|
| `wb_hypothesis` | `(text, mechanism, source, expected_ic, expected_turnover, proposed_by)` | `{hypothesis_id, status}` |
| `wb_factor` | `(hypothesis_id, name, code)` → 落因子表达式（面板片段） | `{factor_id, expr_hash}` |
| `wb_gate_evaluate` | `(factor_id, spec)` 见 §2 | `{net_ic, net_excess_annual, turnover_annual, t_stat, k_windows, decision, reasons[], baseline}` |
| `wb_admit` | `(factor_id, eval_run_id, approved_by, note)` → **仅人可调** | `{admission_id, status}` |
| `wb_retire` | `(factor_id, reason, retired_by)` | `{status}` |
| `wb_registry` | `(status?)` | `[{factor_id, name, status, weight, retired_reason}]` |
| `wb_portfolio` | `(as_of, spec)` → 整截面加权 + §8 约束 | `{portfolio_id, weights, diagnostics}` |
| `wb_order_list` | `(portfolio_id, positions_ref, min_order_cny, limit_band_pct)` | `{items[], summary}` |
| `wb_attribute` | `(portfolio_id, as_of, horizon)` | `{market_car, sector_car, alpha, factor_ic{}, discipline{}}` |
| `wb_report` | `(kind=weekly|daily)` | `{markdown, metrics{}}` |

**每个写工具都必须带 `run_id` 与 `inputs_hash`**（§3），否则拒绝执行。

---

## 2. 闸门契约（唯一准入权，无 LLM）

```python
wb_gate_evaluate(factor_id, spec) -> verdict
spec = {
  "universe": "trade",                 # 可交易域（A股、剔除ST/停牌/次新）
  "train_end": "2024-12-31",           # 训练区：允许被搜索
  "test": {"start": "2025-01-01", "end": "2026-07-15"},   # 样本外：不许被搜索
  "windows": {"step_days": 5},         # 非重叠窗口
  "cost_model": {"round_trip_bps": 20, "hold_days": 5},
  "turnover_cap_annual": 6.0,
  "batch_n": 20,                       # 本批次一共试了多少个假设（多重检验用）
  "baseline": "static_equal_weight_universe"   # 必须打败的静态基线
}
```

**判定规则（全部为真才算 admit）**

1. `net_excess_annual > 0` —— **净**（已扣 `cost_model`），且为正
2. `t_stat ≥ 2.0` 且 `k_windows ≥ 40` —— 非重叠样本外窗口
3. `turnover_annual ≤ turnover_cap_annual`
4. **打败基线**：`net_excess_annual > baseline.net_excess_annual`（条件层/择时层额外要求）
5. **多重检验**：`t_adj = t_stat` 在 `batch_n` 下按 Bonferroni 校正后仍 ≥ 2.0
   （**`batch_n` 由账本自动统计并写入 verdict**，不许调用方自报）

**输出必须带 `reasons[]`**：每条不满足的规则都要写明数字与阈值。**没有 reasons 的 verdict 视为无效。**

---

## 3. 账本契约（SQLite，追加为主，不可改写历史）

```sql
hypothesis(id PK, created_at, source, text, mechanism, expected_ic, expected_turnover, proposed_by, status)
factor(id PK, hypothesis_id, name, code, expr_hash, created_at, status)
eval_run(id PK, factor_id, spec_json, inputs_hash, run_id,
         net_ic, net_excess_annual, turnover_annual, t_stat, k_windows,
         baseline_json, decision, reasons_json, created_at)
admission(id PK, factor_id, eval_run_id, decision, approved_by, approved_at, note)   -- 只追加
registry(factor_id PK, status, weight, updated_at, retired_reason)
portfolio(id PK, as_of, spec_json, weights_json, inputs_hash, run_id, created_at)
order_list(id PK, portfolio_id, date, items_json, created_at, pushed_at)
attribution(id PK, portfolio_id, as_of, market_car, sector_car, alpha, factor_ic_json, discipline_json, created_at)
```

- **`inputs_hash`** = 因子代码 + 数据区间 + 成本模型 + 宇宙规则的哈希 → 同 hash = 可重放
- **`admission` 不可 UPDATE/DELETE**；纠正靠新增一条并标 `supersedes`
- 任何 `decision` 都能从 `eval_run` 重放

---

## 4. 准入状态机（人批是状态，不是提示词）

```
proposed ──▶ implemented ──▶ evaluated ──┬─▶ rejected（连 reasons 写回记忆）
                                          └─▶ pending_approval ──▶ admitted ──▶ monitoring ──▶ retired
                                                   ▲
                                          只有人能调 wb_admit
```

- **agent 只能推进到 `evaluated` 或 `pending_approval`**——技术手段：`wb_admit` 校验 `approved_by` 属于人类主体白名单
- 每次 `rejected` 都要把 **假设类型 + 原因** 写入 `causal-memory`（`record_decision`），
  验收标准：后续 N 轮里**同一类假设不再重复提出**
- `monitoring` 阶段每周复核在用因子的净 IC，衰减者自动降权/退役

---

## 5. 角色契约（配置，不是代码）

```yaml
role: hypothesizer            # /  implementer / constructor / attributor
prompt_ref: prompts/hypothesizer.md
mcp_tools: [get_a_news, get_a_reports, get_a_announcements, get_a_lockup_expiry,
            event_study, dml_cate, search_causal]
inputs:  [ ]                  # 读取的 artifact 种类
outputs: [hypothesis]         # 只能写这些 kinds
budget:  {max_llm_calls: 20, max_minutes: 15}
forbidden: [wb_admit, wb_retire]   # 红线：角色永远不能自己批准/退役
```

**新增一个角色 = 新增一份配置**（零代码）。

---

## 6. 验收基准（今晚实测数字，闸门必须复现）

闸门实现完成后，跑这 6 个已知案例，**必须全部给出 reject，并打印三个数与 reasons**：

| 案例 | 样本内 | 样本外（不重叠窗口） | 净超额 | 期望判定 |
|---|---|---|---|---|
| rev20（20日反转） | rankIC +0.1287，t=1.09（K=7） | +0.0273 / ICIR 0.12 / **t=1.03**（K=74） | 负 | **reject** |
| rev60 | +0.1401 / t=2.40 | +0.0229 / 0.09 / **t=0.79** | 负 | **reject** |
| ma60dev | +0.1830 / t=2.05 | +0.0310 / 0.12 / **t=1.06** | 负 | **reject** |
| **composite** | +0.1926 / t=2.62 | +0.0360 / 0.15 / **t=1.26** | **毛 −4.7%/年；净 −6.3~−12.4%/年** | **reject** |
| ML（lgbm） | — | **rank_ic = −0.0474** | — | **reject** |
| kronos（5日预测） | — | +0.0621 / 0.24 / **t=0.69** | — | **reject** |

**校准要点**：composite 在样本内 `t=2.62` 看着显著，样本外衰减 **83%** —— 这就是
`batch_n` 多重检验校正与"不许在 test 区搜索"两条规则的现实依据。

**通过标准**：闸门输出的 `decision` 与上表完全一致；且任一 verdict 都能从 `eval_run` 重放出相同数字。

---

## 7. 明确不做

- ❌ **闸门里不放 LLM**（一旦放进去，过拟合会被工业化）
- ❌ 不用"排序前 N 等权"做组合（实测尾部毁价值：毛超额 −4.7%/年）→ 用整截面加权 + 换手惩罚
- ❌ 不把 `batch_n` 交给调用方自报（必须账本统计）
- ❌ 不重写 athena 当研究台（它是**执行类 MCP 客户端**：持仓/风控/监控）
- ❌ 不在账本/闸门契约稳定前引入 agent 框架（那时只是"客户端手感"问题）
- ❌ 不允许角色自己批准自己的因子（红线写在服务里，不是提示词里）
