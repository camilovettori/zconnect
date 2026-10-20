"""Test isolation guards.

Loaded by pytest before any test module, so these run before `app.config` / `app.db`
are imported:

* DATABASE_URL is forced to a throwaway SQLite file. Environment variables take
  precedence over `.env` in pydantic-settings, so the real (production) database
  configured in `.env` is never contacted, and `app.main`'s `create_all()` only
  touches the temporary file.
* Real network I/O through httpx is disabled. Tests that need HTTP must replace
  `httpx.AsyncClient` with a fake (as the existing tests do), so no test can ever
  reach a real Zoho organisation or Unify account.
"""

import os
import tempfile
from pathlib import Path

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="zconnect-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_DB_DIR / 'test.db').as_posix()}"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import settings  # noqa: E402

if not settings.normalized_database_url.startswith("sqlite:///"):
    raise RuntimeError("Refusing to run tests against a non-SQLite database")


class _NetworkDisabled(RuntimeError):
    pass


async def _blocked_async_transport(self, request):  # pragma: no cover - guard
    raise _NetworkDisabled(f"Network access is disabled in tests: {request.method} {request.url}")


def _blocked_sync_transport(self, request):  # pragma: no cover - guard
    raise _NetworkDisabled(f"Network access is disabled in tests: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def _disable_real_network(monkeypatch):
    # Block only the real socket transports; in-process transports such as FastAPI's
    # TestClient keep working.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async_transport)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_sync_transport)
    yield

