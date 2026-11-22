#!/usr/bin/env python3
"""Installeur/désinstalleur et gestionnaire de liaison sidecar Gravity Bridge (100 % stdlib)."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = REPO_ROOT / "gravity-bridge-plugin"


def default_target_dir() -> Path:
    """Retourne le dossier utilisateur standard d'installation des plugins Antigravity."""
    return Path.home() / ".gemini" / "config" / "plugins" / "gravity-bridge"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _load_validate_module(plugin_dir: Path):
    validate_path = plugin_dir / "scripts" / "validate.py"
    if not validate_path.is_file():
        raise FileNotFoundError(f"Script validate.py introuvable : {validate_path}")
    spec = importlib.util.spec_from_file_location("gravity_bridge_validate", validate_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Impossible de charger {validate_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ignore_copy(_dir: str, names: List[str]) -> List[str]:
    return [
        n
        for n in names
        if n == "__pycache__" or n.endswith((".pyc", ".pyo"))
    ]


def _import_core(plugin_root: Path):
    root_str = str(plugin_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from core.security import is_explicit_workspace_path
    from core.sidecar_manager import (
        default_antigravity_config_path,
        default_antigravity_projects_dir,
        default_sidecars_dir,
        link_workspace_sidecar,
    )
    from core.store import BridgeStore

    return (
        BridgeStore,
        is_explicit_workspace_path,
        link_workspace_sidecar,
        default_sidecars_dir,
        default_antigravity_config_path,
        default_antigravity_projects_dir,
    )


def _sync_staged_into_target(staged: Path, dst: Path) -> None:
    """Remplace dst par staged en gérant le verrouillage Windows sur le dossier racine."""
    if dst.exists():
        try:
            shutil.rmtree(dst)
        except PermissionError:
            for child in list(dst.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    shutil.copytree(staged, dst, ignore=_ignore_copy, dirs_exist_ok=True)


def install_plugin(
    source: Path,
    target: Path,
    *,
    dry_run: bool = False,
    run_validate: bool = True,
) -> int:
    """Installe gravity-bridge-plugin vers target, adapte python3 sur la copie et valide."""
    src = source.expanduser().resolve()
    dst = target.expanduser().resolve()

    if not src.is_dir() or not (src / "plugin.json").is_file():
        print(f"[ERROR] Source plugin invalide : {src}", file=sys.stderr)
        return 1

    if src == dst:
        print("[ERROR] La source et la cible d'installation sont identiques.", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[DRY-RUN] Copie : {src} -> {dst}")
        print("[DRY-RUN] Adaptation éventuelle 'python3' sur la copie installée uniquement.")
        if run_validate:
            print(f"[DRY-RUN] Validation : validate.py --strict --plugin-root {dst}")
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{dst.name}-stage-", dir=str(dst.parent)) as tmp:
        staged = Path(tmp) / dst.name
        shutil.copytree(src, staged, ignore=_ignore_copy)

        validate_mod = _load_validate_module(staged)
        validate_mod.fix_python_cmd(staged)

        if run_validate:
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(staged / "scripts" / "validate.py"),
                    "--strict",
                    "--plugin-root",
                    str(staged),
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                env=env,
                timeout=30,
                check=False,
            )
            if proc.stdout.strip():
                print(proc.stdout.rstrip())
            if proc.returncode != 0:
                if proc.stderr.strip():
                    print(proc.stderr.rstrip(), file=sys.stderr)
                return proc.returncode

        _sync_staged_into_target(staged, dst)

    print(f"[OK] Plugin Gravity Bridge installé dans {dst}")
    print("[INFO] Stockage boîte aux lettres inchangé (~/.gemini/gravity-bridge/bridge.db).")
    return 0


def uninstall_plugin(target: Path, *, dry_run: bool = False) -> int:
    """Supprime uniquement le dossier du plugin installé (sans toucher à bridge.db)."""
    dst = target.expanduser().resolve()
    if not dst.exists():
        print(f"[INFO] Plugin absent de {dst} : rien à désinstaller.")
        return 0

    if dry_run:
        print(f"[DRY-RUN] Suppression du dossier plugin : {dst}")
        return 0

    shutil.rmtree(dst)
    print(f"[OK] Plugin désinstallé de {dst} (données ~/.gemini/gravity-bridge/ préservées).")
    return 0


def link_workspace_cli(
    workspace_ref: str,
    *,
    target: Path,
    source: Path,
    slug: Optional[str] = None,
    project_id: Optional[str] = None,
    source_project_id: Optional[str] = None,
    configure_config: Optional[bool] = None,
    sidecars_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    projects_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """Génère le micro-sidecar et le projet sidecar dédié d'un workspace cible."""
    dst = target.expanduser().resolve()
    relay_script = dst / "scripts" / "sidecar_relay.py"
    import_root = dst if (dst / "core" / "sidecar_manager.py").is_file() else source.expanduser().resolve()

    (
        BridgeStore,
        is_explicit_workspace_path,
        link_workspace_sidecar,
        default_sidecars_dir,
        default_antigravity_config_path,
        default_antigravity_projects_dir,
    ) = _import_core(import_root)

    resolved_sidecars = (sidecars_dir or default_sidecars_dir()).expanduser().resolve()
    resolved_config = (config_path or default_antigravity_config_path()).expanduser().resolve()
    resolved_projects = (
        projects_dir.expanduser().resolve()
        if projects_dir is not None
        else (
            resolved_config.parent / "projects"
            if config_path is not None
            else default_antigravity_projects_dir()
        )
    )

    if dry_run:
        print(f"[DRY-RUN] Liaison micro-sidecar pour : {workspace_ref}")
        print(f"[DRY-RUN] Script relais cible : {relay_script}")
        print(f"[DRY-RUN] Dossier sidecars : {resolved_sidecars}")
        print(f"[DRY-RUN] Dossier projets : {resolved_projects}")
        if configure_config is not False:
            print(
                f"[DRY-RUN] Projet dédié '<nom> (Bridge)' et mise à jour config : {resolved_config} "
                f"(projectId={project_id or 'auto-bridge'}, sourceProjectId={source_project_id or 'auto'})"
            )
        return 0

    if not relay_script.is_file():
        print(
            f"[ERROR] Relais introuvable : {relay_script}. Installez d'abord le plugin (python install.py).",
            file=sys.stderr,
        )
        return 1

    store = BridgeStore(home=home)
    target_ref = workspace_ref
    if is_explicit_workspace_path(workspace_ref) or Path(workspace_ref).exists():
        reg = store.register_workspace(workspace_ref, slug=slug)
        target_ref = reg["slug"]

    res = link_workspace_sidecar(
        store,
        target_ref,
        relay_script=relay_script,
        sidecars_dir=resolved_sidecars,
        config_path=resolved_config,
        projects_dir=resolved_projects,
        project_id=project_id,
        source_project_id=source_project_id,
        configure_config=configure_config,
        uninstall=False,
    )
    proj_info = f", project_id={res['project_id']}" if res.get("project_id") else ""
    print(f"[OK] Micro-sidecar généré : {res['sidecar_json_path']} (slug={res['slug']}{proj_info})")
    for step in res.get("next_steps", []):
        print(f"[INFO] {step}")
    return 0


def unlink_workspace_cli(
    workspace_ref: str,
    *,
    target: Path,
    source: Path,
    configure_config: Optional[bool] = None,
    sidecars_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    projects_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """Supprime le micro-sidecar et le projet dédié d'un workspace sans altérer bridge.db."""
    dst = target.expanduser().resolve()
    import_root = dst if (dst / "core" / "sidecar_manager.py").is_file() else source.expanduser().resolve()

    (
        BridgeStore,
        _is_explicit,
        link_workspace_sidecar,
        default_sidecars_dir,
        default_antigravity_config_path,
        default_antigravity_projects_dir,
    ) = _import_core(import_root)

    resolved_sidecars = (sidecars_dir or default_sidecars_dir()).expanduser().resolve()
    resolved_config = (config_path or default_antigravity_config_path()).expanduser().resolve()
    resolved_projects = (
        projects_dir.expanduser().resolve()
        if projects_dir is not None
        else (
            resolved_config.parent / "projects"
            if config_path is not None
            else default_antigravity_projects_dir()
        )
    )

    if dry_run:
        print(f"[DRY-RUN] Suppression micro-sidecar pour : {workspace_ref}")
        print(f"[DRY-RUN] Dossier sidecars : {resolved_sidecars}")
        print(f"[DRY-RUN] Dossier projets : {resolved_projects}")
        if configure_config is not False:
            print(f"[DRY-RUN] Nettoyage entrée sidecar dans : {resolved_config}")
        return 0

    store = BridgeStore(home=home)
    res = link_workspace_sidecar(
        store,
        workspace_ref,
        sidecars_dir=resolved_sidecars,
        config_path=resolved_config,
        projects_dir=resolved_projects,
        configure_config=configure_config,
        uninstall=True,
    )
    repaired = res.get("repaired_user_projects", [])
    removed_proj = res.get("removed_bridge_projects", [])
    print(
        f"[OK] Micro-sidecar dissocié : {res['sidecar_id']} "
        f"(removed={res['removed_sidecar_dir']}, "
        f"bridge_projects_removed={len(removed_proj)}, "
        f"user_projects_repaired={len(repaired)})"
    )
    for proj in repaired:
        print(f"[INFO] Projet utilisateur réparé ('sidecar' retiré) : {proj['name']} ({proj['id']})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Installeur / désinstalleur et gestionnaire de liaison sidecar Gravity Bridge"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Dossier source gravity-bridge-plugin (défaut : ./gravity-bridge-plugin).",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=default_target_dir(),
        help="Dossier cible d'installation Antigravity.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--uninstall",
        action="store_true",
        help="Désinstalle le plugin du dossier cible sans supprimer bridge.db.",
    )
    mode_group.add_argument(
        "--link",
        metavar="PATH_OR_SLUG",
        default=None,
        help="Génère le micro-sidecar d'un workspace pointant vers le plugin installé.",
    )
    mode_group.add_argument(
        "--unlink",
        metavar="WORKSPACE",
        default=None,
        help="Supprime le micro-sidecar d'un workspace.",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="Slug personnalisé lors d'un --link sur un nouveau chemin.",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Identifiant explicite du projet sidecar dédié '<nom> (Bridge)' (refusé si projet utilisateur).",
    )
    parser.add_argument(
        "--source-project-id",
        default=None,
        help="Identifiant du projet utilisateur Antigravity App dont recopier dossiers, réglages et permissions.",
    )
    parser.add_argument(
        "--configure-config",
        action="store_true",
        default=None,
        help="Crée/met à jour le projet dédié '<nom> (Bridge)' et ~/.gemini/config/config.json (défaut lors de --link/--unlink).",
    )
    parser.add_argument(
        "--no-configure-config",
        dest="configure_config",
        action="store_false",
        help="Écrit uniquement sidecar.json sans modifier ~/.gemini/config/config.json ni projects/.",
    )
    parser.add_argument(
        "--sidecars-dir",
        type=Path,
        default=None,
        help="Dossier sidecars personnalisé (défaut ~/.gemini/config/sidecars).",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Chemin config.json personnalisé (défaut ~/.gemini/config/config.json).",
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help="Dossier projets Antigravity personnalisé (défaut ~/.gemini/config/projects).",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Répertoire GRAVITY_BRIDGE_HOME personnalisé.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les actions sans modifier le disque.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Ignore la validation stricte post-installation.",
    )
    args = parser.parse_args(argv)

    if args.uninstall:
        return uninstall_plugin(args.target, dry_run=args.dry_run)
    if args.link is not None:
        try:
            return link_workspace_cli(
                args.link,
                target=args.target,
                source=args.source,
                slug=args.slug,
                project_id=args.project_id,
                source_project_id=args.source_project_id,
                configure_config=args.configure_config,
                sidecars_dir=args.sidecars_dir,
                config_path=args.config_path,
                projects_dir=args.projects_dir,
                home=args.home,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
    if args.unlink is not None:
        try:
            return unlink_workspace_cli(
                args.unlink,
                target=args.target,
                source=args.source,
                configure_config=args.configure_config,
                sidecars_dir=args.sidecars_dir,
                config_path=args.config_path,
                projects_dir=args.projects_dir,
                home=args.home,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

    return install_plugin(
        args.source,
        args.target,
        dry_run=args.dry_run,
        run_validate=not args.no_validate,
    )


if __name__ == "__main__":
    raise SystemExit(main())
