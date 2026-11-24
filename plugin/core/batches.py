"""Orchestration de lots (batches), sérialisation handoff et TTL stale pour Gravity Bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Mapping, Optional

from core.security import ValidationError, assert_no_secrets

ALLOWED_CALLBACK_MODES = ("per_thread", "on_batch_complete")
DEFAULT_DISPATCHED_STALE_SECONDS = 1800
DISPATCHED_STALE_ENV = "GRAVITY_BRIDGE_DISPATCHED_STALE_SECONDS"
BATCH_ID_MAX_CHARS = 80
BATCH_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
STALE_DISPATCHED_ERROR_SUMMARY = "Job dispatched expiré (TTL stale dépassé sans réponse cible)"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_REPLY_EXCERPT_CHARS = 240


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_utc_iso(ts: str) -> datetime:
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(normalized)
    return _ensure_utc(dt)


def validate_batch_id(batch_id: Optional[str]) -> Optional[str]:
    """Valide et normalise un identifiant de lot optionnel (None si absent ou blanc)."""
    if batch_id is None:
        return None
    if not isinstance(batch_id, str):
        raise ValidationError("batch_id doit être une chaîne de caractères.")
    if _CONTROL_CHARS_RE.search(batch_id):
        raise ValidationError("batch_id contient des caractères de contrôle interdits.")
    stripped = batch_id.strip()
    if not stripped:
        return None
    if len(stripped) > BATCH_ID_MAX_CHARS:
        raise ValidationError(
            f"batch_id dépasse le plafond de {BATCH_ID_MAX_CHARS} caractères."
        )
    if not BATCH_ID_RE.match(stripped):
        raise ValidationError(
            "batch_id invalide : doit correspondre à ^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$."
        )
    assert_no_secrets(stripped, field_name="batch_id")
    return stripped


def validate_callback_mode(
    callback_mode: Optional[str],
    *,
    batch_id: Optional[str] = None,
) -> str:
    """Valide le mode de callback retour ('per_thread' ou 'on_batch_complete')."""
    if callback_mode is None:
        normalized = "per_thread"
    elif not isinstance(callback_mode, str):
        raise ValidationError("callback_mode doit être une chaîne de caractères.")
    else:
        stripped = callback_mode.strip().lower()
        normalized = stripped if stripped else "per_thread"

    if normalized not in ALLOWED_CALLBACK_MODES:
        raise ValidationError(
            f"callback_mode invalide ({callback_mode!r}). "
            f"Valeurs autorisées : {', '.join(ALLOWED_CALLBACK_MODES)}."
        )

    clean_batch_id = validate_batch_id(batch_id)
    if normalized == "on_batch_complete" and clean_batch_id is None:
        raise ValidationError(
            "callback_mode='on_batch_complete' exige un 'batch_id' non vide."
        )
    return normalized


def resolve_dispatched_stale_seconds(
    override: Optional[int] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Résout le TTL stale des jobs dispatched (override > env > 1800 s)."""
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, int) or override <= 0:
            raise ValidationError(
                f"dispatched_stale_seconds doit être un entier strictement positif (reçu {override!r})."
            )
        return override

    source = os.environ if env is None else env
    raw = source.get(DISPATCHED_STALE_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_DISPATCHED_STALE_SECONDS
    try:
        parsed = int(str(raw).strip())
    except ValueError as exc:
        raise ValidationError(
            f"Variable {DISPATCHED_STALE_ENV} invalide ({raw!r}) : entier > 0 attendu."
        ) from exc
    if parsed <= 0:
        raise ValidationError(
            f"Variable {DISPATCHED_STALE_ENV} invalide ({parsed}) : entier > 0 attendu."
        )
    return parsed


def has_active_outbound_job(
    conn: sqlite3.Connection,
    target_workspace: str,
    *,
    exclude_job_id: Optional[str] = None,
) -> bool:
    """Indique si target_workspace possède déjà un job outbound handoff actif (claimed/dispatched)."""
    params: List[Any] = [target_workspace]
    exclude_sql = ""
    if exclude_job_id is not None:
        exclude_sql = "AND j.id != ?"
        params.append(exclude_job_id)

    row = conn.execute(
        f"""
        SELECT 1
        FROM jobs j
        JOIN threads t ON t.id = j.thread_id
        WHERE j.target_workspace = ?
          AND j.direction = 'outbound'
          AND t.msg_type = 'handoff'
          AND j.status IN ('claimed', 'dispatched')
          {exclude_sql}
        LIMIT 1
        """,
        params,
    ).fetchone()
    return row is not None


def find_stale_dispatched_jobs(
    conn: sqlite3.Connection,
    ref_now: datetime,
    *,
    stale_seconds: Optional[int] = None,
) -> List[str]:
    """Retourne les IDs des jobs outbound dispatched dépassant le TTL stale."""
    ttl = resolve_dispatched_stale_seconds(stale_seconds)
    now_utc = _ensure_utc(ref_now)
    rows = conn.execute(
        """
        SELECT id, dispatched_at, claimed_at, created_at
        FROM jobs
        WHERE status = 'dispatched' AND direction = 'outbound'
        ORDER BY created_at ASC, rowid ASC
        """
    ).fetchall()

    stale_ids: List[str] = []
    for row in rows:
        ref_iso = row["dispatched_at"] or row["claimed_at"] or row["created_at"]
        if not ref_iso:
            continue
        elapsed = (now_utc - _parse_utc_iso(str(ref_iso))).total_seconds()
        if elapsed > ttl:
            stale_ids.append(str(row["id"]))
    return stale_ids


def _format_reply_excerpt(body: Optional[str]) -> Optional[str]:
    if not body:
        return None
    collapsed = " ".join(str(body).split())
    if not collapsed:
        return None
    if len(collapsed) > _MAX_REPLY_EXCERPT_CHARS:
        return collapsed[: _MAX_REPLY_EXCERPT_CHARS - 3].rstrip() + "..."
    return collapsed


def summarize_batch(conn: sqlite3.Connection, batch_id: str) -> Dict[str, Any]:
    """Construit le résumé consolidé d'un lot de fils identifiés par batch_id."""
    clean_batch_id = validate_batch_id(batch_id)
    if clean_batch_id is None:
        raise ValidationError("batch_id est requis pour résumer un lot.")

    thread_rows = conn.execute(
        """
        SELECT id, to_workspace, status, subject, callback_dispatched_at
        FROM threads
        WHERE batch_id = ?
        ORDER BY created_at ASC, rowid ASC
        """,
        (clean_batch_id,),
    ).fetchall()

    threads_summary: List[Dict[str, Any]] = []
    consolidated_file_refs: List[str] = []
    seen_consolidated: set[str] = set()
    done_threads = 0
    has_dispatched_callback = False

    for t_row in thread_rows:
        tid = str(t_row["id"])
        status = str(t_row["status"])
        if status == "done":
            done_threads += 1
        cb_at = t_row["callback_dispatched_at"]
        if cb_at is not None:
            has_dispatched_callback = True

        job_row = conn.execute(
            """
            SELECT status
            FROM jobs
            WHERE thread_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (tid,),
        ).fetchone()
        latest_job_status = str(job_row["status"]) if job_row is not None else None

        msg_rows = conn.execute(
            """
            SELECT msg_type, body, file_refs_json
            FROM messages
            WHERE thread_id = ?
            ORDER BY id ASC
            """,
            (tid,),
        ).fetchall()

        thread_refs: List[str] = []
        seen_thread_refs: set[str] = set()
        last_reply_body: Optional[str] = None
        for m_row in msg_rows:
            if m_row["msg_type"] == "reply":
                last_reply_body = m_row["body"]
            refs = json.loads(m_row["file_refs_json"] or "[]")
            for ref in refs:
                if ref not in seen_thread_refs:
                    seen_thread_refs.add(ref)
                    thread_refs.append(ref)
                if ref not in seen_consolidated:
                    seen_consolidated.add(ref)
                    consolidated_file_refs.append(ref)

        threads_summary.append(
            {
                "thread_id": tid,
                "to_workspace": str(t_row["to_workspace"]),
                "status": status,
                "subject": str(t_row["subject"]),
                "latest_job_status": latest_job_status,
                "callback_dispatched_at": cb_at,
                "last_reply_excerpt": _format_reply_excerpt(last_reply_body),
                "file_refs": thread_refs,
            }
        )

    total = len(threads_summary)
    open_threads = total - done_threads
    return {
        "batch_id": clean_batch_id,
        "total": total,
        "done_threads": done_threads,
        "open_threads": open_threads,
        "all_done": total > 0 and open_threads == 0,
        "has_dispatched_callback": has_dispatched_callback,
        "threads": threads_summary,
        "consolidated_file_refs": consolidated_file_refs,
    }


def evaluate_batch_callback(
    conn: sqlite3.Connection,
    batch_id: Optional[str],
    latest_thread_id: str,
    latest_close: bool,
) -> Dict[str, Any]:
    """Détermine si un callback retour doit être émis selon callback_mode et l'état du lot."""
    if batch_id is None:
        return {
            "emit_callback": True,
            "mode": "per_thread",
            "fail_fast": False,
            "reason": "per_thread",
            "batch_status": None,
        }

    row = conn.execute(
        "SELECT callback_mode FROM threads WHERE id = ?",
        (latest_thread_id,),
    ).fetchone()
    mode = str(row["callback_mode"]) if row is not None and row["callback_mode"] else "per_thread"
    if mode != "on_batch_complete":
        return {
            "emit_callback": True,
            "mode": "per_thread",
            "fail_fast": False,
            "reason": "per_thread",
            "batch_status": None,
        }

    summary = summarize_batch(conn, batch_id)
    if not latest_close:
        return {
            "emit_callback": True,
            "mode": "on_batch_complete",
            "fail_fast": True,
            "reason": "fail_fast",
            "batch_status": "fail_fast",
            "summary": summary,
        }
    if summary["has_dispatched_callback"]:
        return {
            "emit_callback": False,
            "mode": "on_batch_complete",
            "fail_fast": False,
            "reason": "already_dispatched",
            "batch_status": "batch_complete",
            "summary": summary,
        }
    if summary["all_done"]:
        return {
            "emit_callback": True,
            "mode": "on_batch_complete",
            "fail_fast": False,
            "reason": "batch_complete",
            "batch_status": "batch_complete",
            "summary": summary,
        }
    return {
        "emit_callback": False,
        "mode": "on_batch_complete",
        "fail_fast": False,
        "reason": "waiting_batch",
        "batch_status": "waiting_batch",
        "summary": summary,
    }
