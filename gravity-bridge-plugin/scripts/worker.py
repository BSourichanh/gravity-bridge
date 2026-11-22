#!/usr/bin/env python3
"""Worker asynchrone exécutant une session agy stream-json pour Gravity Bridge."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.dispatcher import resolve_agy_executable, terminate_pid  # noqa: E402
from core.security import MAX_LOG_BYTES, redact_secrets, truncate_utf8_bytes  # noqa: E402
from core.store import BridgeStore  # noqa: E402

_OK_STATUSES = {"success", "ok"}


def build_prompt(thread: Dict[str, Any], message: Dict[str, Any]) -> str:
    """Construit le prompt UTF-8 autonome transmis à la session cible (§6.3)."""
    header = (
        f"[GRAVITY-BRIDGE | thread={thread['thread_id']} "
        f"| type={thread['msg_type']} | from={thread['from_workspace']}]"
    )
    approval_label = "pré-approuvé" if thread.get("approved") else "non pré-approuvé"
    lines: List[str] = [
        header,
        f"Objet : {thread['subject']}",
        f"Statut approbation : {approval_label}",
    ]
    file_refs = message.get("file_refs") or []
    if file_refs:
        lines.append(f"Références fichiers : {', '.join(str(r) for r in file_refs)}")

    lines.extend(
        [
            "",
            str(message.get("body") or "").rstrip(),
            "",
            "---",
            "Contrat de sortie obligatoire :",
            "- Respecter strictement les fichiers AGENTS.md, règles, conventions de branche (dev) et vérifications obligatoires du dépôt courant.",
            "- Ne pas lancer de sous-dispatch gravity-bridge en cascade (bloqué techniquement par GRAVITY_BRIDGE_DEPTH).",
            "- Produire une réponse concise contenant :",
            "  1. Synthèse / Décision",
            "  2. Fichiers modifiés (si applicable)",
            "  3. Résultat exact des vérifications / tests",
            "  4. Points bloquants ou questions pour l'atelier (si applicable)",
        ]
    )
    return "\n".join(lines)


def build_agy_command(
    agy_exe: str,
    *,
    mode: str,
    timeout_seconds: int,
    conversation_id: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> List[str]:
    """Construit la ligne de commande agy sans shell en mode stream-json."""
    if agy_exe.lower().endswith(".py"):
        cmd: List[str] = [sys.executable, "-B", agy_exe]
    else:
        cmd = [agy_exe]

    cmd.extend(
        [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--mode",
            mode,
            "--print-timeout",
            f"{timeout_seconds}s",
        ]
    )
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    return cmd


def build_stream_json_input(prompt_text: str) -> str:
    """Sérialise le message utilisateur NDJSON pour --input-format stream-json."""
    return json.dumps(
        {"event": "user", "message": {"content": prompt_text}},
        ensure_ascii=False,
    ) + "\n"


def _extract_error_text(raw_err: Any) -> Optional[str]:
    if raw_err is None:
        return None
    if isinstance(raw_err, str):
        stripped = raw_err.strip()
        return stripped or None
    if isinstance(raw_err, (dict, list)):
        if not raw_err:
            return None
        return json.dumps(raw_err, ensure_ascii=False)
    text = str(raw_err).strip()
    return text or None


def parse_agy_output(stdout_text: str) -> Dict[str, Any]:
    """Parse de façon résiliente la sortie NDJSON stream-json (ou JSON unique) d'agy."""
    conversation_id: Optional[str] = None
    status: Optional[str] = None
    response: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None
    usage: Optional[Dict[str, Any]] = None
    text_deltas: List[str] = []
    has_result_event = False
    single_json_obj: Optional[Dict[str, Any]] = None

    for raw_line in (stdout_text or "").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue

        ev = obj.get("event")
        if ev == "init":
            cid = obj.get("conversation_id")
            if isinstance(cid, str) and cid.strip():
                conversation_id = cid.strip()
        elif ev == "step_update":
            su = obj.get("step_update")
            if isinstance(su, dict):
                cid = su.get("conversation_id")
                if isinstance(cid, str) and cid.strip() and not conversation_id:
                    conversation_id = cid.strip()
                delta = su.get("text_delta")
                if isinstance(delta, str):
                    text_deltas.append(delta)
        elif ev == "result":
            has_result_event = True
            res_obj = obj.get("result") if isinstance(obj.get("result"), dict) else obj
            cid = res_obj.get("conversation_id") or obj.get("conversation_id")
            if isinstance(cid, str) and cid.strip():
                conversation_id = cid.strip()
            if res_obj.get("status") is not None:
                status = str(res_obj["status"])
            if res_obj.get("response") is not None:
                response = str(res_obj["response"])
            error = _extract_error_text(res_obj.get("error") or obj.get("error"))
            if isinstance(res_obj.get("duration_seconds"), (int, float)):
                duration_seconds = float(res_obj["duration_seconds"])
            if isinstance(res_obj.get("usage"), dict):
                usage = res_obj["usage"]
        elif any(k in obj for k in ("status", "response", "conversation_id", "error")):
            single_json_obj = obj

    if has_result_event:
        if response is None and text_deltas:
            response = "".join(text_deltas)
        return {
            "valid": True,
            "conversation_id": conversation_id,
            "status": status,
            "response": response,
            "error": error,
            "duration_seconds": duration_seconds,
            "usage": usage,
        }

    fallback_obj = single_json_obj
    if fallback_obj is None and (stdout_text or "").strip().startswith("{"):
        try:
            whole = json.loads(stdout_text)
            if isinstance(whole, dict) and any(
                k in whole for k in ("status", "response", "conversation_id", "error")
            ):
                fallback_obj = whole
        except (json.JSONDecodeError, ValueError):
            fallback_obj = None

    if fallback_obj is not None:
        cid = fallback_obj.get("conversation_id")
        if isinstance(cid, str) and cid.strip():
            conversation_id = cid.strip()
        if fallback_obj.get("status") is not None:
            status = str(fallback_obj["status"])
        if fallback_obj.get("response") is not None:
            response = str(fallback_obj["response"])
        error = _extract_error_text(fallback_obj.get("error"))
        if isinstance(fallback_obj.get("duration_seconds"), (int, float)):
            duration_seconds = float(fallback_obj["duration_seconds"])
        if isinstance(fallback_obj.get("usage"), dict):
            usage = fallback_obj["usage"]
        return {
            "valid": True,
            "conversation_id": conversation_id,
            "status": status,
            "response": response,
            "error": error,
            "duration_seconds": duration_seconds,
            "usage": usage,
        }

    return {
        "valid": False,
        "conversation_id": conversation_id,
        "status": status,
        "response": "".join(text_deltas) if text_deltas else None,
        "error": error,
        "duration_seconds": duration_seconds,
        "usage": usage,
    }


