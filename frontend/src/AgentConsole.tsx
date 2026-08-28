import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactElement, ReactNode } from "react";
import {
  BrainCircuit,
  Bot,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Clock3,
  Code2,
  FileWarning,
  FileCode2,
  FolderOpen,
  History,
  LoaderCircle,
  ListChecks,
  RotateCcw,
  Send,
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
  if (type === "plan") return "working";
  if (["tool_finished", "agent_finished", "context_compacted"].includes(type)) return "success";
  if (["tool_failed", "agent_error"].includes(type)) return "danger";
  return "neutral";
}

function iconForEvent(type: string) {
  if (type.startsWith("tool") && type !== "tool_failed") return <Terminal size={15} />;
  if (type === "plan") return <ListChecks size={15} />;
  if (type === "reflection") return <RotateCcw size={15} />;
  if (type === "tool_failed" || type === "agent_error") return <XCircle size={15} />;
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

export default function AgentConsole() {
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceEntry[]>([]);
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [filePreview, setFilePreview] = useState<FilePreview | null>(null);
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewMessage, setPreviewMessage] = useState("");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [task, setTask] = useState("");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [run, setRun] = useState<RunResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workspaceExpanded, setWorkspaceExpanded] = useState(true);
  const [historyExpanded, setHistoryExpanded] = useState(true);
  const [thinkingExpanded, setThinkingExpanded] = useState(true);
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set());
  const [workspacePhase, setWorkspacePhase] = useState<WorkspacePhase>("idle");
  const [displayedAnswer, setDisplayedAnswer] = useState("");
  const [answerStreaming, setAnswerStreaming] = useState(false);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [leftWidth, setLeftWidth] = useState(270);
  const [rightWidth, setRightWidth] = useState(310);
  const eventSource = useRef<EventSource | null>(null);
  const refreshInFlight = useRef(false);
  const selectedFilePathRef = useRef<string | null>(null);
  const previewRequestIdRef = useRef(0);
  const conversationRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => () => eventSource.current?.close(), []);

  const contextStats = useMemo(() => {
    const candidate = [...events].reverse().find((event) => typeof event.payload.context === "object");
    return candidate?.payload.context as ContextStats | undefined;
  }, [events]);
  const finalAnswer = useMemo(() => {
    const event = [...events].reverse().find((candidate) => candidate.type === "assistant_message" && candidate.payload.tool_call_count === 0 && readPayloadString(candidate, "content"));
    return event ? readPayloadString(event, "content") : undefined;
  }, [events]);
  const userMessages = useMemo(() => events.filter((event) => event.type === "user_message"), [events]);
  const thinkingEvents = useMemo(() => events.filter((event) => [
    "iteration_started",
    "model_request",
    "model_response",
    "plan",
    "reflection",
    "tool_started",
    "tool_finished",
    "tool_failed",
    "context_truncated",
    "context_compacted",
    "agent_error",
  ].includes(event.type)), [events]);
  const runStatus = useMemo(() => {
    if (!session) return "workspace-required";
    const finished = [...events].reverse().find((event) => event.type === "agent_finished");
    return readPayloadString(finished ?? events[0] ?? { payload: {} } as AgentEvent, "status") ?? (busy ? "running" : "ready");
  }, [busy, events]);
  const iterationCount = new Set(events.map((event) => event.iteration).filter((iteration): iteration is number => iteration !== null)).size;
  const toolCallCount = events.filter((event) => event.type === "tool_started").length;
  const failedToolCount = events.filter((event) => event.type === "tool_failed").length;
  const activeTool = [...events].reverse().find((event) => event.type === "tool_started" && !events.some((candidate) => ["tool_finished", "tool_failed"].includes(candidate.type) && candidate.payload.tool_call_id === event.payload.tool_call_id))?.payload.tool_name;

  const history = useMemo(() => {
    try {
      return JSON.parse(localStorage.getItem("lvyiyou-agent-history") ?? "[]") as Array<{ task: string; timestamp: string }>;
    } catch {
      return [];
    }
  }, [events.length, session]);

  useEffect(() => {
    const answer = finalAnswer ?? "";
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
        setAnswerStreaming(false);
      }
    }, 16);
    return () => window.clearInterval(timer);
  }, [finalAnswer]);

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
    if (!session || !submittedTask) return;
    setError(null);
    setEvents([]);
    setDisplayedAnswer("");
    setAnswerStreaming(false);
    setThinkingExpanded(true);
    setBusy(true);
    eventSource.current?.close();
    try {
      const response = await fetch(`${API_BASE}/sessions/${session.session_id}/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: submittedTask }) });
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
          source.close();
        }
      };
      eventTypes.forEach((eventType) => source.addEventListener(eventType, handleEvent));
      source.onerror = () => {
        setBusy(false);
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

  function resetSession() {
    eventSource.current?.close();
    setSession(null);
    setRun(null);
    setEvents([]);
    setTask("");
    setError(null);
    setBusy(false);
    setDisplayedAnswer("");
    setAnswerStreaming(false);
    setWorkspaceFiles([]);
    setExpandedFolders(new Set());
    setSelectedFilePath(null);
    selectedFilePathRef.current = null;
    previewRequestIdRef.current += 1;
    setFilePreview(null);
    setPreviewState("idle");
    setPreviewMessage("");
    setWorkspacePhase("idle");
  }

  async function chooseDirectory() {
    setError(null);
    setWorkspacePhase("selecting");
    setWorkspaceFiles([]);
    setSelectedFilePath(null);
    selectedFilePathRef.current = null;
    previewRequestIdRef.current += 1;
    setFilePreview(null);
    setPreviewState("idle");
    setPreviewMessage("");
    try {
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      const response = await fetch(`${API_BASE}/workspaces/select`);
      if (!response.ok) throw new Error(await responseError(response, "Could not choose workspace"));
      const selection = (await response.json()) as { workspace_root: string };
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
        <div className="rail-section"><div className="rail-section-heading"><button className="section-toggle" onClick={() => setWorkspaceExpanded((expanded) => !expanded)} aria-expanded={workspaceExpanded}><span className="section-kicker">Workspace</span><span className="toggle-mark">{workspaceExpanded ? "-" : "+"}</span></button><span className="panel-heading-actions"><button className="icon-button" onClick={resetSession} title="Change workspace" aria-label="Change workspace" disabled={!session}><RotateCcw size={15} /></button><button className="icon-button" onClick={() => setLeftCollapsed(true)} title="Hide workspace panel" aria-label="Hide workspace panel"><ChevronLeft size={15} /></button></span></div>{workspaceExpanded && <><h1>Local workspace</h1>
        {!session ? <button className="button button-primary full-width" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><LoaderCircle className="spin" size={16} />Selecting folder...</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={16} />Loading workspace...</> : <><FolderOpen size={16} />Choose folder</>}</button> : <div className="session-bar"><span title={session.workspace_root}>{workspaceRoot || session.workspace_root}</span></div>}
        {workspacePhase !== "idle" && <div className="workspace-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={15} /><span>{workspacePhase === "selecting" ? "Selecting a local folder..." : "Scanning local workspace..."}</span></div>}
        {workspaceFiles.length > 0 && <div className="workspace-tree"><div className="tree-caption">{workspaceFiles.filter((entry) => entry.kind === "file").length} files · {workspaceFiles.filter((entry) => entry.kind === "directory").length} folders</div>{workspaceFiles.filter((entry) => !entry.path.includes("/")).sort((left, right) => Number(right.kind === "directory") - Number(left.kind === "directory") || left.path.localeCompare(right.path)).slice(0, 120).map((entry) => renderTreeEntry(entry))}{workspaceFiles.length > 120 && <div className="tree-more">Showing first 120 entries</div>}</div>}
        {workspacePhase === "loading" && workspaceFiles.length === 0 && <div className="workspace-skeleton" aria-hidden="true"><span /><span /><span /><span /></div>}
        </>}</div>
        <div className="rail-section history-section"><button className="section-toggle" onClick={() => setHistoryExpanded((expanded) => !expanded)} aria-expanded={historyExpanded}><span className="section-kicker"><History size={12} />Recent tasks</span><span className="toggle-mark">{historyExpanded ? "-" : "+"}</span></button>{historyExpanded && <>{history.length === 0 ? <p className="history-empty">Completed tasks will appear here.</p> : <div className="history-list">{history.map((item) => <button className="history-item" key={`${item.timestamp}-${item.task}`} onClick={() => setTask(item.task)}><span>{item.task}</span><time>{new Date(item.timestamp).toLocaleDateString()}</time></button>)}</div>}</>}</div>
      </aside>}
      <div className={`pane-resizer ${leftCollapsed ? "is-hidden" : ""}`} onPointerDown={(event) => resizePane("left", event)} role="separator" aria-label="Resize workspace panel"><span className="resizer-handle" /></div>

      <section className="trace-workbench" aria-label="Agent conversation">
        <div className="workbench-heading"><div><div className="section-kicker">Conversation</div><h2>Agent workspace</h2><p className="run-id">{run ? `run ${run.run_id.slice(0, 8)}` : session ? "Start a task to begin" : "Choose a workspace to begin"}</p></div><div className={`status-chip status-${runStatus}`}><span className="status-chip-dot" />{session ? runStatus : "workspace required"}</div></div>
        <div className="metric-strip compact-metrics">
          <div><span className="metric-label">Duration</span><strong><Clock3 size={14} />{formatDuration(events)}</strong></div>
          <div><span className="metric-label">Iterations</span><strong>{iterationCount || "--"}</strong></div>
          <div><span className="metric-label">Tools</span><strong>{toolCallCount || "--"}</strong></div>
          <div><span className="metric-label">Failures</span><strong className={failedToolCount ? "metric-danger" : ""}>{failedToolCount || "--"}</strong></div>
          <div className="metric-context"><span className="metric-label">Context budget</span><strong>{contextStats ? `${contextStats.total_chars ?? 0} / ${contextStats.max_chars ?? 0}` : "--"}</strong><div className="budget-track"><span style={{ width: `${Math.min(100, (contextStats?.utilization ?? 0) * 100)}%` }} /></div></div>
        </div>

        <div className="conversation-scroll" ref={conversationRef} aria-live="polite">
          {userMessages.map((event) => <article className="chat-message user-message" key={event.event_id}><div className="message-avatar user-avatar"><UserRound size={16} /></div><div className="message-body"><div className="message-meta"><strong>You</strong><time>{formatTime(event.timestamp)}</time></div><p>{readPayloadString(event, "content")}</p></div></article>)}
          {thinkingEvents.length > 0 && <details className="thinking-card" open={thinkingExpanded} onToggle={(event) => setThinkingExpanded(event.currentTarget.open)}><summary><span className="thinking-summary-main"><span className="thinking-avatar"><BrainCircuit size={16} /></span><span><strong>Thinking process</strong><small>{busy ? (typeof activeTool === "string" ? `Working with ${activeTool}` : "Agent is working") : `${thinkingEvents.length} execution steps`}</small></span></span><span className="thinking-summary-state">{busy ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}</span></summary><div className="thinking-steps">{thinkingEvents.map((event) => <div className={`thinking-step tone-${eventTone(event.type)}`} key={event.event_id}><span className="thinking-step-icon">{event.type.startsWith("tool") ? <Wrench size={14} /> : iconForEvent(event.type)}</span><div className="thinking-step-body"><div className="thinking-step-title"><strong>{event.type === "tool_started" ? String(event.payload.tool_name ?? "local tool") : eventLabels[event.type] ?? event.type}</strong><time>{formatTime(event.timestamp)}</time></div><p>{event.type === "plan" || event.type === "reflection" ? readPayloadString(event, "content") : event.type === "tool_failed" || event.type === "agent_error" ? readPayloadString(event, "error") : event.type === "tool_finished" ? "Completed locally" : event.type === "tool_started" ? "Running locally" : event.type === "model_response" ? `${String(event.payload.tool_call_count ?? 0)} tool call(s) returned` : event.type === "model_request" ? `${String(event.payload.message_count ?? 0)} messages sent to model` : event.type.startsWith("context_") ? "Context updated" : event.type === "iteration_started" ? `Iteration ${event.iteration ?? "-"} started` : "Agent state updated"}</p><details className="thinking-payload"><summary>Details</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></div></div>)}</div></details>}
          {(displayedAnswer || finalAnswer) && <article className="chat-message agent-message"><div className="message-avatar agent-avatar"><Bot size={16} /></div><div className="message-body"><div className="message-meta"><strong>Agent</strong><time>{answerStreaming ? "streaming" : "final answer"}</time></div><div className="answer-copy">{displayedAnswer}<span className={`answer-cursor ${answerStreaming ? "is-visible" : ""}`} aria-hidden="true" /></div></div></article>}
          {busy && !finalAnswer && <div className="typing-row"><span className="typing-avatar"><Bot size={15} /></span><span>Agent is thinking</span><i /><i /><i /></div>}
          {!session && <div className="workspace-gate"><div className="workspace-gate-icon"><FolderOpen size={20} /></div><h3>Choose a workspace first</h3><p>Select a real local folder to enable file tools and commands.</p><button className="button button-primary" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><LoaderCircle className="spin" size={14} />Selecting folder...</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={14} />Loading workspace...</> : <><FolderOpen size={14} />Choose folder</>}</button></div>}
          {!events.length && session && <div className="empty-trace conversation-empty"><div className="empty-icon"><Terminal size={19} /></div><h3>Ready when you are</h3><p>Send a task and follow the Agent from intent to local tools to final answer.</p></div>}
          {error && <div className="error-banner"><XCircle size={15} />{error}</div>}
        </div>

        {busy && typeof activeTool === "string" && <div className="activity-footer"><LoaderCircle className="spin" size={14} />Working with <strong>{activeTool}</strong></div>}
        {contextStats?.compaction_count ? <div className="context-status">Memory compression active - {contextStats.compaction_count} compaction{contextStats.compaction_count === 1 ? "" : "s"}</div> : null}
        <div className={`conversation-composer ${!session ? "is-locked" : ""}`}><label className="field-label" htmlFor="task-upgraded">Message the agent</label><textarea id="task-upgraded" value={task} onChange={(event) => setTask(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void runTask(); }} placeholder={session ? "Ask the agent to inspect, edit, and verify your workspace..." : "Choose a workspace to enable local Agent tools..."} rows={3} disabled={!session || busy} /><div className="composer-actions"><span>{session ? "Ctrl + Enter to send" : "Workspace required"}</span><div className="task-actions"><button className="button button-primary" onClick={runTask} disabled={!session || !task.trim() || busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />}{busy ? "Running" : "Send"}</button><button className="button button-quiet" onClick={cancelRun} disabled={!busy}><CircleStop size={16} />Cancel</button></div></div></div>
      </section>

      <div className={`pane-resizer ${rightCollapsed ? "is-hidden" : ""}`} onPointerDown={(event) => resizePane("right", event)} role="separator" aria-label="Resize preview panel"><span className="resizer-handle" /></div>
      {rightCollapsed ? <aside className="file-preview-panel collapsed-pane" aria-label="File preview collapsed"><button className="collapsed-pane-button" onClick={() => setRightCollapsed(false)} title="Show preview" aria-label="Show preview"><ChevronLeft size={17} /></button></aside> : <aside className="file-preview-panel" aria-label="File preview"><div className="preview-heading"><div><div className="section-kicker">Workspace file</div><h3>{filePreview?.path ?? selectedFilePath ?? "Preview"}</h3></div><span className="panel-heading-actions"><button className="icon-button" onClick={() => setRightCollapsed(true)} title="Hide preview panel" aria-label="Hide preview panel"><ChevronRight size={15} /></button>{previewState === "unsupported" || previewState === "error" ? <FileWarning size={17} /> : <FileCode2 size={17} />}</span></div>{previewState === "loading" && <div className="inspector-empty preview-state"><LoaderCircle className="spin" size={20} /><p>Loading preview...</p><span>Reading the selected local file.</span></div>}{previewState === "unsupported" && <div className="inspector-empty preview-state preview-unsupported"><FileWarning size={22} /><h4>Preview unavailable</h4><p>{previewMessage}</p></div>}{previewState === "error" && <div className="inspector-empty preview-state preview-error"><XCircle size={22} /><h4>Could not preview this file</h4><p>{previewMessage}</p></div>}{previewState === "ready" && filePreview && <CodePreview preview={filePreview} />}{previewState === "idle" && <div className="inspector-empty"><FolderOpen size={18} /><p>Choose a folder, then click a file in the workspace tree to preview it.</p></div>}</aside>}
    </main>
    <footer className="footer-bar"><span>Agent Core stays on the backend</span><span>FastAPI · SSE · local tools</span></footer>
  </div>;
}
