"""Dispatcher asynchrone et gestion du cycle de vie des workers Gravity Bridge."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Mapping, Optional

from core.security import (
    DEFAULT_TIMEOUT_SECONDS,
    DispatchBlockedError,
    assert_dispatch_allowed,
    resolve_effective_mode,
    validate_effort,
    validate_mode,
    validate_timeout_seconds,
    validate_wait_seconds,
)
from core.store import BridgeStore, is_pid_alive

WORKER_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "worker.py"
_PROCESS_TERMINATE = 0x0001
_KERNEL32_TERM = None


def _get_kernel32_term():
    """Initialise et met en cache kernel32 pour TerminateProcess (64-bit safe)."""
    global _KERNEL32_TERM
    if _KERNEL32_TERM is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32_TERM = k32
    return _KERNEL32_TERM


def resolve_agy_executable(
    agy_bin: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Résout le chemin de l'exécutable agy (argument, env ou PATH) sans laisser de job orphelin."""
    source_env = os.environ if env is None else env
    explicit = (
        agy_bin.strip()
        if isinstance(agy_bin, str) and agy_bin.strip()
        else str(source_env.get("GRAVITY_BRIDGE_AGY_BIN") or "").strip()
    )

    if explicit:
        explicit_path = Path(explicit).expanduser()
        if ("/" in explicit or "\\" in explicit) or explicit_path.exists():
            if explicit_path.is_file():
                return str(explicit_path.resolve())
            raise DispatchBlockedError(
                f"Exécutable agy introuvable au chemin spécifié : {explicit!r}."
            )
        found_explicit = shutil.which(explicit)
        if found_explicit:
            return found_explicit
        raise DispatchBlockedError(f"Exécutable agy introuvable : {explicit!r}.")

    found = shutil.which("agy")
    if found:
        return found
    raise DispatchBlockedError(
        "Exécutable 'agy' introuvable dans le PATH. Vérifiez l'installation d'Antigravity CLI."
    )


def build_worker_env(
    home_dir: Path,
    *,
    current_depth: int,
    job_id: str,
    thread_id: str,
    agy_bin: Optional[str] = None,
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Prépare les variables d'environnement injectées au worker détaché."""
    env_dict = dict(os.environ if base_env is None else base_env)
    env_dict["GRAVITY_BRIDGE_DEPTH"] = str(current_depth + 1)
    env_dict["GRAVITY_BRIDGE_JOB_ID"] = job_id
    env_dict["GRAVITY_BRIDGE_THREAD_ID"] = thread_id
    env_dict["GRAVITY_BRIDGE_HOME"] = str(home_dir)
    env_dict["PYTHONDONTWRITEBYTECODE"] = "1"
    env_dict["PYTHONUTF8"] = "1"
    env_dict["PYTHONIOENCODING"] = "utf-8"
    if agy_bin:
        env_dict["GRAVITY_BRIDGE_AGY_BIN"] = agy_bin
    return env_dict


def spawn_detached_worker(
    store: BridgeStore,
    *,
    job_id: str,
    thread_id: str,
    target_canonical_path: str,
    current_depth: int,
    agy_bin: Optional[str] = None,
    worker_script: Optional[Path] = None,
    base_env: Optional[Mapping[str, str]] = None,
) -> int:
    """Lance scripts/worker.py en processus détaché (zéro shell=True) et retourne son PID."""
    worker_path = (worker_script or WORKER_SCRIPT_PATH).resolve()
    if not worker_path.is_file():
        raise DispatchBlockedError(f"Script worker introuvable : {worker_path}")

    worker_env = build_worker_env(
        store.home_dir,
        current_depth=current_depth,
        job_id=job_id,
        thread_id=thread_id,
        agy_bin=agy_bin,
        base_env=base_env,
    )

    popen_kwargs: Dict[str, Any] = {
        "cwd": target_canonical_path,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": worker_env,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-B", str(worker_path), "--job-id", job_id],
        **popen_kwargs,
    )
    pid = int(proc.pid)
    # Processus volontairement détaché : libère le handle parent et évite ResourceWarning.
    handle = getattr(proc, "_handle", None)
    close_fn = getattr(handle, "Close", None)
    if callable(close_fn):
        close_fn()
    proc._child_created = False
    return pid


