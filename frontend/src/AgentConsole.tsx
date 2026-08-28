import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import {
  Bot,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleStop,
  Clipboard,
  Clock3,
  Code2,
  FileWarning,
  FileCode2,
  FolderOpen,
  History,
  LoaderCircle,
  Play,
  RotateCcw,
  Terminal,
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
type Filter = "all" | "model" | "tools" | "context" | "errors";
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
  context_truncated: "Context truncated",
  context_compacted: "Context compacted",
  agent_finished: "Agent finished",
  agent_error: "Agent error",
};
const eventTypes = Object.keys(eventLabels);
const filters: Array<{ id: Filter; label: string }> = [
  { id: "all", label: "All" },
  { id: "model", label: "Model" },
  { id: "tools", label: "Tools" },
  { id: "context", label: "Context" },
  { id: "errors", label: "Errors" },
];

function eventTone(type: string): "neutral" | "working" | "success" | "danger" {
  if (["tool_started", "model_request", "iteration_started"].includes(type)) return "working";
  if (["tool_finished", "agent_finished", "context_compacted"].includes(type)) return "success";
  if (["tool_failed", "agent_error"].includes(type)) return "danger";
  return "neutral";
}

function iconForEvent(type: string) {
  if (type.startsWith("tool") && type !== "tool_failed") return <Terminal size={15} />;
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

function eventMatchesFilter(type: string, filter: Filter) {
  if (filter === "all") return true;
  if (filter === "model") return type.startsWith("model") || type === "assistant_message";
  if (filter === "tools") return type.startsWith("tool");
  if (filter === "context") return type.startsWith("context");
  return type === "agent_error" || type === "tool_failed";
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
  const [filter, setFilter] = useState<Filter>("all");
  const [selectedEvent, setSelectedEvent] = useState<AgentEvent | null>(null);
  const [copied, setCopied] = useState(false);
  const [workspaceExpanded, setWorkspaceExpanded] = useState(true);
  const [historyExpanded, setHistoryExpanded] = useState(true);
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set());
  const [workspacePhase, setWorkspacePhase] = useState<WorkspacePhase>("idle");
  const eventSource = useRef<EventSource | null>(null);
  const refreshInFlight = useRef(false);

  useEffect(() => () => eventSource.current?.close(), []);

  const contextStats = useMemo(() => {
    const candidate = [...events].reverse().find((event) => typeof event.payload.context === "object");
    return candidate?.payload.context as ContextStats | undefined;
  }, [events]);
  const finalAnswer = useMemo(() => {
    const event = [...events].reverse().find((candidate) => candidate.type === "assistant_message" && readPayloadString(candidate, "content"));
    return event ? readPayloadString(event, "content") : undefined;
  }, [events]);
  const runStatus = useMemo(() => {
    const finished = [...events].reverse().find((event) => event.type === "agent_finished");
    return readPayloadString(finished ?? events[0] ?? { payload: {} } as AgentEvent, "status") ?? (busy ? "running" : "ready");
  }, [busy, events]);
  const groupedEvents = useMemo(() => {
    const groups = new Map<number | null, AgentEvent[]>();
    for (const event of events.filter((candidate) => eventMatchesFilter(candidate.type, filter))) {
      const key = event.iteration;
      groups.set(key, [...(groups.get(key) ?? []), event]);
    }
    return [...groups.entries()];
  }, [events, filter]);
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
    if (!session || !task.trim()) return;
    setError(null);
    setEvents([]);
    setSelectedEvent(null);
    setBusy(true);
    eventSource.current?.close();
    try {
      const response = await fetch(`${API_BASE}/sessions/${session.session_id}/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: task.trim() }) });
      if (!response.ok) throw new Error(await responseError(response, "Could not start run"));
      const started = (await response.json()) as RunResponse;
      const nextHistory = [{ task: task.trim(), timestamp: new Date().toISOString() }, ...history.filter((item) => item.task !== task.trim())].slice(0, 8);
      localStorage.setItem("lvyiyou-agent-history", JSON.stringify(nextHistory));
      setRun(started);
      const source = new EventSource(`${API_BASE}/runs/${started.run_id}/events`);
      eventSource.current = source;
      const handleEvent = (message: Event) => {
        const event = JSON.parse((message as MessageEvent<string>).data) as AgentEvent;
        setEvents((current) => [...current, event]);
        setSelectedEvent(event);
        if (["tool_finished", "tool_failed", "agent_finished"].includes(event.type)) {
          void loadWorkspaceFiles(started.session_id).catch((reason) => {
            setError(reason instanceof Error ? reason.message : "Could not refresh workspace");
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

  async function copySelectedEvent() {
    if (!selectedEvent) return;
    await navigator.clipboard.writeText(JSON.stringify(selectedEvent.payload, null, 2));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  function resetSession() {
    eventSource.current?.close();
    setSession(null);
    setRun(null);
    setEvents([]);
    setSelectedEvent(null);
    setTask("");
    setError(null);
    setBusy(false);
    setWorkspaceFiles([]);
    setExpandedFolders(new Set());
    setSelectedFilePath(null);
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
      if (firstFile && !selectedFilePath) {
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
    setSelectedFilePath(path);
    setFilePreview(null);
    setPreviewState("loading");
    setPreviewMessage("");
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/files/${path.split("/").map(encodeURIComponent).join("/")}`);
      if (!response.ok) {
        const message = await responseError(response, "Could not preview file");
        const unsupported = response.status === 415 || /binary|UTF-8|text file|not supported/i.test(message);
        setPreviewState(unsupported ? "unsupported" : "error");
        setPreviewMessage(unsupported ? "This file is not a supported UTF-8 text file." : message);
        return;
      }
      const preview = (await response.json()) as FilePreview;
      setFilePreview(preview);
      setPreviewState("ready");
    } catch (reason) {
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

  useEffect(() => {
    if (!session || !busy) return;
    const timer = window.setInterval(() => {
      void loadWorkspaceFiles(session.session_id).catch((reason) => setError(reason instanceof Error ? reason.message : "Could not refresh workspace"));
    }, 700);
    return () => window.clearInterval(timer);
  }, [busy, session]);

  return <div className="console-shell">
    <header className="console-topbar">
      <div className="brand-lockup"><div className="brand-mark"><Bot size={20} /></div><div><p className="brand-name">Local Coding Agent</p><p className="brand-caption">Self-hosted runtime console</p></div></div>
      <div className={`connection-state ${session ? "is-ready" : ""}`}><span className="state-dot" />{busy ? "Run in progress" : session ? "Session ready" : "Awaiting workspace"}</div>
    </header>

    <main className="console-layout">
      <aside className="command-rail" aria-label="Workspace and run controls">
        <div className="rail-section"><div className="rail-section-heading"><button className="section-toggle" onClick={() => setWorkspaceExpanded((expanded) => !expanded)} aria-expanded={workspaceExpanded}><span className="section-kicker">Workspace</span><span className="toggle-mark">{workspaceExpanded ? "-" : "+"}</span></button><button className="icon-button" onClick={resetSession} title="Change workspace" aria-label="Change workspace" disabled={!session}><RotateCcw size={15} /></button></div>{workspaceExpanded && <><h1>Local workspace</h1>
        {!session ? <button className="button button-primary full-width" onClick={() => void chooseDirectory()} disabled={workspacePhase !== "idle"}>{workspacePhase === "selecting" ? <><LoaderCircle className="spin" size={16} />Selecting folder...</> : workspacePhase === "loading" ? <><LoaderCircle className="spin" size={16} />Loading workspace...</> : <><FolderOpen size={16} />Choose folder</>}</button> : <div className="session-bar"><span title={session.workspace_root}>{workspaceRoot || session.workspace_root}</span></div>}
        {workspacePhase !== "idle" && <div className="workspace-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={15} /><span>{workspacePhase === "selecting" ? "Selecting a local folder..." : "Scanning local workspace..."}</span></div>}
        {workspaceFiles.length > 0 && <div className="workspace-tree"><div className="tree-caption">{workspaceFiles.filter((entry) => entry.kind === "file").length} files · {workspaceFiles.filter((entry) => entry.kind === "directory").length} folders</div>{workspaceFiles.filter((entry) => !entry.path.includes("/")).sort((left, right) => Number(right.kind === "directory") - Number(left.kind === "directory") || left.path.localeCompare(right.path)).slice(0, 120).map((entry) => renderTreeEntry(entry))}{workspaceFiles.length > 120 && <div className="tree-more">Showing first 120 entries</div>}</div>}
        {workspacePhase === "loading" && workspaceFiles.length === 0 && <div className="workspace-skeleton" aria-hidden="true"><span /><span /><span /><span /></div>}
        </>}</div>
        <div className="rail-section history-section"><button className="section-toggle" onClick={() => setHistoryExpanded((expanded) => !expanded)} aria-expanded={historyExpanded}><span className="section-kicker"><History size={12} />Recent tasks</span><span className="toggle-mark">{historyExpanded ? "-" : "+"}</span></button>{historyExpanded && <>{history.length === 0 ? <p className="history-empty">Completed tasks will appear here.</p> : <div className="history-list">{history.map((item) => <button className="history-item" key={`${item.timestamp}-${item.task}`} onClick={() => setTask(item.task)}><span>{item.task}</span><time>{new Date(item.timestamp).toLocaleDateString()}</time></button>)}</div>}</>}</div>
      </aside>

      <section className="trace-workbench" aria-label="Agent activity">
        <div className="workbench-heading"><div><div className="section-kicker">Run monitor</div><h2>Agent activity</h2><p className="run-id">{run ? `run ${run.run_id.slice(0, 8)}` : "No active run"}</p></div><div className={`status-chip status-${runStatus}`}><span className="status-chip-dot" />{runStatus}</div></div>
        <div className="metric-strip">
          <div><span className="metric-label">Duration</span><strong><Clock3 size={14} />{formatDuration(events)}</strong></div>
          <div><span className="metric-label">Iterations</span><strong>{iterationCount || "--"}</strong></div>
          <div><span className="metric-label">Tool calls</span><strong>{toolCallCount || "--"}</strong></div>
          <div><span className="metric-label">Failures</span><strong className={failedToolCount ? "metric-danger" : ""}>{failedToolCount || "--"}</strong></div>
          <div className="metric-context"><span className="metric-label">Context budget</span><strong>{contextStats ? `${contextStats.total_chars ?? 0} / ${contextStats.max_chars ?? 0}` : "--"}</strong><div className="budget-track"><span style={{ width: `${Math.min(100, (contextStats?.utilization ?? 0) * 100)}%` }} /></div></div>
        </div>
        {finalAnswer && !busy && <div className="answer-panel"><div className="answer-heading"><CheckCircle2 size={16} />Final answer</div><p>{finalAnswer}</p></div>}
        <div className="filter-bar" role="tablist" aria-label="Event filters">{filters.map((item) => <button key={item.id} className={`filter-tab ${filter === item.id ? "is-active" : ""}`} onClick={() => setFilter(item.id)} role="tab" aria-selected={filter === item.id}>{item.label}<span>{item.id === "all" ? events.length : events.filter((event) => eventMatchesFilter(event.type, item.id)).length}</span></button>)}</div>
        <div className="trace-grid">
          <div className="iteration-list">{groupedEvents.length === 0 ? <div className="empty-trace"><div className="empty-icon"><Terminal size={19} /></div><h3>{events.length ? "No matching events" : "Ready when you are"}</h3><p>{events.length ? "Change the filter to inspect another part of the run." : "Open a workspace and run a task to see the model-to-tool loop here."}</p></div> : groupedEvents.map(([iteration, group]) => <section className="iteration-block" key={iteration ?? "session"}><div className="iteration-heading"><span>{iteration === null ? "Session" : `Iteration ${iteration}`}</span><span>{group.length} events</span></div>{group.map((event) => <button className={`event-row tone-${eventTone(event.type)} ${selectedEvent?.event_id === event.event_id ? "is-selected" : ""}`} key={event.event_id} onClick={() => setSelectedEvent(event)}><span className="event-icon">{iconForEvent(event.type)}</span><span className="event-main"><span className="event-title"><strong>{eventLabels[event.type] ?? event.type}</strong><time>{formatTime(event.timestamp)}</time></span>{event.type.startsWith("tool_") && <span className="tool-name">{String(event.payload.tool_name ?? "local tool")}</span>}{event.type === "assistant_message" && readPayloadString(event, "content") && <span className="event-preview">{readPayloadString(event, "content")}</span>}{(event.type === "tool_failed" || event.type === "agent_error") && readPayloadString(event, "error") && <span className="event-preview">{readPayloadString(event, "error")}</span>}</span></button>)}</section>)}</div>
          <aside className="event-inspector"><div className="inspector-heading"><div><div className="section-kicker">Inspector</div><h3>{selectedEvent ? eventLabels[selectedEvent.type] ?? selectedEvent.type : "Select an event"}</h3></div>{selectedEvent && <button className="icon-button" onClick={copySelectedEvent} title="Copy event payload" aria-label="Copy event payload">{copied ? <Check size={15} /> : <Clipboard size={15} />}</button>}</div>{selectedEvent ? <><div className="inspector-meta"><span>{formatTime(selectedEvent.timestamp)}</span><span>{selectedEvent.iteration === null ? "session" : `iteration ${selectedEvent.iteration}`}</span></div><pre>{JSON.stringify(selectedEvent.payload, null, 2)}</pre></> : <div className="inspector-empty"><Code2 size={18} /><p>Event payloads and execution details appear here.</p></div>}</aside>
        </div>
        {busy && typeof activeTool === "string" && <div className="activity-footer"><LoaderCircle className="spin" size={14} />Working with <strong>{activeTool}</strong></div>}
        {contextStats?.compaction_count ? <div className="context-status">Memory compression active - {contextStats.compaction_count} compaction{contextStats.compaction_count === 1 ? "" : "s"}</div> : null}
        <div className="conversation-composer"><label className="field-label" htmlFor="task-upgraded">Message the agent</label><textarea id="task-upgraded" value={task} onChange={(event) => setTask(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void runTask(); }} placeholder="Ask the agent to inspect, edit, and verify your workspace..." rows={3} disabled={!session || busy} /><div className="composer-actions"><span>Ctrl + Enter to run</span><div className="task-actions"><button className="button button-primary" onClick={runTask} disabled={!session || !task.trim() || busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}{busy ? "Running" : "Run task"}</button><button className="button button-quiet" onClick={cancelRun} disabled={!busy}><CircleStop size={16} />Cancel</button></div></div>{error && <div className="error-banner"><XCircle size={15} />{error}</div>}</div>
      </section>
      <aside className="file-preview-panel" aria-label="File preview"><div className="preview-heading"><div><div className="section-kicker">Workspace file</div><h3>{filePreview?.path ?? selectedFilePath ?? "Preview"}</h3></div>{previewState === "unsupported" || previewState === "error" ? <FileWarning size={17} /> : <FileCode2 size={17} />}</div>{previewState === "loading" && <div className="inspector-empty preview-state"><LoaderCircle className="spin" size={20} /><p>Loading preview...</p><span>Reading the selected local file.</span></div>}{previewState === "unsupported" && <div className="inspector-empty preview-state preview-unsupported"><FileWarning size={22} /><h4>Preview unavailable</h4><p>{previewMessage}</p></div>}{previewState === "error" && <div className="inspector-empty preview-state preview-error"><XCircle size={22} /><h4>Could not preview this file</h4><p>{previewMessage}</p></div>}{previewState === "ready" && filePreview && <CodePreview preview={filePreview} />}{previewState === "idle" && <div className="inspector-empty"><FolderOpen size={18} /><p>Choose a folder, then click a file in the workspace tree to preview it.</p></div>}</aside>
    </main>
    <footer className="footer-bar"><span>Agent Core stays on the backend</span><span>FastAPI · SSE · local tools</span></footer>
  </div>;
}
