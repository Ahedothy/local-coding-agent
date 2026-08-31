# Local Coding Agent

一个面向本地项目的编程智能体。用户用自然语言描述任务后，Agent 会在选定的工作区内阅读代码、搜索相关位置、修改文件、运行测试，并根据工具结果继续迭代，直到给出结论或遇到明确的停止条件。

项目的重点不是提供一个聊天界面，而是自行实现一条可观察、可验证的 Agent 执行链路：模型提出结构化的工具调用意图，工具的参数校验、审批、实际执行、结果回传和循环控制全部发生在本地代码中。

## 项目特点

- **面向真实编程任务**：支持探索代码、构建功能、审查实现、修复缺陷和运行测试，而不只是生成一段代码。
- **从分析到验证的闭环**：Agent 可以先理解项目，再进行多文件修改，执行测试或其他验证命令，并根据失败结果继续修复。
- **可控的本地操作**：命令执行、文件写入、补丁应用和进程管理在产生副作用前请求用户审批；文件修改可在审批时查看完整 diff。
- **CLI 与 Web UI 两种体验**：CLI 适合快速、直接地处理本地任务；Web UI 以时间线、可折叠的 Agent 活动面板、diff 和审批卡片展示执行过程。
- **多轮协作与历史回放**：一次会话可以连续追问；CLI 和 Web UI 共用本地 SQLite 历史，CLI 产生的运行记录可以在 Web UI 中回放并继续。
- **可解释的 Agent 行为**：计划、工具活动、审批、错误和完成状态都以事件形式记录，便于用户理解运行过程，也便于测试和演示。
- **乐观并发保护**：`read_file` 返回内容 SHA-256；编辑工具可携带 `expected_sha256`，若文件在读取后被 IDE、用户或其他进程更新，写入会在副作用发生前拒绝并要求重新读取，避免覆盖较新的修改。
- **项目级指令**：自动读取工作区根目录 `AGENTS.md`，限长注入上下文，并在事件流记录来源、字符数和截断状态。
- **验证与评测**：根据本地命令结果记录验证证据；模型临时错误有限重试；离线 Mock Evaluation 评估 Agent 闭环，GitHub Actions 自动运行检查。

项目没有使用 LangChain、LangGraph、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架或 SDK。允许使用模型厂商的 API 客户端和原生 tool calling，但模型服务端不执行本项目的文件或命令工具。

## 快速开始

### 环境要求

- Python 3.12 或更高版本
- Node.js 和 npm（仅运行 Web UI 时需要）
- 使用真实模型时，需要一个支持 tool calling 的 OpenAI 兼容接口

项目主要在 Windows、Python 3.13 环境下验证，也可以在其他支持 Python、`asyncio` 和本地子进程的系统上运行。

### 安装

在仓库根目录执行：

```powershell
python -m pip install -e .
```

这是可编辑安装：源码修改后不需要重复安装。默认依赖只包含运行所需组件；测试环境请安装 `python -m pip install -e ".[test]"`，开发环境可安装 `python -m pip install -e ".[dev]"`。

### 配置模型

复制 `.env.example` 为 `.env`，填写 OpenAI 兼容接口配置：

```text
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=your-model-name
```

`.env` 已被 Git 忽略，密钥不要提交到仓库、README 或演示视频。`OPENAI_BASE_URL` 可以指向兼容 OpenAI 接口的本地网关或其他服务；模型需要能够返回结构化工具调用。

## 命令行（CLI）

### 推荐用法

在 `backend` 目录启动交互式 CLI：

```powershell
cd backend
python -m coding_agent.cli C:\path\to\workspace
```

只有一个工作目录参数时，默认使用真实模型并进入交互模式。路径中没有空格时可以不加引号；路径包含空格时使用双引号：

```powershell
python -m coding_agent.cli "C:\path\to\my project"
```

启动后在简洁的 `>` 提示符中输入任务，例如：

```text
> 请先了解这个项目的结构，再运行测试并修复失败项。完成后说明修改了什么。
> 继续检查刚才的修改是否有遗漏，并补充必要的测试。
```

输入 `/help` 查看提示，输入 `/exit` 或 `/quit` 退出。空行会被忽略；按 `Ctrl+C` 会正常结束 CLI，不显示 asyncio traceback。

### 审批与 diff

读取文件、搜索代码等只读操作会直接执行。命令执行、文件写入、补丁应用和进程的启动、输入、停止等有副作用的操作会先等待审批。

- **命令审批**：显示待执行命令和工作目录，可选择仅允许本次、允许当前消息后续操作，或拒绝。
- **文件修改审批**：先列出将要修改的文件。输入 `d` 可展开完整 unified diff；较长的 diff 会进入终端分页器，按 Space 翻页、按 `q` 返回审批提示，然后再决定是否允许。
- 拒绝审批后，工具不会执行，Agent 会收到拒绝结果并自行调整方案或向用户说明。

