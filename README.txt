项目名称：Local Coding Agent
Git 仓库：https://github.com/Ahedothy/local-coding-agent

【安装】
在项目根目录执行：
python -m pip install -e .
该命令安装运行依赖；测试环境另执行：python -m pip install -e ".[test]"。
复制 .env.example 为 .env，设置OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL。
API Key 等凭据不要提交到仓库。

【命令行使用（CLI）】
cd backend
python -m coding_agent.cli C:\path\to\workspace
工作目录含空格时使用双引号。
在 > 输入任务，/help 查看提示，/exit 或 /quit 退出。
执行命令或修改文件前会请求审批：y 允许一次，a 允许本轮；文件修改时输入 d 可查看完整 diff。

【网页界面使用（Web UI）】
终端一：
cd backend
python -m uvicorn coding_agent.api:app --port 8000
终端二：
cd frontend
npm ci
npm run dev

【特色功能】
- 用自然语言完成真实编程任务：阅读项目、定位问题、修改多个文件、运行测试，并根据结果继续修复；
- Agent 自主组合文件读取、代码搜索、文件编辑、命令执行等工具，完成从分析到验证的闭环；
- 命令和文件修改先请求审批，可查看涉及的文件与 diff，拒绝操作或只授权当前轮次；
- CLI 展示计划、工具活动和结论；Web UI 提供可展开 diff、撤销修改、历史回放和继续会话；
- 支持同一会话多轮追问，CLI 运行记录可在 Web UI 中继续，适合持续迭代开发。
- 原生读取根目录 AGENTS.md 作为项目指令，并记录注入字符数和截断状态；
- 以 SHA-256 做乐观并发校验，防止基于过期文件内容覆盖新修改；
- 验证证据账本、确定性上下文压缩、模型临时错误重试和离线 Agent 任务评测；
- GitHub Actions 自动运行后端测试、Mock 评测和前端类型检查。

【其它说明】
项目未使用 Agent 框架或 Agent SDK，重要逻辑均自行实现。real provider 用于实际任务，
mock provider、事件日志和离线评测用于确定性验证。demo_task_manager 可演示
“阅读项目—修改代码—运行测试—根据结果继续修复”的完整流程。
