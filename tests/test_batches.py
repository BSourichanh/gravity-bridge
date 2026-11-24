"""Tests unitaires pour plugin/core/batches.py (100 % stdlib)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.batches import (  # noqa: E402
    ALLOWED_CALLBACK_MODES,
    DEFAULT_DISPATCHED_STALE_SECONDS,
    DISPATCHED_STALE_ENV,
    STALE_DISPATCHED_ERROR_SUMMARY,
    evaluate_batch_callback,
    find_stale_dispatched_jobs,
    has_active_outbound_job,
    resolve_dispatched_stale_seconds,
    summarize_batch,
    validate_batch_id,
    validate_callback_mode,
)
from core.security import SecretDetectedError, ValidationError  # noqa: E402
from core.store import BridgeStore, utc_now_iso  # noqa: E402


class TestBatchesModule(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = BridgeStore(home=self.root / "bridge-home")
        self.ws_a = self.root / "ws-a"
        self.ws_b = self.root / "ws-b"
        self.ws_c = self.root / "ws-c"
        for d in (self.ws_a, self.ws_b, self.ws_c):
            d.mkdir()
        self.store.register_workspace(str(self.ws_a), slug="alpha")
        self.store.register_workspace(str(self.ws_b), slug="beta")
        self.store.register_workspace(str(self.ws_c), slug="gamma")
        now = datetime.now(timezone.utc)
        for slug in ("alpha", "beta", "gamma"):
            self.store.record_sidecar_heartbeat(
                slug, sidecar_id=f"gravity-bridge-{slug}", pid=os.getpid(), now=now
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_validate_batch_id_and_callback_mode(self) -> None:
        self.assertIsNone(validate_batch_id(None))
        self.assertIsNone(validate_batch_id("   "))
        self.assertEqual(validate_batch_id(" wave-2.core:1_ok "), "wave-2.core:1_ok")
        self.assertEqual(validate_batch_id("a" * 80), "a" * 80)

        for bad in (123, "bad\nid", "bad space", "-leading", "a" * 81):
            with self.assertRaises(ValidationError):
                validate_batch_id(bad)  # type: ignore[arg-type]

        with self.assertRaises(SecretDetectedError):
            validate_batch_id("sk-" + "secret_0123456789abcdef1234")

        self.assertEqual(ALLOWED_CALLBACK_MODES, ("per_thread", "on_batch_complete"))
        self.assertEqual(validate_callback_mode(None), "per_thread")
        self.assertEqual(validate_callback_mode("   "), "per_thread")
        self.assertEqual(validate_callback_mode("per_thread"), "per_thread")
        self.assertEqual(
            validate_callback_mode("ON_BATCH_COMPLETE", batch_id="wave-2"),
            "on_batch_complete",
        )
        with self.assertRaises(ValidationError):
            validate_callback_mode("on_batch_complete", batch_id=None)
        with self.assertRaises(ValidationError):
            validate_callback_mode("on_batch_complete", batch_id="   ")
        with self.assertRaises(ValidationError):
            validate_callback_mode("invalid_mode", batch_id="wave-2")
        with self.assertRaises(ValidationError):
            validate_callback_mode(123)  # type: ignore[arg-type]

    def test_resolve_dispatched_stale_seconds(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_dispatched_stale_seconds(), DEFAULT_DISPATCHED_STALE_SECONDS
            )
            self.assertEqual(resolve_dispatched_stale_seconds(45), 45)

        with mock.patch.dict(os.environ, {DISPATCHED_STALE_ENV: "120"}, clear=True):
            self.assertEqual(resolve_dispatched_stale_seconds(), 120)
            self.assertEqual(resolve_dispatched_stale_seconds(60), 60)

        for bad_override in (0, -5, True, "120"):
            with self.assertRaises(ValidationError):
                resolve_dispatched_stale_seconds(bad_override)  # type: ignore[arg-type]

        for bad_env in ("0", "-10", "not-an-int"):
            with mock.patch.dict(os.environ, {DISPATCHED_STALE_ENV: bad_env}, clear=True):
                with self.assertRaises(ValidationError):
                    resolve_dispatched_stale_seconds()

    def test_has_active_outbound_job_and_find_stale_dispatched_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        t_q = self.store.create_thread("alpha", "beta", "question", "Q", "Body", approved=True, now=now)
        j_q = self.store.create_job(t_q["thread_id"], t_q["message_id"], "beta", now=now)
        self.store.claim_next_job("beta", sidecar_id="gravity-bridge-beta", now=now)
        self.store.mark_job_dispatched(j_q["id"], target_conversation_id="conv-q1", now=now)

        # Un question dispatched ne compte pas comme handoff actif.
        with self.store._connect() as conn:
            self.assertFalse(has_active_outbound_job(conn, "beta"))

        t_h = self.store.create_thread("alpha", "beta", "handoff", "H1", "Body", approved=True, now=now)
        j_h = self.store.create_job(t_h["thread_id"], t_h["message_id"], "beta", now=now)
        # En status 'queued' uniquement -> pas actif.
        with self.store._connect() as conn:
            self.assertFalse(has_active_outbound_job(conn, "beta"))

        self.store.claim_next_job("beta", sidecar_id="gravity-bridge-beta", now=now)
        with self.store._connect() as conn:
            self.assertTrue(has_active_outbound_job(conn, "beta"))
            self.assertFalse(has_active_outbound_job(conn, "beta", exclude_job_id=j_h["id"]))

        self.store.mark_job_dispatched(j_h["id"], target_conversation_id="conv-h1", now=now)
        with self.store._connect() as conn:
            self.assertTrue(has_active_outbound_job(conn, "beta"))
            self.assertEqual(
                find_stale_dispatched_jobs(conn, now + timedelta(seconds=100), stale_seconds=120),
                [],
            )
            stale = find_stale_dispatched_jobs(
                conn, now + timedelta(seconds=121), stale_seconds=120
            )
            self.assertEqual(set(stale), {j_q["id"], j_h["id"]})

        self.assertIn("TTL stale", STALE_DISPATCHED_ERROR_SUMMARY)

    def test_summarize_batch_and_evaluate_batch_callback(self) -> None:
        now = datetime.now(timezone.utc)
        t1 = self.store.create_thread(
            "alpha",
            "beta",
            "handoff",
            "Lot T1",
            "Faire T1",
            file_refs=["a.py", "shared.py"],
            approved=True,
            caller_conversation_id="11111111-1111-4111-8111-111111111111",
            batch_id="wave-2",
            callback_mode="on_batch_complete",
            now=now,
        )
        t2 = self.store.create_thread(
            "alpha",
            "gamma",
            "handoff",
            "Lot T2",
            "Faire T2",
            file_refs=["shared.py", "b.py"],
            approved=True,
            caller_conversation_id="11111111-1111-4111-8111-111111111111",
            batch_id="wave-2",
            callback_mode="on_batch_complete",
            now=now,
        )

        # Clôture de t1 seul -> waiting_batch.
        self.store.add_reply(
            t1["thread_id"], "beta", "T1 terminé OK.", file_refs=["c.py"], close=True, now=now
        )
        with self.store._connect() as conn:
            summary_mid = summarize_batch(conn, "wave-2")
            self.assertEqual(summary_mid["total"], 2)
            self.assertEqual(summary_mid["done_threads"], 1)
            self.assertEqual(summary_mid["open_threads"], 1)
            self.assertFalse(summary_mid["all_done"])
            self.assertFalse(summary_mid["has_dispatched_callback"])
            self.assertEqual(
                summary_mid["consolidated_file_refs"], ["a.py", "shared.py", "c.py", "b.py"]
            )
            self.assertEqual(summary_mid["threads"][0]["last_reply_excerpt"], "T1 terminé OK.")
            self.assertIsNone(summary_mid["threads"][1]["last_reply_excerpt"])

            ev_wait = evaluate_batch_callback(conn, "wave-2", t1["thread_id"], True)
            self.assertFalse(ev_wait["emit_callback"])
            self.assertEqual(ev_wait["reason"], "waiting_batch")
            self.assertEqual(ev_wait["batch_status"], "waiting_batch")

            ev_ff = evaluate_batch_callback(conn, "wave-2", t2["thread_id"], False)
            self.assertTrue(ev_ff["emit_callback"])
            self.assertTrue(ev_ff["fail_fast"])
            self.assertEqual(ev_ff["reason"], "fail_fast")
            self.assertEqual(ev_ff["batch_status"], "fail_fast")

            ev_none = evaluate_batch_callback(conn, None, t1["thread_id"], True)
            self.assertTrue(ev_none["emit_callback"])
            self.assertEqual(ev_none["mode"], "per_thread")
            self.assertEqual(ev_none["reason"], "per_thread")

        # Clôture de t2 -> batch_complete puis already_dispatched.
        self.store.add_reply(t2["thread_id"], "gamma", "T2 terminé OK.", close=True, now=now)
        with self.store._connect() as conn:
            ev_done = evaluate_batch_callback(conn, "wave-2", t2["thread_id"], True)
            self.assertFalse(ev_done["emit_callback"])
            self.assertEqual(ev_done["reason"], "already_dispatched")
            self.assertEqual(ev_done["batch_status"], "batch_complete")
            summary_end = summarize_batch(conn, "wave-2")
            self.assertTrue(summary_end["all_done"])
            self.assertTrue(summary_end["has_dispatched_callback"])


if __name__ == "__main__":
    unittest.main()
