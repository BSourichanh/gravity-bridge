"""Couche de persistance SQLite WAL et réconciliation des jobs pour Gravity Bridge."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from core.security import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_BODY_BYTES,
    QUEUED_GRACE_SECONDS,
    SLUG_MAX_CHARS,
    DispatchBlockedError,
    NotFoundError,
    ValidationError,
    assert_mode_approval,
    canonical_path_key,
    derive_slug_candidates,
    is_explicit_workspace_path,
    normalize_workspace_path,
    redact_secrets,
    truncate_utf8_bytes,
    validate_body,
    validate_effort,
    validate_file_refs,
    validate_int_range,
    validate_mode,
    validate_msg_type,
    validate_slug,
    validate_subject,
    validate_timeout_seconds,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    slug           TEXT PRIMARY KEY,
    canonical_path TEXT UNIQUE NOT NULL,
    display_name   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
    id                     TEXT PRIMARY KEY,
    from_workspace         TEXT NOT NULL REFERENCES workspaces(slug),
    to_workspace           TEXT NOT NULL REFERENCES workspaces(slug),
    msg_type               TEXT NOT NULL CHECK (msg_type IN ('question', 'handoff', 'info')),
    subject                TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('open', 'done')) DEFAULT 'open',
    approved               INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
    target_conversation_id TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id        TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    sender_workspace TEXT NOT NULL REFERENCES workspaces(slug),
    msg_type         TEXT NOT NULL CHECK (msg_type IN ('question', 'handoff', 'info', 'reply')),
    body             TEXT NOT NULL,
    file_refs_json   TEXT NOT NULL DEFAULT '[]',
    is_read          INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                     TEXT PRIMARY KEY,
    thread_id              TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    message_id             INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    target_workspace       TEXT NOT NULL REFERENCES workspaces(slug),
    mode                   TEXT NOT NULL CHECK (mode IN ('plan', 'accept-edits')),
    timeout_seconds        INTEGER NOT NULL DEFAULT 300 CHECK (timeout_seconds BETWEEN 30 AND 900),
    status                 TEXT NOT NULL CHECK (
                               status IN ('queued', 'running', 'completed', 'failed', 'timed_out', 'cancelled')
                           ),
    pid                    INTEGER,
    child_pid              INTEGER,
    target_conversation_id TEXT,
    model                  TEXT,
    effort                 TEXT,
    exit_code              INTEGER,
    error_summary          TEXT,
    duration_ms            INTEGER,
    started_at             TEXT,
    finished_at            TEXT,
    created_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_to_status   ON threads(to_workspace, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_from_status ON threads(from_workspace, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread     ON messages(thread_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_thread_status  ON jobs(thread_id, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_per_thread
    ON jobs(thread_id) WHERE status IN ('queued', 'running');
"""

ORPHAN_ERROR_SUMMARY = "Processus worker interrompu ou orphelin"
_MAX_ERROR_SUMMARY_BYTES = 4_096
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_FINAL_JOB_STATUSES = ("completed", "failed", "timed_out", "cancelled")

_KERNEL32 = None


def _get_kernel32():
    """Initialise et met en cache kernel32 avec signatures ctypes 64-bit sûres."""
    global _KERNEL32
    if _KERNEL32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = k32
    return _KERNEL32


