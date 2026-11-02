"""Offline benchmark of Zoho duplicate lookup only. No credentials, DB or writes.

Run from backend/: python benchmarks/duplicate_lookup.py
"""
import asyncio
import time
from types import SimpleNamespace

from app.services.zoho_service import ZohoService

PAGE_SIZE = 200
INVOICES = [
    {"invoice_id": str(i), "reference_number": f"O-{i}", "status": "draft"}
    for i in range(450)
]
DELAY_SECONDS = 0.01


def fake_service():
    service = object.__new__(ZohoService)
    calls = {"count": 0}

    async def fake_request(method, path, **kwargs):
        assert method == "GET" and path == "/invoices"
        params = kwargs["params"]
        assert params["filter_by"] == "Status.All"
        page = params["page"]
        calls["count"] += 1
        await asyncio.sleep(DELAY_SECONDS)
        start = (page - 1) * PAGE_SIZE
        subset = INVOICES[start:start + PAGE_SIZE]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "invoices": subset,
                "page_context": {"has_more_page": start + PAGE_SIZE < len(INVOICES)},
            },
        )

    service._request = fake_request
    return service, calls


async def run_case(count):
    order_ids = [f"O-{i}" for i in range(count)]

    old_service, old_calls = fake_service()
    started = time.perf_counter()
    old_matches = [
        await old_service.find_invoices_by_reference_number(order_id)
        for order_id in order_ids
    ]
    old_seconds = time.perf_counter() - started

    new_service, new_calls = fake_service()
    started = time.perf_counter()
    invoices = await new_service.list_all_invoices_for_duplicate_check()
    index = {}
    for invoice in invoices:
        key = str(invoice.get("reference_number") or "").strip().casefold()
        if key:
            index.setdefault(key, []).append(invoice)
    new_matches = [index.get(order_id.strip().casefold(), []) for order_id in order_ids]
    new_seconds = time.perf_counter() - started

    assert old_matches == new_matches, "Duplicate-check results differ"
    expected_pages = (len(INVOICES) + PAGE_SIZE - 1) // PAGE_SIZE
    assert old_calls["count"] == count * expected_pages
    assert new_calls["count"] == expected_pages
    print(
        f"{count:>3} orders | old: {old_seconds:.3f}s / {old_calls['count']} calls"
        f" | batch: {new_seconds:.3f}s / {new_calls['count']} calls"
        f" | speedup: {old_seconds / new_seconds:.1f}x"
    )


async def main():
    print("OFFLINE duplicate-lookup benchmark; no Zoho API or invoice creation")
    print(f"450 simulated invoices, 3 pages, {DELAY_SECONDS * 1000:.0f} ms per API call")
    for count in (10, 50, 100):
        await run_case(count)


if __name__ == "__main__":
    asyncio.run(main())
