"""Tests unitaires du gestionnaire de micro-sidecars core.sidecar_manager (100 % stdlib)."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import core.sidecar_manager as sm_mod  # noqa: E402
from core.security import ValidationError  # noqa: E402
from core.store import BridgeStore  # noqa: E402


class SidecarManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home_dir = self.root / "bridge-home"
        self.sidecars_dir = self.root / "sidecars"
        self.config_path = self.root / "config.json"
        self.projects_dir = self.root / "projects"
        self.ws_target = self.root / "gravity-metrics"
        self.ws_target.mkdir()
        self.store = BridgeStore(home=self.home_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _to_encoded_file_uri(self, folder: Path, *, swap_win_case: bool = False) -> str:
        raw = folder.resolve().as_posix()
        if len(raw) >= 2 and raw[1] == ":":
            drive = raw[0].lower() if swap_win_case else raw[0].upper()
            rest = raw[2:].swapcase() if swap_win_case else raw[2:]
            return f"file:///{drive}%3A{rest}"
        return f"file://{raw}"

    def test_detect_local_python_command_skips_windowsapps_stub(self) -> None:
        with mock.patch.object(os, "name", "nt"), mock.patch.object(
            sm_mod.shutil,
            "which",
            side_effect=lambda name: {
                "python": r"C:\Users\demo\AppData\Local\Microsoft\WindowsApps\python.exe",
                "python3": r"C:\Users\demo\AppData\Local\Microsoft\WindowsApps\python3.exe",
                "py": r"C:\Windows\py.exe",
            }.get(name),
        ):
            cmd, args = sm_mod.detect_local_python_command()
            self.assertEqual((cmd, args), ("py", ["-3"]))

        with mock.patch.object(os, "name", "posix"), mock.patch.object(
            sm_mod.shutil,
            "which",
            side_effect=lambda name: "/usr/bin/python3" if name == "python3" else None,
        ):
            cmd, args = sm_mod.detect_local_python_command()
            self.assertEqual((cmd, args), ("python3", []))

    def test_path_to_folder_uri_round_trip_windows_spaces_and_posix(self) -> None:
        spaced_dir = self.root / "My Repo #1"
        spaced_dir.mkdir()
        uri_local = sm_mod._path_to_folder_uri(spaced_dir)
        self.assertTrue(uri_local.startswith("file:///"))
        self.assertIn("My%20Repo%20%231", uri_local)
        round_tripped = sm_mod._folder_uri_to_path(uri_local)
        self.assertIsNotNone(round_tripped)
        self.assertEqual(sm_mod._norm_path_key(round_tripped), sm_mod._norm_path_key(spaced_dir))

        # Cas explicites Windows, UNC et POSIX sans dépendre de l'OS hôte.
        with mock.patch.object(
            Path, "resolve", return_value=PureWindowsPath(r"D:\Work\My Space\Repo")
        ):
            win_uri = sm_mod._path_to_folder_uri(Path("dummy-win"))
        self.assertEqual(win_uri, "file:///d%3A/Work/My%20Space/Repo")
        parsed_win = sm_mod._folder_uri_to_path(win_uri)
        self.assertIsNotNone(parsed_win)
        self.assertEqual(parsed_win.as_posix().lower(), "d:/work/my space/repo")

        with mock.patch.object(
            Path, "resolve", return_value=PureWindowsPath(r"\\fileserver\repos\my project")
        ):
            unc_uri = sm_mod._path_to_folder_uri(Path("dummy-unc"))
        self.assertEqual(unc_uri, "file://fileserver/repos/my%20project")

        with mock.patch.object(
            Path, "resolve", return_value=PurePosixPath("/opt/work/my repo")
        ):
            posix_uri = sm_mod._path_to_folder_uri(Path("dummy-posix"))
        self.assertEqual(posix_uri, "file:///opt/work/my%20repo")
        self.assertEqual(sm_mod._folder_uri_to_path(posix_uri), Path("/opt/work/my repo"))

    def test_link_creates_dedicated_project_without_user_project_git_vs_local(self) -> None:
        # 1. Dépôt sans .git -> localFolder.
        res_local = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        self.assertEqual(res_local["action"], "linked")
        self.assertTrue(res_local["bridge_project_created"])
        self.assertFalse(res_local["project_auto_detected"])
        self.assertIsNone(res_local["source_project_id"])
        self.assertEqual(res_local["project_name"], "gravity-metrics (Bridge)")
        self.assertTrue(res_local["config_updated"])

        proj_file = Path(res_local["bridge_project_path"])
        self.assertTrue(proj_file.is_file())
        pdata = json.loads(proj_file.read_text(encoding="utf-8"))
        self.assertEqual(pdata["id"], res_local["project_id"])
        self.assertEqual(pdata["name"], "gravity-metrics (Bridge)")
        self.assertEqual(pdata["sidecar"], {"sidecarId": "gravity-bridge-gravity-metrics"})
        self.assertFalse(pdata["isWorkspaceOnly"])
        res_items = pdata["projectResources"]["resources"]
        self.assertEqual(len(res_items), 1)
        self.assertIn("localFolder", res_items[0])

        # 2. Dépôt avec .git -> gitFolder + nouvel UUID dédié.
        git_repo = self.root / "git-repo"
        (git_repo / ".git").mkdir(parents=True)
        res_git = sm_mod.link_workspace_sidecar(
            self.store,
            str(git_repo),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        gdata = json.loads(Path(res_git["bridge_project_path"]).read_text(encoding="utf-8"))
        self.assertEqual(gdata["name"], "git-repo (Bridge)")
        self.assertIn("gitFolder", gdata["projectResources"]["resources"][0])
        self.assertEqual(
            sm_mod._norm_path_key(
                sm_mod._folder_uri_to_path(gdata["projectResources"]["resources"][0]["gitFolder"]["folderUri"])
            ),
            sm_mod._norm_path_key(git_repo),
        )

    def test_mirrors_user_project_single_and_multi_folder_and_keeps_user_project_untouched(self) -> None:
        self.projects_dir.mkdir(parents=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "plugins": {"gravity": {"enabled": True}},
                    "sidecars": {"other/widget": {"enabled": True, "projectId": "p-other"}},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        ws_grav = self.root / "gravity"
        ws_arch = self.root / "gravity-architect"
        ws_grav.mkdir()
        ws_arch.mkdir()

        # Projet utilisateur mono-dossier pour gravity-metrics avec settings et permissionGrants.
        metrics_user_path = self.projects_dir / "33333333-3333-3333-3333-333333333333.json"
        metrics_user_payload = {
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "gravity-metrics",
            "projectResources": {
                "resources": [
                    {
                        "gitFolder": {
                            "folderUri": self._to_encoded_file_uri(
                                self.ws_target, swap_win_case=(os.name == "nt")
                            ),
                            "defaultBranch": "master",
                        }
                    }
                ]
            },
            "permissionGrants": {"permissionGrants": {"command(pytest)": True}, "v2Migrated": True},
            "settings": {"autoExecutionPolicy": "EAGER"},
            "isWorkspaceOnly": False,
        }
        metrics_user_path.write_text(json.dumps(metrics_user_payload), encoding="utf-8")

        # Projet utilisateur multi-dossiers pour gravity-architect.
        arch_user_path = self.projects_dir / "22222222-2222-2222-2222-222222222222.json"
        arch_user_payload = {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "gravity-architect",
            "projectResources": {
                "resources": [
                    {"gitFolder": {"folderUri": self._to_encoded_file_uri(ws_grav)}},
                    {"gitFolder": {"folderUri": self._to_encoded_file_uri(self.ws_target)}},
                    {"localFolder": {"folderUri": self._to_encoded_file_uri(ws_arch)}},
                ]
            },
            "permissionGrants": {"permissionGrants": {}, "v2Migrated": True},
            "settings": {"artifactReviewMode": "TURBO"},
            "isWorkspaceOnly": False,
        }
        arch_user_path.write_text(json.dumps(arch_user_payload), encoding="utf-8")

        res_m = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        self.assertTrue(res_m["project_auto_detected"])
        self.assertEqual(res_m["source_project_id"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(res_m["source_project_name"], "gravity-metrics")
        self.assertEqual(res_m["project_name"], "gravity-metrics (Bridge)")
        self.assertNotEqual(res_m["project_id"], "33333333-3333-3333-3333-333333333333")

        # Projet utilisateur strictement intact (zéro clé "sidecar").
        user_after = json.loads(metrics_user_path.read_text(encoding="utf-8"))
        self.assertEqual(user_after, metrics_user_payload)
        self.assertNotIn("sidecar", user_after)

        # Projet dédié porte le miroir exact + sidecarId.
        bridge_data = json.loads(Path(res_m["bridge_project_path"]).read_text(encoding="utf-8"))
        self.assertEqual(bridge_data["projectResources"], metrics_user_payload["projectResources"])
        self.assertEqual(bridge_data["permissionGrants"], metrics_user_payload["permissionGrants"])
        self.assertEqual(bridge_data["settings"], metrics_user_payload["settings"])
        self.assertEqual(bridge_data["sidecar"], {"sidecarId": "gravity-bridge-gravity-metrics"})
        self.assertEqual(
            set(bridge_data.keys()),
            {"id", "name", "projectResources", "permissionGrants", "settings", "isWorkspaceOnly", "sidecar"},
        )

        # Multi-dossiers pour gravity-architect.
        res_a = sm_mod.link_workspace_sidecar(
            self.store,
            str(ws_arch),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        self.assertEqual(res_a["source_project_id"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(res_a["project_name"], "gravity-architect (Bridge)")
        arch_bridge_data = json.loads(Path(res_a["bridge_project_path"]).read_text(encoding="utf-8"))
        self.assertEqual(arch_bridge_data["projectResources"], arch_user_payload["projectResources"])
        self.assertEqual(arch_bridge_data["settings"], {"artifactReviewMode": "TURBO"})

        # config.json préserve les autres plugins/sidecars.
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["plugins"], {"gravity": {"enabled": True}})
        self.assertEqual(cfg["sidecars"]["other/widget"], {"enabled": True, "projectId": "p-other"})
        self.assertEqual(
            cfg["sidecars"]["gravity-bridge-gravity-metrics"],
            {"enabled": True, "projectId": res_m["project_id"], "schemaVersion": 1},
        )

    def test_idempotent_relink_reuses_uuid_and_upgrades_empty_orphan_project(self) -> None:
        self.projects_dir.mkdir(parents=True)
        # Ancien projet sidecar vide auto-créé par Antigravity + un second doublon vide.
        orphan_1 = self.projects_dir / "88d716f7-402a-4929-bd5e-be965ca69fdd.json"
        orphan_1.write_text(
            json.dumps(
                {
                    "id": "88d716f7-402a-4929-bd5e-be965ca69fdd",
                    "name": "gravity-bridge-gravity-metrics",
                    "projectResources": {},
                    "sidecar": {"sidecarId": "gravity-bridge-gravity-metrics"},
                }
            ),
            encoding="utf-8",
        )
        orphan_2 = self.projects_dir / "99999999-402a-4929-bd5e-be965ca69fdd.json"
        orphan_2.write_text(
            json.dumps(
                {
                    "id": "99999999-402a-4929-bd5e-be965ca69fdd",
                    "name": "Gravity Bridge · gravity-metrics",
                    "projectResources": {},
                    "sidecar": {"sidecarId": "gravity-bridge-gravity-metrics"},
                }
            ),
            encoding="utf-8",
        )

        first = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        self.assertEqual(first["project_id"], "88d716f7-402a-4929-bd5e-be965ca69fdd")
        self.assertFalse(first["bridge_project_created"])
        self.assertEqual(first["project_name"], "gravity-metrics (Bridge)")
        self.assertEqual(first["cleaned_orphan_projects"], [str(orphan_2)])
        self.assertFalse(orphan_2.exists())
        self.assertTrue(orphan_1.is_file())

        second = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        self.assertEqual(second["project_id"], first["project_id"])
        self.assertEqual(second["bridge_project_path"], first["bridge_project_path"])
        self.assertFalse(second["bridge_project_created"])

    def test_repairs_polluted_user_project_on_link_and_unlink(self) -> None:
        self.projects_dir.mkdir(parents=True)
        polluted_path = self.projects_dir / "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1.json"
        polluted_path.write_text(
            json.dumps(
                {
                    "id": "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1",
                    "name": "gravity-metrics",
                    "projectResources": {
                        "resources": [
                            {"gitFolder": {"folderUri": self._to_encoded_file_uri(self.ws_target), "defaultBranch": "master"}}
                        ]
                    },
                    "permissionGrants": {"permissionGrants": {}, "v2Migrated": True},
                    "settings": {"sandboxMode": "off"},
                    "isWorkspaceOnly": False,
                    "sidecar": {"sidecarId": "gravity-bridge-gravity-metrics"},
                }
            ),
            encoding="utf-8",
        )
        self.config_path.write_text(
            json.dumps(
                {
                    "sidecars": {
                        "gravity-bridge-gravity-metrics": {
                            "enabled": True,
                            "projectId": "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1",
                            "schemaVersion": 1,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        # discover_antigravity_project classique continue d'ignorer le pollué, mais include_polluted le retrouve.
        self.assertIsNone(
            sm_mod.discover_antigravity_project(
                str(self.ws_target), slug="gravity-metrics", projects_dir=self.projects_dir
            )
        )
        self.assertIsNotNone(
            sm_mod.discover_antigravity_project(
                str(self.ws_target),
                slug="gravity-metrics",
                projects_dir=self.projects_dir,
                include_polluted_sidecar_id="gravity-bridge-gravity-metrics",
            )
        )

        res = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        self.assertEqual(res["source_project_id"], "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1")
        self.assertEqual(len(res["repaired_user_projects"]), 1)
        self.assertEqual(
            res["repaired_user_projects"][0]["id"],
            "6e0df4c7-d5e9-4e8f-9689-343b5ddf84b1",
        )

        # Le projet utilisateur existe toujours sans "sidecar".
        user_after = json.loads(polluted_path.read_text(encoding="utf-8"))
        self.assertNotIn("sidecar", user_after)
        self.assertEqual(user_after["settings"], {"sandboxMode": "off"})

        # Le projet dédié a tout recopié et config.json pointe dessus.
        bridge_path = Path(res["bridge_project_path"])
        self.assertTrue(bridge_path.is_file())
        bridge_data = json.loads(bridge_path.read_text(encoding="utf-8"))
        self.assertEqual(bridge_data["settings"], {"sandboxMode": "off"})
        self.assertEqual(bridge_data["sidecar"], {"sidecarId": "gravity-bridge-gravity-metrics"})
        cfg_after = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cfg_after["sidecars"]["gravity-bridge-gravity-metrics"]["projectId"],
            res["project_id"],
        )

        # Si on re-pollue artificiellement avant uninstall -> uninstall répare aussi et supprime le dédié uniquement.
        user_repolluted = dict(user_after)
        user_repolluted["sidecar"] = {"sidecarId": "gravity-bridge-gravity-metrics"}
        polluted_path.write_text(json.dumps(user_repolluted), encoding="utf-8")

        unres = sm_mod.link_workspace_sidecar(
            self.store,
            "gravity-metrics",
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
            uninstall=True,
        )
        self.assertEqual(unres["action"], "unlinked")
        self.assertTrue(unres["removed_sidecar_dir"])
        self.assertTrue(unres["config_updated"])
        self.assertEqual(unres["removed_bridge_projects"], [str(bridge_path)])
        self.assertEqual(len(unres["repaired_user_projects"]), 1)
        self.assertFalse(bridge_path.exists())
        self.assertTrue(polluted_path.is_file())
        self.assertNotIn("sidecar", json.loads(polluted_path.read_text(encoding="utf-8")))
        ws_db = self.store.get_workspace("gravity-metrics")
        self.assertEqual(ws_db["sidecar_status"], "unlinked")
        self.assertIsNone(ws_db["project_id"])

    def test_rejects_explicit_user_project_id_and_supports_source_project_id_and_no_configure(self) -> None:
        self.projects_dir.mkdir(parents=True)
        user_proj = self.projects_dir / "user-proj-1.json"
        user_proj.write_text(
            json.dumps(
                {
                    "id": "user-proj-1",
                    "name": "Custom Metrics",
                    "projectResources": {
                        "resources": [{"gitFolder": {"folderUri": self._to_encoded_file_uri(self.ws_target)}}]
                    },
                }
            ),
            encoding="utf-8",
        )

        # project_id pointant vers un projet utilisateur -> ValidationError fail-closed sans écriture.
        with self.assertRaises(ValidationError):
            sm_mod.link_workspace_sidecar(
                self.store,
                str(self.ws_target),
                sidecars_dir=self.sidecars_dir,
                config_path=self.config_path,
                projects_dir=self.projects_dir,
                project_id="user-proj-1",
            )
        self.assertFalse((self.sidecars_dir / "gravity-bridge-gravity-metrics").exists())
        self.assertFalse(self.config_path.exists())

        # configure_config=False -> écrit uniquement sidecar.json sans config.json ni projet dédié.
        res_noconf = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
            configure_config=False,
        )
        self.assertFalse(res_noconf["config_updated"])
        self.assertIsNone(res_noconf["bridge_project_path"])
        self.assertTrue(Path(res_noconf["sidecar_json_path"]).is_file())
        self.assertFalse(self.config_path.exists())
        self.assertEqual(len(list(self.projects_dir.glob("*.json"))), 1)

        # source_project_id + explicit dedicated project_id neuf -> succès.
        res_explicit = sm_mod.link_workspace_sidecar(
            self.store,
            "gravity-metrics",
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
            project_id="bridge-custom-42",
            source_project_id="user-proj-1",
            configure_config=True,
        )
        self.assertEqual(res_explicit["project_id"], "bridge-custom-42")
        self.assertEqual(res_explicit["project_name"], "Custom Metrics (Bridge)")
        self.assertEqual(res_explicit["source_project_id"], "user-proj-1")
        self.assertFalse(res_explicit["project_auto_detected"])
        self.assertTrue((self.projects_dir / "bridge-custom-42.json").is_file())

        # source_project_id pointant vers un projet sidecar dédié -> refusé.
        with self.assertRaises(ValidationError):
            sm_mod.link_workspace_sidecar(
                self.store,
                "gravity-metrics",
                sidecars_dir=self.sidecars_dir,
                config_path=self.config_path,
                projects_dir=self.projects_dir,
                source_project_id="bridge-custom-42",
            )

    def test_sidecar_env_omits_home_only_for_default_gravity_bridge_dir(self) -> None:
        fake_home = self.root / "fake-home"
        expected_default = (fake_home / ".gemini" / "gravity" / "bridge").resolve()
        with mock.patch.object(Path, "home", return_value=fake_home), mock.patch.dict(
            os.environ, {"GRAVITY_BRIDGE_HOME": "", "GRAVITY_HOME": ""}, clear=False
        ):
            default_store = BridgeStore()
            self.assertEqual(default_store.home_dir, expected_default)
            res_default = sm_mod.link_workspace_sidecar(
                default_store,
                str(self.ws_target),
                sidecars_dir=self.sidecars_dir,
                config_path=self.config_path,
                projects_dir=self.projects_dir,
            )
        sc_default = json.loads(
            Path(res_default["sidecar_json_path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn("GRAVITY_BRIDGE_HOME", sc_default["env"])

        custom_sc = sm_mod.link_workspace_sidecar(
            self.store,
            str(self.ws_target),
            sidecars_dir=self.sidecars_dir,
            config_path=self.config_path,
            projects_dir=self.projects_dir,
        )
        sc_custom = json.loads(
            Path(custom_sc["sidecar_json_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(sc_custom["env"]["GRAVITY_BRIDGE_HOME"], str(self.store.home_dir))


if __name__ == "__main__":
    unittest.main()
