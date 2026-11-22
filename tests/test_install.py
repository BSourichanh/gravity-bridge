"""Tests unitaires de l'installeur/désinstalleur install.py."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "gravity-bridge-plugin"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import install as install_mod  # noqa: E402
import validate as validate_mod  # noqa: E402


class InstallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = self.root / "plugins" / "gravity-bridge"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_install_reinstall_and_uninstall_preserve_repo_source(self) -> None:
        repo_mcp_before = json.loads(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repo_mcp_before["mcpServers"]["bridge"]["command"], "python3")

        rc = install_mod.main(["--target", str(self.target)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.target / "plugin.json").is_file())
        self.assertTrue((self.target / "mcp" / "server.py").is_file())
        self.assertTrue((self.target / "scripts" / "worker.py").is_file())
        self.assertEqual(list(self.target.rglob("__pycache__")), [])

        # Un fichier résiduel dans target doit être nettoyé lors d'une réinstallation propre.
        stale_file = self.target / "obsolete.txt"
        stale_file.write_text("stale", encoding="utf-8")
        rc_reinstall = install_mod.main(["--target", str(self.target)])
        self.assertEqual(rc_reinstall, 0)
        self.assertFalse(stale_file.exists())

        # Le dépôt source garde toujours "python3".
        repo_mcp_after = json.loads(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repo_mcp_after["mcpServers"]["bridge"]["command"], "python3")

        # Désinstallation propre et idempotente.
        rc_uninst = install_mod.main(["--uninstall", "--target", str(self.target)])
        self.assertEqual(rc_uninst, 0)
        self.assertFalse(self.target.exists())
        self.assertEqual(
            install_mod.main(["--uninstall", "--target", str(self.target)]), 0
        )

    def test_windows_python3_adaptation_on_installed_copy(self) -> None:
        with mock.patch.object(
            validate_mod, "has_real_python3", return_value=False
        ), mock.patch.object(
            validate_mod, "_detect_portable_python_cmd", return_value="py -3"
        ), mock.patch.object(
            install_mod, "_load_validate_module", return_value=validate_mod
        ):
            rc = install_mod.install_plugin(
                PLUGIN_ROOT,
                self.target,
                dry_run=False,
                run_validate=True,
            )
        self.assertEqual(rc, 0)
        installed_cfg = json.loads(
            (self.target / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(installed_cfg["mcpServers"]["bridge"]["command"], "py")
        self.assertEqual(
            installed_cfg["mcpServers"]["bridge"]["args"],
            ["-3", "-B", "${PLUGIN_ROOT}/mcp/server.py"],
        )

        # Le dépôt source reste inchangé.
        repo_cfg = json.loads(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repo_cfg["mcpServers"]["bridge"]["command"], "python3")

    def test_dry_run_does_not_create_or_delete_target(self) -> None:
        rc = install_mod.main(["--target", str(self.target), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.target.exists())

        # Crée un dossier puis vérifie que --uninstall --dry-run le conserve.
        self.target.mkdir(parents=True)
        (self.target / "marker.txt").write_text("keep", encoding="utf-8")
        rc_un = install_mod.main(["--uninstall", "--target", str(self.target), "--dry-run"])
        self.assertEqual(rc_un, 0)
        self.assertTrue((self.target / "marker.txt").is_file())

    def test_cli_subprocess_install_and_uninstall(self) -> None:
        proc_in = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "install.py"),
                "--target",
                str(self.target),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc_in.returncode, 0, proc_in.stdout + proc_in.stderr)
        self.assertTrue((self.target / "plugin.json").is_file())

        proc_out = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "install.py"),
                "--uninstall",
                "--target",
                str(self.target),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(proc_out.returncode, 0, proc_out.stdout + proc_out.stderr)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
