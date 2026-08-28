import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactElement, ReactNode } from "react";
import {
  FolderCog,
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
  LoaderCircle,
  ListChecks,
  RotateCcw,
  Send,
  ShieldCheck,
  ShieldX,
  Terminal,
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

type SessionResponse = { session_id: string; workspace_root: string };
type RunResponse = { run_id: string; session_id: string; status: string };
type ContextStats = {
  total_chars?: number;
  max_chars?: number;
  utilization?: number;
  compaction_count?: number;
};
type FilePreview = { path: string; content: string; truncated?: boolean };
type WorkspaceEntry = { path: string; kind: "file" | "directory" };
type WorkspacePhase = "idle" | "selecting" | "loading";
type PreviewState = "idle" | "loading" | "ready" | "unsupported" | "error";
type ActivityStep = { event: AgentEvent; events: AgentEvent[] };
type ConversationTurn = { key: string; userEvent: AgentEvent; events: AgentEvent[] };

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";
const eventLabels: Record<string, string> = {
  session_started: "Session started",
  user_message: "User message",
  iteration_started: "Iteration started",
  model_request: "Model request",
  model_response: "Model response",
  tool_started: "Tool started",
  tool_finished: "Tool finished",
  tool_failed: "Tool failed",
  approval_requested: "Approval needed",
  approval_resolved: "Approval resolved",
  assistant_message: "Assistant message",
  plan: "Plan",
  reflection: "Reflection",
  context_truncated: "Context truncated",
  context_compacted: "Context compacted",
  agent_finished: "Agent finished",
  agent_error: "Agent error",
};
const eventTypes = Object.keys(eventLabels);
function eventTone(type: string): "neutral" | "working" | "success" | "danger" {
  if (["tool_started", "model_request", "iteration_started"].includes(type)) return "working";
  if (type === "approval_requested") return "working";
  if (type === "plan") return "working";
  if (["tool_finished", "agent_finished", "context_compacted", "approval_resolved"].includes(type)) return "success";
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

function formatDuration(events: AgentEvent[]) {
  if (events.length < 2) return "--";
  const start = new Date(events[0].timestamp).getTime();
  const end = new Date(events.at(-1)?.timestamp ?? events[0].timestamp).getTime();
  const seconds = Math.max(0, (end - start) / 1000);
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function readPayloadString(event: AgentEvent, key: string) {
  const value = event.payload[key];
  return typeof value === "string" ? value : undefined;
}

function toolDisplayName(toolName: unknown) {
  const names: Record<string, string> = {
    list_files: "Inspect workspace",
    read_file: "Read file",
    write_file: "Create or update file",
    replace_in_file: "Update file",
    apply_patch: "Apply code changes",
    execute_command: "Run local command",
    search_files: "Search workspace",
    get_file_info: "Inspect file",
    list_directory_tree: "Inspect workspace tree",
    git_diff: "Review workspace changes",
  };
  const rawName = typeof toolName === "string" && toolName ? toolName : "local tool";
  return names[rawName] ?? rawName.replaceAll("_", " ");
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function activityOutput(step: ActivityStep) {
  const completed = [...step.events].reverse().find((event) => ["tool_finished", "tool_failed"].includes(event.type));
  return objectValue(completed?.payload.output);
}

function activityPresentation(step: ActivityStep) {
  const event = step.event;
  const output = activityOutput(step);
  const toolName = toolDisplayName(event.payload.tool_name);
  if (event.type === "iteration_started") return { title: `Step ${event.iteration ?? ""}`.trim(), description: "Choosing the next action for this task." };
  if (event.type === "model_request") return { title: "Analyzing the task", description: "Preparing the next action." };
  if (event.type === "model_response") {
    const toolCallCount = Number(event.payload.tool_call_count ?? 0);
    return toolCallCount > 0
      ? { title: "Action selected", description: `${toolCallCount} local action${toolCallCount === 1 ? "" : "s"} queued.` }
      : { title: "Preparing the response", description: "The task is ready for a final answer." };
  }
  if (event.type === "plan") return { title: "Plan", description: readPayloadString(event, "content") ?? "Organizing the work into steps." };
  if (event.type === "reflection") return { title: "Checking the result", description: readPayloadString(event, "content") ?? "Reviewing the latest result." };
  if (event.type === "tool_started" || event.type === "tool_finished") {
    if (toolName === "Run local command" && typeof output?.command === "string") return { title: toolName, description: output.command };
    if (typeof output?.path === "string") return { title: toolName, description: output.path };
    if (Array.isArray(output?.touched_files)) return { title: toolName, description: `${output.touched_files.length} file${output.touched_files.length === 1 ? "" : "s"} updated locally.` };
    return { title: event.type === "tool_started" ? toolName : `${toolName} complete`, description: event.type === "tool_started" ? "Working in your local workspace." : "Local operation completed successfully." };
  }
  if (event.type === "tool_failed") {
    const error = readPayloadString(event, "error") ?? "The local operation could not be completed.";
    return error.includes("replacement count mismatch")
      ? { title: "Update needs a fresh match", description: "The target text was not found exactly. The Agent should reread the file before trying again." }
      : error.includes("unified diff contains no hunks")
        ? { title: "Patch has no changes", description: "The Agent provided file headers without an actual @@ change block. It should reread the file and use replace_in_file for a simple edit or regenerate a complete patch." }
      : error.includes("hunk line count mismatch")
        ? { title: "Patch line counts need fixing", description: "The @@ header counts do not match the hunk lines. Old count = context + removed lines; new count = context + added lines. The Agent should regenerate the hunk instead of resending it." }
      : error.includes("invalid unified diff")
        ? { title: "Patch needs regeneration", description: "The patch format was invalid. The Agent should reread the file and create a fresh patch." }
        : { title: `${toolName} needs attention`, description: error };
  }
  if (event.type === "approval_requested") {
    if (event.payload.automatic === true && event.payload.approval_scope === "session") {
      return { title: "Auto-approved", description: "Session auto-approval is enabled for this operation." };
    }
    return event.payload.automatic === true
      ? { title: "Auto-approved", description: "This operation is covered by approval for the current message." }
      : { title: "Approval needed", description: "Waiting for your approval before a local change or command." };
  }
  if (event.type === "approval_resolved") {
    const decision = readPayloadString(event, "decision");
    if (decision === "approved" && event.payload.automatic === true) {
      return event.payload.approval_scope === "session"
        ? { title: "Auto-approved", description: "Approved automatically while session auto-approval is enabled." }
        : { title: "Auto-approved", description: "Approved automatically for this message." };
    }
    if (decision === "approved" && event.payload.approval_scope === "current_turn") {
      return { title: "Approved for this message", description: "This approval covers the remaining local operations in the current message." };
    }
    return decision === "approved"
      ? { title: "Approval granted", description: "The local operation is allowed to continue." }
      : { title: "Approval declined", description: "The local operation was blocked by your decision." };
  }
  if (event.type === "context_truncated") return { title: "Context trimmed", description: "Keeping the most relevant conversation details." };
  if (event.type === "context_compacted") return { title: "Memory compressed", description: "Conversation context was condensed to stay within the model budget." };
  if (event.type === "agent_error") return { title: "Run stopped", description: readPayloadString(event, "error") ?? "The Agent could not finish this task." };
  return { title: eventLabels[event.type] ?? "Agent activity", description: "Agent state updated." };
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
    if (event.type === "context_truncated" || event.type === "context_compacted" || event.type === "agent_error") {
      steps.push({ event, events: [event] });
      modelStep = undefined;
    }
  }
  return steps;
}

function activityDetails(step: ActivityStep) {
  const event = step.event;
  const output = activityOutput(step);
  const details: Array<[string, string]> = [["Status", event.type === "tool_failed" || event.type === "agent_error" ? "Needs attention" : event.type === "approval_resolved" ? (readPayloadString(event, "decision") === "approved" ? "Approved" : "Declined") : ["tool_finished", "agent_finished"].includes(event.type) ? "Completed" : "In progress"]];
  if (typeof event.payload.tool_name === "string") details.push(["Tool", event.payload.tool_name]);
  if (event.iteration !== null) details.push(["Iteration", String(event.iteration)]);
  if (typeof output?.path === "string") details.push(["Path", output.path]);
  if (typeof output?.command === "string") details.push(["Command", output.command]);
  if (Array.isArray(output?.touched_files)) details.push(["Files updated", String(output.touched_files.length)]);
  if (typeof output?.duration_seconds === "number") details.push(["Duration", `${output.duration_seconds.toFixed(2)}s`]);
  if (typeof event.payload.tool_call_count === "number") details.push(["Tool calls", String(event.payload.tool_call_count)]);
  if (typeof event.payload.message_count === "number") details.push(["Messages", String(event.payload.message_count)]);
  const approvalDetailsValue = objectValue(event.payload.details);
  if (event.type === "approval_requested" && approvalDetailsValue) {
    if (typeof approvalDetailsValue.command === "string") details.push(["Command", approvalDetailsValue.command]);
    if (Array.isArray(approvalDetailsValue.command)) details.push(["Command", approvalDetailsValue.command.join(" ")]);
    if (typeof approvalDetailsValue.path === "string") details.push(["Path", approvalDetailsValue.path]);
  }
  const error = readPayloadString(event, "error");
  if (error) details.push(["Error", error]);
  return details;
}

const thinkingEventTypes = [
  "iteration_started",
  "model_request",
  "model_response",
  "plan",
  "reflection",
  "tool_started",
  "tool_finished",
  "tool_failed",
  "approval_requested",
  "approval_resolved",
  "context_truncated",
  "context_compacted",
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

function conversationStats(events: AgentEvent[]) {
  return {
    duration: formatDuration(events),
    iterations: new Set(events.map((event) => event.iteration).filter((iteration): iteration is number => iteration !== null)).size,
    tools: events.filter((event) => event.type === "tool_started").length,
    failures: events.filter((event) => event.type === "tool_failed").length,
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

function CodePreview({ preview }: { preview: FilePreview }): ReactElement {
  const language = previewLanguage(preview.path);
  const lines = preview.content.split(/\r?\n/);
  return <div className="file-preview-content">
    <div className="preview-toolbar"><span className="preview-language">{language}</span><span>{lines.length} lines{preview.truncated ? " · truncated" : ""}</span></div>
    <div className="code-frame"><ol className="code-lines">{lines.map((line, index) => <li key={index}><span className="line-number">{index + 1}</span><code>{highlightCodeLine(line, language)}</code></li>)}</ol></div>
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
        : <pre className="markdown-code-block" key={"code-" + blockIndex++}><code className={language ? "language-" + language : undefined}>{codeLines.join("\n")}</code></pre>);
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
  return <section className="approval-card" aria-label="Approval required">
    <div className="approval-card-heading"><span className="approval-card-icon"><ShieldCheck size={17} /></span><div><strong>Approval required</strong><p>{readPayloadString(approval, "summary") ?? "The Agent wants to perform a local operation."}</p></div></div>
    {command && <div className="approval-field"><span>Command</span><code>{command}</code></div>}
    {typeof details?.cwd === "string" && <div className="approval-field"><span>Working folder</span><code>{details.cwd}</code></div>}
    {typeof details?.path === "string" && <div className="approval-field"><span>File</span><code>{details.path}</code></div>}
    {preview && <pre className="approval-preview">{preview}</pre>}
    <div className="approval-actions"><button className="approval-button approval-button-reject" onClick={() => onDecide(false)} disabled={busy}><ShieldX size={15} />Reject</button><button className="approval-button approval-button-approve-once" onClick={() => onDecide(true)} disabled={busy}><ShieldCheck size={15} />Approve once</button><button className="approval-button approval-button-approve-turn" onClick={() => onDecide(true, true)} disabled={busy} title="Approve this operation and automatically approve later local operations from the current message"><ListChecks size={15} />Auto-approve this message</button></div>
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workspaceExpanded, setWorkspaceExpanded] = useState(true);
  const [historyExpanded, setHistoryExpanded] = useState(true);
  const [thinkingExpanded, setThinkingExpanded] = useState(true);
  const [expandedActivityTurns, setExpandedActivityTurns] = useState<Set<string>>(new Set());
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set());
  const [workspacePhase, setWorkspacePhase] = useState<WorkspacePhase>("idle");
  const [displayedAnswer, setDisplayedAnswer] = useState("");
  const [answerStreaming, setAnswerStreaming] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<AgentEvent | null>(null);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [revertedChanges, setRevertedChanges] = useState<Set<string>>(new Set());
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [leftWidth, setLeftWidth] = useState(270);
  const [rightWidth, setRightWidth] = useState(310);
  const eventSource = useRef<EventSource | null>(null);
  const answerTimer = useRef<number | null>(null);
  const refreshInFlight = useRef(false);
  const selectedFilePathRef = useRef<string | null>(null);
  const previewRequestIdRef = useRef(0);
  const conversationRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => () => eventSource.current?.close(), []);

  const contextStats = useMemo(() => {
    const candidate = [...events].reverse().find((event) => typeof event.payload.context === "object");
    return candidate?.payload.context as ContextStats | undefined;
  }, [events]);
  const conversationTurns = useMemo(() => buildConversationTurns(events), [events]);
  const latestTurn = conversationTurns.at(-1);
  const currentTurnEvents = latestTurn?.events ?? [];
  const latestFinalAnswer = useMemo(() => finalAnswerForEvents(currentTurnEvents), [currentTurnEvents]);
  const runStatus = useMemo(() => {
    if (!session) return "workspace-required";
    const finished = [...events].reverse().find((event) => event.type === "agent_finished");
    return readPayloadString(finished ?? events[0] ?? { payload: {} } as AgentEvent, "status") ?? (busy ? "running" : "ready");
  }, [busy, events]);
  const history = useMemo(() => {
    try {
      return JSON.parse(localStorage.getItem("lvyiyou-agent-history") ?? "[]") as Array<{ task: string; timestamp: string }>;
    } catch {
      return [];
    }
  }, [events.length, session]);

  useEffect(() => {
    if (answerTimer.current !== null) window.clearInterval(answerTimer.current);
    const answer = latestFinalAnswer ?? "";
    if (!answer) {
      setDisplayedAnswer("");
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
  }, [latestFinalAnswer, latestTurn?.key]);

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

  async function createSession(root: string) {
    if (!root) return;
    setError(null);
    setEvents([]);
    setRun(null);
    setAutoApprovalEnabled(false);
    setExpandedActivityTurns(new Set());
    setPendingApproval(null);
    setApprovalBusy(false);
    setRevertedChanges(new Set());
    try {
      const response = await fetch(`${API_BASE}/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_root: root }) });
      if (!response.ok) throw new Error(await responseError(response, "Could not create session"));
      const created = (await response.json()) as SessionResponse;
      setSession(created);
      await loadWorkspaceFiles(created.session_id);
    } catch (reason) {
      setWorkspacePhase("idle");
      setError(reason instanceof Error ? reason.message : "Could not create session");
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
    setThinkingExpanded(true);
    setBusy(true);
    eventSource.current?.close();
    try {
      const response = await fetch(`${API_BASE}/sessions/${session.session_id}/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: submittedTask, auto_approve: autoApprovalEnabled }) });
      if (!response.ok) throw new Error(await responseError(response, "Could not start run"));
      const started = (await response.json()) as RunResponse;
      const nextHistory = [{ task: submittedTask, timestamp: new Date().toISOString() }, ...history.filter((item) => item.task !== submittedTask)].slice(0, 8);
      localStorage.setItem("lvyiyou-agent-history", JSON.stringify(nextHistory));
      setTask("");
      setRun(started);
      const source = new EventSource(`${API_BASE}/runs/${started.run_id}/events`);
      eventSource.current = source;
      const handleEvent = (message: Event) => {
        const event = JSON.parse((message as MessageEvent<string>).data) as AgentEvent;
        setEvents((current) => [...current, event]);
        if (event.type === "approval_requested") {
          setPendingApproval(event);
          setApprovalBusy(false);
          setThinkingExpanded(true);
        }
        if (event.type === "approval_resolved") {
          setPendingApproval(null);
          setApprovalBusy(false);
        }
        if (["tool_finished", "tool_failed", "agent_finished"].includes(event.type)) {
          void loadWorkspaceFiles(started.session_id).catch((reason) => {
            setError(reason instanceof Error ? reason.message : "Could not refresh workspace");
          });
        }
        if (event.type === "tool_finished" && selectedFilePathRef.current) {
          void selectFile(selectedFilePathRef.current, started.session_id).catch((reason) => {
            setError(reason instanceof Error ? reason.message : "Could not refresh file preview");
          });
        }
        if (event.type === "agent_finished" || event.type === "agent_error") {
          setBusy(false);
          setPendingApproval(null);
          setApprovalBusy(false);
          setThinkingExpanded(false);
          source.close();
        }
      };
      eventTypes.forEach((eventType) => source.addEventListener(eventType, handleEvent));
      source.onerror = () => {
        setBusy(false);
        setPendingApproval(null);
        setApprovalBusy(false);
        source.close();
        setError("The event stream closed unexpectedly.");
      };
    } catch (reason) {
      setBusy(false);
      setError(reason instanceof Error ? reason.message : "Could not start run");
    }
  }

  async function cancelRun() {
    if (!run) return;
    try {
      await fetch(`${API_BASE}/runs/${run.run_id}/cancel`, { method: "POST" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not cancel run");
    }
  }

  async function decideApproval(approved: boolean, approveCurrentTurn = false) {
    if (!session || !pendingApproval) return;
    const approvalId = pendingApproval.payload.approval_id;
    if (typeof approvalId !== "string") return;
    setApprovalBusy(true);
    try {
      const response = await fetch(
        `${API_BASE}/sessions/${session.session_id}/approvals/${approvalId}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved, approve_current_turn: approveCurrentTurn }),
        },
      );
      if (!response.ok) throw new Error(await responseError(response, "Could not resolve approval"));
    } catch (reason) {
      setApprovalBusy(false);
      setError(reason instanceof Error ? reason.message : "Could not resolve approval");
    }
  }

  async function revertChange(changeId: string) {
    if (!session) return;
    try {
      const response = await fetch(
        `${API_BASE}/sessions/${session.session_id}/changes/${changeId}/revert`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await responseError(response, "Could not undo changes"));
      setRevertedChanges((current) => new Set(current).add(changeId));
      await loadWorkspaceFiles(session.session_id);
      if (selectedFilePathRef.current) {
        await selectFile(selectedFilePathRef.current, session.session_id);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not undo changes");
    }
  }

  async function chooseDirectory() {
    setError(null);
    setWorkspacePhase("selecting");

    try {
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      const response = await fetch(`${API_BASE}/workspaces/select`);
      if (!response.ok) throw new Error(await responseError(response, "Could not choose workspace"));
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
      setError(reason instanceof Error ? reason.message : "Could not choose workspace");
      setWorkspacePhase("idle");
    }
  }

  async function loadWorkspaceFiles(sessionId: string) {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/files`);
      if (!response.ok) throw new Error(await responseError(response, "Could not list workspace files"));
      const files = (await response.json()) as WorkspaceEntry[];
      setWorkspaceFiles(files);
      const firstFile = files.find((entry) => entry.kind === "file");
      if (firstFile && !selectedFilePathRef.current) {
        window.setTimeout(() => {
          void selectFile(firstFile.path, sessionId).catch((reason) => {
            setError(reason instanceof Error ? reason.message : "Could not preview file");
          });
        }, 0);
      }
    } finally {
      refreshInFlight.current = false;
      setWorkspacePhase("idle");
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
        const message = await responseError(response, "Could not preview file");
        if (requestId !== previewRequestIdRef.current) return;
        const unsupported = response.status === 415 || /binary|UTF-8|text file|not supported/i.test(message);
        setPreviewState(unsupported ? "unsupported" : "error");
        setPreviewMessage(unsupported ? "This file is not a supported UTF-8 text file." : message);
        return;
      }
      const preview = (await response.json()) as FilePreview;
      if (requestId !== previewRequestIdRef.current) return;
      setFilePreview(preview);
      setPreviewState("ready");
    } catch (reason) {
      if (requestId !== previewRequestIdRef.current) return;
      setPreviewState("error");
      setPreviewMessage(reason instanceof Error ? reason.message : "Could not preview file");
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
      else setRightWidth(Math.min(420, Math.max(220, startWidth - delta)));
    };
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
      void loadWorkspaceFiles(session.session_id).catch((reason) => setError(reason instanceof Error ? reason.message : "Could not refresh workspace"));
    }, 700);
    return () => window.clearInterval(timer);
  }, [busy, session]);

  const layoutStyle = {
    "--left-pane-width": leftCollapsed ? "38px" : `${leftWidth}px`,
    "--right-pane-width": rightCollapsed ? "38px" : `${rightWidth}px`,
  } as CSSProperties;

  return <div className="console-shell">
    <header className="console-topbar">
      <div className="brand-lockup"><div className="brand-mark"><Bot size={20} /></div><div><p className="brand-name">Local Coding Agent</p><p className="brand-caption">Self-hosted runtime console</p></div></div>
      <div className={`connection-state ${session ? "is-ready" : ""}`}><span className="state-dot" />{busy ? "Run in progress" : session ? "Session ready" : "Workspace required"}</div>
    </header>

    <main className="console-layout" style={layoutStyle}>
      {leftCollapsed ? <aside className="command-rail collapsed-pane" aria-label="Workspace panel collapsed"><button className="collapsed-pane-button" onClick={() => setLeftCollapsed(false)} title="Show workspace" aria-label="Show workspace"><ChevronRight size={17} /></button></aside> : <aside className="command-rail" aria-label="Workspace and run controls">
        <div className="rail-section"><div className="rail-section-heading"><button className="section-toggle" onClick={() => setWorkspaceExpanded((expanded) => !expanded)} aria-expanded={workspaceExpanded}><span className="section-toggle-chevron">{workspaceExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</span><span className="section-kicker">Workspace</span></button><span className="panel-heading-actions"><button className="icon-button" onClick={() => void chooseDirectory()} title="Choose another workspace" aria-label="Change workspace" disabled={!session || workspacePhase !== "idle"}><FolderCog size={15} /></button><button className="icon-button" onClick={() => setLeftCollapsed(true)} title="Hide workspace panel" aria-label="Hide workspace panel"><ChevronLeft size={15} /></button></span></div>{workspaceExpanded && <>
        {!session ? <button className="button button-primary full-width" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><FolderOpen size={16} />Selecting folder...</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={16} />Loading workspace...</> : <><FolderOpen size={16} />Choose folder</>}</button> : <div className="session-bar"><span title={session.workspace_root}>{workspaceRoot || session.workspace_root}</span></div>}
        {!session && <div className="workspace-empty-state"><span className="workspace-empty-icon"><FolderOpen size={17} /></span><span><strong>No folder selected</strong><small>Choose a local folder to get started.</small></span></div>}
        {workspacePhase === "loading" && <div className="workspace-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={15} /><span>Scanning local workspace...</span></div>}
        {workspaceFiles.length > 0 && <div className="workspace-tree"><div className="tree-caption">{workspaceFiles.filter((entry) => entry.kind === "file").length} files · {workspaceFiles.filter((entry) => entry.kind === "directory").length} folders</div>{workspaceFiles.filter((entry) => !entry.path.includes("/")).sort((left, right) => Number(right.kind === "directory") - Number(left.kind === "directory") || left.path.localeCompare(right.path)).slice(0, 120).map((entry) => renderTreeEntry(entry))}{workspaceFiles.length > 120 && <div className="tree-more">Showing first 120 entries</div>}</div>}
        {workspacePhase === "loading" && workspaceFiles.length === 0 && <div className="workspace-skeleton" aria-hidden="true"><span /><span /><span /><span /></div>}
        </>}</div>
        <div className="rail-section history-section"><button className="section-toggle" onClick={() => setHistoryExpanded((expanded) => !expanded)} aria-expanded={historyExpanded}><span className="section-toggle-chevron">{historyExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</span><span className="section-kicker">Recent tasks</span></button>{historyExpanded && <>{history.length === 0 ? <p className="history-empty">Completed tasks will appear here.</p> : <div className="history-list">{history.map((item) => <button className="history-item" key={`${item.timestamp}-${item.task}`} onClick={() => setTask(item.task)}><span>{item.task}</span><time>{new Date(item.timestamp).toLocaleDateString()}</time></button>)}</div>}</>}</div>
      </aside>}
      <div className={`pane-resizer ${leftCollapsed ? "is-hidden" : ""}`} onPointerDown={(event) => resizePane("left", event)} role="separator" aria-label="Resize workspace panel"></div>

      <section className="trace-workbench" aria-label="Agent conversation">
        <div className="workbench-heading"><div className="conversation-heading-main"><div className="conversation-title-row"><div className="section-kicker">Conversation</div><div className={`conversation-status status-${runStatus}`}><span className="status-chip-dot" />{session ? runStatus : "workspace required"}</div></div></div><div className="context-budget-compact" title="Current context usage"><span>Context</span><strong>{contextStats ? `${contextStats.total_chars ?? 0} / ${contextStats.max_chars ?? 0}` : "--"}</strong><div className="budget-track"><span style={{ width: `${Math.min(100, (contextStats?.utilization ?? 0) * 100)}%` }} /></div></div></div>

        <div className="conversation-scroll" ref={conversationRef} aria-live="polite">
          {conversationTurns.map((turn) => {
            const turnThinkingEvents = turn.events.filter((event) => thinkingEventTypes.includes(event.type));
            const turnActivitySteps = buildActivitySteps(turnThinkingEvents);
            const turnFinalAnswer = finalAnswerForEvents(turn.events);
            const isLatestTurn = turn.key === latestTurn?.key;
            const turnBusy = isLatestTurn && busy;
            const turnAnswer = isLatestTurn && answerStreaming ? displayedAnswer : turnFinalAnswer;
            const hasAgentError = turnThinkingEvents.some((event) => event.type === "agent_error");
            const stats = conversationStats(turn.events);
            const turnActiveTool = isLatestTurn ? activeToolForEvents(turn.events) : undefined;
            const turnApproval = isLatestTurn ? [...turn.events].reverse().find((event) => event.type === "approval_requested") : undefined;
            const approvalId = turnApproval?.payload.approval_id;
            const approvalIsPending = Boolean(turnApproval && pendingApproval && approvalId === pendingApproval.payload.approval_id);
            const changeIds = [...new Set(changeIdsForEvents(turn.events))];
            return <div className="conversation-turn" key={turn.key}>
              <article className="chat-message user-message"><div className="message-avatar user-avatar"><UserRound size={16} /></div><div className="message-body"><div className="message-meta"><strong>You</strong><time>{formatTime(turn.userEvent.timestamp)}</time></div><p>{readPayloadString(turn.userEvent, "content")}</p></div></article>
              {(turnActivitySteps.length > 0 || turnAnswer) && <article className="chat-message agent-message">
                <div className="message-avatar agent-avatar"><Bot size={16} /></div>
                <div className="message-body">
                  <div className="message-meta"><strong>Agent</strong><time>{turnBusy ? "working" : turnFinalAnswer ? "final answer" : "activity"}</time><span className="message-stats"><span><Clock3 size={11} />{stats.duration}</span><span>{stats.iterations || 0} {stats.iterations === 1 ? "step" : "steps"}</span><span>{stats.tools || 0} {stats.tools === 1 ? "tool" : "tools"}</span>{stats.failures > 0 && <span className="message-stats-danger">{stats.failures} failed</span>}</span></div>
                  {approvalIsPending && pendingApproval && <ApprovalCard approval={pendingApproval} busy={approvalBusy} onDecide={(approved, approveCurrentTurn) => void decideApproval(approved, approveCurrentTurn)} />}
                  {turnActivitySteps.length > 0 && <details className="thinking-inline" open={isLatestTurn ? thinkingExpanded : expandedActivityTurns.has(turn.key)} onToggle={(event) => { const open = event.currentTarget.open; if (isLatestTurn) setThinkingExpanded(open); else setExpandedActivityTurns((current) => { const next = new Set(current); if (open) next.add(turn.key); else next.delete(turn.key); return next; }); }}>
                    <summary><span className="thinking-summary-main"><span className="thinking-avatar"><BrainCircuit size={16} /></span><span><strong>{turnBusy ? "Agent activity" : "Agent activity complete"}</strong><small>{turnBusy ? (turnActiveTool ? `Using ${toolDisplayName(turnActiveTool)}` : "Following the task") : `${turnActivitySteps.length} activity ${turnActivitySteps.length === 1 ? "step" : "steps"}`}</small></span></span><span className="thinking-summary-state">{turnBusy ? <LoaderCircle className="spin" size={15} /> : hasAgentError ? <XCircle size={15} /> : <CheckCircle2 size={15} />}</span></summary>
                    <div className="thinking-steps">{turnActivitySteps.map((step) => { const presentation = activityPresentation(step); return <div className={`thinking-step tone-${eventTone(step.event.type)}`} key={step.events[0].event_id}><span className="thinking-step-icon">{step.event.type.startsWith("tool") ? <Wrench size={14} /> : iconForEvent(step.event.type)}</span><div className="thinking-step-body"><div className="thinking-step-title"><strong>{presentation.title}</strong><time>{formatTime(step.event.timestamp)}</time></div><p>{presentation.description}</p><details className="thinking-payload"><summary>Execution details</summary><dl className="execution-details">{activityDetails(step).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></details></div></div>; })}</div>
                  </details>}
                  {turnAnswer && <div className="answer-copy"><MarkdownAnswer content={turnAnswer} /><span className={`answer-cursor ${isLatestTurn && answerStreaming ? "is-visible" : ""}`} aria-hidden="true" /></div>}
                  {changeIds.length > 0 && <div className="changes-actions"><span><strong>Local changes</strong><small>Unified diff included above.</small></span>{changeIds.map((changeId) => revertedChanges.has(changeId) ? <span className="change-reverted" key={changeId}><CheckCircle2 size={14} />Changes reverted</span> : <button className="undo-button" key={changeId} onClick={() => void revertChange(changeId)}><RotateCcw size={14} />Undo changes</button>)}</div>}
                </div>
              </article>}
            </div>;
          })}
          {busy && !latestFinalAnswer && latestTurn && !latestTurn.events.some((event) => thinkingEventTypes.includes(event.type)) && <div className="activity-pending"><LoaderCircle className="spin" size={14} />Preparing activity</div>}
        {!session && <div className="workspace-gate"><div className="workspace-gate-icon"><FolderOpen size={20} /></div><h3>Choose a workspace first</h3><p>Select a real local folder to enable file tools and commands.</p><button className="button button-primary" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><FolderOpen size={14} />Selecting folder...</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={14} />Loading workspace...</> : <><FolderOpen size={14} />Choose folder</>}</button></div>}
          {!events.length && session && <div className="empty-trace conversation-empty"><div className="empty-icon"><Terminal size={19} /></div><h3>Ready when you are</h3><p>Send a task and follow the Agent from intent to local tools to final answer.</p></div>}
          {error && <div className="error-banner"><XCircle size={15} />{error}</div>}
        </div>

        <div className={`conversation-composer ${!session ? "is-locked" : ""}`}><div className="composer-approval-row"><label className="composer-approval-toggle"><input type="checkbox" checked={autoApprovalEnabled} onChange={(event) => setAutoApprovalEnabled(event.target.checked)} disabled={!session || busy || workspacePhase !== "idle"} /><ShieldCheck size={14} /><span>Auto-approve local actions</span></label><span className={`composer-approval-status ${autoApprovalEnabled ? "is-enabled" : ""}`}>{autoApprovalEnabled ? "Enabled for sent messages" : "Manual approval"}</span></div><label className="visually-hidden" htmlFor="task-upgraded">Message the agent</label><div className="composer-input-shell"><textarea id="task-upgraded" value={task} onChange={(event) => setTask(event.target.value)} onKeyDown={(event) => { if (event.key !== "Enter" || event.nativeEvent.isComposing || event.ctrlKey || event.shiftKey || event.altKey || event.metaKey) return; event.preventDefault(); void runTask(); }} aria-label="Message the agent. Press Enter to send and Ctrl+Enter for a new line." placeholder={session ? "Ask the agent to inspect, edit, and verify your workspace..." : "Choose a workspace to enable local Agent tools..."} rows={3} disabled={!session || busy || workspacePhase !== "idle"} /><button className={`composer-submit ${busy ? "is-cancel" : ""}`} onClick={() => { if (busy) void cancelRun(); else void runTask(); }} disabled={busy ? false : !session || !task.trim()} aria-label={busy ? "Cancel agent run" : "Send message"} title={busy ? "Cancel agent run" : "Send message"}>{busy ? <CircleStop size={16} /> : <Send size={16} />}{busy ? "Cancel" : "Send"}</button></div></div>
      </section>

      <div className={`pane-resizer ${rightCollapsed ? "is-hidden" : ""}`} onPointerDown={(event) => resizePane("right", event)} role="separator" aria-label="Resize preview panel"></div>
      {rightCollapsed ? <aside className="file-preview-panel collapsed-pane" aria-label="File preview collapsed"><button className="collapsed-pane-button" onClick={() => setRightCollapsed(false)} title="Show preview" aria-label="Show preview"><ChevronLeft size={17} /></button></aside> : <aside className="file-preview-panel" aria-label="File preview"><div className="preview-heading"><div><div className="section-kicker">File preview</div><h3>{filePreview?.path ?? selectedFilePath ?? "No file"}</h3></div><span className="panel-heading-actions"><button className="icon-button" onClick={() => setRightCollapsed(true)} title="Hide preview panel" aria-label="Hide preview panel"><ChevronRight size={15} /></button></span></div>{previewState === "loading" && <div className="inspector-empty preview-state"><LoaderCircle className="spin" size={20} /><p>Loading preview...</p><span>Reading the selected local file.</span></div>}{previewState === "unsupported" && <div className="inspector-empty preview-state preview-unsupported"><FileWarning size={22} /><h4>Preview unavailable</h4><p>{previewMessage}</p></div>}{previewState === "error" && <div className="inspector-empty preview-state preview-error"><XCircle size={22} /><h4>Could not preview this file</h4><p>{previewMessage}</p></div>}{previewState === "ready" && filePreview && <CodePreview preview={filePreview} />}{previewState === "idle" && <div className="inspector-empty"><FolderOpen size={18} /><p>Choose a folder, then click a file in the workspace tree to preview it.</p></div>}</aside>}
    </main>
  </div>;
}
