#!/usr/bin/env python3
"""Relais Sidecar Antigravity App pour Gravity Bridge (heartbeat + agentapi)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.security import (  # noqa: E402
    AGENTAPI_TIMEOUT_SECONDS,
    MAX_LOG_BYTES,
    DispatchBlockedError,
    redact_secrets,
    truncate_utf8_bytes,
    validate_conversation_id,
    validate_model,
    validate_slug,
)
from core.sidecar_manager import sidecar_id_for_slug  # noqa: E402
from core.store import BridgeStore  # noqa: E402

HEARTBEAT_INTERVAL_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 1.5
_MAX_TITLE_CHARS = 80
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _extract_cmd_from_batch(bat_path: Path) -> Optional[List[str]]:
    """Extrait l'exécutable natif et sous-commande d'un wrapper Windows .BAT agentapi."""
    try:
        content = bat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith(("@echo", "echo", "rem ", "::")):
            continue
        try:
            tokens = shlex.split(line, posix=False)
        except ValueError:
            continue
        if not tokens:
            continue
        cleaned = [
            tok.strip('"').strip("'")
            for tok in tokens
            if not (tok.startswith("%") and tok.endswith("%")) and tok != "%*"
        ]
        if not cleaned:
            continue
        exe_candidate = Path(cleaned[0])
        if exe_candidate.is_file() and exe_candidate.suffix.lower() not in (".bat", ".cmd"):
            return [str(exe_candidate), *cleaned[1:]]
    return None


def _command_for_path(path_str: str) -> Tuple[List[str], bool]:
    p = Path(path_str).expanduser()
    suffix = p.suffix.lower()
    if suffix == ".py":
        return [sys.executable, "-B", str(p.resolve())], False
    if suffix in (".bat", ".cmd"):
        extracted = _extract_cmd_from_batch(p)
        if extracted:
            return extracted, False
        return [str(p.resolve())], True
    return [str(p.resolve())], False


def resolve_agentapi_command(
    agentapi_bin: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[List[str], bool]:
    """Résout la commande agentapi sans shell (contournant les .BAT Windows si possible)."""
    source_env = os.environ if env is None else env
    explicit = (
        agentapi_bin.strip()
        if isinstance(agentapi_bin, str) and agentapi_bin.strip()
        else str(source_env.get("GRAVITY_BRIDGE_AGENTAPI_BIN") or "").strip()
    )

    if explicit:
        explicit_path = Path(explicit).expanduser()
        if ("/" in explicit or "\\" in explicit) or explicit_path.exists():
            if explicit_path.is_file():
                return _command_for_path(str(explicit_path))
            raise DispatchBlockedError(
                f"Exécutable agentapi introuvable au chemin spécifié : {explicit!r}."
            )
        found_explicit = shutil.which(explicit)
        if found_explicit:
            return _command_for_path(found_explicit)
        raise DispatchBlockedError(f"Exécutable agentapi introuvable : {explicit!r}.")

    found_path = shutil.which("agentapi")
    if found_path:
        cmd, is_bat = _command_for_path(found_path)
        if not is_bat:
            return cmd, False

    env_exe = str(source_env.get("ANTIGRAVITY_AGENTAPI_EXE") or "").strip()
    if env_exe:
        env_exe_path = Path(env_exe).expanduser()
        if env_exe_path.is_file():
            cmd, is_bat = _command_for_path(str(env_exe_path))
            if not is_bat:
                return cmd, False

    if found_path:
        return _command_for_path(found_path)
    if env_exe and Path(env_exe).expanduser().is_file():
        return _command_for_path(env_exe)

    raise DispatchBlockedError(
        "Exécutable 'agentapi' introuvable dans le PATH ni via ANTIGRAVITY_AGENTAPI_EXE."
    )


def build_conversation_title(thread: Mapping[str, Any]) -> str:
    """Formate un titre court et lisible pour la barre latérale d'Antigravity App."""
    raw_subj = str(thread.get("subject") or thread.get("thread_id") or "Fil Gravity Bridge")
    collapsed = re.sub(r"[\r\n\t]+", " ", raw_subj).strip()
    title = f"[Bridge] {collapsed}"
    if len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 3].rstrip() + "..."
    return title


