# 本地多智能体 MCP 协同编排系统 —— 设计与开发文档

## 1. 项目背景与核心目的

### 1.1 解决的痛点

当前在本地桌面端同时使用多个 AI 编程与自动化工具（**Gemini / Antigravity**、**OpenAI Codex Desktop App**、**DeepSeek Harness (`dsh`)**），存在三个核心问题：

1. **割裂的多窗口操作：** 需求沟通、方案调研、代码编写和批处理脚本分散在不同客户端，需要人工频繁复制上下文与切换窗口。
2. **ChatGPT Plus 额度消耗过快：** Codex 已切换为按实际 Token 消耗计费且与网页端「工作（Work）」模式共享配额。如果默认使用旗舰/重型模型（如 Sol，单次消耗约为 Luna 的 20～25 倍）执行日常开发或高频重试循环，极易触发 Plus 额度上限。
3. **工具优势未形成互补：** Codex 擅长强隔离工程开发与沙盒测试，DeepSeek Harness 擅长高性价比长循环推理、文件批处理与办公自动化，缺乏统一的调度层来按任务特性自动分流。

### 1.2 项目目标

构建一套运行在本地的 **“一主两仆”多智能体协同编排系统（Multi-Agent MCP Orchestrator）**：

* **单一入口：** 用户只需向主控端（Gemini / Antigravity / Gemini CLI）提出自然语言需求。
* **自动拆解与派发：** 主控端基于 **MCP（Model Context Protocol）** 协议，根据任务类型、复杂度与成本策略，自动将子任务路由给本地的 **Codex** 或 **DeepSeek Harness** 执行。
* **成本与配额最优：** 在保证工程质量的前提下，通过“三层模型调度策略”保护 ChatGPT Plus 配额，将重 Token 消耗任务分流至 DeepSeek。
* **可验收与可回溯：** 所有子 Agent 的执行日志、代码变更与产物汇总回主控端，由主控端统一校验并向用户交付结果。

---

## 2. 核心需求清单

### 2.1 功能需求（Functional Requirements）

* **FR-1 统一主控调度（Orchestrator）：** 主控端（Gemini）负责需求澄清、联网查资料、任务拆解（DAG / 顺序子任务）、下发指令以及最终结果验收。
* **FR-2 Codex 原生 MCP 接入：** 将本地安装的 OpenAI Codex 以原生 MCP Server 模式（`codex mcp-server`）挂载至主控端，支持代码编写、重构、单元测试与 Git Worktree 沙盒并行开发。
* **FR-3 DeepSeek Harness MCP 桥接层开发（核心编码交付物）：** 鉴于 DeepSeek Harness（`dsh`）原生定位为基于 Cordis 的插件宿主（MCP Client），需开发一个轻量级本地 MCP Server（`dsh-mcp-bridge`），将其 CLI 或本地服务接口（默认 `http://127.0.0.1:3080`）封装为标准 MCP Tools 暴露给主控端。
* **FR-4 智能分级路由（Cost-Aware Routing）：** 建立显式路由规则与调度 Prompt，按任务属性自动分配给 `Codex (Luna)`、`DeepSeek Harness (V4 / V4.1-Flash)` 或 `Codex (旗舰高推理档位)`。
* **FR-5 共享工作区与产物交接：** 约定统一的本地项目目录与中间产物交接规范（如 `.agent-handoff/`），确保多 Agent 在同一目录协作时不发生文件覆写冲突。

### 2.2 非功能需求（Non-Functional Requirements）

* **NFR-1 Plus 配额保护机制：** 默认锁定 Codex 使用 **Luna 模型**；严禁在未触发升级条件或未经用户确认的情况下自动调用 Sol 等重型模型。
* **NFR-2 执行安全与隔离：** Codex 修改代码优先在独立分支或 Git Worktree 中执行；DeepSeek Harness 执行批处理/文件操作时需限制工作目录白名单，防止越权误删。
* **NFR-3 可观测性与容错：** 桥接层需完整捕获子进程退出码、标准输出/错误日志及 `dsh` 执行轨迹 ID（Trajectory ID），支持超时中止、失败重试与断点恢复（Resume / Fork）。

---

## 3. 系统总体架构

### 3.1 拓扑结构（一主两仆 MCP 架构）

