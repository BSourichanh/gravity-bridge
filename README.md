# Gravity Bridge (`v0.1.0`)

Plugin Antigravity autonome (**100 % bibliothèque standard Python 3**, zéro dépendance tierce, zéro hook permanent) permettant la communication structurée et l'exécution déléguée entre workspaces locaux sans jamais déposer de fichiers dans les dépôts suivis.

## Architecture

- **Boîte aux lettres centralisée SQLite WAL :** `~/.gemini/gravity-bridge/bridge.db` (configurable via `$GRAVITY_BRIDGE_HOME`). Stocke `workspaces`, `threads`, `messages` et `jobs` avec verrouillage atomique contre les exécutions concurrentes et réconciliation des PIDs orphelins.
- **Instanciation asynchrone `agy` (`stream-json`) :** après une validation humaine unique (`approved=True`), le serveur MCP spawne `scripts/worker.py` en processus détaché dans le `cwd` du projet cible et exécute `agy --input-format stream-json --output-format stream-json --mode <plan|accept-edits>` avec reprise multi-tours via `--conversation <id>`.
- **Isolation & sécurité :** zéro pollution des dépôts cibles, anti-boucle fail-closed (`GRAVITY_BRIDGE_DEPTH >= 1`), plafond 32 Ko UTF-8 par message, filtrage heuristique des secrets (refus en entrée utilisateur, caviardage en sortie worker/log), jamais `--dangerously-skip-permissions`.

## Installation

### Via l'installeur inclus (recommandé)
```powershell
python install.py
```
L'installeur copie `gravity-bridge-plugin/` vers `~/.gemini/config/plugins/gravity-bridge/`, adapte automatiquement `"python3"` sur la copie installée sous Windows si nécessaire et exécute `validate.py --strict`.

Options utiles :
- `python install.py --dry-run` : simule l'installation sans écrire.
- `python install.py --uninstall` : supprime le plugin installé (préserve `~/.gemini/gravity-bridge/bridge.db`).

### Via Antigravity CLI
```powershell
agy plugin validate ./gravity-bridge-plugin
agy plugin install ./gravity-bridge-plugin
```

## Commandes `/gravity-bridge` & Outils MCP

| Commande | Outils MCP associés | Rôle |
| :--- | :--- | :--- |
| `/gravity-bridge` | `register_workspace`, `list_inbox`, `manage_thread` | Enregistre le workspace actif et résume boîte de réception et jobs |
| `/gravity-bridge workspaces` | `list_workspaces`, `register_workspace` | Liste ou enregistre un workspace par chemin |
| `/gravity-bridge send` | `send_message` | Crée un fil (`question` \| `handoff` \| `info`) et lance optionnellement le worker |
| `/gravity-bridge read <id>` | `read_thread` | Lit le fil, marque lu de façon ciblée et affiche `agy --conversation <id>` |
| `/gravity-bridge take <id>` | `read_thread`, `reply_thread` | Prend en charge un `handoff` dans l'App et clôture en retour |
| `/gravity-bridge reply <id>` | `reply_thread` | Ajoute une réponse (`--close` ou `--dispatch`) |
| `/gravity-bridge status [job]` | `manage_thread` (`get_job` / `cancel_job`) | Suit ou annule un worker actif |
| `/gravity-bridge done` / `prune` | `manage_thread` (`close` / `prune_done`) | Clôture un fil ou purge les vieux fils `done` et leurs logs |

## Développement & validation

Tout développement s'effectue sur la branche `dev` (voir [AGENTS.md](AGENTS.md)). Suite obligatoire :

```powershell
python gravity-bridge-plugin/scripts/validate.py --strict
agy plugin validate ./gravity-bridge-plugin
python -m unittest discover -s tests -p "test_*.py" -v
```
