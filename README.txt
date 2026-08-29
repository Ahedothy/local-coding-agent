项目名称：lvyiyou-coding-agent
Git 仓库：https://github.com/Ahedothy/lvyiyou-coding-agent

运行环境：Python 3.12+、Node.js 20+。

安装后端：
python -m pip install -e ".[test,server]"

配置：
复制 .env.example 为 .env，设置 OPENAI_API_KEY、OPENAI_BASE_URL 和
OPENAI_MODEL。API Key 不得提交到 Git。

运行中文 Web 界面：
cd backend
python -m uvicorn coding_agent.api:app --port 8000

另开终端：
cd frontend
npm ci
npm run dev

也可使用 CLI：
cd backend
python -m coding_agent.cli --workspace "项目目录" --provider real "编程任务"

特色：
本项目未使用任何 Agent 框架或 Agent SDK，自行实现模型工具调用循环、上下文压缩、
本地文件与命令工具、文件工作区路径限制、命令 cwd 校验、超时和重复调用终止、操作审批、修改 diff 与撤销、
多轮对话、SSE 事件展示及 SQLite 历史恢复。新会话提供“探索代码、构建功能、审查代码、修复问题”快捷入口，
点击后可继续编辑任务再发送。模型只负责产生工具调用，本地工具的校验、
执行和结果回传均由项目代码完成。命令在选定项目目录中使用参数数组、shell=False、
审批、超时和输出限制执行。自动化测试共 169 项。
