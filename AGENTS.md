# Gravity Bridge — Instructions Agents

## Périmètre & Architecture
Plugin Antigravity autonome de communication inter-workspaces, 100 % sans dépendance :
- `gravity-bridge-plugin/` : boîte aux lettres SQLite WAL centralisée (`core/store.py`, `core/security.py`), gestionnaire et relais micro-sidecars `agentapi` avec projet dédié `<nom> (Bridge)` (`core/sidecar_manager.py`, `scripts/sidecar_relay.py`), serveur MCP stdio 9 outils (`mcp/server.py`, `mcp_config.json`), skill `/gravity-bridge` (`skills/gravity-bridge/SKILL.md`), règle légère (`rules/AGENTS.md`) et validateur (`scripts/validate.py`).
- `install.py` : installeur/désinstalleur et liaison `--link` / `--unlink` ; `tests/` : suite `unittest`.

## Invariants
- **Branche de développement (`dev`) obligatoire :** tout développement s'effectue sur `dev` (jamais directement sur `main`).
- **Git & versioning (chef d'orchestre uniquement) :** fusion vers `main` et tag annoté `vX.Y.Z` uniquement aux releases sur accord utilisateur ; *Conventional Commits* ; SemVer synchronisé dans `gravity-bridge-plugin/plugin.json` + `CHANGELOG.md`. Sous-agents et workers `swarm` ne committent, ne poussent et ne taguent jamais.
- **100 % bibliothèque standard Python 3** (compatible Python 3.10+) dans `gravity-bridge-plugin/`, `install.py` et `tests/`.
- **Zéro hook permanent :** aucun fichier `hooks.json` ; le plugin reste strictement à la demande (`/gravity-bridge`).
- **Zéro pollution des dépôts cibles :** ne jamais créer de fichier dans les workspaces suivis ; tout stockage centralisé vit dans `$GRAVITY_BRIDGE_HOME` (`~/.gemini/gravity-bridge/`).
- **Portabilité Windows / POSIX :** conserver `"command": "python3"` dans `gravity-bridge-plugin/mcp_config.json` côté dépôt ; l'adaptation vers `python` / `py -3` ne s'applique qu'à la copie installée via `install.py` ou `validate.py --fix-python-cmd`.
- **Isolation des tests :** tous les tests utilisent `TemporaryDirectory` / `$GRAVITY_BRIDGE_HOME` temporaire sans toucher `~/.gemini/`.

## Vérifications obligatoires
```powershell
python gravity-bridge-plugin/scripts/validate.py --strict
agy plugin validate ./gravity-bridge-plugin
python -m unittest discover -s tests -p "test_*.py" -v
```
