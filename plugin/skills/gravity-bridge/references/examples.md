# Gravity Bridge — Exemples & Scénarios

## 1. Envoi et dispatch (`send_message`)

Avant d'appeler avec `dispatch=true`, présenter à l'utilisateur la cible, le type (`question` | `handoff` | `info`), la posture attendue, l'objet et les `file_refs`, puis attendre son feu vert unique.

```json
{
  "from_workspace": "gravity-studio",
  "to_workspace": "gravity-bridge",
  "msg_type": "handoff",
  "workflow": "standard",
  "subject": "Implémenter le lot X",
  "body": "Périmètre, décisions figées et vérifications attendues...",
  "file_refs": ["plugin/core/store.py", "tests/test_store.py"],
  "approved": true,
  "dispatch": true,
  "caller_conversation_id": "<UUID conversation appelante>"
}
```

- `workflow` omis : résolu à `"standard"` sur `handoff` et `"none"` sur `question`/`info` (`"none"` seul autorisé sur `question`/`info`).
- `notify_caller` omis : résolu **Auto si prêt** (`true` si `caller_conversation_id` fourni et sidecar appelant `online`, `false` sinon).
- `dispatch=false` : dépôt passif dans la boîte aux lettres sans ouvrir de conversation App.
- Si un fil passif existant doit être lancé ensuite : `dispatch_thread(thread_id="thr-...", approved=true, workflow="standard", caller_conversation_id="...")`.

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
5. À la fin du lot, clôturer via `reply_thread(..., close=true, dispatch=false)` avec `file_refs` relatifs (`1..20` entrées max, jamais `> 20`) pour déclencher le callback final *one-shot* `status=done` :
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

## 4. Orchestration par lots (`batch_id`, `callback_mode`, `cancel_job`)

Pour grouper plusieurs `send_message` dans un lot avec callback retour unique à la fin (`on_batch_complete`) :

```json
{
  "from_workspace": "gravity-architect",
  "to_workspace": "gravity-metrics",
  "msg_type": "handoff",
  "workflow": "standard",
  "batch_id": "wave-2-core",
  "callback_mode": "on_batch_complete",
  "subject": "Lot W2 — Metrics",
  "body": "Implémenter la tâche T1...",
  "approved": true,
  "dispatch": true,
  "caller_conversation_id": "<UUID conversation appelante>"
}
```

- `callback_mode` : `"per_thread"` (défaut) ou `"on_batch_complete"` (exige `batch_id`).
- Sur `on_batch_complete` :
  - Tant que d'autres fils du lot restent `open`, `reply_thread(..., close=true)` retourne `callback_status="waiting_batch"` sans callback anticipé.
  - Dès que le dernier fil clôture, `callback_status="queued"` (`batch_status="batch_complete"`) pose `callback_dispatched_at` sur le lot et injecte `batch_summary` dans le prompt callback (`depth=0` autorise la poursuite autonome sur la vague suivante d'un plan pré-approuvé ; `depth >= 1` reste verrouillé).
  - Si un fil répond avec `close=false` après un job outbound actif : `batch_status="fail_fast"` déclenche immédiatement le callback d'arrêt.
- Sérialisation & TTL : sur un même `target_workspace`, les jobs `handoff` `outbound` s'exécutent un par un sans jamais bloquer les `callback` ni `question`/`info` ; un job `dispatched` dépassant `GRAVITY_BRIDGE_DISPATCHED_STALE_SECONDS` (défaut `1800` s) passe à `failed`.
- Annulation ciblée par fil : `manage_thread(action="cancel_job", thread_id="thr-...")`.
