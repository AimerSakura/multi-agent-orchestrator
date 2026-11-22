# Multi-Agent MCP Orchestrator

本地 **“一主两仆”多智能体 MCP 协同编排系统**：以 **Gemini CLI / Antigravity** 作为主控大脑（MCP Client），通过标准化 **MCP（Model Context Protocol）** 协议按任务特性与成本策略自动路由至本地 **OpenAI Codex（锁定 Luna 模型）** 与 **DeepSeek Harness（`dsh`）**，在统一本地工作区内协作并保护 ChatGPT Plus 配额。

---

## 架构概览

```text
+-------------------------------------------------------------------+
|                        用户 (自然语言需求)                          |
+---------------------------------+---------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|         主控大脑 (MCP Client): Gemini CLI / Antigravity            |
|  职责: 需求澄清 | 任务拆解 | 成本感知路由 | 结果校验与汇总交付       |
+-----------------+-------------------------------+-----------------+
                  | MCP (stdio)                   | MCP (stdio)
                  v                               v
+-----------------------------------+   +-----------------------------------+
|  Worker A: codex-worker           |   |  Worker B: dsh-mcp-bridge         |
| (Python / codex-mcp-bridge)       |   | (Python / dsh-mcp-bridge)         |
|  默认模型: gpt-6-luna (白名单锁)  |   |  底层调用: dsh --profile headless |
|  隔离机制: sandbox / --worktree   |   |  模型档位: deepseek-flash / v4-pro |
+-----------------+-----------------+   +-----------------+-----------------+
                  |                                       |
                  +-------------------+-------------------+
                                      |
                                      v
+-------------------------------------------------------------------+
|              统一本地工作区 (Shared Local Workspace)                |
|  - 源码仓库 (Git)                                                  |
|  - .agent-handoff/<task_id>/ (跨 Agent 文件交接与验收产物)          |
+-------------------------------------------------------------------+
```

## 核心特性

* **原生桌面持久化会话与硬锁配额保护**：`codex-worker` 通过自研 `codex-mcp-bridge` 默认调用 Codex `0.171.0+` 的 `codex app-server --listen stdio://` 协议，在代码层硬校验 `CODEX_ALLOWED_MODELS`（默认仅放行 `gpt-6-luna`、`gpt-5.6-luna`）与工作区白名单，同时生成的会话直接为 Codex Desktop 可见的 `source="vscode"` 持久化会话；传 `worktree=true` 或设 `CODEX_BACKEND=exec` 时回退至 `codex exec --json`。
* **DeepSeek Harness 桥接服务（`dsh-mcp-bridge`）**：基于 Python 3.12+ 与官方 `mcp>=2.0` SDK（`MCPServer`）构建，封装 `dsh --profile headless --json --patch ...` 供任意 MCP Client 直接调用，并支持读取 `~/.dsh` 原生会话历史。
* **双向原生会话可读**：提供 `codex_list_threads` / `codex_read_thread` 与 `dsh_list_sessions` / `dsh_read_session`，使主控端可直接读取 Desktop App 与 CLI 的完整历史上下文，减少孤立一次性会话。
* **文件驱动交接（`.agent-handoff/`）**：中间调研报告、清洗数据、接口契约通过文件路径交接，避免大段文本在上下文中反复搬运。
* **纵深安全控制**：严格校验 `workspace_dir`、`cwd` 与 `output_file` 必须在允许的工作区根目录白名单内；超时强杀子进程树；支持 `resume`（原生续接）与 `fork`（轨迹分叉）重试。

---

## 前置依赖

