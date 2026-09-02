import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactElement, ReactNode } from "react";
import {
  Bug,
  FolderCog,
  FolderSearch,
  BrainCircuit,
  Bot,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Clock3,
  Code2,
  ChevronDown,
  FileWarning,
  FileCode2,
  FolderOpen,
  Hammer,
  LoaderCircle,
  ListChecks,
  Pencil,
  RotateCcw,
  RefreshCw,
  ScanSearch,
  Send,
  ShieldCheck,
  ShieldX,
  SquarePen,
  Terminal,
  Trash2,
  UserRound,
  Wrench,
  XCircle,
} from "lucide-react";

type AgentEvent = {
  event_id: string;
  type: string;
  session_id: string;
  timestamp: string;
  iteration: number | null;
  payload: Record<string, unknown>;
};

type SessionResponse = { session_id: string; workspace_root: string; status?: string; run_id?: string | null };
type RunResponse = { run_id: string; session_id: string; status: string };
type HistoryItem = {
  run_id: string;
  session_id: string;
  workspace_root?: string | null;
  task?: string | null;
  title?: string | null;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  iterations?: number;
  tool_calls?: number;
  event_count?: number;
  turn_count?: number;
  error?: string | null;
};
type HistoryRecord = { summary: HistoryItem; events: AgentEvent[] };
type FilePreview = { path: string; content: string; truncated?: boolean };
type WorkspaceEntry = { path: string; kind: "file" | "directory" };
type WorkspacePhase = "idle" | "selecting" | "loading";
type PreviewState = "idle" | "loading" | "ready" | "unsupported" | "error";
type BackendStatus = "checking" | "online" | "offline";
type ActivityStep = { event: AgentEvent; events: AgentEvent[] };
type ConversationTurn = { key: string; userEvent: AgentEvent; events: AgentEvent[] };