```text
+-------------------------------------------------------------------+
|                        用户 (自然语言需求)                          |
+---------------------------------+---------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|         主控大脑 (MCP Client): Gemini CLI / Antigravity            |
|  职责: 需求理解 | 联网调研 | 任务拆解 | 路由决策 | 最终验收汇总       |
+-----------------+-------------------------------+-----------------+
                  | MCP (stdio / SSE)             | MCP (stdio)
                  v                               v
+-----------------------------------+   +-----------------------------------+
|  执行手下 A: codex-worker          |   |  执行手下 B: dsh-mcp-bridge       |
|  (原生 `codex mcp-server`)         |   |  (自研轻量级本地 MCP Server)       |
|                                   |   |                                   |
|  默认模型: GPT-6 / 5.6 Luna       |   |  底层调用: dsh CLI / Local HTTP   |
|  升级模型: Sol / 高推理 (需触发)   |   |  (127.0.0.1:3080) / DeepSeek API  |
|  隔离机制: Git Worktree 沙盒       |   |  推荐模型: DeepSeek V4 / V4.1-Flash|
+-----------------+-----------------+   +-----------------+-----------------+
                  |                                       |
                  +-------------------+-------------------+
                                      |
                                      v
+-------------------------------------------------------------------+
|              统一本地工作区 (Shared Local Workspace)                |
|  - 源码仓库 (Git)                                                  |
|  - .agent-handoff/ (跨 Agent 结构化任务单、清洗后数据、验收日志)      |
+-------------------------------------------------------------------+
```

### 3.2 三层模型调度与分工策略

主控端拆解任务时，必须严格遵守以下分工矩阵：

| 层级与路由目标 | 典型适用场景 | 成本与配额考量 |
| --- | --- | --- |
| **层级 1：日常工程主力**<br>`codex-worker`（**锁定 Luna 模型**） | 单文件/常规多文件功能开发、前端 UI 调整、常规 Bug 修复、补全单元测试、常规接口编写。 | 覆盖 80% 以上编码任务；响应极快、原生沙盒体验完整，Plus 额度消耗极低。 |
| **层级 2：重体力活与方案预研**<br>`dsh-mcp-bridge`（**DeepSeek V4 / V4.1-Flash**） | 写代码前的算法/表结构/反爬策略推演；高频试错循环、海量文件清洗、爬虫批处理、数据格式转换；Excel/Word/PPT 办公自动化与定时任务；需要执行轨迹审计（Resume/Fork）的实验任务。 | 走 DeepSeek 独立 API 计费，**零占用 ChatGPT Plus 配额**，缓存命中时成本极低，适合高并发与长周期循环。 |
| **层级 3：深水区攻坚（受控升级）**<br>`codex-worker`（**旗舰 / 高推理档位**） | 跨几十个文件的核心架构重构、复杂异步/并发死锁排查、Luna 连续 2 次尝试修复失败的疑难 Bug。 | 消耗为 Luna 的 20～25 倍，默认禁止调用；仅在“Luna 连续失败 2 次”或“用户明确批准”时按需触发。 |

---

## 4. 核心模块详细设计

### 4.1 模块一：`dsh-mcp-bridge`（DeepSeek Harness MCP 桥接服务）

这是本项目在 Antigravity 中需要重点开发的核心代码模块。本项目已确定采用 **Python 3.12+（`uv` + 官方 `mcp>=2.0` / `MCPServer` SDK）** 实现，通过 `stdio` 协议与主控端通信。

#### 暴露的 MCP Tools 接口规范

1. **`dsh_run_task`**（向 DeepSeek Harness 下发执行任务）
* **输入参数：**
```json
{
  "task_prompt": "string (必填，清晰的任务描述与预期产出)",
  "workspace_dir": "string (必填，允许读写的本地绝对路径)",
  "output_file": "string (可选，建议写入 .agent-handoff/ 下的结果文件路径)",
  "model_tier": "enum ['flash', 'reasoner', 'default'] (默认 'flash')",
  "timeout_sec": "integer (默认 300，防止长循环卡死)"
}
```
* **返回结构：**
```json
{
  "status": "success | failed | timeout",
  "trajectory_id": "string (dsh 执行轨迹 ID，用于后续 resume/fork)",
  "summary": "string (执行结果摘要)",
  "artifacts": ["string (生成或修改的文件路径列表)"],
  "logs_tail": "string (关键日志尾部，截断控制 Token)"
}
```

