"""Gestionnaire de micro-sidecars Antigravity App pour Gravity Bridge (100 % stdlib)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse
import uuid

from core.security import ValidationError, validate_sidecar_id, validate_slug
from core.store import BridgeStore, default_bridge_home

RELAY_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sidecar_relay.py"
BRIDGE_PROJECT_SUFFIX = " (Bridge)"


def default_sidecars_dir() -> Path:
    """Retourne le dossier utilisateur des sidecars Antigravity (~/.gemini/config/sidecars)."""
    override = os.environ.get("GRAVITY_BRIDGE_SIDECARS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".gemini" / "config" / "sidecars").resolve()


def default_antigravity_config_path() -> Path:
    """Retourne le fichier de configuration globale Antigravity (~/.gemini/config/config.json)."""
    override = os.environ.get("GRAVITY_BRIDGE_ANTIGRAVITY_CONFIG", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".gemini" / "config" / "config.json").resolve()


def default_antigravity_projects_dir(config_path: Optional[Path] = None) -> Path:
    """Retourne le dossier des projets Antigravity (~/.gemini/config/projects)."""
    override = os.environ.get("GRAVITY_BRIDGE_ANTIGRAVITY_PROJECTS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if config_path is not None:
        return config_path.expanduser().resolve().parent / "projects"
    return (Path.home() / ".gemini" / "config" / "projects").resolve()


_WINDOWS_URI_DRIVE_RE = re.compile(r"^/[A-Za-z]:(?:[\\/]|$)")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")


def _strip_windows_extended_prefix(path_str: str) -> str:
    if path_str.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_str[8:]
    if path_str.startswith("\\\\?\\"):
        return path_str[4:]
    return path_str


def _folder_uri_to_path(uri: str) -> Optional[Path]:
    """Convertit un folderUri Antigravity (file:///...) en objet Path local."""
    if not isinstance(uri, str):
        return None
    raw = uri.strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if parsed.scheme.lower() == "file":
        netloc = unquote(parsed.netloc)
        path_part = unquote(parsed.path)
        if netloc and netloc.lower() != "localhost":
            if os.name == "nt":
                return Path(f"\\\\{netloc}" + path_part.replace("/", "\\"))
            return Path(f"//{netloc}{path_part}")
        if _WINDOWS_URI_DRIVE_RE.match(path_part):
            path_part = path_part[1:]
        if not path_part:
            return None
        return Path(path_part)

    if not parsed.scheme or _WINDOWS_DRIVE_RE.match(raw):
        return Path(raw).expanduser()

    return None


def _path_to_folder_uri(path: Path) -> str:
    """Convertit un chemin local en folderUri Antigravity (file:///d%3A/... sous Windows)."""
    try:
        resolved_str = str(path.expanduser().resolve())
    except OSError:
        resolved_str = os.path.abspath(str(path.expanduser()))
    resolved_str = _strip_windows_extended_prefix(resolved_str)

    if resolved_str.startswith("\\\\") or resolved_str.startswith("//"):
        unc_body = resolved_str.lstrip("\\/").replace("\\", "/")
        host, _, rest = unc_body.partition("/")
        quoted_rest = quote(rest.lstrip("/"), safe="/")
        return f"file://{quote(host, safe='')}/{quoted_rest}" if quoted_rest else f"file://{quote(host, safe='')}/"

    if _WINDOWS_DRIVE_RE.match(resolved_str):
        drive = resolved_str[0].lower()
        rest_posix = resolved_str[2:].replace("\\", "/")
        if not rest_posix.startswith("/"):
            rest_posix = "/" + rest_posix
        return f"file:///{drive}%3A{quote(rest_posix, safe='/')}"

    posix_path = resolved_str.replace("\\", "/")
    if not posix_path.startswith("/"):
        posix_path = "/" + posix_path
    return f"file://{quote(posix_path, safe='/')}"


def _norm_path_key(path: Path) -> str:
    """Normalise un chemin local en clé de comparaison insensible à la casse sous Windows."""
    try:
        resolved = str(path.expanduser().resolve())
    except OSError:
        resolved = os.path.abspath(str(path.expanduser()))
    if os.name == "nt":
        resolved = _strip_windows_extended_prefix(resolved)
    return os.path.normcase(os.path.normpath(resolved))


def _extract_project_folder_uris(project_data: Dict[str, Any]) -> List[str]:
    """Extrait tous les folderUri déclarés dans projectResources.resources sans doublons."""
    proj_res = project_data.get("projectResources")
    if not isinstance(proj_res, dict):
        return []
    resources = proj_res.get("resources")
    if not isinstance(resources, list):
        return []

    uris: List[str] = []
    seen = set()

    def _add(candidate: Any) -> None:
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                uris.append(stripped)

    for item in resources:
        if not isinstance(item, dict):
            continue
        _add(item.get("folderUri"))
        for key in ("gitFolder", "localFolder"):
            sub = item.get(key)
            if isinstance(sub, dict):
                _add(sub.get("folderUri"))
        for subval in item.values():
            if isinstance(subval, dict):
                _add(subval.get("folderUri"))
    return uris


def _build_default_project_resources(canonical_path: str) -> Dict[str, Any]:
    """Construit un bloc projectResources minimal depuis le dossier canonique du workspace."""
    ws_path = Path(canonical_path)
    folder_uri = _path_to_folder_uri(ws_path)
    folder_key = "gitFolder" if (ws_path / ".git").exists() else "localFolder"
    return {"resources": [{folder_key: {"folderUri": folder_uri}}]}


def _find_project_files(projects_dir: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    """Lit les descripteurs JSON valides présents dans projects_dir."""
    if not projects_dir.is_dir():
        return []
    items: List[Tuple[Path, Dict[str, Any]]] = []
    for json_file in sorted(projects_dir.glob("*.json")):
        try:
            loaded = json.loads(json_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            items.append((json_file, loaded))
    return items


def _project_id_of(json_file: Path, project_data: Dict[str, Any]) -> str:
    return str(project_data.get("id") or json_file.stem).strip()


def _sidecar_id_of(project_data: Dict[str, Any]) -> Optional[str]:
    sidecar_meta = project_data.get("sidecar")
    if not isinstance(sidecar_meta, dict):
        return None
    sc_id = str(sidecar_meta.get("sidecarId") or "").strip()
    return sc_id or None


def _is_dedicated_bridge_project(
    project_data: Dict[str, Any],
    sidecar_id: str,
    slug: str,
) -> bool:
    """Indique si project_data est un projet sidecar dédié de Gravity Bridge (et non un projet utilisateur)."""
    if _sidecar_id_of(project_data) != sidecar_id:
        return False

    name = str(project_data.get("name") or "").strip()
    if name.endswith(BRIDGE_PROJECT_SUFFIX):
        return True
    if name == sidecar_id:
        return True
    if name.startswith(("Gravity Bridge · ", "Bridge · ", "[Bridge] ")):
        return True
    if not _extract_project_folder_uris(project_data):
        return True
    return False


def bridge_project_name_for_slug(slug: str, base_name: Optional[str] = None) -> str:
    """Formate le nom d'affichage officiel du projet sidecar dédié (<nom> (Bridge))."""
    clean_slug = validate_slug(slug)
    label = base_name.strip() if isinstance(base_name, str) and base_name.strip() else clean_slug
    if label.endswith(BRIDGE_PROJECT_SUFFIX):
        return label
    return f"{label}{BRIDGE_PROJECT_SUFFIX}"


def discover_antigravity_project(
    canonical_path: str,
    *,
    slug: Optional[str] = None,
    projects_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    include_polluted_sidecar_id: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Recherche le projet utilisateur Antigravity App correspondant à canonical_path."""
    pdir = (projects_dir or default_antigravity_projects_dir(config_path)).expanduser().resolve()
    project_files = _find_project_files(pdir)
    if not project_files:
        return None

    target_key = _norm_path_key(Path(canonical_path))
    slug_raw = (slug or Path(canonical_path).name or "").strip()
    target_slug = slug_raw.lower()
    candidates: List[Tuple[Tuple[int, int, str, str], Dict[str, str]]] = []

    for json_file, loaded in project_files:
        if loaded.get("sidecar"):
            if not include_polluted_sidecar_id:
                continue
            if _sidecar_id_of(loaded) != include_polluted_sidecar_id:
                continue
            if _is_dedicated_bridge_project(loaded, include_polluted_sidecar_id, slug_raw):
                continue

        pid = _project_id_of(json_file, loaded)
        if not pid:
            continue
        pname = str(loaded.get("name") or pid).strip()

        folder_keys: List[str] = []
        seen_keys = set()
        for uri in _extract_project_folder_uris(loaded):
            fpath = _folder_uri_to_path(uri)
            if fpath is None:
                continue
            fkey = _norm_path_key(fpath)
            if fkey not in seen_keys:
                seen_keys.add(fkey)
                folder_keys.append(fkey)

        pname_norm = pname.lower()
        rank: Optional[Tuple[int, int, str, str]] = None
        if target_key in seen_keys:
            if len(folder_keys) == 1:
                rank = (0, 1, pname_norm, pid)
            elif target_slug and pname_norm == target_slug:
                rank = (1, len(folder_keys), pname_norm, pid)
            else:
                rank = (2, len(folder_keys), pname_norm, pid)
        elif not folder_keys and target_slug and pname_norm == target_slug:
            rank = (3, 0, pname_norm, pid)

        if rank is not None:
            candidates.append(
                (rank, {"id": pid, "name": pname, "source": str(json_file)})
            )

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def _load_source_project_data(
    projects_dir: Path,
    *,
    canonical_path: str,
    slug: str,
    sidecar_id: str,
    source_project_id: Optional[str] = None,
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Charge le projet utilisateur source à refléter dans le projet dédié Bridge."""
    if source_project_id is not None:
        clean_source = source_project_id.strip()
        for json_file, loaded in _find_project_files(projects_dir):
            pid = _project_id_of(json_file, loaded)
            if pid != clean_source:
                continue
            if loaded.get("sidecar"):
                sc_id = _sidecar_id_of(loaded)
                if sc_id != sidecar_id or _is_dedicated_bridge_project(loaded, sidecar_id, slug):
                    raise ValidationError(
                        f"source_project_id={clean_source!r} pointe vers un projet sidecar "
                        "et non vers un projet utilisateur valide."
                    )
            pname = str(loaded.get("name") or pid).strip()
            return pid, pname, loaded
        raise ValidationError(
            f"Projet source Antigravity App introuvable : {clean_source!r} dans {projects_dir}."
        )

    detected = discover_antigravity_project(
        canonical_path,
        slug=slug,
        projects_dir=projects_dir,
        include_polluted_sidecar_id=sidecar_id,
    )
    if detected is None:
        return None
    try:
        loaded = json.loads(Path(detected["source"]).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return detected["id"], detected["name"], loaded


def repair_polluted_user_projects(
    sidecar_id: str,
    slug: str,
    *,
    projects_dir: Path,
) -> List[Dict[str, str]]:
    """Retire la clé 'sidecar' sur tout projet utilisateur pollué par sidecar_id (sans le supprimer)."""
    repaired: List[Dict[str, str]] = []
    for json_file, loaded in _find_project_files(projects_dir):
        if _sidecar_id_of(loaded) != sidecar_id:
            continue
        if _is_dedicated_bridge_project(loaded, sidecar_id, slug):
            continue
        updated = dict(loaded)
        updated.pop("sidecar", None)
        _atomic_write_json(json_file, updated)
        repaired.append(
            {
                "id": _project_id_of(json_file, loaded),
                "name": str(loaded.get("name") or json_file.stem).strip(),
                "path": str(json_file),
            }
        )
    return repaired


def ensure_bridge_sidecar_project(
    projects_dir: Path,
    slug: str,
    sidecar_id: str,
    canonical_path: str,
    *,
    explicit_project_id: Optional[str] = None,
    preferred_project_id: Optional[str] = None,
    source_project: Optional[Tuple[str, str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Crée ou met à jour de façon idempotente le projet sidecar dédié (<nom> (Bridge))."""
    all_projects = _find_project_files(projects_dir)
    by_id: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    dedicated: List[Tuple[Path, Dict[str, Any], str]] = []
    for json_file, loaded in all_projects:
        pid = _project_id_of(json_file, loaded)
        if not pid:
            continue
        by_id[pid] = (json_file, loaded)
        if _is_dedicated_bridge_project(loaded, sidecar_id, slug):
            dedicated.append((json_file, loaded, pid))

    cleaned_orphan_projects: List[str] = []
    existing_data: Optional[Dict[str, Any]] = None
    created = False

    if explicit_project_id is not None:
        bridge_id = explicit_project_id.strip()
        if bridge_id in by_id:
            target_path, existing_data = by_id[bridge_id]
            if not _is_dedicated_bridge_project(existing_data, sidecar_id, slug):
                raise ValidationError(
                    f"project_id={bridge_id!r} correspond à un projet utilisateur existant. "
                    "Lier un micro-sidecar directement à un projet utilisateur pousse Antigravity App "
                    "à le marquer 'sidecar' et à le masquer de la barre Projects lorsqu'il est vide. "
                    "Omettez project_id pour créer/réutiliser un projet dédié '<nom> (Bridge)' ou "
                    "utilisez source_project_id pour désigner le projet à refléter."
                )
        else:
            target_path = projects_dir / f"{bridge_id}.json"
            created = True
    elif dedicated:
        dedicated.sort(
            key=lambda item: (
                0 if (preferred_project_id and item[2] == preferred_project_id) else 1,
                0 if str(item[1].get("name") or "").strip().endswith(BRIDGE_PROJECT_SUFFIX) else 1,
                0 if _extract_project_folder_uris(item[1]) else 1,
                item[2],
            )
        )
        target_path, existing_data, bridge_id = dedicated[0]
        for other_file, other_data, _other_pid in dedicated[1:]:
            if not _extract_project_folder_uris(other_data):
                try:
                    other_file.unlink()
                    cleaned_orphan_projects.append(str(other_file))
                except OSError:
                    pass
    else:
        bridge_id = str(uuid.uuid4())
        target_path = projects_dir / f"{bridge_id}.json"
        created = True

    if explicit_project_id is not None:
        for other_file, other_data, other_pid in dedicated:
            if other_pid != bridge_id and not _extract_project_folder_uris(other_data):
                try:
                    other_file.unlink()
                    cleaned_orphan_projects.append(str(other_file))
                except OSError:
                    pass

    source_data = source_project[2] if source_project is not None else None
    source_name = source_project[1] if source_project is not None else None
    target_name = bridge_project_name_for_slug(slug, base_name=source_name)

    if source_data is not None and _extract_project_folder_uris(source_data):
        proj_resources = copy.deepcopy(source_data["projectResources"])
    else:
        proj_resources = _build_default_project_resources(canonical_path)

    if source_data is not None and isinstance(source_data.get("permissionGrants"), dict):
        perm_grants = copy.deepcopy(source_data["permissionGrants"])
    elif existing_data is not None and isinstance(existing_data.get("permissionGrants"), dict):
        perm_grants = copy.deepcopy(existing_data["permissionGrants"])
    else:
        perm_grants = {}
    perm_grants.setdefault("permissionGrants", {})
    perm_grants.setdefault("v2Migrated", True)

    if source_data is not None and isinstance(source_data.get("settings"), dict):
        proj_settings = copy.deepcopy(source_data["settings"])
    elif existing_data is not None and isinstance(existing_data.get("settings"), dict):
        proj_settings = copy.deepcopy(existing_data["settings"])
    else:
        proj_settings = {}

    payload: Dict[str, Any] = {
        "id": bridge_id,
        "name": target_name,
        "projectResources": proj_resources,
        "permissionGrants": perm_grants,
        "settings": proj_settings,
        "isWorkspaceOnly": False,
        "sidecar": {"sidecarId": sidecar_id},
    }
    _atomic_write_json(target_path, payload)

    return {
        "id": bridge_id,
        "name": target_name,
        "path": str(target_path),
        "created": created,
        "cleaned_orphan_projects": cleaned_orphan_projects,
    }


def sidecar_id_for_slug(slug: str) -> str:
    """Calcule l'identifiant du micro-sidecar associé à un slug de workspace."""
    clean_slug = validate_slug(slug)
    return validate_sidecar_id(f"gravity-bridge-{clean_slug}")


def _is_windows_apps_stub(bin_path: str) -> bool:
    return os.name == "nt" and "windowsapps" in bin_path.lower()


def detect_local_python_command() -> Tuple[str, List[str]]:
    """Détecte une commande Python exécutable localement (sans stub WindowsApps)."""
    candidates = ("python", "python3") if os.name == "nt" else ("python3", "python")
    for name in candidates:
        found = shutil.which(name)
        if found and not _is_windows_apps_stub(found):
            return name, []

    py_launcher = shutil.which("py")
    if py_launcher and os.name == "nt":
        return "py", ["-3"]

    return sys.executable, []


def _atomic_write_json(target: Path, payload: Dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}-",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(raw)
        os.replace(str(tmp_path), str(target))
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _read_configured_sidecar_project_id(config_path: Path, sidecar_id: str) -> Optional[str]:
    if not config_path.is_file():
        return None
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    sidecars = loaded.get("sidecars")
    if not isinstance(sidecars, dict):
        return None
    entry = sidecars.get(sidecar_id)
    if not isinstance(entry, dict):
        return None
    pid = str(entry.get("projectId") or "").strip()
    return pid or None


def _update_antigravity_config(
    config_path: Path,
    sidecar_id: str,
    *,
    project_id: Optional[str] = None,
    remove: bool = False,
) -> bool:
    """Met à jour config.json de façon atomique sans altérer les autres clés ni sidecars."""
    data: Dict[str, Any] = {}
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, dict):
            raise ValidationError(
                f"Le fichier de configuration {config_path} ne contient pas un objet JSON valide."
            )
        data = loaded

    sidecars_section = data.get("sidecars")
    if sidecars_section is None:
        sidecars_section = {}
        data["sidecars"] = sidecars_section
    elif not isinstance(sidecars_section, dict):
        raise ValidationError(
            f"La section 'sidecars' de {config_path} n'est pas un objet JSON."
        )

    if remove:
        if sidecar_id not in sidecars_section:
            return False
        sidecars_section.pop(sidecar_id, None)
        _atomic_write_json(config_path, data)
        return True

    if not project_id or not project_id.strip():
        raise ValidationError("project_id est requis lorsque configure_config=True.")

    existing_entry = sidecars_section.get(sidecar_id)
    entry: Dict[str, Any] = dict(existing_entry) if isinstance(existing_entry, dict) else {}
    existing_sv = entry.get("schemaVersion")
    schema_version = (
        existing_sv
        if isinstance(existing_sv, int) and not isinstance(existing_sv, bool) and existing_sv >= 1
        else 1
    )
    entry["enabled"] = True
    entry["projectId"] = project_id.strip()
    entry["schemaVersion"] = schema_version
    sidecars_section[sidecar_id] = entry
    _atomic_write_json(config_path, data)
    return True


def link_workspace_sidecar(
    store: BridgeStore,
    workspace: str,
    *,
    relay_script: Optional[Path] = None,
    sidecars_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    projects_dir: Optional[Path] = None,
    project_id: Optional[str] = None,
    source_project_id: Optional[str] = None,
    configure_config: Optional[bool] = None,
    uninstall: bool = False,
) -> Dict[str, Any]:
    """Génère ou supprime le micro-sidecar et le projet sidecar dédié d'un workspace."""
    clean_project = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
    clean_source = (
        source_project_id.strip()
        if isinstance(source_project_id, str) and source_project_id.strip()
        else None
    )
    base_sidecars_dir = (sidecars_dir or default_sidecars_dir()).expanduser().resolve()
    cfg_path = (config_path or default_antigravity_config_path()).expanduser().resolve()
    pdir = (projects_dir or default_antigravity_projects_dir(cfg_path)).expanduser().resolve()

    if uninstall:
        ws = store.get_workspace(workspace)
        slug = ws["slug"]
        sidecar_id = sidecar_id_for_slug(slug)
        sidecar_dir = base_sidecars_dir / sidecar_id
        sidecar_json_path = sidecar_dir / "sidecar.json"

        # Ordre anti-race : retirer d'abord le sidecar de config.json avant de nettoyer projects/.
        config_updated = False
        if configure_config is not False and cfg_path.is_file():
            config_updated = _update_antigravity_config(
                cfg_path, sidecar_id, remove=True
            )

        removed_sidecar_dir = False
        if sidecar_dir.exists():
            shutil.rmtree(sidecar_dir)
            removed_sidecar_dir = True

        repaired_user_projects: List[Dict[str, str]] = []
        removed_bridge_projects: List[str] = []
        if configure_config is not False:
            repaired_user_projects = repair_polluted_user_projects(
                sidecar_id, slug, projects_dir=pdir
            )
            for json_file, loaded in _find_project_files(pdir):
                if _is_dedicated_bridge_project(loaded, sidecar_id, slug):
                    try:
                        json_file.unlink()
                        removed_bridge_projects.append(str(json_file))
                    except OSError:
                        pass

        updated_ws = store.set_workspace_sidecar(
            slug, None, clear_project_id=(configure_config is not False)
        )
        return {
            "action": "unlinked",
            "slug": slug,
            "canonical_path": updated_ws["canonical_path"],
            "sidecar_id": sidecar_id,
            "sidecar_dir": str(sidecar_dir),
            "sidecar_json_path": str(sidecar_json_path),
            "removed_sidecar_dir": removed_sidecar_dir,
            "config_updated": config_updated,
            "repaired_user_projects": repaired_user_projects,
            "removed_bridge_projects": removed_bridge_projects,
            "sidecar_status": updated_ws["sidecar_status"],
        }

    ws = store.resolve_workspace(workspace, allow_auto_register=True)
    slug = ws["slug"]
    sidecar_id = sidecar_id_for_slug(slug)

    relay = (relay_script or RELAY_SCRIPT_PATH).expanduser().resolve()
    if not relay.is_file():
        raise ValidationError(f"Script sidecar_relay.py introuvable : {relay}")

    should_configure = configure_config is not False
    source_project: Optional[Tuple[str, str, Dict[str, Any]]] = None
    if should_configure or clean_source is not None:
        source_project = _load_source_project_data(
            pdir,
            canonical_path=ws["canonical_path"],
            slug=slug,
            sidecar_id=sidecar_id,
            source_project_id=clean_source,
        )

    preferred_pid = _read_configured_sidecar_project_id(cfg_path, sidecar_id) or ws.get("project_id")
    bridge_proj: Optional[Dict[str, Any]] = None
    if should_configure:
        # Valide / crée le projet dédié avant toute écriture sur sidecar.json ou config.json.
        bridge_proj = ensure_bridge_sidecar_project(
            pdir,
            slug,
            sidecar_id,
            ws["canonical_path"],
            explicit_project_id=clean_project,
            preferred_project_id=preferred_pid,
            source_project=source_project,
        )

    py_cmd, py_prefix_args = detect_local_python_command()
    env_map: Dict[str, str] = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    default_home = default_bridge_home()
    if store.home_dir != default_home:
        env_map["GRAVITY_BRIDGE_HOME"] = str(store.home_dir)

    sidecar_dir = base_sidecars_dir / sidecar_id
    sidecar_json_path = sidecar_dir / "sidecar.json"
    sidecar_payload: Dict[str, Any] = {
        "display_name": f"Gravity Bridge · {slug}",
        "description": f"Relais Gravity Bridge vers le projet Antigravity App {slug} via agentapi.",
        "command": py_cmd,
        "args": [*py_prefix_args, "-B", str(relay), "--workspace", slug],
        "restart_policy": "always",
        "env": env_map,
    }
    _atomic_write_json(sidecar_json_path, sidecar_payload)

    config_updated = False
    repaired_user_projects = []
    if should_configure and bridge_proj is not None:
        config_updated = _update_antigravity_config(
            cfg_path,
            sidecar_id,
            project_id=bridge_proj["id"],
            remove=False,
        )
        repaired_user_projects = repair_polluted_user_projects(
            sidecar_id, slug, projects_dir=pdir
        )
        updated_ws = store.set_workspace_sidecar(
            slug,
            sidecar_id,
            project_id=bridge_proj["id"],
        )
    else:
        updated_ws = store.set_workspace_sidecar(
            slug,
            sidecar_id,
            project_id=clean_project,
        )

    source_proj_id = source_project[0] if source_project is not None else None
    source_proj_name = source_project[1] if source_project is not None else None
    project_auto_detected = bool(
        should_configure and source_project is not None and clean_source is None
    )

    if config_updated and bridge_proj is not None:
        proj_label = f"{bridge_proj['name']} ({bridge_proj['id']})"
        next_steps = [
            f"Micro-sidecar '{sidecar_id}' configuré et activé dans {cfg_path} sur le projet dédié '{proj_label}' (schemaVersion=1).",
        ]
        if source_project is not None:
            next_steps.append(
                f"Ressources, paramètres et permissions recopiés depuis '{source_proj_name} ({source_proj_id})' sans l'attacher au sidecar."
            )
        else:
            next_steps.append(
                f"Aucun projet utilisateur préexistant détecté ; dossier '{ws['canonical_path']}' attaché directement au projet dédié '{bridge_proj['name']}'."
            )
        if repaired_user_projects:
            repaired_desc = ", ".join(f"{p['name']} ({p['id']})" for p in repaired_user_projects)
            next_steps.append(
                f"Projet(s) utilisateur réparé(s) (clé 'sidecar' retirée) : {repaired_desc}. Rechargez ou redémarrez Antigravity App."
            )
        next_steps.append(
            "Vérifiez que son statut passe à 'online' via /gravity-bridge workspaces."
        )
    else:
        expected_name = bridge_project_name_for_slug(slug, base_name=source_proj_name)
        next_steps = [
            f"Micro-sidecar '{sidecar_id}' écrit dans {sidecar_json_path} (config.json et projects/ non modifiés car configure_config=false).",
            f"Relancez /gravity-bridge link {slug} sans --no-configure-config pour activer son projet dédié '{expected_name}'.",
        ]

    return {
        "action": "linked",
        "slug": slug,
        "canonical_path": updated_ws["canonical_path"],
        "sidecar_id": sidecar_id,
        "sidecar_dir": str(sidecar_dir),
        "sidecar_json_path": str(sidecar_json_path),
        "project_id": updated_ws["project_id"],
        "project_name": bridge_proj["name"] if bridge_proj is not None else None,
        "bridge_project_path": bridge_proj["path"] if bridge_proj is not None else None,
        "bridge_project_created": bridge_proj["created"] if bridge_proj is not None else False,
        "source_project_id": source_proj_id,
        "source_project_name": source_proj_name,
        "project_auto_detected": project_auto_detected,
        "repaired_user_projects": repaired_user_projects,
        "cleaned_orphan_projects": (
            bridge_proj["cleaned_orphan_projects"] if bridge_proj is not None else []
        ),
        "config_updated": config_updated,
        "sidecar_status": updated_ws["sidecar_status"],
        "next_steps": next_steps,
    }