每轮完成时，CLI 默认输出简洁的修改摘要、文件列表以及新增/删除行数，不把很长的 diff 再重复打印一遍。需要诊断原始事件或完整最终 diff 时，可以使用下面的高级选项。

### 高级选项

```powershell
# 使用确定性的本地 Mock provider 做冒烟测试（不需要 API key）
python -m coding_agent.cli --provider mock C:\path\to\workspace

# 将结构化事件追加到 JSONL，便于演示或调试
python -m coding_agent.cli --event-log ..\event_logs\demo.jsonl C:\path\to\workspace

# 输出完整事件流，适合调试脚本；普通用户不需要开启
python -m coding_agent.cli --raw-events C:\path\to\workspace

# 在最终回答中显示本轮累计的完整 diff
python -m coding_agent.cli --show-diff C:\path\to\workspace

# 一次性任务
python -m coding_agent.cli --workspace C:\path\to\workspace --one-shot "检查并修复测试"
```


## Web UI

Web UI 由 FastAPI 后端和 React + TypeScript + Vite 前端组成。它们复用同一套 Agent Core，页面只负责展示事件和提交用户操作，不在浏览器中重新实现 Agent 循环。

终端一启动后端：

```powershell
cd backend
python -m uvicorn coding_agent.api:app --port 8000
```

终端二启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

打开 Vite 输出的地址（通常是 `http://127.0.0.1:5173/`），选择一个本地工作区后即可开始。首页提供四类可编辑的起点：探索代码、构建功能、审查代码、修复问题；点击只会填入任务，不会绕过用户确认自动执行。

运行过程中，页面会显示用户消息、模型活动、工具调用和最终答复。Agent 活动面板可以折叠；文件修改会按文件展开 diff，并标出新增和删除行；审批卡片提供“仅批准一次”“自动批准本条消息后续操作”和“拒绝”；已经完成的修改可以使用“撤销修改”。

## CLI 与 Web UI 的历史协作

运行事件、会话元数据、每次运行的事件流以及受限的上下文快照会保存到本地 SQLite。默认位置为：

```text
backend\history.db
```

可以通过 `CODING_AGENT_HISTORY_DIR` 指定另一个数据库文件，或指定一个用于创建 `history.db` 的目录。CLI 本身保持轻量，不提供历史浏览命令；它写入的记录会出现在 Web UI 的“会话”列表中。Web UI 可以：

1. 按工作区分组查看会话；
2. 回放某次运行的消息、工具活动、审批、错误、最终答复和 diff；
3. 以选中的历史上下文为起点继续对话，而不是重新执行旧的工具调用。

当前交互进程中的会话仍由内存中的 `Session` 和 `ContextManager` 驱动；数据库用于记录和恢复已保存的运行状态。工具输出会做长度限制，但不会自动识别和脱敏所有秘密信息，因此不要把包含敏感内容的历史数据库上传或共享。

## Agent 能做什么

模型可以使用以下本地工具组合完成任务：

| 类别 | 工具 | 典型用途 |
| --- | --- | --- |
| 文件与代码 | `list_files`、`read_file`、`search_files` | 浏览目录、读取文本、定位定义和调用点 |
| 文件修改 | `write_file`、`replace_in_file`、`apply_patch` | 写入文件、精确替换、小范围或多文件补丁 |
| 验证与命令 | `execute_command`、`git_diff` | 运行测试、构建项目、查看 Git 状态和 diff |
| 环境检查 | `get_file_info`、`list_directory_tree`、`inspect_environment` | 判断文件类型、快速了解目录结构、检查运行时和项目标记 |
| 长驻进程 | `manage_process` | 启动、查看、读写和停止开发服务器或交互式程序 |

工具返回统一的成功或失败结果，Agent 会把结果重新放入上下文，再决定下一步。简单问题可以直接回答；复杂问题通常会先给出简短计划，并在工具结果后进行检查或反思。

编辑工具有明确分工：`replace_in_file` 用于一个小的、精确的连续替换；涉及多处、结构变化或多个文件时使用标准 unified diff 的 `apply_patch`。复杂补丁可以先用 `dry_run` 做本地校验，确认上下文和生成的 diff 后再正式写入。多个工具调用按顺序执行，并受每轮调用上限约束，以避免工具之间产生难以解释的竞态。

## 执行流程

一次用户消息的核心流程如下：

```text
用户任务
  → 加入会话上下文
  → 构造模型请求
  → 解析模型文本和工具调用
  → 通过 ToolExecutor 校验并执行本地工具
  → 将工具结果写回上下文
  → 重复，直到得到最终答复或触发停止条件
```

Agent Runtime 为每一轮维护迭代次数、工具调用次数、超时和重复失败调用等计数。达到上限、模型连续返回无效工具调用、任务超时或用户取消时，运行会以可解释的错误或停止状态结束，而不是无限循环。

