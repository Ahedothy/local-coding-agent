项目名称：lvyiyou-coding-agent
Git 仓库：https://github.com/Ahedothy/lvyiyou-coding-agent

配置：复制 .env.example 为 .env，设置 OPENAI_API_KEY、OPENAI_BASE_URL、
OPENAI_MODEL。

安装（首次在项目根目录执行）：
python -m pip install -e .

Web：
终端1：
cd backend
python -m uvicorn coding_agent.api:app --port 8000
终端2：
 cd frontend、npm ci、npm run dev。

CLI：
cd backend
python -m coding_agent.cli 项目目录
（目录含空格时需使用双引号）

只给目录时默认使用 real provider 并进入交互模式，在 > 提示符输入任务；
/help 查看提示，/exit 或 /quit 退出。目录后追加任务会执行一次并退出；也支持
--interactive、--one-shot，--provider mock 可离线测试。

CLI 默认展示简洁的计划、工具活动、审批、失败和完成状态。
--event-log 保存完整日志，--raw-events 输出完整事件；TTY 自动启用 ANSI 颜色，
重定向或 NO_COLOR 时降级为纯文本，最终 diff 高亮增删行。
最终回复默认只汇总修改；需要完整 diff 时加 --show-diff。
执行命令、编辑文件等有副作用的操作前会在终端询问：y 允许一次，a 允许本轮，
文件修改时可用 d 查看完整 diff，其他输入拒绝；退出查看后返回审批。CLI 不提供历史浏览命令，会把事件写入 Web UI 共用的
backend\history.db，之后可在 Web UI 中查看和回放；可用 CODING_AGENT_HISTORY_DIR
指定其他数据库位置。

特色：不依赖 Agent 框架或 SDK，自行实现工具调用循环、上下文压缩、本地工具、
工作区边界、审批、超时、修改撤销、多轮对话、SSE 展示及 SQLite 历史恢复；新会话
提供探索代码、构建功能、审查代码、修复问题快捷入口。
测试 177 项。
