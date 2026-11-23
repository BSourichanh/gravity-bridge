# Gravity Bridge — Instructions Agents

## Périmètre & Architecture
Plugin Antigravity inter-workspaces, 100 % sans dépendance :
- `plugin/` : `plugin.json`, `config/module.json` (`id: "bridge"`), SQLite WAL (`core/store.py`, `core/security.py`), sidecars `agentapi` + `<nom> (Bridge)` (`core/sidecar_manager.py`, `scripts/sidecar_relay.py`), MCP 9 outils (`mcp/server.py`, `mcp_config.json`), skill (`skills/gravity-bridge/SKILL.md`), règle (`rules/AGENTS.md`) et `scripts/validate.py`.
- `install.py` (`PLUGIN_SOURCE = plugin/`, `--link`/`--unlink`) et `tests/` (`unittest`).

## Invariants
- **Branche `dev` & Git (chef d'orchestre seul) :** dev sur `dev` (jamais `main`). Fusion `main` + tag `vX.Y.Z` aux releases sur accord utilisateur ; *Conventional Commits* ; SemVer `plugin/plugin.json` + `CHANGELOG.md`. Sous-agents/workers `swarm` : 0 commit, push ou tag.
- **Qualité & stdlib Python 3.10+ :** KISS, YAGNI, DRY, SOLID, CUPID ; 0 dépendance tierce, UTF-8 sans BOM, LF.
- **Zéro hook permanent :** aucun `hooks.json` ; exécution à la demande (`/gravity-bridge`).
- **Zéro pollution dépôts :** stockage `$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/`.
- **Portabilité Windows/POSIX :** `"command": "python3"` dans `plugin/mcp_config.json` (adapté sur copie via `install.py` / `validate.py --fix-python-cmd`).
- **Isolation tests :** `TemporaryDirectory` et env temporaires sans toucher `~/.gemini/`.

## Vérifications obligatoires
```powershell
python plugin/scripts/validate.py --strict
agy plugin validate ./plugin --strict
python -m unittest discover -s tests -p "test_*.py" -v
```
