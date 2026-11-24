"""Tests round-trip JSON-RPC 2.0 stdio du serveur MCP Gravity Bridge (Lot 3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
SERVER_SCRIPT = PLUGIN_ROOT / "mcp" / "server.py"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.store import BridgeStore, utc_now_iso  # noqa: E402
from scripts.sidecar_relay import run_relay_loop  # noqa: E402

FAKE_AGENTAPI_MCP_SOURCE = r'''import json
import os
import sys
from pathlib import Path

capture_dir = Path(os.environ["FAKE_AGENTAPI_CAPTURE_DIR"])
capture_dir.mkdir(parents=True, exist_ok=True)
scenario = json.loads(Path(os.environ["FAKE_AGENTAPI_SCENARIO"]).read_text(encoding="utf-8"))
idx = len(list(capture_dir.glob("mcp_call_*.json"))) + 1
(capture_dir / f"mcp_call_{idx}.json").write_text(
    json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}, ensure_ascii=False),
    encoding="utf-8",
)
subcmd = sys.argv[1] if len(sys.argv) > 1 else ""
conv_id = scenario.get("conversation_id", "51832851-9d83-41d4-857d-3c2c2f07f016")
if subcmd == "new-conversation":
    print(json.dumps({"response": {"newConversation": {"conversationId": conv_id}}}))
    sys.exit(0)
if subcmd == "send-message":
    print(json.dumps({"response": {"sendMessage": {"recipientId": sys.argv[-2], "content": sys.argv[-1]}}}))
    sys.exit(0)
sys.exit(1)
'''


class McpStdioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home_dir = self.root / "bridge-home"
        self.sidecars_dir = self.root / "sidecars"
        self.config_path = self.root / "config.json"
        self.ws_arch = self.root / "architect"
        self.ws_target = self.root / "gravity-metrics"
        self.ws_arch.mkdir()
        self.ws_target.mkdir()

        self.fake_agentapi = self.root / "fake_agentapi.py"
        self.fake_agentapi.write_text(FAKE_AGENTAPI_MCP_SOURCE, encoding="utf-8")
        self.scenario_file = self.root / "scenario.json"
        self.capture_dir = self.root / "captures"
        self.capture_dir.mkdir()
        self._write_scenario({"conversation_id": "51832851-9d83-41d4-857d-3c2c2f07f016"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_scenario(self, data: dict) -> None:
        self.scenario_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _run_stdio(
        self,
        messages: list[str | dict],
        *,
        extra_env: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> list[dict]:
        env = dict(os.environ)
        env.pop("GRAVITY_BRIDGE_DEPTH", None)
        env.pop("GRAVITY_HOME", None)
        env.pop("GRAVITY_BRIDGE_ANTIGRAVITY_PROJECTS_DIR", None)
        env["GRAVITY_BRIDGE_HOME"] = str(self.home_dir)
        env["GRAVITY_BRIDGE_SIDECARS_DIR"] = str(self.sidecars_dir)
        env["GRAVITY_BRIDGE_ANTIGRAVITY_CONFIG"] = str(self.config_path)
        env["GRAVITY_BRIDGE_AGENTAPI_BIN"] = str(self.fake_agentapi)
        env["FAKE_AGENTAPI_SCENARIO"] = str(self.scenario_file)
        env["FAKE_AGENTAPI_CAPTURE_DIR"] = str(self.capture_dir)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        if extra_env:
            env.update(extra_env)

        lines = [
            m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)
            for m in messages
        ]
        stdin_payload = "\n".join(lines) + "\n"
        proc = subprocess.run(
            [sys.executable, "-B", str(SERVER_SCRIPT)],
            input=stdin_payload,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(PLUGIN_ROOT),
            env=env,
            timeout=timeout,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Serveur MCP terminé avec rc={proc.returncode}, stderr={proc.stderr}",
        )
        out_lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return [json.loads(ln) for ln in out_lines]

    def _call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        resps = self._run_stdio(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            ],
            extra_env=extra_env,
        )
        self.assertEqual(len(resps), 2)
        self.assertIn("result", resps[1], f"Réponse JSON-RPC inattendue : {resps[1]}")
        return resps[1]["result"]


class TestMcpProtocolAndTools(McpStdioTestCase):
    def test_jsonrpc_handshake_errors_and_nine_tools_list(self) -> None:
        resps = self._run_stdio(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
                {"jsonrpc": "2.0", "id": 5, "method": "prompts/list", "params": {}},
                {"jsonrpc": "2.0", "id": 6, "method": "unknown/method", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "no_such_tool", "arguments": {}},
                },
                "{bad json",
            ]
        )

        self.assertEqual(len(resps), 8)
        by_id = {r.get("id"): r for r in resps if r.get("id") is not None}

        init_res = by_id[1]["result"]
        self.assertEqual(init_res["protocolVersion"], "2025-06-18")
        self.assertEqual(init_res["serverInfo"]["name"], "gravity-bridge")
        self.assertEqual(by_id[2]["result"], {})
        self.assertEqual(by_id[4]["result"], {"resources": []})
        self.assertEqual(by_id[5]["result"], {"prompts": []})

        tools = by_id[3]["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertEqual(
            tool_names,
            [
                "register_workspace",
                "link_workspace",
                "list_workspaces",
                "send_message",
                "list_inbox",
                "read_thread",
                "reply_thread",
                "dispatch_thread",
                "manage_thread",
            ],
        )
        for tool in tools:
            self.assertIn("inputSchema", tool)

        by_name = {t["name"]: t for t in tools}
        for tname in ("send_message", "dispatch_thread"):
            wf_prop = by_name[tname]["inputSchema"]["properties"].get("workflow")
            self.assertIsNotNone(wf_prop, f"workflow absent du schéma {tname}")
            self.assertEqual(wf_prop["type"], "string")
            self.assertEqual(wf_prop["enum"], ["none", "quick", "standard", "deep", "evo"])

        self.assertEqual(by_id[6]["error"]["code"], -32601)
        self.assertEqual(by_id[7]["error"]["code"], -32602)
        parse_errs = [r for r in resps if r.get("id") is None]
        self.assertEqual(len(parse_errs), 1)
        self.assertEqual(parse_errs[0]["error"]["code"], -32700)

    def test_passive_mailbox_lifecycle_and_prune(self) -> None:
        r_arch = self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self.assertFalse(r_arch["isError"])
        self.assertTrue(r_arch["structuredContent"]["created"])

        r_tgt = self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        self.assertFalse(r_tgt["isError"])

        send_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "info",
                "subject": "Note d'architecture",
                "body": "Vérifier l'alignement SemVer.",
                "file_refs": ["plugin.json"],
                "dispatch": False,
            },
        )
        self.assertFalse(send_res["isError"])
        tid = send_res["structuredContent"]["thread_id"]

        inbox_1 = self._call_tool(
            "list_inbox",
            {"workspace": "metrics", "direction": "inbox", "unread_only": True},
        )
        self.assertEqual(inbox_1["structuredContent"]["count"], 1)

        read_res = self._call_tool(
            "read_thread",
            {"thread_id": tid, "reader_workspace": "metrics", "mark_read": True},
        )
        self.assertEqual(read_res["structuredContent"]["marked_read_count"], 1)
        self.assertEqual(read_res["structuredContent"]["target_sidecar_status"], "unlinked")

        rep_res = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "metrics",
                "body": "Alignement SemVer confirmé.",
                "close": True,
            },
        )
        self.assertFalse(rep_res["isError"])
        self.assertEqual(rep_res["structuredContent"]["status"], "done")

        ws_list = self._call_tool("list_workspaces", {})["structuredContent"]["workspaces"]
        by_slug = {w["slug"]: w for w in ws_list}
        self.assertEqual(by_slug["architect"]["unread_count"], 1)

        reopen_res = self._call_tool("manage_thread", {"action": "reopen", "thread_id": tid})
        self.assertEqual(reopen_res["structuredContent"]["status"], "open")
        close_res = self._call_tool("manage_thread", {"action": "close", "thread_id": tid})
        self.assertEqual(close_res["structuredContent"]["status"], "done")

        store = BridgeStore(home=self.home_dir)
        old_iso = utc_now_iso(datetime.now(timezone.utc) - timedelta(days=45))
        with store._connect() as conn:
            with store._transaction(conn, immediate=True):
                conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (old_iso, tid))

        prune_res = self._call_tool(
            "manage_thread", {"action": "prune_done", "older_than_days": 30}
        )
        self.assertEqual(prune_res["structuredContent"]["deleted_threads"], 1)

    def test_fail_fast_unlinked_offline_approval_and_anti_loop(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})

        # 1. approved=False refusé même sur question.
        unapproved = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Question non approuvée",
                "body": "Doit échouer.",
                "approved": False,
                "dispatch": True,
            },
        )
        self.assertTrue(unapproved["isError"])
        self.assertIn("approved=True", unapproved["content"][0]["text"])

        # 2. approved=True mais sidecar unlinked -> fail-fast sans créer de fil orphelin.
        unlinked_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Test unlinked",
                "body": "Doit échouer sans fil orphelin.",
                "approved": True,
                "dispatch": True,
            },
        )
        self.assertTrue(unlinked_res["isError"])
        self.assertIn("unlinked", unlinked_res["content"][0]["text"])
        self.assertEqual(
            self._call_tool("list_inbox", {"workspace": "metrics"})["structuredContent"]["count"],
            0,
        )

        # 3. link_workspace crée sidecar.json -> statut offline -> toujours refusé fail-fast.
        linked = self._call_tool(
            "link_workspace",
            {"workspace": "metrics"},
        )
        self.assertFalse(linked["isError"], linked)
        self.assertEqual(linked["structuredContent"]["sidecar_status"], "offline")
        self.assertTrue(Path(linked["structuredContent"]["sidecar_json_path"]).is_file())

        offline_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Test offline",
                "body": "Sidecar lié mais arrêté.",
                "approved": True,
                "dispatch": True,
            },
        )
        self.assertTrue(offline_res["isError"])
        self.assertIn("offline", offline_res["content"][0]["text"])
        self.assertEqual(
            self._call_tool("list_inbox", {"workspace": "metrics"})["structuredContent"]["count"],
            0,
        )

        # 4. GRAVITY_BRIDGE_DEPTH=1 bloque dispatch_true.
        store = BridgeStore(home=self.home_dir)
        store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid()
        )
        depth_blocked = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Sous-dispatch interdit",
                "body": "Bloqué.",
                "approved": True,
                "dispatch": True,
            },
            extra_env={"GRAVITY_BRIDGE_DEPTH": "1"},
        )
        self.assertTrue(depth_blocked["isError"])
        self.assertIn("anti-boucle", depth_blocked["content"][0]["text"].lower())

        # Dissociation propre via link_workspace(uninstall=True).
        unlinked = self._call_tool(
            "link_workspace",
            {"workspace": "metrics", "uninstall": True},
        )
        self.assertFalse(unlinked["isError"])
        self.assertEqual(unlinked["structuredContent"]["sidecar_status"], "unlinked")

    def test_full_cycle_send_relay_reply_followup_and_anti_loop_caller_id(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        self._call_tool("link_workspace", {"workspace": "metrics"})

        store = BridgeStore(home=self.home_dir)
        store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            project_id="proj-metrics-1",
        )

        # 1. send_message(dispatch=True, approved=True) -> job queued.
        send_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Exécution Lot 3",
                "body": "Implémenter le serveur MCP.",
                "model": "flash",
                "approved": True,
                "dispatch": True,
            },
        )
        self.assertFalse(send_res["isError"], send_res)
        sc = send_res["structuredContent"]
        tid = sc["thread_id"]
        job1_id = sc["job"]["id"]
        self.assertEqual(sc["job"]["status"], "queued")
        self.assertEqual(sc["job"]["action"], "new_conversation")

        # 2. Le micro-sidecar traite le job -> status=dispatched + conversationId enregistré.
        relay_env = {
            **os.environ,
            "FAKE_AGENTAPI_CAPTURE_DIR": str(self.capture_dir),
            "FAKE_AGENTAPI_SCENARIO": str(self.scenario_file),
            "ANTIGRAVITY_PROJECT_ID": "proj-metrics-1",
        }
        self.assertEqual(
            run_relay_loop(
                "metrics",
                home=self.home_dir,
                once=True,
                agentapi_bin=str(self.fake_agentapi),
                env=relay_env,
            ),
            0,
        )
        j1_after = self._call_tool("manage_thread", {"action": "get_job", "job_id": job1_id})[
            "structuredContent"
        ]
        self.assertEqual(j1_after["status"], "dispatched")
        self.assertEqual(
            j1_after["target_conversation_id"],
            "51832851-9d83-41d4-857d-3c2c2f07f016",
        )

        # 3. Anti-boucle caller_conversation_id : si la conversation cible tente un dispatch -> bloqué.
        loop_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "metrics",
                "to_workspace": "architect",
                "msg_type": "question",
                "subject": "Boucle",
                "body": "Interdit.",
                "approved": True,
                "dispatch": True,
                "caller_conversation_id": "51832851-9d83-41d4-857d-3c2c2f07f016",
            },
        )
        self.assertTrue(loop_res["isError"])
        self.assertIn("Sous-dispatch interdit", loop_res["content"][0]["text"])

        # 4. La conversation cible répond via reply_thread(..., dispatch=False) -> job1 passe completed.
        target_rep = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "metrics",
                "body": "Lot 3 terminé.",
                "conversation_id": "51832851-9d83-41d4-857d-3c2c2f07f016",
                "dispatch": False,
            },
        )
        self.assertFalse(target_rep["isError"], target_rep)
        self.assertEqual(target_rep["structuredContent"]["completed_job_ids"], [job1_id])
        j1_completed = self._call_tool("manage_thread", {"action": "get_job", "job_id": job1_id})[
            "structuredContent"
        ]
        self.assertEqual(j1_completed["status"], "completed")

        # 5. Relance multi-tours depuis architect -> action send_message sur la même conversation.
        followup = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "architect",
                "body": "Vérifie aussi les tests.",
                "approved": True,
                "dispatch": True,
            },
        )
        self.assertFalse(followup["isError"], followup)
        j2_id = followup["structuredContent"]["job"]["id"]
        self.assertEqual(followup["structuredContent"]["job"]["action"], "send_message")

        self.assertEqual(
            run_relay_loop(
                "metrics",
                home=self.home_dir,
                once=True,
                agentapi_bin=str(self.fake_agentapi),
                env=relay_env,
            ),
            0,
        )
        call2 = json.loads((self.capture_dir / "mcp_call_2.json").read_text(encoding="utf-8"))
        self.assertEqual(call2["argv"][0], "send-message")
        self.assertEqual(call2["argv"][-3], "--")
        self.assertEqual(call2["argv"][-2], "51832851-9d83-41d4-857d-3c2c2f07f016")

        # 6. Annulation propre d'un job créé via dispatch_thread.
        store.add_reply(tid, "metrics", "Tests vérifiés.")
        disp_later = self._call_tool(
            "dispatch_thread",
            {"thread_id": tid, "approved": True},
        )
        self.assertFalse(disp_later["isError"], disp_later)
        j3_id = disp_later["structuredContent"]["job"]["id"]
        cancelled = self._call_tool(
            "manage_thread",
            {"action": "cancel_job", "job_id": j3_id},
        )
        self.assertTrue(cancelled["structuredContent"]["cancelled"])
        self.assertEqual(cancelled["structuredContent"]["status"], "cancelled")

    def test_one_shot_callback_roundtrip_and_dispatch_thread_intent_preservation(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        self._call_tool("link_workspace", {"workspace": "architect"})
        self._call_tool("link_workspace", {"workspace": "metrics"})

        store = BridgeStore(home=self.home_dir)
        store.record_sidecar_heartbeat(
            "architect",
            sidecar_id="gravity-bridge-architect",
            pid=os.getpid(),
            project_id="proj-arch-1",
        )
        store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            project_id="proj-metrics-1",
        )

        caller_conv = "11111111-aaaa-4111-8111-111111111111"
        target_conv = "51832851-9d83-41d4-857d-3c2c2f07f016"

        # 1. send_message avec caller_conversation_id et notify_caller omis -> Auto si prêt = True.
        send_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Lot callback v0.2.0",
                "body": "Exécuter le lot.",
                "approved": True,
                "dispatch": True,
                "caller_conversation_id": caller_conv,
            },
        )
        self.assertFalse(send_res["isError"], send_res)
        sc = send_res["structuredContent"]
        tid = sc["thread_id"]
        self.assertTrue(sc["notify_caller"])
        self.assertEqual(sc["caller_conversation_id"], caller_conv)

        # 2. Relais cible traite le job outbound -> target_conv.
        relay_env = {
            **os.environ,
            "FAKE_AGENTAPI_CAPTURE_DIR": str(self.capture_dir),
            "FAKE_AGENTAPI_SCENARIO": str(self.scenario_file),
        }
        self.assertEqual(
            run_relay_loop(
                "metrics",
                home=self.home_dir,
                once=True,
                agentapi_bin=str(self.fake_agentapi),
                env=relay_env,
            ),
            0,
        )

        # 3. L'agent cible (avec GRAVITY_BRIDGE_DEPTH=1) clôture via reply_thread(..., close=True, dispatch=False).
        rep_close = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "metrics",
                "body": "Lot terminé avec succès.",
                "conversation_id": target_conv,
                "close": True,
                "dispatch": False,
            },
            extra_env={"GRAVITY_BRIDGE_DEPTH": "1"},
        )
        self.assertFalse(rep_close["isError"], rep_close)
        rep_sc = rep_close["structuredContent"]
        self.assertEqual(rep_sc["status"], "done")
        self.assertEqual(rep_sc["callback_status"], "queued")
        cb_job = rep_sc["callback_job"]
        self.assertEqual(cb_job["direction"], "callback")
        self.assertEqual(cb_job["target_workspace"], "architect")
        self.assertEqual(cb_job["target_conversation_id"], caller_conv)

        # 4. Le sidecar appelant traite le callback -> completed sans polluer l'anti-boucle.
        self.assertEqual(
            run_relay_loop(
                "architect",
                home=self.home_dir,
                once=True,
                agentapi_bin=str(self.fake_agentapi),
                env=relay_env,
            ),
            0,
        )
        cb_after = self._call_tool(
            "manage_thread", {"action": "get_job", "job_id": cb_job["id"]}
        )["structuredContent"]
        self.assertEqual(cb_after["status"], "completed")

        # 5. La conversation appelante reste autorisée à lancer un autre dispatch, mais pas target_conv.
        next_send = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Fil suivant",
                "body": "Autre question.",
                "approved": True,
                "dispatch": True,
                "caller_conversation_id": caller_conv,
            },
        )
        self.assertFalse(next_send["isError"], next_send)

        blocked_target = self._call_tool(
            "send_message",
            {
                "from_workspace": "metrics",
                "to_workspace": "architect",
                "msg_type": "question",
                "subject": "Boucle interdite",
                "body": "Doit être bloqué.",
                "approved": True,
                "dispatch": True,
                "caller_conversation_id": target_conv,
            },
        )
        self.assertTrue(blocked_target["isError"])
        self.assertIn("Sous-dispatch interdit", blocked_target["content"][0]["text"])

        # 6. dispatch_thread préserve caller_conversation_id / notify_caller si omis et permet de les basculer.
        passive = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Fil passif",
                "body": "En attente.",
                "dispatch": False,
                "caller_conversation_id": caller_conv,
                "notify_caller": True,
            },
        )
        p_tid = passive["structuredContent"]["thread_id"]
        disp1 = self._call_tool("dispatch_thread", {"thread_id": p_tid, "approved": True})
        self.assertFalse(disp1["isError"], disp1)
        self.assertEqual(disp1["structuredContent"]["caller_conversation_id"], caller_conv)
        self.assertTrue(disp1["structuredContent"]["notify_caller"])

        # Annulation puis re-dispatch avec notify_caller=False explicite.
        self._call_tool(
            "manage_thread",
            {"action": "cancel_job", "job_id": disp1["structuredContent"]["job"]["id"]},
        )
        disp2 = self._call_tool(
            "dispatch_thread",
            {"thread_id": p_tid, "approved": True, "notify_caller": False},
        )
        self.assertFalse(disp2["isError"], disp2)
        self.assertFalse(disp2["structuredContent"]["notify_caller"])
        read_p = self._call_tool("read_thread", {"thread_id": p_tid})["structuredContent"]
        self.assertFalse(read_p["notify_caller"])
        self.assertEqual(read_p["caller_conversation_id"], caller_conv)

    def test_notify_caller_warning_when_caller_offline_and_wait_seconds_on_callback(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        store = BridgeStore(home=self.home_dir)
        store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid()
        )
        caller_conv = "22222222-bbbb-4222-8222-222222222222"

        # Caller unlinked + notify_caller=True -> succès avec warning non bloquant.
        send_warn = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Warning caller unlinked",
                "body": "Travail.",
                "approved": True,
                "dispatch": True,
                "caller_conversation_id": caller_conv,
                "notify_caller": True,
            },
        )
        self.assertFalse(send_warn["isError"], send_warn)
        sc = send_warn["structuredContent"]
        self.assertTrue(sc["notify_caller"])
        self.assertTrue(sc.get("warnings"))
        self.assertIn("link architect", sc["warnings"][0])

        relay_env = {
            **os.environ,
            "FAKE_AGENTAPI_CAPTURE_DIR": str(self.capture_dir),
            "FAKE_AGENTAPI_SCENARIO": str(self.scenario_file),
        }
        self.assertEqual(
            run_relay_loop(
                "metrics",
                home=self.home_dir,
                once=True,
                agentapi_bin=str(self.fake_agentapi),
                env=relay_env,
            ),
            0,
        )

        # Clôture quand caller toujours unlinked -> skipped_sidecar_unlinked sans erreur.
        rep_skip = self._call_tool(
            "reply_thread",
            {
                "thread_id": sc["thread_id"],
                "from_workspace": "metrics",
                "body": "Fait.",
                "close": True,
                "dispatch": False,
            },
        )
        self.assertFalse(rep_skip["isError"], rep_skip)
        self.assertEqual(rep_skip["structuredContent"]["callback_status"], "skipped_sidecar_unlinked")

        # wait_seconds > 0 sur reply_thread attend la complétion du callback et retourne callback_status="dispatched".
        store.record_sidecar_heartbeat(
            "architect", sidecar_id="gravity-bridge-architect", pid=os.getpid()
        )
        t2 = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Wait callback",
                "body": "Travail.",
                "approved": True,
                "dispatch": False,
                "caller_conversation_id": caller_conv,
                "notify_caller": True,
            },
        )["structuredContent"]["thread_id"]

        # Démarre un thread d'arrière-plan qui traite le job callback pendant wait_seconds.
        import threading

        def _bg_relay() -> None:
            import time

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (
                    run_relay_loop(
                        "architect",
                        home=self.home_dir,
                        once=True,
                        agentapi_bin=str(self.fake_agentapi),
                        env=relay_env,
                    )
                    == 0
                ):
                    jobs = store.list_jobs(thread_id=t2, reconcile=False)
                    if jobs and jobs[0]["status"] == "completed":
                        return
                time.sleep(0.1)

        worker = threading.Thread(target=_bg_relay, daemon=True)
        worker.start()
        try:
            rep_wait = self._call_tool(
                "reply_thread",
                {
                    "thread_id": t2,
                    "from_workspace": "metrics",
                    "body": "Fait avec wait.",
                    "close": True,
                    "dispatch": False,
                    "wait_seconds": 5,
                },
            )
        finally:
            worker.join(timeout=5.0)

        self.assertFalse(rep_wait["isError"], rep_wait)
        w_sc = rep_wait["structuredContent"]
        self.assertEqual(w_sc["callback_status"], "dispatched")
        self.assertEqual(w_sc["callback_job"]["status"], "completed")

    def test_link_workspace_creates_dedicated_project_and_repairs_polluted_user_project(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        projects_dir = self.config_path.parent / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        raw_posix = self.ws_target.resolve().as_posix()
        uri = (
            f"file:///{raw_posix[0].lower()}%3A{raw_posix[2:]}"
            if len(raw_posix) >= 2 and raw_posix[1] == ":"
            else f"file://{raw_posix}"
        )
        user_proj_path = projects_dir / "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1.json"
        user_proj_path.write_text(
            json.dumps(
                {
                    "id": "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1",
                    "name": "gravity-metrics",
                    "projectResources": {"resources": [{"gitFolder": {"folderUri": uri}}]},
                    "settings": {"autoExecutionPolicy": "EAGER"},
                    "permissionGrants": {"permissionGrants": {}, "v2Migrated": True},
                    "sidecar": {"sidecarId": "gravity-bridge-metrics"},
                }
            ),
            encoding="utf-8",
        )

        # Un appel avec project_id pointant vers le projet utilisateur doit être refusé.
        rejected = self._call_tool(
            "link_workspace",
            {"workspace": "metrics", "project_id": "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1"},
        )
        self.assertTrue(rejected["isError"])

        linked = self._call_tool("link_workspace", {"workspace": "metrics"})
        self.assertFalse(linked["isError"], linked)
        sc = linked["structuredContent"]
        self.assertTrue(sc["project_auto_detected"])
        self.assertEqual(sc["source_project_id"], "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1")
        self.assertEqual(sc["source_project_name"], "gravity-metrics")
        self.assertNotEqual(sc["project_id"], "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1")
        self.assertEqual(sc["project_name"], "gravity-metrics (Bridge)")
        self.assertTrue(sc["bridge_project_created"])
        self.assertTrue(sc["config_updated"])
        self.assertEqual(len(sc["repaired_user_projects"]), 1)

        # Le projet utilisateur reste présent mais n'a plus la clé "sidecar".
        user_after = json.loads(user_proj_path.read_text(encoding="utf-8"))
        self.assertNotIn("sidecar", user_after)
        self.assertEqual(user_after["settings"], {"autoExecutionPolicy": "EAGER"})

        # Le projet dédié porte les ressources/réglages et la référence dans config.json.
        bridge_data = json.loads(Path(sc["bridge_project_path"]).read_text(encoding="utf-8"))
        self.assertEqual(bridge_data["name"], "gravity-metrics (Bridge)")
        self.assertEqual(bridge_data["projectResources"], user_after["projectResources"])
        self.assertEqual(bridge_data["settings"], {"autoExecutionPolicy": "EAGER"})
        self.assertEqual(bridge_data["sidecar"], {"sidecarId": "gravity-bridge-metrics"})

        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cfg["sidecars"]["gravity-bridge-metrics"],
            {
                "enabled": True,
                "projectId": sc["project_id"],
                "schemaVersion": 1,
            },
        )

        # source_project_id explicite sur ré-exécution reste idempotent sur le même UUID dédié.
        relinked = self._call_tool(
            "link_workspace",
            {"workspace": "metrics", "source_project_id": "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1"},
        )
        self.assertFalse(relinked["isError"], relinked)
        self.assertEqual(relinked["structuredContent"]["project_id"], sc["project_id"])
        self.assertFalse(relinked["structuredContent"]["bridge_project_created"])

    def test_workflow_profile_send_message_and_dispatch_thread(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        store = BridgeStore(home=self.home_dir)
        store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid()
        )

        # Défaut handoff -> "standard".
        s_handoff = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Handoff MCP défaut",
                "body": "Lot sans workflow explicite.",
                "dispatch": False,
            },
        )
        self.assertFalse(s_handoff["isError"], s_handoff)
        tid_h = s_handoff["structuredContent"]["thread_id"]
        self.assertEqual(s_handoff["structuredContent"]["workflow_profile"], "standard")

        # Rejet profil actif sur question.
        s_bad_q = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Question workflow invalide",
                "body": "Doit échouer.",
                "workflow": "standard",
                "dispatch": False,
            },
        )
        self.assertTrue(s_bad_q["isError"])
        self.assertIn("interdit", s_bad_q["content"][0]["text"])

        # dispatch_thread sans workflow préserve l'existant ("standard").
        d1 = self._call_tool("dispatch_thread", {"thread_id": tid_h, "approved": True})
        self.assertFalse(d1["isError"], d1)
        self.assertEqual(d1["structuredContent"]["workflow_profile"], "standard")
        self._call_tool(
            "manage_thread",
            {"action": "cancel_job", "job_id": d1["structuredContent"]["job"]["id"]},
        )

        # dispatch_thread avec workflow="evo" met à jour threads.workflow_profile.
        d2 = self._call_tool(
            "dispatch_thread",
            {"thread_id": tid_h, "approved": True, "workflow": "evo"},
        )
        self.assertFalse(d2["isError"], d2)
        self.assertEqual(d2["structuredContent"]["workflow_profile"], "evo")
        read_h = self._call_tool("read_thread", {"thread_id": tid_h})["structuredContent"]
        self.assertEqual(read_h["workflow_profile"], "evo")

    def test_batch_send_read_and_cancel_job_by_thread_id(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})
        store = BridgeStore(home=self.home_dir)
        store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid()
        )

        bad_mode = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Batch sans ID",
                "body": "Doit échouer.",
                "callback_mode": "on_batch_complete",
            },
        )
        self.assertTrue(bad_mode["isError"])
        self.assertIn("batch_id", bad_mode["content"][0]["text"])

        b1 = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Batch MCP 1",
                "body": "Lot W2 tâche 1.",
                "file_refs": ["plugin/core/batches.py"],
                "approved": True,
                "dispatch": True,
                "batch_id": "mcp-wave-2",
                "callback_mode": "on_batch_complete",
            },
        )
        self.assertFalse(b1["isError"], b1)
        sc1 = b1["structuredContent"]
        self.assertEqual(sc1["batch_id"], "mcp-wave-2")
        self.assertEqual(sc1["callback_mode"], "on_batch_complete")
        tid1 = sc1["thread_id"]

        read_res = self._call_tool("read_thread", {"thread_id": tid1})
        self.assertFalse(read_res["isError"], read_res)
        read_sc = read_res["structuredContent"]
        self.assertEqual(read_sc["batch_id"], "mcp-wave-2")
        self.assertEqual(read_sc["batch_summary"]["total"], 1)
        self.assertEqual(
            read_sc["batch_summary"]["consolidated_file_refs"], ["plugin/core/batches.py"]
        )

        # cancel_job par thread_id uniquement.
        cancel_res = self._call_tool(
            "manage_thread",
            {"action": "cancel_job", "thread_id": tid1},
        )
        self.assertFalse(cancel_res["isError"], cancel_res)
        self.assertTrue(cancel_res["structuredContent"]["cancelled"])
        self.assertEqual(cancel_res["structuredContent"]["status"], "cancelled")

        missing_params = self._call_tool("manage_thread", {"action": "cancel_job"})
        self.assertTrue(missing_params["isError"])


if __name__ == "__main__":
    unittest.main()
