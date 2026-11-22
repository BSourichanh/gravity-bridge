# Règles développeur — Gravity Bridge

## Flux Git & Releases
- Tout développement a lieu sur la branche `dev` ; ne jamais développer directement sur `main`.
- La fusion vers `main` et le tag annoté `vX.Y.Z` ne sont effectués qu'au moment d'une release.
- Messages de commit au format *Conventional Commits* (`feat(...)`, `fix(...)`, `docs:`, `test(...)`, `chore(...)`).
- Maintenir le numéro SemVer synchronisé entre `gravity-bridge-plugin/plugin.json` et `CHANGELOG.md`.

## Contraintes techniques strictes
- **100 % bibliothèque standard Python 3** (compatible Python 3.10+) : aucune dépendance tierce dans `gravity-bridge-plugin/` ni dans `install.py` ou `tests/`.
- **Zéro hook permanent :** ne pas introduire de fichier `hooks.json` ; le plugin reste strictement à la demande (`/gravity-bridge`).
- **Zéro pollution des dépôts cibles :** ne jamais créer de fichier dans les workspaces suivis ; tout stockage centralisé vit dans `$GRAVITY_BRIDGE_HOME` (`~/.gemini/gravity-bridge/`).
- **Portabilité Windows / POSIX :** conserver `"command": "python3"` dans `gravity-bridge-plugin/mcp_config.json` côté dépôt ; l'adaptation vers `python` / `py -3` ne s'applique qu'à la copie installée via `install.py` ou `validate.py --fix-python-cmd`.
- **Isolation des tests :** tous les tests doivent utiliser `TemporaryDirectory` / `$GRAVITY_BRIDGE_HOME` temporaire sans toucher `~/.gemini/`.

## Suite de validation obligatoire avant commit
```powershell
python gravity-bridge-plugin/scripts/validate.py --strict
agy plugin validate ./gravity-bridge-plugin
python -m unittest discover -s tests -p "test_*.py" -v
```