2. **`dsh_resume_or_fork`**（利用 Harness 轨迹能力进行恢复或分叉重试）
* **输入参数：** `trajectory_id`、`mode`（`resume` / `fork`）、`new_instruction`（可选修正指令）。
* **用途：** 当主控端验收发现批处理或推理某一步偏离预期时，直接从特定轨迹恢复，避免从头重跑浪费 Token。

3. **`dsh_health_check`**（环境探测）
* **用途：** 检查本地 `dsh` CLI 是否可用、本地服务端口（如 `127.0.0.1:3080`）是否存活，以及是否配置了备用 DeepSeek API Key。

#### 适配器双模式设计（Adapter Pattern）

为了防止 `dsh` 版本更新导致接口变动，桥接层内部应抽象出执行器接口：

* **主通道（Harness Adapter）：** 优先通过子进程调用 `dsh --profile headless --patch <patch.yml> --json -`（经实机探测已验证支持 `--session-id` 恢复、stdin 传任务文本、`--json` NDJSON 事件流以及 `deepseek-flash` / `deepseek-v4-pro` 模型 ID），并探测本地 Web UI 端口（`127.0.0.1:3080`）健康状态。
* **降级通道（Direct API Fallback，可选）：** 当仅需纯推理预研（不涉及本地插件工具）或显式开启 `DSH_ALLOW_API_FALLBACK=true` 时，支持可选降级到 DeepSeek 官方 API。

---

### 4.2 模块二：Codex MCP 挂载与配额保护配置

利用 Codex 官方原生 MCP Server 能力进行注册，同时通过配置或封装参数锁死默认模型为 Luna。

#### 主控端 MCP 配置示例（如 `~/.gemini/settings.json` 或 Antigravity MCP 配置）

```json
{
  "mcpServers": {
    "codex-worker": {
      "command": "codex",
      "args": ["mcp-server", "-c", "model=\"luna\""]
    },
    "deepseek-harness": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/ABSOLUTE/PATH/TO/packages/dsh-mcp-bridge",
        "dsh-mcp-bridge"
      ],
      "env": {
        "DSH_BASE_URL": "http://127.0.0.1:3080",
        "DSH_ALLOWED_ROOT": "/ABSOLUTE/PATH/TO/YOUR/WORKSPACE",
        "DSH_DEFAULT_TIMEOUT": "300"
      }
    }
  }
}
```

*(注：经 Phase 1 实机探测，`codex-cli 0.141.0` 的 `codex mcp-server` 支持 `-c, --config <key=value>` 覆盖配置项，因此直接通过 `-c model="luna"` 锁定默认模型。)*

---

### 4.3 模块三：主控编排规则文件（`AGENTS.md` / Orchestrator Prompt）

需在工作区生成一份主控系统指令文件，约束 Gemini 主控端的行为规范：

1. **先拆解再派发：** 接到复杂需求后，先输出简短的执行计划（标注每一步由谁执行、用什么模型），确认无高额费用风险后再调用 MCP。
2. **默认低成本路由：** 编码默认派发给 `codex-worker`（Luna）；数据处理、方案预研、批量脚本、办公文档自动化默认派发给 `deepseek-harness`。
3. **升级熔断机制：** 若 `codex-worker`（Luna）在同一问题上连续失败 2 次，主控端应先将报错上下文发给 `deepseek-harness` 做根因分析；若仍无法解决，再向用户申请切换 Codex 旗舰档位攻坚。
4. **交接目录规范：** 跨 Agent 传递中间数据（如调研报告、清洗后的 JSON、接口契约）一律写入 `.agent-handoff/<task_id>/` 目录，通过文件路径交接，减少大段文本在上下文中重复传输造成的 Token 浪费。

---

## 5. 推荐目录结构

在 Antigravity 中建议初始化如下项目结构：

