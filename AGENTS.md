# Gravity Bridge — Instructions Agents

## Périmètre & Architecture
Plugin Antigravity autonome de communication inter-workspaces, 100 % sans dépendance :
- `plugin/` : contrat `config/module.json` (`id: "bridge"`), boîte aux lettres SQLite WAL centralisée (`core/store.py`, `core/security.py`), micro-sidecars `agentapi` avec projet dédié `<nom> (Bridge)` (`core/sidecar_manager.py`, `scripts/sidecar_relay.py`), serveur MCP stdio 9 outils (`mcp/server.py`, `mcp_config.json`), skill `/gravity-bridge` (`skills/gravity-bridge/SKILL.md`), règle légère (`rules/AGENTS.md`) et validateur (`scripts/validate.py`).
- `install.py` : installeur/désinstalleur (`PLUGIN_SOURCE = plugin/`) et liaison `--link` / `--unlink` ; `tests/` : suite `unittest`.

## Invariants
- **Branche `dev` obligatoire :** tout développement s'effectue sur `dev` (jamais directement sur `main`).
- **Git & versioning (chef d'orchestre uniquement) :** fusion vers `main` et tag annoté `vX.Y.Z` uniquement aux releases sur accord utilisateur ; *Conventional Commits* ; SemVer synchronisé dans `plugin/plugin.json` + `CHANGELOG.md`. Sous-agents et workers `swarm` ne committent, ne poussent et ne taguent jamais.
- **Qualité & 100 % stdlib Python 3.10+ :** appliquer KISS, YAGNI, DRY, SOLID, CUPID et zéro dépendance tierce dans `plugin/`, `install.py` et `tests/` (UTF-8 sans BOM, LF).
- **Zéro hook permanent :** aucun `hooks.json` ; le plugin reste strictement à la demande (`/gravity-bridge`).
- **Zéro pollution des dépôts cibles :** aucun fichier dans les workspaces suivis ; stockage centralisé résolu via `$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/`.
- **Portabilité Windows / POSIX :** conserver `"command": "python3"` dans `plugin/mcp_config.json` côté dépôt ; adaptation `python` / `py -3` sur la copie installée via `install.py` ou `validate.py --fix-python-cmd`.
- **Isolation des tests :** tout test utilise `TemporaryDirectory` et variables temporaires sans jamais toucher `~/.gemini/`.

## Vérifications obligatoires
```powershell
python plugin/scripts/validate.py --strict
agy plugin validate ./plugin --strict
python -m unittest discover -s tests -p "test_*.py" -v
```
