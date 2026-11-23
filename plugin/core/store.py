"""Couche de persistance SQLite WAL, heartbeats sidecar et réconciliation pour Gravity Bridge."""

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
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from core.security import (
    CLAIM_GRACE_SECONDS,
    MAX_BODY_BYTES,
    SIDECAR_STALE_SECONDS,
    SLUG_MAX_CHARS,
    DispatchBlockedError,
    NotFoundError,
    ValidationError,
    assert_dispatch_approved,
    canonical_path_key,
    derive_slug_candidates,
    is_explicit_workspace_path,
    normalize_workspace_path,
    redact_secrets,
    truncate_utf8_bytes,
    validate_body,
    validate_conversation_id,
    validate_file_refs,
    validate_int_range,
    validate_job_action,
    validate_job_direction,
    validate_model,
    validate_msg_type,
    validate_sidecar_id,
    validate_slug,
    validate_subject,
    validate_wait_seconds,
)

_JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                     TEXT PRIMARY KEY,
    thread_id              TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    message_id             INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    target_workspace       TEXT NOT NULL REFERENCES workspaces(slug),
    action                 TEXT NOT NULL CHECK (action IN ('new_conversation', 'send_message')),
    direction              TEXT NOT NULL DEFAULT 'outbound' CHECK (direction IN ('outbound', 'callback')),
    model                  TEXT,
    sidecar_id             TEXT,
    target_conversation_id TEXT,
    status                 TEXT NOT NULL CHECK (
                               status IN ('queued', 'claimed', 'dispatched', 'completed', 'failed', 'cancelled')
                           ),
    exit_code              INTEGER,
    error_summary          TEXT,
    claimed_at             TEXT,
    dispatched_at          TEXT,
    finished_at            TEXT,
    created_at             TEXT NOT NULL
)
"""

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS workspaces (
    slug                 TEXT PRIMARY KEY,
    canonical_path       TEXT UNIQUE NOT NULL,
    display_name         TEXT NOT NULL,
    sidecar_id           TEXT,
    sidecar_pid          INTEGER,
    sidecar_last_seen_at TEXT,
    project_id           TEXT,
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
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
    caller_conversation_id TEXT,
    notify_caller          INTEGER NOT NULL DEFAULT 0 CHECK (notify_caller IN (0, 1)),
    callback_dispatched_at TEXT,
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

{_JOBS_TABLE_SQL};

CREATE INDEX IF NOT EXISTS idx_threads_to_status     ON threads(to_workspace, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_from_status   ON threads(from_workspace, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread       ON messages(thread_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_target_status    ON jobs(target_workspace, status, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_thread_status    ON jobs(thread_id, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_pending_per_thread
    ON jobs(thread_id) WHERE status IN ('queued', 'claimed');
"""

ORPHAN_ERROR_SUMMARY = "Sidecar indisponible ou relais interrompu avant dispatch agentapi"
_MAX_ERROR_SUMMARY_BYTES = 4_096
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_FINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
_PENDING_JOB_STATUSES = ("queued", "claimed")
_ACTIVE_JOB_STATUSES = ("queued", "claimed", "dispatched")

_WORKSPACE_COLS = (
    "slug, canonical_path, display_name, sidecar_id, sidecar_pid, "
    "sidecar_last_seen_at, project_id, created_at, last_seen_at"
)

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


def default_bridge_home() -> Path:
    """Retourne le dossier de stockage centralisé par défaut (~/.gemini/gravity/bridge)."""
    return (Path.home() / ".gemini" / "gravity" / "bridge").resolve()


