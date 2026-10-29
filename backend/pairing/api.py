"""Setup, health, and pairing-management endpoints (mother-app boundary)."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from django.middleware.csrf import get_token
from ninja import Router, Schema
from ninja.errors import HttpError

from config import API_VERSION, SERVICE_NAME

from . import services
from .auth import operator_auth
from .models import ExtensionPairing

setup_router = Router(auth=None)
pairing_router = Router(auth=operator_auth)


class HealthOut(Schema):
    status: str
    service: str
    api_version: str


class SetupStatusOut(Schema):
    initialized: bool


class InitializeIn(Schema):
    setup_token: str


class TokenOut(Schema):
    pairing_id: uuid.UUID | None
    extension_token: str | None
    fresh: bool = True


class PairingOut(Schema):
    id: uuid.UUID
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    active: bool


@setup_router.get("/health", response=HealthOut, auth=None)
def health(request):
    return {"status": "ok", "service": SERVICE_NAME, "api_version": API_VERSION}


@setup_router.get("/csrf", auth=None)
def csrf_token(request: HttpRequest):
    # get_token() mints the secret and flags CSRF_COOKIE_NEEDS_UPDATE, so the
    # middleware sets the cookie on this response; the mother app (same
    # origin via the Vite proxy) reads the cookie and sends X-CSRFToken.
    return {"csrf_token": get_token(request)}


@setup_router.get("/setup/status", response=SetupStatusOut, auth=None)
def setup_status(request):
    return {"initialized": services.get_operator().initialized}


@setup_router.post("/setup/initialize", response=TokenOut, auth=None)
def setup_initialize(request, payload: InitializeIn):
    """Bootstrap or re-entry: consume a single-use setup token and open an
    operator session. Fresh bootstrap also issues the first extension
    pairing token (returned exactly once); re-entry returns nulls."""
    from ninja.utils import check_csrf

    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF check failed")
    result = services.initialize_operator(payload.setup_token)
    if result is None:
        raise HttpError(403, "invalid or expired setup token")
    pairing, token, fresh = result
    request.session.cycle_key()
    request.session[services.SESSION_KEY] = True
    return {
        "pairing_id": pairing.id if pairing else None,
        "extension_token": token,
        "fresh": fresh,
    }


@setup_router.post("/setup/logout", auth=operator_auth)
def setup_logout(request):
    request.session.flush()
    return {"ok": True}


def _serialize_pairing(pairing: ExtensionPairing) -> dict:
    return {
        "id": pairing.id,
        "label": pairing.label,
        "created_at": pairing.created_at,
        "last_used_at": pairing.last_used_at,
        "revoked_at": pairing.revoked_at,
        "active": pairing.is_active,
    }


@pairing_router.get("/pairing/status", response=list[PairingOut])
def pairing_status(request):
    return [_serialize_pairing(p) for p in ExtensionPairing.objects.all()]


class RotateIn(Schema):
    label: str = ""


@pairing_router.post("/pairing/rotate", response=TokenOut)
def pairing_rotate(request, payload: RotateIn):
    """Revoke all active pairings, issue one replacement (returned once)."""
    pairing, token = services.rotate_pairings(label=payload.label or "")
    return {"pairing_id": pairing.id, "extension_token": token, "fresh": False}


@pairing_router.post("/pairing/{pairing_id}/revoke")
def pairing_revoke(request, pairing_id: uuid.UUID):
    if not services.revoke_pairing(pairing_id):
        raise HttpError(404, "pairing not found or already revoked")
    return {"ok": True}
