"""Ninja API instances.

Two instances, two trust boundaries:

- ``ext_api``: browser-extension import endpoints, authenticated with the
  ``X-Extension-Token`` pairing credential. CSRF is inapplicable: these
  requests carry no cookies, only the secret header.
- ``app_api``: mother-app endpoints, authenticated with the local-operator
  session cookie. django-ninja marks all its views CSRF-exempt at the Django
  middleware level, so ``OperatorSessionAuth`` enforces CSRF itself via
  ``ninja.utils.check_csrf`` (the same pattern as ninja's APIKeyCookie):
  unsafe methods require the ``X-CSRFToken`` header.
"""

from ninja import NinjaAPI

import audits.api as audits_api
import evidence.api as evidence_api
import listings.api as listings_api
import pairing.api as pairing_api

from . import API_VERSION

ext_api = NinjaAPI(version=API_VERSION, urls_namespace="vcc-extension")
app_api = NinjaAPI(version=API_VERSION, urls_namespace="vcc-app")

ext_api.add_router("", listings_api.ext_router)
ext_api.add_router("", evidence_api.ext_router)

app_api.add_router("", pairing_api.setup_router)
app_api.add_router("", pairing_api.pairing_router)
app_api.add_router("", listings_api.app_router)
app_api.add_router("", evidence_api.app_router)
app_api.add_router("", audits_api.app_router)
