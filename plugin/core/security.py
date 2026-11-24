"""Garde-fous de sécurité, validation et normalisation pour Gravity Bridge."""

from __future__ import annotations

import os
from pathlib import Path
import re
import unicodedata
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

MAX_BODY_BYTES = 32_768
MAX_SUBJECT_CHARS = 160
MAX_FILE_REFS = 20
MAX_FILE_REF_CHARS = 240
MAX_WAIT_SECONDS = 30
MAX_LOG_BYTES = 262_144
SIDECAR_STALE_SECONDS = 20
CLAIM_GRACE_SECONDS = 30
AGENTAPI_TIMEOUT_SECONDS = 30
SLUG_MAX_CHARS = 49
SIDECAR_ID_MAX_CHARS = 80
MODEL_MAX_CHARS = 80
CONVERSATION_ID_MIN_CHARS = 3
CONVERSATION_ID_MAX_CHARS = 128

ALLOWED_THREAD_MSG_TYPES = ("question", "handoff", "info")
ALLOWED_ALL_MSG_TYPES = ("question", "handoff", "info", "reply")
ALLOWED_JOB_ACTIONS = ("new_conversation", "send_message")
ALLOWED_JOB_DIRECTIONS = ("outbound", "callback")
ALLOWED_WORKFLOW_PROFILES = ("none", "quick", "standard", "deep", "evo")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
SIDECAR_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,79}\Z")
CONVERSATION_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{3,128}\Z")
MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}\Z")

_CONTROL_CHARS_STRICT_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_DRIVE_ANY_RE = re.compile(r"^[A-Za-z]:")

PEM_BLOCK_RE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----[\s\S]*?-----END \1-----"
)

SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "aws_access_key",
        re.compile(r"(?<![A-Za-z0-9_-])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    ),
    (
        "google_api_key",
        re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"),
    ),
    (
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})"
        ),
    ),
    (
        "generic_sk_key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    ),
    (
        "pem_private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ),
    (
        "bearer_auth_header",
        re.compile(
            r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._-]{20,}",
            re.IGNORECASE,
        ),
    ),
)


class BridgeError(Exception):
    """Erreur de base pour Gravity Bridge."""


class ValidationError(BridgeError):
    """Erreur de validation des entrées ou des paramètres."""


class SecretDetectedError(ValidationError):
    """Erreur levée lorsqu'un secret potentiel est détecté en entrée."""

    def __init__(self, rule_name: str, field_name: str = "content") -> None:
        self.rule_name = rule_name
        self.field_name = field_name
        super().__init__(
            f"Secret potentiel détecté dans '{field_name}' (règle : {rule_name}). Écriture refusée."
        )


class DispatchBlockedError(BridgeError):
    """Erreur levée lorsqu'un dispatch est bloqué par la politique de sécurité ou d'état."""


class NotFoundError(BridgeError):
    """Erreur levée lorsqu'une ressource (workspace, fil, job) est introuvable."""


def detect_secret_rule(text: str) -> Optional[str]:
    """Retourne le nom de la première règle secret déclenchée, ou None."""
    if not text:
        return None
    for rule_name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return rule_name
    return None


def assert_no_secrets(text: str, field_name: str = "content") -> None:
    """Lève SecretDetectedError si un secret potentiel est détecté dans text."""
    rule_name = detect_secret_rule(text)
    if rule_name is not None:
        raise SecretDetectedError(rule_name=rule_name, field_name=field_name)


def redact_secrets(text: str) -> Tuple[str, List[str]]:
    """Caviarde les secrets potentiels dans une sortie relais ou un log."""
    if not text:
        return text, []

    redacted = text
    triggered: List[str] = []

    def _record(rule: str) -> str:
        if rule not in triggered:
            triggered.append(rule)
        return f"[REDACTED:{rule}]"

    if PEM_BLOCK_RE.search(redacted):
        redacted = PEM_BLOCK_RE.sub(lambda _m: _record("pem_private_key"), redacted)

    for rule_name, pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            redacted = pattern.sub(lambda _m, r=rule_name: _record(r), redacted)

    return redacted, triggered


def is_explicit_workspace_path(value: str) -> bool:
    """Indique si value représente un chemin explicite (~, absolu, racine / ou lecteur Windows)."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if stripped == "~" or stripped.startswith(("~/", "~\\")):
        return True
    if stripped.startswith(("/", "\\")):
        return True
    if _WINDOWS_DRIVE_PREFIX_RE.match(stripped):
        return True
    return Path(stripped).is_absolute()


def canonical_path_key(canonical_path: str) -> str:
    """Retourne la clé de comparaison de chemin canonique adaptée à l'OS."""
    if os.name == "nt":
        return canonical_path.casefold()
    return canonical_path


