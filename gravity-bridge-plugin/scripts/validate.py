#!/usr/bin/env python3
"""Validateur strict du plugin autonome Gravity Bridge (100 % stdlib Python 3)."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT_DEFAULT = SCRIPT_DIR.parent

OFFICIAL_PLUGIN_FIELDS = {
    "name",
    "displayName",
    "version",
    "description",
    "logo",
    "suggestedPrompts",
    "author",
    "homepage",
    "repository",
    "disabled",
}
REQUIRED_MANIFEST_FIELDS = OFFICIAL_PLUGIN_FIELDS - {"disabled"}
ALLOWED_LOCAL_MODULES = {
    "core",
    "security",
    "store",
    "sidecar_manager",
    "sidecar_relay",
    "validate",
    "server",
}
EXPECTED_MCP_TOOLS = {
    "register_workspace",
    "link_workspace",
    "list_workspaces",
    "send_message",
    "list_inbox",
    "read_thread",
    "reply_thread",
    "dispatch_thread",
    "manage_thread",
}

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PRIVATE_LEAK_RES: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "chemin personnel",
        re.compile(
            r"(?:"
            + r"[A-Za-z]:\\" + r"(?:Users|Documents)\\"
            + r"|[A-Za-z]:\\[^\\\s\"'`]+\\" + r"Documents\\"
            + r"|/Us" + r"ers/[^/\s\"'`]+/"
            + r"|/ho" + r"me/[^/\s\"'`]+/"
            + r")",
            re.IGNORECASE,
        ),
    ),
)


class Report:
    """Collecteur d'erreurs, d'avertissements et d'informations de validation."""

    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def ok(self, strict: bool) -> bool:
        return len(self.errors) == 0 and (not strict or len(self.warnings) == 0)


def has_real_python3() -> bool:
    """Vérifie la présence d'un vrai exécutable python3 (en ignorant le stub WindowsApps)."""
    found = shutil.which("python3")
    if not found:
        return False
    if os.name == "nt" and "windowsapps" in found.lower():
        return False
    return True


def _detect_portable_python_cmd() -> str:
    py_bin = shutil.which("python")
    if py_bin and not (os.name == "nt" and "windowsapps" in py_bin.lower()):
        return "python"
    if shutil.which("py"):
        return "py -3"
    return "python"


