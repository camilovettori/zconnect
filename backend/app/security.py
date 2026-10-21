"""Server-to-server authentication between the Next.js server and this API.

The browser never talks to this API directly. The Next.js server validates the user's
Supabase session and forwards each request with a short-lived, HMAC-SHA256 signed identity:

    X-Zconnect-User       verified Supabase user id
    X-Zconnect-Role       "admin" | "user" | "platform_admin" (Supabase app_metadata; only
                          settable server-side). platform_admin is an admin who may also read
                          other companies' analytics.
    X-Zconnect-Company    the user's company id (Supabase app_metadata.company_id)
    X-Zconnect-Timestamp       unix seconds
    X-Zconnect-Content-SHA256  hex SHA-256 of the exact request body bytes (empty body included)
    X-Zconnect-Signature       hex HMAC-SHA256(secret,
                               "v3\\nMETHOD\\nPATH\\nQUERY\\nTIMESTAMP\\nUSER\\nROLE\\nCOMPANY\\nBODY_SHA256")

The signature binds the identity to the method, path, query and body of this request, so it
cannot be replayed against another endpoint or with another payload, and it expires after
MAX_CLOCK_SKEW_SECONDS. The backend hashes the raw bytes it actually received.
Without a configured secret every protected request is rejected (fail closed).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from typing import List

from fastapi import Depends, HTTPException, Request, status

from .config import settings

logger = logging.getLogger(__name__)

SIGNATURE_VERSION = "v3"
MAX_CLOCK_SKEW_SECONDS = 120
MIN_SECRET_LENGTH = 32
ROLES = ("admin", "user", "platform_admin")
ADMIN_ROLES = ("admin", "platform_admin")
COMPANY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

USER_HEADER = "x-zconnect-user"
ROLE_HEADER = "x-zconnect-role"
COMPANY_HEADER = "x-zconnect-company"
TIMESTAMP_HEADER = "x-zconnect-timestamp"
CONTENT_SHA256_HEADER = "x-zconnect-content-sha256"
SIGNATURE_HEADER = "x-zconnect-signature"


@dataclass(frozen=True)
class Identity:
    user_id: str
    role: str
    company_id: str = "default"

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "platform_admin"


def configured_secrets() -> List[bytes]:
    """Current secret plus an optional previous one (zero-downtime rotation).

    Single source of truth: the application settings (environment variable, or .env for
    local development), loaded at startup like every other setting. Values shorter than
    MIN_SECRET_LENGTH characters are ignored, so a weak secret behaves like a missing one.
    """
    values = [settings.ZCONNECT_BACKEND_SECRET, settings.ZCONNECT_BACKEND_SECRET_PREVIOUS]
    return [value.strip().encode("utf-8") for value in values if value and len(value.strip()) >= MIN_SECRET_LENGTH]


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def compute_signature(
    secret: bytes,
    method: str,
    path: str,
    query: str,
    timestamp: str,
    user_id: str,
    role: str,
    company_id: str,
    content_sha256: str,
) -> str:
    message = "\n".join([SIGNATURE_VERSION, method.upper(), path, query, timestamp, user_id, role, company_id, content_sha256])
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _unauthorized(reason: str) -> HTTPException:
    # Same public message for every authentication failure; the reason is logged only.
    logger.warning("Rejected unauthenticated API request: %s", reason)
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


async def require_identity(request: Request) -> Identity:
    secrets = configured_secrets()
    if not secrets:
        logger.critical("ZCONNECT_BACKEND_SECRET is not configured; rejecting API request")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Server security configuration is missing")

    user_id = (request.headers.get(USER_HEADER) or "").strip()
    role = (request.headers.get(ROLE_HEADER) or "").strip()
    company_id = (request.headers.get(COMPANY_HEADER) or "").strip()
    timestamp = (request.headers.get(TIMESTAMP_HEADER) or "").strip()
    signature = (request.headers.get(SIGNATURE_HEADER) or "").strip().lower()
    declared_digest = (request.headers.get(CONTENT_SHA256_HEADER) or "").strip().lower()
    if not (user_id and role and company_id and timestamp and signature and declared_digest):
        raise _unauthorized("missing identity headers")
    if role not in ROLES or len(user_id) > 128 or not COMPANY_ID_PATTERN.match(company_id):
        raise _unauthorized("invalid identity")
    try:
        issued_at = int(timestamp)
    except ValueError:
        raise _unauthorized("invalid timestamp")
    if abs(time.time() - issued_at) > MAX_CLOCK_SKEW_SECONDS:
        raise _unauthorized("expired or future timestamp")

    # Hash the raw bytes actually received (Starlette caches them for the endpoint).
    # The body itself is never logged.
    received_digest = body_sha256(await request.body())
    if not hmac.compare_digest(received_digest, declared_digest):
        raise _unauthorized("body digest mismatch")

    path = request.scope.get("path", "")
    raw_query = request.scope.get("query_string", b"").decode("latin-1")
    query = f"?{raw_query}" if raw_query else ""
    valid = any(
        hmac.compare_digest(
            compute_signature(secret, request.method, path, query, timestamp, user_id, role, company_id, received_digest),
            signature,
        )
        for secret in secrets
    )
    if not valid:
        raise _unauthorized("bad signature")

    identity = Identity(user_id=user_id, role=role, company_id=company_id)
    request.state.identity = identity
    return identity


async def require_admin(identity: Identity = Depends(require_identity)) -> Identity:
    if not identity.is_admin:
        logger.warning("Rejected admin-only API request from user %s (role %s)", identity.user_id, identity.role)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin permission required")
    return identity