def build_prompt(
    thread: Mapping[str, Any],
    message: Mapping[str, Any],
    *,
    is_followup: bool = False,
) -> str:
    """Construit le prompt UTF-8 autonome injecté dans le Projet B via agentapi (§6.3)."""
    header = (
        f"[GRAVITY-BRIDGE | thread={thread['thread_id']} "
        f"| type={thread['msg_type']} | from={thread['from_workspace']} "
        f"| to={thread['to_workspace']} | depth=1]"
    )
    approval_label = "pré-approuvé" if thread.get("approved") else "non pré-approuvé"
    lines: List[str] = [header]
    if is_followup:
        lines.append(f"Relance multi-tours sur le fil existant (message #{message['id']}).")
    lines.extend(
        [
            f"Objet : {thread['subject']}",
            f"Statut approbation : {approval_label}",
        ]
    )

    file_refs = message.get("file_refs") or []
    if file_refs:
        lines.append(f"Références fichiers : {', '.join(str(r) for r in file_refs)}")

    if thread.get("msg_type") in ("question", "info"):
        posture = (
            "Consigne stricte : mode lecture seule uniquement. "
            "Ne modifier aucun fichier et n'effectuer ni commit ni push."
        )
    else:
        posture = (
            "Consigne d'exécution : exécuter le lot demandé dans le périmètre et sous les règles "
            "(AGENTS.md, branche dev, vérifications obligatoires) du dépôt courant."
        )

    lines.extend(
        [
            posture,
            "",
            str(message.get("body") or "").rstrip(),
            "",
            "---",
            "Contrat de retour obligatoire :",
            "- Respecter strictement les fichiers AGENTS.md, règles et vérifications du dépôt courant.",
            "- INTERDICTION d'ouvrir un sous-dispatch gravity-bridge (depth=1, dispatch=false obligatoire).",
            "- Clôturer en appelant l'outil MCP `reply_thread` (`gravity-bridge_bridge`) avec :",
            f"  - thread_id = \"{thread['thread_id']}\"",
            f"  - from_workspace = \"{thread['to_workspace']}\"",
            "  - conversation_id = \"<ton Conversation ID si connu>\"",
            "  - dispatch = false (close = true si le lot handoff est terminé et validé)",
            "  - body concis : 1. Synthèse / Décision, 2. Fichiers modifiés, "
            "3. Résultat exact des vérifications / tests, 4. Points bloquants ou questions.",
        ]
    )
    return "\n".join(lines)


def build_agentapi_command(
    base_cmd: Sequence[str],
    *,
    action: str,
    title: str,
    prompt_text: str,
    target_conversation_id: Optional[str] = None,
    model: Optional[str] = None,
    is_batch_wrapper: bool = False,
) -> List[str]:
    """Construit la ligne de commande agentapi sans shell selon le contrat validé."""
    clean_title = re.sub(r"[\r\n\t]+", " ", title).strip()
    effective_prompt = (
        re.sub(r"\s*[\r\n]+\s*", " | ", prompt_text).strip()
        if is_batch_wrapper
        else prompt_text
    )

    if action == "send_message" and target_conversation_id:
        clean_conv = validate_conversation_id(
            target_conversation_id, field_name="target_conversation_id"
        )
        if clean_conv:
            return [
                *base_cmd,
                "send-message",
                f"--title={clean_title}",
                "--",
                clean_conv,
                effective_prompt,
            ]

    cmd = [*base_cmd, "new-conversation", f"--title={clean_title}"]
    clean_model = validate_model(model)
    if clean_model:
        cmd.append(f"--model={clean_model}")
    cmd.extend(["--", effective_prompt])
    return cmd