def fix_python_cmd(plugin_root: Path) -> int:
    """Adapte 'python3' vers 'python' ou 'py -3' dans mcp_config.json sur une copie installée."""
    if has_real_python3():
        print("[INFO]  Exécutable 'python3' réel détecté dans le PATH : aucune adaptation nécessaire.")
        return 0

    py_cmd = _detect_portable_python_cmd()
    mcp_json = plugin_root / "mcp_config.json"
    if not mcp_json.is_file():
        print(f"[INFO]  Aucun changement requis dans {plugin_root}.")
        return 0

    mdata = json.loads(mcp_json.read_text(encoding="utf-8"))
    changed = False
    servers = mdata.get("mcpServers") if isinstance(mdata, dict) else None
    if isinstance(servers, dict):
        for scfg in servers.values():
            if not isinstance(scfg, dict):
                continue
            if scfg.get("command") == "python3":
                args = list(scfg.get("args") or [])
                if py_cmd == "py -3":
                    scfg["command"] = "py"
                    if not args or args[0] != "-3":
                        scfg["args"] = ["-3", *args]
                else:
                    scfg["command"] = py_cmd
                changed = True

    if changed:
        mcp_json.write_text(
            json.dumps(mdata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[FIXED] {mcp_json} -> '{py_cmd}'")
    else:
        print(f"[INFO]  Aucun changement requis dans {plugin_root}.")
    return 0


def _configure_stdio() -> None:
    """Force stdout/stderr en UTF-8 pour éviter les erreurs cp1252 sous Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _check_stdlib_only(py_file: Path, report: Report) -> None:
    stdlib_names_raw = getattr(sys, "stdlib_module_names", None)
    if not stdlib_names_raw:
        report.error("Python 3.10+ requis pour la validation stdlib.")
        return
    allowed: Set[str] = set(stdlib_names_raw) | ALLOWED_LOCAL_MODULES
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except SyntaxError as exc:
        report.error(f"Syntaxe Python invalide dans {py_file}: {exc}")
        return
    for node in ast.walk(tree):
        mods: List[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module.split(".")[0]]
        for mod in mods:
            if mod not in allowed:
                report.error(f"Import non stdlib {mod!r} détecté dans {py_file}")


def _validate_manifest(root: Path, report: Report) -> None:
    manifest_path = root / "plugin.json"
    if not manifest_path.is_file():
        report.error("plugin.json introuvable à la racine du plugin.")
        return

    raw_bytes = manifest_path.read_bytes()
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        report.error("plugin.json ne doit pas contenir de BOM UTF-8.")

    try:
        manifest = json.loads(raw_bytes.decode("utf-8-sig"))
    except Exception as exc:
        report.error(f"plugin.json invalide : {exc}")
        return

    if not isinstance(manifest, dict):
        report.error("plugin.json doit contenir un objet JSON.")
        return

    unknown_keys = sorted(set(manifest.keys()) - OFFICIAL_PLUGIN_FIELDS)
    if unknown_keys:
        report.error(f"plugin.json : champs non officiels détectés : {unknown_keys}")

    missing_req = sorted(REQUIRED_MANIFEST_FIELDS - set(manifest.keys()))
    if missing_req:
        report.error(f"plugin.json : champs requis manquants : {missing_req}")

    if manifest.get("name") != "gravity-bridge":
        report.error(
            f"plugin.json : 'name' doit être 'gravity-bridge', obtenu {manifest.get('name')!r}."
        )

    version = str(manifest.get("version") or "")
    if not SEMVER_RE.match(version):
        report.error(f"plugin.json : 'version' SemVer invalide ({version!r}).")

    desc = str(manifest.get("description") or "")
    if not desc or len(desc) > 160:
        report.error(f"plugin.json : 'description' vide ou > 160 caractères ({len(desc)}).")

    prompts = manifest.get("suggestedPrompts")
    if (
        not isinstance(prompts, list)
        or len(prompts) != 3
        or not all(isinstance(p, str) and p.strip() for p in prompts)
    ):
        report.error("plugin.json : exactement 3 'suggestedPrompts' non vides sont requis.")

    logo_rel = str(manifest.get("logo") or "")
    logo_path = root / logo_rel
    if not logo_rel or not logo_path.is_file():
        report.error(f"Logo introuvable : {logo_rel!r}")
    elif logo_path.suffix.lower() == ".svg":
        svg = logo_path.read_text(encoding="utf-8", errors="replace")
        vb = re.search(
            r"""viewBox\s*=\s*["']0\s+0\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)["']""",
            svg,
        )
        if not vb or float(vb.group(1)) <= 0 or float(vb.group(1)) != float(vb.group(2)):
            report.error(f"Logo {logo_rel} : viewBox carré 1:1 non détecté.")


def _validate_skills(root: Path, report: Report) -> None:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        report.error("Dossier requis manquant : skills/gravity-bridge/")
        return

    names = [p.name for p in sorted(skills_dir.iterdir()) if p.is_dir()]
    if names != ["gravity-bridge"]:
        report.error(
            f"skills/ doit contenir uniquement 'gravity-bridge/' (trouvé : {names})."
        )
        return

    skill_md = skills_dir / "gravity-bridge" / "SKILL.md"
    if not skill_md.is_file():
        report.error("skills/gravity-bridge/SKILL.md introuvable.")
        return

    txt = skill_md.read_text(encoding="utf-8", errors="replace")
    fm_match = re.match(r"^\s*---\r?\n(.*?)\r?\n---\r?\n", txt, re.DOTALL)
    if not fm_match:
        report.error("skills/gravity-bridge/SKILL.md : frontmatter YAML manquant.")
        return
    fm = fm_match.group(1)
    if not re.search(r"^name:\s*gravity-bridge\s*$", fm, re.MULTILINE):
        report.error("skills/gravity-bridge/SKILL.md : 'name: gravity-bridge' manquant.")
    desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    if not desc_m or not desc_m.group(1).strip():
        report.error("skills/gravity-bridge/SKILL.md : 'description' manquante ou vide.")


def _validate_rules_and_no_hooks(root: Path, report: Report) -> None:
    if (root / "hooks.json").exists():
        report.error("hooks.json détecté : gravity-bridge impose zéro hook permanent.")

    rules_file = root / "rules" / "AGENTS.md"
    if not rules_file.is_file():
        report.error("rules/AGENTS.md introuvable.")
        return

    raw_bytes = rules_file.read_bytes()
    if len(raw_bytes) >= 2048:
        report.error(f"rules/AGENTS.md dépasse 2048 octets ({len(raw_bytes)}).")

    rules_txt = raw_bytes.decode("utf-8", errors="replace")
    if rules_txt.lstrip().startswith("---"):
        report.error("rules/AGENTS.md ne doit pas contenir de frontmatter YAML.")

    non_empty_lines = [ln for ln in rules_txt.splitlines() if ln.strip()]
    if len(non_empty_lines) > 5:
        report.error(
            f"rules/AGENTS.md doit rester minimal (<= 5 lignes non vides, trouvé {len(non_empty_lines)})."
        )


def _validate_mcp_config(root: Path, report: Report) -> None:
    mcp_json = root / "mcp_config.json"
    if not mcp_json.is_file():
        report.error("mcp_config.json introuvable à la racine du plugin.")
        return

    try:
        mcp_raw = mcp_json.read_text(encoding="utf-8")
        mcp_data = json.loads(mcp_raw)
    except Exception as exc:
        report.error(f"mcp_config.json invalide : {exc}")
        return

    if not isinstance(mcp_data, dict) or not isinstance(mcp_data.get("mcpServers"), dict):
        report.error("mcp_config.json doit contenir un objet 'mcpServers'.")
        return

    servers = mcp_data["mcpServers"]
    if "bridge" not in servers or not isinstance(servers["bridge"], dict):
        report.error("mcp_config.json : serveur 'bridge' manquant.")
        return

    for sname, scfg in servers.items():
        if not isinstance(scfg, dict):
            report.error(f"mcp_config.json ({sname}) doit être un objet.")
            continue
        cmd = scfg.get("command")
        args = scfg.get("args")
        if not isinstance(cmd, str) or not cmd.strip():
            report.error(f"mcp_config.json ({sname}) : 'command' manquant.")
        if not isinstance(args, list):
            report.error(f"mcp_config.json ({sname}) : 'args' doit être une liste.")
            continue
        for arg in args:
            if isinstance(arg, str) and arg.endswith(".py"):
                rel = (
                    arg.replace("${PLUGIN_ROOT}/", "")
                    .replace("${PLUGIN_ROOT}\\", "")
                    .lstrip("/\\")
                )
                if not (root / rel).is_file():
                    report.error(f"mcp_config.json ({sname}) : script introuvable {arg!r}")

    if os.name == "nt" and '"command": "python3"' in mcp_raw and not has_real_python3():
        report.info(
            "mcp_config.json portable ('python3') détecté sous Windows : adapté sur la copie installée."
        )


def _run_mcp_dry_run(root: Path, report: Report) -> None:
    server_script = root / "mcp" / "server.py"
    if not server_script.is_file():
        report.error(f"Serveur MCP introuvable : {server_script}")
        return

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["GRAVITY_BRIDGE_HOME"] = str(Path(tmp) / "bridge-home")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        env.pop("GRAVITY_BRIDGE_DEPTH", None)

        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "validate", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_workspaces", "arguments": {}},
            },
        ]
        stdin_payload = "\n".join(json.dumps(r) for r in requests) + "\n"
        proc = subprocess.run(
            [sys.executable, "-B", str(server_script)],
            input=stdin_payload,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(root),
            env=env,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            report.error(f"Dry-run MCP échoué (rc={proc.returncode}): {proc.stderr}")
            return

        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if len(lines) != 3:
            report.error(
                f"Dry-run MCP : 3 réponses attendues, obtenu {len(lines)} ({proc.stderr})"
            )
            return

        try:
            resps: Dict[int, Dict[str, object]] = {
                obj["id"]: obj["result"] for obj in (json.loads(ln) for ln in lines)
            }
        except Exception as exc:
            report.error(f"Dry-run MCP : JSON-RPC invalide ({exc})")
            return

        tools = (resps.get(2) or {}).get("tools") or []  # type: ignore[union-attr]
        tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
        if tool_names != EXPECTED_MCP_TOOLS:
            report.error(
                f"MCP tools/list incomplet : attendu {sorted(EXPECTED_MCP_TOOLS)}, obtenu {sorted(str(n) for n in tool_names)}"
            )

        call_res = resps.get(3) or {}
        structured = call_res.get("structuredContent") if isinstance(call_res, dict) else None
        if (
            not isinstance(call_res, dict)
            or call_res.get("isError")
            or not isinstance(structured, dict)
            or structured.get("workspaces") != []
        ):
            report.error(f"MCP list_workspaces résultat invalide : {call_res!r}")


def validate_plugin(root: Path, report: Report) -> None:
    """Audite le manifeste, les skills/règles, la config MCP, la stdlib et le dry-run MCP."""
    _validate_manifest(root, report)
    _validate_skills(root, report)
    _validate_rules_and_no_hooks(root, report)
    _validate_mcp_config(root, report)

    for sub in ("assets", "core", "mcp", "rules", "scripts", "skills"):
        if not (root / sub).is_dir():
            report.error(f"Dossier requis manquant : {sub}/")

    if not (root / "README.md").is_file():
        report.error("README.md manquant à la racine du plugin.")

    if not (root / "scripts" / "sidecar_relay.py").is_file():
        report.error("Script requis manquant : scripts/sidecar_relay.py")

    for legacy in ("core/dispatcher.py", "scripts/worker.py"):
        if (root / legacy).exists():
            report.error(f"Ancien module CLI obsolète détecté : {legacy}")

    for py_file in sorted(root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        _check_stdlib_only(py_file, report)

    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file() or "__pycache__" in fpath.parts:
            continue
        if fpath.suffix.lower() not in (".py", ".md", ".json", ".svg"):
            continue
        content = fpath.read_text(encoding="utf-8", errors="replace")
        for label, rx in PRIVATE_LEAK_RES:
            if rx.search(content):
                report.error(f"Fuite spécifique détectée ({label}) dans {fpath}")

    _run_mcp_dry_run(root, report)


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Validateur Gravity Bridge")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Échoue également sur les avertissements.",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=PLUGIN_ROOT_DEFAULT,
        help="Racine du plugin gravity-bridge à auditer.",
    )
    parser.add_argument(
        "--fix-python-cmd",
        action="store_true",
        help="Adapte 'python3' vers 'python' ou 'py -3' dans mcp_config.json sous Windows.",
    )
    args = parser.parse_args(argv)
    plugin_root = args.plugin_root.expanduser().resolve()

    if args.fix_python_cmd:
        return fix_python_cmd(plugin_root)

    report = Report()
    validate_plugin(plugin_root, report)

    for info in report.infos:
        print(f"[INFO]  {info}")
    for err in report.errors:
        print(f"[ERROR] {err}")
    for warn in report.warnings:
        print(f"[WARN]  {warn}")

    if report.ok(args.strict):
        print(
            f"[PASS] Validation Plugin réussie (0 erreur, {len(report.warnings)} avertissement)."
        )
        return 0
    print(
        f"[FAIL] Validation Plugin échouée ({len(report.errors)} erreur(s), {len(report.warnings)} avertissement(s))."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
