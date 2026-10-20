"""Read-only verification of the Zoho Invoice list-invoices lookup used for duplicate checks.

Answers, for ONE explicitly confirmed organisation:
  * Is the access token allowed to list invoices (ZohoInvoice.invoices.READ)?
  * Is the reference_number filter applied (an unknown reference returns nothing)?
  * Is it an exact match, or prefix/contains? Is it case-sensitive?
  * Is page_context.has_more_page returned?
  * Are void invoices included with filter_by=Status.All?
  * Do multiple active invoices share a reference (ambiguity)?
  * Does a known invoice's customer match the expected customer?

Safety:
  * GET requests only, enforced by the transport (any other method raises before sending).
  * Only https://*.zohoapis.* hosts. No OAuth refresh: an access token is read from the
    ZOHO_ACCESS_TOKEN environment variable and never printed.
  * --org-id must be repeated in --confirm-org-id.
  * Output: counts and booleans only; references are shown as short hashes; no invoice
    numbers, customer names or amounts. Nothing is written to disk.

Usage (operator runs it; nothing in Zconnect calls it):
  set ZOHO_ACCESS_TOKEN=...   (short-lived token with ZohoInvoice.invoices.READ)
  python tools/zoho_readonly_check.py --org-id 2006... --confirm-org-id 2006... \
      --reference <existing invoice reference> [--customer-id <its contact id>] \
      [--void-reference <reference of a voided invoice>] [--duplicate-reference <ref>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

API_PREFIX = "/invoice/v3"
ALLOWED_HOST_SUFFIXES = (".zohoapis.eu", ".zohoapis.com", ".zohoapis.in", ".zohoapis.com.au", ".zohoapis.jp", ".zohoapis.ca", ".zohoapis.sa")


class ReadOnlyViolation(RuntimeError):
    pass


class GetOnlyTransport(httpx.BaseTransport):
    """Wraps a transport and refuses anything except GET to an allowed Zoho API host."""

    def __init__(self, inner: httpx.BaseTransport):
        self.inner = inner
        self.methods_sent: List[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise ReadOnlyViolation(f"Refusing {request.method}: this tool only sends GET requests")
        host = request.url.host or ""
        if request.url.scheme != "https" or not any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES):
            raise ReadOnlyViolation(f"Refusing request to non-Zoho API host {host!r}")
        self.methods_sent.append(request.method)
        return self.inner.handle_request(request)


def ref_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _list_invoices(client: httpx.Client, org_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    response = client.get(f"{API_PREFIX}/invoices", params={**params, "organization_id": org_id})
    outcome: Dict[str, Any] = {"http_status": response.status_code}
    if response.status_code != 200:
        outcome["error"] = "access denied (check ZohoInvoice.invoices.READ scope)" if response.status_code in (401, 403) else "request failed"
        return outcome
    try:
        data = response.json()
    except ValueError:
        outcome["error"] = "invalid JSON"
        return outcome
    invoices = data.get("invoices") if isinstance(data, dict) else None
    page_context = data.get("page_context") if isinstance(data, dict) else None
    outcome["invoices"] = invoices if isinstance(invoices, list) else None
    outcome["page_context_present"] = isinstance(page_context, dict) and "has_more_page" in page_context
    outcome["has_more_page"] = bool(page_context.get("has_more_page")) if isinstance(page_context, dict) else None
    return outcome


def _refs(invoices: Optional[List[Dict[str, Any]]]) -> List[str]:
    return [str(inv.get("reference_number") or "").strip() for inv in (invoices or []) if isinstance(inv, dict)]


def _summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Strip invoice data: keep only status codes, counts and pagination flags."""
    return {
        "http_status": result.get("http_status"),
        "error": result.get("error"),
        "count": None if result.get("invoices") is None else len(result["invoices"]),
        "page_context_present": result.get("page_context_present"),
        "has_more_page": result.get("has_more_page"),
    }


