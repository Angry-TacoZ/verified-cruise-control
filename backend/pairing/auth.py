"""Ninja auth backends for the two API trust boundaries."""

from __future__ import annotations

from ninja.errors import HttpError
from ninja.security.base import AuthBase
from ninja.utils import check_csrf

from . import services


class ExtensionTokenAuth(AuthBase):
    """``X-Extension-Token`` header, checked against stored verifiers."""

    openapi_type = "apiKey"
    openapi_in = "header"
    openapi_name = "X-Extension-Token"

    def __call__(self, request):
        token = request.headers.get("X-Extension-Token", "")
        return services.verify_extension_token(token)


class OperatorSessionAuth(AuthBase):
    """Local-operator session flag + CSRF enforcement.

    Never accepts the extension token. Because django-ninja views are CSRF
    exempt at the middleware level, the check runs here for every
    session-authenticated request; Django's CSRF machinery only rejects
    unsafe methods (POST/PUT/PATCH/DELETE), so safe reads pass through.
    """

    openapi_type = "apiKey"
    openapi_in = "cookie"
    openapi_name = "sessionid"

    def __call__(self, request):
        session = getattr(request, "session", None)
        if session is None or session.get(services.SESSION_KEY) is not True:
            return None
        if check_csrf(request) is not None:
            raise HttpError(403, "CSRF check failed")
        return True


extension_auth = ExtensionTokenAuth()
operator_auth = OperatorSessionAuth()
