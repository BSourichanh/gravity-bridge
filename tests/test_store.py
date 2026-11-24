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
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.batches import STALE_DISPATCHED_ERROR_SUMMARY  # noqa: E402
from core.security import (  # noqa: E402
    DispatchBlockedError,
    NotFoundError,
    SecretDetectedError,
    ValidationError,
)
from core.store import (  # noqa: E402
    ORPHAN_ERROR_SUMMARY,
    BridgeStore,
    default_bridge_home,
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
                "idx_threads_batch_id",
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

    def test_migrates_v010_schema_to_v020_callback_columns_idempotently(self) -> None:
        v010_home = self.root / "v010-home"
        v010_home.mkdir()
        v010_db = v010_home / "bridge.db"
        conn = sqlite3.connect(str(v010_db))
        try:
            conn.executescript(
                """
                CREATE TABLE workspaces (
                    slug TEXT PRIMARY KEY,
                    canonical_path TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    sidecar_id TEXT,
                    sidecar_pid INTEGER,
                    sidecar_last_seen_at TEXT,
                    project_id TEXT,
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
                    action TEXT NOT NULL,
                    model TEXT,
                    sidecar_id TEXT,
                    target_conversation_id TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    error_summary TEXT,
                    claimed_at TEXT,
                    dispatched_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO workspaces VALUES ('a', '/tmp/a', 'a', NULL, NULL, NULL, NULL, '2026-11-22T10:00:00Z', '2026-11-22T10:00:00Z');
                INSERT INTO workspaces VALUES ('b', '/tmp/b', 'b', NULL, NULL, NULL, NULL, '2026-11-22T10:00:00Z', '2026-11-22T10:00:00Z');
                INSERT INTO threads VALUES ('thr-v010', 'a', 'b', 'handoff', 'V010', 'open', 1, 'conv-tgt-1', '2026-11-22T10:00:00Z', '2026-11-22T10:00:00Z');
                INSERT INTO messages VALUES (1, 'thr-v010', 'a', 'handoff', 'Body', '[]', 0, '2026-11-22T10:00:00Z');
                INSERT INTO jobs VALUES ('job-v010', 'thr-v010', 1, 'b', 'new_conversation', NULL, NULL, 'conv-tgt-1', 'completed', 0, NULL, NULL, '2026-11-22T10:00:01Z', '2026-11-22T10:00:05Z', '2026-11-22T10:00:00Z');
                """
            )
            conn.commit()
        finally:
            conn.close()

        store_v020 = BridgeStore(home=v010_home)
        store_v020_again = BridgeStore(home=v010_home)
        detail = store_v020_again.read_thread("thr-v010", reconcile=False)
        self.assertIsNone(detail["caller_conversation_id"])
        self.assertFalse(detail["notify_caller"])
        self.assertIsNone(detail["callback_dispatched_at"])
        self.assertEqual(detail["workflow_profile"], "none")
        job = store_v020_again.get_job("job-v010", reconcile=False)
        self.assertEqual(job["direction"], "outbound")

    def test_resolve_bridge_home_precedence_chain(self) -> None:
        explicit = self.root / "explicit-bridge"
        custom_bridge = self.root / "env-bridge"
        custom_gravity = self.root / "env-gravity"
        fake_home = self.root / "fake-home"

        with mock.patch.object(Path, "home", return_value=fake_home), mock.patch.dict(
            os.environ,
            {
                "GRAVITY_BRIDGE_HOME": str(custom_bridge),
                "GRAVITY_HOME": str(custom_gravity),
            },
            clear=False,
        ):
            # 1. Argument home prioritaire sur tout.
            self.assertEqual(resolve_bridge_home(explicit), explicit.resolve())
            # 2. $GRAVITY_BRIDGE_HOME prioritaire sur $GRAVITY_HOME/bridge.
            self.assertEqual(resolve_bridge_home(), custom_bridge.resolve())

        with mock.patch.object(Path, "home", return_value=fake_home), mock.patch.dict(
            os.environ,
            {
                "GRAVITY_BRIDGE_HOME": "   ",
                "GRAVITY_HOME": str(custom_gravity),
            },
            clear=False,
        ):
            # 3. $GRAVITY_HOME/bridge lorsque $GRAVITY_BRIDGE_HOME est absent/vide.
            self.assertEqual(resolve_bridge_home(), (custom_gravity / "bridge").resolve())

        clean_env = dict(os.environ)
        clean_env.pop("GRAVITY_BRIDGE_HOME", None)
        clean_env.pop("GRAVITY_HOME", None)
        with mock.patch.object(Path, "home", return_value=fake_home), mock.patch.dict(
            os.environ, clean_env, clear=True
        ):
            # 4. Repli final ~/.gemini/gravity/bridge sans toucher au vrai HOME.
            expected_default = (fake_home / ".gemini" / "gravity" / "bridge").resolve()
            self.assertEqual(default_bridge_home(), expected_default)
            self.assertEqual(resolve_bridge_home(), expected_default)

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

    def test_callback_notify_caller_resolution_one_shot_guards_and_non_pollution(self) -> None:
        now = datetime.now(timezone.utc)
        caller_conv = "11111111-1111-4111-8111-111111111111"
        target_conv = "22222222-2222-4222-8222-222222222222"

        # 1. Auto si prêt : caller unlinked -> notify_caller=False si omis.
        t_unlinked = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Auto unlinked",
            "Body",
            approved=True,
            caller_conversation_id=caller_conv,
            now=now,
        )
        self.assertFalse(t_unlinked["notify_caller"])
        self.assertEqual(t_unlinked["caller_conversation_id"], caller_conv)

        # 2. notify_caller=True explicite avec caller unlinked -> enregistré + avertissement non bloquant.
        t_explicit_unlinked = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Explicit unlinked",
            "Body",
            approved=True,
            caller_conversation_id=caller_conv,
            notify_caller=True,
            now=now,
        )
        self.assertTrue(t_explicit_unlinked["notify_caller"])
        self.assertTrue(t_explicit_unlinked.get("warnings"))
        self.assertIn("unlinked", t_explicit_unlinked["warnings"][0])

        # Clôture quand caller unlinked -> skipped_sidecar_unlinked sans erreur.
        rep_unlinked = self.store.add_reply(
            t_explicit_unlinked["thread_id"],
            "metrics",
            "Terminé.",
            close=True,
            conversation_id=target_conv,
            now=now,
        )
        self.assertEqual(rep_unlinked["status"], "done")
        self.assertEqual(rep_unlinked["callback_status"], "skipped_sidecar_unlinked")
        self.assertNotIn("callback_job", rep_unlinked)
        self.assertIsNone(
            self.store.read_thread(t_explicit_unlinked["thread_id"], reconcile=False)[
                "callback_dispatched_at"
            ]
        )

        # 3. Caller lié mais offline -> skipped_sidecar_offline sans erreur.
        self.store.set_workspace_sidecar("architect", "gravity-bridge-architect")
        rep_offline = self.store.add_reply(
            t_explicit_unlinked["thread_id"],
            "metrics",
            "Deuxième clôture.",
            close=True,
            now=now,
        )
        self.assertEqual(rep_offline["callback_status"], "skipped_sidecar_offline")

        # 4. Caller online -> Auto si prêt active notify_caller=True.
        self.store.record_sidecar_heartbeat(
            "architect",
            sidecar_id="gravity-bridge-architect",
            pid=os.getpid(),
            now=now,
        )
        self.store.record_sidecar_heartbeat(
            "metrics",
            sidecar_id="gravity-bridge-metrics",
            pid=os.getpid(),
            now=now,
        )
        t_auto = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Auto ready",
            "Body",
            approved=True,
            caller_conversation_id=caller_conv,
            now=now,
        )
        self.assertTrue(t_auto["notify_caller"])
        self.assertNotIn("warnings", t_auto)

        # notify_caller=False explicite force 0 malgré sidecar online.
        t_disabled = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Disabled",
            "Body",
            approved=True,
            caller_conversation_id=caller_conv,
            notify_caller=False,
            now=now,
        )
        self.assertFalse(t_disabled["notify_caller"])

        # Garde-fous : close=False SANS job outbound dispatched actif, expéditeur!=to_workspace, same_conv -> aucun callback.
        rep_open = self.store.add_reply(
            t_auto["thread_id"], "metrics", "En cours.", close=False, conversation_id=target_conv, now=now
        )
        self.assertNotIn("callback_status", rep_open)
        self.assertNotIn("callback_job", rep_open)
        rep_from_caller = self.store.add_reply(
            t_auto["thread_id"], "architect", "Précision.", close=True, now=now
        )
        self.assertNotIn("callback_status", rep_from_caller)

        same_conv_id = "33333333-3333-4333-8333-333333333333"
        t_same = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Same conv",
            "Body",
            approved=True,
            caller_conversation_id=same_conv_id,
            notify_caller=True,
            now=now,
        )
        rep_same = self.store.add_reply(
            t_same["thread_id"],
            "metrics",
            "Même conv.",
            close=True,
            conversation_id=same_conv_id,
            now=now,
        )
        self.assertNotIn("callback_status", rep_same)

        # Déclenchement one-shot nominal sur t_auto après job outbound dispatched.
        out_job = self.store.create_job(t_auto["thread_id"], t_auto["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(out_job["id"], target_conversation_id=target_conv, now=now)

        rep_done = self.store.add_reply(
            t_auto["thread_id"],
            "metrics",
            "Lot terminé.",
            close=True,
            conversation_id=target_conv,
            now=now,
        )
        self.assertEqual(rep_done["completed_job_ids"], [out_job["id"]])
        self.assertEqual(rep_done["callback_status"], "queued")
        cb_job = rep_done["callback_job"]
        self.assertEqual(cb_job["direction"], "callback")
        self.assertEqual(cb_job["action"], "send_message")
        self.assertEqual(cb_job["target_workspace"], "architect")
        self.assertEqual(cb_job["target_conversation_id"], caller_conv)

        # Anti-rejeu : second reply_thread(..., close=True) ne crée jamais un second callback.
        rep_replay = self.store.add_reply(
            t_auto["thread_id"], "metrics", "Rejeu.", close=True, now=now
        )
        self.assertNotIn("callback_job", rep_replay)
        self.assertNotIn("callback_status", rep_replay)

        # Finalisation callback : mark_job_dispatched interdit, complete_job ne pollue ni target_conversation_id ni anti-boucle.
        claimed_cb = self.store.claim_next_job(
            "architect", sidecar_id="gravity-bridge-architect", now=now
        )
        self.assertIsNotNone(claimed_cb)
        with self.assertRaises(DispatchBlockedError):
            self.store.mark_job_dispatched(cb_job["id"], target_conversation_id=caller_conv, now=now)

        done_cb = self.store.complete_job(
            cb_job["id"],
            status="completed",
            exit_code=0,
            target_conversation_id=caller_conv,
            now=now,
        )
        self.assertEqual(done_cb["status"], "completed")
        detail_after = self.store.read_thread(t_auto["thread_id"], reconcile=False)
        self.assertEqual(detail_after["target_conversation_id"], target_conv)
        self.assertIsNotNone(detail_after["callback_dispatched_at"])
        self.assertFalse(self.store.is_target_conversation_id(caller_conv))
        self.assertNotIn(caller_conv, self.store.list_target_conversation_ids())
        self.assertTrue(self.store.is_target_conversation_id(target_conv))

    def test_callback_intermediate_open_reply_and_final_close_multiturn(self) -> None:
        now = datetime.now(timezone.utc)
        caller_conv = "44444444-4444-4444-8444-444444444444"
        target_conv = "55555555-5555-4555-8555-555555555555"

        self.store.record_sidecar_heartbeat(
            "architect", sidecar_id="gravity-bridge-architect", pid=os.getpid(), now=now
        )
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=now
        )
        created = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Multi-tours callback",
            "Exécuter étape 1.",
            approved=True,
            caller_conversation_id=caller_conv,
            now=now,
        )
        tid = created["thread_id"]

        # 1. Dispatch outbound actif -> réponse cible close=False déclenche callback status=open sans callback_dispatched_at.
        out1 = self.store.create_job(tid, created["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(out1["id"], target_conversation_id=target_conv, now=now)

        rep_q = self.store.add_reply(
            tid,
            "metrics",
            "Question : valider option A ou B ?",
            close=False,
            conversation_id=target_conv,
            now=now,
        )
        self.assertEqual(rep_q["status"], "open")
        self.assertEqual(rep_q["completed_job_ids"], [out1["id"]])
        self.assertEqual(rep_q["callback_status"], "queued")
        cb1 = rep_q["callback_job"]
        self.assertEqual(cb1["direction"], "callback")
        self.assertEqual(cb1["target_workspace"], "architect")
        self.assertEqual(cb1["target_conversation_id"], caller_conv)

        detail_mid = self.store.read_thread(tid, reconcile=False)
        self.assertEqual(detail_mid["status"], "open")
        self.assertIsNone(detail_mid["callback_dispatched_at"])

        claimed_cb1 = self.store.claim_next_job(
            "architect", sidecar_id="gravity-bridge-architect", now=now
        )
        self.assertIsNotNone(claimed_cb1)
        self.store.complete_job(cb1["id"], status="completed", exit_code=0, now=now)

        # 2. Deuxième reply close=False sans nouveau job outbound dispatched -> AUCUN callback.
        rep_spam = self.store.add_reply(
            tid,
            "metrics",
            "Complément sans nouveau dispatch.",
            close=False,
            conversation_id=target_conv,
            now=now,
        )
        self.assertEqual(rep_spam["status"], "open")
        self.assertEqual(rep_spam["completed_job_ids"], [])
        self.assertNotIn("callback_status", rep_spam)
        self.assertNotIn("callback_job", rep_spam)

        # 3. Relance outbound par l'appelant -> nouvelle réponse cible close=True -> callback final status=done + callback_dispatched_at.
        rep_followup = self.store.add_reply(
            tid,
            "architect",
            "Prendre option A.",
            close=False,
            now=now,
        )
        out2 = self.store.create_job(
            tid, rep_followup["message_id"], "metrics", action="send_message", now=now
        )
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(out2["id"], target_conversation_id=target_conv, now=now)

        rep_final = self.store.add_reply(
            tid,
            "metrics",
            "Option A appliquée et validée.",
            close=True,
            conversation_id=target_conv,
            now=now,
        )
        self.assertEqual(rep_final["status"], "done")
        self.assertEqual(rep_final["completed_job_ids"], [out2["id"]])
        self.assertEqual(rep_final["callback_status"], "queued")
        cb2 = rep_final["callback_job"]
        self.assertEqual(cb2["direction"], "callback")
        self.assertNotEqual(cb2["id"], cb1["id"])

        detail_end = self.store.read_thread(tid, reconcile=False)
        self.assertEqual(detail_end["status"], "done")
        self.assertIsNotNone(detail_end["callback_dispatched_at"])

        # Anti-rejeu après clôture définitive.
        rep_after = self.store.add_reply(tid, "metrics", "Post-clôture.", close=True, now=now)
        self.assertNotIn("callback_status", rep_after)
        self.assertNotIn("callback_job", rep_after)


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

    def test_workflow_profile_defaults_read_thread_and_create_job_updates(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=now
        )

        t_handoff = self.store.create_thread(
            "architect", "metrics", "handoff", "Handoff défaut", "Body", approved=True
        )
        self.assertEqual(t_handoff["workflow_profile"], "standard")
        self.assertEqual(
            self.store.read_thread(t_handoff["thread_id"], reconcile=False)["workflow_profile"],
            "standard",
        )

        t_question = self.store.create_thread(
            "architect", "metrics", "question", "Question défaut", "Body", approved=True
        )
        self.assertEqual(t_question["workflow_profile"], "none")
        self.assertEqual(
            self.store.read_thread(t_question["thread_id"], reconcile=False)["workflow_profile"],
            "none",
        )

        with self.assertRaises(ValidationError):
            self.store.create_thread(
                "architect",
                "metrics",
                "info",
                "Info invalide",
                "Body",
                workflow="deep",
            )

        # create_job(workflow=None) préserve l'existant.
        t_deep = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Handoff deep",
            "Body",
            approved=True,
            workflow="DEEP",
        )
        self.assertEqual(t_deep["workflow_profile"], "deep")
        j_keep = self.store.create_job(
            t_deep["thread_id"], t_deep["message_id"], "metrics", workflow=None, now=now
        )
        self.assertEqual(
            self.store.read_thread(t_deep["thread_id"], reconcile=False)["workflow_profile"],
            "deep",
        )
        self.store.cancel_job(j_keep["id"], now=now)

        # create_job(workflow="evo") met à jour workflow_profile dans la transaction.
        j_upd = self.store.create_job(
            t_deep["thread_id"], t_deep["message_id"], "metrics", workflow="EVO", now=now
        )
        self.assertEqual(
            self.store.read_thread(t_deep["thread_id"], reconcile=False)["workflow_profile"],
            "evo",
        )
        self.store.cancel_job(j_upd["id"], now=now)

        # create_job(workflow="standard") échoue sur fil question.
        with self.assertRaises(ValidationError):
            self.store.create_job(
                t_question["thread_id"],
                t_question["message_id"],
                "metrics",
                workflow="standard",
                now=now,
            )
        self.assertEqual(self.store.list_jobs(thread_id=t_question["thread_id"], reconcile=False), [])

    def test_batch_threads_aggregation_fail_fast_and_already_dispatched(self) -> None:
        now = datetime.now(timezone.utc)
        caller_conv = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        self.store.record_sidecar_heartbeat(
            "architect", sidecar_id="gravity-bridge-architect", pid=os.getpid(), now=now
        )
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=now
        )

        # Validation : on_batch_complete sans batch_id refusé.
        with self.assertRaises(ValidationError):
            self.store.create_thread(
                "architect",
                "metrics",
                "handoff",
                "Sans lot",
                "Body",
                callback_mode="on_batch_complete",
                now=now,
            )

        t1 = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Lot W2 - 1",
            "Body 1",
            file_refs=["a.py"],
            approved=True,
            caller_conversation_id=caller_conv,
            batch_id="batch-w2-01",
            callback_mode="on_batch_complete",
            now=now,
        )
        t2 = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Lot W2 - 2",
            "Body 2",
            file_refs=["b.py"],
            approved=True,
            caller_conversation_id=caller_conv,
            batch_id="batch-w2-01",
            callback_mode="on_batch_complete",
            now=now,
        )
        self.assertEqual(t1["batch_id"], "batch-w2-01")
        self.assertEqual(t1["callback_mode"], "on_batch_complete")

        inbox = self.store.list_inbox("metrics", direction="inbox", status="open")
        self.assertEqual({item["batch_id"] for item in inbox}, {"batch-w2-01"})
        self.assertEqual({item["callback_mode"] for item in inbox}, {"on_batch_complete"})

        detail_1 = self.store.read_thread(t1["thread_id"], reconcile=False)
        self.assertEqual(detail_1["batch_id"], "batch-w2-01")
        self.assertEqual(detail_1["callback_mode"], "on_batch_complete")
        self.assertEqual(detail_1["batch_summary"]["total"], 2)
        self.assertFalse(detail_1["batch_summary"]["all_done"])

        # Clôture de t1 -> waiting_batch, pas de callback_job.
        r1 = self.store.add_reply(
            t1["thread_id"], "metrics", "T1 OK", file_refs=["c.py"], close=True, now=now
        )
        self.assertEqual(r1["callback_status"], "waiting_batch")
        self.assertEqual(r1["batch_status"], "waiting_batch")
        self.assertNotIn("callback_job", r1)

        # Clôture de t2 -> batch_complete, callback_job créé + callback_dispatched_at posé sur le lot.
        r2 = self.store.add_reply(
            t2["thread_id"], "metrics", "T2 OK", close=True, now=now
        )
        self.assertEqual(r2["callback_status"], "queued")
        self.assertEqual(r2["batch_status"], "batch_complete")
        self.assertIn("callback_job", r2)
        self.assertIsNotNone(
            self.store.read_thread(t1["thread_id"], reconcile=False)["callback_dispatched_at"]
        )
        self.assertIsNotNone(
            self.store.read_thread(t2["thread_id"], reconcile=False)["callback_dispatched_at"]
        )

        # Rejeu sur t1 -> already_dispatched.
        r_replay = self.store.add_reply(
            t1["thread_id"], "metrics", "Rejeu tardif", close=True, now=now
        )
        self.assertEqual(r_replay["callback_status"], "already_dispatched")
        self.assertEqual(r_replay["batch_status"], "batch_complete")
        self.assertNotIn("callback_job", r_replay)

        # Fail-fast sur nouveau lot avec close=False après job outbound dispatched.
        t_ff = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Lot FF",
            "Body FF",
            approved=True,
            caller_conversation_id=caller_conv,
            batch_id="batch-w2-ff",
            callback_mode="on_batch_complete",
            now=now,
        )
        j_ff = self.store.create_job(t_ff["thread_id"], t_ff["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(
            j_ff["id"], target_conversation_id="bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb", now=now
        )
        r_ff = self.store.add_reply(
            t_ff["thread_id"],
            "metrics",
            "Blocage critique",
            close=False,
            conversation_id="bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
            now=now,
        )
        self.assertEqual(r_ff["callback_status"], "queued")
        self.assertEqual(r_ff["batch_status"], "fail_fast")
        self.assertIn("callback_job", r_ff)

    def test_claim_next_job_serializes_outbound_handoffs_and_never_blocks_callback_or_question(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=now
        )
        self.store.record_sidecar_heartbeat(
            "architect", sidecar_id="gravity-bridge-architect", pid=os.getpid(), now=now
        )

        h1 = self.store.create_thread("architect", "metrics", "handoff", "H1", "B1", approved=True, now=now)
        h2 = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "H2",
            "B2",
            approved=True,
            now=now + timedelta(seconds=1),
        )
        q1 = self.store.create_thread(
            "architect",
            "metrics",
            "question",
            "Q1",
            "BQ",
            approved=True,
            now=now + timedelta(seconds=2),
        )

        jh1 = self.store.create_job(h1["thread_id"], h1["message_id"], "metrics", now=now)
        jh2 = self.store.create_job(
            h2["thread_id"], h2["message_id"], "metrics", now=now + timedelta(seconds=1)
        )
        jq1 = self.store.create_job(
            q1["thread_id"], q1["message_id"], "metrics", now=now + timedelta(seconds=2)
        )

        # Claim H1 -> passe à claimed.
        c1 = self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.assertIsNotNone(c1)
        assert c1 is not None
        self.assertEqual(c1["id"], jh1["id"])

        # H2 (handoff outbound) est bloqué pendant que H1 est claimed, mais Q1 (question) est claimable !
        c2 = self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.assertIsNotNone(c2)
        assert c2 is not None
        self.assertEqual(c2["id"], jq1["id"])

        # Plus rien de claimable tant que H1 est claimed puis dispatched.
        self.assertIsNone(
            self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        )
        self.store.mark_job_dispatched(jh1["id"], target_conversation_id="conv-h1", now=now)
        self.assertIsNone(
            self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        )

        # Un callback sur le même target_workspace ('metrics') n'est jamais bloqué par un handoff actif.
        t_cb = self.store.create_thread(
            "metrics",
            "architect",
            "handoff",
            "CB",
            "Body",
            approved=True,
            caller_conversation_id="cccccccc-3333-4333-8333-cccccccccccc",
            now=now,
        )
        rep_cb = self.store.add_reply(
            t_cb["thread_id"],
            "architect",
            "Réponse CB",
            close=True,
            conversation_id="dddddddd-4444-4444-8444-dddddddddddd",
            now=now + timedelta(seconds=3),
        )
        cb_id = rep_cb["callback_job"]["id"]
        c_cb = self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.assertIsNotNone(c_cb)
        assert c_cb is not None
        self.assertEqual(c_cb["id"], cb_id)

        # Une fois H1 complété, H2 devient claimable.
        self.store.complete_job(jh1["id"], status="completed", exit_code=0, now=now)
        c3 = self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.assertIsNotNone(c3)
        assert c3 is not None
        self.assertEqual(c3["id"], jh2["id"])

    def test_reconcile_dispatched_stale_ttl_and_cancel_job_by_thread_id(self) -> None:
        now = datetime.now(timezone.utc)
        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=now
        )
        t1 = self.store.create_thread("architect", "metrics", "handoff", "Stale H1", "B1", approved=True, now=now)
        t2 = self.store.create_thread(
            "architect",
            "metrics",
            "handoff",
            "Queued H2",
            "B2",
            approved=True,
            now=now + timedelta(seconds=1),
        )
        j1 = self.store.create_job(t1["thread_id"], t1["message_id"], "metrics", now=now)
        self.store.claim_next_job("metrics", sidecar_id="gravity-bridge-metrics", now=now)
        self.store.mark_job_dispatched(j1["id"], target_conversation_id="conv-stale", now=now)

        # Dépassement TTL stale (1801s > 1800s par défaut) -> failed + débloque H2.
        later = now + timedelta(seconds=1801)
        reconciled = self.store.reconcile_orphan_jobs(now=later)
        self.assertEqual(reconciled, [j1["id"]])
        j1_after = self.store.get_job(j1["id"], reconcile=False)
        self.assertEqual(j1_after["status"], "failed")
        self.assertEqual(j1_after["error_summary"], STALE_DISPATCHED_ERROR_SUMMARY)

        self.store.record_sidecar_heartbeat(
            "metrics", sidecar_id="gravity-bridge-metrics", pid=os.getpid(), now=later
        )
        j2 = self.store.create_job(t2["thread_id"], t2["message_id"], "metrics", now=later)
        cancelled = self.store.cancel_job(thread_id=t2["thread_id"], now=later)
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["id"], j2["id"])
        self.assertEqual(cancelled["status"], "cancelled")

        not_cancelled = self.store.cancel_job(thread_id=t2["thread_id"], now=later)
        self.assertFalse(not_cancelled["cancelled"])

        with self.assertRaises(NotFoundError):
            self.store.cancel_job(thread_id="thr-missing")


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
