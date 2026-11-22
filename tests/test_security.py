"""Tests unitaires du module core.security (100 % stdlib)."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "gravity-bridge-plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.security import (  # noqa: E402
    MAX_BODY_BYTES,
    MAX_FILE_REF_CHARS,
    MAX_FILE_REFS,
    MAX_SUBJECT_CHARS,
    DispatchBlockedError,
    SecretDetectedError,
    ValidationError,
    assert_dispatch_allowed,
    assert_mode_approval,
    assert_no_secrets,
    derive_slug_candidates,
    detect_secret_rule,
    get_bridge_depth,
    is_explicit_workspace_path,
    normalize_workspace_path,
    redact_secrets,
    resolve_effective_mode,
    slugify_name,
    truncate_utf8_bytes,
    validate_body,
    validate_effort,
    validate_file_refs,
    validate_mode,
    validate_msg_type,
    validate_slug,
    validate_subject,
    validate_timeout_seconds,
    validate_wait_seconds,
)


class TestSecretDetectionAndRedaction(unittest.TestCase):
    def test_detects_all_secret_rules_without_leaking_value(self) -> None:
        samples = [
            ("aws_access_key", "key=" + "AKIA" + "1234567890ABCDEF"),
            ("google_api_key", "api=" + "AIza" + "SyDummyKeyForUnitTest123456789012345"),
            ("github_token", "tok=" + "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"),
            ("github_token", "pat=" + "github_pat_" + "1234567890abcdef1234567890abcdef1234567890"),
            ("generic_sk_key", "sk=" + "sk-" + "proj_0123456789abcdefghij"),
            ("pem_private_key", "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA"),
            (
                "bearer_auth_header",
                "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9_abc",
            ),
        ]
        for expected_rule, text in samples:
            with self.subTest(rule=expected_rule):
                self.assertEqual(detect_secret_rule(text), expected_rule)
                with self.assertRaises(SecretDetectedError) as ctx:
                    assert_no_secrets(text, field_name="body")
                self.assertEqual(ctx.exception.rule_name, expected_rule)
                self.assertIn(expected_rule, str(ctx.exception))
                # La valeur secrète elle-même ne doit jamais fuiter dans le message d'erreur.
                token_part = text.split("=", 1)[-1].split("\n", 1)[0]
                if "Bearer " in text:
                    token_part = text.split("Bearer ", 1)[1]
                self.assertNotIn(token_part, str(ctx.exception))

    def test_no_false_positives_on_hyphenated_identifiers(self) -> None:
        benign_texts = [
            "task-implementation-lot-1-complete",
            "disk-usage-report-summary-2026",
            "flask-sqlalchemy-extension-migration",
            "desktop-task-scheduler-service-v2",
            "Autorisation: normal text without bearer token",
        ]
        for text in benign_texts:
            with self.subTest(text=text):
                self.assertIsNone(detect_secret_rule(text))
                assert_no_secrets(text)

    def test_redact_secrets_tokens_and_pem_block(self) -> None:
        aws = "AKIA" + "ABCDEF1234567890"
        sk = "sk-" + "test_0123456789abcdef0123"
        pem = (
            "-----BEGIN "
            + "EC PRIVATE KEY-----\n"
            + "MHQCAQEEIBdummybase64contentline123456\n"
            + "-----END EC PRIVATE KEY-----"
        )
        raw = f"Avant\naws={aws}\n{pem}\napres={sk}\nFin"
        redacted, rules = redact_secrets(raw)
        self.assertNotIn(aws, redacted)
        self.assertNotIn(sk, redacted)
        self.assertNotIn("MHQCAQEEIBdummybase64contentline123456", redacted)
        self.assertIn("[REDACTED:aws_access_key]", redacted)
        self.assertIn("[REDACTED:pem_private_key]", redacted)
        self.assertIn("[REDACTED:generic_sk_key]", redacted)
        self.assertIn("Avant", redacted)
        self.assertIn("Fin", redacted)
        self.assertEqual(rules, ["pem_private_key", "aws_access_key", "generic_sk_key"])


class TestLimitsAndFieldValidation(unittest.TestCase):
    def test_subject_validation(self) -> None:
        self.assertEqual(validate_subject("  Lot 1 prêt  "), "Lot 1 prêt")
        self.assertEqual(validate_subject("a" * MAX_SUBJECT_CHARS), "a" * MAX_SUBJECT_CHARS)
        with self.assertRaises(ValidationError):
            validate_subject("")
        with self.assertRaises(ValidationError):
            validate_subject("ligne1\nligne2")
        with self.assertRaises(ValidationError):
            validate_subject("a" * (MAX_SUBJECT_CHARS + 1))
        with self.assertRaises(SecretDetectedError):
            validate_subject("Cle " + "AKIA" + "1234567890ABCDEF")

    def test_body_utf8_byte_limit_and_null_byte(self) -> None:
        # "é" occupe 2 octets UTF-8 : 16_384 * 2 == 32_768 octets.
        exact_utf8 = "é" * (MAX_BODY_BYTES // 2)
        self.assertEqual(len(exact_utf8.encode("utf-8")), MAX_BODY_BYTES)
        self.assertEqual(validate_body(exact_utf8), exact_utf8)

        too_big = exact_utf8 + "a"
        with self.assertRaises(ValidationError):
            validate_body(too_big)
        with self.assertRaises(ValidationError):
            validate_body("   \n\t  ")
        with self.assertRaises(ValidationError):
            validate_body("hello\x00world")

    def test_truncate_utf8_bytes_respects_codepoint_boundaries(self) -> None:
        text = "🚀" * 10  # 40 octets
        truncated, was_truncated = truncate_utf8_bytes(text, 15, suffix="...")
        self.assertTrue(was_truncated)
        encoded = truncated.encode("utf-8")
        self.assertLessEqual(len(encoded), 15)
        self.assertTrue(truncated.endswith("..."))
        self.assertEqual(truncated, "🚀🚀🚀...")

    def test_file_refs_validation(self) -> None:
        self.assertEqual(validate_file_refs(None), [])
        self.assertEqual(
            validate_file_refs([r"src\core\store.py", "tests/test_store.py"]),
            ["src/core/store.py", "tests/test_store.py"],
        )
        with self.assertRaises(ValidationError):
            validate_file_refs("src/core/store.py")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            validate_file_refs(["a.py"] * (MAX_FILE_REFS + 1))
        with self.assertRaises(ValidationError):
            validate_file_refs(["a" * (MAX_FILE_REF_CHARS + 1)])
        for bad in (
            "/etc/passwd",
            "~/secret.txt",
            "C:\\Users\\test\\a.py",
            "D:rel.py",
            "../outside.py",
            "src/../../escape.py",
            "bad\nline.py",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    validate_file_refs([bad])
        with self.assertRaises(SecretDetectedError):
            validate_file_refs(["keys/" + "AKIA" + "1234567890ABCDEF" + ".txt"])


class TestPathsSlugsAndPolicy(unittest.TestCase):
    def test_explicit_path_detection_and_normalization(self) -> None:
        self.assertFalse(is_explicit_workspace_path("gravity-metrics"))
        self.assertFalse(is_explicit_workspace_path("relative/path"))
        self.assertTrue(is_explicit_workspace_path("~/projets/app"))
        self.assertTrue(is_explicit_workspace_path("/tmp/proj"))
        self.assertTrue(is_explicit_workspace_path("D:\\Neko\\GitHub\\app"))
        self.assertTrue(is_explicit_workspace_path("C:/Users/app"))

        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "MyRepo"
            sub.mkdir()
            norm = normalize_workspace_path(str(sub))
            self.assertTrue(Path(norm).is_dir())
            if len(norm) >= 2 and norm[1] == ":":
                self.assertTrue(norm[0].isupper())
                # Même avec lecteur minuscule, la normalisation retourne le même chemin canonique.
                lower_drive = norm[0].lower() + norm[1:]
                self.assertEqual(normalize_workspace_path(lower_drive), norm)

            file_path = Path(tmp) / "file.txt"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(ValidationError):
                normalize_workspace_path(str(file_path))
            with self.assertRaises(ValidationError):
                normalize_workspace_path(str(Path(tmp) / "missing_dir"))

    def test_slug_validation_and_derivation(self) -> None:
        self.assertEqual(validate_slug("gravity-bridge_01"), "gravity-bridge_01")
        for bad in ("", "-bad", "_bad", "Upper", "a" * 50, "a b"):
            with self.assertRaises(ValidationError):
                validate_slug(bad)

        self.assertEqual(slugify_name("Mon Atelier Étoile!"), "mon-atelier-etoile")
        self.assertEqual(slugify_name("___"), "workspace")
        base, parent = derive_slug_candidates(str(Path("/tmp/GitHub/Gravity-Bridge")))
        self.assertEqual(base, "gravity-bridge")
        self.assertEqual(parent, "github-gravity-bridge")

    def test_depth_guard_fail_closed(self) -> None:
        self.assertEqual(get_bridge_depth({}), 0)
        self.assertEqual(get_bridge_depth({"GRAVITY_BRIDGE_DEPTH": ""}), 0)
        self.assertEqual(assert_dispatch_allowed({"GRAVITY_BRIDGE_DEPTH": "0"}), 0)

        for blocked in ("1", "2", "abc", "-1"):
            with self.subTest(depth=blocked):
                with self.assertRaises(DispatchBlockedError):
                    assert_dispatch_allowed({"GRAVITY_BRIDGE_DEPTH": blocked})

    def test_modes_approval_effort_and_numeric_bounds(self) -> None:
        self.assertEqual(resolve_effective_mode("question"), "plan")
        self.assertEqual(resolve_effective_mode("info"), "plan")
        self.assertEqual(resolve_effective_mode("handoff"), "accept-edits")
        self.assertEqual(resolve_effective_mode("handoff", "plan"), "plan")
        self.assertEqual(validate_msg_type("reply", allow_reply=True), "reply")
        with self.assertRaises(ValidationError):
            validate_msg_type("reply", allow_reply=False)
        with self.assertRaises(ValidationError):
            validate_mode("danger")

        assert_mode_approval("plan", False)
        assert_mode_approval("accept-edits", True)
        with self.assertRaises(DispatchBlockedError):
            assert_mode_approval("accept-edits", False)

        for eff in ("low", "medium", "high"):
            self.assertEqual(validate_effort(eff), eff)
        with self.assertRaises(ValidationError):
            validate_effort("xhigh")

        self.assertEqual(validate_timeout_seconds(None), 300)
        self.assertEqual(validate_timeout_seconds(30), 30)
        self.assertEqual(validate_timeout_seconds(900), 900)
        for bad_t in (29, 901, True, False, "300"):
            with self.assertRaises(ValidationError):
                validate_timeout_seconds(bad_t)  # type: ignore[arg-type]

        self.assertEqual(validate_wait_seconds(None), 0)
        self.assertEqual(validate_wait_seconds(45), 45)
        for bad_w in (-1, 46, True, "5"):
            with self.assertRaises(ValidationError):
                validate_wait_seconds(bad_w)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
