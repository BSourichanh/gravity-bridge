"""Tests unitaires de l'installeur/désinstalleur et liaison CLI install.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugin"
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
        self.assertEqual(install_mod.PLUGIN_SOURCE, PLUGIN_ROOT)
        repo_mcp_before = json.loads(
            (PLUGIN_ROOT / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repo_mcp_before["mcpServers"]["bridge"]["command"], "python3")

        rc = install_mod.main(["--target", str(self.target)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.target / "plugin.json").is_file())
        self.assertTrue((self.target / "config" / "module.json").is_file())
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

    def test_no_fix_python_skips_fix_python_cmd_and_preserves_python3(self) -> None:
        self.assertFalse(install_mod.build_parser().parse_args([]).no_fix_python)
        self.assertTrue(
            install_mod.build_parser().parse_args(["--no-fix-python"]).no_fix_python
        )

        dry_out = io.StringIO()
        with contextlib.redirect_stdout(dry_out):
            rc_dry = install_mod.main(
                ["--target", str(self.target), "--no-fix-python", "--dry-run"]
            )
        self.assertEqual(rc_dry, 0)
        self.assertNotIn("Adaptation éventuelle 'python3'", dry_out.getvalue())
        self.assertFalse(self.target.exists())

        with mock.patch.object(
            validate_mod, "has_real_python3", return_value=False
        ), mock.patch.object(
            validate_mod, "_detect_portable_python_cmd", return_value="py -3"
        ), mock.patch.object(
            validate_mod, "fix_python_cmd", wraps=validate_mod.fix_python_cmd
        ) as mock_fix, mock.patch.object(
            install_mod, "_load_validate_module", return_value=validate_mod
        ):
            rc = install_mod.main(["--target", str(self.target), "--no-fix-python"])

        self.assertEqual(rc, 0)
        mock_fix.assert_not_called()
        installed_cfg = json.loads(
            (self.target / "mcp_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(installed_cfg["mcpServers"]["bridge"]["command"], "python3")
        self.assertEqual(
            installed_cfg["mcpServers"]["bridge"]["args"],
            ["-B", "${PLUGIN_ROOT}/mcp/server.py"],
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

    def test_check_is_read_only_and_detects_state_and_drift(self) -> None:
        # 1. Cible absente : code 0 et zéro écriture disque (aucun __pycache__ généré).
        pycache_dir = PLUGIN_ROOT / "scripts" / "__pycache__"
        had_pycache = pycache_dir.exists()
        rc_absent = install_mod.main(["--check", "--target", str(self.target)])
        self.assertEqual(rc_absent, 0)
        self.assertFalse(self.target.exists())
        if not had_pycache:
            self.assertFalse(pycache_dir.exists())

        # 2. Après installation : à jour, code 0 même avec --strict.
        self.assertEqual(install_mod.main(["--target", str(self.target)]), 0)
        rc_ok = install_mod.main(["--check", "--strict", "--target", str(self.target)])
        self.assertEqual(rc_ok, 0)

        # 3. Dérive simulée : --check avertit (0), --check --strict échoue (1).
        (self.target / "drift.txt").write_text("extra", encoding="utf-8")
        self.assertEqual(install_mod.main(["--check", "--target", str(self.target)]), 0)
        self.assertEqual(
            install_mod.main(["--check", "--strict", "--target", str(self.target)]),
            1,
        )

        # 4. Source invalide : code 1.
        bad_src = self.root / "bad-plugin"
        bad_src.mkdir()
        self.assertEqual(
            install_mod.main(
                ["--check", "--plugin-root", str(bad_src), "--target", str(self.target)]
            ),
            1,
        )

    def test_scope_workspace_install_uninstall_and_missing_workspace_error(self) -> None:
        # --scope workspace sans --workspace -> code CLI 2.
        with self.assertRaises(SystemExit) as ctx:
            install_mod.main(["--scope", "workspace"])
        self.assertEqual(ctx.exception.code, 2)

        ws_target = self.ws_repo / ".agents" / "plugins" / "gravity-bridge"
        rc_check = install_mod.main(
            ["--check", "--scope", "workspace", "--workspace", str(self.ws_repo)]
        )
        self.assertEqual(rc_check, 0)
        self.assertFalse(ws_target.exists())

        rc_in = install_mod.main(
            ["--scope", "workspace", "--workspace", str(self.ws_repo)]
        )
        self.assertEqual(rc_in, 0)
        self.assertTrue((ws_target / "plugin.json").is_file())
        self.assertTrue((ws_target / "config" / "module.json").is_file())

        rc_un = install_mod.main(
            ["--uninstall", "--scope", "workspace", "--workspace", str(self.ws_repo)]
        )
        self.assertEqual(rc_un, 0)
        self.assertFalse(ws_target.exists())

    def test_force_required_for_occupied_target_dir_and_file(self) -> None:
        # Cible occupée par un dossier non-plugin.
        self.target.mkdir(parents=True)
        foreign_file = self.target / "foreign.txt"
        foreign_file.write_text("keep", encoding="utf-8")

        rc_refused = install_mod.main(["--target", str(self.target)])
        self.assertEqual(rc_refused, 1)
        self.assertTrue(foreign_file.is_file())

        rc_forced = install_mod.main(["--target", str(self.target), "--force"])
        self.assertEqual(rc_forced, 0)
        self.assertFalse(foreign_file.exists())
        self.assertTrue((self.target / "plugin.json").is_file())

        # Cible occupée par un fichier régulier.
        file_target = self.root / "occupied-file"
        file_target.write_text("not-a-dir", encoding="utf-8")
        self.assertEqual(install_mod.main(["--target", str(file_target)]), 1)
        self.assertEqual(
            install_mod.main(["--target", str(file_target), "--force"]),
            0,
        )
        self.assertTrue((file_target / "plugin.json").is_file())

    def test_skip_validate_no_validate_alias_and_strict_override(self) -> None:
        with mock.patch.object(
            install_mod, "_run_validate_subprocess", return_value=0
        ) as mock_val:
            self.assertEqual(
                install_mod.main(["--target", str(self.target), "--skip-validate"]),
                0,
            )
            mock_val.assert_not_called()

            self.assertEqual(
                install_mod.main(["--target", str(self.target), "--no-validate"]),
                0,
            )
            mock_val.assert_not_called()

            # --strict force la validation même si --skip-validate est présent.
            self.assertEqual(
                install_mod.main(
                    ["--target", str(self.target), "--skip-validate", "--strict"]
                ),
                0,
            )
            mock_val.assert_called_once()

    def test_method_agy_constraints_and_delegation(self) -> None:
        # --method agy rejeté avec --scope workspace (code 2).
        with self.assertRaises(SystemExit) as ctx_ws:
            install_mod.main(
                [
                    "--method",
                    "agy",
                    "--scope",
                    "workspace",
                    "--workspace",
                    str(self.ws_repo),
                ]
            )
        self.assertEqual(ctx_ws.exception.code, 2)

        # --method agy rejeté avec --target (code 2).
        with self.assertRaises(SystemExit) as ctx_tgt:
            install_mod.main(["--method", "agy", "--target", str(self.target)])
        self.assertEqual(ctx_tgt.exception.code, 2)

        # Délégation mockée vers agy (zéro écriture dans ~/.gemini/).
        fake_global = self.root / "global-plugins" / "gravity-bridge"
        completed = subprocess.CompletedProcess(
            args=["agy", "plugin", "install", str(PLUGIN_ROOT)],
            returncode=0,
            stdout="Installed\n",
            stderr="",
        )
        with mock.patch.object(
            install_mod, "default_target_dir", return_value=fake_global
        ), mock.patch.object(
            install_mod.shutil, "which", return_value="agy"
        ), mock.patch.object(
            install_mod, "_run_validate_subprocess", return_value=0
        ) as mock_val, mock.patch.object(
            install_mod.subprocess, "run", return_value=completed
        ) as mock_run:
            rc = install_mod.main(
                ["--method", "agy", "--plugin-root", str(PLUGIN_ROOT)]
            )
            self.assertEqual(rc, 0)
            mock_val.assert_called_once()
            mock_run.assert_called_once()
            self.assertEqual(
                mock_run.call_args[0][0],
                ["agy", "plugin", "install", str(PLUGIN_ROOT.resolve())],
            )

            rc_un = install_mod.main(["--uninstall", "--method", "agy"])
            self.assertEqual(rc_un, 0)
            self.assertEqual(
                mock_run.call_args[0][0],
                ["agy", "plugin", "uninstall", "gravity-bridge"],
            )


if __name__ == "__main__":
    unittest.main()
