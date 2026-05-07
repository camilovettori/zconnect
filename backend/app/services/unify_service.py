from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from datetime import date as date_cls, datetime
from typing import Any, Dict, List, Optional

import httpx

from ..config import settings
from ..schemas import OrderLine, UnifyOrder, UnifyOrderPreview
from ..utils.money import normalize_unify_money


UNIFY_ORDERS_PATH = "/v1/orders"
UNIFY_ORDERS_PAGE_SIZE = 100
UNIFY_ORDERS_STATUSES = ("new", "confirmed", "received", "checked")
UNIFY_PREVIEW_ORDER_STATUSES = ("new", "confirmed", "received", "checked")
UNIFY_ORDER_ITEMS_SUFFIX = "/items"
UNIFY_BUYERS_PATH = "/v1/buyers"
UNIFY_BUYER_ORGANISATION_PATH = "/v1/buyers/{buyerId}/organisation"
UNIFY_PRODUCTS_PATH = "/v1/products"
UNIFY_ORDER_DETAIL_BATCH_SIZE = 8
UNIFY_TOKEN_URL = "https://oauth.unifyordering.com/oauth2/token"
TOKEN_REFRESH_BUFFER_SECONDS = 60
UNIFY_REQUEST_TIMEOUT_SECONDS = 90
BUYERS_CACHE_TTL_SECONDS = 1800
BUYER_ORGANISATION_CACHE_TTL_SECONDS = 1800
UNIFY_ITEMS_PAGE_SIZE = 100
UNIFY_PRODUCTS_PAGE_SIZE = 100
PREVIEW_MAX_ORDERS = 200
PREVIEW_MAX_DROPPED_DETAILS = 50
PREVIEW_MAX_DUPLICATE_DETAILS = 20

logger = logging.getLogger(__name__)


class UnifyServiceError(Exception):
    pass


@dataclass
class _TokenCacheEntry:
    access_token: str
    expires_at: float


_TOKEN_CACHE: Dict[str, _TokenCacheEntry] = {}
_TOKEN_LOCKS: Dict[str, asyncio.Lock] = {}


@dataclass
class _BuyerCacheEntry:
    buyers: Dict[str, str]
    expires_at: float


_BUYERS_CACHE: Dict[str, _BuyerCacheEntry] = {}
_BUYERS_LOCKS: Dict[str, asyncio.Lock] = {}


@dataclass
class _BuyerOrganisationCacheEntry:
    organisation: Dict[str, Any]
    expires_at: float


_BUYER_ORGANISATION_CACHE: Dict[str, _BuyerOrganisationCacheEntry] = {}
_BUYER_ORGANISATION_LOCKS: Dict[str, asyncio.Lock] = {}


@dataclass
class _ProductCacheEntry:
    products: Dict[str, str]
    expires_at: float


_PRODUCTS_CACHE: Dict[str, _ProductCacheEntry] = {}
_PRODUCTS_LOCKS: Dict[str, asyncio.Lock] = {}


@dataclass
class _ProductNameCacheEntry:
    product_name: str
    source: str
    expires_at: float


_PRODUCT_NAME_CACHE: Dict[str, _ProductNameCacheEntry] = {}
_PRODUCT_NAME_LOCKS: Dict[str, asyncio.Lock] = {}