def normalize_workspace_path(raw_path: str) -> str:
    """Normalise un chemin de répertoire existant et vérifie qu'il s'agit d'un dossier."""
    if not isinstance(raw_path, str):
        raise ValidationError("Le chemin du workspace doit être une chaîne de caractères.")
    stripped = raw_path.strip()
    if not stripped:
        raise ValidationError("Le chemin du workspace ne peut pas être vide.")
    if _CONTROL_CHARS_STRICT_RE.search(stripped):
        raise ValidationError("Le chemin du workspace contient des caractères de contrôle interdits.")

    try:
        resolved = Path(stripped).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"Chemin workspace introuvable ou inaccessible : {stripped}") from exc

    if not resolved.is_dir():
        raise ValidationError(f"Le chemin workspace n'est pas un répertoire : {resolved}")

    result = str(resolved)
    if os.name == "nt":
        if result.startswith("\\\\?\\UNC\\"):
            result = "\\\\" + result[8:]
        elif result.startswith("\\\\?\\"):
            result = result[4:]
        if len(result) >= 2 and result[1] == ":":
            result = result[0].upper() + result[1:]

    return result


def validate_slug(slug: str) -> str:
    """Valide un slug de workspace conforme à [a-z0-9][a-z0-9_-]{0,48}."""
    if not isinstance(slug, str):
        raise ValidationError("Le slug doit être une chaîne de caractères.")
    candidate = slug.strip()
    if not SLUG_RE.match(candidate):
        raise ValidationError(
            "Slug invalide : doit correspondre à ^[a-z0-9][a-z0-9_-]{0,48}$."
        )
    return candidate


def validate_sidecar_id(sidecar_id: str) -> str:
    """Valide un identifiant de sidecar Antigravity (<= 80 caractères, alphanumérique/-/_)."""
    if not isinstance(sidecar_id, str):
        raise ValidationError("sidecar_id doit être une chaîne de caractères.")
    candidate = sidecar_id.strip()
    if not SIDECAR_ID_RE.match(candidate):
        raise ValidationError(
            "sidecar_id invalide : doit correspondre à ^[a-z0-9][a-z0-9_-]{0,79}$."
        )
    return candidate


def slugify_name(name: str, fallback: str = "workspace") -> str:
    """Transforme un nom arbitraire en slug valide [a-z0-9][a-z0-9_-]{0,48}."""
    raw = str(name or "").strip()
    ascii_txt = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", ascii_txt.lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    while cleaned and not cleaned[0].isalnum():
        cleaned = cleaned[1:]
    cleaned = cleaned[:SLUG_MAX_CHARS].rstrip("-_")
    if not cleaned:
        if fallback and fallback != name:
            return slugify_name(fallback, fallback="workspace")
        return "workspace"
    return cleaned


def derive_slug_candidates(canonical_path: str) -> Tuple[str, str]:
    """Calcule le slug de base et le slug qualifié par le dossier parent."""
    path_obj = Path(canonical_path)
    base_slug = slugify_name(path_obj.name, fallback="workspace")
    parent_name = path_obj.parent.name if path_obj.parent else ""
    if not parent_name:
        return base_slug, base_slug

    parent_slug = slugify_name(parent_name, fallback="")
    if not parent_slug:
        return base_slug, base_slug

    combined = f"{parent_slug}-{base_slug}"
    if len(combined) > SLUG_MAX_CHARS:
        combined = combined[:SLUG_MAX_CHARS].rstrip("-_")
    if not SLUG_RE.match(combined):
        combined = slugify_name(combined, fallback=base_slug)
    return base_slug, combined


def validate_subject(subject: str) -> str:
    """Valide l'objet d'un fil (non vide, sans contrôle/saut de ligne, <= 160 car., sans secret)."""
    if not isinstance(subject, str):
        raise ValidationError("L'objet (subject) doit être une chaîne de caractères.")
    cleaned = subject.strip()
    if not cleaned:
        raise ValidationError("L'objet (subject) ne peut pas être vide.")
    if _CONTROL_CHARS_STRICT_RE.search(cleaned):
        raise ValidationError(
            "L'objet (subject) ne doit pas contenir de sauts de ligne ni de caractères de contrôle."
        )
    if len(cleaned) > MAX_SUBJECT_CHARS:
        raise ValidationError(
            f"L'objet (subject) dépasse le plafond de {MAX_SUBJECT_CHARS} caractères."
        )
    assert_no_secrets(cleaned, field_name="subject")
    return cleaned


def validate_body(body: str) -> str:
    """Valide le corps d'un message (non vide, sans nul, <= 32 768 octets UTF-8, sans secret)."""
    if not isinstance(body, str):
        raise ValidationError("Le corps (body) doit être une chaîne de caractères.")
    if "\x00" in body:
        raise ValidationError("Le corps (body) contient un octet nul interdit.")
    if not body.strip():
        raise ValidationError("Le corps (body) ne peut pas être vide.")
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > MAX_BODY_BYTES:
        raise ValidationError(
            f"Le corps (body) dépasse {MAX_BODY_BYTES} octets UTF-8 ({body_bytes} octets)."
        )
    assert_no_secrets(body, field_name="body")
    return body


