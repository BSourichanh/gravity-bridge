"""Tests unitaires et d'intégration du dispatcher et du worker agy (Lot 2)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "gravity-bridge-plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.dispatcher import (  # noqa: E402
    cancel_job,
    dispatch_job,
    resolve_agy_executable,
)
from core.security import (  # noqa: E402
    MAX_BODY_BYTES,
    DispatchBlockedError,
    normalize_workspace_path,
)
from core.store import BridgeStore, is_pid_alive  # noqa: E402

SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import worker as worker_mod  # noqa: E402

FAKE_AGY_SOURCE = r'''import json
import os
import sys
import time
from pathlib import Path

scenario_path = os.environ.get("FAKE_AGY_SCENARIO_FILE", "")
scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8")) if scenario_path else {}

capture_dir = os.environ.get("FAKE_AGY_CAPTURE_DIR", "")
stdin_data = sys.stdin.readline()
if capture_dir:
    cdir = Path(capture_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    idx = len(list(cdir.glob("call_*.json"))) + 1
    payload = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "stdin": stdin_data,
        "env": {
            "GRAVITY_BRIDGE_DEPTH": os.environ.get("GRAVITY_BRIDGE_DEPTH"),
            "GRAVITY_BRIDGE_JOB_ID": os.environ.get("GRAVITY_BRIDGE_JOB_ID"),
            "GRAVITY_BRIDGE_THREAD_ID": os.environ.get("GRAVITY_BRIDGE_THREAD_ID"),
            "GRAVITY_BRIDGE_HOME": os.environ.get("GRAVITY_BRIDGE_HOME"),
        },
    }
    (cdir / f"call_{idx}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

behavior = scenario.get("behavior", "success")
conv_id = scenario.get("conversation_id", "conv-fake-001")

if behavior == "timeout":
    print(json.dumps({"event": "init", "conversation_id": conv_id}), flush=True)
    time.sleep(float(scenario.get("sleep_seconds", 10.0)))
    sys.exit(0)

if behavior == "slow_success":
    print(json.dumps({"event": "init", "conversation_id": conv_id}), flush=True)
    time.sleep(float(scenario.get("sleep_seconds", 15.0)))
    print(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "SUCCESS",
                    "response": "Termine apres attente.",
                },
            }
        ),
        flush=True,
    )
    sys.exit(0)

if behavior == "nonzero_stderr_secret":
    secret_tok = scenario.get("secret", "")
    sys.stderr.write(f"Fatal error token={secret_tok}\n")
    sys.stderr.flush()
    sys.exit(2)

if behavior == "json_status_error":
    print(json.dumps({"event": "init", "conversation_id": conv_id}), flush=True)
    print(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "ERROR",
                    "error": "Permission denied in plan mode",
                },
            }
        ),
        flush=True,
    )
    sys.exit(0)

if behavior == "invalid_json":
    sys.stdout.write("Not JSON output from agy CLI\n")
    sys.stdout.flush()
    sys.exit(0)

if behavior == "single_json":
    print(
        json.dumps(
            {
                "conversation_id": conv_id,
                "status": "SUCCESS",
                "response": scenario.get("response", "Reponse JSON unique."),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    sys.exit(0)

resp = scenario.get("response", "1. Synthese\nOK")
print(json.dumps({"event": "init", "conversation_id": conv_id}), flush=True)
print(
    json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conv_id,
                "step_index": 1,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": resp,
            },
        },
        ensure_ascii=False,
    ),
    flush=True,
)
print(
    json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": conv_id,
                "status": "SUCCESS",
                "response": resp,
                "duration_seconds": 0.12,
                "usage": {"total_tokens": 42},
            },
        },
        ensure_ascii=False,
    ),
    flush=True,
)
sys.exit(0)
'''


class DispatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home_dir = self.root / "bridge-home"
        self.store = BridgeStore(home=self.home_dir)

        self.ws_arch = self.root / "architect"
        self.ws_target = self.root / "target-repo"
        self.ws_arch.mkdir()
        self.ws_target.mkdir()
        self.store.register_workspace(str(self.ws_arch), slug="architect")
        self.store.register_workspace(str(self.ws_target), slug="target")

        self.fake_agy = self.root / "fake_agy.py"
        self.fake_agy.write_text(FAKE_AGY_SOURCE, encoding="utf-8")

        self.scenario_file = self.root / "scenario.json"
        self.capture_dir = self.root / "captures"
        self.capture_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_scenario(self, data: dict) -> None:
        self.scenario_file.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _base_env(self, extra: dict | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("GRAVITY_BRIDGE_DEPTH", None)
        env["FAKE_AGY_SCENARIO_FILE"] = str(self.scenario_file)
        env["FAKE_AGY_CAPTURE_DIR"] = str(self.capture_dir)
        if extra:
            env.update(extra)
        return env

    def _wait_pids_dead(self, *pids: int | None, timeout: float = 4.0) -> None:
        deadline = time.monotonic() + timeout
        for pid in pids:
            if not pid or pid <= 0:
                continue
            while is_pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)


class TestDispatchAndWorkerIntegration(DispatcherTestCase):
    def test_nominal_question_dispatch_wait_and_env_propagation(self) -> None:
        self._write_scenario(
            {
                "behavior": "success",
                "conversation_id": "conv-plan-123",
                "response": "1. Synthese : Architecture validée.",
            }
        )
        created = self.store.create_thread(
            from_workspace="architect",
            to_workspace="target",
            msg_type="question",
            subject="Revue schema",
            body="Peux-tu analyser core/store.py ?",
            file_refs=["core/store.py", "tests/test_store.py"],
            approved=False,
        )

        job = dispatch_job(
            self.store,
            thread_id=created["thread_id"],
            message_id=created["message_id"],
            model="gemini-4-neon",
            effort="low",
            timeout_seconds=120,
            wait_seconds=10,
            agy_bin=str(self.fake_agy),
            env=self._base_env(),
            poll_interval=0.05,
        )

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["mode"], "plan")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["target_conversation_id"], "conv-plan-123")
        self._wait_pids_dead(job["pid"], job["child_pid"])

        captures = sorted(self.capture_dir.glob("call_*.json"))
        self.assertEqual(len(captures), 1)
        cap = json.loads(captures[0].read_text(encoding="utf-8"))

        argv = cap["argv"]
        self.assertIn("--input-format", argv)
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "120s")
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-neon")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertNotIn("--dangerously-skip-permissions", argv)

        self.assertEqual(
            normalize_workspace_path(cap["cwd"]),
            normalize_workspace_path(str(self.ws_target)),
        )
        self.assertEqual(cap["env"]["GRAVITY_BRIDGE_DEPTH"], "1")
        self.assertEqual(cap["env"]["GRAVITY_BRIDGE_JOB_ID"], job["id"])
        self.assertEqual(cap["env"]["GRAVITY_BRIDGE_THREAD_ID"], created["thread_id"])
        self.assertEqual(Path(cap["env"]["GRAVITY_BRIDGE_HOME"]).resolve(), self.home_dir.resolve())

        stdin_obj = json.loads(cap["stdin"])
        self.assertEqual(stdin_obj["event"], "user")
        prompt = stdin_obj["message"]["content"]
        self.assertIn(f"[GRAVITY-BRIDGE | thread={created['thread_id']}", prompt)
        self.assertIn("Objet : Revue schema", prompt)
        self.assertIn("core/store.py, tests/test_store.py", prompt)
        self.assertIn("Peux-tu analyser core/store.py ?", prompt)
        self.assertIn("Contrat de sortie obligatoire :", prompt)

        detail = self.store.read_thread(created["thread_id"])
        self.assertEqual(len(detail["messages"]), 2)
        self.assertEqual(detail["messages"][-1]["sender_workspace"], "target")
        self.assertEqual(detail["messages"][-1]["body"], "1. Synthese : Architecture validée.")
        self.assertIn("agy --conversation conv-plan-123", detail["resume_command"])

    def test_handoff_approval_and_multiturn_conversation_resume(self) -> None:
        created = self.store.create_thread(
            from_workspace="architect",
            to_workspace="target",
            msg_type="handoff",
            subject="Lot 2 implementation",
            body="Implémenter le dispatcher.",
            approved=False,
        )

        # Un handoff non approuvé ne doit pas partir en accept-edits.
        with self.assertRaises(DispatchBlockedError):
            dispatch_job(
                self.store,
                thread_id=created["thread_id"],
                message_id=created["message_id"],
                agy_bin=str(self.fake_agy),
                env=self._base_env(),
            )
        self.assertEqual(self.store.list_jobs(created["thread_id"]), [])

        # Tour 1 avec approve_thread=True.
        self._write_scenario(
            {
                "behavior": "success",
                "conversation_id": "conv-handoff-777",
                "response": "Tour 1 terminé.",
            }
        )
        job1 = dispatch_job(
            self.store,
            thread_id=created["thread_id"],
            message_id=created["message_id"],
            approve_thread=True,
            wait_seconds=10,
            agy_bin=str(self.fake_agy),
            env=self._base_env(),
            poll_interval=0.05,
        )
        self.assertEqual(job1["status"], "completed")
        self.assertEqual(job1["mode"], "accept-edits")
        self._wait_pids_dead(job1["pid"], job1["child_pid"])

        # Tour 2 : réplique + dispatch reprenant --conversation conv-handoff-777.
        reply = self.store.add_reply(
            created["thread_id"],
            from_workspace="architect",
            body="Ajoute aussi un test de timeout.",
        )
        self._write_scenario(
            {
                "behavior": "single_json",
                "conversation_id": "conv-handoff-777",
                "response": "Tour 2 terminé.",
            }
        )
        job2 = dispatch_job(
            self.store,
            thread_id=created["thread_id"],
            message_id=reply["message_id"],
            wait_seconds=10,
            agy_bin=str(self.fake_agy),
            env=self._base_env(),
            poll_interval=0.05,
        )
        self.assertEqual(job2["status"], "completed")
        self._wait_pids_dead(job2["pid"], job2["child_pid"])

        captures = sorted(self.capture_dir.glob("call_*.json"))
        self.assertEqual(len(captures), 2)
        cap2 = json.loads(captures[1].read_text(encoding="utf-8"))
        self.assertIn("--conversation", cap2["argv"])
        self.assertEqual(cap2["argv"][cap2["argv"].index("--conversation") + 1], "conv-handoff-777")
        self.assertEqual(cap2["argv"][cap2["argv"].index("--mode") + 1], "accept-edits")

    def test_depth_guard_and_missing_agy_block_before_creating_job(self) -> None:
        created = self.store.create_thread("architect", "target", "question", "Q", "Body")

        for bad_depth in ("1", "2", "invalid", "-1"):
            with self.subTest(depth=bad_depth):
                with self.assertRaises(DispatchBlockedError):
                    dispatch_job(
                        self.store,
                        thread_id=created["thread_id"],
                        message_id=created["message_id"],
                        agy_bin=str(self.fake_agy),
                        env=self._base_env({"GRAVITY_BRIDGE_DEPTH": bad_depth}),
                    )

        with self.assertRaises(DispatchBlockedError):
            resolve_agy_executable(str(self.root / "missing-agy-bin.exe"))

        with self.assertRaises(DispatchBlockedError):
            dispatch_job(
                self.store,
                thread_id=created["thread_id"],
                message_id=created["message_id"],
                agy_bin=str(self.root / "missing-agy-bin.exe"),
                env=self._base_env(),
            )

        self.assertEqual(self.store.list_jobs(created["thread_id"]), [])

    def test_large_32k_body_passes_via_stdin_without_cmdline_limit_error(self) -> None:
        self._write_scenario(
            {
                "behavior": "success",
                "conversation_id": "conv-large-01",
                "response": "Reçu 32 Ko sans erreur.",
            }
        )
        large_body = "A" * 32_500
        created = self.store.create_thread(
            "architect",
            "target",
            "question",
            "Payload proche de 32 Ko",
            large_body,
        )
        job = dispatch_job(
            self.store,
            thread_id=created["thread_id"],
            message_id=created["message_id"],
            wait_seconds=10,
            agy_bin=str(self.fake_agy),
            env=self._base_env(),
            poll_interval=0.05,
        )
        self.assertEqual(job["status"], "completed")
        self._wait_pids_dead(job["pid"], job["child_pid"])
        cap = json.loads((self.capture_dir / "call_1.json").read_text(encoding="utf-8"))
        prompt = json.loads(cap["stdin"])["message"]["content"]
        self.assertGreater(len(prompt), 32_767)
        self.assertIn(large_body, prompt)


class TestWorkerErrorsRedactionTimeoutAndCancel(DispatcherTestCase):
    def _run_direct(self, job_id: str, *, timeout_override: float | None = None) -> int:
        prev = {
            k: os.environ.get(k)
            for k in ("FAKE_AGY_SCENARIO_FILE", "FAKE_AGY_CAPTURE_DIR", "GRAVITY_BRIDGE_DEPTH")
        }
        try:
            os.environ["FAKE_AGY_SCENARIO_FILE"] = str(self.scenario_file)
            os.environ["FAKE_AGY_CAPTURE_DIR"] = str(self.capture_dir)
            os.environ.pop("GRAVITY_BRIDGE_DEPTH", None)
            return worker_mod.run_job(
                job_id,
                home=self.home_dir,
                agy_bin=str(self.fake_agy),
                timeout_override=timeout_override,
            )
        finally:
            for k, val in prev.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val

    def test_nonzero_exit_redacts_secrets_in_error_summary_and_log(self) -> None:
        secret_tok = "AKIA" + "TESTSECRET123456"
        self._write_scenario({"behavior": "nonzero_stderr_secret", "secret": secret_tok})
        created = self.store.create_thread("architect", "target", "question", "Q", "Check")
        job = self.store.create_job(created["thread_id"], created["message_id"], "target", "plan")

        rc = self._run_direct(job["id"])
        self.assertEqual(rc, 1)

        updated = self.store.get_job(job["id"], reconcile=False)
        self.assertEqual(updated["status"], "failed")
        self.assertEqual(updated["exit_code"], 2)
        self.assertNotIn(secret_tok, updated["error_summary"] or "")
        self.assertIn("[REDACTED:aws_access_key]", updated["error_summary"] or "")

        log_text = (self.home_dir / "logs" / f"{job['id']}.log").read_text(encoding="utf-8")
        self.assertNotIn(secret_tok, log_text)
        self.assertIn("[REDACTED:aws_access_key]", log_text)
        # Aucun message parasite reply ne doit être ajouté en cas d'échec.
        self.assertEqual(len(self.store.read_thread(created["thread_id"])["messages"]), 1)

    def test_json_error_status_and_invalid_json_fail_cleanly(self) -> None:
        created = self.store.create_thread("architect", "target", "question", "Q", "Check")

        self._write_scenario({"behavior": "json_status_error", "conversation_id": "conv-err-1"})
        j1 = self.store.create_job(created["thread_id"], created["message_id"], "target", "plan")
        self.assertEqual(self._run_direct(j1["id"]), 1)
        u1 = self.store.get_job(j1["id"], reconcile=False)
        self.assertEqual(u1["status"], "failed")
        self.assertEqual(u1["target_conversation_id"], "conv-err-1")
        self.assertIn("Permission denied", u1["error_summary"] or "")

        self._write_scenario({"behavior": "invalid_json"})
        j2 = self.store.create_job(created["thread_id"], created["message_id"], "target", "plan")
        self.assertEqual(self._run_direct(j2["id"]), 1)
        u2 = self.store.get_job(j2["id"], reconcile=False)
        self.assertEqual(u2["status"], "failed")
        self.assertIn("non JSON valide", u2["error_summary"] or "")
        self.assertEqual(len(self.store.read_thread(created["thread_id"])["messages"]), 1)

    def test_worker_success_redacts_secret_and_truncates_over_32kb(self) -> None:
        secret_tok = "sk-" + "worker_response_secret_0123456789"
        oversized = f"Synthèse {secret_tok}\n" + ("B" * (MAX_BODY_BYTES + 400))
        self._write_scenario(
            {
                "behavior": "success",
                "conversation_id": "conv-redact-99",
                "response": oversized,
            }
        )
        created = self.store.create_thread("architect", "target", "question", "Q", "Check")
        job = self.store.create_job(created["thread_id"], created["message_id"], "target", "plan")

        rc = self._run_direct(job["id"])
        self.assertEqual(rc, 0)
        updated = self.store.get_job(job["id"], reconcile=False)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["target_conversation_id"], "conv-redact-99")

        detail = self.store.read_thread(created["thread_id"])
        self.assertEqual(len(detail["messages"]), 2)
        reply_body = detail["messages"][-1]["body"]
        self.assertNotIn(secret_tok, reply_body)
        self.assertIn("[REDACTED:generic_sk_key]", reply_body)
        self.assertLessEqual(len(reply_body.encode("utf-8")), MAX_BODY_BYTES)

    def test_timeout_marks_timed_out_captures_init_conv_and_adds_no_reply(self) -> None:
        self._write_scenario(
            {
                "behavior": "timeout",
                "conversation_id": "conv-timeout-555",
                "sleep_seconds": 10.0,
            }
        )
        created = self.store.create_thread("architect", "target", "question", "Timeout", "Wait")
        job = self.store.create_job(
            created["thread_id"], created["message_id"], "target", "plan", timeout_seconds=30
        )

        rc = self._run_direct(job["id"], timeout_override=0.4)
        self.assertEqual(rc, 1)

        updated = self.store.get_job(job["id"], reconcile=False)
        self.assertEqual(updated["status"], "timed_out")
        self.assertEqual(updated["target_conversation_id"], "conv-timeout-555")
        self.assertIn("Timeout", updated["error_summary"] or "")
        self._wait_pids_dead(updated["child_pid"])
        self.assertFalse(is_pid_alive(updated["child_pid"]))
        self.assertEqual(len(self.store.read_thread(created["thread_id"])["messages"]), 1)

    def test_nonblocking_wait_zero_and_cancel_job_kills_worker_and_child(self) -> None:
        self._write_scenario(
            {
                "behavior": "slow_success",
                "conversation_id": "conv-slow-999",
                "sleep_seconds": 30.0,
            }
        )
        created = self.store.create_thread("architect", "target", "question", "Slow", "Long task")

        t0 = time.monotonic()
        job = dispatch_job(
            self.store,
            thread_id=created["thread_id"],
            message_id=created["message_id"],
            wait_seconds=0,
            agy_bin=str(self.fake_agy),
            env=self._base_env(),
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0)
        self.assertIn(job["status"], ("queued", "running"))

        # Attend que worker.py ait démarré fake_agy et enregistré child_pid.
        deadline = time.monotonic() + 8.0
        live_job = job
        while time.monotonic() < deadline:
            live_job = self.store.get_job(job["id"], reconcile=False)
            if live_job.get("status") == "running" and live_job.get("child_pid"):
                break
            time.sleep(0.05)

        self.assertEqual(live_job["status"], "running")
        self.assertIsNotNone(live_job["pid"])
        self.assertIsNotNone(live_job["child_pid"])

        cancelled = cancel_job(self.store, job["id"])
        self.assertTrue(cancelled["terminated"])
        self.assertEqual(cancelled["status"], "cancelled")

        self._wait_pids_dead(live_job["child_pid"], live_job["pid"], timeout=4.0)
        self.assertFalse(is_pid_alive(live_job["child_pid"]))
        self.assertFalse(is_pid_alive(live_job["pid"]))

        # Même si une clôture tardive tentait de passer le job en failed, cancelled reste préservé.
        preserved = self.store.complete_job(job["id"], status="failed", error_summary="late")
        self.assertEqual(preserved["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