class UnifyService:
    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.last_fetch_debug: Dict[str, Any] = {}
        self._sampled_endpoints: set[str] = set()

    def _cache_key(self) -> str:
        return "|".join([self.base_url, self.client_id, self.client_secret])

    def _lock(self) -> asyncio.Lock:
        key = self._cache_key()
        if key not in _TOKEN_LOCKS:
            _TOKEN_LOCKS[key] = asyncio.Lock()
        return _TOKEN_LOCKS[key]

    def _buyers_lock(self) -> asyncio.Lock:
        key = self._cache_key()
        if key not in _BUYERS_LOCKS:
            _BUYERS_LOCKS[key] = asyncio.Lock()
        return _BUYERS_LOCKS[key]

    def _buyer_organisation_lock(self) -> asyncio.Lock:
        key = self._cache_key()
        if key not in _BUYER_ORGANISATION_LOCKS:
            _BUYER_ORGANISATION_LOCKS[key] = asyncio.Lock()
        return _BUYER_ORGANISATION_LOCKS[key]

    def _products_lock(self) -> asyncio.Lock:
        key = self._cache_key()
        if key not in _PRODUCTS_LOCKS:
            _PRODUCTS_LOCKS[key] = asyncio.Lock()
        return _PRODUCTS_LOCKS[key]

    def _product_name_lock(self) -> asyncio.Lock:
        key = self._cache_key()
        if key not in _PRODUCT_NAME_LOCKS:
            _PRODUCT_NAME_LOCKS[key] = asyncio.Lock()
        return _PRODUCT_NAME_LOCKS[key]

    def _cached_token(self) -> Optional[str]:
        entry = _TOKEN_CACHE.get(self._cache_key())
        if not entry or time.time() >= entry.expires_at:
            return None
        return entry.access_token

    def _cached_buyers(self) -> Optional[Dict[str, str]]:
        entry = _BUYERS_CACHE.get(self._cache_key())
        if not entry or time.time() >= entry.expires_at:
            return None
        return dict(entry.buyers)

    def _store_buyers_cache(self, buyers: Dict[str, str]) -> None:
        _BUYERS_CACHE[self._cache_key()] = _BuyerCacheEntry(
            buyers=dict(buyers),
            expires_at=time.time() + BUYERS_CACHE_TTL_SECONDS,
        )

    def _cached_buyer_organisation(self, buyer_id: str) -> Optional[Dict[str, Any]]:
        entry = _BUYER_ORGANISATION_CACHE.get(f"{self._cache_key()}::{buyer_id}")
        if not entry or time.time() >= entry.expires_at:
            return None
        return dict(entry.organisation)

    def _store_buyer_organisation_cache(self, buyer_id: str, organisation: Dict[str, Any]) -> None:
        _BUYER_ORGANISATION_CACHE[f"{self._cache_key()}::{buyer_id}"] = _BuyerOrganisationCacheEntry(
            organisation=dict(organisation),
            expires_at=time.time() + BUYER_ORGANISATION_CACHE_TTL_SECONDS,
        )

    def _cached_products(self) -> Optional[Dict[str, str]]:
        entry = _PRODUCTS_CACHE.get(self._cache_key())
        if not entry or time.time() >= entry.expires_at:
            return None
        return dict(entry.products)

    def _store_products_cache(self, products: Dict[str, str]) -> None:
        _PRODUCTS_CACHE[self._cache_key()] = _ProductCacheEntry(
            products=dict(products),
            expires_at=time.time() + BUYERS_CACHE_TTL_SECONDS,
        )

    def _cached_product_name(self, product_identifier: str) -> Optional[str]:
        entry = _PRODUCT_NAME_CACHE.get(f"{self._cache_key()}::{product_identifier}")
        if not entry or time.time() >= entry.expires_at:
            return None
        return entry.product_name

    def _store_product_name_cache(self, product_identifiers: List[str], product_name: str, source: str) -> None:
        if not product_name:
            return
        now = time.time()
        for product_identifier in product_identifiers:
            if not product_identifier:
                continue
            _PRODUCT_NAME_CACHE[f"{self._cache_key()}::{product_identifier}"] = _ProductNameCacheEntry(
                product_name=product_name,
                source=source,
                expires_at=now + BUYERS_CACHE_TTL_SECONDS,
            )

    def _basic_auth_header(self) -> str:
        if not self.client_id:
            raise UnifyServiceError("Unify client ID is missing")
        if not self.client_secret:
            raise UnifyServiceError("Unify client secret is missing")

        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('ascii')}"

    async def _authenticate(self) -> str:
        payload = {"grant_type": "client_credentials"}
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            async with httpx.AsyncClient(timeout=min(30, UNIFY_REQUEST_TIMEOUT_SECONDS // 2)) as client:
                resp = await client.post(UNIFY_TOKEN_URL, data=payload, headers=headers)
        except Exception as exc:
            logger.exception("Unify authentication request failed")
            raise UnifyServiceError(f"Unify authentication request failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.error("Unify authentication failed with status %s: %s", resp.status_code, resp.text)
            raise UnifyServiceError(f"Unify authentication failed {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise UnifyServiceError(f"Unify authentication returned invalid JSON: {resp.text}") from exc

        access_token = (
            data.get("access_token")
            or data.get("accessToken")
            or data.get("token")
            or (data.get("data") or {}).get("access_token")
            or (data.get("data") or {}).get("accessToken")
        )
        if not access_token:
            raise UnifyServiceError("Unify authentication returned no access_token")

        expires_in = (
            data.get("expires_in")
            or data.get("expiresIn")
            or data.get("expires_in_sec")
            or (data.get("data") or {}).get("expires_in")
            or (data.get("data") or {}).get("expiresIn")
            or 3600
        )
        try:
            expires_in_seconds = int(expires_in)
        except (TypeError, ValueError):
            expires_in_seconds = 3600

        _TOKEN_CACHE[self._cache_key()] = _TokenCacheEntry(
            access_token=str(access_token),
            expires_at=time.time() + max(0, expires_in_seconds - TOKEN_REFRESH_BUFFER_SECONDS),
        )
        logger.info("Unify token acquired successfully; expires_in=%s", expires_in_seconds)
        return str(access_token)

    async def get_access_token(self, force_refresh: bool = False) -> str:
        cached = None if force_refresh else self._cached_token()
        if cached:
            return cached

        async with self._lock():
            if not force_refresh:
                cached = self._cached_token()
                if cached:
                    return cached
            return await self._authenticate()

    async def _headers(self, force_refresh: bool = False) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.get_access_token(force_refresh=force_refresh)}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = await self._headers(force_refresh=force_refresh)

        try:
            async with httpx.AsyncClient(timeout=UNIFY_REQUEST_TIMEOUT_SECONDS) as client:
                resp = await client.request(method, url, params=params, headers=headers)
        except Exception as exc:
            logger.exception("Unify request failed: %s %s", method, path)
            raise UnifyServiceError(f"Unify {method} {path} request failed: {exc}") from exc

        if resp.status_code == 401 and not force_refresh:
            logger.warning("Unify request returned 401 for %s %s; refreshing token and retrying", method, path)
            headers = await self._headers(force_refresh=True)
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.request(method, url, params=params, headers=headers)
            except Exception as exc:
                logger.exception("Unify retry request failed: %s %s", method, path)
                raise UnifyServiceError(f"Unify {method} {path} retry failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.error("Unify %s %s returned %s: %s", method, path, resp.status_code, resp.text)
            raise UnifyServiceError(f"Unify {method} {path} returned {resp.status_code}: {resp.text}")

        return resp

    def _parse_json_or_fail(self, resp: httpx.Response, context: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise UnifyServiceError(f"{context} returned invalid JSON: {resp.text}") from exc

    def _extract_orders_payload(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("orders", "data", "results", "orderItems", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise UnifyServiceError("Unify orders response was not a list or supported envelope")

    def _extract_products_payload(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if not payload:
                return []
            for key in ("products", "data", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise UnifyServiceError("Unify products response was not a list or supported envelope")

    def _extract_buyers_payload(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if not payload:
                return []
            for key in ("buyers", "buyer", "data", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise UnifyServiceError("Unify buyers response was not a list or supported envelope")

    def _extract_buyer_organisation_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            for key in ("organisation", "organization", "data", "result"):
                value = payload.get(key)
                if isinstance(value, dict):
                    return value
            return payload
        raise UnifyServiceError("Unify buyer organisation response was not an object")

    def _extract_items_payload(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if not payload:
                return []
            for key in ("items", "orderItems", "data", "results", "order_items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise UnifyServiceError("Unify order items response was not a list or supported envelope")

    def _extract_order_detail_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            for key in ("order", "data", "result"):
                value = payload.get(key)
                if isinstance(value, dict):
                    return value
            return payload
        raise UnifyServiceError("Unify order detail response was not a dict or supported envelope")

    def _response_shape(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, list):
            return {"type": "list", "length": len(payload)}
        if isinstance(payload, dict):
            shape = {"type": "dict", "keys": sorted(payload.keys())}
            next_token = payload.get("nextPageToken") or payload.get("next_page_token") or payload.get("nextToken")
            if next_token:
                shape["nextPageToken"] = True
            return shape
        return {"type": type(payload).__name__}

    def _safe_sample_value(self, value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            text = value.strip()
            if len(text) <= 80:
                return text
            return f"{text[:40]}...{text[-16:]}"
        if isinstance(value, list):
            if depth >= 2:
                return f"[list:{len(value)}]"
            return [self._safe_sample_value(item, depth + 1) for item in value[:2]]
        if isinstance(value, dict):
            if depth >= 2:
                return {key: "<redacted>" for key in list(value.keys())[:6]}
            sample: Dict[str, Any] = {}
            for key in list(value.keys())[:12]:
                sample[key] = self._safe_sample_value(value.get(key), depth + 1)
            return sample
        return str(value)

    def _log_endpoint_sample(self, endpoint: str, payload: Any) -> None:
        if not getattr(settings, "UNIFY_DEBUG_SAMPLES", False):
            return
        if endpoint in self._sampled_endpoints:
            return
        self._sampled_endpoints.add(endpoint)

        top_keys: List[str] = []
        item_keys: List[str] = []
        pagination_keys: List[str] = []
        if isinstance(payload, dict):
            top_keys = sorted(payload.keys())
            pagination_keys = [key for key in top_keys if "token" in key.lower() or "page" in key.lower()]
            sample_source = payload
            for key in ("orders", "items", "buyers", "products", "data", "results", "orderItems", "order_items"):
                candidate = payload.get(key)
                if isinstance(candidate, list) and candidate:
                    sample_source = candidate[0]
                    if isinstance(sample_source, dict):
                        item_keys = sorted(sample_source.keys())
                    break
        elif isinstance(payload, list):
            sample_source = payload[0] if payload else []
            if isinstance(sample_source, dict):
                item_keys = sorted(sample_source.keys())
        else:
            sample_source = payload

        logger.info(
            "Unify sample endpoint=%s top_keys=%s item_keys=%s pagination_keys=%s sample=%s",
            endpoint,
            top_keys,
            item_keys,
            pagination_keys,
            self._safe_sample_value(sample_source),
        )

    def _log_shape(self, label: str, payload: Any) -> None:
        if getattr(settings, "UNIFY_DEBUG_SHAPES", False):
            logger.info("Unify %s response shape: %s", label, self._response_shape(payload))

    def _log_money_debug(
        self,
        raw_order: Dict[str, Any],
        raw_item: Optional[Dict[str, Any]],
        normalized_order_total: float,
        normalized_item_rate: float,
    ) -> None:
        if not getattr(settings, "UNIFY_DEBUG_MONEY", False):
            return

        raw_order_total = self._extract_amount_value(raw_order.get("totalNetAmount") or raw_order.get("total_net_amount"))
        raw_item_total = 0.0
        if raw_item:
            raw_item_total = self._extract_amount_value(raw_item.get("totalNetAmount") or raw_item.get("total_net_amount"))

        logger.info(
            "Unify money debug order_id=%s raw_order_total=%s normalized_order_total=%s raw_item_total=%s normalized_item_rate=%s",
            self._extract_order_id(raw_order),
            raw_order_total,
            normalized_order_total,
            raw_item_total,
            normalized_item_rate,
        )

    def _unsupported_shape_error(self, label: str, payload: Any) -> UnifyServiceError:
        shape = self._response_shape(payload)
        logger.error("Unify %s response had unsupported shape: %s", label, shape)
        return UnifyServiceError(f"Unify {label} response had unsupported shape: {shape}")

    def _extract_order_id(self, raw: Dict[str, Any]) -> Optional[str]:
        value = raw.get("order_id") or raw.get("orderId") or raw.get("id")
        return str(value) if value is not None and str(value).strip() else None

    def _extract_status(self, raw: Dict[str, Any]) -> str:
        value = raw.get("status") or raw.get("state") or raw.get("order_status") or ""
        return str(value).strip().lower()

    def _is_preview_ready_status(self, status: str) -> bool:
        return status in {"confirmed", "received", "checked", "ready", "completed", "delivered"}

    def _parse_date_value(self, value: Any) -> Optional[date_cls]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        candidate = text.split("T", 1)[0].split(" ", 1)[0]
        try:
            return date_cls.fromisoformat(candidate)
        except ValueError:
            pass

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _parse_datetime_value(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass

        candidate = text.split("T", 1)[0].split(" ", 1)[0]
        try:
            parsed_date = date_cls.fromisoformat(candidate)
        except ValueError:
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day)

    def _extract_delivery_date(self, raw: Dict[str, Any]) -> Optional[str]:
        raw_value = self._extract_raw_delivery_date_value(raw)
        if raw_value is None:
            return None
        parsed = self._parse_date_value(raw_value)
        return parsed.isoformat() if parsed else None

    def _extract_raw_delivery_date_value(self, raw: Dict[str, Any]) -> Any:
        buyer = raw.get("buyer") or {}
        candidates = [
            raw.get("deliveryDate"),
            raw.get("delivery_date"),
            raw.get("deliveryAt"),
            raw.get("delivery_at"),
            buyer.get("deliveryDate"),
            buyer.get("delivery_date"),
        ]
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return candidate
        return None

    def _extract_raw_create_time_value(self, raw: Dict[str, Any]) -> Any:
        candidates = [
            raw.get("createTime"),
            raw.get("create_time"),
            raw.get("createdAt"),
            raw.get("created_at"),
            raw.get("createdOn"),
            raw.get("created_on"),
        ]
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return candidate
        return None

    def _extract_detail_delivery_date_value(self, raw: Dict[str, Any]) -> Any:
        candidates = [
            raw.get("deliveryDate"),
            raw.get("delivery_date"),
            raw.get("deliveryAt"),
            raw.get("delivery_at"),
        ]
        for candidate in candidates:
            if candidate is not None and str(candidate).strip():
                return candidate
        return None

    async def _resolve_delivery_date_for_preview(
        self,
        raw_order: Dict[str, Any],
        order_id: str,
        *,
        allow_detail_fallback: bool = True,
    ) -> tuple[Optional[str], Optional[str], bool]:
        raw_delivery_value = self._extract_raw_delivery_date_value(raw_order)
        parsed_raw_delivery = self._parse_date_value(raw_delivery_value) if raw_delivery_value else None
        if parsed_raw_delivery:
            return str(raw_delivery_value), parsed_raw_delivery.isoformat(), False

        if not allow_detail_fallback:
            return None, None, False

        detail_started_at = time.perf_counter()
        detail_payload = await self.fetch_order_detail(order_id)
        detail_delivery_value = self._extract_detail_delivery_date_value(detail_payload)
        detail_duration = time.perf_counter() - detail_started_at
        logger.info(
            "Unify deliveryDate fallback used order_id=%s raw_delivery_date=%s detail_delivery_date=%s detail_duration_seconds=%.3f",
            order_id,
            raw_delivery_value,
            detail_delivery_value,
            detail_duration,
        )
        parsed_detail_delivery = self._parse_date_value(detail_delivery_value) if detail_delivery_value else None
        return (
            None if detail_delivery_value is None else str(detail_delivery_value),
            parsed_detail_delivery.isoformat() if parsed_detail_delivery else None,
            True,
        )

    def _delivery_date_in_range(self, delivery_date: Optional[str], date_from: str, date_to: str) -> bool:
        parsed_date = self._parse_date_value(delivery_date)
        start_date = self._parse_date_value(date_from)
        end_date = self._parse_date_value(date_to)
        if not parsed_date or not start_date or not end_date:
            return False
        return start_date <= parsed_date <= end_date

    def _log_delivery_filter_debug(
        self,
        order_id: str,
        create_time: Any,
        raw_delivery_date: Any,
        delivery_date: Optional[str],
        parsed_delivery_date: Optional[date_cls],
        included: bool,
        reason: str,
    ) -> None:
        if not getattr(settings, "UNIFY_DEBUG_SHAPES", False) and not getattr(settings, "UNIFY_DEBUG_MONEY", False):
            return

        logger.info(
            "Unify delivery filter debug order_id=%s createTime=%s raw_deliveryDate=%s deliveryDate=%s parsed_delivery_date=%s included=%s reason=%s",
            order_id,
            create_time,
            raw_delivery_date,
            delivery_date,
            parsed_delivery_date.isoformat() if parsed_delivery_date else None,
            included,
            reason,
        )

    def _clean_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _is_meaningful_customer_label(self, value: Any) -> bool:
        text = self._clean_text(value)
        if not text:
            return False
        return any(char.isalpha() for char in text)

    def _first_meaningful_customer_label(self, *candidates: Any) -> Optional[str]:
        for candidate in candidates:
            text = self._clean_text(candidate)
            if text and self._is_meaningful_customer_label(text):
                return text
        return None

    def _extract_delivery_address_text(self, address: Any) -> Optional[str]:
        if isinstance(address, str):
            return self._clean_text(address)
        if not isinstance(address, dict):
            return None

        parts: List[str] = []
        for key in (
            "name",
            "companyName",
            "company_name",
            "address1",
            "address2",
            "address3",
            "street",
            "street1",
            "street2",
            "city",
            "state",
            "postalCode",
            "postcode",
            "country",
        ):
            text = self._clean_text(address.get(key))
            if text and text not in parts:
                parts.append(text)
        return ", ".join(parts) if parts else None

    def _extract_readable_buyer_name(self, payload: Dict[str, Any]) -> Optional[str]:
        buyer = payload.get("buyer") if isinstance(payload.get("buyer"), dict) else {}
        first_name = self._clean_text(payload.get("firstName") or payload.get("first_name") or buyer.get("firstName") or buyer.get("first_name"))
        last_name = self._clean_text(payload.get("lastName") or payload.get("last_name") or buyer.get("lastName") or buyer.get("last_name"))
        contact_full_name = " ".join(part for part in [first_name, last_name] if part).strip() or None
        candidates = [
            payload.get("name"),
            payload.get("displayName"),
            payload.get("display_name"),
            payload.get("companyName"),
            payload.get("company_name"),
            payload.get("businessName"),
            payload.get("business_name"),
            payload.get("organizationName"),
            payload.get("organization_name"),
            payload.get("fullName"),
            payload.get("full_name"),
            contact_full_name,
            payload.get("buyerDisplayName"),
            payload.get("buyer_display_name"),
            payload.get("customerDisplayName"),
            payload.get("customer_display_name"),
            payload.get("buyerName"),
            payload.get("buyer_name"),
            payload.get("customerName"),
            payload.get("customer_name"),
            payload.get("contactName"),
            payload.get("contact_name"),
            payload.get("additionalBuyerField1"),
            payload.get("additional_buyer_field1"),
            payload.get("additionalBuyerField2"),
            payload.get("additional_buyer_field2"),
            buyer.get("name"),
            buyer.get("displayName"),
            buyer.get("display_name"),
            buyer.get("companyName"),
            buyer.get("company_name"),
            buyer.get("businessName"),
            buyer.get("business_name"),
            buyer.get("organizationName"),
            buyer.get("organization_name"),
            buyer.get("fullName"),
            buyer.get("full_name"),
            " ".join(part for part in [self._clean_text(buyer.get("firstName") or buyer.get("first_name")), self._clean_text(buyer.get("lastName") or buyer.get("last_name"))] if part).strip() or None,
            buyer.get("buyerDisplayName"),
            buyer.get("buyer_display_name"),
            buyer.get("customerDisplayName"),
            buyer.get("customer_display_name"),
            buyer.get("additionalBuyerField1"),
            buyer.get("additional_buyer_field1"),
            buyer.get("additionalBuyerField2"),
            buyer.get("additional_buyer_field2"),
            buyer.get("contactName"),
            buyer.get("contact_name"),
        ]

        for candidate in candidates:
            text = self._clean_text(candidate)
            if text and self._is_meaningful_customer_label(text):
                return text
        return None

    def _extract_buyer_organisation_name(self, organisation: Dict[str, Any]) -> Optional[str]:
        candidates = [
            organisation.get("displayName"),
            organisation.get("display_name"),
            organisation.get("name"),
            organisation.get("companyName"),
            organisation.get("company_name"),
            organisation.get("businessName"),
            organisation.get("business_name"),
        ]
        for candidate in candidates:
            text = self._clean_text(candidate)
            if text:
                return text
        return None

    def _extract_buyer_organisation_address(self, organisation: Dict[str, Any]) -> Optional[str]:
        business_address = (
            organisation.get("businessAddress")
            or organisation.get("business_address")
            or organisation.get("address")
            or organisation.get("deliveryAddress")
            or organisation.get("delivery_address")
        )
        return self._extract_delivery_address_text(business_address)

    async def fetch_buyer_organisation(self, buyer_id: str) -> Dict[str, Any]:
        buyer_key = self._clean_text(buyer_id)
        if not buyer_key:
            raise UnifyServiceError("Buyer ID is missing")

        cached = self._cached_buyer_organisation(buyer_key)
        if cached is not None:
            logger.info("Unify reused cached buyer organisation buyer_id=%s", buyer_key)
            return cached

        async with self._buyer_organisation_lock():
            cached = self._cached_buyer_organisation(buyer_key)
            if cached is not None:
                logger.info("Unify reused cached buyer organisation buyer_id=%s", buyer_key)
                return cached

            path = UNIFY_BUYER_ORGANISATION_PATH.format(buyerId=buyer_key)
            logger.info("Unify fetching buyer organisation buyer_id=%s path=%s", buyer_key, path)
            resp = await self._request("GET", path)
            payload = self._parse_json_or_fail(resp, "Unify fetch buyer organisation")
            self._log_shape("buyer_organisation", payload)
            self._log_endpoint_sample("buyer_organisation", payload)
            organisation = self._extract_buyer_organisation_payload(payload)
            normalized = {
                "id": self._clean_text(organisation.get("id")) or buyer_key,
                "displayName": self._extract_buyer_organisation_name(organisation),
                "businessAddress": organisation.get("businessAddress") or organisation.get("business_address") or organisation.get("address"),
                "vatNumber": self._clean_text(organisation.get("vatNumber") or organisation.get("vat_number")),
            }
            self._store_buyer_organisation_cache(buyer_key, normalized)
            logger.info(
                "Unify fetched buyer organisation buyer_id=%s has_display_name=%s has_business_address=%s has_vat_number=%s",
                buyer_key,
                bool(normalized.get("displayName")),
                bool(normalized.get("businessAddress")),
                bool(normalized.get("vatNumber")),
            )
            return normalized

    def _resolve_buyer_details(
        self,
        raw: Dict[str, Any],
        buyer_id: Optional[str],
        buyer_name_map: Optional[Dict[str, str]] = None,
        buyer_organisation: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        buyer = raw.get("buyer") if isinstance(raw.get("buyer"), dict) else {}
        buyer_key = str(buyer_id).strip() if buyer_id is not None and str(buyer_id).strip() else ""
        organisation_name = None
        organisation_address = None
        if isinstance(buyer_organisation, dict):
            organisation_name = self._extract_buyer_organisation_name(buyer_organisation)
            organisation_address = self._extract_buyer_organisation_address(buyer_organisation)

        raw_delivery_address = self._extract_delivery_address_text(
            raw.get("deliveryAddress") or raw.get("delivery_address") or buyer.get("deliveryAddress") or buyer.get("delivery_address") or {}
        )
        delivery_address = organisation_address or raw_delivery_address

        buyer_name = self._extract_readable_buyer_name(raw)
        if not buyer_name:
            buyer_name = self._extract_customer_name(raw, buyer_key or None, buyer_name_map, include_address_fallback=False)
        if not buyer_name:
            buyer_name = organisation_name
        if not buyer_name and buyer_key:
            buyer_name = f"Customer {buyer_key}"

        customer_name = buyer_name
        if not customer_name:
            customer_name = self._extract_customer_name(raw, buyer_key or None, buyer_name_map, include_address_fallback=False)
        if not customer_name:
            customer_name = organisation_name
        if not customer_name and buyer_key:
            customer_name = f"Customer {buyer_key}"

        return buyer_name, customer_name, delivery_address

    def _extract_customer_name(
        self,
        raw: Dict[str, Any],
        buyer_id: Optional[str],
        buyer_name_map: Optional[Dict[str, str]] = None,
        *,
        include_address_fallback: bool = True,
    ) -> Optional[str]:
        buyer = raw.get("buyer") if isinstance(raw.get("buyer"), dict) else {}
        customer = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}
        buyer_key = str(buyer_id).strip() if buyer_id is not None and str(buyer_id).strip() else ""

        candidates = [
            raw.get("buyerName"),
            raw.get("buyer_name"),
            raw.get("customerName"),
            raw.get("customer_name"),
            raw.get("companyName"),
            raw.get("company_name"),
            raw.get("additionalBuyerField1"),
            raw.get("additional_buyer_field1"),
            raw.get("additionalBuyerField2"),
            raw.get("additional_buyer_field2"),
            buyer.get("name"),
            buyer.get("displayName"),
            buyer.get("display_name"),
            buyer.get("companyName"),
            buyer.get("company_name"),
            buyer.get("additionalBuyerField1"),
            buyer.get("additional_buyer_field1"),
            buyer.get("additionalBuyerField2"),
            buyer.get("additional_buyer_field2"),
            customer.get("name"),
            customer.get("displayName"),
            customer.get("display_name"),
            customer.get("companyName"),
            customer.get("company_name"),
            customer.get("businessName"),
            customer.get("business_name"),
            customer.get("organizationName"),
            customer.get("organization_name"),
        ]
        if include_address_fallback:
            candidates.append(self._extract_delivery_address_text(raw.get("deliveryAddress") or raw.get("delivery_address") or {}))

        for candidate in candidates:
            text = self._clean_text(candidate)
            if text and self._is_meaningful_customer_label(text):
                return text

        if buyer_name_map and buyer_key:
            mapped_name = buyer_name_map.get(buyer_key)
            cleaned_mapped_name = self._clean_text(mapped_name)
            if cleaned_mapped_name and self._is_meaningful_customer_label(cleaned_mapped_name):
                return cleaned_mapped_name

        return None

    def _extract_order_details(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            for key in ("order", "data", "result"):
                value = payload.get(key)
                if isinstance(value, dict):
                    return value
            return payload
        raise UnifyServiceError("Unify order details response was not an object")

    def _collect_product_mappings(self, payload: Any, mapping: Dict[str, str]) -> None:
        if isinstance(payload, list):
            for item in payload:
                self._collect_product_mappings(item, mapping)
            return
        if not isinstance(payload, dict):
            return

        display_name = None
        for candidate in (
            payload.get("displayName"),
            payload.get("display_name"),
            payload.get("name"),
            payload.get("productName"),
            payload.get("product_name"),
            payload.get("title"),
            payload.get("label"),
            payload.get("productDisplayName"),
            payload.get("product_display_name"),
        ):
            display_name = self._clean_text(candidate)
            if display_name:
                break

        candidate_ids: List[str] = []
        for candidate in (
            payload.get("productModificationId"),
            payload.get("productModificationID"),
            payload.get("product_modification_id"),
            payload.get("externalProductModificationId"),
            payload.get("externalProductModificationID"),
            payload.get("external_product_modification_id"),
            payload.get("productId"),
            payload.get("product_id"),
            payload.get("externalProductId"),
            payload.get("externalProductID"),
            payload.get("external_product_id"),
            payload.get("id"),
        ):
            text = self._clean_text(candidate)
            if text and text not in candidate_ids:
                candidate_ids.append(text)
        if display_name:
            for candidate_id in candidate_ids:
                mapping[candidate_id] = display_name

        for child_key in (
            "modifications",
            "productModifications",
            "product_modifications",
            "variants",
            "variant",
            "product",
            "products",
            "productModification",
            "product_modification",
            "productModificationDto",
            "items",
            "data",
            "results",
        ):
            child = payload.get(child_key)
            if isinstance(child, (list, dict)):
                self._collect_product_mappings(child, mapping)

    def _extract_product_name_from_payload(self, payload: Any) -> Optional[str]:
        if isinstance(payload, list):
            for item in payload:
                name = self._extract_product_name_from_payload(item)
                if name:
                    return name
            return None
        if not isinstance(payload, dict):
            return None

        for candidate in (
            payload.get("displayName"),
            payload.get("display_name"),
            payload.get("name"),
            payload.get("productName"),
            payload.get("product_name"),
            payload.get("title"),
            payload.get("label"),
            payload.get("productDisplayName"),
            payload.get("product_display_name"),
        ):
            name = self._clean_text(candidate)
            if name and self._is_meaningful_label(name):
                return name

        for child_key in (
            "product",
            "productModification",
            "productModifications",
            "modifications",
            "variants",
            "data",
            "results",
            "items",
        ):
            child = payload.get(child_key)
            if isinstance(child, (dict, list)):
                name = self._extract_product_name_from_payload(child)
                if name:
                    return name
        return None

    async def _fetch_product_name_from_path(self, path: str) -> Optional[str]:
        resp = await self._request("GET", path)
        if resp.status_code != 200:
            return None
        payload = self._parse_json_or_fail(resp, f"Unify fetch product detail {path}")
        self._log_shape(path, payload)
        self._log_endpoint_sample(path, payload)
        return self._extract_product_name_from_payload(payload)

    async def _resolve_product_name_by_identifier(
        self,
        *,
        identifier: str,
        order_id: Optional[str] = None,
        endpoint_log: Optional[List[str]] = None,
    ) -> Optional[str]:
        cached = self._cached_product_name(identifier)
        if cached:
            return cached

        async with self._product_name_lock():
            cached = self._cached_product_name(identifier)
            if cached:
                return cached

            attempts = [
                f"/v1/productModifications/{identifier}",
                f"/v1/products/{identifier}",
                f"/v1/products/{identifier}/modifications",
            ]

            for path in attempts:
                if endpoint_log is not None:
                    endpoint_log.append(path)
                try:
                    product_name = await self._fetch_product_name_from_path(path)
                except UnifyServiceError as exc:
                    logger.info(
                        "Unify product resolution endpoint failed order_id=%s identifier=%s path=%s error=%s",
                        order_id,
                        identifier,
                        path,
                        exc,
                    )
                    continue
                except Exception as exc:
                    logger.info(
                        "Unify product resolution endpoint errored order_id=%s identifier=%s path=%s error=%s",
                        order_id,
                        identifier,
                        path,
                        exc,
                    )
                    continue

                if product_name:
                    self._store_product_name_cache([identifier], product_name, path)
                    logger.info(
                        "Unify resolved product name order_id=%s identifier=%s path=%s product_name=%s",
                        order_id,
                        identifier,
                        path,
                        product_name,
                    )
                    return product_name

        return None

    def _extract_item_product_candidates(self, item: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        for _, candidate in self._extract_item_product_candidate_pairs(item):
            text = self._clean_text(candidate)
            if text and text not in candidates:
                candidates.append(text)
        return candidates

    def _extract_item_product_candidate_pairs(self, item: Dict[str, Any]) -> List[tuple[str, str]]:
        candidate_pairs: List[tuple[str, str]] = []
        for field_name, candidate in (
            ("productModificationId", item.get("productModificationId")),
            ("productModificationID", item.get("productModificationID")),
            ("product_modification_id", item.get("product_modification_id")),
            ("externalProductModificationId", item.get("externalProductModificationId")),
            ("externalProductModificationID", item.get("externalProductModificationID")),
            ("external_product_modification_id", item.get("external_product_modification_id")),
            ("productId", item.get("productId")),
            ("product_id", item.get("product_id")),
            ("externalProductId", item.get("externalProductId")),
            ("externalProductID", item.get("externalProductID")),
            ("external_product_id", item.get("external_product_id")),
            ("id", item.get("id")),
        ):
            text = self._clean_text(candidate)
            if text and all(existing_value != text for _, existing_value in candidate_pairs):
                candidate_pairs.append((field_name, text))
        return candidate_pairs

    def _extract_item_product_debug_fields(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "displayName": item.get("displayName"),
            "name": item.get("name"),
            "productModificationId": item.get("productModificationId"),
            "externalProductModificationId": item.get("externalProductModificationId"),
            "productId": item.get("productId"),
            "id": item.get("id"),
        }

    def _is_meaningful_label(self, value: Optional[str]) -> bool:
        if not value:
            return False
        text = value.strip()
        if not text:
            return False
        return not text.isdigit()

    async def _resolve_item_display_name(
        self,
        item: Dict[str, Any],
        product_map: Optional[Dict[str, str]],
        *,
        order_id: Optional[str] = None,
    ) -> tuple[str, bool, List[str]]:
        explicit_display_name = self._clean_text(item.get("displayName") or item.get("display_name") or item.get("name"))
        explicit_display_name = explicit_display_name if self._is_meaningful_label(explicit_display_name) else None
        candidate_pairs = self._extract_item_product_candidate_pairs(item)
        candidate_ids = [candidate_id for _, candidate_id in candidate_pairs]
        mapped_product_name = None
        matched_key = None
        if product_map:
            for candidate_id in candidate_ids:
                mapped_product_name = self._clean_text(product_map.get(candidate_id))
                if mapped_product_name:
                    matched_key = candidate_id
                    break

        endpoint_log: List[str] = []
        resolved_from_endpoint = None
        if not explicit_display_name and not mapped_product_name:
            for field_name, candidate_id in candidate_pairs:
                cached_name = self._cached_product_name(candidate_id)
                if cached_name:
                    resolved_from_endpoint = cached_name
                    matched_key = candidate_id
                    endpoint_log.append(f"cache:{field_name}")
                    break

            if not resolved_from_endpoint:
                for field_name, candidate_id in candidate_pairs:
                    endpoint_name = await self._resolve_product_name_by_identifier(
                        identifier=candidate_id,
                        order_id=order_id,
                        endpoint_log=endpoint_log,
                    )
                    if endpoint_name:
                        resolved_from_endpoint = endpoint_name
                        matched_key = candidate_id
                        break

        fallback_display_name = f"Product {candidate_ids[0]}" if candidate_ids else "Product unknown"
        display_name = explicit_display_name or mapped_product_name or resolved_from_endpoint or fallback_display_name
        unresolved = not explicit_display_name and not mapped_product_name and not resolved_from_endpoint
        if unresolved:
            logger.warning(
                "Unify unresolved product name order_id=%s item_debug_fields=%s item_keys=%s candidate_ids=%s product_match_found=%s matched_key=%s endpoints_checked=%s reason=%s",
                order_id,
                self._extract_item_product_debug_fields(item),
                sorted(item.keys()),
                candidate_ids,
                bool(mapped_product_name or resolved_from_endpoint),
                matched_key,
                endpoint_log or ["product_map_only"],
                "no_meaningful_display_name_and_no_detail_endpoint_match",
            )
        else:
            if resolved_from_endpoint:
                logger.info(
                    "Unify resolved item display name order_id=%s matched_key=%s product_name=%s endpoints_checked=%s",
                    order_id,
                    matched_key,
                    resolved_from_endpoint,
                    endpoint_log,
                )
        if display_name and candidate_ids:
            source_label = "explicit_name" if explicit_display_name else ("product_map" if mapped_product_name else "endpoint_resolution")
            self._store_product_name_cache(candidate_ids, str(display_name), source_label)
        return str(display_name or ""), unresolved, candidate_ids

    def _collect_buyer_mappings(self, payload: Any, mapping: Dict[str, str]) -> None:
        if isinstance(payload, list):
            for item in payload:
                self._collect_buyer_mappings(item, mapping)
            return
        if not isinstance(payload, dict):
            return

        candidate_id = (
            payload.get("buyerId")
            or payload.get("buyer_id")
            or payload.get("id")
            or payload.get("externalBuyerId")
            or payload.get("external_buyer_id")
        )
        display_name = self._extract_readable_buyer_name(payload)
        if candidate_id is not None and display_name:
            mapping[str(candidate_id)] = str(display_name)

        for child_key in ("buyer", "buyers", "data", "results", "items"):
            child = payload.get(child_key)
            if isinstance(child, (list, dict)):
                self._collect_buyer_mappings(child, mapping)

    async def fetch_buyers(self) -> Dict[str, str]:
        cached = self._cached_buyers()
        if cached is not None:
            logger.info("Unify reused %s cached buyers for name mapping", len(cached))
            return cached

        buyers: Dict[str, str] = {}
        page_token: Optional[str] = None
        seen_tokens: set[str] = set()

        async with self._buyers_lock():
            cached = self._cached_buyers()
            if cached is not None:
                logger.info("Unify reused %s cached buyers for name mapping", len(cached))
                return cached

            while True:
                params: Dict[str, Any] = {}
                if page_token:
                    params["nextPageToken"] = page_token

                resp = await self._request("GET", UNIFY_BUYERS_PATH, params=params or None)
                payload = self._parse_json_or_fail(resp, "Unify fetch buyers")
                self._log_shape("buyers", payload)
                self._log_endpoint_sample("buyers", payload)
                if isinstance(payload, dict):
                    buyers_payload = self._extract_buyers_payload(payload)
                    self._collect_buyer_mappings(buyers_payload, buyers)
                    self._collect_buyer_mappings(payload, buyers)
                    next_token = self._extract_next_page_token(payload)
                elif isinstance(payload, list):
                    buyers_payload = self._extract_buyers_payload(payload)
                    self._collect_buyer_mappings(buyers_payload, buyers)
                    next_token = None
                else:
                    raise self._unsupported_shape_error("buyers", payload)

                if not next_token:
                    break
                next_token = str(next_token)
                if next_token in seen_tokens:
                    logger.warning("Unify buyers pagination repeated nextPageToken=%s; stopping to avoid loop", next_token)
                    break
                seen_tokens.add(next_token)
                page_token = next_token

            self._store_buyers_cache(buyers)
            logger.info("Unify fetched %s buyers for name mapping", len(buyers))
            return buyers

    async def fetch_products(self) -> Dict[str, str]:
        cached = self._cached_products()
        if cached is not None:
            logger.info("Unify reused %s cached products for name mapping", len(cached))
            return cached

        products: Dict[str, str] = {}
        page_token: Optional[str] = None
        seen_tokens: set[str] = set()

        async with self._products_lock():
            cached = self._cached_products()
            if cached is not None:
                logger.info("Unify reused %s cached products for name mapping", len(cached))
                return cached

            while True:
                params: Dict[str, Any] = {}
                if page_token:
                    params["nextPageToken"] = page_token

                resp = await self._request("GET", UNIFY_PRODUCTS_PATH, params=params or None)
                payload = self._parse_json_or_fail(resp, "Unify fetch products")
                self._log_shape("products", payload)
                self._log_endpoint_sample("products", payload)
                if isinstance(payload, dict):
                    products_payload = self._extract_products_payload(payload)
                    self._collect_product_mappings(products_payload, products)
                    self._collect_product_mappings(payload, products)
                    next_token = payload.get("nextPageToken") or payload.get("next_page_token") or payload.get("nextToken")
                elif isinstance(payload, list):
                    products_payload = self._extract_products_payload(payload)
                    self._collect_product_mappings(products_payload, products)
                    next_token = None
                else:
                    raise self._unsupported_shape_error("products", payload)

                if not next_token:
                    break
                next_token = str(next_token)
                if next_token in seen_tokens:
                    logger.warning("Unify products pagination repeated nextPageToken=%s; stopping to avoid loop", next_token)
                    break
                seen_tokens.add(next_token)
                page_token = next_token

            self._store_products_cache(products)
            logger.info("Unify fetched %s products for name mapping", len(products))
            return products

    def _extract_product_modification_id(self, item: Dict[str, Any]) -> Optional[str]:
        value = (
            item.get("productModificationId")
            or item.get("product_modification_id")
            or item.get("productModificationID")
            or item.get("product_id")
            or item.get("productId")
            or item.get("id")
        )
        return str(value) if value is not None and str(value).strip() else None

    def _extract_item_status(self, item: Dict[str, Any]) -> str:
        value = item.get("status") or item.get("state") or ""
        return str(value).strip().lower()

    def _extract_tax_percentage(self, item: Dict[str, Any]) -> Optional[float]:
        candidates = [
            item.get("taxPercentage"),
            item.get("tax_percentage"),
            item.get("vatPercentage"),
            item.get("vat_percentage"),
            item.get("taxRate"),
            item.get("tax_rate"),
            item.get("vatRate"),
            item.get("vat_rate"),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
        return None

    def _is_delivery_charge_item(self, item: Dict[str, Any]) -> bool:
        text = " ".join(
            str(value).lower()
            for value in [
                item.get("lineType"),
                item.get("line_type"),
                item.get("type"),
                item.get("name"),
                item.get("displayName"),
                item.get("display_name"),
            ]
            if value
        )
        return "delivery" in text or "shipping" in text or "postage" in text

    def _extract_amount_value(self, value: Any) -> float:
        if isinstance(value, dict):
            for key in ("amount", "value", "total", "netAmount"):
                inner = value.get(key)
                if inner is not None:
                    try:
                        return float(inner)
                    except (TypeError, ValueError):
                        continue
            return 0.0
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _to_major_currency(self, value: Any) -> float:
        amount = self._extract_amount_value(value)
        return normalize_unify_money(amount)

    def _extract_next_page_token(self, payload: Any) -> Optional[str]:
        if isinstance(payload, dict):
            token = payload.get("nextPageToken") or payload.get("next_page_token") or payload.get("nextToken")
            return str(token) if token else None
        return None

    async def fetch_order_items(self, order_id: str) -> List[Dict[str, Any]]:
        started_at = time.perf_counter()
        items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        seen_tokens: set[str] = set()
        logger.info("Unify order items fetch started order_id=%s", order_id)

        while True:
            params: Dict[str, Any] = {}
            if page_token:
                params["nextPageToken"] = page_token

            resp = await self._request(
                "GET",
                f"{UNIFY_ORDERS_PATH}/{order_id}{UNIFY_ORDER_ITEMS_SUFFIX}",
                params=params or None,
            )
            payload = self._parse_json_or_fail(resp, f"Unify fetch order items {order_id}")
            self._log_shape(f"order items for {order_id}", payload)
            self._log_endpoint_sample("order_items", payload)
            if isinstance(payload, dict):
                page_items = self._extract_items_payload(payload)
                next_token = self._extract_next_page_token(payload)
            elif isinstance(payload, list):
                page_items = payload
                next_token = None
            else:
                raise self._unsupported_shape_error(f"order items for {order_id}", payload)

            items.extend(page_items)
            logger.info(
                "Unify fetched %s items for order %s%s",
                len(page_items),
                order_id,
                f" nextPageToken={next_token}" if next_token else "",
            )

            if not next_token:
                break
            if next_token in seen_tokens:
                logger.warning("Unify order items pagination repeated nextPageToken=%s for order %s; stopping", next_token, order_id)
                break
            seen_tokens.add(next_token)
            page_token = next_token

        logger.info(
            "Unify order items fetch finished order_id=%s item_count=%s duration_seconds=%.3f",
            order_id,
            len(items),
            time.perf_counter() - started_at,
        )
        return items

    async def fetch_order_detail(self, order_id: str) -> Dict[str, Any]:
        started_at = time.perf_counter()
        logger.info("Unify order detail fetch started order_id=%s", order_id)
        resp = await self._request("GET", f"{UNIFY_ORDERS_PATH}/{order_id}")
        payload = self._parse_json_or_fail(resp, f"Unify fetch order detail {order_id}")
        self._log_shape(f"order detail for {order_id}", payload)
        self._log_endpoint_sample("order_detail", payload)
        detail = self._extract_order_detail_payload(payload)
        logger.info(
            "Unify order detail fetch finished order_id=%s duration_seconds=%.3f",
            order_id,
            time.perf_counter() - started_at,
        )
        return detail

    def _build_preview_order(
        self,
        *,
        order_id: str,
        customer_name: str,
        buyer_name: Optional[str],
        buyer_id: Optional[str],
        delivery_address: Optional[str],
        delivery_date: str,
        total: float,
        status: str,
        preview_status: str,
        preview_reason: str,
    ) -> UnifyOrderPreview:
        return UnifyOrderPreview(
            order_id=order_id,
            customer_name=customer_name,
            buyer_name=buyer_name,
            buyer_id=buyer_id,
            delivery_address=delivery_address,
            delivery_date=delivery_date,
            total=float(total),
            status=status,
            preview_status=preview_status,
            preview_reason=preview_reason,
        )

    async def _hydrate_order_with_items(
        self,
        order: UnifyOrderPreview,
        *,
        buyer_name_map: Optional[Dict[str, str]] = None,
        product_map: Optional[Dict[str, str]] = None,
    ) -> UnifyOrder:
        started_at = time.perf_counter()
        detail = await self.fetch_order_detail(order.order_id)
        items = await self.fetch_order_items(order.order_id)

        if buyer_name_map is None:
            buyer_name_map = {}
        if product_map is None:
            product_result = await asyncio.gather(self.fetch_products(), return_exceptions=True)
            product_map = {} if isinstance(product_result[0], Exception) else product_result[0]

        buyer_id = (
            detail.get("buyerId")
            or detail.get("buyer_id")
            or (detail.get("buyer") or {}).get("id")
            or order.buyer_id
        )
        buyer_organisation = None
        if buyer_id is not None:
            try:
                buyer_organisation = await self.fetch_buyer_organisation(str(buyer_id))
            except UnifyServiceError as exc:
                logger.warning("Unify buyer organisation fetch failed for order_id=%s buyer_id=%s: %s", order.order_id, buyer_id, exc)
        buyer_name, customer_name, delivery_address = self._resolve_buyer_details(
            detail,
            str(buyer_id) if buyer_id is not None else None,
            buyer_name_map,
            buyer_organisation,
        )
        customer_name = customer_name or order.customer_name or buyer_name or f"Customer {buyer_id}" or order.order_id

        preview_reason = (order.preview_reason or "").strip()
        preview_status = (order.preview_status or "ready").strip().lower() or "ready"
        status = (self._extract_status(detail) or order.status or "confirmed").strip().lower()
        order_date_value = detail.get("order_date") or detail.get("orderDate") or order.delivery_date
        delivery_date_value = self._extract_detail_delivery_date_value(detail) or order.delivery_date

        rows: List[OrderLine] = []
        for item in items:
            item_status = self._extract_item_status(item)
            candidate_ids = self._extract_item_product_candidates(item)
            product_id = candidate_ids[0] if candidate_ids else None
            quantity = item.get("quantity", 0)
            quantity_value = float(quantity) if quantity is not None else 0.0
            total_net_amount = self._to_major_currency(item.get("totalNetAmount") or item.get("total_net_amount"))
            if total_net_amount == 0.0:
                total_net_amount = self._to_major_currency(item.get("amount") or item.get("netAmount") or item.get("price"))
            rate = total_net_amount / quantity_value if quantity_value else 0.0
            display_name, unresolved_product, _ = await self._resolve_item_display_name(item, product_map, order_id=order.order_id)
            if item_status not in {"confirmed", "received", "checked", "ready", "completed", "delivered"} and not preview_reason:
                preview_reason = f"Order item status {item_status}"
                preview_status = "blocked"
            if unresolved_product and not preview_reason:
                preview_reason = f"Unresolved product mapping for {product_id or 'unknown'}"
            tax_percentage = self._extract_tax_percentage(item)
            line_type = "delivery" if self._is_delivery_charge_item(item) else "product"
            rows.append(
                OrderLine(
                    item_sku=str(product_id or ""),
                    item_name=str(display_name),
                    quantity=quantity_value,
                    price=rate,
                    unify_product_key=str(product_id) if product_id is not None else None,
                    product_id=str(product_id) if product_id is not None else None,
                    tax_percentage=tax_percentage,
                    line_type=line_type,
                )
            )

        normalized_order_total = self._to_major_currency(detail.get("totalNetAmount") or detail.get("total_net_amount"))
        if normalized_order_total == 0.0:
            normalized_order_total = sum([line.quantity * line.price for line in rows])
        normalized_vat_total = self._to_major_currency(detail.get("totalVatAmount") or detail.get("total_vat_amount"))
        normalized_delivery_fee = self._to_major_currency(
            detail.get("totalDeliveryFee")
            or detail.get("total_delivery_fee")
            or detail.get("deliveryFee")
            or detail.get("delivery_fee")
        )
        has_delivery_item = any(self._is_delivery_charge_item(item) for item in items)
        if normalized_delivery_fee > 0 and not has_delivery_item:
            delivery_tax = self._extract_tax_percentage(detail.get("deliveryFee") or detail.get("delivery_fee") or detail)
            rows.append(
                OrderLine(
                    item_sku="DELIVERY",
                    item_name="Delivery charge",
                    quantity=1.0,
                    price=normalized_delivery_fee,
                    unify_product_key=None,
                    product_id=None,
                    tax_percentage=delivery_tax or 23.0,
                    line_type="delivery",
                )
            )
        elif normalized_delivery_fee > 0 and has_delivery_item:
            logger.info(
                "Unify delivery charge already present as an item line; skipping separate header delivery line order_id=%s",
                order.order_id,
            )

        if not preview_reason and status and not self._is_preview_ready_status(status):
            preview_reason = f"Order status {status}"
            preview_status = "blocked"

        logger.info(
            "Unify order hydration finished order_id=%s item_count=%s duration_seconds=%.3f",
            order.order_id,
            len(rows),
            time.perf_counter() - started_at,
        )

        return UnifyOrder(
            order_id=order.order_id,
            customer_name=str(customer_name),
            buyer_name=str(buyer_name) if buyer_name else None,
            order_date=str(order_date_value or delivery_date_value or order.delivery_date or ""),
            delivery_date=str(delivery_date_value or order.delivery_date or ""),
            delivery_address=str(delivery_address) if delivery_address else None,
            lines=rows,
            total=float(normalized_order_total),
            buyer_id=str(buyer_id) if buyer_id is not None else order.buyer_id,
            status=status or order.status,
            preview_status=preview_status,
            preview_reason=preview_reason,
            total_net_amount=float(normalized_order_total),
            total_vat_amount=float(normalized_vat_total),
            total_delivery_fee=float(normalized_delivery_fee),
        )

    async def fetch_orders_preview(
        self,
        date_from: str,
        date_to: str,
        max_pages: Optional[int] = 5,
    ) -> List[UnifyOrderPreview]:
        request_started_at = time.perf_counter()
        raw_fetch_started_at = request_started_at
        logger.info(
            "Unify fetch_orders_preview received date_from=%s date_to=%s max_pages=%s",
            date_from,
            date_to,
            max_pages,
        )
        self.last_fetch_debug = {}

        page_token: Optional[str] = None
        seen_tokens: set[str] = set()
        raw_page_count = 0
        raw_count = 0
        duplicate_raw_count = 0
        duplicate_raw_order_ids: List[Dict[str, Any]] = []
        dropped_orders: List[Dict[str, Any]] = []
        dropped_order_ids: set[str] = set()
        seen_order_ids: set[str] = set()
        statuses_used = list(UNIFY_PREVIEW_ORDER_STATUSES)
        page_size_used = UNIFY_ORDERS_PAGE_SIZE
        preview_truncated = False
        preview_truncation_reason: Optional[str] = None
        early_stop_reason: Optional[str] = None
        parsed_start_date = self._parse_date_value(date_from)
        parsed_end_date = self._parse_date_value(date_to)
        ordering_assessment: Dict[str, Any] = {
            "createTimeDescendingObserved": None,
            "deliveryDateDescendingObserved": None,
            "createTimeComparablePages": 0,
            "deliveryDateComparablePages": 0,
            "earlyStopEligible": False,
        }
        create_time_descending = True
        delivery_date_descending = True
        previous_page_last_create_time: Optional[datetime] = None
        previous_page_last_delivery_date: Optional[date_cls] = None
        preview_orders: List[UnifyOrderPreview] = []
        preview_status_counts = {"ready": 0, "blocked_in_range": 0}
        dropped_out_of_range_count = 0
        total_in_range_orders = 0
        detail_fallback_attempts = 0
        detail_fallback_failures = 0
        detail_fallback_duration = 0.0
        buyer_map: Optional[Dict[str, str]] = None

        start_at = time.perf_counter()
        max_fetch_duration = 80
        while True:
            if time.perf_counter() - start_at > max_fetch_duration:
                total_duration = time.perf_counter() - request_started_at
                logger.info(
                    "Unify fetch_orders_preview pagination summary rawPageCount=%s rawOrdersCount=%s statusesUsed=%s totalDurationSeconds=%.3f previewTruncated=%s previewTruncationReason=%s earlyStopReason=%s orderingAssessment=%s",
                    raw_page_count,
                    raw_count,
                    statuses_used,
                    total_duration,
                    preview_truncated,
                    preview_truncation_reason,
                    early_stop_reason,
                    ordering_assessment,
                )
                raise UnifyServiceError("Unify fetch_orders timed out after 80 seconds")
            if max_pages is not None and raw_page_count >= max_pages:
                preview_truncated = True
                preview_truncation_reason = "max_pages_reached"
                break
            page_number = raw_page_count + 1
            params: Dict[str, Any] = {"pageSize": page_size_used, "statuses": statuses_used}
            if page_token:
                params["pageToken"] = page_token

            logger.info(
                "Unify raw orders page request page_number=%s request_params=%s pageToken=%s",
                page_number,
                params,
                page_token,
            )
            page_request_started_at = time.perf_counter()
            resp = await self._request("GET", UNIFY_ORDERS_PATH, params=params)
            payload = self._parse_json_or_fail(resp, "Unify fetch orders")
            self._log_shape("orders", payload)
            if isinstance(payload, dict):
                page_orders = self._extract_orders_payload(payload)
                next_token = self._extract_next_page_token(payload)
            elif isinstance(payload, list):
                page_orders = payload
                next_token = None
            else:
                raise self._unsupported_shape_error("orders", payload)

            page_order_ids = [order_id for order_id in (self._extract_order_id(raw) for raw in page_orders) if order_id]
            raw_page_count += 1
            raw_count += len(page_orders)
            logger.info(
                "Unify raw orders page fetched page_number=%s request_params=%s pageToken=%s nextPageToken=%s page_size_returned=%s rawOrderIds=%s",
                page_number,
                params,
                page_token,
                next_token,
                len(page_orders),
                page_order_ids,
            )
            page_first = page_orders[0] if page_orders else {}
            page_last = page_orders[-1] if page_orders else {}
            page_first_order_id = self._extract_order_id(page_first) if page_first else None
            page_last_order_id = self._extract_order_id(page_last) if page_last else None
            page_first_delivery_date = self._extract_raw_delivery_date_value(page_first) if page_first else None
            page_last_delivery_date = self._extract_raw_delivery_date_value(page_last) if page_last else None
            page_first_create_time = self._extract_raw_create_time_value(page_first) if page_first else None
            page_last_create_time = self._extract_raw_create_time_value(page_last) if page_last else None
            page_create_times = [
                parsed
                for parsed in (self._parse_datetime_value(self._extract_raw_create_time_value(raw)) for raw in page_orders)
                if parsed is not None
            ]
            page_delivery_dates = [
                parsed
                for parsed in (self._parse_date_value(self._extract_raw_delivery_date_value(raw)) for raw in page_orders)
                if parsed is not None
            ]
            if page_create_times and len(page_create_times) == len(page_orders):
                ordering_assessment["createTimeComparablePages"] += 1
                if any(left < right for left, right in zip(page_create_times, page_create_times[1:])):
                    create_time_descending = False
                if previous_page_last_create_time is not None and page_create_times[0] > previous_page_last_create_time:
                    create_time_descending = False
                previous_page_last_create_time = page_create_times[-1]
            else:
                if page_orders:
                    create_time_descending = False
            if page_delivery_dates and len(page_delivery_dates) == len(page_orders):
                ordering_assessment["deliveryDateComparablePages"] += 1
                if any(left < right for left, right in zip(page_delivery_dates, page_delivery_dates[1:])):
                    delivery_date_descending = False
                if previous_page_last_delivery_date is not None and page_delivery_dates[0] > previous_page_last_delivery_date:
                    delivery_date_descending = False
                previous_page_last_delivery_date = page_delivery_dates[-1]
            else:
                if page_orders:
                    delivery_date_descending = False
            page_request_duration = time.perf_counter() - page_request_started_at
            cumulative_duration = time.perf_counter() - request_started_at
            logger.info(
                "Unify raw orders scan page_number=%s page_size_returned=%s rawOrdersCount=%s page_request_duration_seconds=%.3f cumulative_duration_seconds=%.3f first_order_id=%s last_order_id=%s first_deliveryDate=%s last_deliveryDate=%s first_createTime=%s last_createTime=%s",
                page_number,
                len(page_orders),
                raw_count,
                page_request_duration,
                cumulative_duration,
                page_first_order_id,
                page_last_order_id,
                None if page_first_delivery_date is None else str(page_first_delivery_date),
                None if page_last_delivery_date is None else str(page_last_delivery_date),
                None if page_first_create_time is None else str(page_first_create_time),
                None if page_last_create_time is None else str(page_last_create_time),
            )

            for raw in page_orders:
                order_id = self._extract_order_id(raw)
                if order_id and order_id in seen_order_ids:
                    duplicate_raw_count += 1
                    if len(duplicate_raw_order_ids) < PREVIEW_MAX_DUPLICATE_DETAILS:
                        duplicate_raw_delivery = self._extract_raw_delivery_date_value(raw)
                        duplicate_parsed_delivery = self._parse_date_value(duplicate_raw_delivery)
                        duplicate_raw_order_ids.append(
                            {
                                "id": order_id,
                                "rawDeliveryDate": None if duplicate_raw_delivery is None else str(duplicate_raw_delivery),
                                "parsedDeliveryDate": duplicate_parsed_delivery.isoformat() if duplicate_parsed_delivery else None,
                                "selectedFrom": date_from,
                                "selectedTo": date_to,
                            }
                        )
                    continue
                if order_id:
                    seen_order_ids.add(order_id)
                if not order_id:
                    if "<missing>" not in dropped_order_ids:
                        dropped_order_ids.add("<missing>")
                        dropped_orders.append({"id": "<missing>", "reason": "missing_order_id"})
                    continue

                raw_delivery_value = self._extract_raw_delivery_date_value(raw)
                delivery_value = raw_delivery_value
                parsed_delivery_date = self._parse_date_value(delivery_value) if delivery_value else None
                detail_fallback_used = False
                if not parsed_delivery_date:
                    detail_fallback_attempts += 1
                    detail_started_at = time.perf_counter()
                    try:
                        delivery_value, parsed_delivery_date_str, detail_fallback_used = await self._resolve_delivery_date_for_preview(
                            raw,
                            order_id,
                            allow_detail_fallback=True,
                        )
                        if parsed_delivery_date_str:
                            parsed_delivery_date = self._parse_date_value(parsed_delivery_date_str)
                        if detail_fallback_used:
                            delivery_value = delivery_value or parsed_delivery_date_str
                    except UnifyServiceError as exc:
                        detail_fallback_failures += 1
                        logger.warning("Unify deliveryDate fallback failed for order_id=%s: %s", order_id, exc)
                        delivery_value = raw_delivery_value
                        parsed_delivery_date = self._parse_date_value(delivery_value) if delivery_value else None
                        if not delivery_value:
                            delivery_value = None
                    finally:
                        detail_fallback_duration += time.perf_counter() - detail_started_at

                raw_status = self._extract_status(raw) or "confirmed"
                buyer_id = raw.get("buyerId") or raw.get("buyer_id") or (raw.get("buyer") or {}).get("id")
                buyer_name = None
                customer_name = None
                delivery_address = None
                total = self._to_major_currency(raw.get("totalNetAmount") or raw.get("total_net_amount"))

                preview_status = "ready"
                preview_reason = ""
                if not delivery_value:
                    preview_status = "blocked"
                    preview_reason = "Missing delivery date in raw preview"
                elif not parsed_delivery_date:
                    preview_status = "blocked"
                    preview_reason = "Unparseable delivery date"
                elif not parsed_start_date or not parsed_end_date:
                    preview_status = "blocked"
                    preview_reason = "Invalid selected date range"
                elif not (parsed_start_date <= parsed_delivery_date <= parsed_end_date):
                    preview_status = "blocked"
                    preview_reason = "Outside selected range"
                elif raw_status and not self._is_preview_ready_status(raw_status):
                    preview_status = "blocked"
                    preview_reason = f"Order status {raw_status}"

                in_range = bool(
                    parsed_delivery_date
                    and parsed_start_date
                    and parsed_end_date
                    and parsed_start_date <= parsed_delivery_date <= parsed_end_date
                )
                if in_range:
                    total_in_range_orders += 1
                else:
                    dropped_out_of_range_count += 1

                if in_range:
                    buyer_organisation = None
                    if buyer_id is not None:
                        try:
                            buyer_organisation = await self.fetch_buyer_organisation(str(buyer_id))
                        except UnifyServiceError as exc:
                            logger.warning(
                                "Unify buyer organisation fetch failed during preview name resolution order_id=%s buyer_id=%s: %s",
                                order_id,
                                buyer_id,
                                exc,
                            )
                    name_source = raw
                    if buyer_name is None and customer_name is None:
                        try:
                            name_source = await self.fetch_order_detail(order_id)
                        except UnifyServiceError as exc:
                            logger.warning(
                                "Unify preview customer-name detail fetch failed order_id=%s buyer_id=%s: %s",
                                order_id,
                                buyer_id,
                                exc,
                            )
                            name_source = raw
                    buyer_name, customer_name, delivery_address = self._resolve_buyer_details(
                        name_source,
                        str(buyer_id) if buyer_id is not None else None,
                        buyer_map,
                        buyer_organisation,
                    )
                    if not customer_name:
                        customer_name = f"Customer {buyer_id}" if buyer_id is not None else order_id
                    logger.info(
                        "Unify preview customer resolution order_id=%s buyer_id=%s buyer_name=%s customer_name=%s",
                        order_id,
                        buyer_id,
                        buyer_name,
                        customer_name,
                    )

                if len(preview_orders) >= PREVIEW_MAX_ORDERS:
                    preview_truncated = True
                    preview_truncation_reason = "max_preview_orders_reached"
                    continue

                if preview_status == "ready":
                    preview_status_counts["ready"] += 1
                    preview_orders.append(
                        self._build_preview_order(
                            order_id=order_id,
                            customer_name=str(customer_name),
                            buyer_name=str(buyer_name) if buyer_name else None,
                            buyer_id=str(buyer_id) if buyer_id is not None else None,
                            delivery_address=str(delivery_address) if delivery_address else None,
                            delivery_date=str(delivery_value or ""),
                            total=float(total),
                            status=raw_status,
                            preview_status=preview_status,
                            preview_reason=preview_reason,
                        )
                    )
                elif in_range:
                    preview_status_counts["blocked_in_range"] += 1
                    if not preview_reason:
                        preview_reason = preview_status
                    preview_orders.append(
                        self._build_preview_order(
                            order_id=order_id,
                            customer_name=str(customer_name),
                            buyer_name=str(buyer_name) if buyer_name else None,
                            buyer_id=str(buyer_id) if buyer_id is not None else None,
                            delivery_address=str(delivery_address) if delivery_address else None,
                            delivery_date=str(delivery_value or ""),
                            total=float(total),
                            status=raw_status,
                            preview_status=preview_status,
                            preview_reason=preview_reason,
                        )
                    )
                else:
                    if not preview_reason:
                        preview_reason = "Outside selected range"
                    if order_id not in dropped_order_ids and len(dropped_orders) < PREVIEW_MAX_DROPPED_DETAILS:
                        dropped_order_ids.add(order_id)
                        dropped_orders.append(
                            {
                                "id": order_id,
                                "reason": preview_reason,
                                "rawDeliveryDate": None if raw_delivery_value is None else str(raw_delivery_value),
                                "finalDeliveryDate": None if delivery_value is None else str(delivery_value),
                                "parsedDeliveryDate": parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                                "selectedFrom": date_from,
                                "selectedTo": date_to,
                            }
                        )

            if not next_token:
                break

            if delivery_date_descending and parsed_start_date and page_delivery_dates:
                oldest_page_delivery_date = page_delivery_dates[-1]
                if oldest_page_delivery_date < parsed_start_date:
                    early_stop_reason = "older_than_selected_range"
                    logger.info(
                        "Unify preview early stop triggered page_number=%s oldest_delivery_date=%s selected_start_date=%s",
                        page_number,
                        oldest_page_delivery_date.isoformat(),
                        parsed_start_date.isoformat(),
                    )
                    break

            next_token = str(next_token)
            if next_token == page_token or next_token in seen_tokens:
                logger.warning(
                    "Unify orders pagination repeated nextPageToken=%s page_number=%s pageTokenSent=%s; stopping to avoid loop",
                    next_token,
                    page_number,
                    page_token,
                )
                break
            seen_tokens.add(next_token)
            page_token = next_token

        raw_fetch_duration = time.perf_counter() - raw_fetch_started_at
        ordering_assessment["createTimeDescendingObserved"] = (
            create_time_descending if ordering_assessment["createTimeComparablePages"] else None
        )
        ordering_assessment["deliveryDateDescendingObserved"] = (
            delivery_date_descending if ordering_assessment["deliveryDateComparablePages"] else None
        )
        ordering_assessment["earlyStopEligible"] = bool(
            ordering_assessment["deliveryDateDescendingObserved"] and parsed_start_date and not preview_truncated
        )
        logger.info(
            "Unify raw orders fetch finished raw_count=%s unique_count=%s page_count=%s duration_seconds=%.3f",
            raw_count,
            raw_count - duplicate_raw_count,
            raw_page_count,
            raw_fetch_duration,
        )

        total_duration = time.perf_counter() - request_started_at
        preview_count = len(preview_orders)
        logger.info(
            "Unify fetch_orders_preview finished total_orders=%s ready_count=%s blocked_in_range_count=%s dropped_out_of_range_count=%s detail_fallback_attempts=%s detail_fallback_failures=%s raw_fetch_duration_seconds=%.3f detail_fallback_duration_seconds=%.3f total_duration_seconds=%.3f",
            preview_count,
            preview_status_counts["ready"],
            preview_status_counts["blocked_in_range"],
            dropped_out_of_range_count,
            detail_fallback_attempts,
            detail_fallback_failures,
            raw_fetch_duration,
            detail_fallback_duration,
            total_duration,
        )
        logger.info(
            "Unify fetch_orders_preview pagination summary rawPageCount=%s rawOrdersCount=%s statusesUsed=%s totalDurationSeconds=%.3f previewTruncated=%s previewTruncationReason=%s earlyStopReason=%s orderingAssessment=%s",
            raw_page_count,
            raw_count,
            statuses_used,
            total_duration,
            preview_truncated,
            preview_truncation_reason,
            early_stop_reason,
            ordering_assessment,
        )
        self.last_fetch_debug = {
            "rawOrdersCount": raw_count,
            "rawPageCount": raw_page_count,
            "total_scanned_orders": raw_count,
            "statusesUsed": statuses_used,
            "pageSizeUsed": page_size_used,
            "previewTruncated": preview_truncated,
            "previewTruncationReason": preview_truncation_reason,
            "earlyStopReason": early_stop_reason,
            "orderingAssessment": ordering_assessment,
            "duplicateOrdersCount": duplicate_raw_count,
            "duplicateRawOrderIds": duplicate_raw_order_ids,
            "droppedOrdersCount": len(dropped_orders),
            "droppedOrders": dropped_orders,
            "previewOrdersCount": preview_count,
            "preview_count": preview_count,
            "total_in_range_orders": total_in_range_orders,
            "readyCount": preview_status_counts["ready"],
            "blockedCount": preview_status_counts["blocked_in_range"],
            "blocked_in_range_count": preview_status_counts["blocked_in_range"],
            "dropped_out_of_range_count": dropped_out_of_range_count,
            "detailFallbackAttempts": detail_fallback_attempts,
            "detailFallbackFailures": detail_fallback_failures,
            "itemHydrationCalls": 0,
            "rawFetchDurationSeconds": raw_fetch_duration,
            "detailFallbackDurationSeconds": detail_fallback_duration,
            "totalDurationSeconds": total_duration,
            "dateFrom": date_from,
            "dateTo": date_to,
        }
        return preview_orders

    async def fetch_orders_for_export(
        self,
        date_from: str,
        date_to: str,
        order_ids: Optional[List[str]] = None,
    ) -> List[UnifyOrder]:
        selected = {order_id.strip() for order_id in (order_ids or []) if order_id and order_id.strip()}
        if selected:
            selected_ids = list(selected)
            logger.info(
                "Unify export hydration requested for selected order_ids=%s date_from=%s date_to=%s",
                selected_ids,
                date_from,
                date_to,
            )
            results = await asyncio.gather(
                *(self.fetch_order_with_items(order_id) for order_id in selected_ids),
                return_exceptions=True,
            )
            hydrated: List[UnifyOrder] = []
            for order_id, result in zip(selected_ids, results):
                if isinstance(result, Exception):
                    logger.warning("Unify selected order hydration failed for export order_id=%s: %s", order_id, result)
                    hydrated.append(
                        UnifyOrder(
                            order_id=order_id,
                            customer_name=order_id,
                            buyer_name=None,
                            order_date=date_from,
                            delivery_date=date_from,
                            delivery_address=None,
                            lines=[],
                            total=0.0,
                            buyer_id=None,
                            status="confirmed",
                            preview_status="item_fetch_failed",
                            preview_reason=str(result),
                            total_net_amount=0.0,
                            total_vat_amount=0.0,
                            total_delivery_fee=0.0,
                        )
                    )
                    continue
                hydrated.append(result)
            return hydrated

        previews = await self.fetch_orders_preview(date_from, date_to, max_pages=None)
        previews = [order for order in previews if order.preview_status == "ready"]

        if not previews:
            return []

        hydrated: List[UnifyOrder] = []
        for order in previews:
            try:
                hydrated.append(await self._hydrate_order_with_items(order))
            except UnifyServiceError as exc:
                logger.warning("Unify order hydration failed for export order_id=%s: %s", order.order_id, exc)
                hydrated.append(
                    UnifyOrder(
                        order_id=order.order_id,
                        customer_name=order.customer_name,
                        buyer_name=order.buyer_name,
                        order_date=order.delivery_date or date_from,
                        delivery_date=order.delivery_date or date_from,
                        delivery_address=order.delivery_address,
                        lines=[],
                        total=order.total,
                        buyer_id=order.buyer_id,
                        status=order.status,
                        preview_status="item_fetch_failed",
                        preview_reason=str(exc),
                        total_net_amount=order.total,
                        total_vat_amount=0.0,
                        total_delivery_fee=0.0,
                    )
                )

        return hydrated

    async def fetch_order_with_items(
        self,
        order_id: str,
        *,
        buyer_name_map: Optional[Dict[str, str]] = None,
        product_map: Optional[Dict[str, str]] = None,
    ) -> UnifyOrder:
        preview = self._build_preview_order(
            order_id=order_id,
            customer_name=order_id,
            buyer_name=None,
            buyer_id=None,
            delivery_address=None,
            delivery_date="",
            total=0.0,
            status="confirmed",
            preview_status="ready",
            preview_reason="",
        )
        return await self._hydrate_order_with_items(
            preview,
            buyer_name_map=buyer_name_map,
            product_map=product_map,
        )

    async def test_connection(self) -> bool:
        # Unify API supports `pageSize` pagination; `limit` is not defined in docs.
        resp = await self._request("GET", UNIFY_ORDERS_PATH, params={"pageSize": 1}, force_refresh=True)
        payload = self._extract_orders_payload(self._parse_json_or_fail(resp, "Unify test connection"))
        logger.info("Unify connection test succeeded")
        return True if isinstance(payload, list) else False

    async def fetch_orders(self, date_from: str, date_to: str) -> List[UnifyOrder]:
        logger.info("Unify fetch_orders received date_from=%s date_to=%s", date_from, date_to)
        self.last_fetch_debug = {}
        raw_orders: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        seen_tokens: set[str] = set()
        raw_page_count = 0
        raw_page_tokens: List[Optional[str]] = []
        raw_order_ids: List[str] = []
        raw_order_ids_by_page: List[Dict[str, Any]] = []
        statuses_used = list(UNIFY_ORDERS_STATUSES)
        page_size_used = UNIFY_ORDERS_PAGE_SIZE
        target_order_ids = {"4308188", "4311120"}
        target_order_traces: Dict[str, Dict[str, Any]] = {
            order_id: {
                "raw_found": False,
                "detail_fetch_attempted": False,
                "detail_fetch_succeeded": False,
                "detail_delivery_date": None,
                "passed_filter": False,
                "dropped_reason": None,
                "included_in_preview": False,
            }
            for order_id in target_order_ids
        }
        raw_count = 0
        orders_processed_count = 0
        duplicate_raw_count = 0
        included_by_delivery = 0
        included_preview_count = 0
        raw_status_not_ready_count = 0
        item_fetch_attempts_count = 0
        item_fetch_failed_count = 0
        missing_items_count = 0
        mapping_issue_count = 0
        dropped_orders: List[Dict[str, Any]] = []
        seen_order_ids: set[str] = set()
        duplicate_raw_order_ids: List[Dict[str, Any]] = []
        dropped_order_ids: set[str] = set()

        def _log_target_order_trace(order_id: str) -> None:
            trace = target_order_traces[order_id]
            if trace["included_in_preview"]:
                loss_stage = "included_in_preview"
            elif not trace["raw_found"]:
                loss_stage = "raw_orders_fetch"
            elif not trace["detail_fetch_attempted"]:
                loss_stage = "detail_fetch_not_attempted"
            elif not trace["detail_fetch_succeeded"]:
                loss_stage = "detail_fetch"
            elif not trace["passed_filter"]:
                loss_stage = "delivery_date_filter"
            else:
                loss_stage = "post_filter_pre_preview"

            logger.info(
                "order_trace order_id=%s raw_found=%s detail_fetched=%s detail_fetch_succeeded=%s detail_delivery_date=%s passed_filter=%s dropped_reason=%s included_in_preview=%s loss_stage=%s",
                order_id,
                trace["raw_found"],
                trace["detail_fetch_attempted"],
                trace["detail_fetch_succeeded"],
                trace["detail_delivery_date"],
                trace["passed_filter"],
                trace["dropped_reason"],
                trace["included_in_preview"],
                loss_stage,
            )

        def _append_dropped_order(
            order_id: str,
            reason: str,
            *,
            raw_delivery_value: Any = None,
            final_delivery_value: Any = None,
            parsed_delivery_date: Optional[date_cls] = None,
        ) -> None:
            if order_id in dropped_order_ids:
                return
            dropped_order_ids.add(order_id)
            dropped_orders.append(
                {
                    "id": order_id,
                    "reason": reason,
                    "rawDeliveryDate": None if raw_delivery_value is None else str(raw_delivery_value),
                    "finalDeliveryDate": None if final_delivery_value is None else str(final_delivery_value),
                    "parsedDeliveryDate": parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                    "selectedFrom": date_from,
                    "selectedTo": date_to,
                }
            )

        while True:
            page_number = raw_page_count + 1
            params: Dict[str, Any] = {
                "pageSize": page_size_used,
                "statuses": statuses_used,
            }
            if page_token:
                params["pageToken"] = page_token

            logger.info(
                "Unify raw orders page request page_number=%s request_params=%s pageToken=%s",
                page_number,
                params,
                page_token,
            )

            resp = await self._request("GET", UNIFY_ORDERS_PATH, params=params)
            payload = self._parse_json_or_fail(resp, "Unify fetch orders")
            self._log_shape("orders", payload)
            if isinstance(payload, dict):
                page_orders = self._extract_orders_payload(payload)
                next_token = self._extract_next_page_token(payload)
            elif isinstance(payload, list):
                page_orders = payload
                next_token = None
            else:
                raise self._unsupported_shape_error("orders", payload)

            page_order_ids = [order_id for order_id in (self._extract_order_id(raw) for raw in page_orders) if order_id]
            page_size_returned = len(page_orders)
            raw_page_count += 1
            raw_page_tokens.append(page_token)
            raw_order_ids.extend(page_order_ids)
            raw_order_ids_by_page.append(
                {
                    "pageNumber": page_number,
                    "pageTokenSent": page_token,
                    "requestParamsUsed": dict(params),
                    "pageSizeReturned": page_size_returned,
                    "nextPageTokenReturned": next_token,
                    "rawOrderIds": page_order_ids,
                }
            )
            logger.info(
                "Unify raw orders page fetched page_number=%s request_params=%s pageToken=%s nextPageToken=%s page_size_returned=%s rawOrderIds=%s",
                page_number,
                params,
                page_token,
                next_token,
                page_size_returned,
                page_order_ids,
            )
            raw_count += len(page_orders)
            for raw in page_orders:
                order_id = self._extract_order_id(raw)
                if order_id in target_order_traces:
                    target_order_traces[order_id]["raw_found"] = True
                if order_id and order_id in seen_order_ids:
                    duplicate_raw_count += 1
                    duplicate_raw_delivery = self._extract_raw_delivery_date_value(raw)
                    duplicate_parsed_delivery = self._parse_date_value(duplicate_raw_delivery)
                    duplicate_raw_order_ids.append(
                        {
                            "id": order_id,
                            "rawDeliveryDate": None if duplicate_raw_delivery is None else str(duplicate_raw_delivery),
                            "parsedDeliveryDate": duplicate_parsed_delivery.isoformat() if duplicate_parsed_delivery else None,
                            "selectedFrom": date_from,
                            "selectedTo": date_to,
                        }
                    )
                    logger.info(
                        "Unify duplicate raw order row order_id=%s raw_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                        order_id,
                        duplicate_raw_delivery,
                        duplicate_parsed_delivery.isoformat() if duplicate_parsed_delivery else None,
                        date_from,
                        date_to,
                        "duplicate_raw_order_id",
                    )
                    continue
                if order_id:
                    seen_order_ids.add(order_id)
                raw_orders.append(raw)

            if not next_token:
                break
            next_token = str(next_token)
            if next_token == page_token or next_token in seen_tokens:
                logger.warning(
                    "Unify orders pagination repeated nextPageToken=%s page_number=%s pageTokenSent=%s; stopping to avoid loop",
                    next_token,
                    page_number,
                    page_token,
                )
                break
            seen_tokens.add(next_token)
            page_token = next_token

        found_4308188 = "4308188" in raw_order_ids
        found_4311120 = "4311120" in raw_order_ids
        logger.info(
            "Unify raw orders pagination summary rawPageCount=%s rawPageTokens=%s rawOrderIds=%s rawOrderIdsByPage=%s statusesUsed=%s pageSizeUsed=%s found_4308188=%s found_4311120=%s",
            raw_page_count,
            raw_page_tokens,
            raw_order_ids,
            raw_order_ids_by_page,
            statuses_used,
            page_size_used,
            found_4308188,
            found_4311120,
        )

        products_fetch_failed = False
        buyer_map: Dict[str, str] = {}
        logger.info("Unify buyers list fetch skipped for fetch_orders; buyer organisation lookup is used for display names")

        product_result = await asyncio.gather(
            self.fetch_products(),
            return_exceptions=True,
        )

        if isinstance(product_result[0], Exception):
            products_fetch_failed = True
            product_map = {}
            if isinstance(product_result[0], UnifyServiceError):
                logger.warning("Unify product lookup failed; continuing with raw item names: %s", product_result[0])
            else:
                logger.exception("Unify product lookup failed unexpectedly")
        else:
            product_map = product_result[0]
        logger.info("Unify products fetched for fetch_orders product_count=%s failed=%s", len(product_map), products_fetch_failed)

        detail_fetch_failed_count = 0
        detail_failures: List[Dict[str, Any]] = []
        detail_by_order_id: Dict[str, Dict[str, Any]] = {}
        included_order_ids: List[str] = []
        order_ids: List[str] = [order_id for order_id in (self._extract_order_id(raw) for raw in raw_orders) if order_id]

        for batch_start in range(0, len(order_ids), UNIFY_ORDER_DETAIL_BATCH_SIZE):
            batch_ids = order_ids[batch_start : batch_start + UNIFY_ORDER_DETAIL_BATCH_SIZE]
            logger.info(
                "Unify order detail batch started batch_start=%s batch_size=%s total_orders=%s",
                batch_start,
                len(batch_ids),
                len(order_ids),
            )
            for order_id in batch_ids:
                if order_id in target_order_traces:
                    target_order_traces[order_id]["detail_fetch_attempted"] = True
            batch_results = await asyncio.gather(
                *(self.fetch_order_detail(order_id) for order_id in batch_ids),
                return_exceptions=True,
            )
            for order_id, result in zip(batch_ids, batch_results):
                if isinstance(result, Exception):
                    detail_fetch_failed_count += 1
                    detail_failures.append({"id": order_id, "reason": "detail_fetch_failed"})
                    logger.exception("Unify order detail fetch failed order_id=%s", order_id)
                    continue
                detail_by_order_id[order_id] = result
                if order_id in target_order_traces:
                    target_order_traces[order_id]["detail_fetch_succeeded"] = True
            logger.info(
                "Unify order detail batch finished batch_start=%s batch_size=%s successful=%s failed=%s",
                batch_start,
                len(batch_ids),
                len([order_id for order_id in batch_ids if order_id in detail_by_order_id]),
                len([order_id for order_id in batch_ids if order_id not in detail_by_order_id]),
            )

        parsed_start_date = self._parse_date_value(date_from)
        parsed_end_date = self._parse_date_value(date_to)
        orders: List[UnifyOrder] = []
        for raw in raw_orders:
            order_id = self._extract_order_id(raw)
            raw_delivery_value = self._extract_raw_delivery_date_value(raw)
            if not order_id:
                dropped_record = {"id": "<missing>", "reason": "missing_order_id"}
                if raw_delivery_value is not None:
                    dropped_record["rawDeliveryDate"] = str(raw_delivery_value)
                if "<missing>" not in dropped_order_ids:
                    dropped_order_ids.add("<missing>")
                    dropped_orders.append(dropped_record)
                continue

            detail = detail_by_order_id.get(order_id)
            detail_delivery_value = self._extract_detail_delivery_date_value(detail) if detail else None
            parsed_delivery_date = self._parse_date_value(detail_delivery_value) if detail_delivery_value else None
            if order_id in target_order_traces:
                target_order_traces[order_id]["detail_delivery_date"] = None if detail_delivery_value is None else str(detail_delivery_value)

            logger.info(
                "Unify delivery evaluation order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s",
                order_id,
                raw_delivery_value,
                detail_delivery_value,
                parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                date_from,
                date_to,
            )

            if detail is None:
                if order_id in target_order_traces:
                    target_order_traces[order_id]["dropped_reason"] = "detail_fetch_failed"
                    target_order_traces[order_id]["passed_filter"] = False
                _append_dropped_order(
                    order_id,
                    "detail_fetch_failed",
                    raw_delivery_value=raw_delivery_value,
                    final_delivery_value=detail_delivery_value,
                    parsed_delivery_date=parsed_delivery_date,
                )
                logger.info(
                    "Unify dropped order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                    order_id,
                    raw_delivery_value,
                    detail_delivery_value,
                    parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                    date_from,
                    date_to,
                    "detail_fetch_failed",
                )
                continue

            if detail_delivery_value is None:
                if order_id in target_order_traces:
                    target_order_traces[order_id]["dropped_reason"] = "missing_delivery_date"
                    target_order_traces[order_id]["passed_filter"] = False
                _append_dropped_order(
                    order_id,
                    "missing_delivery_date",
                    raw_delivery_value=raw_delivery_value,
                    final_delivery_value=detail_delivery_value,
                    parsed_delivery_date=parsed_delivery_date,
                )
                logger.info(
                    "Unify dropped order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                    order_id,
                    raw_delivery_value,
                    detail_delivery_value,
                    parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                    date_from,
                    date_to,
                    "missing_delivery_date",
                )
                continue

            if not parsed_delivery_date:
                if order_id in target_order_traces:
                    target_order_traces[order_id]["dropped_reason"] = "unparseable_delivery_date"
                    target_order_traces[order_id]["passed_filter"] = False
                _append_dropped_order(
                    order_id,
                    "unparseable_delivery_date",
                    raw_delivery_value=raw_delivery_value,
                    final_delivery_value=detail_delivery_value,
                    parsed_delivery_date=parsed_delivery_date,
                )
                logger.info(
                    "Unify dropped order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                    order_id,
                    raw_delivery_value,
                    detail_delivery_value,
                    parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                    date_from,
                    date_to,
                    "unparseable_delivery_date",
                )
                continue

            if not parsed_start_date or not parsed_end_date:
                if order_id in target_order_traces:
                    target_order_traces[order_id]["dropped_reason"] = "invalid_selected_date_range"
                    target_order_traces[order_id]["passed_filter"] = False
                _append_dropped_order(
                    order_id,
                    "invalid_selected_date_range",
                    raw_delivery_value=raw_delivery_value,
                    final_delivery_value=detail_delivery_value,
                    parsed_delivery_date=parsed_delivery_date,
                )
                logger.info(
                    "Unify dropped order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                    order_id,
                    raw_delivery_value,
                    detail_delivery_value,
                    parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                    date_from,
                    date_to,
                    "invalid_selected_date_range",
                )
                continue

            in_range = parsed_start_date <= parsed_delivery_date <= parsed_end_date
            if order_id in target_order_traces:
                target_order_traces[order_id]["passed_filter"] = in_range
            if not in_range:
                if order_id in target_order_traces:
                    target_order_traces[order_id]["dropped_reason"] = "outside_selected_range"
                _append_dropped_order(
                    order_id,
                    "outside_selected_range",
                    raw_delivery_value=raw_delivery_value,
                    final_delivery_value=detail_delivery_value,
                    parsed_delivery_date=parsed_delivery_date,
                )
                logger.info(
                    "Unify dropped order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                    order_id,
                    raw_delivery_value,
                    detail_delivery_value,
                    parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                    date_from,
                    date_to,
                    "outside_selected_range",
                )
                continue

            logger.info(
                "Unify included order_id=%s raw_delivery_date=%s final_delivery_date=%s parsed_delivery_date=%s selectedFrom=%s selectedTo=%s reason=%s",
                order_id,
                raw_delivery_value,
                detail_delivery_value,
                parsed_delivery_date.isoformat() if parsed_delivery_date else None,
                date_from,
                date_to,
                "in_range",
            )
            included_by_delivery += 1
            if order_id in target_order_traces:
                target_order_traces[order_id]["included_in_preview"] = True
                target_order_traces[order_id]["dropped_reason"] = None

            preview_status = "ready"
            preview_reason = ""
            raw_item_error: Optional[str] = None
            try:
                items = await self.fetch_order_items(order_id)
            except UnifyServiceError as exc:
                raw_item_error = str(exc)
                logger.error("Unify order %s items fetch failed: %s", order_id, exc)
                item_fetch_failed_count += 1
                preview_status = "item_fetch_failed"
                preview_reason = raw_item_error
                items = []
            finally:
                item_fetch_attempts_count += 1

            logger.info("Unify per-order item fetch finished order_id=%s item_count=%s", order_id, len(items))

            if not items:
                if preview_status == "ready":
                    preview_status = "missing_items"
                    preview_reason = "No order items returned"
                if preview_status == "missing_items":
                    missing_items_count += 1
                logger.info("Unify order %s had no items but remains in preview", order_id)

            buyer_id = (
                detail.get("buyerId")
                or detail.get("buyer_id")
                or (detail.get("buyer") or {}).get("id")
            )
            buyer_organisation = None
            if buyer_id is not None:
                try:
                    buyer_organisation = await self.fetch_buyer_organisation(str(buyer_id))
                except UnifyServiceError as exc:
                    logger.warning(
                        "Unify buyer organisation fetch failed during order preview hydration order_id=%s buyer_id=%s: %s",
                        order_id,
                        buyer_id,
                        exc,
                    )
            buyer_name, customer_name, delivery_address = self._resolve_buyer_details(
                detail,
                str(buyer_id) if buyer_id is not None else None,
                buyer_map,
                buyer_organisation,
            )
            if not customer_name:
                customer_name = f"Customer {buyer_id}" if buyer_id is not None else order_id

            rows = []
            first_valid_item: Optional[Dict[str, Any]] = None
            mapping_issue_detected = False
            for item in items:
                item_status = self._extract_item_status(item)
                candidate_ids = self._extract_item_product_candidates(item)
                product_id = candidate_ids[0] if candidate_ids else None
                quantity = item.get("quantity", 0)
                quantity_value = float(quantity) if quantity is not None else 0.0
                total_net_amount = self._to_major_currency(item.get("totalNetAmount") or item.get("total_net_amount"))
                if total_net_amount == 0.0:
                    total_net_amount = self._to_major_currency(item.get("amount") or item.get("netAmount") or item.get("price"))
                rate = total_net_amount / quantity_value if quantity_value else 0.0
                display_name, unresolved_product, _ = await self._resolve_item_display_name(item, product_map, order_id=order_id)
                if unresolved_product or not display_name or display_name == str(product_id or ""):
                    mapping_issue_detected = True
                if item_status not in {"confirmed", "received", "checked", "ready", "completed", "delivered"}:
                    preview_reason = preview_reason or f"Order item status {item_status}"
                    preview_status = "blocked"
                tax_percentage = self._extract_tax_percentage(item)
                line_type = "delivery" if self._is_delivery_charge_item(item) else "product"
                line = OrderLine(
                    item_sku=str(product_id or ""),
                    item_name=str(display_name),
                    quantity=quantity_value,
                    price=rate,
                    unify_product_key=str(product_id) if product_id is not None else None,
                    product_id=str(product_id) if product_id is not None else None,
                    tax_percentage=tax_percentage,
                    line_type=line_type,
                )
                rows.append(line)
                if first_valid_item is None:
                    first_valid_item = item

            normalized_order_total = self._to_major_currency(detail.get("totalNetAmount") or detail.get("total_net_amount"))
            if normalized_order_total == 0.0:
                normalized_order_total = sum([l.quantity * l.price for l in rows])
            normalized_vat_total = self._to_major_currency(detail.get("totalVatAmount") or detail.get("total_vat_amount"))
            normalized_delivery_fee = self._to_major_currency(
                detail.get("totalDeliveryFee")
                or detail.get("total_delivery_fee")
                or detail.get("deliveryFee")
                or detail.get("delivery_fee")
            )
            has_delivery_item = any(self._is_delivery_charge_item(item) for item in items)
            if normalized_delivery_fee > 0 and not has_delivery_item:
                delivery_tax = self._extract_tax_percentage(detail.get("deliveryFee") or detail.get("delivery_fee") or detail)
                rows.append(
                    OrderLine(
                        item_sku="DELIVERY",
                        item_name="Delivery charge",
                        quantity=1.0,
                        price=normalized_delivery_fee,
                        unify_product_key=None,
                        product_id=None,
                        tax_percentage=delivery_tax or 23.0,
                        line_type="delivery",
                    )
                )
            elif normalized_delivery_fee > 0 and has_delivery_item:
                logger.info(
                    "Unify delivery charge already present as an item line; skipping separate header delivery line order_id=%s",
                    order_id,
                )
            if mapping_issue_detected and preview_status == "ready":
                preview_status = "mapping_issue"
                preview_reason = preview_reason or "Some item mappings fell back to raw names"
                mapping_issue_count += 1
            raw_status = self._extract_status(detail)
            if raw_status and not self._is_preview_ready_status(raw_status) and preview_status == "ready":
                preview_reason = preview_reason or f"Order status {raw_status}"
                preview_status = "blocked"
            if preview_status != "ready" and not preview_reason:
                preview_reason = preview_status
            if raw_status and not self._is_preview_ready_status(raw_status):
                raw_status_not_ready_count += 1
            self._log_money_debug(detail, first_valid_item, normalized_order_total, rows[0].price if rows else 0.0)
            order_date_value = detail.get("order_date") or detail.get("orderDate") or detail_delivery_value or date_from
            order = UnifyOrder(
                order_id=order_id,
                customer_name=str(customer_name),
                buyer_name=str(buyer_name) if buyer_name else None,
                order_date=str(order_date_value),
                delivery_date=str(detail_delivery_value or detail.get("order_date") or detail.get("orderDate") or date_from),
                delivery_address=str(delivery_address) if delivery_address else None,
                lines=rows,
                total=float(normalized_order_total),
                buyer_id=str(buyer_id) if buyer_id is not None else None,
                status=raw_status or "confirmed",
                preview_status=preview_status,
                preview_reason=preview_reason,
                total_net_amount=float(normalized_order_total),
                total_vat_amount=float(normalized_vat_total),
                total_delivery_fee=float(normalized_delivery_fee),
            )
            orders.append(order)
            included_order_ids.append(order_id)
            included_preview_count += 1
            orders_processed_count += 1

        for order_id in sorted(target_order_traces):
            _log_target_order_trace(order_id)

        logger.info("Included order IDs for %s: %s", date_from, included_order_ids)
        logger.info(
            "Unify delivery debug summary: rawOrdersCount=%s filteredOrdersCount=%s previewOrdersCount=%s droppedOrdersCount=%s detailFetchFailures=%s itemFetchAttempts=%s itemFetchFailures=%s droppedOrders=%s",
            raw_count,
            included_by_delivery,
            included_preview_count,
            len(dropped_orders),
            detail_fetch_failed_count,
            item_fetch_attempts_count,
            item_fetch_failed_count,
            [{"id": item["id"], "reason": item["reason"]} for item in dropped_orders],
        )
        self.last_fetch_debug = {
            "rawOrdersCount": raw_count,
            "rawPageCount": raw_page_count,
            "rawPageTokens": raw_page_tokens,
            "rawOrderIds": raw_order_ids,
            "rawOrderIdsByPage": raw_order_ids_by_page,
            "statusesUsed": statuses_used,
            "pageSizeUsed": page_size_used,
            "filteredOrdersCount": included_by_delivery,
            "ordersProcessedCount": orders_processed_count,
            "previewOrdersCount": included_preview_count,
            "duplicateOrdersCount": duplicate_raw_count,
            "duplicateRawOrderIds": duplicate_raw_order_ids,
            "statusNotReadyCount": raw_status_not_ready_count,
            "detailFetchFailedCount": detail_fetch_failed_count,
            "detailFetchFailures": detail_failures,
            "itemFetchAttempts": item_fetch_attempts_count,
            "itemFetchFailedCount": item_fetch_failed_count,
            "itemFetchFailures": item_fetch_failed_count,
            "missingItemsCount": missing_items_count,
            "mappingIssueCount": mapping_issue_count,
            "buyersFetchFailed": False,
            "productsFetchFailed": products_fetch_failed,
            "droppedOrdersCount": len(dropped_orders),
            "droppedOrders": dropped_orders,
            "includedOrderIds": included_order_ids,
            "dateFrom": date_from,
            "dateTo": date_to,
        }
        logger.info("Unify delivery dropped orders detailed: %s", dropped_orders)
        logger.info("Unify fetched %s delivery-date orders with details for %s to %s", len(orders), date_from, date_to)
        return orders


def make_unify_service() -> UnifyService:
    return UnifyService(
        base_url=settings.UNIFY_BASE_URL,
        client_id=settings.UNIFY_CLIENT_ID,
        client_secret=settings.UNIFY_CLIENT_SECRET,
    )