def truncate_utf8_bytes(text: str, max_bytes: int, suffix: str = "") -> Tuple[str, bool]:
    """Tronque text aux frontières UTF-8 pour tenir dans max_bytes avec suffix optionnel."""
    if max_bytes < 0:
        raise ValidationError("max_bytes doit être positif.")
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False

    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) >= max_bytes:
        return suffix_bytes[:max_bytes].decode("utf-8", errors="ignore"), True

    budget = max_bytes - len(suffix_bytes)
    head = raw[:budget].decode("utf-8", errors="ignore")
    return f"{head}{suffix}", True


def validate_file_refs(file_refs: Optional[Sequence[str]]) -> List[str]:
    """Valide et normalise la liste de références de fichiers relatives et informatives."""
    if file_refs is None:
        return []
    if isinstance(file_refs, (str, bytes)) or not isinstance(file_refs, (list, tuple)):
        raise ValidationError("file_refs doit être une liste de chemins relatifs.")
    if len(file_refs) > MAX_FILE_REFS:
        raise ValidationError(f"file_refs dépasse le plafond de {MAX_FILE_REFS} entrées.")

    normalized: List[str] = []
    for item in file_refs:
        if not isinstance(item, str):
            raise ValidationError("Chaque élément de file_refs doit être une chaîne.")
        stripped = item.strip()
        if not stripped:
            raise ValidationError("file_refs ne peut pas contenir d'entrée vide.")
        if _CONTROL_CHARS_STRICT_RE.search(stripped):
            raise ValidationError("file_refs contient des caractères de contrôle interdits.")
        if len(stripped) > MAX_FILE_REF_CHARS:
            raise ValidationError(
                f"Une référence file_refs dépasse {MAX_FILE_REF_CHARS} caractères."
            )

        posix_ref = stripped.replace("\\", "/")
        if (
            posix_ref.startswith(("/", "~"))
            or _WINDOWS_DRIVE_ANY_RE.match(posix_ref)
            or Path(stripped).is_absolute()
        ):
            raise ValidationError(
                "file_refs doit contenir uniquement des chemins relatifs (absolu ou ~ interdit)."
            )

        segments = [seg for seg in posix_ref.split("/") if seg]
        if ".." in segments:
            raise ValidationError("file_refs interdit le segment parent '..'.")

        assert_no_secrets(posix_ref, field_name="file_refs")
        normalized.append(posix_ref)

    return normalized


def validate_msg_type(msg_type: str, *, allow_reply: bool = False) -> str:
    """Valide le type d'un fil ou d'un message."""
    if not isinstance(msg_type, str):
        raise ValidationError("msg_type doit être une chaîne de caractères.")
    allowed = ALLOWED_ALL_MSG_TYPES if allow_reply else ALLOWED_THREAD_MSG_TYPES
    if msg_type not in allowed:
        raise ValidationError(
            f"msg_type invalide ({msg_type!r}). Valeurs autorisées : {', '.join(allowed)}."
        )
    return msg_type


def validate_workflow_profile(workflow: Optional[str], *, msg_type: str) -> str:
    """Valide et normalise le profil gravity-workflow selon msg_type."""
    if workflow is None:
        return "standard" if msg_type == "handoff" else "none"
    if not isinstance(workflow, str):
        raise ValidationError("workflow doit être une chaîne de caractères.")
    normalized = workflow.strip().lower()
    if not normalized:
        return "standard" if msg_type == "handoff" else "none"
    if normalized not in ALLOWED_WORKFLOW_PROFILES:
        raise ValidationError(
            f"workflow invalide ({workflow!r}). Valeurs autorisées : {', '.join(ALLOWED_WORKFLOW_PROFILES)}."
        )
    if msg_type in ("question", "info") and normalized != "none":
        raise ValidationError(
            f"workflow={normalized!r} interdit sur msg_type={msg_type!r} (seul 'none' est autorisé)."
        )
    return normalized


def validate_job_action(action: str) -> str:
    """Valide l'action agentapi d'un job (new_conversation ou send_message)."""
    if not isinstance(action, str):
        raise ValidationError("action doit être une chaîne de caractères.")
    if action not in ALLOWED_JOB_ACTIONS:
        raise ValidationError(
            f"action de job invalide ({action!r}). Valeurs autorisées : {', '.join(ALLOWED_JOB_ACTIONS)}."
        )
    return action


