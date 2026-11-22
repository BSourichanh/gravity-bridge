# Gravity Bridge

- Sur `/gravity-bridge [workspaces|send|read|take|reply|status|done|prune]` uniquement, utiliser le skill `gravity-bridge` et le serveur MCP `gravity-bridge_bridge` (zéro appel proactif hors invocation).
- Exiger une validation humaine explicite avant tout `send_message(..., dispatch=true)`, `reply_thread(..., dispatch=true)` ou `dispatch_thread` (feu vert unique).
- Si `GRAVITY_BRIDGE_DEPTH >= 1`, ne jamais déclencher de sous-dispatch ; répondre uniquement en base via `reply_thread(..., dispatch=false)`.
