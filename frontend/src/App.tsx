import { useEffect, useMemo, useRef, useState } from "react";
import { Bot, CheckCircle2, CircleStop, FileCode2, FolderOpen, LoaderCircle, Play, RotateCcw, Terminal, XCircle } from "lucide-react";

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
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";

const eventLabels: Record<string, string> = {
  session_started: "Session started", user_message: "User message", iteration_started: "Iteration",
  model_request: "Model request", model_response: "Model response", tool_started: "Tool started",
  tool_finished: "Tool finished", tool_failed: "Tool failed", assistant_message: "Assistant message",
  context_truncated: "Context truncated", context_compacted: "Context compacted", agent_finished: "Agent finished", agent_error: "Agent error",
};
const eventTypes = Object.keys(eventLabels);

function eventTone(type: string): "neutral" | "working" | "success" | "danger" {
  if (["tool_started", "model_request", "iteration_started"].includes(type)) return "working";
  if (["tool_finished", "agent_finished", "context_compacted"].includes(type)) return "success";
  if (["tool_failed", "agent_error"].includes(type)) return "danger";
  return "neutral";
}

function iconForEvent(type: string) {
  if (type.startsWith("tool") && type !== "tool_failed") return <Terminal size={16} />;
  if (type === "tool_failed" || type === "agent_error") return <XCircle size={16} />;
  if (type === "agent_finished" || type === "tool_finished") return <CheckCircle2 size={16} />;
  if (type.startsWith("model") || type === "assistant_message") return <Bot size={16} />;
  return <FileCode2 size={16} />;
}