```text
multi-agent-orchestrator/
├── DESIGN.md                      # 本文档（项目规格说明）
├── AGENTS.md                      # 给主控端（Gemini/Antigravity）的路由与配额守则
├── .gitignore                     # 忽略 .venv/、__pycache__/、.agent-handoff/ 等
├── config/
│   ├── mcp-settings.example.json  # Gemini CLI / Antigravity MCP 配置模板
│   └── routing-policy.yaml        # 路由规则与熔断阈值配置
├── packages/
│   └── dsh-mcp-bridge/            # DeepSeek Harness MCP 桥接服务源码 (Python / uv)
│       ├── pyproject.toml
│       ├── src/
│       │   └── dsh_mcp_bridge/
│       │       ├── __init__.py
│       │       ├── server.py      # MCP Server 入口与 Tool 注册 (MCPServer)
│       │       ├── adapters/
│       │       │   ├── __init__.py
│       │       │   ├── dsh_cli.py # dsh CLI 子进程调用实现
│       │       │   ├── dsh_http.py# dsh 本地 3080 端口调用实现
│       │       │   └── ds_api.py  # (可选) DeepSeek API 降级通道
│       │       ├── security.py    # 工作目录白名单校验与路径越界防护
│       │       └── logger.py      # 执行日志与轨迹追踪记录
│       └── tests/                 # pytest 单元测试
├── scripts/
│   ├── check-env.ps1              # Windows 一键检测 codex / dsh / uv / python 环境脚本
│   └── smoke_test.py              # 端到端联通性冒烟测试
└── .agent-handoff/                # 运行时跨 Agent 产物交接目录（加入 .gitignore）
```

---

## 6. 分阶段开发计划与验收标准

### Phase 1：本地环境探测与接口对齐（已完成首轮实机探测）

* **开发任务：**
1. 编写 `scripts/check-env.ps1` 探测本机操作系统、Python (`uv`)、`codex` CLI 版本与 `mcp-server` 支持参数、以及 `dsh` 的 CLI 命令格式和本地服务端口（`3080`）接口形态。
2. 验证 `codex mcp-server` 能否指定默认模型为 Luna（实机已确认：`codex-cli 0.141.0` 支持 `codex mcp-server -c model="luna"`）。
3. 实机已确认：`uv 0.7.12` 可用（含 Python 3.12 / 3.13）；`dsh 0.2.0-rc.2` 支持 `dsh --profile headless --patch <yaml> --json [--session-id <id>] -`（NDJSON 事件输出，内置模型 ID 为 `deepseek-flash` 与 `deepseek-v4-pro`），以及 `dsh --profile web`（默认监听 `127.0.0.1:3080`）；受限环境下需通过 `DSH_HOME` 指向可写目录以避免 `EPERM`。

* **验收标准：** 环境脚本输出明确的可用性报告，确认 `codex` 与 `dsh` 的实际调用方式无黑盒假设。

### Phase 2：`dsh-mcp-bridge` 核心开发与单测

* **开发任务：**
1. 初始化 MCP Server 工程，实现 `dsh_health_check`、`dsh_run_task`、`dsh_resume_or_fork` 三个核心 Tool。
2. 实现路径白名单校验（禁止访问 `DSH_ALLOWED_ROOT` 之外的路径）及超时强杀机制。
3. 编写单元测试与 MCP Inspector 本地联调测试。

* **验收标准：** 通过 MCP 测试客户端调用 `dsh_run_task` 执行一个本地数据清洗/文件整理任务，能正确在 `.agent-handoff/` 生成产物并返回结构化 JSON 与 `trajectory_id`。

### Phase 3：主控联调与三层路由落地

* **开发任务：**
1. 生成最终的 `AGENTS.md` 主控规则文档与 `mcp-settings.json` 配置。
2. 跑通一个端到端实战闭环测试（例如：主控端接收需求 → 派发 `deepseek-harness` 生成结构化数据或技术方案写入 `.agent-handoff/` → 派发 `codex-worker (Luna)` 读取该文件编写代码并跑通单元测试 → 主控端验收汇总）。

* **验收标准：** 全程只需在主控端输入 1 次需求，成功触发双 Worker 协同且全程未触发高额旗舰模型消耗。

---

## 7. 在 Antigravity 中的起步指令参考

把本文件放入 Antigravity 项目后，你可以直接对 Antigravity 的 Agent 说：

> “请完整阅读根目录下的 `DESIGN.md`。我们先进入 **Phase 1**：请检测我当前电脑的操作系统、Node.js/Python 环境，以及本地 `codex` 和 `dsh`（DeepSeek Harness）的命令行帮助文档和本地服务端口状态。根据探测结果，先给出 `dsh-mcp-bridge` 推荐采用的技术栈（TypeScript 或 Python）和适配方案，确认后再开始脚手架搭建。”
