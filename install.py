#!/usr/bin/env python3
"""Installeur/désinstalleur et gestionnaire de liaison sidecar Gravity Bridge (100 % stdlib)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
PLUGIN_SOURCE = REPO_ROOT / "plugin"


def default_target_dir() -> Path:
    """Retourne le dossier utilisateur standard d'installation des plugins Antigravity."""
    return Path.home() / ".gemini" / "config" / "plugins" / "gravity-bridge"


def resolve_target_dir(
    *,
    scope: str = "global",
    workspace: Optional[Path] = None,
    target: Optional[Path] = None,
) -> Path:
    """Résout le dossier cible selon --target, --scope et --workspace."""
    if target is not None:
        return target
    if scope == "workspace":
        if workspace is None:
            raise ValueError("--scope workspace exige --workspace PATH.")
        return workspace.expanduser() / ".agents" / "plugins" / "gravity-bridge"
    return default_target_dir()


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
    prev_dwb = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("gravity_bridge_validate", validate_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Impossible de charger {validate_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = prev_dwb


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


def _read_plugin_manifest(root: Path) -> Optional[Dict[str, object]]:
    manifest = root / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _is_existing_bridge_install(dst: Path) -> bool:
    data = _read_plugin_manifest(dst)
    return isinstance(data, dict) and data.get("name") == "gravity-bridge"


def _is_target_occupied(dst: Path) -> bool:
    """Retourne True si dst existe avec du contenu étranger (ni absent, ni install Bridge, ni dossier vide)."""
    if dst.is_symlink() or dst.is_file():
        return True
    if not dst.exists():
        return False
    if _is_existing_bridge_install(dst):
        return False
    try:
        return any(dst.iterdir())
    except OSError:
        return True


def _iter_plugin_files(root: Path) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix.lower() in (".pyc", ".pyo"):
            continue
        files[path.relative_to(root).as_posix()] = path
    return files


def _normalized_mcp_config(raw_text: str) -> object:
    """Normalise mcp_config.json pour ignorer l'adaptation portable python3 -> python / py -3."""
    data = json.loads(raw_text)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, dict):
        for scfg in servers.values():
            if not isinstance(scfg, dict):
                continue
            cmd = scfg.get("command")
            args = list(scfg.get("args") or [])
            if cmd == "py" and args[:1] == ["-3"]:
                scfg["command"] = "python3"
                scfg["args"] = args[1:]
            elif cmd in ("python", "python3"):
                scfg["command"] = "python3"
    return data


def _detect_install_drift(src: Path, dst: Path) -> List[str]:
    """Compare la source et la copie installée en lecture seule et retourne les écarts éventuels."""
    drifts: List[str] = []
    src_manifest = _read_plugin_manifest(src) or {}
    dst_manifest = _read_plugin_manifest(dst) or {}
    src_ver = str(src_manifest.get("version") or "?")
    dst_ver = str(dst_manifest.get("version") or "?")
    if src_ver != dst_ver:
        drifts.append(f"version installée {dst_ver} != source {src_ver}")

    src_files = _iter_plugin_files(src)
    dst_files = _iter_plugin_files(dst)
    missing = sorted(set(src_files) - set(dst_files))
    extra = sorted(set(dst_files) - set(src_files))
    if missing:
        drifts.append(f"{len(missing)} fichier(s) manquant(s)")
    if extra:
        drifts.append(f"{len(extra)} fichier(s) supplémentaire(s)")

    modified: List[str] = []
    for rel in sorted(set(src_files) & set(dst_files)):
        if rel == "mcp_config.json":
            try:
                if _normalized_mcp_config(src_files[rel].read_text(encoding="utf-8")) != _normalized_mcp_config(
                    dst_files[rel].read_text(encoding="utf-8")
                ):
                    modified.append(rel)
            except Exception:
                modified.append(rel)
        else:
            if src_files[rel].read_bytes() != dst_files[rel].read_bytes():
                modified.append(rel)
    if modified:
        drifts.append(f"{len(modified)} fichier(s) modifié(s)")
    return drifts


def _sync_staged_into_target(staged: Path, dst: Path) -> None:
    """Remplace dst par staged en gérant le verrouillage Windows sur le dossier racine."""
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.exists():
        try:
            shutil.rmtree(dst)
        except PermissionError:
            for child in list(dst.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    shutil.copytree(staged, dst, ignore=_ignore_copy, dirs_exist_ok=True)


def _run_validate_subprocess(plugin_root: Path) -> int:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            str(plugin_root / "scripts" / "validate.py"),
            "--strict",
            "--plugin-root",
            str(plugin_root),
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
    if proc.returncode != 0 and proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def check_plugin(
    source: Path,
    target: Path,
    *,
    scope: str = "global",
    method: str = "copy",
    strict: bool = False,
) -> int:
    """Inspecte l'environnement, la source et la cible en 100 % lecture seule (zéro écriture disque)."""
    src = source.expanduser().resolve()
    dst = target.expanduser().resolve()
    errors: List[str] = []
    warnings: List[str] = []

    py_ok = sys.version_info >= (3, 10)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"[CHECK] Python : {py_ver} ({sys.executable})")
    if not py_ok:
        errors.append(f"Python 3.10+ requis (détecté : {py_ver}).")

    agy_bin = shutil.which("agy")
    print(f"[CHECK] agy CLI : {agy_bin or 'absent'}")
    if method == "agy" and not agy_bin:
        errors.append("CLI 'agy' introuvable dans le PATH pour --method agy.")

    print(f"[CHECK] Scope : {scope}")
    print(f"[CHECK] Méthode : {method}")
    print(f"[CHECK] Source : {src}")
    print(f"[CHECK] Cible : {dst}")

    src_manifest = _read_plugin_manifest(src)
    if not src.is_dir() or src_manifest is None:
        errors.append(f"Source plugin invalide ou plugin.json illisible : {src}")
    else:
        src_ver = src_manifest.get("version", "?")
        print(f"[CHECK] Manifeste source : {src_manifest.get('name')} v{src_ver}")
        try:
            validate_mod = _load_validate_module(src)
            report = validate_mod.Report()
            validate_mod._validate_manifest(src, report)
            validate_mod._validate_module_contract(src, report)
            validate_mod._validate_skills(src, report)
            validate_mod._validate_rules_and_no_hooks(src, report)
            validate_mod._validate_mcp_config(src, report)
            for py_file in sorted(src.rglob("*.py")):
                if "__pycache__" in py_file.parts:
                    continue
                validate_mod._check_stdlib_only(py_file, report)
            errors.extend(report.errors)
            warnings.extend(report.warnings)
            for info in report.infos:
                print(f"[CHECK] {info}")
        except Exception as exc:
            errors.append(f"Impossible d'auditer la source en lecture seule : {exc}")

    if not dst.exists() and not dst.is_symlink():
        print("[CHECK] État cible : non installé")
    elif _is_target_occupied(dst):
        msg = f"Cible occupée par un contenu étranger : {dst}"
        print(f"[CHECK] État cible : occupée ({dst})")
        warnings.append(msg)
    elif _is_existing_bridge_install(dst):
        dst_manifest = _read_plugin_manifest(dst) or {}
        dst_ver = dst_manifest.get("version", "?")
        drifts = _detect_install_drift(src, dst) if src.is_dir() else []
        if drifts:
            drift_txt = ", ".join(drifts)
            print(f"[CHECK] État cible : installé v{dst_ver} (dérive détectée : {drift_txt})")
            warnings.append(f"Dérive détectée sur la cible : {drift_txt}")
        else:
            print(f"[CHECK] État cible : installé et à jour (v{dst_ver})")
    else:
        print("[CHECK] État cible : dossier vide")

    for err in errors:
        print(f"[ERROR] {err}", file=sys.stderr)
    for warn in warnings:
        print(f"[WARN]  {warn}")

    if errors or (strict and warnings):
        return 1
    return 0


def install_plugin(
    source: Path,
    target: Path,
    *,
    dry_run: bool = False,
    run_validate: bool = True,
    force: bool = False,
    method: str = "copy",
    no_fix_python: bool = False,
) -> int:
    """Installe plugin/ vers target (copy ou agy), adapte python3 sur la copie et valide."""
    src = source.expanduser().resolve()
    dst = target.expanduser().resolve()

    if not src.is_dir() or not (src / "plugin.json").is_file():
        print(f"[ERROR] Source plugin invalide : {src}", file=sys.stderr)
        return 1

    if method == "agy":
        if _is_target_occupied(dst) and not force:
            print(
                f"[ERROR] Cible occupée : {dst} (utilisez --force pour écraser).",
                file=sys.stderr,
            )
            return 1
        if dry_run:
            if run_validate:
                print(f"[DRY-RUN] Validation : validate.py --strict --plugin-root {src}")
            print(f"[DRY-RUN] Délégation : agy plugin install {src}")
            return 0
        if run_validate:
            rc_val = _run_validate_subprocess(src)
            if rc_val != 0:
                return rc_val
        agy_bin = shutil.which("agy")
        if not agy_bin:
            print("[ERROR] CLI 'agy' introuvable dans le PATH.", file=sys.stderr)
            return 1
        proc = subprocess.run(
            [agy_bin, "plugin", "install", str(src)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
        )
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.returncode != 0:
            if proc.stderr.strip():
                print(proc.stderr.rstrip(), file=sys.stderr)
            return proc.returncode
        print(f"[OK] Plugin Gravity Bridge installé via agy depuis {src}")
        return 0

    if src == dst:
        print("[ERROR] La source et la cible d'installation sont identiques.", file=sys.stderr)
        return 1

    if _is_target_occupied(dst) and not force:
        print(
            f"[ERROR] Cible occupée : {dst} (utilisez --force pour écraser).",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print(f"[DRY-RUN] Copie : {src} -> {dst}")
        if not no_fix_python:
            print("[DRY-RUN] Adaptation éventuelle 'python3' sur la copie installée uniquement.")
        if run_validate:
            print(f"[DRY-RUN] Validation : validate.py --strict --plugin-root {dst}")
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{dst.name}-stage-", dir=str(dst.parent)) as tmp:
        staged = Path(tmp) / dst.name
        shutil.copytree(src, staged, ignore=_ignore_copy)

        if not no_fix_python:
            validate_mod = _load_validate_module(staged)
            validate_mod.fix_python_cmd(staged)

        if run_validate:
            rc_val = _run_validate_subprocess(staged)
            if rc_val != 0:
                return rc_val

        _sync_staged_into_target(staged, dst)

    print(f"[OK] Plugin Gravity Bridge installé dans {dst}")
    print("[INFO] Stockage boîte aux lettres inchangé (~/.gemini/gravity/bridge/bridge.db).")
    return 0


def uninstall_plugin(
    target: Path,
    *,
    dry_run: bool = False,
    method: str = "copy",
) -> int:
    """Supprime uniquement le dossier du plugin installé (sans toucher à bridge.db)."""
    if method == "agy":
        if dry_run:
            print("[DRY-RUN] Délégation : agy plugin uninstall gravity-bridge")
            return 0
        agy_bin = shutil.which("agy")
        if not agy_bin:
            print("[ERROR] CLI 'agy' introuvable dans le PATH.", file=sys.stderr)
            return 1
        proc = subprocess.run(
            [agy_bin, "plugin", "uninstall", "gravity-bridge"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
        )
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.returncode != 0:
            if proc.stderr.strip():
                print(proc.stderr.rstrip(), file=sys.stderr)
            return proc.returncode
        print("[OK] Plugin désinstallé via agy (données ~/.gemini/gravity/bridge/ préservées).")
        return 0

    dst = target.expanduser().resolve()
    if not dst.exists() and not dst.is_symlink():
        print(f"[INFO] Plugin absent de {dst} : rien à désinstaller.")
        return 0

    if dry_run:
        print(f"[DRY-RUN] Suppression du dossier plugin : {dst}")
        return 0

    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    else:
        shutil.rmtree(dst)
    print(f"[OK] Plugin désinstallé de {dst} (données ~/.gemini/gravity/bridge/ préservées).")
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


def build_parser() -> argparse.ArgumentParser:
    """Construit le parseur CLI de l'installeur Gravity Bridge."""
    parser = argparse.ArgumentParser(
        description="Installeur / désinstalleur et gestionnaire de liaison sidecar Gravity Bridge"
    )
    parser.add_argument(
        "--plugin-root",
        "--source",
        dest="plugin_root",
        type=Path,
        default=PLUGIN_SOURCE,
        help="Dossier source du plugin (défaut : ./plugin ; alias --source).",
    )
    parser.add_argument(
        "--scope",
        choices=("global", "workspace"),
        default="global",
        help="Portée d'installation : global (~/.gemini/config/plugins) ou workspace (<workspace>/.agents/plugins).",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Dossier du workspace cible requis avec --scope workspace.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Dossier cible explicite d'installation (prioritaire sur --scope).",
    )
    parser.add_argument(
        "--method",
        choices=("copy", "agy"),
        default="copy",
        help="Méthode d'installation : copy (défaut stdlib) ou agy (délégation à 'agy plugin install', --scope global uniquement).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--check",
        action="store_true",
        help="Inspection 100 %% lecture seule (Python, agy, source, cible et dérive éventuelle).",
    )
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
        help="Répertoire de stockage personnalisé (défaut : $GRAVITY_BRIDGE_HOME > $GRAVITY_HOME/bridge > ~/.gemini/gravity/bridge).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les actions sans modifier le disque.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Autorise l'écrasement d'une cible occupée ou installation existante.",
    )
    parser.add_argument(
        "--skip-validate",
        "--no-validate",
        dest="skip_validate",
        action="store_true",
        help="Ignore la validation pré-installation (hors --strict ; alias --no-validate).",
    )
    parser.add_argument(
        "--no-fix-python",
        action="store_true",
        help="Désactive l'adaptation 'python3' sur la copie installée (conserve mcp_config.json tel quel).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exige une validation stricte (force la validation et échoue sur avertissement en --check).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.scope == "workspace" and args.workspace is None:
        parser.error("--scope workspace exige --workspace PATH.")
    if args.workspace is not None:
        ws_expanded = args.workspace.expanduser()
        if ws_expanded.exists() and not ws_expanded.is_dir():
            parser.error(f"--workspace doit désigner un dossier : {args.workspace}")
    if args.method == "agy":
        if args.scope != "global":
            parser.error("--method agy est limité à --scope global (incompatible avec --scope workspace).")
        if args.target is not None:
            parser.error("--method agy est incompatible avec --target.")

    resolved_target = resolve_target_dir(
        scope=args.scope,
        workspace=args.workspace,
        target=args.target,
    )

    if args.check:
        return check_plugin(
            args.plugin_root,
            resolved_target,
            scope=args.scope,
            method=args.method,
            strict=args.strict,
        )
    if args.uninstall:
        return uninstall_plugin(
            resolved_target,
            dry_run=args.dry_run,
            method=args.method,
        )
    if args.link is not None:
        try:
            return link_workspace_cli(
                args.link,
                target=resolved_target,
                source=args.plugin_root,
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
                target=resolved_target,
                source=args.plugin_root,
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
        args.plugin_root,
        resolved_target,
        dry_run=args.dry_run,
        run_validate=bool(args.strict or not args.skip_validate),
        force=args.force,
        method=args.method,
        no_fix_python=args.no_fix_python,
    )


if __name__ == "__main__":
    raise SystemExit(main())
