"""Tests unitaires et de concurrence SQLite WAL pour core.store (100 % stdlib)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "gravity-bridge-plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.security import (  # noqa: E402
    MAX_BODY_BYTES,
    DispatchBlockedError,
    NotFoundError,
    SecretDetectedError,
    ValidationError,
)
from core.store import (  # noqa: E402
    ORPHAN_ERROR_SUMMARY,
    BridgeStore,
    is_pid_alive,
    resolve_bridge_home,
    utc_now_iso,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home_dir = self.root / "bridge-home"
        self.store = BridgeStore(home=self.home_dir)

        self.ws_arch = self.root / "gravity-architect"
        self.ws_metrics = self.root / "gravity-metrics"
        self.ws_arch.mkdir()
        self.ws_metrics.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestSchemaAndWorkspaces(StoreTestCase):
    def test_pragmas_tables_and_indexes_initialized(self) -> None:
        with self.store._connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(int(fk_on), 1)
        self.assertEqual(int(busy_ms), 5000)
        self.assertEqual(tables, {"workspaces", "threads", "messages", "jobs"})
        self.assertEqual(
            indexes,
            {
                "idx_threads_to_status",
                "idx_threads_from_status",
                "idx_messages_thread",
                "idx_jobs_thread_status",
                "idx_jobs_one_active_per_thread",
            },
        )

    def test_resolve_bridge_home_uses_env_variable(self) -> None:
        prev = os.environ.get("GRAVITY_BRIDGE_HOME")
        try:
            custom = self.root / "env-bridge"
            os.environ["GRAVITY_BRIDGE_HOME"] = str(custom)
            self.assertEqual(resolve_bridge_home(), custom.resolve())
        finally:
            if prev is None:
                os.environ.pop("GRAVITY_BRIDGE_HOME", None)
            else:
                os.environ["GRAVITY_BRIDGE_HOME"] = prev

    def test_register_workspace_idempotence_and_windows_case_dedup(self) -> None:
        first = self.store.register_workspace(str(self.ws_arch))
        self.assertTrue(first["created"])
        self.assertEqual(first["slug"], "gravity-architect")

        second = self.store.register_workspace(str(self.ws_arch))
        self.assertFalse(second["created"])
        self.assertEqual(second["slug"], first["slug"])

        if os.name == "nt":
            alt_case = first["canonical_path"][0].lower() + first["canonical_path"][1:]
            alt_case = alt_case.replace("gravity-architect", "Gravity-Architect")
            third = self.store.register_workspace(alt_case)
            self.assertFalse(third["created"])
            self.assertEqual(third["slug"], first["slug"])

        with self.assertRaises(ValidationError):
            self.store.register_workspace(str(self.ws_arch), slug="autre-slug")

    def test_slug_collision_resolution_chain(self) -> None:
        d1 = self.root / "alpha" / "app"
        d2 = self.root / "beta" / "app"
        d3 = self.root / "sub1" / "beta" / "app"
        d4 = self.root / "sub2" / "beta" / "app"
        for d in (d1, d2, d3, d4):
            d.mkdir(parents=True)

        r1 = self.store.register_workspace(str(d1))
        r2 = self.store.register_workspace(str(d2))
        r3 = self.store.register_workspace(str(d3))
        r4 = self.store.register_workspace(str(d4))

        self.assertEqual(r1["slug"], "app")
        self.assertEqual(r2["slug"], "beta-app")
        self.assertEqual(r3["slug"], "app-2")
        self.assertEqual(r4["slug"], "app-3")

    def test_resolve_workspace_strict_slug_vs_explicit_path(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.resolve_workspace("inconnu-sans-chemin")

        auto_ws = self.store.resolve_workspace(str(self.ws_metrics))
        self.assertEqual(auto_ws["slug"], "gravity-metrics")
        self.assertTrue(auto_ws["created"])


class TestThreadsInboxAndReplies(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.register_workspace(str(self.ws_arch), slug="architect")
        self.store.register_workspace(str(self.ws_metrics), slug="metrics")

    def test_create_list_inbox_non_destructive_and_targeted_read_marking(self) -> None:
        created = self.store.create_thread(
            from_workspace="architect",
            to_workspace="metrics",
            msg_type="question",
            subject="Audit format SQLite",
            body="Peux-tu vérifier le schéma ?",
            file_refs=[r"mcp\server.py"],
            approved=False,
        )
        tid = created["thread_id"]
        self.assertTrue(tid.startswith("thr-"))
        self.assertEqual(created["file_refs"], ["mcp/server.py"])

        # list_inbox ne doit jamais modifier is_read.
        inbox_1 = self.store.list_inbox("metrics", direction="inbox")
        self.assertEqual(len(inbox_1), 1)
        self.assertEqual(inbox_1[0]["unread_count"], 1)

        # Lecture par l'émetteur lui-même : le message #1 reste non lu pour metrics.
        self.store.read_thread(tid, reader_workspace="architect", mark_read=True)
        inbox_after_arch = self.store.list_inbox("metrics", direction="inbox")
        self.assertEqual(inbox_after_arch[0]["unread_count"], 1)

        # Lecture par metrics avec mark_read=False : reste non lu.
        self.store.read_thread(tid, reader_workspace="metrics", mark_read=False)
        self.assertEqual(self.store.list_inbox("metrics")[0]["unread_count"], 1)

        # Lecture par metrics avec mark_read=True : passe à lu.
        detail = self.store.read_thread(tid, reader_workspace="metrics", mark_read=True)
        self.assertEqual(detail["marked_read_count"], 1)
        self.assertEqual(self.store.list_inbox("metrics")[0]["unread_count"], 0)
        self.assertEqual(self.store.list_inbox("metrics", unread_only=True), [])

        # Réponse de metrics : visible comme non lue dans l'outbox de architect.
        self.store.add_reply(tid, from_workspace="metrics", body="Schéma validé.")
        outbox_arch = self.store.list_inbox("architect", direction="outbox", unread_only=True)
        self.assertEqual(len(outbox_arch), 1)
        self.assertEqual(outbox_arch[0]["unread_count"], 1)

        # list_workspaces reflète correctement open_inbox_count et unread_count.
        wss = {w["slug"]: w for w in self.store.list_workspaces()}
        self.assertEqual(wss["architect"]["unread_count"], 1)
        self.assertEqual(wss["metrics"]["open_inbox_count"], 1)
        self.assertEqual(wss["metrics"]["unread_count"], 0)

    def test_add_reply_secret_rejection_and_worker_reply_redaction_truncation(self) -> None:
        created = self.store.create_thread(
            from_workspace="architect",
            to_workspace="metrics",
            msg_type="handoff",
            subject="Lot 1",
            body="Implémenter le store.",
            approved=True,
        )
        tid = created["thread_id"]

        with self.assertRaises(SecretDetectedError):
            self.store.add_reply(
                tid,
                from_workspace="metrics",
                body="Voici " + "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6",
            )

        secret_tok = "sk-" + "worker_secret_0123456789abcdef"
        oversized = f"Debut {secret_tok}\n" + ("x" * (MAX_BODY_BYTES + 500))
        log_file = str(self.home_dir / "logs" / "job-test.log")
        res = self.store.record_worker_reply(
            tid,
            sender_workspace="metrics",
            body=oversized,
            log_path=log_file,
        )
        self.assertTrue(res["truncated"])
        self.assertEqual(res["redacted_rules"], ["generic_sk_key"])

        detail = self.store.read_thread(tid)
        last_body = detail["messages"][-1]["body"]
        self.assertNotIn(secret_tok, last_body)
        self.assertIn("[REDACTED:generic_sk_key]", last_body)
        self.assertIn(log_file, last_body)
        self.assertLessEqual(len(last_body.encode("utf-8")), MAX_BODY_BYTES)


class TestJobsAndReconciliation(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.register_workspace(str(self.ws_arch), slug="architect")
        self.store.register_workspace(str(self.ws_metrics), slug="metrics")

    def test_job_lifecycle_approval_and_single_active_guard(self) -> None:
        created = self.store.create_thread(
            from_workspace="architect",
            to_workspace="metrics",
            msg_type="handoff",
            subject="Handoff approuvé plus tard",
            body="Exécuter Lot 1.",
            approved=False,
        )
        tid = created["thread_id"]
        mid = created["message_id"]

        # Refus d'accept-edits si approved=False.
        with self.assertRaises(DispatchBlockedError):
            self.store.create_job(tid, mid, "metrics", mode="accept-edits")

        # Approbation différée via create_job(..., approve_thread=True).
        job = self.store.create_job(
            tid,
            mid,
            "metrics",
            mode="accept-edits",
            timeout_seconds=120,
            model="gemini-4-neon",
            effort="medium",
            approve_thread=True,
        )
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["timeout_seconds"], 120)

        # Un deuxième job actif sur le même fil doit être rejeté atomiquement.
        with self.assertRaises(DispatchBlockedError):
            self.store.create_job(tid, mid, "metrics", mode="plan")

        running = self.store.mark_job_running(job["id"], pid=os.getpid())
        self.assertEqual(running["status"], "running")
        self.store.update_job_child_pid(job["id"], child_pid=os.getpid())

        done = self.store.complete_job(
            job["id"],
            status="completed",
            exit_code=0,
            target_conversation_id="conv-xyz-123",
            duration_ms=420,
        )
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["target_conversation_id"], "conv-xyz-123")

        detail = self.store.read_thread(tid)
        self.assertTrue(detail["approved"])
        self.assertEqual(detail["target_conversation_id"], "conv-xyz-123")
        self.assertEqual(
            detail["resume_command"],
            f'cd "{detail["target_canonical_path"]}" && agy --conversation conv-xyz-123',
        )

        # Une fois terminé, un nouveau job sur le même fil est autorisé.
        job2 = self.store.create_job(tid, mid, "metrics", mode="accept-edits")
        self.assertEqual(job2["status"], "queued")

    def test_reconcile_orphans_dead_pid_real_process_queued_grace_and_timeout(self) -> None:
        # Vérification sur un vrai processus court qui se termine.
        proc = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
        dead_real_pid = proc.pid
        proc.wait(timeout=10)
        self.assertTrue(is_pid_alive(os.getpid()))
        self.assertFalse(is_pid_alive(dead_real_pid))

        t1 = self.store.create_thread("architect", "metrics", "question", "Q1", "Body 1")
        t2 = self.store.create_thread("architect", "metrics", "question", "Q2", "Body 2")
        t3 = self.store.create_thread("architect", "metrics", "question", "Q3", "Body 3")
        t4 = self.store.create_thread("architect", "metrics", "question", "Q4", "Body 4")

        j_dead = self.store.create_job(t1["thread_id"], t1["message_id"], "metrics", mode="plan")
        j_live_timeout = self.store.create_job(
            t2["thread_id"], t2["message_id"], "metrics", mode="plan", timeout_seconds=30
        )
        j_queued_stale = self.store.create_job(
            t3["thread_id"], t3["message_id"], "metrics", mode="plan"
        )
        j_queued_fresh = self.store.create_job(
            t4["thread_id"], t4["message_id"], "metrics", mode="plan"
        )

        self.store.mark_job_running(j_dead["id"], pid=dead_real_pid)
        self.store.mark_job_running(j_live_timeout["id"], pid=os.getpid())

        # Antidate j_live_timeout de 90s et j_queued_stale de 45s.
        now = datetime.now(timezone.utc)
        old_90 = utc_now_iso(now - timedelta(seconds=90))
        old_45 = utc_now_iso(now - timedelta(seconds=45))
        with self.store._connect() as conn:
            with self.store._transaction(conn, immediate=True):
                conn.execute(
                    "UPDATE jobs SET started_at = ?, created_at = ? WHERE id = ?",
                    (old_90, old_90, j_live_timeout["id"]),
                )
                conn.execute(
                    "UPDATE jobs SET created_at = ? WHERE id = ?",
                    (old_45, j_queued_stale["id"]),
                )

        reconciled = self.store.reconcile_orphan_jobs(now=now)
        self.assertEqual(
            set(reconciled),
            {j_dead["id"], j_live_timeout["id"], j_queued_stale["id"]},
        )

        for jid in reconciled:
            job = self.store.get_job(jid, reconcile=False)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error_summary"], ORPHAN_ERROR_SUMMARY)

        fresh = self.store.get_job(j_queued_fresh["id"], reconcile=False)
        self.assertEqual(fresh["status"], "queued")

    def test_close_reopen_and_prune_done_cleans_logs(self) -> None:
        t_old = self.store.create_thread("architect", "metrics", "info", "Ancien", "Old")
        t_recent = self.store.create_thread("architect", "metrics", "info", "Recent", "New")
        t_open = self.store.create_thread("architect", "metrics", "info", "Ouvert", "Open")

        j_old = self.store.create_job(t_old["thread_id"], t_old["message_id"], "metrics", mode="plan")
        self.store.mark_job_running(j_old["id"], pid=os.getpid())
        self.store.complete_job(j_old["id"], status="completed", exit_code=0)

        self.store.close_thread(t_old["thread_id"])
        self.store.close_thread(t_recent["thread_id"])
        # Vérifie reopen_thread + re-close.
        reopened = self.store.reopen_thread(t_recent["thread_id"])
        self.assertEqual(reopened["status"], "open")
        self.store.close_thread(t_recent["thread_id"])

        self.store.logs_dir.mkdir(parents=True, exist_ok=True)
        old_log = self.store.logs_dir / f"{j_old['id']}.log"
        old_log.write_text("log content", encoding="utf-8")

        now = datetime.now(timezone.utc)
        ancient_iso = utc_now_iso(now - timedelta(days=45))
        with self.store._connect() as conn:
            with self.store._transaction(conn, immediate=True):
                conn.execute(
                    "UPDATE threads SET updated_at = ? WHERE id = ?",
                    (ancient_iso, t_old["thread_id"]),
                )

        prune_res = self.store.prune_done(older_than_days=30, now=now)
        self.assertEqual(prune_res["deleted_threads"], 1)
        self.assertEqual(prune_res["deleted_thread_ids"], [t_old["thread_id"]])
        self.assertEqual(prune_res["deleted_logs"], 1)
        self.assertFalse(old_log.exists())

        with self.assertRaises(NotFoundError):
            self.store.read_thread(t_old["thread_id"])
        self.assertEqual(self.store.read_thread(t_recent["thread_id"])["status"], "done")
        self.assertEqual(self.store.read_thread(t_open["thread_id"])["status"], "open")


class TestMultiProcessWalConcurrency(StoreTestCase):
    def test_concurrent_subprocess_writes_succeed_without_lock_errors(self) -> None:
        self.store.register_workspace(str(self.ws_arch), slug="architect")
        self.store.register_workspace(str(self.ws_metrics), slug="metrics")

        worker_code = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from core.store import BridgeStore\n"
            "store = BridgeStore(home=Path(sys.argv[2]))\n"
            "wid = sys.argv[3]\n"
            "for i in range(6):\n"
            "    res = store.create_thread('architect', 'metrics', 'info', f'Msg {wid}-{i}', f'Body {wid}-{i}')\n"
            "    store.add_reply(res['thread_id'], 'metrics', f'Ack {wid}-{i}')\n"
        )

        num_workers = 6
        iterations_per_worker = 6
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    worker_code,
                    str(PLUGIN_ROOT),
                    str(self.home_dir),
                    str(idx),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for idx in range(num_workers)
        ]

        failures = []
        for idx, proc in enumerate(procs):
            out, err = proc.communicate(timeout=30)
            if proc.returncode != 0:
                failures.append((idx, proc.returncode, out, err))

        self.assertEqual(failures, [], f"Échecs de concurrence SQLite : {failures}")

        inbox = self.store.list_inbox("metrics", direction="inbox", status="all", limit=200)
        self.assertEqual(len(inbox), num_workers * iterations_per_worker)
        for item in inbox:
            self.assertEqual(item["message_count"], 2)


if __name__ == "__main__":
    unittest.main()