function mergeAgentEvents(...groups: AgentEvent[][]): AgentEvent[] {
  const byId = new Map<string, AgentEvent>();
  groups.flat().forEach((event) => byId.set(event.event_id, event));
  return [...byId.values()].sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";
const BACKEND_UNAVAILABLE_MESSAGE =
  "无法连接本地后端。请先在 backend 目录运行 python -m uvicorn coding_agent.api:app --port 8000，然后重试。";

function isNetworkRequestError(reason: unknown): boolean {
  if (!(reason instanceof Error)) return false;
  return reason instanceof TypeError
    || /failed to fetch|networkerror|network request failed|load failed/i.test(reason.message);
}

function requestErrorMessage(reason: unknown, fallback: string): string {
  if (isNetworkRequestError(reason)) return BACKEND_UNAVAILABLE_MESSAGE;
  if (reason instanceof Error && reason.message.trim()) return reason.message;
  return fallback;
}
const eventLabels: Record<string, string> = {
  session_started: "会话已创建",
  user_message: "用户消息",
  iteration_started: "开始处理",
  model_request: "模型请求",
  model_response: "模型响应",
  model_retry_scheduled: "模型调用将重试",
  tool_started: "开始执行工具",
  tool_finished: "工具执行完成",
  tool_failed: "工具执行失败",
  approval_requested: "等待审批",
  approval_resolved: "审批已处理",
  assistant_message: "Agent 回复",
  plan: "计划",
  reflection: "检查结果",
  context_truncated: "整理上下文",
  context_compacted: "压缩上下文",
  workspace_instructions_loaded: "已加载项目指令",
  verification_updated: "验证证据更新",
  agent_finished: "Agent 已完成",
  agent_error: "Agent 出错",
};
const eventTypes = Object.keys(eventLabels);

const starterActions = [
  {
    id: "explore",
    title: "探索代码",
    description: "了解项目结构、入口和测试方式。",
    icon: FolderSearch,
    prompt:
      "请探索这个代码项目。检查项目结构、入口和测试方式，然后总结主要组件以及运行方法。不要修改文件。",
  },
  {
    id: "build",
    title: "构建新功能",
    description: "规划并实现功能，同时补充测试。",
    icon: Hammer,
    prompt:
      "请先检查这个项目，然后实现所需功能。先给出简短计划，再完成必要修改并运行相关测试。",
  },
  {
    id: "review",
    title: "审查代码",
    description: "发现正确性、可靠性和可维护性问题。",
    icon: ScanSearch,
    prompt:
      "请审查这个代码项目的正确性、可靠性、安全性和可维护性。检查相关文件和测试，然后使用文件路径报告问题并给出具体建议。不要修改文件。",
  },
  {
    id: "fix",
    title: "修复问题",
    description: "定位问题，最小修改并验证。",
    icon: Bug,
    prompt:
      "请找出并修复这个项目当前失败的测试或 Bug。先检查相关代码，进行最小修改，然后运行完整的相关测试套件进行验证。",
  },
] as const;

function eventTone(type: string): "neutral" | "working" | "success" | "danger" {
  if (["tool_started", "model_request", "iteration_started"].includes(type)) return "working";
  if (type === "approval_requested") return "working";
  if (type === "plan") return "working";
  if (["tool_finished", "agent_finished", "context_compacted", "approval_resolved", "verification_updated"].includes(type)) return "success";
  if (["tool_failed", "agent_error"].includes(type)) return "danger";
  return "neutral";
}

function iconForEvent(type: string) {
  if (type.startsWith("tool") && type !== "tool_failed") return <Terminal size={15} />;
  if (type === "plan") return <ListChecks size={15} />;
  if (type === "reflection") return <RotateCcw size={15} />;
  if (type === "tool_failed" || type === "agent_error") return <XCircle size={15} />;
  if (type === "approval_requested") return <ShieldCheck size={15} />;
  if (type === "agent_finished" || type === "tool_finished") return <CheckCircle2 size={15} />;
  if (type.startsWith("model") || type === "assistant_message") return <Bot size={15} />;
  if (type.startsWith("context")) return <Code2 size={15} />;
  return <FileCode2 size={15} />;
}

function formatTime(timestamp: string) {
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatHistoryDate(timestamp?: string | null) {
  if (!timestamp) return "--";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? "--" : date.toLocaleDateString();
}

function normalizeProjectPath(workspaceRoot?: string | null) {
  return (workspaceRoot ?? "").replaceAll("\\", "/").replace(/\/+$/, "");
}

function projectName(workspaceRoot?: string | null) {
  const normalized = normalizeProjectPath(workspaceRoot);
  return normalized.split("/").at(-1) || "项目";
}

function projectNameWithDisambiguation(workspaceRoot: string | null | undefined, duplicateNames: Set<string>) {
  const name = projectName(workspaceRoot);
  if (!duplicateNames.has(name)) return name;
  const normalized = normalizeProjectPath(workspaceRoot);
  const parts = normalized.split("/").filter(Boolean);
  return parts.length > 1 ? `${name} · ${parts.at(-2)}` : name;
}

function projectKey(workspaceRoot?: string | null) {
  return normalizeProjectPath(workspaceRoot).toLocaleLowerCase();
}

function formatDuration(events: AgentEvent[]) {
  if (events.length < 2) return "--";
  const start = new Date(events[0].timestamp).getTime();
  const end = new Date(events.at(-1)?.timestamp ?? events[0].timestamp).getTime();
  const seconds = Math.max(0, (end - start) / 1000);
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

function runStatusLabel(status: string) {
  const labels: Record<string, string> = {
    "workspace-required": "请选择项目",
    running: "执行中",
    ready: "就绪",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] ?? `状态：${status}`;
}

function readPayloadString(event: AgentEvent, key: string) {
  const value = event.payload[key];
  return typeof value === "string" ? value : undefined;
}

function toolDisplayName(toolName: unknown) {
  const names: Record<string, string> = {
    list_files: "浏览文件",
    read_file: "读取文件",
    write_file: "写入文件",
    replace_in_file: "替换文件内容",
    apply_patch: "应用修改",
    execute_command: "运行命令",
    search_files: "搜索代码",
    get_file_info: "查看文件信息",
    list_directory_tree: "查看目录结构",
    git_diff: "查看代码变更",
    inspect_environment: "检查运行环境",
    manage_process: "管理本地进程",
  };
  const rawName = typeof toolName === "string" && toolName.trim() ? toolName.trim() : "本地工具";
  // Event payloads use the canonical underscore name.  Normalize spaces as a
  // defensive measure for older persisted events, so a valid tool is not
  // misleadingly rendered as “未知工具（inspect environment）”.
  const canonicalName = rawName.replaceAll(" ", "_");
  return names[canonicalName] ?? `未知工具（${rawName.replaceAll("_", " ")}）`;
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function isIgnorableGitDiffFailure(event: AgentEvent) {
  if (event.type !== "tool_failed" || event.payload.tool_name !== "git_diff") return false;
  const output = objectValue(event.payload.output);
  if (output?.repository === false) return true;
  const error = readPayloadString(event, "error") ?? "";
  return /not\s+(?:a\s+)?git\s+repository|git executable is not available/i.test(error);
}

function isHiddenVerificationEvent(event: AgentEvent) {
  if (event.type !== "verification_updated") return false;
  const summary = objectValue(event.payload.verification);
  const status = typeof summary?.status === "string" ? summary.status : "";
  return status === "unverified" || status === "not_required";
}

function activityOutput(step: ActivityStep) {
  const completed = [...step.events].reverse().find((event) => ["tool_finished", "tool_failed"].includes(event.type));
  return objectValue(completed?.payload.output);
}

function activityPresentation(step: ActivityStep) {
  const event = step.event;
  const output = activityOutput(step);
  const toolName = toolDisplayName(event.payload.tool_name);
  if (event.type === "iteration_started") return { title: `第 ${event.iteration ?? ""} 步`.trim(), description: "正在规划下一步。" };
  if (event.type === "model_request") return { title: "分析任务", description: "正在生成下一步操作。" };
  if (event.type === "model_response") {
    const toolCallCount = Number(event.payload.tool_call_count ?? 0);
    return toolCallCount > 0
      ? { title: "已选择操作", description: `已安排 ${toolCallCount} 个本地操作。` }
      : { title: "准备回复", description: "正在整理结果。" };
  }
  if (event.type === "model_retry_scheduled") {
    const attempt = Number(event.payload.attempt ?? 0);
    const reason = String(event.payload.reason ?? "临时错误");
    const delay = Number(event.payload.delay_seconds ?? 0);
    const reasonLabel = reason.includes("429") || reason.includes("rate") ? "请求频率受限" : reason.includes("connection") ? "网络连接暂时失败" : reason.includes("500") || reason.includes("server") ? "模型服务暂时不可用" : reason.includes("timeout") ? "模型响应超时" : "模型响应异常";
    return { title: "模型调用将重试", description: `${reasonLabel}，第 ${attempt} 次尝试将在 ${delay.toFixed(1)} 秒后进行。` };
  }
  if (event.type === "plan") return { title: "计划", description: readPayloadString(event, "content") ?? "正在拆分任务步骤。" };
  if (event.type === "reflection") return { title: "检查结果", description: readPayloadString(event, "content") ?? "正在检查执行结果。" };
  if (event.type === "tool_started" || event.type === "tool_finished") {
    if (event.payload.tool_name === "execute_command" && typeof output?.command === "string") return { title: toolName, description: output.command };
    if (typeof output?.path === "string") return { title: toolName, description: output.path };
    if (Array.isArray(output?.touched_files)) return { title: toolName, description: `已在本地更新 ${output.touched_files.length} 个文件。` };
    return { title: event.type === "tool_started" ? toolName : `${toolName}已完成`, description: event.type === "tool_started" ? "正在工作区中执行。" : "操作已完成。" };
  }
  if (event.type === "tool_failed") {
    const error = readPayloadString(event, "error") ?? "本地操作未能完成。";
    return error.includes("replacement count mismatch")
      ? { title: "更新需要重新匹配", description: "没有精确找到目标文本。Agent 应先重新读取文件，再重试。" }
      : error.includes("unified diff contains no hunks")
        ? { title: "补丁没有实际修改", description: "Agent 只提供了文件头，没有真正的 @@ 修改块。应重新读取文件，使用 replace_in_file 完成简单编辑，或重新生成完整补丁。" }
      : error.includes("hunk line count mismatch")
        ? { title: "补丁行数需要修正", description: "@@ 头部的行数与修改块不匹配。旧行数=上下文行+删除行，新行数=上下文行+新增行。Agent 应重新生成修改块。" }
      : error.includes("invalid unified diff")
        ? { title: "需要重新生成补丁", description: "补丁格式无效。Agent 应重新读取文件并生成新的补丁。" }
      : error.includes("content fingerprint mismatch")
        ? { title: "文件指纹不匹配", description: "本次修改携带的文件指纹与当前内容不一致。Agent 应重新读取文件后再生成修改。" }
      : error.includes("stale file")
        ? { title: "文件已被更新", description: "文件在 Agent 读取后发生了变化。请重新读取最新内容，再生成修改。" }
        : { title: `${toolName}失败`, description: error };
  }
  if (event.type === "approval_requested") {
    if (event.payload.automatic === true && event.payload.approval_scope === "session") {
      return { title: "已自动批准", description: "当前操作已启用会话级自动批准。" };
    }
    return event.payload.automatic === true
      ? { title: "已自动批准", description: "当前操作已被本条消息的审批范围覆盖。" }
      : { title: "需要审批", description: "文件修改或命令执行前需要你的确认。" };
  }
  if (event.type === "approval_resolved") {
    const decision = readPayloadString(event, "decision");
    if (decision === "approved" && event.payload.automatic === true) {
      return event.payload.approval_scope === "session"
        ? { title: "已自动批准", description: "已启用会话级自动批准。" }
        : { title: "已自动批准", description: "已对本条消息自动放行。" };
    }
    if (decision === "approved" && event.payload.approval_scope === "current_turn") {
      return { title: "已批准本条消息", description: "本次审批覆盖当前消息剩余的本地操作。" };
    }
    return decision === "approved"
      ? { title: "审批通过", description: "本地操作可以继续执行。" }
      : { title: "审批拒绝", description: "本地操作已被你的决定拦截。" };
  }
  if (event.type === "context_truncated") return { title: "上下文已整理", description: "已保留最近的关键信息。" };
  if (event.type === "context_compacted") return { title: "上下文已压缩", description: "已压缩对话内容，以控制输入长度。" };
  if (event.type === "workspace_instructions_loaded") {
    const found = event.payload.found === true;
    const chars = Number(event.payload.injected_chars ?? 0);
    const truncated = event.payload.truncated === true;
    if (!found) return { title: "未找到项目指令", description: "工作区根目录没有 AGENTS.md。" };
    return { title: "已加载项目指令", description: `已注入 AGENTS.md ${chars} 个字符${truncated ? "（内容已截断）" : ""}。` };
  }
  if (event.type === "verification_updated") {
    const summary = objectValue(event.payload.verification);
    const status = typeof summary?.status === "string" ? summary.status : "";
    const labels: Record<string, string> = {
      verified: "验证通过",
      partially_verified: "部分验证",
      unverified: "尚未验证",
      not_required: "无需验证",
    };
    return { title: labels[status] ?? "验证状态更新", description: `工作区版本 v${String(summary?.workspace_version ?? "?")} 的证据账本已更新。` };
  }
  if (event.type === "agent_error") {
    const error = readPayloadString(event, "error") ?? "任务未完成。";
    if (error.includes("file changed repeatedly") || error.includes("file kept changing")) {
      return { title: "文件持续变化，已停止", description: "为避免覆盖更新后的内容，Agent 已停止。请检查文件后重新发起修改。" };
    }
    return { title: "执行已停止", description: error };
  }
  return { title: eventLabels[event.type] ?? "Agent 活动", description: "Agent 状态已更新。" };
}

function buildActivitySteps(events: AgentEvent[]): ActivityStep[] {
  const steps: ActivityStep[] = [];
  const toolSteps = new Map<string, ActivityStep>();
  const approvalSteps = new Map<string, ActivityStep>();
  let modelStep: ActivityStep | undefined;

  for (const event of events) {
    if (event.type === "iteration_started") continue;
    if (event.type === "model_request") {
      modelStep = { event, events: [event] };
      steps.push(modelStep);
      continue;
    }
    if (event.type === "model_response" && modelStep) {
      modelStep.events.push(event);
      modelStep.event = event;
      continue;
    }
    if (event.type === "plan" || event.type === "reflection") {
      if (modelStep && modelStep.event.type === "model_response") {
        modelStep.events.push(event);
        modelStep.event = event;
      } else {
        steps.push({ event, events: [event] });
      }
      modelStep = undefined;
      continue;
    }
    if (event.type === "tool_started") {
      const step = { event, events: [event] };
      steps.push(step);
      const toolCallId = event.payload.tool_call_id;
      if (typeof toolCallId === "string") toolSteps.set(toolCallId, step);
      modelStep = undefined;
      continue;
    }
    if (event.type === "approval_requested") {
      const step = { event, events: [event] };
      steps.push(step);
      const approvalId = event.payload.approval_id;
      if (typeof approvalId === "string") approvalSteps.set(approvalId, step);
      modelStep = undefined;
      continue;
    }
    if (event.type === "approval_resolved") {
      const approvalId = event.payload.approval_id;
      const step = typeof approvalId === "string" ? approvalSteps.get(approvalId) : undefined;
      if (step) {
        step.events.push(event);
        step.event = event;
      } else {
        steps.push({ event, events: [event] });
      }
      continue;
    }
    if (event.type === "tool_finished" || event.type === "tool_failed") {
      const toolCallId = event.payload.tool_call_id;
      const step = typeof toolCallId === "string" ? toolSteps.get(toolCallId) : undefined;
      if (step) {
        step.events.push(event);
        step.event = event;
      } else {
        steps.push({ event, events: [event] });
      }
      continue;
    }
    if (event.type === "context_truncated" || event.type === "context_compacted" || event.type === "workspace_instructions_loaded" || event.type === "verification_updated" || event.type === "agent_error") {
      steps.push({ event, events: [event] });
      modelStep = undefined;
    }
  }
  return steps;
}

function activityDetails(step: ActivityStep) {
  const event = step.event;
  const output = activityOutput(step);
  const details: Array<[string, string]> = [["状态", event.type === "tool_failed" || event.type === "agent_error" ? "需要处理" : event.type === "approval_resolved" ? (readPayloadString(event, "decision") === "approved" ? "已批准" : "已拒绝") : ["tool_finished", "agent_finished"].includes(event.type) ? "已完成" : "进行中"]];
  if (typeof event.payload.tool_name === "string") details.push(["工具", toolDisplayName(event.payload.tool_name)]);
  if (event.iteration !== null) details.push(["迭代", String(event.iteration)]);
  if (typeof output?.path === "string") details.push(["路径", output.path]);
  if (typeof output?.command === "string") details.push(["命令", output.command]);
  if (typeof output?.process_id === "string") details.push(["进程", output.process_id]);
  if (typeof output?.status === "string") details.push(["进程状态", output.status]);
  if (typeof output?.pid === "number") details.push(["PID", String(output.pid)]);
  if (typeof output?.operation === "string" && output.operation !== "status") details.push(["操作", output.operation]);
  if (Array.isArray(output?.touched_files)) details.push(["更新文件数", String(output.touched_files.length)]);
  if (typeof output?.duration_seconds === "number") details.push(["耗时", `${output.duration_seconds.toFixed(2)} 秒`]);
  if (typeof event.payload.tool_call_count === "number") details.push(["工具调用数", String(event.payload.tool_call_count)]);
  if (typeof event.payload.message_count === "number") details.push(["消息数", String(event.payload.message_count)]);
  const approvalDetailsValue = objectValue(event.payload.details);
  if (event.type === "approval_requested" && approvalDetailsValue) {
    if (typeof approvalDetailsValue.command === "string") details.push(["命令", approvalDetailsValue.command]);
    if (Array.isArray(approvalDetailsValue.command)) details.push(["命令", approvalDetailsValue.command.join(" ")]);
    if (typeof approvalDetailsValue.path === "string") details.push(["路径", approvalDetailsValue.path]);
  }
  const error = readPayloadString(event, "error");
  if (error) details.push(["错误", error]);
  return details;
}

const thinkingEventTypes = [
  "iteration_started",
  "model_request",
  "model_response",
  "model_retry_scheduled",
  "plan",
  "reflection",
  "tool_started",
  "tool_finished",
  "tool_failed",
  "approval_requested",
  "approval_resolved",
  "context_truncated",
  "context_compacted",
  "workspace_instructions_loaded",
  "verification_updated",
  "agent_error",
];

function buildConversationTurns(events: AgentEvent[]): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  let current: ConversationTurn | undefined;
  for (const event of events) {
    if (event.type === "user_message") {
      const turnIndex = event.payload.turn_index;
      current = {
        key: typeof turnIndex === "number" ? `turn-${turnIndex}` : event.event_id,
        userEvent: event,
        events: [event],
      };
      turns.push(current);
      continue;
    }
    if (current) current.events.push(event);
  }
  return turns;
}

function finalAnswerForEvents(events: AgentEvent[]) {
  const event = [...events].reverse().find((candidate) => candidate.type === "assistant_message" && candidate.payload.tool_call_count === 0 && readPayloadString(candidate, "content"));
  return event ? readPayloadString(event, "content") : undefined;
}

function verificationForEvents(events: AgentEvent[]) {
  const event = [...events].reverse().find((candidate) => candidate.type === "verification_updated" || candidate.type === "agent_finished");
  return event ? objectValue(event.payload.verification) : undefined;
}

function verificationStatusLabel(status: unknown) {
  const labels: Record<string, string> = {
    verified: "已验证",
    partially_verified: "部分验证",
    unverified: "未验证",
    not_required: "无需验证",
  };
  return typeof status === "string" ? labels[status] ?? status : "未验证";
}

function verificationKindLabel(kind: unknown) {
  const labels: Record<string, string> = {
    test: "测试",
    build: "构建",
    lint: "Lint",
    typecheck: "类型检查",
    smoke: "Smoke Test",
    config: "配置检查",
    benchmark: "基准检查",
    scope: "范围检查",
    other: "其他检查",
  };
  return typeof kind === "string" ? labels[kind] ?? kind : "验证";
}

function VerificationCard({ summary }: { summary: Record<string, unknown> }) {
  const status = typeof summary.status === "string" ? summary.status : "unverified";
  const workspaceVersion = typeof summary.workspace_version === "number" ? summary.workspace_version : 0;
  const evidence = (Array.isArray(summary.evidence) ? summary.evidence : []).filter((item) => {
    const entry = objectValue(item);
    return entry?.workspace_version === workspaceVersion;
  });
  const currentCount = typeof summary.current_evidence_count === "number" ? summary.current_evidence_count : 0;
  return <section className={`verification-card verification-${status}`} aria-label="验证证据">
    <div className="verification-card-header">
      <div className="verification-card-title"><ShieldCheck size={15} /><strong>验证证据</strong><span className="verification-status-chip">{verificationStatusLabel(status)}</span></div>
      <span className="verification-version">工作区 v{String(summary.workspace_version ?? 0)}</span>
    </div>
    <p className="verification-card-summary">当前版本有 {currentCount} 条有效验证证据。</p>
    {evidence.length > 0 && <div className="verification-evidence-list">{evidence.slice(-5).reverse().map((item, index) => {
      const entry = objectValue(item);
      if (!entry) return null;
      const success = entry.success === true;
      const criteria = Array.isArray(entry.criteria) ? entry.criteria.filter((value): value is string => typeof value === "string") : [];
      const command = Array.isArray(entry.command) ? entry.command.filter((value): value is string => typeof value === "string").join(" ") : "";
      return <div className={`verification-evidence ${success ? "is-success" : "is-failed"}`} key={typeof entry.evidence_id === "string" ? entry.evidence_id : `${index}`}>
        <span className="verification-evidence-icon">{success ? <CheckCircle2 size={13} /> : <ShieldX size={13} />}</span>
        <div className="verification-evidence-body"><div><strong>{verificationKindLabel(entry.kind)}</strong><span>工作区 v{String(entry.workspace_version ?? "?")}</span></div>{command && <code>{command}</code>}<small>{criteria.length > 0 ? criteria.join("；") : String(entry.summary ?? "")}</small></div>
      </div>;
    })}</div>}
  </section>;
}

function conversationStats(events: AgentEvent[]) {
  return {
    duration: formatDuration(events),
    iterations: new Set(events.map((event) => event.iteration).filter((iteration): iteration is number => iteration !== null)).size,
    tools: events.filter((event) => event.type === "tool_started").length,
  };
}

function activeToolForEvents(events: AgentEvent[]) {
  return [...events].reverse().find((event) => {
    if (event.type === "approval_requested") return true;
    return event.type === "tool_started" && !events.some((candidate) => ["tool_finished", "tool_failed"].includes(candidate.type) && candidate.payload.tool_call_id === event.payload.tool_call_id);
  })?.payload.tool_name;
}

function changeIdsForEvents(events: AgentEvent[]) {
  return events
    .filter((event) => event.type === "tool_finished")
    .map((event) => {
      const metadata = objectValue(event.payload.metadata);
      const changeId = metadata?.change_id;
      return typeof changeId === "string" ? changeId : null;
    })
    .filter((value): value is string => value !== null);
}

const languageByExtension: Record<string, string> = {
  c: "C",
  cc: "C++",
  cpp: "C++",
  css: "CSS",
  csv: "CSV",
  go: "Go",
  h: "C header",
  hpp: "C++ header",
  html: "HTML",
  java: "Java",
  js: "JavaScript",
  json: "JSON",
  jsx: "JSX",
  md: "Markdown",
  py: "Python",
  rs: "Rust",
  sh: "Shell",
  sql: "SQL",
  toml: "TOML",
  ts: "TypeScript",
  tsx: "TSX",
  txt: "Text",
  xml: "XML",
  yaml: "YAML",
  yml: "YAML",
};

function previewLanguage(path: string) {
  const fileName = path.split("/").at(-1)?.toLowerCase() ?? "";
  if (fileName === "dockerfile") return "Dockerfile";
  if (fileName === "makefile") return "Makefile";
  const extension = fileName.split(".").at(-1) ?? "";
  return languageByExtension[extension] ?? "Plain text";
}

function highlightCodeLine(line: string, language: string): ReactNode[] {
  if (language === "Plain text" || language === "CSV" || language === "Markdown") return [line];
  const tokenPattern = /\/\/.*$|\/\*.*?\*\/|<!--.*?-->|^\s*#.*$|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\b[0-9]+(?:\.[0-9]+)?\b|[A-Za-z_$][\w$]*(?=\s*\()|\b(?:async|await|break|case|catch|class|const|continue|def|else|export|extends|false|finally|for|from|function|if|implements|import|in|interface|let|new|null|not|or|private|public|return|static|this|throw|true|try|type|undefined|var|void|while|with|yield)\b/g;
  const keywordPattern = /^(?:async|await|break|case|catch|class|const|continue|def|else|export|extends|false|finally|for|from|function|if|implements|import|in|interface|let|new|null|not|or|private|public|return|static|this|throw|true|try|type|undefined|var|void|while|with|yield)$/;
  const tokens: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenPattern.exec(line)) !== null) {
    if (match.index > cursor) tokens.push(line.slice(cursor, match.index));
    const token = match[0];
    const className = token.startsWith("//") || token.startsWith("/*") || token.startsWith("<!--") || /^\s*#/.test(token)
      ? "syntax-comment"
      : /^[\"'`]/.test(token)
        ? "syntax-string"
        : /^\d/.test(token)
          ? "syntax-number"
          : keywordPattern.test(token)
            ? "syntax-keyword"
            : "syntax-function";
    tokens.push(<span className={className} key={`${match.index}-${token}`}>{token}</span>);
    cursor = match.index + token.length;
  }
  if (cursor < line.length) tokens.push(line.slice(cursor));
  return tokens.length ? tokens : [line];
}

function renderHighlightedCodeLines(lines: string[], language: string, keyPrefix: string): ReactNode[] {
  const highlightLanguage = /^(text|txt|plaintext|md|markdown)?$/i.test(language) ? "Plain text" : language;
  return lines.map((line, index) => <span className="code-preview-line" key={`${keyPrefix}-${index}`}>{highlightCodeLine(line, highlightLanguage)}</span>);
}

function CodePreview({ preview }: { preview: FilePreview }): ReactElement {
  const language = previewLanguage(preview.path);
  const lines = preview.content.split(/\r?\n/);
  if (language === "Markdown") {
    return <div className="file-preview-content file-preview-markdown">
      <div className="preview-toolbar"><span className="preview-language">{language}</span><span>{lines.length} 行{preview.truncated ? " · 已截断" : ""}</span></div>
      <div className="markdown-preview-frame"><MarkdownAnswer content={preview.content} /></div>
    </div>;
  }
  return <div className="file-preview-content">
    <div className="preview-toolbar"><span className="preview-language">{language}</span><span>{lines.length} 行{preview.truncated ? " · 已截断" : ""}</span></div>
    <div className="markdown-preview-frame file-preview-code-frame"><pre className="markdown-code-block"><code>{renderHighlightedCodeLines(lines, language, "file-preview")}</code></pre></div>
  </div>;
}

function isSafeMarkdownUrl(url: string) {
  return /^(https?:|mailto:)/i.test(url.trim());
}

function renderMarkdownInline(text: string, keyPrefix: string): ReactNode[] {
  const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^)]*\))/g;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  let tokenIndex = 0;
  while ((match = tokenPattern.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const token = match[0];
    const key = `${keyPrefix}-${tokenIndex++}`;
    if (token.startsWith("`") && token.endsWith("`")) {
      nodes.push(<code className="markdown-inline-code" key={key}>{token.slice(1, -1)}</code>);
    } else if ((token.startsWith("**") && token.endsWith("**")) || (token.startsWith("__") && token.endsWith("__"))) {
      nodes.push(<strong key={key}>{renderMarkdownInline(token.slice(2, -2), key)}</strong>);
    } else if ((token.startsWith("*") && token.endsWith("*")) || (token.startsWith("_") && token.endsWith("_"))) {
      nodes.push(<em key={key}>{renderMarkdownInline(token.slice(1, -1), key)}</em>);
    } else {
      const linkMatch = /^\[([^\]]+)\]\(([^)]*)\)$/.exec(token);
      if (linkMatch && isSafeMarkdownUrl(linkMatch[2])) {
        nodes.push(<a href={linkMatch[2].trim()} target="_blank" rel="noreferrer" key={key}>{renderMarkdownInline(linkMatch[1], key)}</a>);
      } else {
        nodes.push(token);
      }
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

type DiffFile = { path: string; lines: string[]; added: number; removed: number };

function diffFilePath(header: string) {
  const value = header.replace(/^\+\+\+\s+/, "").split("\t", 1)[0].trim();
  return value.startsWith("b/") ? value.slice(2) : value;
}

function parseDiffFiles(diff: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const line of diff.replace(/\r\n/g, "\n").split("\n")) {
    if (line.startsWith("diff --git ")) {
      if (current) files.push(current);
      current = { path: "", lines: [line], added: 0, removed: 0 };
      continue;
    }
    if (line.startsWith("--- ") && !current) {
      current = { path: "", lines: [], added: 0, removed: 0 };
    }
    if (!current) continue;
    current.lines.push(line);
    if (line.startsWith("+++ ")) {
      current.path = diffFilePath(line);
    } else if (line.startsWith("+") && !line.startsWith("+++")) {
      current.added += 1;
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      current.removed += 1;
    }
  }
  if (current) files.push(current);
  return files.filter((file) => file.path && file.lines.some((line) => line.startsWith("@@ ")));
}