def is_agy_result_success(returncode: int, parsed: Dict[str, Any]) -> bool:
    """Vérifie le succès global d'une exécution agy (code retour, JSON valide, status et error)."""
    if returncode != 0 or not parsed.get("valid"):
        return False
    if parsed.get("error"):
        return False
    raw_status = parsed.get("status")
    if raw_status is None:
        return True
    return str(raw_status).strip().lower() in _OK_STATUSES


def write_bounded_log(
    log_path: Path,
    text: str,
    max_bytes: int = MAX_LOG_BYTES,
) -> None:
    """Écrit le log worker après caviardage des secrets et troncature UTF-8."""
    redacted, _ = redact_secrets(text or "")
    bounded, _ = truncate_utf8_bytes(redacted, max_bytes, suffix="\n[LOG TRONQUE]\n")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(bounded, encoding="utf-8")


def _to_text(chunk: Any) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def _format_worker_log(
    *,
    job_id: str,
    cmd: List[str],
    returncode: Optional[int],
    stdout_text: str,
    stderr_text: str,
    note: Optional[str] = None,
) -> str:
    parts = [
        f"job_id={job_id}",
        f"cmd={' '.join(cmd)}",
        f"returncode={returncode}",
    ]
    if note:
        parts.append(f"note={note}")
    if stderr_text.strip():
        parts.extend(["--- STDERR ---", stderr_text.rstrip()])
    if stdout_text.strip():
        parts.extend(["--- STDOUT ---", stdout_text.rstrip()])
    return "\n".join(parts) + "\n"


def _build_failure_summary(
    returncode: int,
    parsed: Dict[str, Any],
    stdout_text: str,
    stderr_text: str,
) -> str:
    if parsed.get("error"):
        return str(parsed["error"])
    status_val = parsed.get("status")
    if parsed.get("valid") and status_val is not None:
        base = f"Statut agy en échec ({status_val}, rc={returncode})."
        if stderr_text.strip():
            return f"{base} {stderr_text.strip()}"
        return base
    if stderr_text.strip():
        return stderr_text.strip()
    if not parsed.get("valid"):
        snippet = stdout_text.strip()[:500]
        return f"Sortie agy non JSON valide (rc={returncode}) : {snippet}"
    return f"Échec exécution agy (rc={returncode})."