def _find_conversation_id_in_obj(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for key in ("newConversation", "response", "payload"):
            if key in obj:
                nested = _find_conversation_id_in_obj(obj[key])
                if nested:
                    return nested
        for key in ("conversationId", "conversation_id"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in obj.values():
            if isinstance(val, (dict, list)):
                nested = _find_conversation_id_in_obj(val)
                if nested:
                    return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = _find_conversation_id_in_obj(item)
            if nested:
                return nested
    return None


def _extract_conv_from_text(text: str) -> Optional[str]:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        found = _find_conversation_id_in_obj(parsed)
        if found:
            return validate_conversation_id(found)
    except (json.JSONDecodeError, ValueError):
        pass

    for line in stripped.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        found = _find_conversation_id_in_obj(parsed)
        if found:
            return validate_conversation_id(found)

    uuid_match = _UUID_RE.search(stripped)
    if uuid_match:
        return uuid_match.group(0)
    return None


def extract_conversation_id(
    stdout_text: str,
    new_event_contents: Sequence[str] = (),
) -> Optional[str]:
    """Extrait conversationId depuis stdout JSON d'agentapi puis les fichiers events/."""
    found = _extract_conv_from_text(stdout_text)
    if found:
        return found
    for content in new_event_contents:
        found = _extract_conv_from_text(content)
        if found:
            return found
    return None


def _snapshot_events_dir(events_dir: Optional[Path]) -> Dict[str, Tuple[int, int]]:
    if events_dir is None or not events_dir.is_dir():
        return {}
    snap: Dict[str, Tuple[int, int]] = {}
    for entry in sorted(events_dir.iterdir()):
        if entry.is_file():
            st = entry.stat()
            snap[entry.name] = (int(st.st_size), int(st.st_mtime_ns))
    return snap


def _read_new_event_contents(
    events_dir: Optional[Path],
    before: Mapping[str, Tuple[int, int]],
) -> List[str]:
    if events_dir is None or not events_dir.is_dir():
        return []
    contents: List[str] = []
    for entry in sorted(events_dir.iterdir()):
        if not entry.is_file():
            continue
        st = entry.stat()
        sig = (int(st.st_size), int(st.st_mtime_ns))
        if before.get(entry.name) == sig:
            continue
        try:
            contents.append(entry.read_text(encoding="utf-8", errors="replace")[:32768])
        except OSError:
            continue
    return contents


def write_bounded_log(
    log_path: Path,
    text: str,
    max_bytes: int = MAX_LOG_BYTES,
) -> None:
    """Écrit le log relais après caviardage des secrets et troncature UTF-8."""
    redacted, _ = redact_secrets(text or "")
    bounded, _ = truncate_utf8_bytes(redacted, max_bytes, suffix="\n[LOG TRONQUE]\n")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(bounded, encoding="utf-8")


def _invoke_agentapi(
    cmd: Sequence[str],
    *,
    cwd: str,
    timeout_s: float,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env) if env is not None else None,
            timeout=timeout_s,
            check=False,
        )
        return {
            "returncode": int(proc.returncode),
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "timed_out": False,
            "duration_ms": max(0, int((time.monotonic() - t0) * 1000)),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "returncode": -1,
            "stdout": out,
            "stderr": err or f"Timeout agentapi après {timeout_s:.1f}s.",
            "timed_out": True,
            "duration_ms": max(0, int((time.monotonic() - t0) * 1000)),
        }
    except OSError as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"Erreur exécution agentapi : {exc}",
            "timed_out": False,
            "duration_ms": max(0, int((time.monotonic() - t0) * 1000)),
        }


def _format_relay_log(
    *,
    job_id: str,
    action: str,
    cmd: Sequence[str],
    result: Mapping[str, Any],
    notes: Sequence[str] = (),
) -> str:
    parts = [
        f"job_id={job_id}",
        f"action={action}",
        f"cmd={' '.join(cmd[:-1] + ['<PROMPT>']) if cmd else ''}",
        f"returncode={result.get('returncode')}",
        f"duration_ms={result.get('duration_ms')}",
    ]
    for note in notes:
        parts.append(f"note={note}")
    stderr_txt = str(result.get("stderr") or "").strip()
    stdout_txt = str(result.get("stdout") or "").strip()
    if stderr_txt:
        parts.extend(["--- STDERR ---", stderr_txt])
    if stdout_txt:
        parts.extend(["--- STDOUT ---", stdout_txt])
    return "\n".join(parts) + "\n"


