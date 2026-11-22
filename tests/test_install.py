"""Tests unitaires de l'installeur/désinstalleur et liaison CLI install.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
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
        self.home_dir = self.root / "bridge-home"
        self.sidecars_dir = self.root / "sidecars"
        self.config_path = self.root / "config.json"
        self.ws_repo = self.root / "sample-project"
        self.ws_repo.mkdir()

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
        self.assertTrue((self.target / "scripts" / "sidecar_relay.py").is_file())
        self.assertFalse((self.target / "scripts" / "worker.py").exists())
        self.assertEqual(list(self.target.rglob("__pycache__")), [])

        stale_file = self.target / "obsolete.txt"
        stale_file.write_text("stale", encoding="utf-8")
        rc_reinstall = install_mod.main(["--target", str(self.target)])
        self.assertEqual(rc_reinstall, 0)
        self.assertFalse(stale_file.exists())

        repo_mcp_after = json.loads(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repo_mcp_after["mcpServers"]["bridge"]["command"], "python3")

        rc_uninst = install_mod.main(["--uninstall", "--target", str(self.target)])
        self.assertEqual(rc_uninst, 0)
        self.assertFalse(self.target.exists())
        self.assertEqual(
            install_mod.main(["--uninstall", "--target", str(self.target)]), 0
        )

    def test_reinstall_handles_locked_target_root_directory(self) -> None:
        self.assertEqual(install_mod.main(["--target", str(self.target)]), 0)
        stale = self.target / "stale.txt"
        stale.write_text("old", encoding="utf-8")

        import shutil

        real_rmtree = shutil.rmtree
        target_resolved = self.target.resolve()

        def _rmtree_locked_root(path, *args, **kwargs):
            if Path(path).resolve() == target_resolved:
                raise PermissionError(32, "Directory locked by active MCP cwd")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(install_mod.shutil, "rmtree", side_effect=_rmtree_locked_root):
            rc = install_mod.main(["--target", str(self.target)])

        self.assertEqual(rc, 0)
        self.assertFalse(stale.exists())
        self.assertTrue((self.target / "plugin.json").is_file())
        self.assertTrue((self.target / "scripts" / "sidecar_relay.py").is_file())

    def test_link_and_unlink_cli_point_to_installed_target_relay(self) -> None:
        projects_dir = self.root / "projects"
        # --link avant installation échoue proprement.
        rc_missing = install_mod.main(
            [
                "--link",
                str(self.ws_repo),
                "--target",
                str(self.target),
                "--sidecars-dir",
                str(self.sidecars_dir),
                "--config-path",
                str(self.config_path),
                "--projects-dir",
                str(projects_dir),
                "--home",
                str(self.home_dir),
            ]
        )
        self.assertEqual(rc_missing, 1)

        # Installe puis lie le workspace avec --slug et --project-id dédié neuf.
        self.assertEqual(install_mod.main(["--target", str(self.target)]), 0)
        rc_link = install_mod.main(
            [
                "--link",
                str(self.ws_repo),
                "--slug",
                "sample-ws",
                "--project-id",
                "proj-sample-bridge-1",
                "--configure-config",
                "--target",
                str(self.target),
                "--sidecars-dir",
                str(self.sidecars_dir),
                "--config-path",
                str(self.config_path),
                "--projects-dir",
                str(projects_dir),
                "--home",
                str(self.home_dir),
            ]
        )
        self.assertEqual(rc_link, 0)

        sidecar_json = self.sidecars_dir / "gravity-bridge-sample-ws" / "sidecar.json"
        self.assertTrue(sidecar_json.is_file())
        sc = json.loads(sidecar_json.read_text(encoding="utf-8"))
        self.assertIn(
            str((self.target / "scripts" / "sidecar_relay.py").resolve()),
            sc["args"],
        )
        self.assertIn("sample-ws", sc["args"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cfg["sidecars"]["gravity-bridge-sample-ws"],
            {"enabled": True, "projectId": "proj-sample-bridge-1", "schemaVersion": 1},
        )
        bridge_proj = projects_dir / "proj-sample-bridge-1.json"
        self.assertTrue(bridge_proj.is_file())
        self.assertEqual(
            json.loads(bridge_proj.read_text(encoding="utf-8"))["name"],
            "sample-ws (Bridge)",
        )

        # --unlink supprime le micro-sidecar, le projet dédié et nettoie config.json.
        rc_unlink = install_mod.main(
            [
                "--unlink",
                "sample-ws",
                "--configure-config",
                "--target",
                str(self.target),
                "--sidecars-dir",
                str(self.sidecars_dir),
                "--config-path",
                str(self.config_path),
                "--projects-dir",
                str(projects_dir),
                "--home",
                str(self.home_dir),
            ]
        )
        self.assertEqual(rc_unlink, 0)
        self.assertFalse(sidecar_json.parent.exists())
        self.assertFalse(bridge_proj.exists())
        cfg_after = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("gravity-bridge-sample-ws", cfg_after["sidecars"])

    def test_link_cli_creates_dedicated_bridge_project_mirrors_source_and_cleans_on_unlink(self) -> None:
        self.assertEqual(install_mod.main(["--target", str(self.target)]), 0)
        projects_dir = self.root / "projects"
        projects_dir.mkdir()
        raw_posix = self.ws_repo.resolve().as_posix()
        uri = (
            f"file:///{raw_posix[0].lower()}%3A{raw_posix[2:]}"
            if len(raw_posix) >= 2 and raw_posix[1] == ":"
            else f"file://{raw_posix}"
        )
        user_proj = projects_dir / "proj-auto-99.json"
        user_proj.write_text(
            json.dumps(
                {
                    "id": "proj-auto-99",
                    "name": "sample-project",
                    "projectResources": {"resources": [{"gitFolder": {"folderUri": uri}}]},
                    "settings": {"artifactReviewMode": "TURBO"},
                    "sidecar": {"sidecarId": "gravity-bridge-sample-project"},
                }
            ),
            encoding="utf-8",
        )

        rc = install_mod.main(
            [
                "--link",
                str(self.ws_repo),
                "--source-project-id",
                "proj-auto-99",
                "--configure-config",
                "--target",
                str(self.target),
                "--sidecars-dir",
                str(self.sidecars_dir),
                "--config-path",
                str(self.config_path),
                "--projects-dir",
                str(projects_dir),
                "--home",
                str(self.home_dir),
            ]
        )
        self.assertEqual(rc, 0)
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        bridge_id = cfg["sidecars"]["gravity-bridge-sample-project"]["projectId"]
        self.assertNotEqual(bridge_id, "proj-auto-99")
        self.assertEqual(cfg["sidecars"]["gravity-bridge-sample-project"]["enabled"], True)
        self.assertEqual(cfg["sidecars"]["gravity-bridge-sample-project"]["schemaVersion"], 1)

        # Le projet utilisateur a été réparé (sans "sidecar") et préservé.
        user_after = json.loads(user_proj.read_text(encoding="utf-8"))
        self.assertNotIn("sidecar", user_after)

        # Le projet dédié porte le suffixe (Bridge) et reflète les ressources/settings.
        bridge_file = projects_dir / f"{bridge_id}.json"
        self.assertTrue(bridge_file.is_file())
        bridge_data = json.loads(bridge_file.read_text(encoding="utf-8"))
        self.assertEqual(bridge_data["name"], "sample-project (Bridge)")
        self.assertEqual(bridge_data["settings"], {"artifactReviewMode": "TURBO"})
        self.assertEqual(bridge_data["projectResources"], user_after["projectResources"])

        # --unlink avec --projects-dir supprime uniquement le projet dédié et laisse le projet utilisateur.
        rc_un = install_mod.main(
            [
                "--unlink",
                "sample-project",
                "--target",
                str(self.target),
                "--sidecars-dir",
                str(self.sidecars_dir),
                "--config-path",
                str(self.config_path),
                "--projects-dir",
                str(projects_dir),
                "--home",
                str(self.home_dir),
            ]
        )
        self.assertEqual(rc_un, 0)
        self.assertFalse(bridge_file.exists())
        self.assertTrue(user_proj.is_file())

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

        repo_cfg = json.loads(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repo_cfg["mcpServers"]["bridge"]["command"], "python3")

    def test_dry_run_does_not_create_or_delete_target(self) -> None:
        rc = install_mod.main(["--target", str(self.target), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.target.exists())

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