def run_job(
    job_id: str,
    *,
    home: Optional[Path] = None,
    agy_bin: Optional[str] = None,
    timeout_override: Optional[float] = None,
) -> int:
    """Exécute un job queued/running via agy stream-json et persiste le résultat."""
    store = BridgeStore(home=home)
    log_path = store.logs_dir / f"{job_id}.log"

    try:
        job = store.get_job(job_id, reconcile=False)
        if job["status"] == "cancelled":
            return 0

        thread = store.read_thread(job["thread_id"], mark_read=False, reconcile=False)
        message = store.get_message(int(job["message_id"]))
        store.mark_job_running(job_id, pid=os.getpid())

        agy_exe = resolve_agy_executable(agy_bin)
        cmd = build_agy_command(
            agy_exe,
            mode=job["mode"],
            timeout_seconds=int(job["timeout_seconds"]),
            conversation_id=thread.get("target_conversation_id"),
            model=job.get("model"),
            effort=job.get("effort"),
        )
        prompt_text = build_prompt(thread, message)
        stdin_payload = build_stream_json_input(prompt_text)

        child_env = dict(os.environ)
        raw_depth = str(child_env.get("GRAVITY_BRIDGE_DEPTH") or "").strip()
        try:
            depth_val = int(raw_depth) if raw_depth else 0
        except ValueError:
            depth_val = 1
        child_env["GRAVITY_BRIDGE_DEPTH"] = str(max(1, depth_val))
        child_env["GRAVITY_BRIDGE_JOB_ID"] = job_id
        child_env["GRAVITY_BRIDGE_THREAD_ID"] = job["thread_id"]
        child_env["GRAVITY_BRIDGE_HOME"] = str(store.home_dir)
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"

        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=thread["target_canonical_path"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
        store.update_job_child_pid(job_id, int(proc.pid))

        effective_timeout = (
            float(timeout_override)
            if timeout_override is not None
            else float(int(job["timeout_seconds"]) + 15)
        )

        try:
            stdout_data, stderr_data = proc.communicate(
                input=stdin_payload,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial_out = _to_text(exc.stdout)
            partial_err = _to_text(exc.stderr)
            terminate_pid(int(proc.pid))
            try:
                proc.kill()
            except OSError:
                pass
            try:
                rem_out, rem_err = proc.communicate(timeout=5)
            except Exception:
                rem_out, rem_err = "", ""
            full_out = partial_out + _to_text(rem_out)
            full_err = partial_err + _to_text(rem_err)
            elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
            parsed_partial = parse_agy_output(full_out)

            write_bounded_log(
                log_path,
                _format_worker_log(
                    job_id=job_id,
                    cmd=cmd,
                    returncode=proc.returncode,
                    stdout_text=full_out,
                    stderr_text=full_err,
                    note=f"Timeout après {effective_timeout:.1f}s",
                ),
            )
            store.complete_job(
                job_id,
                status="timed_out",
                exit_code=proc.returncode,
                target_conversation_id=parsed_partial.get("conversation_id"),
                error_summary=f"Timeout agy après {effective_timeout:.1f}s.",
                duration_ms=elapsed_ms,
            )
            return 1

        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
        stdout_text = _to_text(stdout_data)
        stderr_text = _to_text(stderr_data)
        returncode = int(proc.returncode or 0)

        write_bounded_log(
            log_path,
            _format_worker_log(
                job_id=job_id,
                cmd=cmd,
                returncode=returncode,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            ),
        )

        parsed = parse_agy_output(stdout_text)
        if is_agy_result_success(returncode, parsed):
            latest = store.get_job(job_id, reconcile=False)
            if latest["status"] == "cancelled":
                return 0
            response_text = parsed.get("response")
            if response_text is None:
                response_text = ""
            store.record_worker_reply(
                job["thread_id"],
                job["target_workspace"],
                str(response_text),
                log_path=str(log_path),
            )
            store.complete_job(
                job_id,
                status="completed",
                exit_code=0,
                target_conversation_id=parsed.get("conversation_id"),
                duration_ms=elapsed_ms,
            )
            return 0

        err_summary = _build_failure_summary(returncode, parsed, stdout_text, stderr_text)
        store.complete_job(
            job_id,
            status="failed",
            exit_code=returncode,
            target_conversation_id=parsed.get("conversation_id"),
            error_summary=err_summary,
            duration_ms=elapsed_ms,
        )
        return 1

    except Exception as exc:
        try:
            write_bounded_log(log_path, f"[WORKER EXCEPTION]\n{type(exc).__name__}: {exc}\n")
        except Exception:
            pass
        try:
            store.complete_job(
                job_id,
                status="failed",
                error_summary=f"Erreur worker : {type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Worker Gravity Bridge pour agy stream-json")
    parser.add_argument("--job-id", required=True, help="Identifiant du job SQLite à exécuter.")
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Répertoire GRAVITY_BRIDGE_HOME optionnel.",
    )
    args = parser.parse_args(argv)
    return run_job(args.job_id, home=args.home)


if __name__ == "__main__":
    raise SystemExit(main())
