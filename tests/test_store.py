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


class TestSchemaMigrationAndWorkspaces(StoreTestCase):
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
                "idx_jobs_target_status",
                "idx_jobs_thread_status",
                "idx_jobs_one_pending_per_thread",
            },
        )

    def test_migrates_legacy_cli_schema_idempotently(self) -> None:
        legacy_home = self.root / "legacy-home"
        legacy_home.mkdir()
        legacy_db = legacy_home / "bridge.db"
        conn = sqlite3.connect(str(legacy_db))
        try:
            conn.executescript(
                """
                CREATE TABLE workspaces (
                    slug TEXT PRIMARY KEY,
                    canonical_path TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    from_workspace TEXT NOT NULL REFERENCES workspaces(slug),
                    to_workspace TEXT NOT NULL REFERENCES workspaces(slug),
                    msg_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    approved INTEGER NOT NULL DEFAULT 0,
                    target_conversation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    sender_workspace TEXT NOT NULL REFERENCES workspaces(slug),
                    msg_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    file_refs_json TEXT NOT NULL DEFAULT '[]',
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    target_workspace TEXT NOT NULL REFERENCES workspaces(slug),
                    mode TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL DEFAULT 300,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    child_pid INTEGER,
                    target_conversation_id TEXT,
                    model TEXT,
                    effort TEXT,
                    exit_code INTEGER,
                    error_summary TEXT,
                    duration_ms INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX idx_jobs_one_active_per_thread
                    ON jobs(thread_id) WHERE status IN ('queued', 'running');
                INSERT INTO workspaces VALUES ('a', '/tmp/a', 'a', '2026-11-22T10:00:00Z', '2026-11-22T10:00:00Z');
                INSERT INTO workspaces VALUES ('b', '/tmp/b', 'b', '2026-11-22T10:00:00Z', '2026-11-22T10:00:00Z');
                INSERT INTO threads VALUES ('thr-1', 'a', 'b', 'question', 'Old', 'open', 1, 'conv-legacy-1', '2026-11-22T10:00:00Z', '2026-11-22T10:00:00Z');
                INSERT INTO messages VALUES (1, 'thr-1', 'a', 'question', 'Ping', '[]', 0, '2026-11-22T10:00:00Z');
                INSERT INTO jobs VALUES ('job-1', 'thr-1', 1, 'b', 'plan', 300, 'completed', 123, 456, 'conv-legacy-1', NULL, NULL, 0, NULL, 100, '2026-11-22T10:00:01Z', '2026-11-22T10:00:05Z', '2026-11-22T10:00:00Z');
                INSERT INTO jobs VALUES ('job-2', 'thr-1', 1, 'b', 'plan', 300, 'timed_out', 123, 456, NULL, NULL, NULL, 1, 'timeout', 30000, '2026-11-22T10:01:01Z', '2026-11-22T10:01:31Z', '2026-11-22T10:01:00Z');
                """
            )
            conn.commit()
        finally:
            conn.close()

        migrated = BridgeStore(home=legacy_home)
        # Ré-ouverture idempotente.
        migrated_again = BridgeStore(home=legacy_home)
        jobs = migrated_again.list_jobs(thread_id="thr-1", reconcile=False)
        by_id = {j["id"]: j for j in jobs}
        self.assertEqual(by_id["job-1"]["status"], "completed")
        self.assertEqual(by_id["job-1"]["action"], "send_message")
        self.assertEqual(by_id["job-2"]["status"], "failed")
        self.assertEqual(by_id["job-2"]["action"], "new_conversation")

        ws_b = migrated.get_workspace("b")
        self.assertEqual(ws_b["sidecar_status"], "unlinked")
        self.assertIsNone(ws_b["project_id"])

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
        first = self.store.register_workspace(str(self.ws_arch), project_id="proj-arch-1")
        self.assertTrue(first["created"])
        self.assertEqual(first["slug"], "gravity-architect")
        self.assertEqual(first["project_id"], "proj-arch-1")
        self.assertEqual(first["sidecar_status"], "unlinked")

        second = self.store.register_workspace(str(self.ws_arch))
        self.assertFalse(second["created"])
        self.assertEqual(second["slug"], first["slug"])
        self.assertEqual(second["project_id"], "proj-arch-1")

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

    def test_sidecar_status_unlinked_offline_online_transitions(self) -> None:
        self.store.register_workspace(str(self.ws_metrics), slug="metrics")
        self.assertEqual(self.store.get_workspace("metrics")["sidecar_status"], "unlinked")

        # Lié mais jamais démarré -> offline.
        self.store.set_workspace_sidecar("metrics", "gravity-bridge-metrics")
        self.assertEqual(self.store.get_workspace("metrics")["sidecar_status"], "offline")

        # Heartbeat récent + PID courant vivant -> online.
        now = datetime.now(timezone.utc)
        hb = self.store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            project_id="proj-metrics-1",
            now=now,
        )
        self.assertEqual(hb["sidecar_status"], "online")
        self.assertEqual(hb["project_id"], "proj-metrics-1")

        # Au-delà de SIDECAR_STALE_SECONDS -> offline.
        stale = self.store.get_workspace("metrics", now=now + timedelta(seconds=25))
        self.assertEqual(stale["sidecar_status"], "offline")

        # PID mort -> offline même avec heartbeat récent.
        dead_pid_ws = self.store.get_workspace(
            "metrics", now=now, pid_checker=lambda _pid: False
        )
        self.assertEqual(dead_pid_ws["sidecar_status"], "offline")

        # Dissociation rétrocompatible (conserve project_id si clear_project_id=False).
        kept = self.store.set_workspace_sidecar("metrics", None)
        self.assertEqual(kept["sidecar_status"], "unlinked")
        self.assertEqual(kept["project_id"], "proj-metrics-1")

        # Dissociation avec clear_project_id=True efface project_id.
        cleared = self.store.set_workspace_sidecar(
            "metrics", None, clear_project_id=True
        )
        self.assertEqual(cleared["sidecar_status"], "unlinked")
        self.assertIsNone(cleared["project_id"])


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

        inbox_1 = self.store.list_inbox("metrics", direction="inbox")
        self.assertEqual(len(inbox_1), 1)
        self.assertEqual(inbox_1[0]["unread_count"], 1)

        self.store.read_thread(tid, reader_workspace="architect", mark_read=True)
        inbox_after_arch = self.store.list_inbox("metrics", direction="inbox")
        self.assertEqual(inbox_after_arch[0]["unread_count"], 1)

        self.store.read_thread(tid, reader_workspace="metrics", mark_read=False)
        self.assertEqual(self.store.list_inbox("metrics")[0]["unread_count"], 1)

        detail = self.store.read_thread(tid, reader_workspace="metrics", mark_read=True)
        self.assertEqual(detail["marked_read_count"], 1)
        self.assertNotIn("resume_command", detail)
        self.assertEqual(detail["target_sidecar_status"], "unlinked")
        self.assertEqual(self.store.list_inbox("metrics")[0]["unread_count"], 0)
        self.assertEqual(self.store.list_inbox("metrics", unread_only=True), [])

        self.store.add_reply(tid, from_workspace="metrics", body="Schéma validé.")
        outbox_arch = self.store.list_inbox("architect", direction="outbox", unread_only=True)
        self.assertEqual(len(outbox_arch), 1)
        self.assertEqual(outbox_arch[0]["unread_count"], 1)

        wss = {w["slug"]: w for w in self.store.list_workspaces()}
        self.assertEqual(wss["architect"]["unread_count"], 1)
        self.assertEqual(wss["metrics"]["open_inbox_count"], 1)
        self.assertEqual(wss["metrics"]["unread_count"], 0)

    def test_add_reply_secret_rejection_and_dispatched_auto_completion(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            project_id="proj-m",
            now=now,
        )
        created = self.store.create_thread(
            from_workspace="architect",
            to_workspace="metrics",
            msg_type="handoff",
            subject="Lot 1",
            body="Implémenter le store.",
            approved=True,
        )
        tid = created["thread_id"]
        job = self.store.create_job(tid, created["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(job["id"], target_conversation_id="conv-abc-123", now=now)

        with self.assertRaises(SecretDetectedError):
            self.store.add_reply(
                tid,
                from_workspace="metrics",
                body="Voici " + "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6",
            )

        conv_override = "51832851-9d83-41d4-857d-3c2c2f07f016"
        rep = self.store.add_reply(
            tid,
            from_workspace="metrics",
            body="Lot terminé.",
            conversation_id=conv_override,
            close=True,
        )
        self.assertEqual(rep["completed_job_ids"], [job["id"]])
        self.assertEqual(rep["conversation_id"], conv_override)
        self.assertTrue(self.store.is_target_conversation_id(conv_override))

        finished_job = self.store.get_job(job["id"], reconcile=False)
        self.assertEqual(finished_job["status"], "completed")
        self.assertIsNotNone(finished_job["finished_at"])

        detail = self.store.read_thread(tid, reconcile=False)
        self.assertEqual(detail["target_conversation_id"], conv_override)
        self.assertEqual(detail["target_project_id"], "proj-m")


class TestJobsAndReconciliation(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.register_workspace(str(self.ws_arch), slug="architect")
        self.store.register_workspace(str(self.ws_metrics), slug="metrics")

    def test_job_lifecycle_sidecar_online_required_and_single_pending_guard(self) -> None:
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

        # Sidecar unlinked -> fail-fast.
        with self.assertRaises(DispatchBlockedError):
            self.store.create_job(tid, mid, "metrics", approve_thread=True)

        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            now=now,
        )

        # Sidecar online mais approved=False -> refus.
        with self.assertRaises(DispatchBlockedError):
            self.store.create_job(tid, mid, "metrics", now=now)

        # Approbation différée via create_job(..., approve_thread=True).
        job = self.store.create_job(
            tid,
            mid,
            "metrics",
            model="flash",
            approve_thread=True,
            now=now,
        )
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["action"], "new_conversation")
        self.assertEqual(job["model"], "flash")

        # Deuxième job pending sur le même fil rejeté atomiquement.
        with self.assertRaises(DispatchBlockedError):
            self.store.create_job(tid, mid, "metrics", now=now)

        claimed = self.store.claim_next_job(
            "metrics", sidecar_id="gravity-bridge-metrics", now=now
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["status"], "claimed")

        dispatched = self.store.mark_job_dispatched(
            job["id"],
            target_conversation_id="conv-xyz-123",
            exit_code=0,
            now=now,
        )
        self.assertEqual(dispatched["status"], "dispatched")
        self.assertEqual(dispatched["target_conversation_id"], "conv-xyz-123")

        # Une fois dispatched (non pending), une relance send_message peut être mise en file.
        job2 = self.store.create_job(tid, mid, "metrics", now=now)
        self.assertEqual(job2["status"], "queued")
        self.assertEqual(job2["action"], "send_message")
        self.assertEqual(job2["target_conversation_id"], "conv-xyz-123")

        cancelled = self.store.cancel_job(job2["id"], now=now)
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["status"], "cancelled")

    def test_reconcile_orphans_spares_dispatched_and_fails_stale_queued_claimed(self) -> None:
        proc = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
        dead_real_pid = proc.pid
        proc.wait(timeout=10)
        self.assertTrue(is_pid_alive(os.getpid()))
        self.assertFalse(is_pid_alive(dead_real_pid))

        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            now=now,
        )

        t1 = self.store.create_thread("architect", "metrics", "question", "Q1", "Body 1", approved=True)
        t2 = self.store.create_thread("architect", "metrics", "question", "Q2", "Body 2", approved=True)
        t3 = self.store.create_thread("architect", "metrics", "question", "Q3", "Body 3", approved=True)
        t4 = self.store.create_thread("architect", "metrics", "question", "Q4", "Body 4", approved=True)

        j_claimed_dead = self.store.create_job(t1["thread_id"], t1["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)

        j_dispatched_old = self.store.create_job(t2["thread_id"], t2["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(j_dispatched_old["id"], target_conversation_id="conv-stay", now=now)

        j_queued_stale = self.store.create_job(t3["thread_id"], t3["message_id"], "metrics", now=now)
        j_queued_fresh = self.store.create_job(t4["thread_id"], t4["message_id"], "metrics", now=now)

        old_45 = utc_now_iso(now - timedelta(seconds=45))
        with self.store._connect() as conn:
            with self.store._transaction(conn, immediate=True):
                conn.execute(
                    "UPDATE jobs SET created_at = ?, dispatched_at = ? WHERE id = ?",
                    (old_45, old_45, j_dispatched_old["id"]),
                )
                conn.execute(
                    "UPDATE jobs SET created_at = ? WHERE id = ?",
                    (old_45, j_queued_stale["id"]),
                )
                conn.execute(
                    "UPDATE workspaces SET sidecar_pid = ? WHERE slug = 'metrics'",
                    (dead_real_pid,),
                )

        reconciled = self.store.reconcile_orphan_jobs(now=now)
        self.assertEqual(set(reconciled), {j_claimed_dead["id"], j_queued_stale["id"]})

        for jid in reconciled:
            job = self.store.get_job(jid, reconcile=False)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error_summary"], ORPHAN_ERROR_SUMMARY)

        # dispatched et queued frais doivent rester intacts.
        self.assertEqual(
            self.store.get_job(j_dispatched_old["id"], reconcile=False)["status"], "dispatched"
        )
        self.assertEqual(
            self.store.get_job(j_queued_fresh["id"], reconcile=False)["status"], "queued"
        )

    def test_close_reopen_and_prune_done_cleans_logs(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=now
        )
        t_old = self.store.create_thread("architect", "metrics", "info", "Ancien", "Old", approved=True)
        t_recent = self.store.create_thread("architect", "metrics", "info", "Recent", "New")
        t_open = self.store.create_thread("architect", "metrics", "info", "Ouvert", "Open")

        j_old = self.store.create_job(t_old["thread_id"], t_old["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(j_old["id"], target_conversation_id="conv-old", now=now)
        self.store.complete_job(j_old["id"], status="completed", exit_code=0, now=now)

        self.store.close_thread(t_old["thread_id"])
        self.store.close_thread(t_recent["thread_id"])
        reopened = self.store.reopen_thread(t_recent["thread_id"])
        self.assertEqual(reopened["status"], "open")
        self.store.close_thread(t_recent["thread_id"])

        self.store.logs_dir.mkdir(parents=True, exist_ok=True)
        old_log = self.store.logs_dir / f"{j_old['id']}.log"
        old_log.write_text("log content", encoding="utf-8")

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
