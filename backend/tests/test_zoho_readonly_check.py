"""Tests for tools/zoho_readonly_check.py using an in-process mock Zoho (no network)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "zoho_readonly_check.py"
spec = importlib.util.spec_from_file_location("zoho_readonly_check", TOOL_PATH)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

ORG = "20069999"
SECRET_TOKEN = "1000.secret-token-value-should-never-appear"
INVOICES = [
    {"invoice_id": "4600000000001", "invoice_number": "INV-000101", "reference_number": "ORD-7781", "customer_id": "C-1", "customer_name": "Secret Bakery Ltd", "status": "draft", "total": 123.45},
    {"invoice_id": "4600000000002", "invoice_number": "INV-000102", "reference_number": "ORD-7781-B", "customer_id": "C-2", "customer_name": "Other Cafe", "status": "sent", "total": 10.0},
    {"invoice_id": "4600000000003", "invoice_number": "INV-000103", "reference_number": "ORD-VOID", "customer_id": "C-1", "customer_name": "Secret Bakery Ltd", "status": "void", "total": 5.0},
    {"invoice_id": "4600000000004", "invoice_number": "INV-000104", "reference_number": "ORD-DUP", "customer_id": "C-1", "customer_name": "Secret Bakery Ltd", "status": "draft", "total": 7.0},
    {"invoice_id": "4600000000005", "invoice_number": "INV-000105", "reference_number": "ORD-DUP", "customer_id": "C-3", "customer_name": "Third Shop", "status": "sent", "total": 8.0},
]


def mock_zoho(semantics: str, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(parse_qs(request.url.query.decode()))))
        if request.url.path.endswith("/organizations"):
            return httpx.Response(200, json={"organizations": [{"organization_id": ORG, "name": "Target Org"}, {"organization_id": "1", "name": "Other"}]})
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        ref = params.get("reference_number")
        if ref is None:
            items = INVOICES[: int(params.get("per_page", 200))]
            return httpx.Response(200, json={"invoices": items, "page_context": {"page": 1, "per_page": 1, "has_more_page": True}})
        if semantics == "exact":
            items = [inv for inv in INVOICES if inv["reference_number"] == ref]
        else:  # contains, case-insensitive
            items = [inv for inv in INVOICES if ref.lower() in inv["reference_number"].lower()]
        return httpx.Response(200, json={"invoices": items, "page_context": {"page": 1, "per_page": 200, "has_more_page": False}})

    return httpx.MockTransport(handler)


def make_client(semantics="exact", seen=None, base_url="https://www.zohoapis.eu"):
    seen = [] if seen is None else seen
    transport = tool.GetOnlyTransport(mock_zoho(semantics, seen))
    return httpx.Client(base_url=base_url, transport=transport), transport, seen


def test_exact_semantics_report():
    client, transport, seen = make_client("exact")
    with client:
        report = tool.run_checks(client, ORG, "ORD-7781", customer_id="C-1", void_reference="ORD-VOID", duplicate_reference="ORD-DUP")
    assert report["organization_found_for_token"] is True and report["organization_name"] == "Target Org"
    assert report["filter_applied"] is True
    assert report["known_reference_exact_matches"] == 1 and report["known_reference_other_results"] == 0
    assert report["filter_semantics"] == "exact_or_word"
    assert report["case_insensitive"] is False
    assert report["customer_id_matches"] == [True]
    assert report["void_included_in_status_all"] is True
    assert report["duplicate_reference_active_matches"] == 2
    assert report["pagination_probe"]["page_context_present"] is True and report["pagination_probe"]["has_more_page"] is True
    assert set(transport.methods_sent) == {"GET"}
    assert all(q.get("filter_by") == ["Status.All"] for m, p, q in seen if p.endswith("/invoices"))


def test_contains_semantics_are_detected():
    client, _, _ = make_client("contains")
    with client:
        report = tool.run_checks(client, ORG, "ORD-7781")
    assert report["filter_semantics"] == "prefix_or_contains"
    assert report["known_reference_other_results"] == 1  # ORD-7781-B also returned
    assert report["case_insensitive"] is True


def test_report_contains_no_invoice_data_or_token():
    client, _, _ = make_client("contains")
    with client:
        report = tool.run_checks(client, ORG, "ORD-7781", customer_id="C-1", void_reference="ORD-VOID", duplicate_reference="ORD-DUP")
    text = json.dumps(report)
    for sensitive in ("ORD-7781", "ORD-VOID", "ORD-DUP", "INV-0001", "Secret Bakery", "Other Cafe", "123.45", "4600000000001", SECRET_TOKEN):
        assert sensitive not in text


def test_non_get_requests_are_refused_before_sending():
    client, transport, seen = make_client()
    with client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(tool.ReadOnlyViolation):
                client.request(method, "/invoice/v3/invoices")
    assert seen == [] and transport.methods_sent == []


def test_non_zoho_hosts_are_refused():
    client, transport, seen = make_client(base_url="https://evil.example.com")
    with client:
        with pytest.raises(tool.ReadOnlyViolation):
            client.get("/invoice/v3/invoices")
    assert seen == []


@pytest.mark.parametrize(
    "argv, env, message",
    [
        (["--org-id", ORG, "--confirm-org-id", "123", "--reference", "R"], {"ZOHO_ACCESS_TOKEN": "t"}, "do not match"),
        (["--org-id", ORG, "--confirm-org-id", ORG, "--reference", "R"], {}, "ZOHO_ACCESS_TOKEN"),
        (["--org-id", ORG, "--confirm-org-id", ORG, "--reference", "R", "--base-url", "http://www.zohoapis.eu"], {"ZOHO_ACCESS_TOKEN": "t"}, "https Zoho API host"),
    ],
)
def test_main_refuses_unsafe_invocations_without_sending(monkeypatch, capsys, argv, env, message):
    monkeypatch.delenv("ZOHO_ACCESS_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def no_network(*args, **kwargs):
        raise AssertionError("no transport may be created")

    monkeypatch.setattr(tool.httpx, "HTTPTransport", no_network)
    assert tool.main(argv) == 2
    assert message in capsys.readouterr().err


def test_main_never_prints_the_token(monkeypatch, capsys):
    monkeypatch.setenv("ZOHO_ACCESS_TOKEN", SECRET_TOKEN)
    seen = []
    monkeypatch.setattr(tool.httpx, "HTTPTransport", lambda: mock_zoho("exact", seen))
    assert tool.main(["--org-id", ORG, "--confirm-org-id", ORG, "--reference", "ORD-7781"]) == 0
    out = capsys.readouterr()
    assert SECRET_TOKEN not in out.out and SECRET_TOKEN not in out.err
    report = json.loads(out.out)
    assert report["requests_sent"]["methods"] == ["GET"]
    assert all(method == "GET" for method, _, _ in seen)
