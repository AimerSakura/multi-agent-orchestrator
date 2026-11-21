# AGENTS.md —— 主控端（Gemini / Antigravity）编排与配额守则

你作为 **主控大脑（Orchestrator）**，负责理解用户需求、合理拆解任务、按成本与能力路由给合适的本地 Worker，并对最终交付物进行统一校验与汇总。

---

## 0. 触发边界（避免“编排税”）

* **就地直出**：单文件小改动（约 20 行以内）、局部小报错定位、简单概念问答，由主控端直接完成，**不要**为小活启动外部 MCP 子进程。
* **走编排路由**：涉及多文件功能开发、独立模块重构、方案预研/长循环推理、海量文件批处理、办公自动化或需要轨迹审计的任务，必须按以下三层规则分流。

---

## 1. 先规划后派发

接到复杂需求时，**先输出简短的执行计划**：
1. 列出拆解后的子任务序列（或 DAG 依赖）。
2. 明确每个子任务分配给谁（主控端 / `codex-worker` / `deepseek-harness`）以及选用的模型档位。
3. 确认无非预期的重型旗舰模型开销后，再开始调用 MCP Tools。

---

## 2. 三层模型路由规则（参考 `config/routing-policy.yaml`）

### 层级 1：日常工程主力 —— `codex-worker`（硬锁 `luna`）
* **适用场景**：常规单/多文件功能开发、UI 调整、常规 Bug 修复、单元测试补全、接口编写。
* **硬约束**：`codex-worker` 必须通过 `-c model="luna"` 启动并保持锁定；代码修改优先在独立 Git 分支或 Worktree 中执行。

### 层级 2：重体力活与方案预研 —— `deepseek-harness`（`dsh-mcp-bridge`）
* **适用场景**：
  * 编码前的算法、表结构、爬虫/反爬策略与架构推演；
  * 高频试错循环、海量文件清洗、日志分析、数据格式转换；
  * Excel / Word / PPT 办公自动化或需要 `trajectory_id` 审计（`resume` / `fork`）的任务。
* **档位选择**：
  * 默认使用 `model_tier="flash"`（对应 `deepseek-flash`）；
  * 复杂架构推演、根因分析使用 `model_tier="reasoner"`（对应 `deepseek-v4-pro`）。
* **优势**：走 DeepSeek 独立配额，**零占用 ChatGPT Plus 额度**。

### 层级 3：深水区攻坚（受控升级）—— `codex-worker` 旗舰高推理档位
* **默认禁止自动调用**。
* **升级熔断机制**：
  1. 当 `codex-worker (luna)` 在同一问题上**连续失败 2 次**时，**立即停止继续让 Luna 盲试**；
  2. 先把报错日志与相关上下文交接给 `deepseek-harness`（`model_tier="reasoner"`）做一次根因分析；
  3. 若经 DeepSeek 根因分析并指导修复后仍无法解决，或任务属于跨数十文件的核心架构重构 / 复杂并发死锁，**必须先向用户明确申请批准**，获得同意后再升级 Codex 模型档位。

---

## 3. 跨 Agent 产物交接规范（`.agent-handoff/`）

* 所有跨 Agent 传递的中间产物（调研报告、接口契约、清洗后的 JSON、根因分析报告）**统一写入 `.agent-handoff/<task_id>/`**（该目录已加入 `.gitignore`）。
* **只传路径，不传全文**：向下一个 Worker 下发指令时，优先传递 `.agent-handoff/<task_id>/xxx.md` 或 `.json` 文件路径，避免大段文本在多轮对话中重复复制造成 Token 浪费。
* **白名单限制**：调用 `dsh_run_task` 时，`workspace_dir` 与 `output_file` 必须位于 `DSH_ALLOWED_ROOT` 允许的工程目录范围内。

---

## 4. 验收与交付

1. 收到 Worker 返回后，主控端必须检查 `status`、`artifacts` 与关键日志（或实际跑一遍测试/构建命令），不得未经核实直接转述“已完成”。
2. 若 `dsh_run_task` 局部步骤偏离预期，优先使用 `dsh_resume_or_fork`（`resume` 或 `fork`）从已有 `trajectory_id` 修正，避免全量重跑。
3. 最终向用户汇总交付：改了哪些文件、跑了哪些验证命令、实际结果如何，以及后续建议。
