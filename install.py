#!/usr/bin/env python3
"""Installeur/désinstalleur cross-platform du plugin Gravity Bridge (100 % stdlib Python 3)."""

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

        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(staged, dst, ignore=_ignore_copy)

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


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Installeur / désinstalleur du plugin Gravity Bridge"
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
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Désinstalle le plugin du dossier cible sans supprimer bridge.db.",
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
    return install_plugin(
        args.source,
        args.target,
        dry_run=args.dry_run,
        run_validate=not args.no_validate,
    )


if __name__ == "__main__":
    raise SystemExit(main())