function formatTime(timestamp: string) {
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function App() {
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [task, setTask] = useState("");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [run, setRun] = useState<RunResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eventSource = useRef<EventSource | null>(null);

  useEffect(() => () => eventSource.current?.close(), []);

  const latestEvent = events.at(-1);
  const contextStats = useMemo(() => {
    const candidate = [...events].reverse().find((event) => event.payload.context && typeof event.payload.context === "object");
    return candidate?.payload.context as { total_chars?: number; max_chars?: number; utilization?: number; compaction_count?: number } | undefined;
  }, [events]);
  const activeTool = useMemo(() => events.findLast((event) => event.type === "tool_started")?.payload.tool_name, [events]);
  const finalAnswer = useMemo(() => {
    const event = [...events].reverse().find(
      (candidate) => candidate.type === "assistant_message" && typeof candidate.payload.content === "string" && candidate.payload.content.length > 0,
    );
    return event ? String(event.payload.content) : undefined;
  }, [events]);

  async function responseError(response: Response, fallback: string) {
    try {
      const body = (await response.json()) as { detail?: string };
      return body.detail ?? fallback;
    } catch { return fallback; }
  }

  async function createSession() {
    setError(null); setEvents([]); setRun(null);
    try {
      const response = await fetch(`${API_BASE}/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_root: workspaceRoot }) });
      if (!response.ok) throw new Error(await responseError(response, "Could not create session"));
      setSession((await response.json()) as SessionResponse);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not create session"); }
  }

  async function runTask() {
    if (!session || !task.trim()) return;
    setError(null); setEvents([]); setBusy(true); eventSource.current?.close();
    try {
      const response = await fetch(`${API_BASE}/sessions/${session.session_id}/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task: task.trim() }) });
      if (!response.ok) throw new Error(await responseError(response, "Could not start run"));
      const started = (await response.json()) as RunResponse;
      setRun(started);
      const source = new EventSource(`${API_BASE}/runs/${started.run_id}/events`);
      eventSource.current = source;
      const handleEvent = (message: Event) => {
        const event = JSON.parse((message as MessageEvent<string>).data) as AgentEvent;
        setEvents((current) => [...current, event]);
        if (event.type === "agent_finished" || event.type === "agent_error") { setBusy(false); source.close(); }
      };
      eventTypes.forEach((eventType) => source.addEventListener(eventType, handleEvent));
      source.onerror = () => { setBusy(false); source.close(); setError("The event stream closed unexpectedly."); };
    } catch (reason) { setBusy(false); setError(reason instanceof Error ? reason.message : "Could not start run"); }
  }

  async function cancelRun() {
    if (!run) return;
    try { await fetch(`${API_BASE}/runs/${run.run_id}/cancel`, { method: "POST" }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not cancel run"); }
  }

  function resetSession() {
    eventSource.current?.close(); setSession(null); setRun(null); setEvents([]); setTask(""); setError(null); setBusy(false);
  }

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark"><Bot size={21} /></div><div><p className="brand-name">Local Coding Agent</p><p className="brand-caption">Self-hosted runtime console</p></div></div>
      <div className={`connection-state ${session ? "is-ready" : ""}`}><span className="state-dot" />{session ? "Session ready" : "Awaiting workspace"}</div>
    </header>

    <main className="workspace-layout">
      <section className="control-panel" aria-label="Run controls">
        <div className="section-kicker">Workspace</div><h1>Give the agent a real codebase.</h1>
        <p className="intro-copy">Point the runtime at a local folder, then watch every model decision and tool result arrive live.</p>
        <label className="field-label" htmlFor="workspace">Workspace root</label>
        <div className="input-with-icon"><FolderOpen size={17} /><input id="workspace" value={workspaceRoot} onChange={(event) => setWorkspaceRoot(event.target.value)} placeholder="C:/projects/my-repo" disabled={Boolean(session)} /></div>
        {!session ? <button className="button button-primary full-width" onClick={createSession} disabled={!workspaceRoot.trim()}><FolderOpen size={17} />Open workspace</button> : <div className="session-bar"><span title={session.workspace_root}>{session.workspace_root}</span><button className="icon-button" onClick={resetSession} title="Change workspace" aria-label="Change workspace"><RotateCcw size={16} /></button></div>}
        <div className="divider" /><div className="section-kicker">Task</div>
        <label className="field-label" htmlFor="task">Instruction</label>
        <textarea id="task" value={task} onChange={(event) => setTask(event.target.value)} placeholder="Fix the failing tests, make the smallest change, and verify the result." rows={7} disabled={!session || busy} />
        <div className="task-actions"><button className="button button-primary" onClick={runTask} disabled={!session || !task.trim() || busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <Play size={17} />}{busy ? "Running" : "Run task"}</button><button className="button button-quiet" onClick={cancelRun} disabled={!busy}><CircleStop size={17} />Cancel</button></div>
        {error && <div className="error-banner"><XCircle size={16} />{error}</div>}
        <div className="control-note"><span className="note-line" /><p>Tools execute on the configured machine and remain bounded by the workspace policy.</p></div>
      </section>

      <section className="activity-panel" aria-label="Agent activity">
        <div className="activity-header"><div><div className="section-kicker">Live trace</div><h2>Agent activity</h2></div><div className="trace-meta"><span>{events.length} events</span>{contextStats && <span title="Current model context usage">Context {contextStats.total_chars ?? 0}/{contextStats.max_chars ?? 0}</span>}{busy && <span className="live-label"><span className="live-dot" />Live</span>}</div></div>
        {finalAnswer && !busy && <div className="answer-panel"><div className="answer-heading"><CheckCircle2 size={17} />Final answer</div><p>{finalAnswer}</p></div>}
        <div className="trace-list">{events.length === 0 ? <div className="empty-trace"><div className="empty-icon"><Terminal size={20} /></div><h3>Nothing running yet</h3><p>Open a workspace and run a task to see the model-to-tool loop here.</p></div> : events.map((event) => <article className={`trace-row tone-${eventTone(event.type)}`} key={event.event_id}><div className="trace-icon">{iconForEvent(event.type)}</div><div className="trace-content"><div className="trace-title-line"><strong>{eventLabels[event.type] ?? event.type}</strong><time>{formatTime(event.timestamp)}</time></div>{event.type.startsWith("tool_") && <span className="tool-name">{String(event.payload.tool_name ?? "local tool")}</span>}{event.type === "assistant_message" && typeof event.payload.content === "string" && event.payload.content && <p className="trace-message">{event.payload.content}</p>}{(event.type === "tool_failed" || event.type === "agent_error") && typeof event.payload.error === "string" && event.payload.error && <p className="trace-message">{event.payload.error}</p>}{["model_request", "model_response", "agent_finished"].includes(event.type) && <details><summary>Inspect payload</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</div></article>)}</div>
        {latestEvent && busy && typeof activeTool === "string" && <div className="activity-footer"><LoaderCircle className="spin" size={14} />Working with <strong>{activeTool}</strong></div>}
        {contextStats && contextStats.compaction_count ? <div className="context-status">Memory compression active - {contextStats.compaction_count} compaction{contextStats.compaction_count === 1 ? "" : "s"}</div> : null}
      </section>
    </main>
    <footer className="footer-bar"><span>Agent Core stays on the backend</span><span>FastAPI · SSE · local tools</span></footer>
  </div>;
}

export default App;