def resolve_bridge_home(home: Optional[Path] = None) -> Path:
    """Résout le dossier centralisé (home > $GRAVITY_BRIDGE_HOME > $GRAVITY_HOME/bridge > ~/.gemini/gravity/bridge)."""
    if home is not None:
        return Path(home).expanduser().resolve()
    env_home = os.environ.get("GRAVITY_BRIDGE_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    gravity_home = os.environ.get("GRAVITY_HOME", "").strip()
    if gravity_home:
        return (Path(gravity_home).expanduser() / "bridge").resolve()
    return default_bridge_home()


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


def _normalize_optional_text(value: Optional[str], *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} doit être une chaîne de caractères.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValidationError(f"{field_name} contient des caractères de contrôle interdits.")
    stripped = value.strip()
    return stripped or None


def compute_sidecar_status(
    ws_row: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    pid_checker: Optional[Callable[[int], bool]] = None,
) -> str:
    """Calcule le statut courant d'un micro-sidecar (unlinked, offline ou online)."""
    sidecar_id = ws_row["sidecar_id"]
    if not sidecar_id:
        return "unlinked"

    last_seen = ws_row["sidecar_last_seen_at"]
    if not last_seen:
        return "offline"

    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    else:
        ref_now = ref_now.astimezone(timezone.utc)

    try:
        seen_dt = parse_utc_iso(str(last_seen))
    except ValueError:
        return "offline"

    if (ref_now - seen_dt).total_seconds() > SIDECAR_STALE_SECONDS:
        return "offline"

    sidecar_pid = ws_row["sidecar_pid"]
    if sidecar_pid is not None:
        check_pid = pid_checker or is_pid_alive
        if not check_pid(int(sidecar_pid)):
            return "offline"

    return "online"


def _row_to_workspace(
    row: sqlite3.Row,
    *,
    now: Optional[datetime] = None,
    pid_checker: Optional[Callable[[int], bool]] = None,
) -> Dict[str, Any]:
    return {
        "slug": row["slug"],
        "canonical_path": row["canonical_path"],
        "display_name": row["display_name"],
        "sidecar_id": row["sidecar_id"],
        "sidecar_pid": row["sidecar_pid"],
        "sidecar_last_seen_at": row["sidecar_last_seen_at"],
        "project_id": row["project_id"],
        "sidecar_status": compute_sidecar_status(row, now=now, pid_checker=pid_checker),
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
    }


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "message_id": int(row["message_id"]),
        "target_workspace": row["target_workspace"],
        "action": row["action"],
        "direction": row["direction"],
        "model": row["model"],
        "sidecar_id": row["sidecar_id"],
        "target_conversation_id": row["target_conversation_id"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "error_summary": row["error_summary"],
        "claimed_at": row["claimed_at"],
        "dispatched_at": row["dispatched_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


def resolve_notify_caller(
    caller_ws: Mapping[str, Any],
    *,
    caller_conversation_id: Optional[str],
    notify_caller: Optional[bool],
    existing_notify: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Résout l'intention de callback retour (Auto si prêt) et un avertissement non bloquant éventuel."""
    if notify_caller is not None and not isinstance(notify_caller, bool):
        raise ValidationError("Le paramètre 'notify_caller' doit être un booléen.")

    caller_status = str(caller_ws.get("sidecar_status") or "unlinked")
    caller_slug = str(caller_ws.get("slug") or "")

    if notify_caller is False:
        return False, None

    if notify_caller is True:
        warning: Optional[str] = None
        if caller_status == "unlinked":
            warning = (
                f"Callback retour activé (notify_caller=true), mais le sidecar du workspace appelant "
                f"{caller_slug!r} n'est pas lié (sidecar_status='unlinked'). "
                f"Exécutez /gravity-bridge link {caller_slug} et activez-le dans Antigravity App."
            )
        elif caller_status == "offline":
            warning = (
                f"Callback retour activé (notify_caller=true), mais le sidecar du workspace appelant "
                f"{caller_slug!r} est hors ligne (sidecar_status='offline'). "
                f"Activez ou redémarrez le sidecar (/gravity-bridge link {caller_slug} / Antigravity App)."
            )
        return True, warning

    if existing_notify:
        return True, None
    return bool(caller_conversation_id is not None and caller_status == "online"), None


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

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Migre de façon idempotente une base bridge.db antérieure (CLI) vers le schéma Sidecar."""
        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        if "workspaces" in existing_tables:
            ws_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(workspaces)").fetchall()
            }
            for col_name, col_type in (
                ("sidecar_id", "TEXT"),
                ("sidecar_pid", "INTEGER"),
                ("sidecar_last_seen_at", "TEXT"),
                ("project_id", "TEXT"),
            ):
                if col_name not in ws_cols:
                    conn.execute(f"ALTER TABLE workspaces ADD COLUMN {col_name} {col_type}")

        if "threads" in existing_tables:
            thread_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(threads)").fetchall()
            }
            for col_name, col_type in (
                ("caller_conversation_id", "TEXT"),
                ("notify_caller", "INTEGER NOT NULL DEFAULT 0 CHECK (notify_caller IN (0, 1))"),
                ("callback_dispatched_at", "TEXT"),
            ):
                if col_name not in thread_cols:
                    conn.execute(f"ALTER TABLE threads ADD COLUMN {col_name} {col_type}")

        if "jobs" in existing_tables:
            job_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "action" not in job_cols:
                conn.execute("DROP INDEX IF EXISTS idx_jobs_one_active_per_thread")
                conn.execute("DROP INDEX IF EXISTS idx_jobs_thread_status")
                conn.execute("DROP INDEX IF EXISTS idx_jobs_target_status")
                conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy_cli")
                conn.execute(_JOBS_TABLE_SQL.strip())

                now_iso = utc_now_iso()
                legacy_rows = conn.execute("SELECT * FROM jobs_legacy_cli").fetchall()
                for legacy in legacy_rows:
                    raw_status = str(legacy["status"] or "")
                    if raw_status in ("completed", "failed", "cancelled"):
                        mapped_status = raw_status
                        err_sum = legacy["error_summary"]
                        fin_at = legacy["finished_at"]
                    else:
                        mapped_status = "failed"
                        err_sum = (
                            legacy["error_summary"]
                            or f"Job CLI '{raw_status}' migré vers le schéma Sidecar."
                        )
                        fin_at = legacy["finished_at"] or now_iso

                    conv_id = legacy["target_conversation_id"]
                    mapped_action = "send_message" if conv_id else "new_conversation"
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, thread_id, message_id, target_workspace, action, model,
                            sidecar_id, target_conversation_id, status, exit_code,
                            error_summary, claimed_at, dispatched_at, finished_at, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            legacy["id"],
                            legacy["thread_id"],
                            int(legacy["message_id"]),
                            legacy["target_workspace"],
                            mapped_action,
                            legacy["model"] if "model" in job_cols else None,
                            conv_id,
                            mapped_status,
                            legacy["exit_code"],
                            err_sum,
                            legacy["started_at"],
                            legacy["started_at"],
                            fin_at,
                            legacy["created_at"],
                        ),
                    )
                conn.execute("DROP TABLE jobs_legacy_cli")
            elif "direction" not in job_cols:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound' "
                    "CHECK (direction IN ('outbound', 'callback'))"
                )

        conn.execute("DROP INDEX IF EXISTS idx_jobs_one_active_per_thread")

    def init_db(self) -> None:
        """Initialise le dossier de stockage, applique les migrations et crée le schéma DDL."""
        self.home_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                self._migrate_schema(conn)
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
            f"SELECT {_WORKSPACE_COLS} FROM workspaces"
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
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enregistre un workspace local ou met à jour last_seen_at/project_id s'il existe déjà."""
        canonical = normalize_workspace_path(path)
        requested_slug = validate_slug(slug) if slug is not None else None
        clean_project_id = _normalize_optional_text(project_id, field_name="project_id")
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
                        """
                        UPDATE workspaces
                        SET last_seen_at = ?,
                            project_id = COALESCE(?, project_id)
                        WHERE slug = ?
                        """,
                        (now_iso, clean_project_id, existing["slug"]),
                    )
                    updated = conn.execute(
                        f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                        (existing["slug"],),
                    ).fetchone()
                    res = _row_to_workspace(updated)
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
                    INSERT INTO workspaces (
                        slug, canonical_path, display_name,
                        sidecar_id, sidecar_pid, sidecar_last_seen_at, project_id,
                        created_at, last_seen_at
                    )
                    VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (chosen_slug, canonical, resolved_display, clean_project_id, now_iso, now_iso),
                )
                inserted = conn.execute(
                    f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                    (chosen_slug,),
                ).fetchone()

        res = _row_to_workspace(inserted)
        res["created"] = True
        return res

    def get_workspace(
        self,
        ref: str,
        *,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> Dict[str, Any]:
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
                    f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                    (cleaned,),
                ).fetchone()

        if row is None:
            raise NotFoundError(f"Workspace introuvable : {cleaned!r}.")
        return _row_to_workspace(row, now=now, pid_checker=pid_checker)

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

    def set_workspace_sidecar(
        self,
        workspace: str,
        sidecar_id: Optional[str],
        *,
        project_id: Optional[str] = None,
        clear_project_id: bool = False,
    ) -> Dict[str, Any]:
        """Associe ou dissocie l'identifiant du micro-sidecar d'un workspace."""
        ws = self.get_workspace(workspace)
        clean_sidecar = validate_sidecar_id(sidecar_id) if sidecar_id is not None else None
        clean_project = _normalize_optional_text(project_id, field_name="project_id")

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                if clean_sidecar is None:
                    if clear_project_id:
                        conn.execute(
                            """
                            UPDATE workspaces
                            SET sidecar_id = NULL,
                                sidecar_pid = NULL,
                                sidecar_last_seen_at = NULL,
                                project_id = NULL
                            WHERE slug = ?
                            """,
                            (ws["slug"],),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE workspaces
                            SET sidecar_id = NULL,
                                sidecar_pid = NULL,
                                sidecar_last_seen_at = NULL,
                                project_id = COALESCE(?, project_id)
                            WHERE slug = ?
                            """,
                            (clean_project, ws["slug"]),
                        )
                elif ws.get("sidecar_id") != clean_sidecar:
                    conn.execute(
                        """
                        UPDATE workspaces
                        SET sidecar_id = ?,
                            sidecar_pid = NULL,
                            sidecar_last_seen_at = NULL,
                            project_id = COALESCE(?, project_id)
                        WHERE slug = ?
                        """,
                        (clean_sidecar, clean_project, ws["slug"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE workspaces
                        SET sidecar_id = ?,
                            project_id = COALESCE(?, project_id)
                        WHERE slug = ?
                        """,
                        (clean_sidecar, clean_project, ws["slug"]),
                    )
                row = conn.execute(
                    f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                    (ws["slug"],),
                ).fetchone()
        return _row_to_workspace(row)

    def record_sidecar_heartbeat(
        self,
        workspace: str,
        *,
        sidecar_id: str,
        pid: int,
        project_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Enregistre le heartbeat d'un relais sidecar actif dans bridge.db."""
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValidationError(f"PID sidecar invalide : {pid!r}")
        valid_sidecar = validate_sidecar_id(sidecar_id)
        clean_project = _normalize_optional_text(project_id, field_name="project_id")
        ws = self.get_workspace(workspace)
        now_iso = utc_now_iso(now)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                conn.execute(
                    """
                    UPDATE workspaces
                    SET sidecar_id = ?,
                        sidecar_pid = ?,
                        sidecar_last_seen_at = ?,
                        project_id = COALESCE(?, project_id),
                        last_seen_at = ?
                    WHERE slug = ?
                    """,
                    (valid_sidecar, pid, now_iso, clean_project, now_iso, ws["slug"]),
                )
                row = conn.execute(
                    f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                    (ws["slug"],),
                ).fetchone()
        return _row_to_workspace(row, now=now)

    def assert_sidecar_online(
        self,
        workspace: str,
        *,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> Dict[str, Any]:
        """Vérifie en fail-fast que le micro-sidecar du workspace cible est en ligne."""
        ws = self.get_workspace(workspace, now=now, pid_checker=pid_checker)
        status = ws["sidecar_status"]
        if status == "online":
            return ws
        if status == "unlinked":
            raise DispatchBlockedError(
                f"Le workspace cible {ws['slug']!r} n'est lié à aucun micro-sidecar "
                f"(sidecar_status='unlinked'). Exécutez /gravity-bridge link {ws['slug']} "
                "et activez-le dans Antigravity App, ou envoyez avec dispatch=false."
            )
        raise DispatchBlockedError(
            f"Le micro-sidecar du workspace cible {ws['slug']!r} ({ws.get('sidecar_id')}) "
            "est hors ligne (sidecar_status='offline'). Activez ou redémarrez le sidecar dans "
            "Antigravity App, ou envoyez avec dispatch=false."
        )

    def list_target_conversation_ids(self) -> List[str]:
        """Retourne l'ensemble des conversation_id ouverts par Gravity Bridge (anti-boucle)."""
        with self._connect() as conn:
            t_rows = conn.execute(
                "SELECT DISTINCT target_conversation_id FROM threads WHERE target_conversation_id IS NOT NULL"
            ).fetchall()
            j_rows = conn.execute(
                "SELECT DISTINCT target_conversation_id FROM jobs "
                "WHERE target_conversation_id IS NOT NULL AND direction = 'outbound'"
            ).fetchall()
        seen: set[str] = set()
        result: List[str] = []
        for row in (*t_rows, *j_rows):
            val = str(row[0] or "").strip()
            if val and val.casefold() not in seen:
                seen.add(val.casefold())
                result.append(val)
        return result

    def is_target_conversation_id(self, conversation_id: str) -> bool:
        """Indique si conversation_id correspond à une session ouverte par Gravity Bridge."""
        valid = validate_conversation_id(conversation_id, field_name="conversation_id")
        if valid is None:
            return False
        folded = valid.casefold()
        with self._connect() as conn:
            t_hit = conn.execute(
                "SELECT 1 FROM threads WHERE LOWER(target_conversation_id) = ? LIMIT 1",
                (folded,),
            ).fetchone()
            if t_hit is not None:
                return True
            j_hit = conn.execute(
                "SELECT 1 FROM jobs WHERE LOWER(target_conversation_id) = ? AND direction = 'outbound' LIMIT 1",
                (folded,),
            ).fetchone()
            return j_hit is not None

    def list_workspaces(
        self,
        *,
        reconcile: bool = True,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Liste tous les workspaces triés par slug avec leur statut sidecar et compteurs."""
        if reconcile:
            self.reconcile_orphan_jobs(now=now, pid_checker=pid_checker)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    w.slug,
                    w.canonical_path,
                    w.display_name,
                    w.sidecar_id,
                    w.sidecar_pid,
                    w.sidecar_last_seen_at,
                    w.project_id,
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
                          AND j.status IN ('queued', 'claimed', 'dispatched')
                    ) AS active_jobs_count
                FROM workspaces w
                ORDER BY w.slug ASC
                """
            ).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            ws = _row_to_workspace(row, now=now, pid_checker=pid_checker)
            ws["open_inbox_count"] = int(row["open_inbox_count"])
            ws["unread_count"] = int(row["unread_count"])
            ws["active_jobs_count"] = int(row["active_jobs_count"])
            results.append(ws)
        return results

    def create_thread(
        self,
        from_workspace: str,
        to_workspace: str,
        msg_type: str,
        subject: str,
        body: str,
        file_refs: Optional[Sequence[str]] = None,
        approved: bool = False,
        caller_conversation_id: Optional[str] = None,
        notify_caller: Optional[bool] = None,
        *,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> Dict[str, Any]:
        """Crée un nouveau fil typé et son message initial #1."""
        valid_type = validate_msg_type(msg_type, allow_reply=False)
        valid_subject = validate_subject(subject)
        valid_body = validate_body(body)
        valid_refs = validate_file_refs(file_refs)
        clean_caller_conv = validate_conversation_id(
            caller_conversation_id, field_name="caller_conversation_id"
        )

        sender = self.resolve_workspace(from_workspace, allow_auto_register=True)
        if now is not None or pid_checker is not None:
            sender = self.get_workspace(sender["slug"], now=now, pid_checker=pid_checker)
        recipient = self.resolve_workspace(to_workspace, allow_auto_register=True)

        resolved_notify, warning = resolve_notify_caller(
            sender,
            caller_conversation_id=clean_caller_conv,
            notify_caller=notify_caller,
        )

        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        else:
            now_dt = now_dt.astimezone(timezone.utc)
        now_iso = utc_now_iso(now_dt)
        refs_json = json.dumps(valid_refs, ensure_ascii=False)
        approved_int = 1 if approved else 0
        notify_int = 1 if resolved_notify else 0

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                thread_id = self._next_unique_id(conn, "threads", "thr", now_dt)
                conn.execute(
                    """
                    INSERT INTO threads (
                        id, from_workspace, to_workspace, msg_type, subject,
                        status, approved, target_conversation_id,
                        caller_conversation_id, notify_caller, callback_dispatched_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'open', ?, NULL, ?, ?, NULL, ?, ?)
                    """,
                    (
                        thread_id,
                        sender["slug"],
                        recipient["slug"],
                        valid_type,
                        valid_subject,
                        approved_int,
                        clean_caller_conv,
                        notify_int,
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

        result: Dict[str, Any] = {
            "thread_id": thread_id,
            "message_id": message_id,
            "from_workspace": sender["slug"],
            "to_workspace": recipient["slug"],
            "msg_type": valid_type,
            "subject": valid_subject,
            "status": "open",
            "approved": bool(approved_int),
            "caller_conversation_id": clean_caller_conv,
            "notify_caller": resolved_notify,
            "file_refs": valid_refs,
            "created_at": now_iso,
        }
        if warning:
            result["warnings"] = [warning]
        return result

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
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> Dict[str, Any]:
        """Lit un fil complet et marque optionnellement les messages adverses comme lus."""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValidationError("thread_id est requis.")
        tid = thread_id.strip()

        if reconcile:
            self.reconcile_orphan_jobs(now=now, pid_checker=pid_checker)

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
                SELECT
                    t.*,
                    w.canonical_path AS target_canonical_path,
                    w.project_id AS target_project_id,
                    w.sidecar_id AS sidecar_id,
                    w.sidecar_pid AS sidecar_pid,
                    w.sidecar_last_seen_at AS sidecar_last_seen_at
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

        target_sidecar_status = compute_sidecar_status(
            thread_row, now=now, pid_checker=pid_checker
        )

        return {
            "thread_id": thread_row["id"],
            "from_workspace": thread_row["from_workspace"],
            "to_workspace": thread_row["to_workspace"],
            "target_canonical_path": thread_row["target_canonical_path"],
            "target_project_id": thread_row["target_project_id"],
            "target_sidecar_status": target_sidecar_status,
            "msg_type": thread_row["msg_type"],
            "subject": thread_row["subject"],
            "status": thread_row["status"],
            "approved": bool(thread_row["approved"]),
            "target_conversation_id": thread_row["target_conversation_id"],
            "caller_conversation_id": thread_row["caller_conversation_id"],
            "notify_caller": bool(thread_row["notify_caller"]),
            "callback_dispatched_at": thread_row["callback_dispatched_at"],
            "file_refs": all_file_refs,
            "marked_read_count": marked_read_count,
            "created_at": thread_row["created_at"],
            "updated_at": thread_row["updated_at"],
            "messages": messages,
            "latest_job": _row_to_job(job_row) if job_row is not None else None,
        }

    def get_message(self, message_id: int) -> Dict[str, Any]:
        """Retourne un message par son identifiant entier."""
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValidationError(f"message_id invalide : {message_id!r}.")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, thread_id, sender_workspace, msg_type, body, file_refs_json, is_read, created_at
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Message introuvable : {message_id}.")
        return {
            "id": int(row["id"]),
            "thread_id": row["thread_id"],
            "sender_workspace": row["sender_workspace"],
            "msg_type": row["msg_type"],
            "body": row["body"],
            "file_refs": json.loads(row["file_refs_json"] or "[]"),
            "is_read": bool(row["is_read"]),
            "created_at": row["created_at"],
        }

    def add_reply(
        self,
        thread_id: str,
        from_workspace: str,
        body: str,
        file_refs: Optional[Sequence[str]] = None,
        close: bool = False,
        conversation_id: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> Dict[str, Any]:
        """Ajoute une réponse (filtrée secrets), enregistre conversation_id, clôt les jobs dispatched et déclenche le callback one-shot."""
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValidationError("thread_id est requis.")
        tid = thread_id.strip()
        valid_body = validate_body(body)
        valid_refs = validate_file_refs(file_refs)
        clean_conv = validate_conversation_id(conversation_id, field_name="conversation_id")
        sender = self.resolve_workspace(from_workspace, allow_auto_register=True)

        self.reconcile_orphan_jobs(now=now, pid_checker=pid_checker)

        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        else:
            now_dt = now_dt.astimezone(timezone.utc)
        now_iso = utc_now_iso(now_dt)
        new_status = "done" if close else "open"
        refs_json = json.dumps(valid_refs, ensure_ascii=False)
        completed_job_ids: List[str] = []
        callback_status: Optional[str] = None
        callback_job: Optional[Dict[str, Any]] = None

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute(
                    """
                    SELECT id, from_workspace, to_workspace, approved, target_conversation_id,
                           caller_conversation_id, notify_caller, callback_dispatched_at
                    FROM threads WHERE id = ?
                    """,
                    (tid,),
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

                is_target_reply = sender["slug"] == row["to_workspace"]
                if is_target_reply and clean_conv is not None:
                    conn.execute(
                        """
                        UPDATE threads
                        SET status = ?, target_conversation_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_status, clean_conv, now_iso, tid),
                    )
                else:
                    conn.execute(
                        "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
                        (new_status, now_iso, tid),
                    )

                effective_conv = (
                    clean_conv
                    if (is_target_reply and clean_conv is not None)
                    else row["target_conversation_id"]
                )

                if is_target_reply:
                    disp_rows = conn.execute(
                        "SELECT id FROM jobs WHERE thread_id = ? AND status = 'dispatched'",
                        (tid,),
                    ).fetchall()
                    completed_job_ids = [r["id"] for r in disp_rows]
                    if completed_job_ids:
                        conn.execute(
                            """
                            UPDATE jobs
                            SET status = 'completed',
                                target_conversation_id = COALESCE(?, target_conversation_id),
                                finished_at = ?
                            WHERE thread_id = ? AND status = 'dispatched'
                            """,
                            (clean_conv, now_iso, tid),
                        )

                caller_conv = row["caller_conversation_id"]
                same_conv = (
                    caller_conv is not None
                    and effective_conv is not None
                    and str(caller_conv).casefold() == str(effective_conv).casefold()
                )
                should_notify = (
                    is_target_reply
                    and bool(row["notify_caller"])
                    and bool(row["approved"])
                    and bool(caller_conv)
                    and not same_conv
                    and row["callback_dispatched_at"] is None
                    and (close or bool(completed_job_ids))
                )
                if should_notify:
                    pending_row = conn.execute(
                        "SELECT 1 FROM jobs WHERE thread_id = ? AND status IN ('queued', 'claimed') LIMIT 1",
                        (tid,),
                    ).fetchone()
                    if pending_row is None:
                        from_ws_row = conn.execute(
                            f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                            (row["from_workspace"],),
                        ).fetchone()
                        caller_sidecar_status = (
                            compute_sidecar_status(from_ws_row, now=now_dt, pid_checker=pid_checker)
                            if from_ws_row is not None
                            else "unlinked"
                        )
                        if caller_sidecar_status == "online":
                            if close:
                                conn.execute(
                                    "UPDATE threads SET callback_dispatched_at = ? WHERE id = ?",
                                    (now_iso, tid),
                                )
                            cb_job_id = self._next_unique_id(conn, "jobs", "job", now_dt)
                            conn.execute(
                                """
                                INSERT INTO jobs (
                                    id, thread_id, message_id, target_workspace, action, direction, model,
                                    sidecar_id, target_conversation_id, status, exit_code,
                                    error_summary, claimed_at, dispatched_at, finished_at, created_at
                                )
                                VALUES (?, ?, ?, ?, 'send_message', 'callback', NULL, ?, ?, 'queued', NULL, NULL, NULL, NULL, NULL, ?)
                                """,
                                (
                                    cb_job_id,
                                    tid,
                                    message_id,
                                    row["from_workspace"],
                                    from_ws_row["sidecar_id"],
                                    caller_conv,
                                    now_iso,
                                ),
                            )
                            cb_row = conn.execute(
                                "SELECT * FROM jobs WHERE id = ?", (cb_job_id,)
                            ).fetchone()
                            callback_job = _row_to_job(cb_row)
                            callback_status = "queued"
                        else:
                            callback_status = f"skipped_sidecar_{caller_sidecar_status}"

        result: Dict[str, Any] = {
            "thread_id": tid,
            "message_id": message_id,
            "sender_workspace": sender["slug"],
            "status": new_status,
            "file_refs": valid_refs,
            "conversation_id": effective_conv,
            "completed_job_ids": completed_job_ids,
            "created_at": now_iso,
        }
        if callback_status is not None:
            result["callback_status"] = callback_status
        if callback_job is not None:
            result["callback_job"] = callback_job
        return result

    def create_job(
        self,
        thread_id: str,
        message_id: int,
        target_workspace: str,
        *,
        action: Optional[str] = None,
        model: Optional[str] = None,
        approve_thread: bool = False,
        require_sidecar_online: bool = True,
        caller_conversation_id: Optional[str] = None,
        notify_caller: Optional[bool] = None,
        sync_callback: bool = False,
        now: Optional[datetime] = None,
        pid_checker: Optional[Callable[[int], bool]] = None,
    ) -> Dict[str, Any]:
        """Crée atomiquement un job queued après vérification d'approbation et sidecar en ligne."""
        valid_model = validate_model(model)
        if action is not None:
            validate_job_action(action)
        clean_caller_conv = validate_conversation_id(
            caller_conversation_id, field_name="caller_conversation_id"
        )
        if notify_caller is not None and not isinstance(notify_caller, bool):
            raise ValidationError("Le paramètre 'notify_caller' doit être un booléen.")

        self.reconcile_orphan_jobs(now=now, pid_checker=pid_checker)
        ws = (
            self.assert_sidecar_online(target_workspace, now=now, pid_checker=pid_checker)
            if require_sidecar_online
            else self.get_workspace(target_workspace, now=now, pid_checker=pid_checker)
        )

        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        else:
            now_dt = now_dt.astimezone(timezone.utc)
        now_iso = utc_now_iso(now_dt)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                t_row = conn.execute(
                    """
                    SELECT id, from_workspace, to_workspace, approved, target_conversation_id,
                           caller_conversation_id, notify_caller
                    FROM threads WHERE id = ?
                    """,
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

                if ws["slug"] != t_row["to_workspace"]:
                    raise ValidationError(
                        f"target_workspace ({ws['slug']!r}) ne correspond pas au destinataire du fil ({t_row['to_workspace']!r})."
                    )

                effective_approved = bool(t_row["approved"]) or bool(approve_thread)
                assert_dispatch_approved(effective_approved)

                if approve_thread and not t_row["approved"]:
                    conn.execute(
                        "UPDATE threads SET approved = 1, updated_at = ? WHERE id = ?",
                        (now_iso, thread_id),
                    )

                if sync_callback:
                    from_ws_row = conn.execute(
                        f"SELECT {_WORKSPACE_COLS} FROM workspaces WHERE slug = ?",
                        (t_row["from_workspace"],),
                    ).fetchone()
                    if from_ws_row is not None:
                        caller_ws = _row_to_workspace(
                            from_ws_row, now=now_dt, pid_checker=pid_checker
                        )
                        effective_caller = (
                            clean_caller_conv
                            if clean_caller_conv is not None
                            else t_row["caller_conversation_id"]
                        )
                        resolved_notify, _ = resolve_notify_caller(
                            caller_ws,
                            caller_conversation_id=effective_caller,
                            notify_caller=notify_caller,
                            existing_notify=bool(t_row["notify_caller"]),
                        )
                        conn.execute(
                            """
                            UPDATE threads
                            SET caller_conversation_id = ?,
                                notify_caller = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                effective_caller,
                                1 if resolved_notify else 0,
                                now_iso,
                                thread_id,
                            ),
                        )

                existing_conv = t_row["target_conversation_id"]
                effective_action = (
                    action
                    if action is not None
                    else ("send_message" if existing_conv else "new_conversation")
                )
                job_conv_id = existing_conv if effective_action == "send_message" else None

                job_id = self._next_unique_id(conn, "jobs", "job", now_dt)
                try:
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, thread_id, message_id, target_workspace, action, direction, model,
                            sidecar_id, target_conversation_id, status, exit_code,
                            error_summary, claimed_at, dispatched_at, finished_at, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, 'outbound', ?, ?, ?, 'queued', NULL, NULL, NULL, NULL, NULL, ?)
                        """,
                        (
                            job_id,
                            thread_id,
                            message_id,
                            ws["slug"],
                            effective_action,
                            valid_model,
                            ws.get("sidecar_id"),
                            job_conv_id,
                            now_iso,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DispatchBlockedError(
                        f"Un job en attente existe déjà sur le fil {thread_id}."
                    ) from exc

                job_row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()

        return _row_to_job(job_row)

    def claim_next_job(
        self,
        target_workspace: str,
        *,
        sidecar_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Verrouille atomiquement le plus ancien job queued pour un workspace cible."""
        valid_sidecar = validate_sidecar_id(sidecar_id)
        ws = self.get_workspace(target_workspace, now=now)
        now_iso = utc_now_iso(now)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute(
                    """
                    SELECT id FROM jobs
                    WHERE target_workspace = ? AND status = 'queued'
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                    """,
                    (ws["slug"],),
                ).fetchone()
                if row is None:
                    return None
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'claimed',
                        sidecar_id = ?,
                        claimed_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (valid_sidecar, now_iso, row["id"]),
                )
                if cur.rowcount == 0:
                    return None
                claimed = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (row["id"],)
                ).fetchone()
        return _row_to_job(claimed)

    def mark_job_dispatched(
        self,
        job_id: str,
        *,
        target_conversation_id: Optional[str] = None,
        exit_code: int = 0,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Passe un job à dispatched après invocation agentapi réussie."""
        clean_conv = validate_conversation_id(
            target_conversation_id, field_name="target_conversation_id"
        )
        now_iso = utc_now_iso(now)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"Job introuvable : {job_id!r}.")
                if row["status"] == "cancelled":
                    return _row_to_job(row)
                if row["direction"] == "callback":
                    raise DispatchBlockedError(
                        f"Impossible de marquer dispatched le job callback {job_id} (complétion directe requise)."
                    )
                if row["status"] in _FINAL_JOB_STATUSES:
                    raise DispatchBlockedError(
                        f"Impossible de marquer dispatched le job {job_id} (statut final : {row['status']})."
                    )

                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'dispatched',
                        exit_code = ?,
                        target_conversation_id = COALESCE(?, target_conversation_id),
                        dispatched_at = COALESCE(dispatched_at, ?)
                    WHERE id = ?
                    """,
                    (exit_code, clean_conv, now_iso, job_id),
                )
                if clean_conv is not None and row["direction"] == "outbound":
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

    def complete_job(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: Optional[int] = None,
        target_conversation_id: Optional[str] = None,
        error_summary: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Clôture un job vers un statut final (completed, failed, cancelled)."""
        if status not in _FINAL_JOB_STATUSES:
            raise ValidationError(
                f"Statut final de job invalide ({status!r}). Attendu : {_FINAL_JOB_STATUSES}."
            )
        clean_conv = validate_conversation_id(
            target_conversation_id, field_name="target_conversation_id"
        )
        clean_err: Optional[str] = None
        if error_summary is not None:
            redacted_err, _ = redact_secrets(str(error_summary))
            clean_err, _ = truncate_utf8_bytes(redacted_err, _MAX_ERROR_SUMMARY_BYTES, suffix="...")

        now_iso = utc_now_iso(now)
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"Job introuvable : {job_id!r}.")
                if row["status"] == "cancelled" and status != "cancelled":
                    return _row_to_job(row)

                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        exit_code = ?,
                        target_conversation_id = COALESCE(?, target_conversation_id),
                        error_summary = ?,
                        finished_at = ?
                    WHERE id = ?
                    """,
                    (status, exit_code, clean_conv, clean_err, now_iso, job_id),
                )
                if clean_conv is not None and row["direction"] == "outbound":
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

    def cancel_job(self, job_id: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Annule un job en attente (queued/claimed) ou déjà envoyé (dispatched)."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValidationError("job_id est requis.")
        jid = job_id.strip()
        self.reconcile_orphan_jobs(now=now)
        now_iso = utc_now_iso(now)

        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
                if row is None:
                    raise NotFoundError(f"Job introuvable : {jid!r}.")
                if row["status"] not in _ACTIVE_JOB_STATUSES:
                    res = _row_to_job(row)
                    res["cancelled"] = False
                    return res

                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        error_summary = COALESCE(error_summary, 'Job annulé à la demande de l''utilisateur.'),
                        finished_at = ?
                    WHERE id = ?
                    """,
                    (now_iso, jid),
                )
                updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        res = _row_to_job(updated)
        res["cancelled"] = True
        return res

    def wait_for_job_dispatch(
        self,
        job_id: str,
        wait_seconds: int = 0,
        poll_interval: float = 0.2,
    ) -> Dict[str, Any]:
        """Attend de façon bornée qu'un job queued passe à dispatched/completed/failed."""
        import time

        valid_wait = validate_wait_seconds(wait_seconds)
        if valid_wait <= 0:
            return self.get_job(job_id, reconcile=False)

        deadline = time.monotonic() + valid_wait
        step = max(0.05, float(poll_interval))
        while True:
            current = self.get_job(job_id, reconcile=True)
            if current["status"] not in _PENDING_JOB_STATUSES:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            time.sleep(min(step, remaining))

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
        """Passe en failed les jobs queued/claimed bloqués ou dont le sidecar est mort."""
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
                SELECT j.id, j.status, j.claimed_at, j.created_at, w.sidecar_pid
                FROM jobs j
                LEFT JOIN workspaces w ON w.slug = j.target_workspace
                WHERE j.status IN ('queued', 'claimed')
                """
            ).fetchall()

        if not candidates:
            return []

        to_fail: List[str] = []
        for row in candidates:
            jid = row["id"]
            st = row["status"]
            ref_iso = row["claimed_at"] if (st == "claimed" and row["claimed_at"]) else row["created_at"]
            elapsed_sec = (ref_now - parse_utc_iso(ref_iso)).total_seconds()

            is_orphan = elapsed_sec > CLAIM_GRACE_SECONDS
            if not is_orphan and st == "claimed":
                spid = row["sidecar_pid"]
                if spid is not None and not check_pid(int(spid)):
                    is_orphan = True

            if is_orphan:
                to_fail.append(jid)

        if not to_fail:
            return []

        reconciled_ids: List[str] = []
        with self._connect() as conn:
            with self._transaction(conn, immediate=True):
                for jid in to_fail:
                    cur = conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed',
                            error_summary = ?,
                            finished_at = ?
                        WHERE id = ? AND status IN ('queued', 'claimed')
                        """,
                        (ORPHAN_ERROR_SUMMARY, ref_now_iso, jid),
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
                          WHERE j.thread_id = t.id AND j.status IN ('queued', 'claimed')
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