## 系统架构

```text
CLI ─────────────┐
                 ├─ Agent Runtime ── ContextManager
Web UI ─ FastAPI ┘         │
                           ├─ ModelProvider
                           ├─ ToolRegistry / ToolExecutor
                           ├─ ApprovalGate
                           ├─ Workspace
                           └─ EventBus ── CLI / SSE / SQLite / JSONL
```

- **Agent Runtime**：实现模型调用、工具调用、结果回传和终止条件。
- **ModelProvider**：把内部请求转换为 OpenAI 兼容请求，再把模型响应归一化为内部 `ModelResponse` 和 `ToolCall`。
- **ContextManager**：维护 system、user、assistant、tool 消息，在字符预算超限时裁剪旧的非系统内容，并限制过长工具输出。
- **ToolRegistry**：登记工具、暴露 JSON schema、检查名称唯一性。
- **ToolExecutor**：使用同一套 Pydantic 参数模型做校验，执行工具并把异常转换成结构化结果。
- **Workspace**：集中处理路径解析、工作区边界、文件大小和文本/二进制判断，以及命令工作目录校验。
- **EventBus**：发布结构化事件，让 CLI、Web SSE、SQLite 历史和 JSONL 日志共享同一条可观察链路。

模型厂商的 tool calling 在这里仅作为“模型表达工具意图的结构化格式”。模型服务端不会访问本地文件，也不会替项目执行命令；所有工具执行和循环控制由仓库中的 Python 代码完成。

## 操作边界

直接文件操作和命令工作目录都会经过工作区校验：路径会解析为真实路径，阻止常见的 `..` 越界、工作区外绝对路径、符号链接逃逸以及 `.git` 元数据访问；文本读写有大小限制，命令使用参数数组、`shell=False`、固定超时和有上限的输出。对明显危险的可执行文件还有额外的拒绝规则。

这些措施解决的是本地开发中的误操作、路径越界和 shell 注入风险，并不改变子进程的用户权限。使用不可信项目或恶意代码时，应在容器、虚拟机、低权限账户等更强的系统隔离环境中运行本项目；审批也应当由用户结合命令内容自行判断。

## 演示项目

`demo_task_manager` 是一个小型多文件任务管理库，故意在搜索、分页和报表聚合处留下缺陷。可以在演示前恢复到失败状态，然后启动真实 provider：

```powershell
cd backend
python -m coding_agent.cli "C:\path\to\local-coding-agent\demo_task_manager"
```

在 `>` 中输入：

```text
请先阅读项目结构和测试，运行完整测试套件，修复失败项，尽量保持修改最小，并再次运行测试验证。
```

一个有说服力的执行轨迹通常是：读取测试和实现 → 运行测试发现失败 → 修改两个相关文件 → 再次运行测试通过 → 汇总修改。演示时可以保留审批、Thinking Process、测试失败到成功的过程，能比单文件计算器更充分地体现 Agent 的自主分析和闭环验证能力。

## 测试与开发

运行完整测试：

```powershell
python -m pytest
```

测试使用 `MockModelProvider` 覆盖 Agent 循环、工具调用、上下文裁剪、审批、历史和错误处理，不需要网络或 API key。Windows 当前账户若没有创建符号链接的权限，相关安全场景会被 pytest 标记为跳过；这反映的是操作系统权限差异，不是测试失败。

需要查看事件时间线时，可以先记录 JSONL，再使用只读 trace 工具：

```powershell
cd backend
python -m coding_agent.cli --event-log ..\event_logs\demo.jsonl C:\path\to\workspace
python -m coding_agent.trace ..\event_logs\demo.jsonl --timeline
```

trace 只解析已记录的事件，不会再次调用模型、执行工具或恢复活动中的审批。更完整的模块边界、测试策略和演示安排见：

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：架构、职责和设计取舍
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：当前架构事实（含历史说明）


## 常见问题

**为什么终端里找不到 `coding-agent` 命令？**  本项目的推荐入口是模块形式，不依赖系统 PATH 中存在额外的控制台脚本：

```powershell
cd backend
python -m coding_agent.cli C:\path\to\workspace
```

**Windows 上后端启动后命令执行报 `NotImplementedError`？**  不要使用 `uvicorn --reload`。某些 Windows 环境下 reload 子进程会选择不支持 `create_subprocess_exec` 的事件循环；修改 Python 代码后手动重启后端即可。

**路径什么时候需要引号？**  路径不含空格时可以直接写；含空格时必须用双引号。PowerShell 的反斜杠不需要额外转义。

**没有 API key 能否试用？**  可以使用 `--provider mock` 做本地冒烟测试，但它不会模拟真实模型的完整规划能力；完整多轮和工具协作应使用真实 provider，自动化测试则使用仓库内的 Mock provider。