def process_one_job(
    store: BridgeStore,
    workspace_slug: str,
    *,
    sidecar_id: str,
    agentapi_bin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: float = AGENTAPI_TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Verrouille et traite le prochain job queued pour workspace_slug, ou None si vide."""
    job = store.claim_next_job(workspace_slug, sidecar_id=sidecar_id)
    if job is None:
        return None

    job_id = job["id"]
    log_path = store.logs_dir / f"{job_id}.log"
    source_env = os.environ if env is None else env

    try:
        thread = store.read_thread(job["thread_id"], mark_read=False, reconcile=False)
        message = store.get_message(int(job["message_id"]))
        base_cmd, is_batch = resolve_agentapi_command(agentapi_bin, env=source_env)

        data_dir_raw = str(source_env.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or "").strip()
        events_dir = (Path(data_dir_raw).parent / "events") if data_dir_raw else None
        events_before = _snapshot_events_dir(events_dir)

        title = build_conversation_title(thread)
        target_conv = job.get("target_conversation_id") or thread.get("target_conversation_id")
        effective_action = job["action"]
        if effective_action == "send_message" and not target_conv:
            effective_action = "new_conversation"

        prompt_text = build_prompt(
            thread,
            message,
            is_followup=(effective_action == "send_message"),
        )
        cmd = build_agentapi_command(
            base_cmd,
            action=effective_action,
            title=title,
            prompt_text=prompt_text,
            target_conversation_id=target_conv,
            model=job.get("model"),
            is_batch_wrapper=is_batch,
        )

        notes: List[str] = []
        result = _invoke_agentapi(
            cmd,
            cwd=thread["target_canonical_path"],
            timeout_s=timeout_seconds,
            env=source_env,
        )

        if (
            effective_action == "send_message"
            and result["returncode"] != 0
            and not result["timed_out"]
        ):
            notes.append(
                f"Repli new-conversation après échec send-message ({result['returncode']}): "
                f"{str(result.get('stderr') or result.get('stdout') or '').strip()[:240]}"
            )
            effective_action = "new_conversation"
            prompt_text = build_prompt(thread, message, is_followup=False)
            cmd = build_agentapi_command(
                base_cmd,
                action="new_conversation",
                title=title,
                prompt_text=prompt_text,
                model=job.get("model"),
                is_batch_wrapper=is_batch,
            )
            result = _invoke_agentapi(
                cmd,
                cwd=thread["target_canonical_path"],
                timeout_s=timeout_seconds,
                env=source_env,
            )

        new_events = _read_new_event_contents(events_dir, events_before)
        write_bounded_log(
            log_path,
            _format_relay_log(
                job_id=job_id,
                action=effective_action,
                cmd=cmd,
                result=result,
                notes=notes,
            ),
        )

        if result["returncode"] == 0:
            extracted_conv = extract_conversation_id(str(result.get("stdout") or ""), new_events)
            final_conv = extracted_conv or (
                target_conv if effective_action == "send_message" else None
            )
            return store.mark_job_dispatched(
                job_id,
                target_conversation_id=final_conv,
                exit_code=0,
            )

        err_msg = (
            str(result.get("stderr") or "").strip()
            or str(result.get("stdout") or "").strip()
            or f"Échec agentapi (rc={result['returncode']})."
        )
        return store.complete_job(
            job_id,
            status="failed",
            exit_code=int(result["returncode"]),
            error_summary=err_msg,
        )

    except Exception as exc:
        try:
            write_bounded_log(log_path, f"[RELAY EXCEPTION]\n{type(exc).__name__}: {exc}\n")
        except Exception:
            pass
        try:
            return store.complete_job(
                job_id,
                status="failed",
                error_summary=f"Erreur relais sidecar : {type(exc).__name__}: {exc}",
            )
        except Exception:
            return None


def run_relay_loop(
    workspace_slug: str,
    *,
    home: Optional[Path] = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    once: bool = False,
    max_iterations: Optional[int] = None,
    agentapi_bin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Boucle principale du micro-sidecar : heartbeat périodique + traitement des jobs."""
    clean_slug = validate_slug(workspace_slug)
    sidecar_id = sidecar_id_for_slug(clean_slug)
    source_env = os.environ if env is None else env
    project_id = str(source_env.get("ANTIGRAVITY_PROJECT_ID") or "").strip() or None
    store = BridgeStore(home=home)

    stop_requested = False

    def _handle_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    try:
        signal.signal(signal.SIGINT, _handle_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_stop)
    except ValueError:
        pass

    last_heartbeat = 0.0
    iterations = 0
    step = max(0.05, float(poll_interval))
    hb_step = max(0.2, float(heartbeat_interval))

    while not stop_requested:
        now_mono = time.monotonic()
        if iterations == 0 or (now_mono - last_heartbeat) >= hb_step:
            store.record_sidecar_heartbeat(
                clean_slug,
                sidecar_id=sidecar_id,
                pid=os.getpid(),
                project_id=project_id,
            )
            last_heartbeat = now_mono

        while not stop_requested:
            processed = process_one_job(
                store,
                clean_slug,
                sidecar_id=sidecar_id,
                agentapi_bin=agentapi_bin,
                env=source_env,
            )
            if processed is None or once:
                break

        iterations += 1
        if once or (max_iterations is not None and iterations >= max_iterations):
            break
        time.sleep(step)

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Relais Sidecar Antigravity App pour Gravity Bridge"
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Slug du workspace cible enregistré dans bridge.db.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Répertoire GRAVITY_BRIDGE_HOME optionnel.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL_SECONDS,
        help="Intervalle de scrutation SQLite en secondes (défaut 1.5).",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=HEARTBEAT_INTERVAL_SECONDS,
        help="Intervalle de heartbeat sidecar en secondes (défaut 5.0).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exécute un heartbeat et traite au plus un job avant de quitter.",
    )
    parser.add_argument(
        "--agentapi-bin",
        default=None,
        help="Chemin explicite vers l'exécutable agentapi (tests).",
    )
    args = parser.parse_args(argv)
    return run_relay_loop(
        args.workspace,
        home=args.home,
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
        once=args.once,
        agentapi_bin=args.agentapi_bin,
    )


if __name__ == "__main__":
    raise SystemExit(main())