* **Python 3.12+** 与 **[uv](https://docs.astral.sh/uv/)**（推荐 `0.7+`）
* **OpenAI Codex CLI / Codex Desktop App**（已实测 `0.171.0-alpha.13` `app-server` 与 `exec --json`）
* **DeepSeek Harness (`dsh`)**（已实测 `0.2.0-rc.2`）
* **Git**

---

## 快速开始

### 1. 探测本地环境（Windows PowerShell）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-env.ps1
```

### 2. 安装 `dsh-mcp-bridge` 依赖并运行单测

```powershell
cd packages\dsh-mcp-bridge
uv sync
uv run pytest -v
```

> **Windows 跨盘符环境提示**：若仓库放在非系统盘（如 `D:\`）且 `uv sync` 查询 `.venv\Scripts\python.exe` 出现 `0xc06d007e` 错误，可将 `UV_PROJECT_ENVIRONMENT` 指向 `C:\` 上的目录，例如：
> `$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv-envs\multi-agent-orchestrator"`

### 3. 运行双服务端 MCP stdio 冒烟测试

```powershell
uv run --directory packages\dsh-mcp-bridge python ..\..\scripts\smoke_test.py
```

`smoke_test.py` 会依次通过真实 `stdio` 连接验证 `dsh-mcp-bridge` 与 `codex-mcp-bridge` 的握手、Tool 列表、模型配额拦截及路径白名单拦截（如仅测 `dsh` 桥接层可追加 `--skip-codex`）。

### 4. 生成本地 MCP 配置并接入主控端

运行脚本自动生成包含本机绝对路径与 Windows `UV_PROJECT_ENVIRONMENT` 的配置（已加入 `.gitignore`，不会提交到 Git；可通过 `-CodexModel` 覆盖默认 `gpt-6-luna`）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\render-mcp-config.ps1
```

然后将生成的 `config/mcp-settings.local.json`（或参考模板 [config/mcp-settings.example.json](config/mcp-settings.example.json)）中的 `mcpServers` 项合并至对应客户端配置：

* **Antigravity**：`~/.gemini/config/mcp_config.json`（可在界面 **Additional Options (...) > MCP Servers** 查看连接状态）
* **Gemini CLI**：`~/.gemini/settings.json`

---

## 暴露的 MCP Tools

### `dsh-mcp-bridge`

| Tool 名称 | 说明 | 核心参数 |
| --- | --- | --- |
| `dsh_health_check` | 探测 `dsh` CLI、`codex` CLI、`127.0.0.1:3080` 端口及备用 API Key 状态 | 无 |
| `dsh_run_task` | 通过 `dsh --profile headless --json` 执行任务并追踪产物与 `trajectory_id` | `task_prompt`, `workspace_dir`, `output_file?`, `model_tier?`, `timeout_sec?` |
| `dsh_resume_or_fork` | 从历史 `trajectory_id` 恢复会话（`resume`）或分叉出新分支继续（`fork`） | `trajectory_id`, `mode?`, `new_instruction?`, `workspace_dir?`, `model_tier?`, `timeout_sec?` |
| `dsh_list_sessions` | 只读检索 `~/.dsh` 原生会话列表（来自 `session_projcache`） | `workspace_dir?`, `limit?`, `search_term?` |
| `dsh_read_session` | 只读读取指定 `dsh` 会话多轮历史（优先解压 `.v4.jsonl.zstd`，自动降级摘要） | `session_id`, `max_turns?`, `max_chars_per_message?` |

### `codex-worker` (`codex-mcp-bridge`)

| Tool 名称 | 说明 | 核心参数 |
| --- | --- | --- |
| `codex_health_check` | 探测 Codex CLI、后端协议（默认 `app-server`）、默认模型及配额白名单 | 无 |
| `codex` | 发起 Codex 持久化会话（默认生成 Desktop 可见的 `source="vscode"` 会话；传 `worktree=true` 走 `exec --worktree`） | `prompt`, `cwd`, `sandbox?`, `model?`, `worktree?`, `timeout_sec?` |
| `codex-reply` | 沿用已有 `thread_id` 继续对话 | `thread_id`, `prompt`, `cwd`, `model?`, `sandbox?`, `timeout_sec?` |
| `codex_list_threads` | 只读检索 Codex Desktop/CLI 持久化会话列表 | `cwd?`, `limit?`, `include_exec?`, `search_term?` |
| `codex_read_thread` | 只读读取指定 Codex 会话的历史轮次与消息摘要 | `thread_id`, `max_turns?`, `max_chars_per_message?` |

### 环境变量

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `DSH_ALLOWED_ROOT` | 当前工作目录 | 允许 `dsh_run_task` 读写的工作区根目录白名单 |
| `DSH_BIN` | `dsh` | `dsh` 可执行文件名称或绝对路径 |
| `DSH_BASE_URL` | `http://127.0.0.1:3080` | `dsh web` 本地探测地址 |
| `DSH_HOME` | 未设置（使用 `dsh` 默认目录） | 可选覆盖 `DSH_HOME`（受限目录写权限时建议指定） |
| `DSH_DEFAULT_TIMEOUT` | `300` | 单任务默认超时时间（秒） |
| `DSH_APPROVAL_POLICY` | `never` | Headless 执行时的审批策略（`never` / `ask`） |
| `DSH_MODEL_FLASH` | `deepseek-flash` | `model_tier="flash"` 映射的模型 ID |
| `DSH_MODEL_REASONER` | `deepseek-v4-pro` | `model_tier="reasoner"` 映射的模型 ID |
| `DSH_ALLOW_API_FALLBACK` | `false` | 设为 `true` 且配置 `DEEPSEEK_API_KEY` 时允许纯推理 API 降级 |
| `DEEPSEEK_API_KEY` | 未设置 | 可选 DeepSeek 官方 API Key（仅降级通道使用） |

---

## 相关文档

* [设计规格说明书 (`DESIGN.md`)](DESIGN.md)
* [主控编排守则 (`AGENTS.md`)](AGENTS.md)
* [路由与熔断策略 (`config/routing-policy.yaml`)](config/routing-policy.yaml)
