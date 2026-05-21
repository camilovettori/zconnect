from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from ..config import settings


ZOHO_TOKEN_URL = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_INVOICE_API_PREFIX = "/invoice/v3"
ZOHO_ITEMS_PATH = "/items"
TOKEN_REFRESH_BUFFER_SECONDS = 60

logger = logging.getLogger(__name__)


class ZohoServiceError(Exception):
    pass


@dataclass
class _TokenCacheEntry:
    access_token: str
    expires_at: float


_TOKEN_CACHE: Dict[str, _TokenCacheEntry] = {}
_TOKEN_LOCKS: Dict[str, asyncio.Lock] = {}


class ZohoService:
    def __init__(self, base_url: str, client_id: str, client_secret: str, refresh_token: str, org_id: str):
        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url.endswith(ZOHO_INVOICE_API_PREFIX):
            normalized_base_url = normalized_base_url[: -len(ZOHO_INVOICE_API_PREFIX)]

        self.base_url = normalized_base_url
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.org_id = org_id.strip()
        self._tax_cache: Dict[float, Optional[str]] = {}

    def _cache_key(self) -> str:
        return "|".join([self.base_url, self.client_id, self.refresh_token, self.org_id])

    def _lock(self) -> asyncio.Lock:
        key = self._cache_key()
        if key not in _TOKEN_LOCKS:
            _TOKEN_LOCKS[key] = asyncio.Lock()
        return _TOKEN_LOCKS[key]

    def _cached_token(self) -> Optional[str]:
        entry = _TOKEN_CACHE.get(self._cache_key())
        if not entry:
            return None
        if time.time() >= entry.expires_at:
            return None
        return entry.access_token

    def _endpoint(self, path: str) -> str:
        return f"{self.base_url}{ZOHO_INVOICE_API_PREFIX}/{path.lstrip('/')}"

    async def _refresh_access_token(self) -> str:
        if not self.client_id:
            raise ZohoServiceError("Zoho client ID is missing")
        if not self.client_secret:
            raise ZohoServiceError("Zoho client secret is missing")
        if not self.refresh_token:
            raise ZohoServiceError("Zoho refresh token is missing")

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(ZOHO_TOKEN_URL, data=payload)

        if resp.status_code not in (200, 201):
            raise ZohoServiceError(f"Zoho token refresh failed {resp.status_code}: {resp.text}")

        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise ZohoServiceError("Zoho token refresh returned no access_token")

        expires_in = data.get("expires_in_sec", data.get("expires_in", 3600))
        try:
            expires_in_seconds = int(expires_in)
        except (TypeError, ValueError):
            expires_in_seconds = 3600

        _TOKEN_CACHE[self._cache_key()] = _TokenCacheEntry(
            access_token=access_token,
            expires_at=time.time() + max(0, expires_in_seconds - TOKEN_REFRESH_BUFFER_SECONDS),
        )
        return access_token

    async def get_access_token(self, force_refresh: bool = False) -> str:
        cached = None if force_refresh else self._cached_token()
        if cached:
            return cached

        async with self._lock():
            if not force_refresh:
                cached = self._cached_token()
                if cached:
                    return cached
            return await self._refresh_access_token()

    async def _headers(self, force_refresh: bool = False) -> Dict[str, str]:
        access_token = await self.get_access_token(force_refresh=force_refresh)
        if not self.org_id:
            raise ZohoServiceError("Zoho organization ID is missing")

        return {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "X-com-zoho-invoice-organizationid": self.org_id,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        force_refresh: bool = False,
    ) -> httpx.Response:
        headers = await self._headers(force_refresh=force_refresh)
        url = self._endpoint(path)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, params=params, json=json)

        if resp.status_code == 401 and not force_refresh:
            headers = await self._headers(force_refresh=True)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(method, url, headers=headers, params=params, json=json)

        return resp

    async def test_connection(self) -> bool:
        resp = await self._request("GET", "/contacts", params={"per_page": 1}, force_refresh=False)
        if resp.status_code in (200, 201):
            return True
        raise ZohoServiceError(f"Zoho test connection failed {resp.status_code}: {resp.text}")

    async def find_contact_by_name(self, name: str) -> Optional[str]:
        resp = await self._request("GET", "/contacts", params={"contact_name": name, "per_page": 20})
        if resp.status_code != 200:
            raise ZohoServiceError(f"Zoho query contact failed {resp.status_code}: {resp.text}")

        data = resp.json().get("contacts", [])
        for c in data:
            if c.get("contact_name") == name:
                return c.get("contact_id")
        return None

    def _extract_contact_record(self, payload: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        contact = payload.get("contact")
        if isinstance(contact, dict):
            return contact
        contacts = payload.get("contacts")
        if isinstance(contacts, list) and len(contacts) == 1 and isinstance(contacts[0], dict):
            return contacts[0]
        return payload

    def _extract_contact_name(self, payload: Any) -> Optional[str]:
        contact = self._extract_contact_record(payload)
        if not contact:
            return None
        for key in ("contact_name", "display_name", "displayName", "name"):
            value = contact.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    async def get_contact_by_id(self, contact_id: str) -> Optional[Dict[str, Any]]:
        contact_key = (contact_id or "").strip()
        if not contact_key:
            return None

        resp = await self._request("GET", f"/contacts/{contact_key}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ZohoServiceError(f"Zoho get contact failed {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ZohoServiceError(f"Zoho get contact returned invalid JSON: {resp.text}") from exc
        return self._extract_contact_record(data)

    async def update_contact_name(self, contact_id: str, name: str) -> bool:
        contact_key = (contact_id or "").strip()
        search_name = (name or "").strip()
        if not contact_key or not search_name:
            return False

        resp = await self._request("PUT", f"/contacts/{contact_key}", json={"contact_name": search_name})
        if resp.status_code in (200, 201):
            return True
        if resp.status_code == 404:
            return False
        raise ZohoServiceError(f"Zoho update contact failed {resp.status_code}: {resp.text}")

    def _extract_item_id(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        candidates = []
        item = payload.get("item")
        if isinstance(item, dict):
            candidates.extend([item.get("item_id"), item.get("id")])
        candidates.extend([payload.get("item_id"), payload.get("id")])
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        return None

    def _extract_items_payload(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            items = payload.get("items")
            return items if isinstance(items, list) else []
        if isinstance(payload, list):
            return payload
        return []

    async def find_item_by_name(self, name: str) -> Optional[str]:
        search_name = (name or "").strip()
        if not search_name:
            return None

        resp = await self._request(
            "GET",
            ZOHO_ITEMS_PATH,
            params={
                "name": search_name,
                "filter_by": "Status.All",
                "per_page": 200,
            },
        )
        if resp.status_code != 200:
            raise ZohoServiceError(f"Zoho item lookup failed {resp.status_code}: {resp.text}")

        data = resp.json()
        items = self._extract_items_payload(data)
        normalized_target = " ".join(search_name.lower().split())
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_name = str(item.get("name") or "").strip()
            if not candidate_name:
                continue
            if " ".join(candidate_name.lower().split()) == normalized_target:
                item_id = item.get("item_id") or item.get("id")
                if item_id is not None and str(item_id).strip():
                    return str(item_id).strip()
        return None

    async def find_item_by_sku(self, sku: str) -> Optional[str]:
        return await self.find_item_by_name(sku)

    async def create_item(self, name: str, rate: float, tax_id: str) -> str:
        payload: Dict[str, Any] = {
            "name": name,
            "rate": rate,
            "tax_id": tax_id,
        }
        resp = await self._request("POST", ZOHO_ITEMS_PATH, json=payload)
        if resp.status_code not in (200, 201):
            raise ZohoServiceError(f"Zoho create item failed {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ZohoServiceError(f"Zoho create item returned invalid JSON: {resp.text}") from exc

        item_id = self._extract_item_id(data)
        if not item_id:
            raise ZohoServiceError(f"Zoho create item returned no item_id: {data}")
        return item_id

    async def create_contact(self, name: str) -> str:
        payload = {"contact_name": name}
        resp = await self._request("POST", "/contacts", json=payload)
        if resp.status_code not in (200, 201):
            raise ZohoServiceError(f"Zoho create contact failed {resp.status_code}: {resp.text}")
        contact_id = resp.json().get("contact", {}).get("contact_id")
        if not contact_id:
            raise ZohoServiceError("Zoho create contact returned no contact_id")
        return contact_id

    async def fetch_taxes(self) -> List[Dict[str, Any]]:
        resp = await self._request("GET", "/settings/taxes", params={"per_page": 200})
        if resp.status_code != 200:
            raise ZohoServiceError(f"Zoho taxes lookup failed {resp.status_code}: {resp.text}")
        data = resp.json()
        if not isinstance(data, dict):
            return []
        taxes = data.get("taxes", [])
        return taxes if isinstance(taxes, list) else []

    async def create_draft_invoice_from_payload(self, payload: Dict[str, Any]) -> str:
        request_payload = dict(payload)
        request_payload.pop("status", None)

        line_items = request_payload.get("line_items") or []
        if not isinstance(line_items, list):
            raise ZohoServiceError(
                json.dumps(
                    {
                        "error": "Zoho API failed",
                        "details": "line_items must be a list",
                    }
                )
            )

        for line in line_items:
            if not isinstance(line, dict) or not line.get("tax_id"):
                raise ZohoServiceError(
                    json.dumps(
                        {
                            "error": "Zoho API failed",
                            "details": f"Missing tax_id for line: {line}",
                        }
                    )
                )

        logger.info(
            {
                "action": "create_invoice",
                "customer_id": request_payload.get("customer_id"),
                "line_items": line_items,
            }
        )

        try:
            resp = await self._request("POST", "/invoices", json=request_payload)
        except Exception as exc:
            logger.exception(
                {
                    "zoho_payload": request_payload,
                    "status_code": None,
                    "response_text": None,
                }
            )
            raise ZohoServiceError(json.dumps({"error": "Zoho API failed", "details": str(exc)})) from exc

        if resp.status_code not in (200, 201):
            logger.error({"zoho_payload": request_payload, "status_code": resp.status_code, "response_text": resp.text})
            raise ZohoServiceError(
                json.dumps({"error": "Zoho API failed", "details": resp.text})
            )

        try:
            data = resp.json()
        except ValueError as exc:
            logger.error({"zoho_payload": request_payload, "status_code": resp.status_code, "response_text": resp.text})
            raise ZohoServiceError(
                json.dumps({"error": "Zoho API failed", "details": resp.text})
            ) from exc

        invoice = data.get("invoice") if isinstance(data, dict) else None
        candidates: List[Any] = []
        if isinstance(invoice, dict):
            candidates.extend([invoice.get("invoice_id"), invoice.get("id"), invoice.get("invoiceId")])
        if isinstance(data, dict):
            candidates.extend([data.get("invoice_id"), data.get("id")])

        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()

        raise ZohoServiceError(
            json.dumps(
                {
                    "error": "Zoho API failed",
                    "details": f"Zoho create invoice returned no invoice identifier: {data}",
                }
            )
        )

    async def create_draft_invoice(self, contact_id: str, lines: List[Dict[str, Any]], reference_number: str) -> str:
        payload = {
            "customer_id": contact_id,
            "reference_number": reference_number,
            "line_items": lines,
            "notes": "",
            "status": "draft",
            "is_inclusive_tax": False,
        }
        return await self.create_draft_invoice_from_payload(payload)

    async def get_tax_by_rate(self, rate: float) -> Optional[str]:
        normalized_target = round(float(rate), 2)
        if normalized_target in self._tax_cache:
            return self._tax_cache[normalized_target]

        taxes = await self.fetch_taxes()
        for tax in taxes:
            try:
                percentage = round(float(tax.get("tax_percentage", tax.get("tax_percentage", tax.get("percentage", 0)))), 2)
            except (TypeError, ValueError, AttributeError):
                continue
            if percentage == normalized_target:
                tax_id = tax.get("tax_id") or tax.get("id")
                if tax_id:
                    resolved = str(tax_id)
                    self._tax_cache[normalized_target] = resolved
                    return resolved
        self._tax_cache[normalized_target] = None
        return None


def make_zoho_service() -> ZohoService:
    return ZohoService(
        base_url=settings.ZOHO_BASE_URL,
        client_id=settings.ZOHO_CLIENT_ID,
        client_secret=settings.ZOHO_CLIENT_SECRET,
        refresh_token=settings.ZOHO_REFRESH_TOKEN,
        org_id=settings.ZOHO_ORG_ID,
    )
