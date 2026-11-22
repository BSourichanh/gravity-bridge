# Gravity Bridge (`v0.2.0`)

Plugin Antigravity autonome (**100 % bibliothèque standard Python 3**, zéro dépendance tierce, zéro CLI `agy`, zéro hook permanent) permettant la communication structurée et l'ouverture directe de conversations natives entre projets dans **Antigravity App**, sans jamais déposer de fichiers dans les dépôts suivis.

## Architecture

- **Contrat module & manifeste (`plugin/`) :** `plugin/plugin.json` (9 champs officiels Antigravity) et `plugin/config/module.json` (`schema_version: 1`, `id: "bridge"`, `storage.subdir: "bridge"`).
- **Boîte aux lettres centralisée SQLite WAL :** `~/.gemini/gravity/bridge/bridge.db` (résolution `home` > `$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge`). Stocke `workspaces` (avec heartbeat sidecar et `project_id`), `threads` (avec `caller_conversation_id`, `notify_caller`, `callback_dispatched_at`), `messages` et `jobs` (`direction: 'outbound' | 'callback'`) avec verrouillage atomique (`BEGIN IMMEDIATE`) et migration idempotente.
- **Micro-sidecars natifs Antigravity App (`agentapi`) :** chaque projet cible lié dispose d'un micro-sidecar léger (`~/.gemini/config/sidecars/gravity-bridge-<slug>/sidecar.json`) exécutant `scripts/sidecar_relay.py`. Après feu vert humain unique (`approved=True`), le relais crée une vraie conversation dans la barre latérale du projet cible via `agentapi new-conversation` (ou la poursuit via `agentapi send-message`), sans jamais lancer de CLI `agy` isolé ni toucher aux bases internes d'Antigravity.
- **Callback retour sécurisé (*one-shot*) :** si `notify_caller` est activé (explicitement ou en mode *Auto si prêt* lorsque `caller_conversation_id` est fourni et le sidecar appelant est `online`), la clôture du fil par la cible (`reply_thread(..., close=True, dispatch=False)`) déclenche côté serveur un unique job `direction='callback'` vers `caller_conversation_id` via `agentapi send-message` (zéro repli `new-conversation`, skip propre `skipped_sidecar_offline`/`skipped_sidecar_unlinked` si le sidecar appelant est indisponible).
- **Isolation & sécurité :** zéro pollution des dépôts cibles, fail-fast si le sidecar cible est `unlinked`/`offline`, anti-boucle (`depth=1`, `caller_conversation_id`, `GRAVITY_BRIDGE_DEPTH`, exclusion des jobs `callback` de `list_target_conversation_ids()`), plafond 32 Ko UTF-8 par message et filtrage heuristique des secrets (refus en entrée utilisateur, caviardage en sortie relais/log).

## Installation & Liaison des projets

### 1. Installer le plugin
```powershell
python install.py
```
L'installeur copie `plugin/` vers `~/.gemini/config/plugins/gravity-bridge/`, adapte automatiquement `"python3"` sur la copie installée sous Windows si nécessaire et exécute `validate.py --strict`.

Options utiles :
- `python install.py --dry-run` : simule l'installation sans écrire.
- `python install.py --uninstall` : supprime le plugin installé (préserve `~/.gemini/gravity/bridge/bridge.db`).

*(Ou via CLI : `agy plugin validate ./plugin --strict` puis `agy plugin install ./plugin`.)*

### 2. Lier un projet cible (micro-sidecar)
Depuis le chat (`/gravity-bridge link <slug> [chemin]`) ou en CLI :
```powershell
python install.py --link /chemin/vers/mon-projet
```
`link_workspace` / `install.py --link` génère `sidecar.json`, crée ou met à jour un **projet sidecar dédié `<nom> (Bridge)`** dans `~/.gemini/config/projects/*.json` (en recopiant `projectResources`, `settings` et `permissionGrants` du projet utilisateur détecté, ou directement le dossier local si absent) et active le sidecar dans `~/.gemini/config/config.json` avec `"enabled": true` et `"schemaVersion": 1`. Votre projet utilisateur n'est **jamais** marqué `"sidecar"` et reste donc toujours visible dans `Projects` même à `0` conversation, tandis que `<nom> (Bridge)` s'affiche uniquement pendant qu'une conversation Bridge y est ouverte *(tout projet utilisateur précédemment pollué par `"sidecar"` est automatiquement réparé ; options `--source-project-id <id>`, `--project-id <bridge_id>` ou `--no-configure-config` si besoin)*. Pour dissocier et supprimer le projet dédié : `python install.py --unlink <slug>`.

## Commandes `/gravity-bridge` & Outils MCP

| Commande | Outils MCP associés | Rôle |
| :--- | :--- | :--- |
| `/gravity-bridge` | `register_workspace`, `list_workspaces`, `list_inbox`, `manage_thread` | Enregistre le workspace actif et résume sidecars, boîte de réception et jobs |
| `/gravity-bridge workspaces` | `list_workspaces`, `register_workspace` | Liste les workspaces et leur statut sidecar (`online`/`offline`/`unlinked`) |
| `/gravity-bridge link <slug>` | `link_workspace` | Génère ou supprime le micro-sidecar d'un projet cible |
| `/gravity-bridge send` | `send_message` | Crée un fil (`question` \| `handoff` \| `info`) et ouvre la conversation cible si `dispatch=true` |
| `/gravity-bridge read <id>` | `read_thread` | Lit le fil, marque lu de façon ciblée et affiche `target_conversation_id` |
| `/gravity-bridge take <id>` | `read_thread`, `reply_thread` | Prend en charge un `handoff` dans l'App et clôture en retour |
| `/gravity-bridge reply <id>` | `reply_thread` | Ajoute une réponse (`--close` ou `--dispatch` via `agentapi send-message`) |
| `/gravity-bridge status [job]` | `manage_thread` (`get_job` / `cancel_job`) | Suit ou annule un job sidecar |
| `/gravity-bridge done` / `prune` | `manage_thread` (`close` / `prune_done`) | Clôture un fil ou purge les vieux fils `done` et leurs logs |

## Développement & validation

Tout développement s'effectue sur la branche `dev` (voir [AGENTS.md](AGENTS.md)). Suite obligatoire :

```powershell
python plugin/scripts/validate.py --strict
agy plugin validate ./plugin --strict
python -m unittest discover -s tests -p "test_*.py" -v
```
