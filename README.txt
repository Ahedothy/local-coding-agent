项目名称：Local Coding Agent
Git 仓库：https://github.com/Ahedothy/local-coding-agent

【项目简介】
这是一个从零实现的本地编程智能体。用户提出编程任务后，Agent 通过与大语言模型的多轮对话，自主决定读取文件、执行命令、修改代码，并持续验证结果，直至完成任务。未使用Agent框架；模型输出严格解析为结构化Tool Call；所有工具均在本地执行。

【运行】
要求 Python 3.12+；Web UI 另需 Node.js。
1. 安装：python -m pip install -e .
2. 复制 .env.example 为 .env，填写 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL。
3. CLI：cd backend；python -m coding_agent.cli "工作区路径"
4. Web：终端1进入 backend 运行 python -m uvicorn coding_agent.api:app --port 8000；终端2进入 frontend 执行 npm ci 和 npm run dev。
5. 测试：python -m pip install -e ".[test]"；python -m pytest

【特色功能】
- 自研 Agent Runtime：完成“模型决策—本地工具执行—结果回传—继续推理”的多轮闭环，并用迭代数、工具数、超时、重复失败检测和模型重试避免失控。
- 完整本地工具链：文件读写与搜索、精确替换、多文件补丁、命令执行、Git diff、环境检查和进程管理；Pydantic 生成 schema 并校验参数。
- 安全可控：写文件和执行命令前审批，可预览 diff；限制工作区越界、符号链接逃逸、.git 访问和危险命令。读取文件返回 SHA-256，编辑时校验版本，防止覆盖用户的新修改。
- 可观察、可恢复：统一事件流驱动 CLI、Web SSE、JSONL 和 SQLite；支持多轮追问、历史回放、继续会话及安全撤销。
- 上下文与质量闭环：压缩旧工具结果、读取根目录 AGENTS.md、记录测试/构建等验证证据；提供 Mock Provider、离线评测、196 项测试和 CI。
