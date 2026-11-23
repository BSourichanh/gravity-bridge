# Gravity Bridge — Exemples & Scénarios

## 1. Envoi et dispatch (`send_message`)

Avant d'appeler avec `dispatch=true`, présenter à l'utilisateur la cible, le type (`question` | `handoff` | `info`), la posture attendue, l'objet et les `file_refs`, puis attendre son feu vert unique.

```json
{
  "from_workspace": "gravity-studio",
  "to_workspace": "gravity-bridge",
  "msg_type": "handoff",
  "subject": "Implémenter le lot X",
  "body": "Périmètre, décisions figées et vérifications attendues...",
  "file_refs": ["plugin/core/store.py", "tests/test_store.py"],
  "approved": true,
  "dispatch": true,
  "caller_conversation_id": "<UUID conversation appelante>"
}
```

- `notify_caller` omis : résolu **Auto si prêt** (`true` si `caller_conversation_id` fourni et sidecar appelant `online`, `false` sinon).
- `dispatch=false` : dépôt passif dans la boîte aux lettres sans ouvrir de conversation App.
- Si un fil passif existant doit être lancé ensuite : `dispatch_thread(thread_id="thr-...", approved=true, caller_conversation_id="...")`.

## 2. Prise en charge cible (`take` & `reply_thread`)

Dans la conversation ouverte côté cible (`depth=1`, `GRAVITY_BRIDGE_DEPTH >= 1`) :

1. Lire le fil :
```json
{
  "thread_id": "thr-20261123-abc123",
  "reader_workspace": "gravity-bridge",
  "mark_read": true
}
```
2. Si `msg_type == "handoff"` et `approved == true`, exécuter directement dans le périmètre et sous les règles (`AGENTS.md`) du dépôt cible.
3. En cas de question ou blocage avant la fin, répondre sans clôturer (`close=false, dispatch=false`). Si cette réponse complète un job outbound `dispatched` actif et que le sidecar appelant est `online`, un callback intermédiaire `status=open` est enfilé automatiquement sans poser `callback_dispatched_at` :
```json
{
  "thread_id": "thr-20261123-abc123",
  "from_workspace": "gravity-bridge",
  "body": "Question : valider le choix A ou B avant finalisation.",
  "conversation_id": "<UUID conversation cible>",
  "close": false,
  "dispatch": false
}
```
4. L'appelant peut alors relancer la conversation cible après arbitrage humain via `reply_thread(..., dispatch=true, approved=true)`.
5. À la fin du lot, clôturer via `reply_thread(..., close=true, dispatch=false)` pour déclencher le callback final *one-shot* `status=done` :
```json
{
  "thread_id": "thr-20261123-abc123",
  "from_workspace": "gravity-bridge",
  "body": "1. Synthèse...\n2. Fichiers modifiés...\n3. Vérifications exactes...",
  "file_refs": ["plugin/core/store.py"],
  "conversation_id": "<UUID conversation cible>",
  "close": true,
  "dispatch": false
}
```

## 3. Liaison sidecar (`link_workspace`)

Pour lier un projet cible et réparer tout projet utilisateur pollué par `"sidecar"` :
```json
{
  "workspace": "gravity-bridge"
}
```
Génère `~/.gemini/config/sidecars/gravity-bridge-<slug>/sidecar.json`, crée/met à jour `<nom> (Bridge)` dans `~/.gemini/config/projects/*.json` et active le sidecar dans `~/.gemini/config/config.json` (`"schemaVersion": 1`).
