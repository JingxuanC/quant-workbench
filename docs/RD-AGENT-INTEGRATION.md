# RD-Agent 接入边界（SPINE §7）

**允许**：RD-Agent 作为**生成器**，产出「假设」与「因子表达式」，写进 `wb_hypothesis` / `wb_factor`。
**禁止**：RD-Agent（或任何 LLM）参与 `wb_gate_evaluate` 的判定，也**不允许**自行 `wb_admit`。

## 为什么（今晚实测）
- RD-Agent 在**同一份数据上搜索巨大假设空间** → 是多重检验机器：其回测结果按构造就是**样本内**的。
- 今晚量到同一现象：20 因子里挑 3 个 → 样本内 t=2.62（像显著）→ **样本外衰减 83%、t=1.26** → 净超额为负。
- 因此：**生成侧求召回，闸门侧零容忍**；`batch_n` 必须由账本统计（RD-Agent 会极大抬高它，
  这正是规则5存在的意义）。

## 接线方式
1. 训练区限定 `≤2024-12-31`；`test` 区（2025-01~）**任何生成器都不许看**。
2. RD-Agent 的每个产出 → `wb_hypothesis`（带 `mechanism`，说不出对手的丢弃）→ `wb_factor`。
3. `wb_gate_evaluate` → `pending_approval`（若通过）→ **人** `wb_admit`。
4. 被否的假设连原因写入 `causal-memory`（`record_decision`），防止重复提。