def resolve_bridge_home(home: Optional[Path] = None) -> Path:
    """Résout le dossier de stockage centralisé ($GRAVITY_BRIDGE_HOME ou ~/.gemini/gravity-bridge)."""
    if home is not None:
        return Path(home).expanduser().resolve()
    env_home = os.environ.get("GRAVITY_BRIDGE_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".gemini" / "gravity-bridge").resolve()


def utc_now_iso(now: Optional[datetime] = None) -> str:
    """Formate un horodatage UTC ISO 8601 (YYYY-MM-DDTHH:MM:SSZ)."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(ts: str) -> datetime:
    """Parse un horodatage ISO 8601 UTC (compatible Python 3.10+)."""
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def generate_id(prefix: str, now: Optional[datetime] = None) -> str:
    """Génère un identifiant lisible et triable (<prefix>-YYYYMMDD-<hex6>)."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return f"{prefix}-{dt.strftime('%Y%m%d')}-{secrets.token_hex(3)}"


def is_pid_alive(pid: int) -> bool:
    """Vérifie de façon cross-platform si un processus PID est encore actif."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        k32 = _get_kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD(0)
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return int(exit_code.value) == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _row_to_workspace(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "slug": row["slug"],
        "canonical_path": row["canonical_path"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
    }


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "message_id": row["message_id"],
        "target_workspace": row["target_workspace"],
        "mode": row["mode"],
        "timeout_seconds": row["timeout_seconds"],
        "status": row["status"],
        "pid": row["pid"],
        "child_pid": row["child_pid"],
        "target_conversation_id": row["target_conversation_id"],
        "model": row["model"],
        "effort": row["effort"],
        "exit_code": row["exit_code"],
        "error_summary": row["error_summary"],
        "duration_ms": row["duration_ms"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


class BridgeStore:
    """Gestionnaire SQLite WAL centralisé pour les workspaces, fils, messages et jobs."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home_dir = resolve_bridge_home(home)
        self.db_path = self.home_dir / "bridge.db"
        self.logs_dir = self.home_dir / "logs"
        self.init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.home_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            # busy_timeout doit précéder journal_mode=WAL pour absorber toute contention initiale.
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(
        self,
        conn: sqlite3.Connection,
        immediate: bool = True,
    ) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")

    def init_db(self) -> None:
        """Initialise le dossier de stockage, le schéma DDL et les index."""
        self.home_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                for stmt in SCHEMA_SQL.strip().split(";"):
                    cleaned = stmt.strip()
                    if cleaned:
                        conn.execute(cleaned)

    def _find_workspace_row_by_path(
        self,
        conn: sqlite3.Connection,
        canonical_path: str,
    ) -> Optional[sqlite3.Row]:
        target_key = canonical_path_key(canonical_path)
        rows = conn.execute(
            "SELECT slug, canonical_path, display_name, created_at, last_seen_at FROM workspaces"
        ).fetchall()
        for row in rows:
            if canonical_path_key(row["canonical_path"]) == target_key:
                return row
        return None

    def _allocate_slug(
        self,
        canonical_path: str,
        requested_slug: Optional[str],
        taken_slugs: set[str],
    ) -> str:
        if requested_slug is not None:
            if requested_slug in taken_slugs:
                raise ValidationError(
                    f"Le slug '{requested_slug}' est déjà utilisé par un autre workspace."
                )
            return requested_slug

        base_slug, parent_slug = derive_slug_candidates(canonical_path)
        if base_slug not in taken_slugs:
            return base_slug
        if parent_slug not in taken_slugs:
            return parent_slug

        counter = 2
        while True:
            suffix = f"-{counter}"
            prefix_budget = max(1, SLUG_MAX_CHARS - len(suffix))
            prefix = base_slug[:prefix_budget].rstrip("-_") or "ws"
            candidate = f"{prefix}{suffix}"
            if candidate not in taken_slugs:
                return candidate
            counter += 1

    def register_workspace(
        self,
        path: str,
        slug: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enregistre un workspace local ou met à jour last_seen_at s'il existe déjà."""
        canonical = normalize_workspace_path(path)
        requested_slug = validate_slug(slug) if slug is not None else None
        now_iso = utc_now_iso()

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                existing = self._find_workspace_row_by_path(conn, canonical)
                if existing is not None:
                    if requested_slug is not None and requested_slug != existing["slug"]:
                        raise ValidationError(
                            f"Le chemin {canonical!r} est déjà enregistré sous le slug {existing['slug']!r}."
                        )
                    conn.execute(
                        "UPDATE workspaces SET last_seen_at = ? WHERE slug = ?",
                        (now_iso, existing["slug"]),
                    )
                    res = _row_to_workspace(existing)
                    res["last_seen_at"] = now_iso
                    res["created"] = False
                    return res

                taken = {
                    row["slug"]
                    for row in conn.execute("SELECT slug FROM workspaces").fetchall()
                }
                chosen_slug = self._allocate_slug(canonical, requested_slug, taken)
                resolved_display = (
                    display_name.strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else (Path(canonical).name or chosen_slug)
                )
                conn.execute(
                    """
                    INSERT INTO workspaces (slug, canonical_path, display_name, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chosen_slug, canonical, resolved_display, now_iso, now_iso),
                )

        return {
            "slug": chosen_slug,
            "canonical_path": canonical,
            "display_name": resolved_display,
            "created_at": now_iso,
            "last_seen_at": now_iso,
            "created": True,
        }

    def get_workspace(self, ref: str) -> Dict[str, Any]:
        """Résout un workspace enregistré par slug strict ou par chemin explicite."""
        if not isinstance(ref, str) or not ref.strip():
            raise ValidationError("La référence de workspace ne peut pas être vide.")
        cleaned = ref.strip()

        with self._connect() as conn:
            if is_explicit_workspace_path(cleaned):
                canonical = normalize_workspace_path(cleaned)
                row = self._find_workspace_row_by_path(conn, canonical)
            else:
                row = conn.execute(
                    "SELECT slug, canonical_path, display_name, created_at, last_seen_at "
                    "FROM workspaces WHERE slug = ?",
                    (cleaned,),
                ).fetchone()

        if row is None:
            raise NotFoundError(f"Workspace introuvable : {cleaned!r}.")
        return _row_to_workspace(row)

    def resolve_workspace(
        self,
        ref: str,
        *,
        allow_auto_register: bool = True,
    ) -> Dict[str, Any]:
        """Résout un slug existant ou enregistre à la volée uniquement un chemin explicite."""
        if not isinstance(ref, str) or not ref.strip():
            raise ValidationError("La référence de workspace ne peut pas être vide.")
        cleaned = ref.strip()
        if is_explicit_workspace_path(cleaned):
            try:
                return self.get_workspace(cleaned)
            except NotFoundError:
                if allow_auto_register:
                    return self.register_workspace(cleaned)
                raise
        return self.get_workspace(cleaned)

    def list_workspaces(self, *, reconcile: bool = True) -> List[Dict[str, Any]]:
        """Liste tous les workspaces triés par slug avec leurs compteurs d'activité."""
        if reconcile:
            self.reconcile_orphan_jobs()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    w.slug,
                    w.canonical_path,
                    w.display_name,
                    w.created_at,
                    w.last_seen_at,
                    (
                        SELECT COUNT(*)
                        FROM threads t
                        WHERE t.to_workspace = w.slug AND t.status = 'open'
                    ) AS open_inbox_count,
                    (
                        SELECT COUNT(*)
                        FROM messages m
                        JOIN threads t ON t.id = m.thread_id
                        WHERE (t.to_workspace = w.slug OR t.from_workspace = w.slug)
                          AND m.is_read = 0
                          AND m.sender_workspace != w.slug
                    ) AS unread_count,
                    (
                        SELECT COUNT(*)
                        FROM jobs j
                        WHERE j.target_workspace = w.slug
                          AND j.status IN ('queued', 'running')
                    ) AS active_jobs_count
                FROM workspaces w
                ORDER BY w.slug ASC
                """
            ).fetchall()

        return [
            {
                "slug": row["slug"],
                "canonical_path": row["canonical_path"],
                "display_name": row["display_name"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "open_inbox_count": int(row["open_inbox_count"]),
                "unread_count": int(row["unread_count"]),
                "active_jobs_count": int(row["active_jobs_count"]),
            }
            for row in rows
        ]

    def create_thread(
        self,
        from_workspace: str,
        to_workspace: str,
        msg_type: str,
        subject: str,
        body: str,
        file_refs: Optional[Sequence[str]] = None,
        approved: bool = False,
    ) -> Dict[str, Any]:
        """Crée un nouveau fil typé et son message initial #1."""
        valid_type = validate_msg_type(msg_type, allow_reply=False)
        valid_subject = validate_subject(subject)
        valid_body = validate_body(body)
        valid_refs = validate_file_refs(file_refs)

        sender = self.resolve_workspace(from_workspace, allow_auto_register=True)
        recipient = self.resolve_workspace(to_workspace, allow_auto_register=True)
        now_dt = datetime.now(timezone.utc)
        now_iso = utc_now_iso(now_dt)
        refs_json = json.dumps(valid_refs, ensure_ascii=False)
        approved_int = 1 if approved else 0

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                thread_id = self._next_unique_id(conn, "threads", "thr", now_dt)
                conn.execute(
                    """
                    INSERT INTO threads (
                        id, from_workspace, to_workspace, msg_type, subject,
                        status, approved, target_conversation_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'open', ?, NULL, ?, ?)
                    """,
                    (
                        thread_id,
                        sender["slug"],
                        recipient["slug"],
                        valid_type,
                        valid_subject,
                        approved_int,
                        now_iso,
                        now_iso,
                    ),
                )
                cur = conn.execute(
                    """
                    INSERT INTO messages (
                        thread_id, sender_workspace, msg_type, body, file_refs_json, is_read, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (thread_id, sender["slug"], valid_type, valid_body, refs_json, now_iso),
                )
                message_id = int(cur.lastrowid)

        return {
            "thread_id": thread_id,
            "message_id": message_id,
            "from_workspace": sender["slug"],
            "to_workspace": recipient["slug"],
            "msg_type": valid_type,
            "subject": valid_subject,
            "status": "open",
            "approved": bool(approved_int),
            "file_refs": valid_refs,
            "created_at": now_iso,
        }

    def list_inbox(
        self,
        workspace: str,
        direction: str = "inbox",
        status: str = "open",
        unread_only: bool = False,
        limit: int = 20,
        *,
        reconcile: bool = True,
    ) -> List[Dict[str, Any]]:
        """Liste sans effet de bord les fils d'un workspace (jamais de modification de is_read)."""
        if direction not in ("inbox", "outbox", "all"):
            raise ValidationError(
                f"direction invalide ({direction!r}). Valeurs : inbox, outbox, all."
            )
        if status not in ("open", "done", "all"):
            raise ValidationError(
                f"status invalide ({status!r}). Valeurs : open, done, all."
            )
        valid_limit = validate_int_range(
            limit, name="limit", default=20, min_value=1, max_value=200
        )

        ws = self.get_workspace(workspace)
        ws_slug = ws["slug"]

        if reconcile:
            self.reconcile_orphan_jobs()

        where_parts: List[str] = []
        where_params: List[Any] = []
        if direction == "inbox":
            where_parts.append("t.to_workspace = ?")
            where_params.append(ws_slug)
        elif direction == "outbox":
            where_parts.append("t.from_workspace = ?")
            where_params.append(ws_slug)
        else:
            where_parts.append("(t.to_workspace = ? OR t.from_workspace = ?)")
            where_params.extend([ws_slug, ws_slug])

        if status != "all":
            where_parts.append("t.status = ?")
            where_params.append(status)

        unread_filter = "AND unread_count > 0" if unread_only else ""
        sql = f"""
            SELECT *
            FROM (
                SELECT
                    t.id AS thread_id,
                    t.from_workspace,
                    t.to_workspace,
                    t.msg_type,
                    t.subject,
                    t.status,
                    t.approved,
                    t.target_conversation_id,
                    t.created_at,
                    t.updated_at,
                    (
                        SELECT COUNT(*)
                        FROM messages m
                        WHERE m.thread_id = t.id
                          AND m.is_read = 0
                          AND m.sender_workspace != ?
                    ) AS unread_count,
                    (
                        SELECT COUNT(*)
                        FROM messages m
                        WHERE m.thread_id = t.id
                    ) AS message_count,
                    (
                        SELECT j.id
                        FROM jobs j
                        WHERE j.thread_id = t.id
                        ORDER BY j.created_at DESC, j.rowid DESC
                        LIMIT 1
                    ) AS latest_job_id,
                    (
                        SELECT j.status
                        FROM jobs j
                        WHERE j.thread_id = t.id
                        ORDER BY j.created_at DESC, j.rowid DESC
                        LIMIT 1
                    ) AS latest_job_status
                FROM threads t
                WHERE {' AND '.join(where_parts)}
            )
            WHERE 1 = 1 {unread_filter}
            ORDER BY updated_at DESC, thread_id DESC
            LIMIT ?
        """

        with self._connect() as conn:
            rows = conn.execute(sql, [ws_slug, *where_params, valid_limit]).fetchall()

        return [
            {
                "thread_id": row["thread_id"],
                "from_workspace": row["from_workspace"],
                "to_workspace": row["to_workspace"],
                "msg_type": row["msg_type"],
                "subject": row["subject"],
                "status": row["status"],
                "approved": bool(row["approved"]),
                "target_conversation_id": row["target_conversation_id"],
                "unread_count": int(row["unread_count"]),
                "message_count": int(row["message_count"]),
                "latest_job_id": row["latest_job_id"],
                "latest_job_status": row["latest_job_status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def read_thread(
        self,
        thread_id: str,
        reader_workspace: Optional[str] = None,
        mark_read: bool = True,
        *,
        reconcile: bool = True,
    ) -> Dict[str, Any]:
        """Lit un fil complet et marque optionnellement les messages adverses comme lus."""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValidationError("thread_id est requis.")
        tid = thread_id.strip()

        if reconcile:
            self.reconcile_orphan_jobs()

        reader_slug = self.get_workspace(reader_workspace)["slug"] if reader_workspace else None
        marked_read_count = 0

        with self._connect() as conn:
            if mark_read and reader_slug is not None:
                with self._transaction(conn, immediate=True):
                    exists = conn.execute(
                        "SELECT 1 FROM threads WHERE id = ?", (tid,)
                    ).fetchone()
                    if exists is None:
                        raise NotFoundError(f"Fil introuvable : {tid!r}.")
                    cur = conn.execute(
                        """
                        UPDATE messages
                        SET is_read = 1
                        WHERE thread_id = ? AND sender_workspace != ? AND is_read = 0
                        """,
                        (tid, reader_slug),
                    )
                    marked_read_count = int(cur.rowcount)

            thread_row = conn.execute(
                """
                SELECT t.*, w.canonical_path AS target_canonical_path
                FROM threads t
                JOIN workspaces w ON w.slug = t.to_workspace
                WHERE t.id = ?
                """,
                (tid,),
            ).fetchone()
            if thread_row is None:
                raise NotFoundError(f"Fil introuvable : {tid!r}.")

            msg_rows = conn.execute(
                """
                SELECT id, thread_id, sender_workspace, msg_type, body, file_refs_json, is_read, created_at
                FROM messages
                WHERE thread_id = ?
                ORDER BY id ASC
                """,
                (tid,),
            ).fetchall()

            job_row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE thread_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (tid,),
            ).fetchone()

        messages: List[Dict[str, Any]] = []
        all_file_refs: List[str] = []
        seen_refs: set[str] = set()
        for m in msg_rows:
            refs = json.loads(m["file_refs_json"] or "[]")
            for ref in refs:
                if ref not in seen_refs:
                    seen_refs.add(ref)
                    all_file_refs.append(ref)
            messages.append(
                {
                    "id": int(m["id"]),
                    "thread_id": m["thread_id"],
                    "sender_workspace": m["sender_workspace"],
                    "msg_type": m["msg_type"],
                    "body": m["body"],
                    "file_refs": refs,
                    "is_read": bool(m["is_read"]),
                    "created_at": m["created_at"],
                }
            )

        conv_id = thread_row["target_conversation_id"]
        target_path = thread_row["target_canonical_path"]
        resume_command = (
            f'cd "{target_path}" && agy --conversation {conv_id}'
            if conv_id
            else None
        )

        return {
            "thread_id": thread_row["id"],
            "from_workspace": thread_row["from_workspace"],
            "to_workspace": thread_row["to_workspace"],
            "target_canonical_path": target_path,
            "msg_type": thread_row["msg_type"],
            "subject": thread_row["subject"],
            "status": thread_row["status"],
            "approved": bool(thread_row["approved"]),
            "target_conversation_id": conv_id,
            "resume_command": resume_command,
            "file_refs": all_file_refs,
            "marked_read_count": marked_read_count,
            "created_at": thread_row["created_at"],
            "updated_at": thread_row["updated_at"],
            "messages": messages,
            "latest_job": _row_to_job(job_row) if job_row is not None else None,
        }

    def add_reply(
        self,
        thread_id: str,
        from_workspace: str,
        body: str,
        file_refs: Optional[Sequence[str]] = None,
        close: bool = False,
    ) -> Dict[str, Any]:
        """Ajoute une réponse utilisateur (filtrée strictement contre les secrets) à un fil."""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValidationError("thread_id est requis.")
        tid = thread_id.strip()
        valid_body = validate_body(body)
        valid_refs = validate_file_refs(file_refs)
        sender = self.resolve_workspace(from_workspace, allow_auto_register=True)
        now_iso = utc_now_iso()
        new_status = "done" if close else "open"
        refs_json = json.dumps(valid_refs, ensure_ascii=False)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute(
                    "SELECT id FROM threads WHERE id = ?", (tid,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Fil introuvable : {tid!r}.")

                cur = conn.execute(
                    """
                    INSERT INTO messages (
                        thread_id, sender_workspace, msg_type, body, file_refs_json, is_read, created_at
                    )
                    VALUES (?, ?, 'reply', ?, ?, 0, ?)
                    """,
                    (tid, sender["slug"], valid_body, refs_json, now_iso),
                )
                message_id = int(cur.lastrowid)

                conn.execute(
                    """
                    UPDATE messages
                    SET is_read = 1
                    WHERE thread_id = ? AND sender_workspace != ? AND is_read = 0
                    """,
                    (tid, sender["slug"]),
                )
                conn.execute(
                    "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now_iso, tid),
                )

        return {
            "thread_id": tid,
            "message_id": message_id,
            "sender_workspace": sender["slug"],
            "status": new_status,
            "file_refs": valid_refs,
            "created_at": now_iso,
        }

    def record_worker_reply(
        self,
        thread_id: str,
        sender_workspace: str,
        body: str,
        *,
        log_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persiste une réponse worker après caviardage des secrets et troncature UTF-8 à 32 Ko."""
        cleaned_body, redacted_rules = redact_secrets(body or "")
        if not cleaned_body.strip():
            cleaned_body = "(Réponse vide du worker)"

        suffix = (
            f"\n\n[Tronqué à {MAX_BODY_BYTES} octets ; sortie complète : {log_path}]"
            if log_path
            else f"\n\n[Tronqué à {MAX_BODY_BYTES} octets]"
        )
        final_body, truncated = truncate_utf8_bytes(cleaned_body, MAX_BODY_BYTES, suffix=suffix)
        now_iso = utc_now_iso()

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute(
                    "SELECT id FROM threads WHERE id = ?", (thread_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Fil introuvable : {thread_id!r}.")
                cur = conn.execute(
                    """
                    INSERT INTO messages (
                        thread_id, sender_workspace, msg_type, body, file_refs_json, is_read, created_at
                    )
                    VALUES (?, ?, 'reply', ?, '[]', 0, ?)
                    """,
                    (thread_id, sender_workspace, final_body, now_iso),
                )
                message_id = int(cur.lastrowid)
                conn.execute(
                    "UPDATE threads SET updated_at = ? WHERE id = ?",
                    (now_iso, thread_id),
                )

        return {
            "thread_id": thread_id,
            "message_id": message_id,
            "truncated": truncated,
            "redacted_rules": redacted_rules,
            "created_at": now_iso,
        }

    def create_job(
        self,
        thread_id: str,
        message_id: int,
        target_workspace: str,
        mode: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        *,
        approve_thread: bool = False,
    ) -> Dict[str, Any]:
        """Crée atomiquement un job queued (en vérifiant approbation et unicité par fil)."""
        valid_mode = validate_mode(mode)
        if valid_mode is None:
            raise ValidationError("mode est requis pour créer un job.")
        valid_timeout = validate_timeout_seconds(timeout_seconds)
        valid_effort = validate_effort(effort)
        valid_model = model.strip() if isinstance(model, str) and model.strip() else None

        self.reconcile_orphan_jobs()

        now_dt = datetime.now(timezone.utc)
        now_iso = utc_now_iso(now_dt)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                t_row = conn.execute(
                    "SELECT id, to_workspace, approved FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
                if t_row is None:
                    raise NotFoundError(f"Fil introuvable : {thread_id!r}.")

                m_row = conn.execute(
                    "SELECT id FROM messages WHERE id = ? AND thread_id = ?",
                    (message_id, thread_id),
                ).fetchone()
                if m_row is None:
                    raise NotFoundError(
                        f"Message {message_id} introuvable dans le fil {thread_id!r}."
                    )

                if target_workspace != t_row["to_workspace"]:
                    raise ValidationError(
                        f"target_workspace ({target_workspace!r}) ne correspond pas au destinataire du fil ({t_row['to_workspace']!r})."
                    )

                effective_approved = bool(t_row["approved"]) or bool(approve_thread)
                assert_mode_approval(valid_mode, effective_approved)

                if approve_thread and not t_row["approved"]:
                    conn.execute(
                        "UPDATE threads SET approved = 1, updated_at = ? WHERE id = ?",
                        (now_iso, thread_id),
                    )

                job_id = self._next_unique_id(conn, "jobs", "job", now_dt)
                try:
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, thread_id, message_id, target_workspace, mode, timeout_seconds,
                            status, pid, child_pid, target_conversation_id, model, effort,
                            exit_code, error_summary, duration_ms, started_at, finished_at, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)
                        """,
                        (
                            job_id,
                            thread_id,
                            message_id,
                            target_workspace,
                            valid_mode,
                            valid_timeout,
                            valid_model,
                            valid_effort,
                            now_iso,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DispatchBlockedError(
                        f"Un job actif existe déjà sur le fil {thread_id}."
                    ) from exc

                job_row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()

        return _row_to_job(job_row)

    def mark_job_running(
        self,
        job_id: str,
        pid: int,
        child_pid: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Passe un job à running en enregistrant le PID worker (et optionnellement child_pid)."""
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValidationError(f"PID worker invalide : {pid!r}")
        now_iso = utc_now_iso()

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute(
                    "SELECT status, started_at FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Job introuvable : {job_id!r}.")
                if row["status"] in _FINAL_JOB_STATUSES:
                    raise DispatchBlockedError(
                        f"Impossible de démarrer le job {job_id} (statut final : {row['status']})."
                    )
                started_at = row["started_at"] or now_iso
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'running',
                        pid = ?,
                        child_pid = COALESCE(?, child_pid),
                        started_at = ?
                    WHERE id = ?
                    """,
                    (pid, child_pid, started_at, job_id),
                )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(updated)

    def update_job_child_pid(self, job_id: str, child_pid: int) -> None:
        """Enregistre le PID du sous-processus agy exécuté par le worker."""
        if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid <= 0:
            raise ValidationError(f"child_pid invalide : {child_pid!r}")
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                cur = conn.execute(
                    "UPDATE jobs SET child_pid = ? WHERE id = ? AND status IN ('queued', 'running')",
                    (child_pid, job_id),
                )
                if cur.rowcount == 0:
                    exists = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    if exists is None:
                        raise NotFoundError(f"Job introuvable : {job_id!r}.")

    def complete_job(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: Optional[int] = None,
        target_conversation_id: Optional[str] = None,
        error_summary: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Clôture un job vers un statut final et met à jour target_conversation_id si présent."""
        if status not in _FINAL_JOB_STATUSES:
            raise ValidationError(
                f"Statut final de job invalide ({status!r}). Attendu : {_FINAL_JOB_STATUSES}."
            )
        clean_conv = (
            target_conversation_id.strip()
            if isinstance(target_conversation_id, str) and target_conversation_id.strip()
            else None
        )
        clean_err: Optional[str] = None
        if error_summary is not None:
            redacted_err, _ = redact_secrets(str(error_summary))
            clean_err, _ = truncate_utf8_bytes(redacted_err, _MAX_ERROR_SUMMARY_BYTES, suffix="...")

        now_iso = utc_now_iso()
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"Job introuvable : {job_id!r}.")

                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        exit_code = ?,
                        target_conversation_id = COALESCE(?, target_conversation_id),
                        error_summary = ?,
                        duration_ms = ?,
                        finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        exit_code,
                        clean_conv,
                        clean_err,
                        duration_ms,
                        now_iso,
                        job_id,
                    ),
                )
                if clean_conv is not None:
                    conn.execute(
                        "UPDATE threads SET target_conversation_id = ?, updated_at = ? WHERE id = ?",
                        (clean_conv, now_iso, row["thread_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE threads SET updated_at = ? WHERE id = ?",
                        (now_iso, row["thread_id"]),
                    )

                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(updated)

    def get_job(self, job_id: str, *, reconcile: bool = True) -> Dict[str, Any]:
        """Retourne l'état d'un job après réconciliation éventuelle des orphelins."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValidationError("job_id est requis.")
        jid = job_id.strip()
        if reconcile:
            self.reconcile_orphan_jobs()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row is None:
            raise NotFoundError(f"Job introuvable : {jid!r}.")
        return _row_to_job(row)

    def list_jobs(
        self,
        thread_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        *,
        reconcile: bool = True,
    ) -> List[Dict[str, Any]]:
        """Liste les jobs filtrés optionnellement par fil et/ou statut."""
        valid_limit = validate_int_range(
            limit, name="limit", default=20, min_value=1, max_value=200
        )
        if reconcile:
            self.reconcile_orphan_jobs()

        where_parts: List[str] = []
        params: List[Any] = []
        if thread_id is not None:
            where_parts.append("thread_id = ?")
            params.append(thread_id)
        if status is not None:
            where_parts.append("status = ?")
            params.append(status)

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        sql = f"""
            SELECT * FROM jobs
            {where_sql}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, [*params, valid_limit]).fetchall()
        return [_row_to_job(row) for row in rows]

    def reconcile_orphan_jobs(
        self,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> List[str]:
        """Passe en failed les jobs queued/running morts, sans PID après grâce ou expirés."""
        check_pid = pid_checker or is_pid_alive
        ref_now = now or datetime.now(timezone.utc)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=timezone.utc)
        else:
            ref_now = ref_now.astimezone(timezone.utc)
        ref_now_iso = utc_now_iso(ref_now)

        with self._connect() as conn:
            candidates = conn.execute(
                """
                SELECT id, status, pid, timeout_seconds, started_at, created_at
                FROM jobs
                WHERE status IN ('queued', 'running')
                """
            ).fetchall()

        if not candidates:
            return []

        to_fail: List[tuple[str, Optional[int]]] = []
        for row in candidates:
            jid = row["id"]
            st = row["status"]
            pid = row["pid"]
            timeout_sec = int(row["timeout_seconds"])
            ref_start_iso = row["started_at"] or row["created_at"]
            elapsed_sec = (ref_now - parse_utc_iso(ref_start_iso)).total_seconds()
            queued_elapsed_sec = (ref_now - parse_utc_iso(row["created_at"])).total_seconds()

            is_orphan = False
            if pid is not None:
                if not check_pid(int(pid)):
                    is_orphan = True
            elif st == "queued" and queued_elapsed_sec > QUEUED_GRACE_SECONDS:
                is_orphan = True
            elif st == "running":
                is_orphan = True

            if not is_orphan and elapsed_sec > (timeout_sec + 30):
                is_orphan = True

            if is_orphan:
                duration_ms = max(0, int(elapsed_sec * 1000))
                to_fail.append((jid, duration_ms))

        if not to_fail:
            return []

        reconciled_ids: List[str] = []
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                for jid, duration_ms in to_fail:
                    cur = conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed',
                            error_summary = ?,
                            duration_ms = COALESCE(duration_ms, ?),
                            finished_at = ?
                        WHERE id = ? AND status IN ('queued', 'running')
                        """,
                        (ORPHAN_ERROR_SUMMARY, duration_ms, ref_now_iso, jid),
                    )
                    if cur.rowcount > 0:
                        reconciled_ids.append(jid)
        return reconciled_ids

    def close_thread(self, thread_id: str) -> Dict[str, Any]:
        """Passe le statut d'un fil à done."""
        return self._set_thread_status(thread_id, "done")

    def reopen_thread(self, thread_id: str) -> Dict[str, Any]:
        """Passe le statut d'un fil à open."""
        return self._set_thread_status(thread_id, "open")

    def _set_thread_status(self, thread_id: str, new_status: str) -> Dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValidationError("thread_id est requis.")
        tid = thread_id.strip()
        now_iso = utc_now_iso()
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                cur = conn.execute(
                    "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now_iso, tid),
                )
                if cur.rowcount == 0:
                    raise NotFoundError(f"Fil introuvable : {tid!r}.")
        return {"thread_id": tid, "status": new_status, "updated_at": now_iso}

    def prune_done(
        self,
        older_than_days: int = 30,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Supprime les fils done antérieurs au seuil (et leurs fichiers logs associés)."""
        days = validate_int_range(
            older_than_days, name="older_than_days", default=30, min_value=0, max_value=3650
        )
        ref_now = now or datetime.now(timezone.utc)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=timezone.utc)
        else:
            ref_now = ref_now.astimezone(timezone.utc)

        cutoff_dt = ref_now - timedelta(days=days)
        cutoff_iso = utc_now_iso(cutoff_dt)
        cmp_op = "<=" if days == 0 else "<"

        deleted_thread_ids: List[str] = []
        job_ids_to_clean: List[str] = []

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                t_rows = conn.execute(
                    f"""
                    SELECT t.id
                    FROM threads t
                    WHERE t.status = 'done'
                      AND t.updated_at {cmp_op} ?
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs j
                          WHERE j.thread_id = t.id AND j.status IN ('queued', 'running')
                      )
                    """,
                    (cutoff_iso,),
                ).fetchall()
                deleted_thread_ids = [r["id"] for r in t_rows]
                if deleted_thread_ids:
                    placeholders = ",".join("?" for _ in deleted_thread_ids)
                    j_rows = conn.execute(
                        f"SELECT id FROM jobs WHERE thread_id IN ({placeholders})",
                        deleted_thread_ids,
                    ).fetchall()
                    job_ids_to_clean = [r["id"] for r in j_rows]
                    conn.execute(
                        f"DELETE FROM threads WHERE id IN ({placeholders})",
                        deleted_thread_ids,
                    )

        deleted_logs = 0
        for jid in job_ids_to_clean:
            log_file = self.logs_dir / f"{jid}.log"
            if log_file.is_file():
                try:
                    log_file.unlink()
                    deleted_logs += 1
                except OSError:
                    pass

        return {
            "deleted_threads": len(deleted_thread_ids),
            "deleted_thread_ids": deleted_thread_ids,
            "deleted_logs": deleted_logs,
            "cutoff_at": cutoff_iso,
        }

    def _next_unique_id(
        self,
        conn: sqlite3.Connection,
        table: str,
        prefix: str,
        now_dt: datetime,
    ) -> str:
        for _ in range(8):
            candidate = generate_id(prefix, now_dt)
            exists = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)
            ).fetchone()
            if exists is None:
                return candidate
        raise BridgeError(f"Impossible de générer un identifiant unique pour {table}.")