def validate_job_direction(direction: str) -> str:
    """Valide la direction d'un job (outbound ou callback)."""
    if not isinstance(direction, str):
        raise ValidationError("direction doit être une chaîne de caractères.")
    if direction not in ALLOWED_JOB_DIRECTIONS:
        raise ValidationError(
            f"direction de job invalide ({direction!r}). Valeurs autorisées : {', '.join(ALLOWED_JOB_DIRECTIONS)}."
        )
    return direction


def validate_conversation_id(
    value: Optional[str],
    *,
    field_name: str = "conversation_id",
) -> Optional[str]:
    """Valide un identifiant de conversation Antigravity (None si vide)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} doit être une chaîne de caractères.")
    if _CONTROL_CHARS_STRICT_RE.search(value):
        raise ValidationError(f"{field_name} contient des caractères de contrôle interdits.")
    stripped = value.strip()
    if not stripped:
        return None
    if not CONVERSATION_ID_RE.match(stripped):
        raise ValidationError(
            f"{field_name} invalide : doit correspondre à ^[A-Za-z0-9_-]{{3,128}}$."
        )
    return stripped


def validate_model(model: Optional[str]) -> Optional[str]:
    """Valide un sélecteur de modèle agentapi optionnel (None si vide)."""
    if model is None:
        return None
    if not isinstance(model, str):
        raise ValidationError("model doit être une chaîne de caractères.")
    if _CONTROL_CHARS_STRICT_RE.search(model):
        raise ValidationError("model contient des caractères de contrôle interdits.")
    stripped = model.strip()
    if not stripped:
        return None
    if not MODEL_RE.match(stripped):
        raise ValidationError(
            "model invalide : doit correspondre à ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$."
        )
    assert_no_secrets(stripped, field_name="model")
    return stripped


def validate_int_range(
    value: Optional[int],
    *,
    name: str,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    """Valide un entier dans [min_value, max_value] en rejetant les booléens."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} doit être un entier (reçu {type(value).__name__}).")
    if value < min_value or value > max_value:
        raise ValidationError(
            f"{name} doit être compris entre {min_value} et {max_value} (reçu {value})."
        )
    return value


def validate_wait_seconds(wait_seconds: Optional[int]) -> int:
    """Valide wait_seconds dans [0, 30]."""
    return validate_int_range(
        wait_seconds,
        name="wait_seconds",
        default=0,
        min_value=0,
        max_value=MAX_WAIT_SECONDS,
    )


def get_bridge_depth(env: Optional[Mapping[str, str]] = None) -> int:
    """Lit GRAVITY_BRIDGE_DEPTH en fail-closed (non entier ou négatif interdit)."""
    source = os.environ if env is None else env
    raw = source.get("GRAVITY_BRIDGE_DEPTH")
    if raw is None:
        return 0
    stripped = str(raw).strip()
    if not stripped:
        return 0
    try:
        depth = int(stripped)
    except ValueError as exc:
        raise DispatchBlockedError(
            "Variable GRAVITY_BRIDGE_DEPTH invalide (non entière) : dispatch refusé (fail-closed)."
        ) from exc
    if depth < 0:
        raise DispatchBlockedError(
            "Variable GRAVITY_BRIDGE_DEPTH négative : dispatch refusé (fail-closed)."
        )
    return depth


def assert_dispatch_allowed(
    env: Optional[Mapping[str, str]] = None,
    *,
    caller_conversation_id: Optional[str] = None,
    known_target_conversation_ids: Optional[Iterable[str]] = None,
) -> int:
    """Vérifie la garde anti-boucle (GRAVITY_BRIDGE_DEPTH et caller_conversation_id)."""
    depth = get_bridge_depth(env)
    if depth >= 1:
        raise DispatchBlockedError(
            f"Sous-dispatch interdit à GRAVITY_BRIDGE_DEPTH={depth} (anti-boucle fail-closed). "
            "Utilisez reply_thread sans dispatch pour répondre."
        )

    valid_caller = validate_conversation_id(
        caller_conversation_id, field_name="caller_conversation_id"
    )
    if valid_caller is not None and known_target_conversation_ids is not None:
        known_folded = {
            str(cid).strip().casefold()
            for cid in known_target_conversation_ids
            if isinstance(cid, str) and cid.strip()
        }
        if valid_caller.casefold() in known_folded:
            raise DispatchBlockedError(
                "Sous-dispatch interdit depuis une conversation ouverte par Gravity Bridge "
                f"(caller_conversation_id={valid_caller!r}). Utilisez reply_thread(..., dispatch=false)."
            )
    return depth


def assert_dispatch_approved(approved: bool) -> None:
    """Exige le feu vert humain unique (approved=True) avant tout dispatch=True."""
    if not bool(approved):
        raise DispatchBlockedError(
            "Feu vert humain unique requis (approved=True) avant tout dispatch=true."
        )