def run_checks(
    client: httpx.Client,
    org_id: str,
    reference: str,
    customer_id: Optional[str] = None,
    void_reference: Optional[str] = None,
    duplicate_reference: Optional[str] = None,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {"organization_id": org_id, "reference_hash": ref_hash(reference)}

    orgs = client.get(f"{API_PREFIX}/organizations")
    org_names = {}
    if orgs.status_code == 200:
        try:
            org_names = {str(o.get("organization_id")): str(o.get("name") or "") for o in orgs.json().get("organizations", [])}
        except (ValueError, AttributeError):
            org_names = {}
    report["organization_found_for_token"] = org_id in org_names
    report["organization_name"] = org_names.get(org_id)  # shown so the operator can confirm the target

    unknown = _list_invoices(client, org_id, {"reference_number": f"zconnect-check-{uuid.uuid4().hex}", "filter_by": "Status.All", "per_page": 200})
    report["unknown_reference"] = _summary(unknown)
    report["filter_applied"] = unknown.get("invoices") == []

    exact = _list_invoices(client, org_id, {"reference_number": reference, "filter_by": "Status.All", "per_page": 200})
    exact_refs = _refs(exact.get("invoices"))
    report["known_reference"] = _summary(exact)
    report["known_reference_exact_matches"] = sum(1 for r in exact_refs if r == reference)
    report["known_reference_other_results"] = sum(1 for r in exact_refs if r != reference)

    if len(reference) > 2:
        prefix = _list_invoices(client, org_id, {"reference_number": reference[:-1], "filter_by": "Status.All", "per_page": 200})
        report["prefix_query"] = _summary(prefix)
        report["filter_semantics"] = "prefix_or_contains" if reference in _refs(prefix.get("invoices")) else "exact_or_word"
    swapped = reference.swapcase()
    if swapped != reference:
        case = _list_invoices(client, org_id, {"reference_number": swapped, "filter_by": "Status.All", "per_page": 200})
        report["case_swapped_query"] = _summary(case)
        report["case_insensitive"] = any(r.casefold() == reference.casefold() for r in _refs(case.get("invoices")))

    if customer_id is not None:
        matches = [inv for inv in (exact.get("invoices") or []) if str(inv.get("reference_number") or "").strip() == reference]
        report["customer_id_matches"] = [str(inv.get("customer_id") or "") == customer_id for inv in matches]

    if void_reference:
        void_all = _list_invoices(client, org_id, {"reference_number": void_reference, "filter_by": "Status.All", "per_page": 200})
        statuses = [str(inv.get("status") or "").lower() for inv in (void_all.get("invoices") or []) if str(inv.get("reference_number") or "").strip() == void_reference]
        report["void_reference_hash"] = ref_hash(void_reference)
        report["void_included_in_status_all"] = "void" in statuses
        report["void_reference_statuses"] = sorted(set(statuses))

    if duplicate_reference:
        dup = _list_invoices(client, org_id, {"reference_number": duplicate_reference, "filter_by": "Status.All", "per_page": 200})
        active = [inv for inv in (dup.get("invoices") or []) if str(inv.get("reference_number") or "").strip() == duplicate_reference and str(inv.get("status") or "").lower() != "void"]
        report["duplicate_reference_hash"] = ref_hash(duplicate_reference)
        report["duplicate_reference_active_matches"] = len(active)

    page = _list_invoices(client, org_id, {"filter_by": "Status.All", "per_page": 1, "page": 1})
    report["pagination_probe"] = _summary(page)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Zoho Invoice lookup verification (GET only).")
    parser.add_argument("--base-url", default="https://www.zohoapis.eu")
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--confirm-org-id", required=True, help="Repeat the organisation id to confirm the target")
    parser.add_argument("--reference", required=True, help="Reference number of an existing invoice in that organisation")
    parser.add_argument("--customer-id")
    parser.add_argument("--void-reference")
    parser.add_argument("--duplicate-reference")
    args = parser.parse_args(argv)

    if args.org_id.strip() != args.confirm_org_id.strip():
        print("Organisation ids do not match; nothing was sent.", file=sys.stderr)
        return 2
    token = os.environ.get("ZOHO_ACCESS_TOKEN", "").strip()
    if not token:
        print("Set ZOHO_ACCESS_TOKEN (read scope); nothing was sent.", file=sys.stderr)
        return 2
    base = urlparse(args.base_url)
    if base.scheme != "https" or not any((base.hostname or "").endswith(s) for s in ALLOWED_HOST_SUFFIXES):
        print("Base URL must be an https Zoho API host; nothing was sent.", file=sys.stderr)
        return 2

    transport = GetOnlyTransport(httpx.HTTPTransport())
    headers = {"Authorization": f"Zoho-oauthtoken {token}", "X-com-zoho-invoice-organizationid": args.org_id.strip()}
    with httpx.Client(base_url=args.base_url, transport=transport, headers=headers, timeout=20) as client:
        report = run_checks(client, args.org_id.strip(), args.reference.strip(), args.customer_id, args.void_reference, args.duplicate_reference)
    report["requests_sent"] = {"total": len(transport.methods_sent), "methods": sorted(set(transport.methods_sent))}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
