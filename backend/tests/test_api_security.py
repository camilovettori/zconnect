"""Authentication and authorization of the FastAPI backend (app/security.py).

Real dependency chain (no auth overrides) against an isolated SQLite database. Requests are
signed the way the Next.js server signs them; anything else must be rejected.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.services.export_service import list_export_events

ADMIN_ONLY = {
    ("POST", "/api/settings"),
    ("POST", "/api/settings/unify/test"),
    ("POST", "/api/settings/zoho/test"),
    ("GET", "/api/unify/debug-samples"),
    ("POST", "/api/export/reset-sync-state"),
    ("POST", "/api/export/reset-order/{order_id}"),
    ("POST", "/api/export/reset-selected-runs"),
    ("POST", "/api/export/reconcile-order/{order_id}"),
}


def api_routes():
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            for method in route.methods:
                out.append((method, route.path))
    return sorted(out)


def concrete(path: str) -> str:
    return path.replace("{order_id}", "O-1").replace("{id}", "1")


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def seed_exported(db, order_id="O-1"):
    run = models.SyncRun(date_from="2026-10-01", date_to="2026-10-01", status="success")
    db.add(run)
    db.commit()
    db.add(models.ExportedOrder(sync_run_id=run.id, unify_order_id=order_id, customer_name="C", status="exported", message="Invoice X created"))
    db.commit()
    return run.id


# Readable by any signed-in user, always scoped to the signed company (tests/test_analytics.py).
COMPANY_SCOPED = {
    ("GET", "/api/analytics/summary"),
    ("GET", "/api/analytics/filters"),
    ("GET", "/api/analytics/companies"),
}


def test_every_api_route_is_covered_by_this_suite():
    """Fails if a new route appears, so its protection level is decided deliberately."""
    assert len(api_routes()) == 19
    assert ADMIN_ONLY <= set(api_routes())
    assert COMPANY_SCOPED <= set(api_routes()) and not (COMPANY_SCOPED & ADMIN_ONLY)


@pytest.mark.parametrize("method, path", api_routes())
def test_direct_access_without_credentials_is_rejected(client, method, path):
    response = client.request(method, concrete(path), json={})
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


@pytest.mark.parametrize("method, path", api_routes())
def test_missing_server_secret_fails_closed(client, monkeypatch, sign_request, method, path):
    headers = sign_request(method, concrete(path))  # even a correctly signed request
    monkeypatch.setattr(settings, "ZCONNECT_BACKEND_SECRET", "")
    response = client.request(method, concrete(path), json={}, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "Server security configuration is missing"}


def test_short_secret_is_treated_as_missing(client, monkeypatch, sign_request):
    monkeypatch.setattr(settings, "ZCONNECT_BACKEND_SECRET", "too-short")
    response = client.get("/api/export/history", headers=sign_request("GET", "/api/export/history", secret="too-short"))
    assert response.status_code == 503


def test_valid_signed_request_is_accepted(client, sign_request):
    response = client.get("/api/export/history", headers=sign_request("GET", "/api/export/history", role="user"))
    assert response.status_code == 200
    assert response.json() == []


def test_protected_read_endpoints_require_identity(client, sign_request, db):
    seed_exported(db)
    for path in ("/api/export/history", "/api/export/history/1", "/api/settings"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=sign_request("GET", path, role="user")).status_code == 200


@pytest.mark.parametrize(
    "tamper",
    ["wrong_secret", "role_escalation", "other_user", "other_path", "other_method", "expired", "future", "garbage_signature", "bad_timestamp", "unknown_role"],
)
def test_tampered_or_replayed_credentials_are_rejected(client, sign_request, tamper):
    path = "/api/export/history"
    headers = sign_request("GET", path, user_id="user-1", role="user")
    if tamper == "wrong_secret":
        headers = sign_request("GET", path, user_id="user-1", role="user", secret="another-secret-" + "x" * 40)
    elif tamper == "role_escalation":
        headers["X-Zconnect-Role"] = "admin"
    elif tamper == "other_user":
        headers["X-Zconnect-User"] = "user-2"
    elif tamper == "other_path":
        headers = sign_request("GET", "/api/settings", user_id="user-1", role="user")
    elif tamper == "other_method":
        headers = sign_request("POST", path, user_id="user-1", role="user")
    elif tamper == "expired":
        headers = sign_request("GET", path, user_id="user-1", role="user", timestamp=int(time.time()) - 600)
    elif tamper == "future":
        headers = sign_request("GET", path, user_id="user-1", role="user", timestamp=int(time.time()) + 600)
    elif tamper == "garbage_signature":
        headers["X-Zconnect-Signature"] = "0" * 64
    elif tamper == "bad_timestamp":
        headers["X-Zconnect-Timestamp"] = "yesterday"
    elif tamper == "unknown_role":
        headers = sign_request("GET", path, user_id="user-1", role="superuser")
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_query_string_is_part_of_the_signature(client, sign_request):
    signed_for = sign_request("GET", "/api/unify/debug-samples", query="?date_from=2026-01-01&date_to=2026-01-02")
    response = client.get("/api/unify/debug-samples?date_from=2026-01-01&date_to=2026-12-31", headers=signed_for)
    assert response.status_code == 401


def test_previous_secret_is_accepted_during_rotation(client, monkeypatch, sign_request):
    old = "zconnect-old-secret-" + "abcdef0123456789" * 3
    new = "zconnect-new-secret-" + "9876543210fedcba" * 3
    monkeypatch.setattr(settings, "ZCONNECT_BACKEND_SECRET", new)
    monkeypatch.setattr(settings, "ZCONNECT_BACKEND_SECRET_PREVIOUS", old)
    for secret in (new, old):
        assert client.get("/api/export/history", headers=sign_request("GET", "/api/export/history", secret=secret)).status_code == 200
    monkeypatch.setattr(settings, "ZCONNECT_BACKEND_SECRET_PREVIOUS", "")
    assert client.get("/api/export/history", headers=sign_request("GET", "/api/export/history", secret=old)).status_code == 401


@pytest.mark.parametrize("method, path", sorted(ADMIN_ONLY))
def test_user_role_cannot_perform_sensitive_operations(client, sign_request, db, method, path):
    seed_exported(db)
    url = concrete(path)
    body = json.dumps({"run_ids": [1]}).encode()
    headers = {**sign_request(method, url, user_id="user-1", role="user", body=body), "Content-Type": "application/json"}
    response = client.request(method, url, content=body, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin permission required"}
    assert db.query(models.ExportedOrder).one().status == "exported"  # nothing changed


def test_unauthorized_reset_and_reconcile_change_nothing_and_admin_can(client, sign_request, db):
    seed_exported(db)
    for path in ("/api/export/reset-order/O-1", "/api/export/reconcile-order/O-1"):
        assert client.post(path).status_code == 401
        assert client.post(path, headers=sign_request("POST", path, role="user")).status_code == 403
    assert db.query(models.ExportedOrder).one().status == "exported"

    ok = client.post("/api/export/reset-order/O-1", headers=sign_request("POST", "/api/export/reset-order/O-1", user_id="admin-1", role="admin"))
    assert ok.status_code == 200
    db.expire_all()
    assert db.query(models.ExportedOrder).one().status == "reset"
    event = [e for e in list_export_events(db, "default", "O-1") if e.event_type == "reset"][0]
    assert event.actor_id == "admin-1"


def test_spoofed_actor_header_cannot_impersonate(client, sign_request, db):
    seed_exported(db)
    headers = sign_request("POST", "/api/export/reset-order/O-1", user_id="admin-1", role="admin")
    headers["X-Zconnect-Actor"] = "someone-else"
    assert client.post("/api/export/reset-order/O-1", headers=headers).status_code == 200
    actors = {e.actor_id for e in list_export_events(db, "default", "O-1")}
    assert actors == {"admin-1"}


def test_user_role_can_read_and_run_non_sensitive_operations(client, sign_request):
    path = "/api/export/run-csv"
    body = json.dumps({"orders": []}).encode()
    headers = {**sign_request("POST", path, role="user", body=body), "Content-Type": "application/json"}
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 400  # reached the handler (validation), not blocked by auth


def test_api_docs_are_not_published_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_health_root_stays_public_and_reveals_nothing_sensitive(client):
    response = client.get("/")
    assert response.status_code == 200
    assert set(response.json()) == {"message"}


# ---------------------------------------------------------------------------
# Request body integrity (SHA-256 of the exact bytes is part of the signature)
# ---------------------------------------------------------------------------


def _json_request(sign_request, method, path, payload, **kwargs):
    body = json.dumps(payload).encode()
    headers = {**sign_request(method, path, body=body, **kwargs), "Content-Type": "application/json"}
    return body, headers


def test_valid_json_body_is_accepted(client, sign_request, db):
    seed_exported(db)
    path = "/api/export/reset-selected-runs"
    body, headers = _json_request(sign_request, "POST", path, {"run_ids": [1]})
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["reset_exported_orders"] == 1


def test_valid_empty_body_is_accepted(client, sign_request, db):
    seed_exported(db)
    path = "/api/export/reset-order/O-1"
    assert client.post(path, headers=sign_request("POST", path, body=b"")).status_code == 200


def test_altered_body_is_rejected(client, sign_request, db):
    seed_exported(db)
    path = "/api/export/reset-selected-runs"
    _, headers = _json_request(sign_request, "POST", path, {"run_ids": [999]})
    tampered = json.dumps({"run_ids": [1]}).encode()  # same headers, different payload
    response = client.post(path, content=tampered, headers=headers)
    assert response.status_code == 401
    assert db.query(models.ExportedOrder).one().status == "exported"


def test_body_added_to_a_request_signed_as_empty_is_rejected(client, sign_request, db):
    seed_exported(db)
    path = "/api/export/reset-order/O-1"
    headers = sign_request("POST", path, body=b"")
    response = client.post(path, content=b'{"injected": true}', headers={**headers, "Content-Type": "application/json"})
    assert response.status_code == 401


def test_missing_body_digest_header_is_rejected(client, sign_request):
    path = "/api/export/history"
    headers = sign_request("GET", path)
    headers.pop("X-Zconnect-Content-SHA256")
    assert client.get(path, headers=headers).status_code == 401


def test_body_digest_header_that_does_not_match_the_body_is_rejected(client, sign_request):
    path = "/api/export/run-csv"
    body, headers = _json_request(sign_request, "POST", path, {"orders": []}, role="user")
    headers["X-Zconnect-Content-SHA256"] = "0" * 64
    assert client.post(path, content=body, headers=headers).status_code == 401


def test_signature_from_another_payload_cannot_be_replayed(client, sign_request, db):
    seed_exported(db)
    path = "/api/export/reset-selected-runs"
    first_body, first_headers = _json_request(sign_request, "POST", path, {"run_ids": [2]})
    assert client.post(path, content=first_body, headers=first_headers).status_code == 404  # run 2 does not exist
    replay = json.dumps({"run_ids": [1]}).encode()
    first_headers["X-Zconnect-Content-SHA256"] = __import__("hashlib").sha256(replay).hexdigest()
    assert client.post(path, content=replay, headers=first_headers).status_code == 401


def test_multipart_body_digest_covers_the_exact_bytes(client, sign_request):
    boundary = "zconnectboundary"
    crlf = "\r\n"
    body = crlf.join(
        [
            f"--{boundary}",
            'Content-Disposition: form-data; name="file"; filename="o.csv"',
            "Content-Type: text/csv",
            "",
            "order_id,customer_name",
            f"--{boundary}--",
            "",
        ]
    ).encode()
    path = "/api/unify/preview-csv"
    headers = {**sign_request("POST", path, role="user", body=body), "Content-Type": f"multipart/form-data; boundary={boundary}"}
    accepted = client.post(path, content=body, headers=headers)
    assert accepted.status_code != 401  # authenticated; the handler processed the upload
    altered = body.replace(b"order_id", b"order_ix")
    assert client.post(path, content=altered, headers=headers).status_code == 401


def test_rejection_logs_never_contain_body_or_secret(client, sign_request, caplog):
    from tests.conftest import TEST_BACKEND_SECRET

    path = "/api/export/run-csv"
    body, headers = _json_request(sign_request, "POST", path, {"orders": [{"customer_name": "Very Secret Customer"}]}, role="user")
    caplog.set_level("DEBUG")
    client.post(path, content=body + b" ", headers=headers)
    logged = " | ".join(record.getMessage() for record in caplog.records)
    assert "Very Secret Customer" not in logged
    assert TEST_BACKEND_SECRET not in logged
