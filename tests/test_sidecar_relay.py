"""Tests unitaires du relais sidecar scripts/sidecar_relay.py (100 % stdlib)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.store import BridgeStore  # noqa: E402
from scripts.sidecar_relay import (  # noqa: E402
    build_agentapi_command,
    build_callback_prompt,
    build_prompt,
    extract_conversation_id,
    process_one_job,
    resolve_agentapi_command,
    run_relay_loop,
)

FAKE_AGENTAPI_SOURCE = r'''import json
import os
import sys
from pathlib import Path

capture_dir = Path(os.environ["FAKE_AGENTAPI_CAPTURE_DIR"])
capture_dir.mkdir(parents=True, exist_ok=True)
scenario = json.loads(Path(os.environ["FAKE_AGENTAPI_SCENARIO"]).read_text(encoding="utf-8"))

idx = len(list(capture_dir.glob("call_*.json"))) + 1
(capture_dir / f"call_{idx}.json").write_text(
    json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}, ensure_ascii=False),
    encoding="utf-8",
)

subcmd = sys.argv[1] if len(sys.argv) > 1 else ""
if subcmd == "send-message" and scenario.get("fail_send_message"):
    sys.stderr.write("conversation introuvable\n")
    sys.exit(1)

if subcmd == "new-conversation":
    conv_id = scenario.get("conversation_id", "51832851-9d83-41d4-857d-3c2c2f07f016")
    if scenario.get("emit_in_event_only"):
        ev_dir = Path(os.environ["ANTIGRAVITY_EXECUTABLE_DATA_DIR"]).parent / "events"
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / f"ev_{idx}.json").write_text(
            json.dumps({"payload": {"newConversation": {"conversationId": conv_id}}}),
            encoding="utf-8",
        )
        print("{}")
    else:
        print(
            json.dumps(
                {
                    "response": {
                        "newConversation": {
                            "prompt": sys.argv[-1],
                            "conversationId": conv_id,
                        }
                    }
                },
                ensure_ascii=False,
            )
        )
    secret = scenario.get("leak_secret_stderr")
    if secret:
        sys.stderr.write(f"debug token={secret}\n")
    sys.exit(0)

if subcmd == "send-message":
    rec_id = sys.argv[-2]
    print(
        json.dumps(
            {
                "response": {
                    "sendMessage": {
                        "recipientId": rec_id,
                        "content": sys.argv[-1],
                    }
                }
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

sys.stderr.write(f"unknown subcommand: {subcmd}\n")
sys.exit(2)
'''


class SidecarRelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home_dir = self.root / "bridge-home"
        self.store = BridgeStore(home=self.home_dir)

        self.ws_arch = self.root / "architect"
        self.ws_target = self.root / "gravity-metrics"
        self.ws_arch.mkdir()
        self.ws_target.mkdir()
        self.store.register_workspace(str(self.ws_arch), slug="architect")
        self.store.register_workspace(str(self.ws_target), slug="metrics")
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid()
        )

        self.fake_agentapi = self.root / "fake_agentapi.py"
        self.fake_agentapi.write_text(FAKE_AGENTAPI_SOURCE, encoding="utf-8")
        self.capture_dir = self.root / "captures"
        self.scenario_path = self.root / "scenario.json"
        self._write_scenario({})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_scenario(self, data: dict) -> None:
        self.scenario_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["FAKE_AGENTAPI_CAPTURE_DIR"] = str(self.capture_dir)
        env["FAKE_AGENTAPI_SCENARIO"] = str(self.scenario_path)
        env["ANTIGRAVITY_PROJECT_ID"] = "proj-metrics-99"
        data_dir = self.root / "sidecar_data" / "gravity-bridge-metrics" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        env["ANTIGRAVITY_EXECUTABLE_DATA_DIR"] = str(data_dir)
        return env

    def test_command_builder_and_windows_bat_extraction(self) -> None:
        # Vérifie que '--' précède STRICTEMENT <recipient_id> sur send-message.
        cmd_send = build_agentapi_command(
            ["agentapi"],
            action="send_message",
            title="[Bridge] Suivi",
            prompt_text="Ligne 1\nLigne 2",
            target_conversation_id="51832851-9d83-41d4-857d-3c2c2f07f016",
        )
        self.assertEqual(
            cmd_send,
            [
                "agentapi",
                "send-message",
                "--title=[Bridge] Suivi",
                "--",
                "51832851-9d83-41d4-857d-3c2c2f07f016",
                "Ligne 1\nLigne 2",
            ],
        )

        # Vérifie l'extraction du binaire natif d'un wrapper .BAT.
        fake_exe = self.root / "language_server.exe"
        fake_exe.write_bytes(b"")
        bat = self.root / "agentapi.BAT"
        bat.write_text(
            f'@echo off\n"{fake_exe}" agentapi %*\n',
            encoding="utf-8",
        )
        resolved_cmd, is_bat = resolve_agentapi_command(str(bat))
        self.assertFalse(is_bat)
        self.assertEqual(resolved_cmd, [str(fake_exe.resolve()), "agentapi"])

        # Extraction conversationId JSON et events/.
        self.assertEqual(
            extract_conversation_id('{"response":{"newConversation":{"conversationId":"abc-123"}}}'),
            "abc-123",
        )
        self.assertEqual(
            extract_conversation_id(
                "",
                ['{"payload":{"newConversation":{"conversationId":"def-456"}}}'],
            ),
            "def-456",
        )

    def test_new_conversation_send_message_fallback_and_secret_log_redaction(self) -> None:
        secret = "sk-" + "relay_secret_0123456789abcdef"
        self._write_scenario(
            {
                "conversation_id": "51832851-9d83-41d4-857d-3c2c2f07f016",
                "leak_secret_stderr": secret,
            }
        )

        created = self.store.create_thread(
            "architect",
            "metrics",
            "question",
            "Vérifier métriques",
            "Peux-tu contrôler le manifeste ?",
            file_refs=["plugin.json"],
            approved=True,
        )
        j1 = self.store.create_job(
            created["thread_id"],
            created["message_id"],
            "metrics",
            model="flash",
        )

        rc = run_relay_loop(
            "metrics",
            home=self.home_dir,
            once=True,
            agentapi_bin=str(self.fake_agentapi),
            env=self._env(),
        )
        self.assertEqual(rc, 0)

        j1_done = self.store.get_job(j1["id"], reconcile=False)
        self.assertEqual(j1_done["status"], "dispatched")
        self.assertEqual(
            j1_done["target_conversation_id"],
            "51832851-9d83-41d4-857d-3c2c2f07f016",
        )
        self.assertEqual(self.store.get_workspace("metrics")["project_id"], "proj-metrics-99")

        # Vérifie caviardage du log.
        log_text = (self.store.logs_dir / f"{j1['id']}.log").read_text(encoding="utf-8")
        self.assertNotIn(secret, log_text)
        self.assertIn("[REDACTED:generic_sk_key]", log_text)

        # Vérifie les arguments exacts passés à fake_agentapi.py.
        call1 = json.loads((self.capture_dir / "call_1.json").read_text(encoding="utf-8"))
        self.assertEqual(call1["argv"][0], "new-conversation")
        self.assertIn("--title=[Bridge] Vérifier métriques", call1["argv"])
        self.assertIn("--model=flash", call1["argv"])
        self.assertIn("depth=1", call1["argv"][-1])
        self.assertIn("mode lecture seule", call1["argv"][-1])
        self.assertIn("reply_thread", call1["argv"][-1])

        # Tour 2 : relance sur la même conversation via send-message.
        rep = self.store.add_reply(created["thread_id"], "architect", "Précision : vérifie aussi CHANGELOG.md.")
        j2 = self.store.create_job(created["thread_id"], rep["message_id"], "metrics")
        self.assertEqual(j2["action"], "send_message")

        res2 = process_one_job(
            self.store,
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            agentapi_bin=str(self.fake_agentapi),
            env=self._env(),
        )
        self.assertIsNotNone(res2)
        assert res2 is not None
        self.assertEqual(res2["status"], "dispatched")
        call2 = json.loads((self.capture_dir / "call_2.json").read_text(encoding="utf-8"))
        self.assertEqual(call2["argv"][0], "send-message")
        self.assertEqual(call2["argv"][-3], "--")
        self.assertEqual(call2["argv"][-2], "51832851-9d83-41d4-857d-3c2c2f07f016")

        # Tour 3 : si send-message échoue, repli propre vers new-conversation + events/ fallback.
        self._write_scenario(
            {
                "fail_send_message": True,
                "emit_in_event_only": True,
                "conversation_id": "99999999-8888-4777-8666-555555555555",
            }
        )
        rep3 = self.store.add_reply(created["thread_id"], "architect", "Relance après archivage.")
        j3 = self.store.create_job(created["thread_id"], rep3["message_id"], "metrics")
        res3 = process_one_job(
            self.store,
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            agentapi_bin=str(self.fake_agentapi),
            env=self._env(),
        )
        self.assertIsNotNone(res3)
        assert res3 is not None
        self.assertEqual(res3["status"], "dispatched")
        self.assertEqual(
            res3["target_conversation_id"],
            "99999999-8888-4777-8666-555555555555",
        )

    def test_callback_job_uses_caller_cwd_target_no_new_conv_fallback_and_no_antiloop_pollution(self) -> None:
        caller_conv = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        target_conv = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

        self.store.record_sidecar_heartbeat(
            "architect", sidecar_id="gravity-bridge-architect", pid=os.getpid()
        )
        created = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Callback E2E",
            "Exécuter le lot.",
            approved=True,
            caller_conversation_id=caller_conv,
        )
        tid = created["thread_id"]
        out_job = self.store.create_job(tid, created["message_id"], "metrics")
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics")
        self.store.mark_job_dispatched(out_job["id"], target_conversation_id=target_conv)

        replied = self.store.add_reply(
            tid,
            "metrics",
            "Lot validé et prêt.",
            file_refs=["plugin/core/store.py"],
            close=True,
            conversation_id=target_conv,
        )
        self.assertEqual(replied["callback_status"], "queued")
        cb_job = replied["callback_job"]

        # 1. Exécution nominale sur le sidecar de l'appelant (architect).
        cb_env = self._env()
        cb_env["ANTIGRAVITY_PROJECT_ID"] = "proj-arch-1"
        res_cb = process_one_job(
            self.store,
            "architect",
            sidecar_id="gravity-bridge-architect",
            agentapi_bin=str(self.fake_agentapi),
            env=cb_env,
        )
        self.assertIsNotNone(res_cb)
        assert res_cb is not None
        self.assertEqual(res_cb["status"], "completed")
        self.assertEqual(res_cb["direction"], "callback")
        self.assertEqual(res_cb["exit_code"], 0)

        call_cb = json.loads((self.capture_dir / "call_1.json").read_text(encoding="utf-8"))
        self.assertEqual(call_cb["argv"][0], "send-message")
        self.assertEqual(call_cb["argv"][-3], "--")
        self.assertEqual(call_cb["argv"][-2], caller_conv)
        self.assertEqual(Path(call_cb["cwd"]).resolve(), self.ws_arch.resolve())
        prompt_sent = call_cb["argv"][-1]
        self.assertTrue(
            prompt_sent.startswith(
                f"[GRAVITY-BRIDGE | CALLBACK | thread={tid} | from=metrics | status=done | depth=1]"
            )
        )
        self.assertIn("read_thread", prompt_sent)
        self.assertIn("ne pas re-dispatcher automatiquement", prompt_sent.lower())

        # threads.target_conversation_id et l'anti-boucle restent non pollués.
        detail = self.store.read_thread(tid, reconcile=False)
        self.assertEqual(detail["target_conversation_id"], target_conv)
        self.assertNotIn(caller_conv, self.store.list_target_conversation_ids())
        self.assertFalse(self.store.is_target_conversation_id(caller_conv))

        # 2. En cas d'échec send-message sur callback, AUCUN repli vers new-conversation.
        self._write_scenario({"fail_send_message": True})
        created_fail = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Callback Fail",
            "Exécuter.",
            approved=True,
            caller_conversation_id=caller_conv,
        )
        rep_fail = self.store.add_reply(
            created_fail["thread_id"],
            "metrics",
            "Clôture échec.",
            close=True,
            conversation_id=target_conv,
        )
        cb_fail_job = rep_fail["callback_job"]
        res_fail = process_one_job(
            self.store,
            "architect",
            sidecar_id="gravity-bridge-architect",
            agentapi_bin=str(self.fake_agentapi),
            env=cb_env,
        )
        self.assertIsNotNone(res_fail)
        assert res_fail is not None
        self.assertEqual(res_fail["status"], "failed")
        self.assertEqual(res_fail["exit_code"], 1)
        # Un seul appel agentapi (call_2), jamais de call_3 new-conversation.
        self.assertTrue((self.capture_dir / "call_2.json").is_file())
        self.assertFalse((self.capture_dir / "call_3.json").exists())
        call_2 = json.loads((self.capture_dir / "call_2.json").read_text(encoding="utf-8"))
        self.assertEqual(call_2["argv"][0], "send-message")
        self.assertEqual(
            self.store.read_thread(created_fail["thread_id"], reconcile=False)["target_conversation_id"],
            target_conv,
        )
        self.assertNotIn(caller_conv, self.store.list_target_conversation_ids())
        self.assertEqual(self.store.get_job(cb_fail_job["id"], reconcile=False)["status"], "failed")

    def test_callback_open_intermediate_prompt_formatting(self) -> None:
        self._write_scenario({})
        caller_conv = "cccccccc-1111-4111-8111-cccccccccccc"
        target_conv = "dddddddd-2222-4222-8222-dddddddddddd"

        self.store.record_sidecar_heartbeat(
            "architect", sidecar_id="gravity-bridge-architect", pid=os.getpid()
        )
        created = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Callback Open",
            "Exécuter le lot.",
            approved=True,
            caller_conversation_id=caller_conv,
        )
        tid = created["thread_id"]
        out_job = self.store.create_job(tid, created["message_id"], "metrics")
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics")
        self.store.mark_job_dispatched(out_job["id"], target_conversation_id=target_conv)

        replied = self.store.add_reply(
            tid,
            "metrics",
            "Blocage : choisir le schéma A ou B.",
            close=False,
            conversation_id=target_conv,
        )
        self.assertEqual(replied["status"], "open")
        self.assertEqual(replied["callback_status"], "queued")

        cb_env = self._env()
        res = process_one_job(
            self.store,
            "architect",
            sidecar_id="gravity-bridge-architect",
            agentapi_bin=str(self.fake_agentapi),
            env=cb_env,
        )
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["status"], "completed")

        call_open = json.loads((self.capture_dir / "call_1.json").read_text(encoding="utf-8"))
        prompt_sent = call_open["argv"][-1]
        self.assertTrue(
            prompt_sent.startswith(
                f"[GRAVITY-BRIDGE | CALLBACK | thread={tid} | from=metrics | status=open | depth=1]"
            )
        )
        self.assertIn("fil toujours ouvert", prompt_sent.lower())
        self.assertIn("reply_thread(..., dispatch=true)", prompt_sent)
        self.assertNotIn("Fil clôturé", prompt_sent)
        self.assertIsNone(self.store.read_thread(tid, reconcile=False)["callback_dispatched_at"])

    def test_build_prompt_workflow_injection_followup_and_no_leak(self) -> None:
        base_msg = {"id": 42, "body": "Implémenter le lot.", "file_refs": ["docs/task.md"]}
        handoff_thread = {
            "thread_id": "thr-20261124-wf1111",
            "msg_type": "handoff",
            "workflow_profile": "deep",
            "from_workspace": "architect",
            "to_workspace": "metrics",
            "subject": "Lot W1",
            "approved": True,
        }

        prompt_init = build_prompt(handoff_thread, base_msg, is_followup=False)
        self.assertTrue(prompt_init.splitlines()[0].startswith("[GRAVITY-BRIDGE | "))
        self.assertIn("/gravity-workflow deep", prompt_init)
        self.assertIn("caveman ultra", prompt_init)
        self.assertIn("last_verified.patch", prompt_init)
        self.assertIn("file_refs = [\"chemin/relatif\", ...] (1 à 20 max, jamais > 20)", prompt_init)
        self.assertIn("[START]", prompt_init)
        self.assertIn("ask_question", prompt_init)

        prompt_followup = build_prompt(handoff_thread, base_msg, is_followup=True)
        self.assertTrue(prompt_followup.splitlines()[0].startswith("[GRAVITY-BRIDGE | "))
        self.assertIn("/gravity-workflow deep", prompt_followup)
        self.assertIn("caveman ultra", prompt_followup)
        self.assertNotIn("[START]", prompt_followup)
        self.assertIn("file_refs = [\"chemin/relatif\", ...] (1 à 20 max, jamais > 20)", prompt_followup)

        # Aucune injection pour workflow_profile="none", question, info ni build_callback_prompt.
        for label, thr in (
            ("handoff_none", {**handoff_thread, "workflow_profile": "none"}),
            ("question", {**handoff_thread, "msg_type": "question", "workflow_profile": "deep"}),
            ("info", {**handoff_thread, "msg_type": "info", "workflow_profile": "standard"}),
        ):
            with self.subTest(case=label):
                p = build_prompt(thr, base_msg, is_followup=False)
                self.assertTrue(p.splitlines()[0].startswith("[GRAVITY-BRIDGE | "))
                self.assertNotIn("/gravity-workflow", p)
                self.assertNotIn("caveman ultra", p)
                self.assertIn("file_refs = [\"chemin/relatif\", ...] (1 à 20 max, jamais > 20)", p)

        cb_prompt = build_callback_prompt({**handoff_thread, "status": "done"}, base_msg)
        self.assertNotIn("/gravity-workflow", cb_prompt)
        self.assertNotIn("caveman ultra", cb_prompt)


if __name__ == "__main__":
    unittest.main()