function UnifiedDiff({ diff }: { diff: string }): ReactElement {
  const files = parseDiffFiles(diff);
  if (!files.length) {
    return <pre className="markdown-code-block"><code>{diff}</code></pre>;
  }
  return <div className="diff-files">{files.map((file) => <details className="diff-file" key={file.path}>
    <summary><span className="diff-file-summary-main"><ChevronDown className="diff-file-chevron" size={14} /><code>{file.path}</code></span><span className="diff-file-stats"><span className="diff-added">+{file.added}</span><span className="diff-removed">-{file.removed}</span></span></summary>
    <pre className="diff-file-body"><code>{file.lines.map((line, lineIndex) => <span className={"diff-line " + (line.startsWith("@@ ") ? "diff-hunk" : line.startsWith("+") && !line.startsWith("+++") ? "diff-added-line" : line.startsWith("-") && !line.startsWith("---") ? "diff-removed-line" : "")} key={file.path + "-" + lineIndex}>{line}{lineIndex < file.lines.length - 1 ? "\n" : ""}</span>)}</code></pre>
  </details>)}</div>;
}

function MarkdownAnswer({ content }: { content: string }): ReactElement {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  let blockIndex = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = /^\s*```\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      const language = fence[1];
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(language.toLowerCase() === "diff" || language.toLowerCase() === "patch"
        ? <UnifiedDiff diff={codeLines.join("\n")} key={"diff-" + blockIndex++} />
        : <pre className="markdown-code-block" key={"code-" + blockIndex++}><code className={language ? "language-" + language : undefined}>{renderHighlightedCodeLines(codeLines, language, `markdown-code-${blockIndex}`)}</code></pre>);
      continue;
    }

    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      const HeadingTag = `h${level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
      blocks.push(<HeadingTag className="markdown-heading" key={`heading-${blockIndex++}`}>{renderMarkdownInline(heading[2], `heading-${blockIndex}`)}</HeadingTag>);
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoteLines: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(<blockquote className="markdown-quote" key={`quote-${blockIndex++}`}>{renderMarkdownInline(quoteLines.join("\n"), `quote-${blockIndex}`)}</blockquote>);
      continue;
    }

    const unordered = /^\s*[-+*]\s+(.+)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (unordered || ordered) {
      const items: string[] = [];
      const orderedList = Boolean(ordered);
      while (index < lines.length) {
        const item = (orderedList ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-+*]\s+(.+)$/).exec(lines[index]);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      const ListTag = orderedList ? "ol" : "ul";
      blocks.push(<ListTag className="markdown-list" key={`list-${blockIndex++}`}>{items.map((item, itemIndex) => <li key={`${blockIndex}-${itemIndex}`}>{renderMarkdownInline(item, `item-${blockIndex}-${itemIndex}`)}</li>)}</ListTag>);
      continue;
    }

    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      blocks.push(<hr className="markdown-rule" key={`rule-${blockIndex++}`} />);
      index += 1;
      continue;
    }

    const paragraphLines = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^\s*```/.test(lines[index]) && !/^(#{1,6})\s+/.test(lines[index]) && !/^\s*>\s?/.test(lines[index]) && !/^\s*[-+*]\s+/.test(lines[index]) && !/^\s*\d+[.)]\s+/.test(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    blocks.push(<p className="markdown-paragraph" key={`paragraph-${blockIndex++}`}>{renderMarkdownInline(paragraphLines.join("\n"), `paragraph-${blockIndex}`)}</p>);
  }
  return <div className="markdown-answer">{blocks}</div>;
}

function ApprovalCard({
  approval,
  busy,
  onDecide,
}: {
  approval: AgentEvent;
  busy: boolean;
  onDecide: (approved: boolean, approveCurrentTurn?: boolean) => void;
}): ReactElement {
  const details = objectValue(approval.payload.details);
  const command = Array.isArray(details?.command) ? details.command.join(" ") : undefined;
  const patch = typeof details?.patch === "string" ? details.patch : undefined;
  const contentPreview = typeof details?.content_preview === "string" ? details.content_preview : undefined;
  const oldText = typeof details?.old_text === "string" ? details.old_text : undefined;
  const newText = typeof details?.new_text === "string" ? details.new_text : undefined;
  const preview = patch ?? contentPreview ?? (oldText !== undefined || newText !== undefined
    ? `- ${oldText ?? ""}\n+ ${newText ?? ""}`
    : undefined);
  return <section className="approval-card" aria-label="需要审批">
    <div className="approval-card-heading"><span className="approval-card-icon"><ShieldCheck size={17} /></span><div><strong>需要审批</strong><p>{readPayloadString(approval, "summary") ?? "Agent 希望执行一项本地操作。"}</p></div></div>
    {command && <div className="approval-field"><span>命令</span><code>{command}</code></div>}
    {typeof details?.cwd === "string" && <div className="approval-field"><span>工作目录</span><code>{details.cwd}</code></div>}
    {typeof details?.path === "string" && <div className="approval-field"><span>文件</span><code>{details.path}</code></div>}
    {preview && <pre className="approval-preview">{preview}</pre>}
    <div className="approval-actions"><button className="approval-button approval-button-reject" onClick={() => onDecide(false)} disabled={busy}><ShieldX size={15} />拒绝</button><button className="approval-button approval-button-approve-once" onClick={() => onDecide(true)} disabled={busy}><ShieldCheck size={15} />仅批准一次</button><button className="approval-button approval-button-approve-turn" onClick={() => onDecide(true, true)} disabled={busy} title="批准当前操作，并自动批准本条消息后续的本地操作"><ListChecks size={15} />自动批准本条消息</button></div>
  </section>;
}

export default function AgentConsole() {
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceEntry[]>([]);
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [filePreview, setFilePreview] = useState<FilePreview | null>(null);
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewMessage, setPreviewMessage] = useState("");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [task, setTask] = useState("");
  const [autoApprovalEnabled, setAutoApprovalEnabled] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [run, setRun] = useState<RunResponse | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [backendStatus, setBackendStatus] = useState<BackendStatus>("checking");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workspaceExpanded, setWorkspaceExpanded] = useState(true);
  const [historyExpanded, setHistoryExpanded] = useState(true);
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(new Set());
  const [thinkingExpanded, setThinkingExpanded] = useState(true);
  const [expandedActivityTurns, setExpandedActivityTurns] = useState<Set<string>>(new Set());
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set());
  const [workspacePhase, setWorkspacePhase] = useState<WorkspacePhase>("idle");
  const [projectSwitching, setProjectSwitching] = useState(false);
  const [displayedAnswer, setDisplayedAnswer] = useState("");
  const [answerStreaming, setAnswerStreaming] = useState(false);
  const [instantAnswerTurnKey, setInstantAnswerTurnKey] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<AgentEvent | null>(null);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [revertedChanges, setRevertedChanges] = useState<Set<string>>(new Set());
  const [unavailableChanges, setUnavailableChanges] = useState<Set<string>>(new Set());
  const [editingHistoryId, setEditingHistoryId] = useState<string | null>(null);
  const [historyTitleDraft, setHistoryTitleDraft] = useState("");
  const [pendingDelete, setPendingDelete] = useState<{ type: "session" | "project"; id: string; label: string } | null>(null);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [leftWidth, setLeftWidth] = useState(270);
  const [rightWidth, setRightWidth] = useState(360);
  const [workspacePanelRatio, setWorkspacePanelRatio] = useState(0.52);
  const eventSource = useRef<EventSource | null>(null);
  const activeStreamRunId = useRef<string | null>(null);
  const activeSessionId = useRef<string | null>(null);
  const workspaceFilesCache = useRef<Map<string, WorkspaceEntry[]>>(new Map());
  const workspaceRoots = useRef<Map<string, string>>(new Map());
  const conversationEventsCache = useRef<Map<string, AgentEvent[]>>(new Map());
  const projectsCollapsedInitially = useRef(false);
  const answerTimer = useRef<number | null>(null);
  const workspaceRequestsInFlight = useRef<Set<string>>(new Set());
  const historyRequestInFlight = useRef(false);
  const selectedFilePathRef = useRef<string | null>(null);
  const previewRequestIdRef = useRef(0);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const taskInputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => () => {
    activeStreamRunId.current = null;
    eventSource.current?.close();
  }, []);

  useEffect(() => {
    activeSessionId.current = session?.session_id ?? null;
  }, [session]);

  const conversationTurns = useMemo(() => buildConversationTurns(events), [events]);
  const projectGroups = useMemo(() => {
    const groups = new Map<string, { key: string; root: string; name: string; items: HistoryItem[] }>();
    history.forEach((item) => {
      const key = projectKey(item.workspace_root) || "unknown-project";
      const group = groups.get(key) ?? {
        key,
        root: item.workspace_root ?? "",
        name: projectName(item.workspace_root),
        items: [],
      };
      group.items.push(item);
      groups.set(key, group);
    });
    const duplicateNames = new Set<string>();
    const nameCounts = new Map<string, number>();
    groups.forEach((group) => nameCounts.set(group.name, (nameCounts.get(group.name) ?? 0) + 1));
    nameCounts.forEach((count, name) => { if (count > 1) duplicateNames.add(name); });
    groups.forEach((group) => { group.name = projectNameWithDisambiguation(group.root, duplicateNames); });
    return [...groups.values()].sort((left, right) => {
      const leftActive = projectKey(session?.workspace_root) === left.key;
      const rightActive = projectKey(session?.workspace_root) === right.key;
      return Number(rightActive) - Number(leftActive);
    });
  }, [history, session?.workspace_root]);

  useEffect(() => {
    if (projectsCollapsedInitially.current || projectGroups.length === 0) return;
    projectsCollapsedInitially.current = true;
    setCollapsedProjects(new Set(projectGroups.map((group) => group.key)));
  }, [projectGroups]);
  const latestTurn = conversationTurns.at(-1);
  const currentTurnEvents = latestTurn?.events ?? [];
  const latestFinalAnswer = useMemo(() => finalAnswerForEvents(currentTurnEvents), [currentTurnEvents]);
  const runStatus = useMemo(() => {
    if (!session) return "workspace-required";
    if (run?.status) return run.status;
    const finished = [...events].reverse().find((event) => event.type === "agent_finished");
    return readPayloadString(finished ?? events[0] ?? { payload: {} } as AgentEvent, "status") ?? (busy ? "running" : "ready");
  }, [busy, events, run, session]);

  useEffect(() => {
    void loadHistory(true);
  }, []);

  useEffect(() => {
    if (backendStatus === "offline") return;
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void loadHistory();
    };
    const timer = window.setInterval(refreshWhenVisible, 15000);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [backendStatus]);

  useEffect(() => {
    if (answerTimer.current !== null) window.clearInterval(answerTimer.current);
    const answer = latestFinalAnswer ?? "";
    if (!answer) {
      setDisplayedAnswer("");
      setAnswerStreaming(false);
      return;
    }
    if (instantAnswerTurnKey === latestTurn?.key) {
      setDisplayedAnswer(answer);
      setAnswerStreaming(false);
      return;
    }
    setDisplayedAnswer("");
    setAnswerStreaming(true);
    let cursor = 0;
    const timer = window.setInterval(() => {
      cursor = Math.min(answer.length, cursor + Math.max(1, Math.ceil(answer.length / 90)));
      setDisplayedAnswer(answer.slice(0, cursor));
      if (cursor >= answer.length) {
        window.clearInterval(timer);
        answerTimer.current = null;
        setAnswerStreaming(false);
      }
    }, 16);
    answerTimer.current = timer;
    return () => window.clearInterval(timer);
  }, [instantAnswerTurnKey, latestFinalAnswer, latestTurn?.key]);

  useEffect(() => {
    if (!events.length && !displayedAnswer) return;
    const container = conversationRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [events.length, displayedAnswer]);

  async function responseError(response: Response, fallback: string) {
    try {
      const body = (await response.json()) as { detail?: string };
      return body.detail ?? fallback;
    } catch {
      return fallback;
    }
  }

  async function loadHistory(showLoading = false) {
    if (historyRequestInFlight.current) return;
    historyRequestInFlight.current = true;
    if (showLoading) {
      setHistoryLoading(true);
      setHistoryError(null);
      setBackendStatus("checking");
    }
    try {
      const response = await fetch(`${API_BASE}/history?limit=50`);
      setBackendStatus("online");
      if (!response.ok) throw new Error(await responseError(response, "无法加载运行历史"));
      const loaded = (await response.json()) as HistoryItem[];
      setHistoryError(null);
      setHistory((current) => {
        const loadedSessionIds = new Set(loaded.map((item) => item.session_id));
        const stillRunning = current.filter(
          (item) => item.status === "running" && !loadedSessionIds.has(item.session_id),
        );
        return [...loaded, ...stillRunning].slice(0, 50);
      });
    } catch (reason) {
      const message = requestErrorMessage(reason, "无法加载运行历史");
      if (isNetworkRequestError(reason)) setBackendStatus("offline");
      setHistoryError(message);
    } finally {
      historyRequestInFlight.current = false;
      if (showLoading) setHistoryLoading(false);
    }
  }

  async function deleteHistorySession(item: HistoryItem) {
    try {
      const response = await fetch(`${API_BASE}/history/sessions/${encodeURIComponent(item.session_id)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response, "无法删除会话"));
      setHistory((current) => current.filter((candidate) => candidate.session_id !== item.session_id));
      setPendingDelete(null);
      if (item.session_id === session?.session_id) {
        eventSource.current?.close();
        activeStreamRunId.current = null;
        activeSessionId.current = null;
        setSession(null);
        setRun(null);
        setEvents([]);
        setWorkspaceFiles([]);
        setWorkspaceRoot("");
        setSelectedFilePath(null);
        selectedFilePathRef.current = null;
        setFilePreview(null);
        setPreviewState("idle");
      }
    } catch (reason) {
      setError(requestErrorMessage(reason, "无法删除会话"));
    }
  }

  async function renameHistorySession(item: HistoryItem) {
    const title = historyTitleDraft.trim();
    if (!title) return;
    try {
      const response = await fetch(`${API_BASE}/history/sessions/${encodeURIComponent(item.session_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) });
      if (!response.ok) throw new Error(await responseError(response, "无法重命名会话"));
      const updated = (await response.json()) as HistoryItem;
      setHistory((current) => current.map((candidate) => candidate.session_id === item.session_id ? { ...candidate, title: updated.title } : candidate));
      setEditingHistoryId(null);
    } catch (reason) {
      setError(requestErrorMessage(reason, "无法重命名会话"));
    }
  }

  async function deleteProjectHistory(root: string) {
    const items = history.filter((item) => projectKey(item.workspace_root) === projectKey(root));
    for (const item of items) {
      const response = await fetch(`${API_BASE}/history/sessions/${encodeURIComponent(item.session_id)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response, "无法删除项目历史"));
    }
    setHistory((current) => current.filter((item) => projectKey(item.workspace_root) !== projectKey(root)));
    setPendingDelete(null);
    if (projectKey(session?.workspace_root) === projectKey(root)) {
      eventSource.current?.close(); activeStreamRunId.current = null; activeSessionId.current = null;
      setSession(null); setRun(null); setEvents([]); setWorkspaceFiles([]); setWorkspaceRoot("");
      setSelectedFilePath(null); selectedFilePathRef.current = null; setFilePreview(null); setPreviewState("idle");
    }
  }

  function subscribeToRun(started: RunResponse, initialEvents: AgentEvent[] = [], workspaceSessionId?: string) {
    eventSource.current?.close();
    activeStreamRunId.current = started.run_id;
    const knownEventIds = new Set(initialEvents.map((event) => event.event_id));
    const optimisticUserEvent = initialEvents.find(
      (event) => event.event_id === `optimistic-user-${started.run_id}`,
    );
    const isCurrentWorkspace = started.session_id === (workspaceSessionId ?? session?.session_id);

    if (started.status !== "running") {
      setBusy(false);
      return;
    }

    setBusy(true);
    const source = new EventSource(`${API_BASE}/runs/${started.run_id}/events`);
    eventSource.current = source;
    const handleEvent = (message: Event) => {
      if (activeStreamRunId.current !== started.run_id) return;
      const event = JSON.parse((message as MessageEvent<string>).data) as AgentEvent;
      if (knownEventIds.has(event.event_id)) return;
      knownEventIds.add(event.event_id);
      const replacesOptimisticUser = Boolean(
        optimisticUserEvent
        && event.type === "user_message"
        && event.payload.content === optimisticUserEvent.payload.content,
      );
      setEvents((current) => {
        const next = replacesOptimisticUser
          ? current.map((item) => item.event_id === optimisticUserEvent?.event_id ? event : item)
          : current.some((item) => item.event_id === event.event_id) ? current : [...current, event];
        conversationEventsCache.current.set(started.session_id, next);
        return next;
      });
      if (event.type === "approval_requested") {
        setPendingApproval(event);
        setApprovalBusy(false);
        setThinkingExpanded(true);
      }
      if (event.type === "approval_resolved") {
        setPendingApproval(null);
        setApprovalBusy(false);
      }
      if (event.type === "user_message") {
        void loadHistory();
      }
      if (isCurrentWorkspace && ["tool_finished", "tool_failed", "agent_finished"].includes(event.type)) {
        void loadWorkspaceFiles(started.session_id).catch((reason) => {
          setError(requestErrorMessage(reason, "无法刷新工作区"));
        });
      }
      if (isCurrentWorkspace && event.type === "tool_finished" && selectedFilePathRef.current) {
        void selectFile(selectedFilePathRef.current, started.session_id).catch((reason) => {
          setError(requestErrorMessage(reason, "无法刷新文件预览"));
        });
      }
      if (event.type === "agent_finished" || event.type === "agent_error") {
        setBusy(false);
        setPendingApproval(null);
        setApprovalBusy(false);
        setThinkingExpanded(false);
        void loadHistory();
        source.close();
        if (activeStreamRunId.current === started.run_id) activeStreamRunId.current = null;
      }
    };
    eventTypes.forEach((eventType) => source.addEventListener(eventType, handleEvent));
    source.onerror = () => {
      if (activeStreamRunId.current !== started.run_id) return;
      source.close();
      setBusy(false);
      setPendingApproval(null);
      setApprovalBusy(false);
      void loadHistory();
      setError("事件流意外关闭。");
    };
  }

  async function replayHistory(_runId: string, sessionId?: string, targetRoot?: string | null) {
    if (!sessionId) return;
    setError(null);
    eventSource.current?.close();
    activeStreamRunId.current = null;
    setBusy(false);
    const switchingProject = projectKey(session?.workspace_root) !== projectKey(targetRoot);
    setProjectSwitching(switchingProject);
    try {
      if (switchingProject) setWorkspacePhase("loading");
      const activationResponse = await fetch(`${API_BASE}/history/sessions/${encodeURIComponent(sessionId)}/activate`, { method: "POST" });
      if (!activationResponse.ok) throw new Error(await responseError(activationResponse, "无法激活此会话"));
      const activated = (await activationResponse.json()) as SessionResponse;
      const cachedEvents = conversationEventsCache.current.get(sessionId) ?? [];
      const liveRunId = activated.run_id ?? _runId;
      const liveStatus = activated.status === "running" ? "running" : "completed";
      const activatedRun: RunResponse = { run_id: liveRunId, session_id: activated.session_id, status: liveStatus };
      activeSessionId.current = activated.session_id;
      workspaceRoots.current.set(activated.session_id, activated.workspace_root);
      setSession(activated);
      setWorkspaceRoot(activated.workspace_root);
      setEvents(cachedEvents);
      setRun(activatedRun);
      setPendingApproval(null);
      setApprovalBusy(false);
      setThinkingExpanded(false);
      setRevertedChanges(new Set());
      setUnavailableChanges(new Set());
      const loadedTurns = buildConversationTurns(cachedEvents);
      setInstantAnswerTurnKey(liveStatus === "running" ? null : loadedTurns.at(-1)?.key ?? null);
      const cachedFiles = workspaceFilesCache.current.get(projectKey(activated.workspace_root));
      if (switchingProject) setWorkspaceFiles(cachedFiles ?? []);
      if (switchingProject) {
        setSelectedFilePath(null);
        selectedFilePathRef.current = null;
        previewRequestIdRef.current += 1;
        setFilePreview(null);
        setPreviewState("idle");
        setPreviewMessage("");
        setWorkspacePhase(cachedFiles ? "idle" : "loading");
        void loadWorkspaceFiles(activated.session_id).catch((reason) => {
          setError(requestErrorMessage(reason, "无法加载工作区文件"));
        }).finally(() => setProjectSwitching(false));
      } else {
        setProjectSwitching(false);
        setWorkspacePhase("idle");
      }
      if (liveStatus === "running") subscribeToRun(activatedRun, cachedEvents, activated.session_id);

      void (async () => {
        const response = await fetch(`${API_BASE}/history/sessions/${encodeURIComponent(sessionId)}`);
        if (!response.ok) throw new Error(await responseError(response, "无法加载此会话"));
        const record = (await response.json()) as HistoryRecord;
        conversationEventsCache.current.set(sessionId, mergeAgentEvents(cachedEvents, record.events));
        setEvents((current) => mergeAgentEvents(current, record.events));
        if (liveStatus !== "running") {
          setRun({ run_id: record.summary.run_id, session_id: record.summary.session_id, status: record.summary.status });
          setInstantAnswerTurnKey(buildConversationTurns(record.events).at(-1)?.key ?? null);
        }
      })().catch((reason) => {
        setError(requestErrorMessage(reason, "无法加载此会话"));
      });
    } catch (reason) {
      setWorkspacePhase("idle");
      setError(requestErrorMessage(reason, "无法加载此次运行"));
      setProjectSwitching(false);
    }
  }

  async function newConversation(projectRoot?: string) {
    const root = projectRoot || session?.workspace_root || workspaceRoot;
    const switchingProject = projectKey(session?.workspace_root) !== projectKey(root);
    eventSource.current?.close();
    activeStreamRunId.current = null;
    setBusy(false);
    setProjectSwitching(switchingProject);
    if (switchingProject) {
      setWorkspacePhase("loading");
      setWorkspaceFiles([]);
      setSelectedFilePath(null);
      selectedFilePathRef.current = null;
      previewRequestIdRef.current += 1;
      setFilePreview(null);
      setPreviewState("idle");
      setPreviewMessage("");
    }
    setEvents([]);
    setRun(null);
    setTask("");
    setInstantAnswerTurnKey(null);
    setPendingApproval(null);
    setApprovalBusy(false);
    setRevertedChanges(new Set());
    setUnavailableChanges(new Set());
    if (root) {
      await createSession(root, switchingProject);
      setProjectSwitching(false);
    } else {
      setSession(null);
      setWorkspaceFiles([]);
      setSelectedFilePath(null);
      selectedFilePathRef.current = null;
      setFilePreview(null);
      setPreviewState("idle");
      setProjectSwitching(false);
    }
  }

  async function createSession(root: string, refreshProject = true) {
    if (!root) return;
    setError(null);
    setEvents([]);
    setRun(null);
    setInstantAnswerTurnKey(null);
    setAutoApprovalEnabled(false);
    setExpandedActivityTurns(new Set());
    setPendingApproval(null);
    setApprovalBusy(false);
    setRevertedChanges(new Set());
    setUnavailableChanges(new Set());
    try {
      const response = await fetch(`${API_BASE}/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_root: root }) });
      if (!response.ok) throw new Error(await responseError(response, "无法创建会话"));
      const created = (await response.json()) as SessionResponse;
      activeSessionId.current = created.session_id;
      workspaceRoots.current.set(created.session_id, created.workspace_root);
      setSession(created);
      if (!refreshProject) {
        setWorkspacePhase("idle");
        return;
      }
      const cachedFiles = workspaceFilesCache.current.get(projectKey(created.workspace_root));
      setWorkspaceFiles(cachedFiles ?? []);
      setWorkspacePhase(cachedFiles ? "idle" : "loading");
      void loadWorkspaceFiles(created.session_id).catch((reason) => {
        setError(requestErrorMessage(reason, "无法加载工作区文件"));
      });
    } catch (reason) {
      setWorkspacePhase("idle");
      setError(requestErrorMessage(reason, "无法创建会话"));
    }
  }

  async function runTask() {
    const submittedTask = task.trim();
    if (!session || workspacePhase !== "idle" || !submittedTask) return;
    setError(null);
    if (answerTimer.current !== null) {
      window.clearInterval(answerTimer.current);
      answerTimer.current = null;
    }
    setAnswerStreaming(false);
    setInstantAnswerTurnKey(latestTurn?.key ?? null);
    // Start each run collapsed; approval requests still expand the panel.
    setBusy(true);
    eventSource.current?.close();
    activeStreamRunId.current = null;
    try {
      const response = await fetch(`${API_BASE}/sessions/${session.session_id}/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: submittedTask, auto_approve: autoApprovalEnabled }) });
      if (!response.ok) throw new Error(await responseError(response, "无法启动运行"));
      const started = (await response.json()) as RunResponse;
      setTask("");
      setRun(started);
      // Apply the new turn and its collapsed default in the same React update
      // window. This prevents the previous turn from briefly collapsing before
      // the optimistic user message becomes the latest turn.
      setThinkingExpanded(false);
      const optimisticUserEvent: AgentEvent = {
        event_id: `optimistic-user-${started.run_id}`,
        type: "user_message",
        session_id: started.session_id,
        timestamp: new Date().toISOString(),
        iteration: null,
        payload: {
          content: submittedTask,
          turn_index: events.filter((event) => event.type === "user_message").length + 1,
        },
      };
      setEvents((current) => {
        const next = [...current, optimisticUserEvent];
        conversationEventsCache.current.set(started.session_id, next);
        return next;
      });
      setHistory((current) => {
        const existing = current.find((item) => item.session_id === started.session_id);
        const optimistic: HistoryItem = {
          run_id: started.run_id,
          session_id: started.session_id,
          workspace_root: session.workspace_root,
          task: existing?.task ?? submittedTask,
          title: existing?.title ?? "新会话",
          status: "running",
          started_at: existing?.started_at ?? new Date().toISOString(),
          turn_count: (existing?.turn_count ?? 0) + 1,
        };
        return [optimistic, ...current.filter((item) => item.session_id !== started.session_id)].slice(0, 50);
      });
      void loadHistory();
      subscribeToRun(started, [optimisticUserEvent]);
    } catch (reason) {
      setBusy(false);
      setError(requestErrorMessage(reason, "无法启动运行"));
    }
  }

  function chooseStarterPrompt(prompt: string) {
    setTask(prompt);
    requestAnimationFrame(() => taskInputRef.current?.focus());
  }

  async function cancelRun() {
    if (!run) return;
    try {
      await fetch(`${API_BASE}/runs/${run.run_id}/cancel`, { method: "POST" });
    } catch (reason) {
      setError(requestErrorMessage(reason, "无法取消运行"));
    }
  }

  async function decideApproval(approved: boolean, approveCurrentTurn = false) {
    if (!pendingApproval) return;
    const approvalSessionId = run?.session_id ?? session?.session_id;
    if (!approvalSessionId) return;
    const approvalId = pendingApproval.payload.approval_id;
    if (typeof approvalId !== "string") return;
    setApprovalBusy(true);
    try {
      const response = await fetch(
        `${API_BASE}/sessions/${approvalSessionId}/approvals/${approvalId}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved, approve_current_turn: approveCurrentTurn }),
        },
      );
       if (!response.ok) throw new Error(await responseError(response, "无法处理审批"));
    } catch (reason) {
      setApprovalBusy(false);
      setError(requestErrorMessage(reason, "无法处理审批"));
    }
  }

  async function revertChange(changeId: string) {
    if (!session) return;
    try {
      const response = await fetch(
        `${API_BASE}/sessions/${session.session_id}/changes/${changeId}/revert`,
        { method: "POST" },
      );
       if (!response.ok) {
        if (response.status === 409 || response.status === 404) {
          // The file changed after the Agent edit (or the record is no longer
          // available). Hide this action instead of leaving a misleading undo
          // button that can never succeed.
          setUnavailableChanges((current) => new Set(current).add(changeId));
          return;
        }
        throw new Error(await responseError(response, "无法撤销修改"));
      }
      setRevertedChanges((current) => new Set(current).add(changeId));
      await loadWorkspaceFiles(session.session_id);
      if (selectedFilePathRef.current) {
        await selectFile(selectedFilePathRef.current, session.session_id);
      }
    } catch (reason) {
      setError(requestErrorMessage(reason, "无法撤销修改"));
    }
  }

  async function chooseDirectory() {
    setError(null);
    setWorkspacePhase("selecting");

    try {
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      const response = await fetch(`${API_BASE}/workspaces/select`);
       if (!response.ok) throw new Error(await responseError(response, "无法选择工作区"));
      const selection = (await response.json()) as { workspace_root: string };
      setWorkspaceFiles([]);
      setSelectedFilePath(null);
      selectedFilePathRef.current = null;
      previewRequestIdRef.current += 1;
      setFilePreview(null);
      setPreviewState("idle");
      setPreviewMessage("");
      setWorkspacePhase("loading");
      setWorkspaceRoot(selection.workspace_root);
      await createSession(selection.workspace_root);
    } catch (reason) {
      setError(requestErrorMessage(reason, "无法选择工作区"));
      setWorkspacePhase("idle");
    }
  }

  async function loadWorkspaceFiles(sessionId: string) {
    if (workspaceRequestsInFlight.current.has(sessionId)) return;
    workspaceRequestsInFlight.current.add(sessionId);
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/files`);
      if (!response.ok) throw new Error(await responseError(response, "无法列出工作区文件"));
      const files = (await response.json()) as WorkspaceEntry[];
      const cacheKey = projectKey(workspaceRoots.current.get(sessionId)) || sessionId;
      workspaceFilesCache.current.set(cacheKey, files);
      if (activeSessionId.current !== sessionId) return;
      setWorkspaceFiles(files);
      const firstFile = files.find((entry) => entry.kind === "file");
      if (firstFile && !selectedFilePathRef.current) {
        window.setTimeout(() => {
          void selectFile(firstFile.path, sessionId).catch((reason) => {
            setError(requestErrorMessage(reason, "无法预览文件"));
          });
        }, 0);
      }
    } finally {
      workspaceRequestsInFlight.current.delete(sessionId);
      if (activeSessionId.current === sessionId) setWorkspacePhase("idle");
    }
  }

  async function selectFile(path: string, sessionId = session?.session_id) {
    if (!sessionId) return;
    const requestId = ++previewRequestIdRef.current;
    setSelectedFilePath(path);
    selectedFilePathRef.current = path;
    setFilePreview(null);
    setPreviewState("loading");
    setPreviewMessage("");
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/files/${path.split("/").map(encodeURIComponent).join("/")}`);
      if (!response.ok) {
        const message = await responseError(response, "无法预览文件");
        if (requestId !== previewRequestIdRef.current) return;
        const unsupported = response.status === 415 || /binary|UTF-8|text file|not supported/i.test(message);
        setPreviewState(unsupported ? "unsupported" : "error");
        setPreviewMessage(unsupported ? "此文件不是受支持的 UTF-8 文本文件。" : message);
        return;
      }
      const preview = (await response.json()) as FilePreview;
      if (requestId !== previewRequestIdRef.current) return;
      setFilePreview(preview);
      setPreviewState("ready");
    } catch (reason) {
      if (requestId !== previewRequestIdRef.current) return;
      setPreviewState("error");
      setPreviewMessage(requestErrorMessage(reason, "无法预览文件"));
    }
  }

  function toggleFolder(path: string) {
    setExpandedFolders((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  }

  function renderTreeEntry(entry: WorkspaceEntry, depth = 0): ReactElement {
    const children = workspaceFiles.filter((candidate) => {
      const rest = candidate.path.startsWith(`${entry.path}/`) ? candidate.path.slice(entry.path.length + 1) : "";
      return rest.length > 0 && !rest.includes("/");
    }).sort((left, right) => Number(right.kind === "directory") - Number(left.kind === "directory") || left.path.localeCompare(right.path));
    const expanded = expandedFolders.has(entry.path);
    return <div className="tree-node" key={entry.path}>
      {entry.kind === "directory" ? <button className="tree-folder" style={{ paddingLeft: `${8 + depth * 12}px` }} onClick={() => toggleFolder(entry.path)}><ChevronRight className={expanded ? "folder-chevron is-open" : "folder-chevron"} size={13} /><FolderOpen size={14} /><span>{entry.path.split("/").at(-1)}</span></button> : <button className={`tree-file ${selectedFilePath === entry.path ? "is-selected" : ""}`} style={{ paddingLeft: `${21 + depth * 12}px` }} onClick={() => void selectFile(entry.path)}><FileCode2 size={14} /><span>{entry.path.split("/").at(-1)}</span></button>}
      {entry.kind === "directory" && expanded && <div className="tree-children">{children.map((child) => renderTreeEntry(child, depth + 1))}</div>}
    </div>;
  }

  function resizePane(side: "left" | "right", event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = side === "left" ? leftWidth : rightWidth;
    const onMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX;
      if (side === "left") setLeftWidth(Math.min(420, Math.max(220, startWidth + delta)));
      else setRightWidth(Math.min(560, Math.max(280, startWidth - delta)));
    };
    const stop = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop, { once: true });
  }

  function resizeRailSections(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const rail = event.currentTarget.parentElement;
    if (!rail) return;
    const bounds = rail.getBoundingClientRect();
    const update = (clientY: number) => {
      const ratio = (clientY - bounds.top) / bounds.height;
      setWorkspacePanelRatio(Math.min(0.78, Math.max(0.24, ratio)));
    };
    update(event.clientY);
    const onMove = (moveEvent: PointerEvent) => update(moveEvent.clientY);
    const stop = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop, { once: true });
  }

  useEffect(() => {
    if (!session || !busy) return;
    const timer = window.setInterval(() => {
      void loadWorkspaceFiles(session.session_id).catch((reason) => setError(requestErrorMessage(reason, "无法刷新工作区")));
    }, 700);
    return () => window.clearInterval(timer);
  }, [busy, session]);

  const layoutStyle = {
    "--left-pane-width": leftCollapsed ? "38px" : `${leftWidth}px`,
    "--right-pane-width": rightCollapsed ? "38px" : `${rightWidth}px`,
    "--workspace-panel-height": `${workspacePanelRatio * 100}%`,
  } as CSSProperties;

  return <div className="console-shell">
    <header className="console-topbar">
      <div className="brand-lockup"><div className="brand-mark"><Bot size={20} /></div><div><p className="brand-name">Local Coding Agent</p><p className="brand-caption">本地项目编程助手</p></div></div>
      <div className={`connection-state ${backendStatus === "offline" ? "is-offline" : session ? "is-ready" : ""}`}><span className="state-dot" />{busy ? "执行中" : backendStatus === "offline" ? "后端未连接" : backendStatus === "checking" ? "正在连接后端" : session ? "会话就绪" : "需要选择项目"}</div>
    </header>

<main className="console-layout" style={layoutStyle}>{(editingHistoryId || pendingDelete) && <div className="inline-dialog" role="dialog">{editingHistoryId && <form onSubmit={(event) => { event.preventDefault(); const item = history.find((candidate) => candidate.session_id === editingHistoryId); if (item) void renameHistorySession(item); }}><label>重命名会话</label><input autoFocus value={historyTitleDraft} onChange={(event) => setHistoryTitleDraft(event.target.value)} /><button type="submit">保存</button><button type="button" onClick={() => setEditingHistoryId(null)}>取消</button></form>}{pendingDelete && <div><p>确定删除{pendingDelete.type === "project" ? "项目历史" : "会话"}“{pendingDelete.label}”吗？</p><button onClick={() => pendingDelete.type === "project" ? void deleteProjectHistory(pendingDelete.id) : void deleteHistorySession(history.find((item) => item.session_id === pendingDelete.id) as HistoryItem)}>确定删除</button><button onClick={() => setPendingDelete(null)}>取消</button></div>}</div>}
      {leftCollapsed ? <aside className="command-rail collapsed-pane" aria-label="项目面板已折叠"><button className="collapsed-pane-button" onClick={() => setLeftCollapsed(false)} title="显示项目" aria-label="显示项目"><ChevronRight size={17} /></button></aside> : <aside className="command-rail" aria-label="项目和运行控制">
        <div className="rail-section"><div className="rail-section-heading"><button className="section-toggle" onClick={() => setWorkspaceExpanded((expanded) => !expanded)} aria-expanded={workspaceExpanded}><span className="section-toggle-chevron">{workspaceExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</span><span className="section-kicker">项目</span></button><span className="panel-heading-actions"><button className="icon-button" onClick={() => void chooseDirectory()} title="选择其他项目" aria-label="切换项目" disabled={!session || workspacePhase !== "idle"}><FolderCog size={15} /></button>{session && <button className="icon-button" onClick={() => void loadWorkspaceFiles(session.session_id)} title="刷新文件树" aria-label="刷新文件树" disabled={workspacePhase === "loading"}><RefreshCw size={15} /></button>}<button className="icon-button" onClick={() => setLeftCollapsed(true)} title="隐藏项目面板" aria-label="隐藏项目面板"><ChevronLeft size={15} /></button></span></div>{workspaceExpanded && <>
        {!session ? <button className="button button-primary full-width" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><FolderOpen size={16} />选择文件夹…</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={16} />正在加载项目…</> : <><FolderOpen size={16} />选择项目</>}</button> : <div className="session-bar"><span title={`本地路径：${session.workspace_root}`}>{projectName(workspaceRoot || session.workspace_root)}</span></div>}
        {!session && <div className="workspace-empty-state"><span className="workspace-empty-icon"><FolderOpen size={17} /></span><span><strong>尚未选择项目</strong><small>选择一个本地项目开始使用。</small></span></div>}
        {workspacePhase === "loading" && <div className="workspace-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={15} /><span>{projectSwitching ? "正在切换项目…" : "正在读取项目…"}</span></div>}
        {workspaceFiles.length > 0 && <div className="workspace-tree"><div className="tree-caption">{workspaceFiles.filter((entry) => entry.kind === "file").length} 个文件 · {workspaceFiles.filter((entry) => entry.kind === "directory").length} 个文件夹</div>{workspaceFiles.filter((entry) => !entry.path.includes("/")).sort((left, right) => Number(right.kind === "directory") - Number(left.kind === "directory") || left.path.localeCompare(right.path)).slice(0, 120).map((entry) => renderTreeEntry(entry))}{workspaceFiles.length > 120 && <div className="tree-more">仅显示前 120 项</div>}</div>}
        {workspacePhase === "loading" && workspaceFiles.length === 0 && <div className="workspace-skeleton" aria-hidden="true"><span /><span /><span /><span /></div>}
        {session && workspacePhase === "idle" && workspaceFiles.length === 0 && <div className="workspace-empty-state workspace-empty-folder"><span className="workspace-empty-icon"><FolderOpen size={17} /></span><span><strong>工作区为空</strong><small>此文件夹中没有可显示的内容。</small></span></div>}
        </>}</div>
        <div className="rail-resizer" onPointerDown={resizeRailSections} role="separator" aria-label="调整项目和会话面板高度" title="上下拖动调整面板高度"><span /></div>
        <div className="rail-section history-section">
          <div className="history-section-heading">
            <button className="section-toggle" onClick={() => setHistoryExpanded((expanded) => !expanded)} aria-expanded={historyExpanded}>
              <span className="section-toggle-chevron">{historyExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</span>
              <span className="section-kicker">会话</span>
            </button>
            {projectGroups.length === 0 && <button className="new-conversation-button" onClick={() => void newConversation()} title="开始新会话" disabled={backendStatus === "offline"}><SquarePen size={14} /><span>新建</span></button>}
          </div>
          {historyExpanded && <>
            {historyLoading && history.length === 0 && <p className="history-empty">正在加载会话…</p>}
            {historyError && <div className="history-connection-error" role="status">
              <strong>{backendStatus === "offline" ? "后端未连接" : "历史记录加载失败"}</strong>
              <span>{historyError}</span>
              <button type="button" onClick={() => void loadHistory(true)}>重新连接</button>
            </div>}
            {!historyLoading && !historyError && history.length === 0 && <p className="history-empty">暂无会话。</p>}
            {history.length > 0 && <div className="project-history-list">{projectGroups.map((group) => {
                      const collapsed = collapsedProjects.has(group.key);
                      const active = group.key === projectKey(session?.workspace_root);
                      return <section className={`project-history-group ${active ? "is-active" : ""}`} key={group.key}>
                        <div className="project-history-header-row">
                          <button className="project-history-heading" onClick={() => setCollapsedProjects((current) => { const next = new Set(current); if (next.has(group.key)) next.delete(group.key); else next.add(group.key); return next; })} aria-expanded={!collapsed} title={group.root ? `本地路径：${group.root}` : "项目路径不可用"}>
                            <span className="project-history-heading-main"><span className="project-history-chevron">{collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}</span><FolderOpen size={15} /><span><strong>{group.name}</strong><small>{group.items.length} 个会话</small></span></span>
                          </button>
                          <button className="project-new-button" onClick={() => void newConversation(group.root)} title={`在 ${group.name} 中新建会话`} aria-label={`在 ${group.name} 中新建会话`}><SquarePen size={14} /></button>
                          <button className="history-action-button history-action-danger" onClick={() => setPendingDelete({ type: "project", id: group.key, label: group.name })} title="删除项目历史" aria-label={`删除项目历史 ${group.name}`}><Trash2 size={14} /></button>
                        </div>
                        {!collapsed && <div className="history-list">{group.items.map((item) => {
                          const displayTitle = item.title ?? item.task ?? "新会话";
                          return <div className="history-item-row" key={item.session_id}>
                            <button className={`history-item ${item.session_id === session?.session_id ? "is-selected" : ""}`} onClick={() => void replayHistory(item.run_id, item.session_id, item.workspace_root)} title={displayTitle}><span className="history-item-copy"><strong>{displayTitle}</strong></span><time>{formatHistoryDate(item.started_at)}</time></button>
                            <button className="history-action-button" onClick={() => { setEditingHistoryId(item.session_id); setHistoryTitleDraft(displayTitle); }} title="重命名会话"><Pencil size={14} /></button>
                            <button className="history-action-button history-action-danger" onClick={() => setPendingDelete({ type: "session", id: item.session_id, label: displayTitle })} title="删除会话"><Trash2 size={14} /></button>
                          </div>;
                        })}</div>}
                      </section>;
                    })}</div>}
          </>}
        </div>
        </aside>}
      <div className={`pane-resizer ${leftCollapsed ? "is-hidden" : ""}`} onPointerDown={(event) => resizePane("left", event)} role="separator" aria-label="调整工作区面板宽度"></div>

      <section className="trace-workbench" aria-label="Agent 对话">
        <div className="workbench-heading"><div className="conversation-heading-main"><div className="conversation-title-row"><div className="section-kicker">对话</div><div className={`conversation-status status-${runStatus}`}><span className="status-chip-dot" />{runStatusLabel(runStatus)}</div></div></div></div>

        <div className="conversation-scroll" ref={conversationRef} aria-live="polite">
          {conversationTurns.map((turn) => {
            const turnThinkingEvents = turn.events.filter((event) => thinkingEventTypes.includes(event.type) && !isHiddenVerificationEvent(event));
            const ignoredGitDiffCallIds = new Set(turnThinkingEvents.filter(isIgnorableGitDiffFailure).map((event) => event.payload.tool_call_id).filter((value): value is string => typeof value === "string"));
            const visibleThinkingEvents = turnThinkingEvents.filter((event) => !isIgnorableGitDiffFailure(event) && !(event.type === "tool_started" && ignoredGitDiffCallIds.has(String(event.payload.tool_call_id))));
            const turnActivitySteps = buildActivitySteps(visibleThinkingEvents);
            const turnFinalAnswer = finalAnswerForEvents(turn.events);
            const isLatestTurn = turn.key === latestTurn?.key;
            const turnBusy = isLatestTurn && busy;
            const turnAnswer = isLatestTurn && answerStreaming ? displayedAnswer : turnFinalAnswer;
            const verification = verificationForEvents(turn.events);
            const showVerification = Boolean(
              verification
              && (verification.status === "verified" || verification.status === "partially_verified")
              && turnFinalAnswer
              && !turnBusy
              && (!isLatestTurn || !answerStreaming),
            );
            const hasAgentError = turnThinkingEvents.some((event) => event.type === "agent_error");
            const stats = conversationStats(turn.events);
            const turnActiveTool = isLatestTurn ? activeToolForEvents(turn.events) : undefined;
            const turnApproval = isLatestTurn ? [...turn.events].reverse().find((event) => event.type === "approval_requested") : undefined;
            const approvalId = turnApproval?.payload.approval_id;
            const approvalIsPending = Boolean(turnApproval && pendingApproval && approvalId === pendingApproval.payload.approval_id);
            const changeIds = [...new Set(changeIdsForEvents(turn.events))]
              .filter((changeId) => !unavailableChanges.has(changeId));
            return <div className="conversation-turn" key={turn.key}>
              <article className="chat-message user-message"><div className="message-avatar user-avatar"><UserRound size={16} /></div><div className="message-body"><div className="message-meta"><strong>你</strong><time>{formatTime(turn.userEvent.timestamp)}</time></div><p>{readPayloadString(turn.userEvent, "content")}</p></div></article>
              {(turnActivitySteps.length > 0 || turnAnswer) && <article className="chat-message agent-message">
                <div className="message-avatar agent-avatar"><Bot size={16} /></div>
                <div className="message-body">
                  <div className="message-meta"><strong>Agent</strong><time>{turnBusy ? "执行中" : turnFinalAnswer ? "最终回复" : "活动"}</time><span className="message-stats"><span><Clock3 size={11} />{stats.duration}</span><span>{stats.iterations || 0} 步</span><span>{stats.tools || 0} 个工具</span></span></div>
                  {approvalIsPending && pendingApproval && <ApprovalCard approval={pendingApproval} busy={approvalBusy} onDecide={(approved, approveCurrentTurn) => void decideApproval(approved, approveCurrentTurn)} />}
                  {turnActivitySteps.length > 0 && <details className="thinking-inline" open={isLatestTurn ? thinkingExpanded : expandedActivityTurns.has(turn.key)} onToggle={(event) => { const open = event.currentTarget.open; if (isLatestTurn) setThinkingExpanded(open); else setExpandedActivityTurns((current) => { const next = new Set(current); if (open) next.add(turn.key); else next.delete(turn.key); return next; }); }}>
                    <summary><span className="thinking-summary-main"><span className="thinking-avatar"><BrainCircuit size={16} /></span><span><strong>{turnBusy ? "Agent 活动" : "Agent 活动已完成"}</strong><small>{turnBusy ? (turnActiveTool ? `正在${toolDisplayName(turnActiveTool)}` : "正在处理任务") : `${turnActivitySteps.length} 个活动步骤`}</small></span></span><span className="thinking-summary-state">{turnBusy ? <LoaderCircle className="spin" size={15} /> : hasAgentError ? <XCircle size={15} /> : <CheckCircle2 size={15} />}</span></summary>
                    <div className="thinking-steps">{turnActivitySteps.map((step) => { const presentation = activityPresentation(step); return <div className={`thinking-step tone-${eventTone(step.event.type)}`} key={step.events[0].event_id}><span className="thinking-step-icon">{step.event.type.startsWith("tool") ? <Wrench size={14} /> : iconForEvent(step.event.type)}</span><div className="thinking-step-body"><div className="thinking-step-title"><strong>{presentation.title}</strong><time>{formatTime(step.event.timestamp)}</time></div><p>{presentation.description}</p><details className="thinking-payload"><summary>执行详情</summary><dl className="execution-details">{activityDetails(step).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></details></div></div>; })}</div>
                  </details>}
                  {turnAnswer && <div className="answer-copy"><MarkdownAnswer content={turnAnswer} /><span className={`answer-cursor ${isLatestTurn && answerStreaming ? "is-visible" : ""}`} aria-hidden="true" /></div>}
                  {showVerification && verification && <VerificationCard summary={verification} />}
                  {changeIds.length > 0 && isLatestTurn && turnFinalAnswer && !turnBusy && <div className="changes-actions"><span><strong>文件修改</strong><small>仅可撤销当前会话最新一轮修改。</small></span>{changeIds.map((changeId) => revertedChanges.has(changeId) ? <span className="change-reverted" key={changeId}><CheckCircle2 size={14} />已撤销修改</span> : <button className="undo-button" key={changeId} onClick={() => void revertChange(changeId)}><RotateCcw size={14} />撤销修改</button>)}</div>}
                </div>
              </article>}
            </div>;
          })}
          {busy && !latestFinalAnswer && latestTurn && !latestTurn.events.some((event) => thinkingEventTypes.includes(event.type)) && <div className="activity-pending"><LoaderCircle className="spin" size={14} />正在准备活动</div>}
        {!session && <div className="workspace-gate"><div className="workspace-gate-icon"><FolderOpen size={20} /></div><h3>请先选择项目</h3><p>选择本地项目后即可使用文件和命令工具。</p><button className="button button-primary" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><FolderOpen size={14} />选择文件夹…</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={14} />正在加载项目…</> : <><FolderOpen size={14} />选择项目</>}</button></div>}
          {!events.length && session && <div className="empty-trace conversation-empty"><div className="empty-icon"><Terminal size={19} /></div><h3>你想从哪里开始？</h3><p>选择一个起点，或在下方描述自己的任务。</p><div className="starter-grid">{starterActions.map((action) => { const Icon = action.icon; return <button type="button" className="starter-card" key={action.id} onClick={() => chooseStarterPrompt(action.prompt)} disabled={busy || workspacePhase !== "idle"}><Icon size={17} /><span><strong>{action.title}</strong><small>{action.description}</small></span></button>; })}</div></div>}
          {error && <div className="error-banner"><XCircle size={15} />{error}</div>}
        </div>

        <div className={`conversation-composer ${!session ? "is-locked" : ""}`}><div className="composer-approval-row"><label className="composer-approval-toggle"><input type="checkbox" checked={autoApprovalEnabled} onChange={(event) => setAutoApprovalEnabled(event.target.checked)} disabled={!session || busy || workspacePhase !== "idle"} /><ShieldCheck size={14} /><span>自动批准本地操作</span></label><span className={`composer-approval-status ${autoApprovalEnabled ? "is-enabled" : ""}`}>{autoApprovalEnabled ? "本条消息自动批准" : "需要手动审批"}</span></div><label className="visually-hidden" htmlFor="task-upgraded">给 Agent 发送消息</label><div className="composer-input-shell"><textarea ref={taskInputRef} id="task-upgraded" value={task} onChange={(event) => setTask(event.target.value)} onKeyDown={(event) => { if (event.key !== "Enter" || event.nativeEvent.isComposing || event.ctrlKey || event.shiftKey || event.altKey || event.metaKey) return; event.preventDefault(); void runTask(); }} aria-label="给 Agent 发送消息。按 Enter 发送，按 Ctrl+Enter 换行。" placeholder={session ? "让 Agent 检查、修改并验证你的工作区…" : "选择工作区后即可使用本地 Agent 工具…"} rows={3} disabled={!session || busy || workspacePhase !== "idle"} /><button className={`composer-submit ${busy ? "is-cancel" : ""}`} onClick={() => { if (busy) void cancelRun(); else void runTask(); }} disabled={busy ? false : !session || !task.trim()} aria-label={busy ? "取消 Agent 执行" : "发送消息"} title={busy ? "取消 Agent 执行" : "发送消息"}>{busy ? <CircleStop size={16} /> : <Send size={16} />}{busy ? "取消" : "发送"}</button></div></div>
      </section>

      <div className={`pane-resizer ${rightCollapsed ? "is-hidden" : ""}`} onPointerDown={(event) => resizePane("right", event)} role="separator" aria-label="调整文件预览面板宽度"></div>
      {rightCollapsed ? <aside className="file-preview-panel collapsed-pane" aria-label="文件预览面板已折叠"><button className="collapsed-pane-button" onClick={() => setRightCollapsed(false)} title="显示预览" aria-label="显示预览"><ChevronLeft size={17} /></button></aside> : <aside className="file-preview-panel" aria-label="文件预览"><div className="preview-heading"><div><div className="section-kicker">文件预览</div><h3>{filePreview?.path ?? selectedFilePath ?? "暂无文件"}</h3></div><span className="panel-heading-actions"><button className="icon-button" onClick={() => setRightCollapsed(true)} title="隐藏预览面板" aria-label="隐藏预览面板"><ChevronRight size={15} /></button></span></div>{previewState === "loading" && <div className="inspector-empty preview-state"><LoaderCircle className="spin" size={20} /><p>正在加载预览…</p><span>正在读取文件。</span></div>}{previewState === "unsupported" && <div className="inspector-empty preview-state preview-unsupported"><FileWarning size={22} /><h4>无法预览</h4><p>{previewMessage}</p></div>}{previewState === "error" && <div className="inspector-empty preview-state preview-error"><XCircle size={22} /><h4>文件预览失败</h4><p>{previewMessage}</p></div>}{previewState === "ready" && filePreview && <CodePreview preview={filePreview} />}{previewState === "idle" && <div className="inspector-empty"><FolderOpen size={18} /><p>选择文件后即可预览。</p></div>}</aside>}
    </main>
  </div>;
}