def dispatch_job(
    store: BridgeStore,
    *,
    thread_id: str,
    message_id: int,
    mode: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    wait_seconds: int = 0,
    approve_thread: bool = False,
    agy_bin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    worker_script: Optional[Path] = None,
    poll_interval: float = 0.25,
) -> Dict[str, Any]:
    """Crée un job et instancie worker.py de façon asynchrone (avec attente bornée optionnelle)."""
    current_depth = assert_dispatch_allowed(env)
    valid_mode = validate_mode(mode)
    valid_effort = validate_effort(effort)
    valid_timeout = validate_timeout_seconds(timeout_seconds)
    valid_wait = validate_wait_seconds(wait_seconds)
    resolved_agy = resolve_agy_executable(agy_bin, env=env)

    worker_path = (worker_script or WORKER_SCRIPT_PATH).resolve()
    if not worker_path.is_file():
        raise DispatchBlockedError(f"Script worker introuvable : {worker_path}")

    thread = store.read_thread(thread_id, mark_read=False, reconcile=True)
    effective_mode = resolve_effective_mode(thread["msg_type"], valid_mode)

    job = store.create_job(
        thread_id=thread_id,
        message_id=message_id,
        target_workspace=thread["to_workspace"],
        mode=effective_mode,
        timeout_seconds=valid_timeout,
        model=model,
        effort=valid_effort,
        approve_thread=approve_thread,
    )

    try:
        worker_pid = spawn_detached_worker(
            store,
            job_id=job["id"],
            thread_id=thread_id,
            target_canonical_path=thread["target_canonical_path"],
            current_depth=current_depth,
            agy_bin=resolved_agy,
            worker_script=worker_path,
            base_env=env,
        )
        store.set_job_pid_if_queued(job["id"], worker_pid)
    except Exception as exc:
        store.complete_job(
            job["id"],
            status="failed",
            error_summary=f"Échec lancement worker : {exc}",
        )
        raise

    if valid_wait > 0:
        deadline = time.monotonic() + valid_wait
        step = max(0.05, float(poll_interval))
        while True:
            current = store.get_job(job["id"], reconcile=True)
            if current["status"] not in ("queued", "running"):
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            time.sleep(min(step, remaining))

    return store.get_job(job["id"], reconcile=False)


def terminate_pid(pid: int) -> bool:
    """Termine proprement un processus (ou groupe de processus sur POSIX) par son PID."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if not is_pid_alive(pid):
        return False

    if os.name == "nt":
        k32 = _get_kernel32_term()
        handle = k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(k32.TerminateProcess(handle, 1))
        finally:
            k32.CloseHandle(handle)

    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False


def cancel_job(store: BridgeStore, job_id: str) -> Dict[str, Any]:
    """Annule un job actif en terminant child_pid puis pid sans laisser d'orphelin."""
    job = store.get_job(job_id, reconcile=True)
    if job["status"] not in ("queued", "running"):
        result = dict(job)
        result["terminated"] = False
        return result

    killed_pids: set[int] = set()
    child_pid = job.get("child_pid")
    if isinstance(child_pid, int) and child_pid > 0:
        terminate_pid(child_pid)
        killed_pids.add(child_pid)

    worker_pid = job.get("pid")
    if isinstance(worker_pid, int) and worker_pid > 0:
        terminate_pid(worker_pid)
        killed_pids.add(worker_pid)

    updated = store.complete_job(
        job_id,
        status="cancelled",
        error_summary="Job annulé à la demande de l'utilisateur.",
    )
    late_child_pid = updated.get("child_pid")
    if (
        isinstance(late_child_pid, int)
        and late_child_pid > 0
        and late_child_pid not in killed_pids
    ):
        terminate_pid(late_child_pid)

    result = dict(updated)
    result["terminated"] = True
    return result
