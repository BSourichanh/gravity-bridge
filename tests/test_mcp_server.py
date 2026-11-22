"""Tests round-trip JSON-RPC 2.0 stdio du serveur MCP Gravity Bridge (Lot 3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "gravity-bridge-plugin"
SERVER_SCRIPT = PLUGIN_ROOT / "mcp" / "server.py"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.store import BridgeStore, is_pid_alive, utc_now_iso  # noqa: E402

FAKE_AGY_MCP_SOURCE = r'''import json
import os
import sys
import time
from pathlib import Path

scenario_path = os.environ.get("FAKE_AGY_SCENARIO_FILE", "")
scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8")) if scenario_path else {}
capture_dir = os.environ.get("FAKE_AGY_CAPTURE_DIR", "")
stdin_line = sys.stdin.readline()
if capture_dir:
    cdir = Path(capture_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    idx = len(list(cdir.glob("mcp_call_*.json"))) + 1
    (cdir / f"mcp_call_{idx}.json").write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "stdin": stdin_line,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

conv_id = scenario.get("conversation_id", "conv-mcp-001")
mode = scenario.get("behavior", "success")

if mode == "slow":
    print(json.dumps({"event": "init", "conversation_id": conv_id}), flush=True)
    time.sleep(float(scenario.get("sleep_seconds", 25.0)))
    sys.exit(0)

resp = scenario.get("response", "Réponse synthétique du worker.")
print(json.dumps({"event": "init", "conversation_id": conv_id}), flush=True)
print(
    json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": conv_id,
                "status": "SUCCESS",
                "response": resp,
            },
        },
        ensure_ascii=False,
    ),
    flush=True,
)
sys.exit(0)
'''


class McpStdioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home_dir = self.root / "bridge-home"
        self.ws_arch = self.root / "architect"
        self.ws_target = self.root / "gravity-metrics"
        self.ws_arch.mkdir()
        self.ws_target.mkdir()

        self.fake_agy = self.root / "fake_agy.py"
        self.fake_agy.write_text(FAKE_AGY_MCP_SOURCE, encoding="utf-8")
        self.scenario_file = self.root / "scenario.json"
        self.capture_dir = self.root / "captures"
        self.capture_dir.mkdir()
        self._write_scenario({"behavior": "success", "conversation_id": "conv-mcp-001"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_scenario(self, data: dict) -> None:
        self.scenario_file.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _run_stdio(
        self,
        messages: list[str | dict],
        *,
        extra_env: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> list[dict]:
        env = dict(os.environ)
        env.pop("GRAVITY_BRIDGE_DEPTH", None)
        env["GRAVITY_BRIDGE_HOME"] = str(self.home_dir)
        env["GRAVITY_BRIDGE_AGY_BIN"] = str(self.fake_agy)
        env["FAKE_AGY_SCENARIO_FILE"] = str(self.scenario_file)
        env["FAKE_AGY_CAPTURE_DIR"] = str(self.capture_dir)
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

    def _wait_pids_dead(self, *pids: int | None, timeout: float = 4.0) -> None:
        deadline = time.monotonic() + timeout
        for pid in pids:
            if not pid or pid <= 0:
                continue
            while is_pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)


class TestMcpProtocolAndTools(McpStdioTestCase):
    def test_jsonrpc_handshake_errors_and_tools_list(self) -> None:
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

        # La notification n'émet aucune réponse -> 8 réponses pour 9 lignes.
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

        self.assertEqual(by_id[6]["error"]["code"], -32601)
        self.assertEqual(by_id[7]["error"]["code"], -32602)
        parse_errs = [r for r in resps if r.get("id") is None]
        self.assertEqual(len(parse_errs), 1)
        self.assertEqual(parse_errs[0]["error"]["code"], -32700)

    def test_passive_mailbox_lifecycle_and_prune(self) -> None:
        r_arch = self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self.assertFalse(r_arch["isError"])
        self.assertTrue(r_arch["structuredContent"]["created"])

        r_arch_2 = self._call_tool("register_workspace", {"path": str(self.ws_arch)})
        self.assertFalse(r_arch_2["structuredContent"]["created"])

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
        self.assertEqual(inbox_1["structuredContent"]["threads"][0]["unread_count"], 1)

        # Lecture par metrics marque comme lu.
        read_res = self._call_tool(
            "read_thread",
            {"thread_id": tid, "reader_workspace": "metrics", "mark_read": True},
        )
        self.assertEqual(read_res["structuredContent"]["marked_read_count"], 1)
        self.assertEqual(
            self._call_tool("list_inbox", {"workspace": "metrics", "unread_only": True})[
                "structuredContent"
            ]["count"],
            0,
        )

        # Réponse passive vers architect + clôture.
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

        # Antidate le fil et teste prune_done.
        store = BridgeStore(home=self.home_dir)
        old_iso = utc_now_iso(datetime.now(timezone.utc) - timedelta(days=45))
        with store._connect() as conn:
            with store._transaction(conn, immediate=True):
                conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (old_iso, tid))

        prune_res = self._call_tool(
            "manage_thread", {"action": "prune_done", "older_than_days": 30}
        )
        self.assertEqual(prune_res["structuredContent"]["deleted_threads"], 1)

    def test_dispatch_send_reply_resume_and_dispatch_thread(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})

        # 1. send_message avec dispatch=True, wait_seconds=10.
        self._write_scenario(
            {
                "behavior": "success",
                "conversation_id": "conv-stdio-101",
                "response": "Lot exécuté avec succès.",
            }
        )
        send_res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Exécution Lot 3",
                "body": "Implémenter le serveur MCP.",
                "approved": True,
                "dispatch": True,
                "wait_seconds": 10,
            },
        )
        self.assertFalse(send_res["isError"], send_res)
        sc = send_res["structuredContent"]
        self.assertEqual(sc["job"]["status"], "completed")
        self.assertEqual(sc["job"]["target_conversation_id"], "conv-stdio-101")
        tid = sc["thread_id"]
        self._wait_pids_dead(sc["job"]["pid"], sc["job"]["child_pid"])

        detail = self._call_tool("read_thread", {"thread_id": tid})["structuredContent"]
        self.assertIn("agy --conversation conv-stdio-101", detail["resume_command"])
        self.assertEqual(len(detail["messages"]), 2)

        # 2. reply_thread(..., dispatch=True) dans le sens inverse to -> from doit échouer.
        bad_dir = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "metrics",
                "body": "Tentative reverse dispatch",
                "dispatch": True,
            },
        )
        self.assertTrue(bad_dir["isError"])

        # 3. reply_thread(..., close=True, dispatch=True) doit échouer.
        bad_close = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "architect",
                "body": "Close + dispatch",
                "close": True,
                "dispatch": True,
            },
        )
        self.assertTrue(bad_close["isError"])

        # 4. reply_thread(..., dispatch=True) dans le sens from -> to reprend --conversation.
        self._write_scenario(
            {
                "behavior": "success",
                "conversation_id": "conv-stdio-101",
                "response": "Tour 2 validé.",
            }
        )
        rep_disp = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "architect",
                "body": "Précision tour 2.",
                "dispatch": True,
                "wait_seconds": 10,
            },
        )
        self.assertFalse(rep_disp["isError"], rep_disp)
        self.assertEqual(rep_disp["structuredContent"]["job"]["status"], "completed")
        self._wait_pids_dead(
            rep_disp["structuredContent"]["job"]["pid"],
            rep_disp["structuredContent"]["job"]["child_pid"],
        )

        # 5. dispatch_thread sur un handoff passif non approuvé : échoue sans approved, puis réussit avec approved=True.
        passive = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Handoff différé",
                "body": "Attente feu vert.",
                "approved": False,
                "dispatch": False,
            },
        )["structuredContent"]
        tid2 = passive["thread_id"]

        blocked_dt = self._call_tool("dispatch_thread", {"thread_id": tid2})
        self.assertTrue(blocked_dt["isError"])

        ok_dt = self._call_tool(
            "dispatch_thread",
            {"thread_id": tid2, "approved": True, "wait_seconds": 10},
        )
        self.assertFalse(ok_dt["isError"], ok_dt)
        job2 = ok_dt["structuredContent"]["job"]
        self.assertEqual(job2["status"], "completed")
        self._wait_pids_dead(job2["pid"], job2["child_pid"])

        # Vérifie get_job par job_id, par thread_id et global.
        gj_id = self._call_tool("manage_thread", {"action": "get_job", "job_id": job2["id"]})
        self.assertEqual(gj_id["structuredContent"]["id"], job2["id"])
        gj_thr = self._call_tool("manage_thread", {"action": "get_job", "thread_id": tid2})
        self.assertEqual(gj_thr["structuredContent"]["count"], 1)

        captures = sorted(self.capture_dir.glob("mcp_call_*.json"))
        self.assertEqual(len(captures), 3)
        cap2 = json.loads(captures[1].read_text(encoding="utf-8"))
        self.assertIn("--conversation", cap2["argv"])
        self.assertEqual(cap2["argv"][cap2["argv"].index("--conversation") + 1], "conv-stdio-101")

    def test_manage_thread_cancel_job_kills_detached_worker_and_child(self) -> None:
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})

        self._write_scenario({"behavior": "slow", "conversation_id": "conv-slow-mcp"})
        res = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Tâche longue",
                "body": "Attend.",
                "dispatch": True,
                "wait_seconds": 0,
            },
        )
        self.assertFalse(res["isError"], res)
        job_id = res["structuredContent"]["job"]["id"]

        store = BridgeStore(home=self.home_dir)
        deadline = time.monotonic() + 8.0
        live_job = None
        while time.monotonic() < deadline:
            live_job = store.get_job(job_id, reconcile=False)
            if live_job.get("status") == "running" and live_job.get("child_pid"):
                break
            time.sleep(0.05)

        self.assertIsNotNone(live_job)
        self.assertEqual(live_job["status"], "running")
        self.assertIsNotNone(live_job["child_pid"])

        cancel_res = self._call_tool(
            "manage_thread", {"action": "cancel_job", "job_id": job_id}
        )
        self.assertFalse(cancel_res["isError"], cancel_res)
        sc = cancel_res["structuredContent"]
        self.assertTrue(sc["terminated"])
        self.assertEqual(sc["status"], "cancelled")

        self._wait_pids_dead(live_job["child_pid"], live_job["pid"], timeout=4.0)
        self.assertFalse(is_pid_alive(live_job["child_pid"]))
        self.assertFalse(is_pid_alive(live_job["pid"]))


class TestMcpSecurityGuardrails(McpStdioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._call_tool("register_workspace", {"path": str(self.ws_arch), "slug": "architect"})
        self._call_tool("register_workspace", {"path": str(self.ws_target), "slug": "metrics"})

    def test_secret_and_unapproved_handoff_rejected_without_orphan_thread(self) -> None:
        secret_val = "AKIA" + "MCPTESTSECRET123"
        res_sec = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Secret test",
                "body": f"Clé {secret_val}",
            },
        )
        self.assertTrue(res_sec["isError"])
        err_txt = res_sec["content"][0]["text"]
        self.assertIn("aws_access_key", err_txt)
        self.assertNotIn(secret_val, err_txt)

        res_unapproved = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "handoff",
                "subject": "Handoff sans feu vert",
                "body": "Modifier main.py",
                "approved": False,
                "dispatch": True,
            },
        )
        self.assertTrue(res_unapproved["isError"])

        inbox = self._call_tool(
            "list_inbox", {"workspace": "metrics", "status": "all"}
        )["structuredContent"]
        self.assertEqual(inbox["count"], 0)

    def test_depth_one_blocks_dispatch_but_allows_passive_reply(self) -> None:
        # Crée d'abord un fil à depth=0.
        created = self._call_tool(
            "send_message",
            {
                "from_workspace": "architect",
                "to_workspace": "metrics",
                "msg_type": "question",
                "subject": "Question depth",
                "body": "Analyse demandée.",
                "dispatch": False,
            },
        )["structuredContent"]
        tid = created["thread_id"]

        # À GRAVITY_BRIDGE_DEPTH=1, tout dispatch=true doit être refusé sans créer de fil.
        blocked = self._call_tool(
            "send_message",
            {
                "from_workspace": "metrics",
                "to_workspace": "architect",
                "msg_type": "question",
                "subject": "Cascade interdite",
                "body": "Tentative boucle",
                "dispatch": True,
            },
            extra_env={"GRAVITY_BRIDGE_DEPTH": "1"},
        )
        self.assertTrue(blocked["isError"])
        self.assertEqual(
            self._call_tool("list_inbox", {"workspace": "architect", "status": "all"})[
                "structuredContent"
            ]["count"],
            0,
        )

        # En revanche, la réponse passive SQLite reste autorisée à depth=1.
        rep = self._call_tool(
            "reply_thread",
            {
                "thread_id": tid,
                "from_workspace": "metrics",
                "body": "Réponse passive depuis session enfant.",
                "dispatch": False,
            },
            extra_env={"GRAVITY_BRIDGE_DEPTH": "1"},
        )
        self.assertFalse(rep["isError"], rep)


if __name__ == "__main__":
    unittest.main()
