"""Pairing + operator-bootstrap services. Plaintext tokens exist only as
function return values at issuance time; storage holds SHA-256 verifiers."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from django.db import transaction
from django.utils import timezone

from .models import ExtensionPairing, LocalOperator

SESSION_KEY = "vcc_operator"


def _verifier(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits of entropy


def get_operator() -> LocalOperator:
    return LocalOperator.objects.get_or_create(pk=1)[0]


def ensure_setup_token(*, regenerate: bool = False) -> str | None:
    """Return a usable single-use setup token, generating one if needed.

    Before initialization this bootstraps the operator; afterwards
    ``regenerate=True`` issues a re-entry token (e.g. expired session or a
    second browser) without de-initializing. Returns None when a token is
    already outstanding (it cannot be recovered from its verifier) or when
    issuance was not explicitly requested after initialization.
    """
    operator = get_operator()
    if operator.initialized and not regenerate:
        return None
    if operator.setup_verifier and not regenerate:
        return None
    token = _new_token()
    operator.setup_verifier = _verifier(token)
    operator.save(update_fields=["setup_verifier", "updated_at"])
    return token


def verify_setup_token(candidate: str) -> bool:
    operator = get_operator()
    if not operator.setup_verifier or not candidate:
        return False
    return hmac.compare_digest(operator.setup_verifier, _verifier(candidate))


def create_pairing(
    *, label: str = "", rotated_from: ExtensionPairing | None = None
) -> tuple[ExtensionPairing, str]:
    token = _new_token()
    pairing = ExtensionPairing.objects.create(
        label=label, token_verifier=_verifier(token), rotated_from=rotated_from
    )
    return pairing, token


@transaction.atomic
def initialize_operator(
    setup_token: str,
) -> tuple[ExtensionPairing | None, str | None, bool] | None:
    """Consume a setup token and open an operator session.

    Returns (pairing, plaintext, fresh). Fresh bootstrap also issues the
    first pairing token; re-entry (already initialized) opens the session
    without minting a pairing. Tokens are single-use in both cases.
    """
    if not verify_setup_token(setup_token):
        return None
    operator = LocalOperator.objects.select_for_update().get(pk=1)
    if operator.setup_verifier is None or not verify_setup_token(setup_token):
        return None
    operator.setup_verifier = None
    if not operator.initialized:
        operator.initialized = True
        operator.save(update_fields=["initialized", "setup_verifier", "updated_at"])
        pairing, token = create_pairing(label="initial pairing")
        return pairing, token, True
    operator.save(update_fields=["setup_verifier", "updated_at"])
    return None, None, False


def verify_extension_token(candidate: str) -> ExtensionPairing | None:
    if not candidate:
        return None
    digest = _verifier(candidate)
    for pairing in ExtensionPairing.objects.filter(revoked_at__isnull=True):
        if hmac.compare_digest(pairing.token_verifier, digest):
            ExtensionPairing.objects.filter(pk=pairing.pk).update(
                last_used_at=timezone.now()
            )
            return pairing
    return None


@transaction.atomic
def rotate_pairings(*, label: str = "") -> tuple[ExtensionPairing, str]:
    """Revoke every active pairing and issue exactly one replacement."""
    now = timezone.now()
    active = list(ExtensionPairing.objects.filter(revoked_at__isnull=True))
    ExtensionPairing.objects.filter(revoked_at__isnull=True).update(revoked_at=now)
    rotated_from = active[0] if len(active) == 1 else None
    return create_pairing(label=label, rotated_from=rotated_from)


def revoke_pairing(pairing_id) -> bool:
    updated = ExtensionPairing.objects.filter(
        pk=pairing_id, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
    return updated > 0
