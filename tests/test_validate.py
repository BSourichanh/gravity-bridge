"""Tests unitaires du validateur strict plugin/scripts/validate.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugin"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate as validate_mod  # noqa: E402


def _ignore_pycache(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]


class ValidateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _copy_plugin(self, name: str = "plugin-copy") -> Path:
        dst = self.root / name
        shutil.copytree(PLUGIN_ROOT, dst, ignore=_ignore_pycache)
        return dst

    def test_real_plugin_passes_strict_via_api_and_cli(self) -> None:
        rc = validate_mod.main(["--strict", "--plugin-root", str(PLUGIN_ROOT)])
        self.assertEqual(rc, 0)

        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS_DIR / "validate.py"),
                "--strict",
                "--plugin-root",
                str(PLUGIN_ROOT),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("[PASS]", proc.stdout)

    def test_detects_manifest_non_official_field_and_long_description(self) -> None:
        copy = self._copy_plugin("bad-manifest")
        m_path = copy / "plugin.json"
        data = json.loads(m_path.read_text(encoding="utf-8"))
        data["$schema"] = "https://example.com/schema.json"
        data["description"] = "x" * 161
        m_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        rep = validate_mod.Report()
        validate_mod.validate_plugin(copy, rep)
        joined = "\n".join(rep.errors)
        self.assertIn("champs non officiels", joined)
        self.assertIn("> 160", joined)

    def test_detects_non_square_svg_logo(self) -> None:
        copy = self._copy_plugin("bad-svg")
        svg_path = copy / "assets" / "logo.svg"
        svg_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 128"></svg>',
            encoding="utf-8",
        )

        rep = validate_mod.Report()
        validate_mod.validate_plugin(copy, rep)
        self.assertTrue(any("viewBox carré 1:1" in e for e in rep.errors), rep.errors)

    def test_detects_forbidden_hooks_json_and_rules_frontmatter(self) -> None:
        copy = self._copy_plugin("bad-hooks-rules")
        (copy / "hooks.json").write_text("{}", encoding="utf-8")
        (copy / "rules" / "AGENTS.md").write_text(
            "---\nname: bad\n---\n# Rule\n", encoding="utf-8"
        )

        rep = validate_mod.Report()
        validate_mod.validate_plugin(copy, rep)
        joined = "\n".join(rep.errors)
        self.assertIn("hooks.json détecté", joined)
        self.assertIn("frontmatter YAML", joined)

    def test_detects_non_stdlib_import_and_missing_bridge_mcp_key(self) -> None:
        copy = self._copy_plugin("bad-stdlib-mcp")
        (copy / "core" / "extra.py").write_text("import requests\n", encoding="utf-8")
        (copy / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "python3", "args": []}}}),
            encoding="utf-8",
        )

        rep = validate_mod.Report()
        validate_mod.validate_plugin(copy, rep)
        joined = "\n".join(rep.errors)
        self.assertIn("requests", joined)
        self.assertIn("serveur 'bridge' manquant", joined)

    def test_detects_personal_path_leak_in_markdown(self) -> None:
        copy = self._copy_plugin("bad-leak")
        leak_path = "C:\\" + "Users\\alice\\Documents\\secret.txt"
        (copy / "README.md").write_text(f"Chemin: {leak_path}\n", encoding="utf-8")

        rep = validate_mod.Report()
        validate_mod.validate_plugin(copy, rep)
        self.assertTrue(any("chemin personnel" in e for e in rep.errors), rep.errors)

    def test_detects_missing_relay_and_legacy_cli_files(self) -> None:
        copy = self._copy_plugin("bad-legacy")
        (copy / "scripts" / "sidecar_relay.py").unlink()
        (copy / "scripts" / "worker.py").write_text("# legacy\n", encoding="utf-8")

        rep = validate_mod.Report()
        validate_mod.validate_plugin(copy, rep)
        joined = "\n".join(rep.errors)
        self.assertIn("sidecar_relay.py", joined)
        self.assertIn("scripts/worker.py", joined)

    def test_fix_python_cmd_rewrites_only_target_copy_for_python_and_py3(self) -> None:
        repo_mcp_before = (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")

        copy_py = self._copy_plugin("fix-python")
        with mock.patch.object(validate_mod, "has_real_python3", return_value=False), mock.patch.object(
            validate_mod, "_detect_portable_python_cmd", return_value="python"
        ):
            self.assertEqual(validate_mod.fix_python_cmd(copy_py), 0)

        cfg_py = json.loads((copy_py / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg_py["mcpServers"]["bridge"]["command"], "python")
        self.assertEqual(
            cfg_py["mcpServers"]["bridge"]["args"], ["-B", "${PLUGIN_ROOT}/mcp/server.py"]
        )

        copy_py3 = self._copy_plugin("fix-py-3")
        with mock.patch.object(validate_mod, "has_real_python3", return_value=False), mock.patch.object(
            validate_mod, "_detect_portable_python_cmd", return_value="py -3"
        ):
            self.assertEqual(validate_mod.fix_python_cmd(copy_py3), 0)

        cfg_py3 = json.loads((copy_py3 / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg_py3["mcpServers"]["bridge"]["command"], "py")
        self.assertEqual(
            cfg_py3["mcpServers"]["bridge"]["args"],
            ["-3", "-B", "${PLUGIN_ROOT}/mcp/server.py"],
        )

        # Le dépôt source ne doit jamais être altéré.
        self.assertEqual(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8"),
            repo_mcp_before,
        )

    def test_detects_invalid_or_missing_module_contract(self) -> None:
        copy_missing = self._copy_plugin("missing-module")
        (copy_missing / "config" / "module.json").unlink()
        rep_missing = validate_mod.Report()
        validate_mod.validate_plugin(copy_missing, rep_missing)
        self.assertTrue(
            any("config/module.json introuvable" in e for e in rep_missing.errors),
            rep_missing.errors,
        )

        copy_bad = self._copy_plugin("bad-module")
        mod_path = copy_bad / "config" / "module.json"
        bad_payload = json.loads(mod_path.read_text(encoding="utf-8"))
        bad_payload["schema_version"] = 2
        bad_payload["id"] = "other"
        bad_payload["plugin_name"] = "wrong-plugin"
        bad_payload["storage"] = {"subdir": "wrong", "env_override": "bad_env"}
        bad_payload["commands"] = ["no-slash"]
        bad_payload["ui"] = {"mode": "", "target": ""}
        bad_payload["signals"] = {"produces": [""], "consumes": "invalid"}
        mod_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(bad_payload).encode("utf-8"))

        rep_bad = validate_mod.Report()
        validate_mod.validate_plugin(copy_bad, rep_bad)
        joined = "\n".join(rep_bad.errors)
        self.assertIn("BOM UTF-8", joined)
        self.assertIn("schema_version", joined)
        self.assertIn("'id'", joined)
        self.assertIn("plugin_name", joined)
        self.assertIn("storage.subdir", joined)
        self.assertIn("storage.env_override", joined)
        self.assertIn("commands", joined)
        self.assertIn("ui.mode", joined)
        self.assertIn("signals.produces", joined)
        self.assertIn("signals.consumes", joined)

    def test_d4_rules_thresholds_bom_crlf_skill_and_repo_agents(self) -> None:
        # 1. rules/AGENTS.md entre 601 et 1000 octets -> warning uniquement (échoue en --strict).
        copy_warn = self._copy_plugin("rules-warn")
        (copy_warn / "rules" / "AGENTS.md").write_bytes(b"# Rule\n" + (b"a" * 650) + b"\n")
        rep_warn = validate_mod.Report()
        validate_mod.validate_plugin(copy_warn, rep_warn)
        self.assertEqual(rep_warn.errors, [], rep_warn.errors)
        self.assertTrue(any("600 octets" in w for w in rep_warn.warnings), rep_warn.warnings)
        self.assertTrue(rep_warn.ok(strict=False))
        self.assertFalse(rep_warn.ok(strict=True))

        # 2. rules/AGENTS.md > 1000 octets, BOM et CRLF -> erreurs.
        copy_bad_rules = self._copy_plugin("rules-err")
        (copy_bad_rules / "rules" / "AGENTS.md").write_bytes(
            b"\xef\xbb\xbf# Rule\r\n" + (b"b" * 1010) + b"\r\n"
        )
        rep_bad_rules = validate_mod.Report()
        validate_mod.validate_plugin(copy_bad_rules, rep_bad_rules)
        joined_rules = "\n".join(rep_bad_rules.errors)
        self.assertIn("BOM UTF-8", joined_rules)
        self.assertIn("CRLF", joined_rules)
        self.assertIn("1000 octets", joined_rules)

        # 3. SKILL.md > 3500 octets, BOM, CRLF et references/examples.md manquant -> erreurs.
        copy_bad_skill = self._copy_plugin("skill-err")
        skill_path = copy_bad_skill / "skills" / "gravity-bridge" / "SKILL.md"
        skill_path.write_bytes(
            b"\xef\xbb\xbf---\r\nname: gravity-bridge\r\ndescription: test\r\n---\r\n"
            + (b"x" * 3550)
            + b"\r\n"
        )
        (copy_bad_skill / "skills" / "gravity-bridge" / "references" / "examples.md").unlink()
        rep_bad_skill = validate_mod.Report()
        validate_mod.validate_plugin(copy_bad_skill, rep_bad_skill)
        joined_skill = "\n".join(rep_bad_skill.errors)
        self.assertIn("SKILL.md ne doit pas contenir de BOM", joined_skill)
        self.assertIn("SKILL.md doit utiliser des fins de ligne LF", joined_skill)
        self.assertIn("3500 octets", joined_skill)
        self.assertIn("references/examples.md", joined_skill)

        # 4. AGENTS.md racine (lorsque root.name == "plugin") > 2500 octets, BOM, CRLF -> erreurs.
        repo_sim = self.root / "repo-sim"
        repo_sim.mkdir(parents=True, exist_ok=True)
        sim_plugin = repo_sim / "plugin"
        shutil.copytree(PLUGIN_ROOT, sim_plugin, ignore=_ignore_pycache)
        (repo_sim / "AGENTS.md").write_bytes(
            b"\xef\xbb\xbf# Agents\r\n" + (b"z" * 2550) + b"\r\n"
        )
        rep_repo = validate_mod.Report()
        validate_mod.validate_plugin(sim_plugin, rep_repo)
        joined_repo = "\n".join(rep_repo.errors)
        self.assertIn("AGENTS.md racine ne doit pas contenir de BOM", joined_repo)
        self.assertIn("AGENTS.md racine doit utiliser des fins de ligne LF", joined_repo)
        self.assertIn("2500 octets", joined_repo)


if __name__ == "__main__":
    unittest.main()
