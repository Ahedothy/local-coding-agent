"""Best-effort JSONL persistence for observable Agent events."""

from __future__ import annotations

import re
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event import AgentEvent


_RUN_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


class JsonlEventLogger:
    """Append each event as one JSON object without affecting Agent execution."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, event: AgentEvent) -> bool:
        """Write one event and return False when logging fails."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(event.model_dump_json())
                stream.write("\n")
        except Exception:
            # Observability must never turn a successful Agent step into a failure.
            return False
        return True

    def __call__(self, event: AgentEvent) -> None:
        """Allow the logger to be used directly as an Agent event handler."""
        self.write(event)


class JsonlRunStore:
    """Persist one append-only JSONL event stream per API run.

    The store is deliberately read/write-only: loading a run only validates
    recorded events and never restores an Agent or executes a tool.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()

    def _path_for(self, run_id: str) -> Path:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid run id")
        return self.directory / f"{run_id}.jsonl"

    def append(self, run_id: str, event: AgentEvent) -> bool:
        """Append one event, returning False when persistence is unavailable."""
        try:
            path = self._path_for(run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(event.model_dump_json())
                stream.write("\n")
        except Exception:
            # History must never turn a valid Agent step into a failed run.
            return False
        return True

    def list_run_ids(self) -> list[str]:
        """Return persisted run ids newest first, ignoring unrelated files."""
        try:
            paths = [
                path
                for path in self.directory.glob("*.jsonl")
                if _RUN_ID_PATTERN.fullmatch(path.stem)
            ]
        except OSError:
            return []
        return [path.stem for path in sorted(paths, key=_modified_time, reverse=True)]

    def read(self, run_id: str) -> list[AgentEvent]:
        """Read and validate one persisted event stream."""
        path = self._path_for(run_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read run history {run_id}: {exc}") from exc

        events: list[AgentEvent] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                events.append(AgentEvent.model_validate_json(line))
            except Exception as exc:
                raise ValueError(
                    f"invalid AgentEvent in run history {run_id} at line {line_number}: {exc}"
                ) from exc
        return events


class SqliteRunStore:
    """Persist API runs, events, and resumable Agent session state locally.

    SQLite is used for Web API history because it supports indexed run lookup
    and atomic session snapshots. Replay still reads only recorded data; the
    store never constructs an Agent or executes a tool.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        legacy_jsonl_directory: str | Path | None = None,
        initialize: bool = True,
    ) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._initialized = False
        self._legacy_jsonl_directory = (
            Path(legacy_jsonl_directory).expanduser()
            if legacy_jsonl_directory is not None
            else None
        )
        if initialize:
            self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._initialize()
        self._initialized = True
        if self._legacy_jsonl_directory is not None:
            self._migrate_legacy_jsonl(self._legacy_jsonl_directory)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        workspace_root TEXT,
                        task TEXT,
                        status TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        run_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY (run_id, sequence),
                        FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        workspace_root TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT 'New conversation',
                        title_status TEXT NOT NULL DEFAULT 'pending',
                        session_json TEXT NOT NULL,
                        context_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS run_states (
                        run_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        workspace_root TEXT NOT NULL,
                        session_json TEXT NOT NULL,
                        context_json TEXT NOT NULL,
                        runtime_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_recent
                        ON runs (COALESCE(finished_at, started_at, created_at) DESC);
                    """
                )
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                }
                if "title" not in columns:
                    connection.execute(
                        "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT 'New conversation'"
                    )
                if "title_status" not in columns:
                    connection.execute(
                        "ALTER TABLE sessions ADD COLUMN title_status TEXT NOT NULL DEFAULT 'pending'"
                    )
        except sqlite3.Error as exc:
            raise ValueError(f"could not initialize history database: {exc}") from exc

    def _path_for(self, run_id: str) -> None:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid run id")

    def append(self, run_id: str, event: AgentEvent) -> bool:
        """Append an event and update the lightweight run index."""
        try:
            self._path_for(run_id)
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                self._append_locked(connection, run_id, event)
        except Exception:
            # History must never turn a valid Agent step into a failed run.
            return False
        return True

    def _append_locked(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event: AgentEvent,
    ) -> None:
        event_json = event.model_dump_json()
        existing = connection.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO runs(run_id, session_id, started_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    run_id,
                    event.session_id,
                    event.timestamp.isoformat(),
                    _now_iso(),
                ),
            )
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO events(run_id, sequence, event_json) VALUES (?, ?, ?)",
            (run_id, sequence, event_json),
        )
        payload = event.payload
        if event.type.value == "session_started":
            connection.execute(
                "UPDATE runs SET workspace_root = ? WHERE run_id = ?",
                (str(payload.get("workspace_root") or ""), run_id),
            )
        elif event.type.value == "user_message":
            connection.execute(
                "UPDATE runs SET task = ? WHERE run_id = ?",
                (str(payload.get("content") or ""), run_id),
            )
        elif event.type.value == "agent_error":
            connection.execute(
                "UPDATE runs SET status = ?, error = ? WHERE run_id = ?",
                ("failed", str(payload.get("error") or ""), run_id),
            )
        elif event.type.value == "agent_finished":
            connection.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    str(payload.get("status") or "completed"),
                    payload.get("error"),
                    event.timestamp.isoformat(),
                    run_id,
                ),
            )

    def save_session(self, session: Any, context_manager: Any) -> bool:
        """Save enough local state to start a later turn in the same session."""
        try:
            self._ensure_initialized()
            session_json = json.dumps(
                session.model_dump(mode="json"), ensure_ascii=False
            )
            context_json = json.dumps(
                context_manager.to_state(), ensure_ascii=False
            )
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions(
                        session_id, workspace_root, session_json, context_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        workspace_root = excluded.workspace_root,
                        session_json = excluded.session_json,
                        context_json = excluded.context_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session.session_id,
                        str(session.workspace_root),
                        session_json,
                        context_json,
                        _now_iso(),
                    ),
                )
        except Exception:
            return False
        return True

    def save_run_state(
        self,
        run_id: str,
        session: Any,
        context_manager: Any,
        runtime_state: dict[str, Any] | None = None,
    ) -> bool:
        """Save the exact resumable state reached by one recorded run."""
        try:
            self._path_for(run_id)
            self._ensure_initialized()
            session_json = json.dumps(
                session.model_dump(mode="json"), ensure_ascii=False
            )
            context_json = json.dumps(
                context_manager.to_state(), ensure_ascii=False
            )
            runtime_json = json.dumps(runtime_state or {}, ensure_ascii=False)
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO run_states(
                        run_id, session_id, workspace_root, session_json,
                        context_json, runtime_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        session_id = excluded.session_id,
                        workspace_root = excluded.workspace_root,
                        session_json = excluded.session_json,
                        context_json = excluded.context_json,
                        runtime_json = excluded.runtime_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        session.session_id,
                        str(session.workspace_root),
                        session_json,
                        context_json,
                        runtime_json,
                        _now_iso(),
                    ),
                )
        except Exception:
            return False
        return True

    def set_session_title_if_default(self, session_id: str, title: str) -> bool:
        """Set a session title once; later turns cannot rename it."""
        clean_title = " ".join(title.split()).strip()
        if not clean_title:
            return False
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET title = ?, title_status = 'ready'
                    WHERE session_id = ?
                      AND title_status IN ('pending', 'generating')
                    """,
                    (clean_title, session_id),
                )
                return cursor.rowcount > 0
        except Exception:
            return False

    def begin_session_title(self, session_id: str) -> bool:
        """Claim the one title-generation attempt for a session."""
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET title_status = 'generating'
                    WHERE session_id = ? AND title_status = 'pending'
                    """,
                    (session_id,),
                )
                return cursor.rowcount > 0
        except Exception:
            return False

    def set_generated_session_title(self, session_id: str, title: str) -> bool:
        """Store a generated title while this session is still unclaimed."""
        clean_title = " ".join(title.split()).strip()
        if not clean_title or clean_title == "New conversation":
            return False
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET title = ?, title_status = 'ready'
                    WHERE session_id = ? AND title_status = 'generating'
                    """,
                    (clean_title, session_id),
                )
                return cursor.rowcount > 0
        except Exception:
            return False

    def set_session_title_fallback(self, session_id: str, title: str) -> bool:
        """Store a temporary local title while an async summary is pending."""
        clean_title = " ".join(title.split()).strip()
        if not clean_title:
            return False
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET title = ?
                    WHERE session_id = ? AND title_status = 'generating'
                    """,
                    (clean_title, session_id),
                )
                return cursor.rowcount > 0
        except Exception:
            return False

    def get_session_title_status(self, session_id: str) -> str | None:
        """Return the title lifecycle state for a session."""
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT title_status FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return str(row["title_status"])

    def get_session_title(self, session_id: str) -> str | None:
        """Return the persisted display title for a session."""
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT title FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        value = row["title"]
        return str(value) if value else None

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT session_json, context_json FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ValueError(f"could not read saved session: {exc}") from exc
        if row is None:
            return None
        try:
            return {
                "session": json.loads(row["session_json"]),
                "context": json.loads(row["context_json"]),
            }
        except json.JSONDecodeError as exc:
            raise ValueError("saved session state is invalid JSON") from exc

    def load_run_state(self, run_id: str) -> dict[str, Any] | None:
        """Load the snapshot captured at the end of a particular run."""
        self._path_for(run_id)
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT session_json, context_json, runtime_json
                    FROM run_states WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ValueError(f"could not read saved run state: {exc}") from exc
        if row is None:
            return None
        try:
            return {
                "session": json.loads(row["session_json"]),
                "context": json.loads(row["context_json"]),
                "runtime": json.loads(row["runtime_json"]),
            }
        except json.JSONDecodeError as exc:
            raise ValueError("saved run state is invalid JSON") from exc

    def list_run_ids(self) -> list[str]:
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id FROM runs
                    ORDER BY COALESCE(finished_at, started_at, created_at) DESC
                    """
                ).fetchall()
        except Exception:
            return []
        return [str(row["run_id"]) for row in rows]

    def list_run_ids_for_session(self, session_id: str) -> list[str]:
        """Return one session's run ids without scanning unrelated history."""
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id FROM runs
                    WHERE session_id = ?
                    ORDER BY COALESCE(finished_at, started_at, created_at) DESC
                    """,
                    (session_id,),
                ).fetchall()
        except Exception:
            return []
        return [str(row["run_id"]) for row in rows]

    def read(self, run_id: str) -> list[AgentEvent]:
        self._path_for(run_id)
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    "SELECT event_json FROM events WHERE run_id = ? ORDER BY sequence",
                    (run_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"could not read run history {run_id}: {exc}") from exc
        events: list[AgentEvent] = []
        for sequence, row in enumerate(rows, start=1):
            try:
                events.append(AgentEvent.model_validate_json(row["event_json"]))
            except Exception as exc:
                raise ValueError(
                    f"invalid AgentEvent in run history {run_id} at sequence {sequence}: {exc}"
                ) from exc
        return events

    def read_session(self, session_id: str) -> list[AgentEvent]:
        """Read all events for one session in run and event order."""
        try:
            self._ensure_initialized()
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT events.event_json
                    FROM events
                    INNER JOIN runs ON runs.run_id = events.run_id
                    WHERE runs.session_id = ?
                    ORDER BY events.sequence, events.rowid
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"could not read session history {session_id}: {exc}") from exc
        result: list[AgentEvent] = []
        for sequence, row in enumerate(rows, start=1):
            try:
                result.append(AgentEvent.model_validate_json(row["event_json"]))
            except Exception as exc:
                raise ValueError(
                    f"invalid AgentEvent in session history {session_id} at sequence {sequence}: {exc}"
                ) from exc
        return result

    def _migrate_legacy_jsonl(self, directory: Path) -> None:
        if not directory.exists() or not directory.is_dir():
            return
        for path in directory.glob("*.jsonl"):
            if not _RUN_ID_PATTERN.fullmatch(path.stem):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                events = [
                    AgentEvent.model_validate_json(line)
                    for line in lines
                    if line.strip()
                ]
                with self._lock, self._connect() as connection:
                    existing = connection.execute(
                        "SELECT 1 FROM runs WHERE run_id = ?", (path.stem,)
                    ).fetchone()
                    if existing is None:
                        for event in events:
                            self._append_locked(connection, path.stem, event)
            except Exception:
                continue


def _modified_time(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
