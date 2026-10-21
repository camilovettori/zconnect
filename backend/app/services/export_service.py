import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import select, update

from .unify_service import UnifyService
from .zoho_service import ZohoAmbiguousResponseError, ZohoService, ZohoServiceError
from .. import models
from ..schemas import UnifyOrder


class ExportServiceError(Exception):
    pass


# Analytics stores invoiced values in the currency the rest of the app displays (EUR); the
# Zoho organisation's currency is not stored anywhere else.
ANALYTICS_CURRENCY = "EUR"
DELIVERY_PRODUCT_KEY = "delivery-charge"
_CENT = Decimal("0.01")


def parse_iso_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def delivery_line_flags(order: Any, line_items: List[Dict[str, Any]]) -> Optional[List[bool]]:
    """Which payload lines are delivery charges. The payload builder emits one line per order line
    with a positive quantity and a valid price, in order, plus an optional synthetic "Delivery
    charge" line; when the counts do not line up, returns None (name matching is used instead)."""
    kept = [getattr(line, "line_type", None) == "delivery" for line in getattr(order, "lines", None) or [] if (line.quantity or 0) > 0 and line.price is not None and line.price >= 0]
    if len(line_items) == len(kept):
        return kept
    if len(line_items) == len(kept) + 1 and str(line_items[-1].get("name", "")).lower() == "delivery charge":
        return kept + [True]
    return None


def analytics_lines(line_items: List[Dict[str, Any]], company_id: str, delivery_flags: Optional[List[bool]] = None) -> List["models.OrderLineFact"]:
    """Order line facts from the exact Zoho payload lines: net value = quantity x rate (pre-tax).
    Delivery charges get one shared key so they never appear as a product."""
    lines = []
    for index, item in enumerate(line_items):
        name = str(item.get("name") or item.get("description") or "Unnamed item").strip() or "Unnamed item"
        is_delivery = delivery_flags[index] if delivery_flags is not None and index < len(delivery_flags) else False
        if is_delivery or name.lower() == "delivery charge":
            key = DELIVERY_PRODUCT_KEY
        elif item.get("item_id"):
            key = f"zoho:{item['item_id']}"
        else:
            key = "name:" + re.sub(r"\s+", " ", name.casefold())
        quantity = _decimal(item.get("quantity"))
        unit_price = _decimal(item.get("rate"))
        lines.append(
            models.OrderLineFact(
                company_id=company_id,
                product_key=key[:255],
                product_name=name[:255],
                quantity=quantity,
                unit_price=unit_price,
                net_amount=(quantity * unit_price).quantize(_CENT, rounding=ROUND_HALF_UP),
            )
        )
    return lines


logger = logging.getLogger(__name__)

# exported_orders.status values. One row per Unify order (uq_unify_order_id); the row is
# updated in place, never duplicated and never deleted by Zconnect.
EXPORT_STATUS_IN_PROGRESS = "in_progress"  # claimed; a Zoho call may be underway
EXPORT_STATUS_EXPORTED = "exported"  # a Zoho invoice exists for this order
EXPORT_STATUS_FAILED = "failed"  # definitely no invoice from the last attempt
EXPORT_STATUS_SKIPPED = "skipped"  # not processed (preview status was not ready)
EXPORT_STATUS_RESET = "reset"  # soft reset by a user; may be synced again after a Zoho check
EXPORT_STATUS_NEEDS_VERIFICATION = "needs_verification"  # Zoho outcome unknown; never auto-retried
# May be claimed again by a sync (which always checks Zoho before creating).
RETRYABLE_EXPORT_STATUSES = (EXPORT_STATUS_FAILED, EXPORT_STATUS_SKIPPED, EXPORT_STATUS_RESET)
# Safety states: never claimed by a sync, never reset; only Zoho reconciliation resolves them.
SAFETY_EXPORT_STATUSES = (EXPORT_STATUS_IN_PROGRESS, EXPORT_STATUS_NEEDS_VERIFICATION)
# An in_progress claim older than this is treated as abandoned and may be reconciled.
STALE_IN_PROGRESS_AFTER = timedelta(minutes=30)
# Give Zoho time to list a just-created invoice before reconciling needs_verification.
MIN_VERIFICATION_AGE = timedelta(minutes=5)
_AUDIT_MESSAGE_LIMIT = 4000
_AUDIT_TIME_RE = re.compile(r"\[run \d+, (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")


def audit_text(message: str, sync_run_id: Optional[int], prior_status: Optional[str] = None, prior_message: Optional[str] = None) -> str:
    """Record message with run, UTC time and the previous status/message (audit trail)."""
    text = f"{message} [run {sync_run_id}, {datetime.utcnow().isoformat(timespec='seconds')}Z]"
    if prior_status:
        text += f" | previous status {prior_status}: {(prior_message or '').strip()}"
    return text[:_AUDIT_MESSAGE_LIMIT]


def latest_audit_time(message: Optional[str]) -> Optional[datetime]:
    """UTC time of the most recent audit entry in a record message, if present."""
    match = _AUDIT_TIME_RE.search(message or "")
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1))
    except ValueError:
        return None


@dataclass
class AuditContext:
    """Who/where an export event belongs to. tenant_id isolates event history per company
    (this deployment is single-tenant; see report). actor_id is the signed-in user when known.
    company_id is the signed-in user's company: it owns the sync run and its captured values."""

    tenant_id: str = "default"
    actor_id: Optional[str] = None
    company_id: str = models.DEFAULT_COMPANY_ID


_SECRET_RE = re.compile(
    # token after an auth scheme; key=value style secrets; any long token-like string
    r"((?:zoho-oauthtoken|bearer|basic)\s+)\S+"
    r"|((?:access_token|refresh_token|client_secret|client_id|password|api_key|apikey)\W{0,3})[^\s,\"'}&]+"
    r"|\b[A-Za-z0-9._\-]{32,}\b",
    re.IGNORECASE,
)


def safe_detail(text: Any, limit: int = 300) -> Optional[str]:
    """Short diagnostic for the audit log: credentials redacted, whitespace collapsed, capped."""
    if text is None:
        return None
    cleaned = _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + "[redacted]", str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit] or None


def zoho_diagnostic(reason: Any) -> Optional[str]:
    """Summarise a Zoho error without its response body (which may contain invoice data)."""
    text = str(reason or "")
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        parts = [str(data.get("error") or "Zoho error")]
        if data.get("status_code"):
            parts.append(f"HTTP {data.get('status_code')}")
        return safe_detail(" ".join(parts))
    return safe_detail(text, limit=160)


def add_export_event(
    db: Session,
    audit: "AuditContext",
    *,
    unify_order_id: str,
    event_type: str,
    previous_status: Optional[str] = None,
    new_status: Optional[str] = None,
    sync_run_id: Optional[int] = None,
    export_record_id: Optional[int] = None,
    zoho_invoice_id: Optional[str] = None,
    detail: Any = None,
) -> None:
    """Stage one append-only audit event; the caller commits it with the state change."""
    db.add(
        models.ExportEvent(
            tenant_id=(audit.tenant_id or "default")[:64],
            unify_order_id=unify_order_id,
            export_record_id=export_record_id,
            sync_run_id=sync_run_id,
            event_type=event_type,
            previous_status=previous_status,
            new_status=new_status,
            zoho_invoice_id=zoho_invoice_id,
            actor_id=(audit.actor_id or None) and audit.actor_id[:128],
            detail=safe_detail(detail),
        )
    )


def list_export_events(db: Session, tenant_id: str, unify_order_id: Optional[str] = None) -> List[models.ExportEvent]:
    """Events for one tenant (optionally one order), oldest first. Always tenant-scoped."""
    query = select(models.ExportEvent).where(models.ExportEvent.tenant_id == tenant_id)
    if unify_order_id is not None:
        query = query.where(models.ExportEvent.unify_order_id == unify_order_id)
    return list(db.execute(query.order_by(models.ExportEvent.id)).scalars().all())


class ReconcileError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _ClaimOwnershipLost(Exception):
    pass


def soft_reset_export_records(
    db: Session,
    records: List[models.ExportedOrder],
    source: str,
    audit: Optional["AuditContext"] = None,
) -> Dict[str, List[str]]:
    """Mark export records as reset without deleting anything.

    Invoice ids, exported_invoices rows, sync_run totals and the record history are kept.
    in_progress / needs_verification records are never reset (they need Zoho reconciliation).
    Each update is conditional on the status read, so a concurrent change is never overwritten.
    """
    audit = audit or AuditContext()
    outcome: Dict[str, List[str]] = {"reset": [], "already_reset": [], "blocked": []}
    for record in records:
        current_status = record.status
        if current_status in SAFETY_EXPORT_STATUSES:
            outcome["blocked"].append(record.unify_order_id)
            add_export_event(
                db,
                audit,
                unify_order_id=record.unify_order_id,
                event_type="reset_refused",
                previous_status=current_status,
                new_status=current_status,
                sync_run_id=record.sync_run_id,
                export_record_id=record.id,
                detail=f"Reset refused ({source}): order needs a Zoho check first",
            )
            continue
        if current_status == EXPORT_STATUS_RESET:
            outcome["already_reset"].append(record.unify_order_id)
            continue
        result = db.execute(
            update(models.ExportedOrder)
            .where(models.ExportedOrder.id == record.id, models.ExportedOrder.status == current_status)
            .values(
                status=EXPORT_STATUS_RESET,
                message=audit_text(f"Reset by user ({source}); the next sync will check Zoho before creating an invoice", record.sync_run_id, current_status, record.message),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            outcome["reset"].append(record.unify_order_id)
            add_export_event(
                db,
                audit,
                unify_order_id=record.unify_order_id,
                event_type="reset",
                previous_status=current_status,
                new_status=EXPORT_STATUS_RESET,
                sync_run_id=record.sync_run_id,
                export_record_id=record.id,
                detail=f"Soft reset ({source})",
            )
        else:
            outcome["blocked"].append(record.unify_order_id)
    db.commit()
    db.expire_all()
    return outcome


@dataclass
class _ClaimResult:
    claimed: bool
    prior_status: Optional[str] = None
    prior_message: Optional[str] = None
    current_status: Optional[str] = None
    token: Optional[str] = None
    record_id: Optional[int] = None


@dataclass
class _OrderAttemptState:
    order_id: str = ""
    customer_name: str = ""
    claimed: bool = False
    claim_token: Optional[str] = None
    export_record_id: Optional[int] = None
    prior_status: Optional[str] = None
    prior_message: Optional[str] = None
    zoho_create_started: bool = False
    zoho_invoice_id: Optional[str] = None


ZOHO_STANDARD_TAX_ID = "229622000000038161"
ZOHO_REDUCED_TAX_ID = "229622000000038157"
ZOHO_ZERO_TAX_ID = "229622000000038165"

BAKERY_TAX_KEYWORDS = (
    "brownie",
    "cake",
    "cheesecake",
    "challah",
    "cookie",
    "cookies",
    "flapjack",
    "flapjacks",
    "loaf",
    "loafs",
    "loaves",
    "square",
    "squares",
    "bread",
    "bun",
    "buns",
    "muffin",
    "muffins",
    "pastry",
    "pastries",
    "scone",
    "scones",
    "biscoff",
    "cinnamon roll",
    "cream cheese frosting",
)
ZOHO_INVOICE_PAYMENT_GATEWAY_NAMES = ("stripe", "gocardless")

EXEMPT_TAX_KEYWORDS = (
    "tax exempt",
    "tax-exempt",
    "exempt",
    "zero rated",
    "zerorated",
    "zero-rated",
    "vat exempt",
    "vat-exempt",
)


def resolve_tax_id_for_product(name: str, line_type: str | None = None, tax_percentage: float | None = None) -> str:
    normalized_name = " ".join((name or "").lower().split())
    normalized_line_type = (line_type or "").strip().lower()

    if tax_percentage is not None:
        try:
            explicit_rate = float(tax_percentage)
        except (TypeError, ValueError):
            explicit_rate = None
        else:
            if explicit_rate <= 0:
                return ZOHO_ZERO_TAX_ID
            if abs(explicit_rate - 13.5) < 0.51:
                return ZOHO_REDUCED_TAX_ID
            if explicit_rate >= 20:
                return ZOHO_STANDARD_TAX_ID

    if normalized_line_type == "delivery":
        return ZOHO_STANDARD_TAX_ID

    if any(keyword in normalized_name for keyword in EXEMPT_TAX_KEYWORDS):
        return ZOHO_ZERO_TAX_ID
    if any(keyword in normalized_name for keyword in BAKERY_TAX_KEYWORDS):
        return ZOHO_REDUCED_TAX_ID
    return ZOHO_STANDARD_TAX_ID


def resolve_tax_id(rate: float) -> str:
    return resolve_tax_id_for_product("", tax_percentage=rate)


class ExportService:
    def __init__(self, unify_service: UnifyService, zoho_service: ZohoService):
        self.unify_service = unify_service
        self.zoho_service = zoho_service
        # Set by the caller (route) per request; defaults keep tests and scripts working.
        self.audit = AuditContext()

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

    def _pick_meaningful_customer_label(self, *candidates: Any) -> Optional[str]:
        for candidate in candidates:
            text = self._clean_text(candidate)
            if text and self._is_meaningful_customer_label(text):
                return text
        return None

    def _is_meaningful_label(self, value: Optional[str]) -> bool:
        if not value:
            return False
        text = value.strip()
        if not text:
            return False
        return not text.isdigit()

    def _fallback_product_label(self, line: UnifyOrder) -> str:
        for candidate in (getattr(line, "unify_product_key", None), line.product_id, line.item_sku):
            text = self._clean_text(candidate)
            if text:
                return f"Product {text}"
        return "Product unknown"

    def _normalize_key_fallback(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    def _resolve_unify_product_key(self, line: Any, resolved_name: Optional[str] = None) -> Optional[str]:
        for candidate in (
            getattr(line, "unify_product_key", None),
            getattr(line, "product_modification_id", None),
            getattr(line, "external_product_modification_id", None),
            getattr(line, "product_id", None),
            getattr(line, "external_product_id", None),
            getattr(line, "item_sku", None),
        ):
            text = self._clean_text(candidate)
            if text:
                return text
        if resolved_name:
            return self._normalize_key_fallback(resolved_name)
        return None

    def _resolve_line_name(self, line: Any) -> str:
        candidate_name = self._clean_text(getattr(line, "item_name", None))
        if candidate_name and self._is_meaningful_label(candidate_name):
            return candidate_name
        return self._fallback_product_label(line)

    def _resolve_line_tax_id(self, line: Any, resolved_name: str) -> str:
        tax_percentage = getattr(line, "tax_percentage", None)
        tax_id = resolve_tax_id_for_product(resolved_name, getattr(line, "line_type", None), tax_percentage)
        logger.info(
            "Resolved Zoho tax order_line_name=%s line_type=%s tax_percentage=%s tax_id=%s",
            resolved_name,
            getattr(line, "line_type", None),
            tax_percentage,
            tax_id,
        )
        return tax_id

    def _resolve_line_price(self, line: Any) -> float:
        try:
            return float(getattr(line, "price", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _get_item_mapping(self, db: Session, unify_product_key: str) -> Optional[models.ZohoItemMapping]:
        mapping = (
            db.execute(
                select(models.ZohoItemMapping).where(models.ZohoItemMapping.unify_product_key == unify_product_key)
            ).scalar_one_or_none()
        )
        if mapping:
            logger.info(
                "Zoho item cache hit unify_product_key=%s zoho_item_id=%s zoho_item_name=%s tax_id=%s",
                unify_product_key,
                mapping.zoho_item_id,
                mapping.zoho_item_name,
                mapping.tax_id,
            )
        else:
            logger.info("Zoho item cache miss unify_product_key=%s", unify_product_key)
        return mapping

    def _upsert_item_mapping(
        self,
        db: Session,
        *,
        unify_product_key: str,
        unify_product_name: str,
        zoho_item_id: str,
        zoho_item_name: str,
        tax_id: str,
    ) -> None:
        existing = (
            db.execute(
                select(models.ZohoItemMapping).where(models.ZohoItemMapping.unify_product_key == unify_product_key)
            ).scalar_one_or_none()
        )
        if existing:
            existing.unify_product_name = unify_product_name
            existing.zoho_item_id = zoho_item_id
            existing.zoho_item_name = zoho_item_name
            existing.tax_id = tax_id
            existing.updated_at = datetime.utcnow()
            logger.info(
                "Zoho item cache updated unify_product_key=%s zoho_item_id=%s zoho_item_name=%s tax_id=%s",
                unify_product_key,
                zoho_item_id,
                zoho_item_name,
                tax_id,
            )
        else:
            db.add(
                models.ZohoItemMapping(
                    unify_product_key=unify_product_key,
                    unify_product_name=unify_product_name,
                    zoho_item_id=zoho_item_id,
                    zoho_item_name=zoho_item_name,
                    tax_id=tax_id,
                )
            )
            logger.info(
                "Zoho item cache stored unify_product_key=%s zoho_item_id=%s zoho_item_name=%s tax_id=%s",
                unify_product_key,
                zoho_item_id,
                zoho_item_name,
                tax_id,
            )
        db.commit()

    async def _resolve_zoho_item_payload_for_line(
        self,
        db: Session,
        order: UnifyOrder,
        line: Any,
    ) -> Dict[str, Any]:
        raw_name = self._clean_text(getattr(line, "item_name", None))
        resolved_name = self._resolve_line_name(line)
        resolved_tax_id = self._resolve_line_tax_id(line, resolved_name)
        resolved_rate = self._resolve_line_price(line)
        resolved_quantity = float(getattr(line, "quantity", 0.0) or 0.0)
        resolved_key = self._resolve_unify_product_key(line, resolved_name)
        lookup_result = "not_attempted"
        zoho_item_created = False
        fallback_to_direct_line = False
        final_payload: Dict[str, Any]

        if getattr(line, "line_type", None) != "delivery" and not self._is_meaningful_label(raw_name) and not self._is_meaningful_label(resolved_name):
            lookup_result = "skipped_non_meaningful_name"
            fallback_to_direct_line = True
            final_payload = {
                "name": resolved_name,
                "quantity": resolved_quantity,
                "rate": resolved_rate,
                "tax_id": resolved_tax_id,
            }
            logger.info(
                "Zoho line resolution order_id=%s product_name=%s unify_product_key=%s resolved_tax_id=%s zoho_item_lookup_result=%s zoho_item_created=%s fallback_to_direct_line=%s",
                order.order_id,
                resolved_name,
                resolved_key,
                resolved_tax_id,
                lookup_result,
                zoho_item_created,
                fallback_to_direct_line,
            )
            logger.info(
                "Zoho line payload order_id=%s line_name=%s zoho_item_id=%s uses_item_id=%s fallback_to_direct_line=%s payload=%s",
                order.order_id,
                resolved_name,
                None,
                False,
                fallback_to_direct_line,
                final_payload,
            )
            return final_payload

        mapping = self._get_item_mapping(db, resolved_key)
        if mapping and mapping.zoho_item_id:
            lookup_result = "cache_hit"
            final_payload = {
                "name": resolved_name,
                "item_id": mapping.zoho_item_id,
                "quantity": resolved_quantity,
                "rate": resolved_rate,
                "tax_id": resolved_tax_id,
            }
            logger.info(
                "Resolved Zoho item from local cache order_id=%s unify_product_key=%s zoho_item_id=%s",
                order.order_id,
                resolved_key,
                mapping.zoho_item_id,
            )
            logger.info(
                "Zoho line resolution order_id=%s product_name=%s unify_product_key=%s resolved_tax_id=%s zoho_item_lookup_result=%s zoho_item_created=%s fallback_to_direct_line=%s",
                order.order_id,
                resolved_name,
                resolved_key,
                resolved_tax_id,
                lookup_result,
                zoho_item_created,
                fallback_to_direct_line,
            )
            logger.info(
                "Zoho line payload order_id=%s line_name=%s zoho_item_id=%s uses_item_id=%s fallback_to_direct_line=%s payload=%s",
                order.order_id,
                resolved_name,
                mapping.zoho_item_id,
                True,
                fallback_to_direct_line,
                final_payload,
            )
            return final_payload

        try:
            logger.info(
                "Looking up Zoho item order_id=%s unify_product_key=%s resolved_name=%s",
                order.order_id,
                resolved_key,
                resolved_name,
            )
            zoho_item_id = await self.zoho_service.find_item_by_name(resolved_name)
            if zoho_item_id:
                lookup_result = "zoho_item_found"
                final_payload = {
                    "name": resolved_name,
                    "item_id": zoho_item_id,
                    "quantity": resolved_quantity,
                    "rate": resolved_rate,
                    "tax_id": resolved_tax_id,
                }
                logger.info(
                    "Zoho item found order_id=%s unify_product_key=%s resolved_name=%s zoho_item_id=%s",
                    order.order_id,
                    resolved_key,
                    resolved_name,
                    zoho_item_id,
                )
                self._upsert_item_mapping(
                    db,
                    unify_product_key=resolved_key,
                    unify_product_name=resolved_name,
                    zoho_item_id=zoho_item_id,
                    zoho_item_name=resolved_name,
                    tax_id=resolved_tax_id,
                )
                logger.info(
                    "Zoho line resolution order_id=%s product_name=%s unify_product_key=%s resolved_tax_id=%s zoho_item_lookup_result=%s zoho_item_created=%s fallback_to_direct_line=%s",
                    order.order_id,
                    resolved_name,
                    resolved_key,
                    resolved_tax_id,
                    lookup_result,
                    zoho_item_created,
                    fallback_to_direct_line,
                )
                logger.info(
                    "Zoho line payload order_id=%s line_name=%s zoho_item_id=%s uses_item_id=%s fallback_to_direct_line=%s payload=%s",
                    order.order_id,
                    resolved_name,
                    zoho_item_id,
                    True,
                    fallback_to_direct_line,
                    final_payload,
                )
                return final_payload

            logger.info(
                "Zoho item not found order_id=%s unify_product_key=%s resolved_name=%s; creating item",
                order.order_id,
                resolved_key,
                resolved_name,
            )
            zoho_item_id = await self.zoho_service.create_item(resolved_name, resolved_rate, resolved_tax_id)
            lookup_result = "zoho_item_not_found"
            zoho_item_created = True
            final_payload = {
                "name": resolved_name,
                "item_id": zoho_item_id,
                "quantity": resolved_quantity,
                "rate": resolved_rate,
                "tax_id": resolved_tax_id,
            }
            logger.info(
                "Zoho item created order_id=%s unify_product_key=%s resolved_name=%s zoho_item_id=%s",
                order.order_id,
                resolved_key,
                resolved_name,
                zoho_item_id,
            )
            self._upsert_item_mapping(
                db,
                unify_product_key=resolved_key,
                unify_product_name=resolved_name,
                zoho_item_id=zoho_item_id,
                zoho_item_name=resolved_name,
                tax_id=resolved_tax_id,
            )
            logger.info(
                "Zoho line resolution order_id=%s product_name=%s unify_product_key=%s resolved_tax_id=%s zoho_item_lookup_result=%s zoho_item_created=%s fallback_to_direct_line=%s",
                order.order_id,
                resolved_name,
                resolved_key,
                resolved_tax_id,
                lookup_result,
                zoho_item_created,
                fallback_to_direct_line,
            )
            logger.info(
                "Zoho line payload order_id=%s line_name=%s zoho_item_id=%s uses_item_id=%s fallback_to_direct_line=%s payload=%s",
                order.order_id,
                resolved_name,
                zoho_item_id,
                True,
                fallback_to_direct_line,
                final_payload,
            )
            return final_payload
        except ZohoServiceError as exc:
            lookup_result = "zoho_item_error"
            fallback_to_direct_line = True
            logger.warning(
                "Zoho item resolution failed order_id=%s unify_product_key=%s resolved_name=%s reason=%s; falling back to direct line item",
                order.order_id,
                resolved_key,
                resolved_name,
                exc,
            )
        except Exception as exc:
            lookup_result = "zoho_item_error"
            fallback_to_direct_line = True
            logger.warning(
                "Zoho item resolution error order_id=%s unify_product_key=%s resolved_name=%s reason=%s; falling back to direct line item",
                order.order_id,
                resolved_key,
                resolved_name,
                exc,
            )

        logger.info(
            "Zoho line resolution order_id=%s product_name=%s unify_product_key=%s resolved_tax_id=%s zoho_item_lookup_result=%s zoho_item_created=%s fallback_to_direct_line=%s",
            order.order_id,
            resolved_name,
            resolved_key,
            resolved_tax_id,
            lookup_result,
            zoho_item_created,
            fallback_to_direct_line,
        )
        final_payload = {
            "name": resolved_name,
            "quantity": resolved_quantity,
            "rate": resolved_rate,
            "tax_id": resolved_tax_id,
        }
        logger.info(
            "Zoho line payload order_id=%s line_name=%s zoho_item_id=%s uses_item_id=%s fallback_to_direct_line=%s payload=%s",
            order.order_id,
            resolved_name,
            None,
            False,
            fallback_to_direct_line,
            final_payload,
        )
        return final_payload

    def _order_exists(self, db: Session, unify_order_id: str) -> bool:
        existing = db.execute(select(models.ExportedOrder).where(models.ExportedOrder.unify_order_id == unify_order_id)).scalar_one_or_none()
        return existing is not None

    def _buyer_key(self, order: UnifyOrder) -> str:
        return (order.buyer_id or order.order_id).strip()

    def _resolve_invoice_contact_name(self, order: UnifyOrder) -> Optional[str]:
        return self._pick_meaningful_customer_label(
            order.buyer_name,
            order.customer_name,
            f"Customer {order.buyer_id}" if order.buyer_id else None,
        )

    def _resolve_customer_display_name(self, order: UnifyOrder) -> str:
        resolved = self._resolve_invoice_contact_name(order)
        if resolved:
            return resolved
        if order.buyer_id:
            return f"Customer {order.buyer_id}"
        return order.order_id

    def _normalize_customer_name(self, value: Any) -> str:
        text = self._clean_text(value)
        if not text:
            return ""
        return " ".join(text.split()).lower()

    def _extract_zoho_contact_name(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else payload
        if not isinstance(contact, dict):
            return None
        for key in ("contact_name", "display_name", "displayName", "name"):
            text = self._clean_text(contact.get(key))
            if text:
                return text
        return None

    def _needs_contact_name_refresh(self, current_name: Optional[str], desired_name: str) -> bool:
        desired_text = self._clean_text(desired_name)
        if not desired_text:
            return False
        current_text = self._clean_text(current_name)
        if not current_text:
            return True
        if not self._is_meaningful_customer_label(current_text):
            return True
        return self._normalize_customer_name(current_text) != self._normalize_customer_name(desired_text)

    def _log_customer_resolution(self, order: UnifyOrder, resolved_name: str, contact_name: str) -> None:
        logger.info(
            "Customer resolution order_id=%s buyer_id=%s raw_customer_name=%s raw_buyer_name=%s final_resolved_customer_display_name=%s zoho_contact_name=%s",
            order.order_id,
            order.buyer_id,
            order.customer_name,
            order.buyer_name,
            resolved_name,
            contact_name,
        )

    def _resolve_invoice_date(self, order: UnifyOrder) -> Optional[str]:
        return self._clean_text(order.delivery_date or order.order_date)

    def build_invoice_notes(self, order: UnifyOrder) -> str:
        storage_note = None
        for candidate in (
            getattr(order, "storage_note", None),
            getattr(order, "storage_instruction", None),
            getattr(order, "storage_instructions", None),
            getattr(order, "storageInstructions", None),
        ):
            storage_note = self._clean_text(candidate)
            if storage_note:
                break

        lines: List[str] = []
        if storage_note:
            lines.append(f"Storage: {storage_note}")
            lines.append("")

        lines.append("VAT IE9802711J")
        return "\n".join(lines)

    def _format_exception_message(self, exc: BaseException | None) -> str:
        if exc is None:
            return "Export failed before finalization"
        message = str(exc).strip()
        return message or exc.__class__.__name__

    def _persist_failed_sync_run(
        self,
        db: Session,
        sync_run_id: int,
        *,
        date_from: str,
        date_to: str,
        selected_order_ids: List[str],
        error_message: str,
        total_orders: int,
        total_customers: int,
        total_invoices: int,
        error_messages: List[str],
    ) -> None:
        db.rollback()
        sync_run = db.get(models.SyncRun, sync_run_id)
        if sync_run is None:
            logger.error(
                "Unable to persist failed export state because sync run %s no longer exists date_from=%s date_to=%s selected_order_ids=%s error=%s",
                sync_run_id,
                date_from,
                date_to,
                selected_order_ids,
                error_message,
            )
            return

        sync_run.status = "failed"
        sync_run.finished_at = datetime.utcnow()
        sync_run.total_orders = total_orders
        sync_run.total_customers = total_customers
        sync_run.total_invoices = total_invoices

        combined_errors = list(error_messages)
        if error_message:
            combined_errors.append(error_message)
        sync_run.errors = "\n".join([item for item in combined_errors if item]) or error_message

        db.add(sync_run)
        db.commit()

    def _validate_invoice_payload(
        self,
        order: UnifyOrder,
        *,
        contact_name: Optional[str],
        invoice_date: Optional[str],
        line_items: List[Dict[str, Any]],
        line_errors: List[str],
    ) -> List[str]:
        errors: List[str] = []
        if not self._clean_text(order.order_id):
            errors.append("Missing order_id")
        if not self._clean_text(contact_name):
            errors.append("Missing contact name")
        if not self._clean_text(invoice_date):
            errors.append("Missing invoice date")
        if not line_items:
            errors.append("Missing line items")

        for index, line in enumerate(line_items, start=1):
            name = self._clean_text(line.get("name"))
            if not name:
                errors.append(f"Missing line name at position {index}")
            try:
                quantity = float(line.get("quantity"))
            except (TypeError, ValueError):
                quantity = -1
            if quantity <= 0:
                errors.append(f"Invalid quantity for line {index}")
            try:
                rate = float(line.get("rate"))
            except (TypeError, ValueError):
                rate = -1
            if rate < 0:
                errors.append(f"Invalid rate for line {index}")
            if line.get("tax_id") in (None, ""):
                errors.append("Missing Zoho tax configuration for one or more invoice lines")

        errors.extend(line_errors)
        return errors

    async def _build_line_items(self, db: Session, order: UnifyOrder) -> Tuple[List[Dict[str, Any]], List[str]]:
        line_items = []
        skipped_messages: List[str] = []
        delivery_line_present = False
        logger.info("Zoho item resolution enabled for order %s with mandatory direct-line fallback", order.order_id)
        item_ids_used: List[str] = []
        tax_ids_used: List[str] = []

        for line in order.lines:
            if line.quantity <= 0:
                skipped_messages.append(f"invalid quantity for {line.item_sku or line.item_name}")
                logger.warning("Skipped invalid item for order %s: quantity <= 0 (%s)", order.order_id, line.item_sku or line.item_name)
                continue
            if line.price is None or line.price < 0:
                skipped_messages.append(f"invalid price for {line.item_sku or line.item_name}")
                logger.warning("Skipped invalid item for order %s: missing/invalid price (%s)", order.order_id, line.item_sku or line.item_name)
                continue
            if line.line_type == "delivery":
                delivery_line_present = True
            resolved_name = self._resolve_line_name(line)
            try:
                payload_line = await self._resolve_zoho_item_payload_for_line(db, order, line)
            except Exception as exc:
                logger.warning(
                    "Unexpected error while resolving Zoho line order_id=%s line_name=%s reason=%s; falling back to direct line item",
                    order.order_id,
                    resolved_name,
                    exc,
                )
                resolved_tax_id = self._resolve_line_tax_id(line, resolved_name)
                payload_line = {
                    "name": resolved_name,
                    "quantity": line.quantity,
                    "rate": line.price,
                    "tax_id": resolved_tax_id,
                }

            if payload_line.get("item_id"):
                item_ids_used.append(str(payload_line.get("item_id")))
                logger.info(
                    "Using Zoho item-backed line order_id=%s line_name=%s item_id=%s tax_id=%s payload=%s",
                    order.order_id,
                    resolved_name,
                    payload_line.get("item_id"),
                    payload_line.get("tax_id"),
                    payload_line,
                )
            else:
                logger.info(
                    "Using direct Zoho line fallback order_id=%s line_name=%s tax_id=%s payload=%s",
                    order.order_id,
                    resolved_name,
                    payload_line.get("tax_id"),
                    payload_line,
                )

            if payload_line.get("tax_id"):
                tax_ids_used.append(str(payload_line.get("tax_id")))
            line_items.append(payload_line)

        if not delivery_line_present and order.total_delivery_fee > 0:
            tax_id = resolve_tax_id_for_product("Delivery charge", "delivery", 23.0)
            line_items.append(
                {
                    "name": "Delivery charge",
                    "quantity": 1,
                    "rate": order.total_delivery_fee,
                    "tax_id": tax_id,
                }
            )
            tax_ids_used.append(str(tax_id))
            logger.info(
                "Applied Zoho tax resolver order_id=%s line_name=%s line_type=%s tax_rate=%s tax_id=%s",
                order.order_id,
                "Delivery charge",
                "delivery",
                23,
                tax_id,
            )
            logger.info(
                "Using direct Zoho line fallback order_id=%s line_name=%s reason=%s",
                order.order_id,
                "Delivery charge",
                "missing explicit delivery item",
            )

        logger.info(
            "Zoho line summary order_id=%s item_ids_used=%s tax_ids_used=%s fallback_to_direct_line=%s",
            order.order_id,
            item_ids_used,
            tax_ids_used,
            any("item_id" not in line for line in line_items),
        )
        return line_items, skipped_messages

    async def _resolve_contact_for_order(self, db: Session, order: UnifyOrder, contact_name: str) -> str:
        buyer_key = self._buyer_key(order)
        lookup_query = self._clean_text(contact_name) or f"Customer {buyer_key}"
        existing_contact_found = False
        existing_contact_name = None
        contact_updated = False
        contact = db.execute(select(models.CustomerMapping).where(models.CustomerMapping.unify_customer_name == buyer_key)).scalar_one_or_none()
        if contact:
            contact_id = contact.zoho_contact_id
            existing_contact = await self.zoho_service.get_contact_by_id(contact_id)
            existing_contact_found = existing_contact is not None
            existing_contact_name = self._extract_zoho_contact_name(existing_contact)
            if self._needs_contact_name_refresh(existing_contact_name, lookup_query):
                logger.info(
                    "Refreshing cached Zoho contact name order_id=%s buyer_id=%s contact_id=%s existing_contact_name=%s desired_contact_name=%s",
                    order.order_id,
                    buyer_key,
                    contact_id,
                    existing_contact_name,
                    lookup_query,
                )
                contact_updated = await self.zoho_service.update_contact_name(contact_id, lookup_query)
                if contact_updated:
                    existing_contact_name = lookup_query
                    logger.info(
                        "Updated cached Zoho contact name order_id=%s buyer_id=%s contact_id=%s final_contact_name=%s",
                        order.order_id,
                        buyer_key,
                        contact_id,
                        lookup_query,
                    )
                    logger.info(
                        "Zoho contact resolution order_id=%s buyer_id=%s resolved_customer_name=%s contact_lookup_query=%s existing_contact_found=%s existing_contact_name=%s contact_updated=%s final_contact_name=%s customer_id=%s",
                        order.order_id,
                        buyer_key,
                        lookup_query,
                        lookup_query,
                        existing_contact_found,
                        existing_contact_name,
                        contact_updated,
                        lookup_query,
                        contact_id,
                    )
                    return contact_id
                logger.warning(
                    "Cached Zoho contact rename failed order_id=%s buyer_id=%s contact_id=%s existing_contact_name=%s desired_contact_name=%s; recreating contact",
                    order.order_id,
                    buyer_key,
                    contact_id,
                    existing_contact_name,
                    lookup_query,
                )
                contact = None
            else:
                logger.info(
                    "Resolved Zoho contact for order %s from cache buyer_key=%s contact_name=%s",
                    order.order_id,
                    buyer_key,
                    lookup_query,
                )
                logger.info(
                    "Zoho contact resolution order_id=%s buyer_id=%s resolved_customer_name=%s contact_lookup_query=%s existing_contact_found=%s existing_contact_name=%s contact_updated=%s final_contact_name=%s customer_id=%s",
                    order.order_id,
                    buyer_key,
                    lookup_query,
                    lookup_query,
                    existing_contact_found,
                    existing_contact_name,
                    contact_updated,
                    existing_contact_name or lookup_query,
                    contact_id,
                )
                return contact_id

        logger.info(
            "Resolving Zoho contact for order %s buyer_key=%s contact_name=%s",
            order.order_id,
            buyer_key,
            lookup_query,
        )
        existing = await self.zoho_service.find_contact_by_name(lookup_query)
        existing_contact_found = existing is not None
        if existing:
            contact_id = existing
            existing_contact = await self.zoho_service.get_contact_by_id(contact_id)
            existing_contact_name = self._extract_zoho_contact_name(existing_contact)
            logger.info(
                "Reused existing Zoho contact for order %s buyer_key=%s contact_name=%s contact_id=%s",
                order.order_id,
                buyer_key,
                lookup_query,
                contact_id,
            )
            if self._needs_contact_name_refresh(existing_contact_name, lookup_query):
                contact_updated = await self.zoho_service.update_contact_name(contact_id, lookup_query)
                if contact_updated:
                    existing_contact_name = lookup_query
                    logger.info(
                        "Updated existing Zoho contact for order %s buyer_key=%s contact_id=%s new_contact_name=%s",
                        order.order_id,
                        buyer_key,
                        contact_id,
                        lookup_query,
                    )
                else:
                    logger.warning(
                        "Existing Zoho contact update failed order_id=%s buyer_id=%s contact_id=%s existing_contact_name=%s desired_contact_name=%s; creating replacement contact",
                        order.order_id,
                        buyer_key,
                        contact_id,
                        existing_contact_name,
                        lookup_query,
                    )
                    contact_id = await self.zoho_service.create_contact(lookup_query)
                    existing_contact_name = lookup_query
                    contact_updated = False
                    existing_contact_found = False
        else:
            contact_id = await self.zoho_service.create_contact(lookup_query)
            existing_contact_name = None
            logger.info(
                "Created new Zoho contact for order %s buyer_key=%s contact_name=%s contact_id=%s",
                order.order_id,
                buyer_key,
                lookup_query,
                contact_id,
            )

        db_contact = db.execute(select(models.CustomerMapping).where(models.CustomerMapping.unify_customer_name == buyer_key)).scalar_one_or_none()
        if db_contact:
            db_contact.zoho_contact_id = contact_id
        else:
            db_contact = models.CustomerMapping(unify_customer_name=buyer_key, zoho_contact_id=contact_id)
            db.add(db_contact)
        db.commit()
        logger.info(
            "Zoho contact resolution order_id=%s buyer_id=%s resolved_customer_name=%s contact_lookup_query=%s existing_contact_found=%s existing_contact_name=%s contact_updated=%s final_contact_name=%s customer_id=%s",
            order.order_id,
            buyer_key,
            lookup_query,
            lookup_query,
            existing_contact_found,
            existing_contact_name,
            contact_updated,
            existing_contact_name or lookup_query,
            contact_id,
        )
        return contact_id

    async def _build_draft_invoice_payload(self, order: UnifyOrder, contact_id: str, line_items: List[Dict[str, Any]], invoice_date: str, contact_name: str) -> Dict[str, Any]:
        payload = {
            "customer_id": contact_id,
            "reference_number": order.order_id,
            "date": invoice_date,
            "line_items": line_items,
            "notes": self.build_invoice_notes(order),
            "is_inclusive_tax": False,
            "status": "draft",
            "payment_options": {
                "payment_gateways": [
                    {"gateway_name": gateway_name, "configured": True}
                    for gateway_name in ZOHO_INVOICE_PAYMENT_GATEWAY_NAMES
                ]
            },
        }

        contact_person_ids = await self.zoho_service.get_contact_person_ids(contact_id)
        if contact_person_ids:
            payload["contact_persons"] = contact_person_ids
        logger.info(
            "Zoho invoice payment/contact options order_id=%s payment_gateways=%s contact_person_ids=%s "
            "note=GoCardless is not in Zoho's officially documented invoice payment_gateway values; verify manually if it does not apply",
            order.order_id,
            ZOHO_INVOICE_PAYMENT_GATEWAY_NAMES,
            contact_person_ids,
        )

        logger.info(
            "Built Zoho draft invoice payload order_id=%s contact_name=%s customer_id=%s line_count=%s date=%s has_delivery_address=%s",
            order.order_id,
            contact_name,
            contact_id,
            len(line_items),
            invoice_date,
            bool(order.delivery_address),
        )
        return payload

    # ------------------------------------------------------------------
    # Idempotent per-order export state (exported_orders)
    # ------------------------------------------------------------------

    def _get_export_record(self, db: Session, unify_order_id: str) -> Optional[models.ExportedOrder]:
        return db.execute(
            select(models.ExportedOrder).where(models.ExportedOrder.unify_order_id == unify_order_id)
        ).scalar_one_or_none()

    def _audit_message(self, message: str, sync_run_id: int, prior_status: Optional[str] = None, prior_message: Optional[str] = None) -> str:
        return audit_text(message, sync_run_id, prior_status, prior_message)

    def _claim_order(self, db: Session, sync_run_id: int, order_id: str, customer_name: str) -> _ClaimResult:
        """Atomically mark the order as in_progress before any Zoho call.

        New orders are claimed by INSERT (uq_unify_order_id rejects a concurrent claimer).
        Existing failed / skipped / reset rows are claimed by a conditional UPDATE that only
        succeeds while the row still has that status. Every other status is not claimable.
        The row keeps its original sync_run_id (history); ownership is tracked by a claim
        token written into the message, so later writes only apply while we still own it.
        """
        token = uuid.uuid4().hex[:16]
        claim_text = f"Export attempt started (claim {token})"
        existing = self._get_export_record(db, order_id)
        if existing is None:
            record = models.ExportedOrder(
                sync_run_id=sync_run_id,
                unify_order_id=order_id,
                customer_name=customer_name,
                status=EXPORT_STATUS_IN_PROGRESS,
                message=audit_text(claim_text, sync_run_id),
            )
            db.add(record)
            try:
                db.flush()
                add_export_event(
                    db,
                    self.audit,
                    unify_order_id=order_id,
                    event_type="claimed",
                    previous_status=None,
                    new_status=EXPORT_STATUS_IN_PROGRESS,
                    sync_run_id=sync_run_id,
                    export_record_id=record.id,
                    detail="New order claimed for export",
                )
                db.commit()
            except IntegrityError:
                db.rollback()
                current = self._get_export_record(db, order_id)
                logger.warning("Order %s was claimed concurrently by another request", order_id)
                return _ClaimResult(False, current_status=current.status if current else None, record_id=current.id if current else None)
            return _ClaimResult(True, token=token, record_id=record.id)

        prior_status = existing.status
        prior_message = existing.message
        record_id = existing.id
        if prior_status not in RETRYABLE_EXPORT_STATUSES:
            return _ClaimResult(False, current_status=prior_status, record_id=record_id)

        result = db.execute(
            update(models.ExportedOrder)
            .where(
                models.ExportedOrder.unify_order_id == order_id,
                models.ExportedOrder.status == prior_status,
            )
            .values(
                status=EXPORT_STATUS_IN_PROGRESS,
                message=audit_text(claim_text, sync_run_id, prior_status, prior_message),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            db.expire_all()
            current = self._get_export_record(db, order_id)
            logger.warning("Order %s retry claim lost to a concurrent request", order_id)
            return _ClaimResult(False, current_status=current.status if current else None, record_id=record_id)
        add_export_event(
            db,
            self.audit,
            unify_order_id=order_id,
            event_type="claimed",
            previous_status=prior_status,
            new_status=EXPORT_STATUS_IN_PROGRESS,
            sync_run_id=sync_run_id,
            export_record_id=record_id,
            detail=f"Order claimed for export after status {prior_status}",
        )
        db.commit()
        db.expire_all()
        return _ClaimResult(True, prior_status=prior_status, prior_message=prior_message, token=token, record_id=record_id)

    def _owned_row(self, state: _OrderAttemptState):
        """WHERE clause matching the record only while this attempt still owns its claim."""
        return (
            models.ExportedOrder.unify_order_id == state.order_id,
            models.ExportedOrder.status == EXPORT_STATUS_IN_PROGRESS,
            models.ExportedOrder.message.like(f"%(claim {state.claim_token})%"),
        )

    def _finalize_claim(self, db: Session, sync_run_id: int, state: _OrderAttemptState, status: str, message: str) -> Dict[str, Any]:
        """Best-effort update of the claimed row. If the write fails the row stays in_progress,
        which blocks further attempts until it is reconciled against Zoho (safe)."""
        detail: Dict[str, Any] = {"order_id": state.order_id, "status": status, "message": message}
        try:
            result = db.execute(
                update(models.ExportedOrder)
                .where(*self._owned_row(state))
                .values(status=status, message=self._audit_message(message, sync_run_id, state.prior_status, state.prior_message))
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                logger.critical(
                    "Export record changed while order %s was being processed; outcome %s not recorded: %s",
                    state.order_id,
                    status,
                    message,
                )
                detail["message"] = f"{message} (the export record was changed concurrently; check History)"
                self._record_stale_attempt(db, sync_run_id, state, status, message)
                return detail
            add_export_event(
                db,
                self.audit,
                unify_order_id=state.order_id,
                event_type=status,
                previous_status=EXPORT_STATUS_IN_PROGRESS,
                new_status=status,
                sync_run_id=sync_run_id,
                export_record_id=state.export_record_id,
                zoho_invoice_id=state.zoho_invoice_id,
                detail=zoho_diagnostic(message),
            )
            db.commit()
            db.expire_all()
        except Exception:
            db.rollback()
            logger.exception(
                "Could not record export status order_id=%s status=%s message=%s zoho_invoice_id=%s; record left in_progress",
                state.order_id,
                status,
                message,
                state.zoho_invoice_id,
            )
        return detail

    def _persist_exported(self, db: Session, sync_run_id: int, state: _OrderAttemptState, invoice_id: str, buyer_key: str, message: str) -> None:
        result = db.execute(
            update(models.ExportedOrder)
            .where(*self._owned_row(state))
            .values(status=EXPORT_STATUS_EXPORTED, message=self._audit_message(message, sync_run_id, state.prior_status, state.prior_message))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            raise _ClaimOwnershipLost(f"export record for {state.order_id} is no longer owned by this attempt")
        self._record_invoice_event(
            db,
            sync_run_id=sync_run_id,
            customer_name=buyer_key,
            order_id=state.order_id,
            invoice_id=invoice_id,
            message="Draft invoice created" if message.startswith("Invoice ") else message,
        )
        linked = not message.startswith("Invoice ")
        add_export_event(
            db,
            self.audit,
            unify_order_id=state.order_id,
            event_type="invoice_linked" if linked else "invoice_created",
            previous_status=EXPORT_STATUS_IN_PROGRESS,
            new_status=EXPORT_STATUS_EXPORTED,
            sync_run_id=sync_run_id,
            export_record_id=state.export_record_id,
            zoho_invoice_id=invoice_id,
            detail="Existing Zoho invoice linked; no new invoice created" if linked else "Draft invoice created in Zoho",
        )
        db.commit()
        db.expire_all()

    def _record_invoice_event(self, db: Session, *, sync_run_id: int, customer_name: str, order_id: str, invoice_id: str, message: str) -> None:
        """Add the exported_invoices row for this order's Zoho invoice once. Re-linking the
        same invoice to the same order (after a reset or reconciliation) must not create a
        second row, so sales reporting never counts that invoice twice."""
        existing_rows = db.execute(
            select(models.ExportedInvoice.unify_order_ids).where(models.ExportedInvoice.zoho_invoice_id == invoice_id)
        ).scalars().all()
        if any(order_id in (order_ids or []) for order_ids in existing_rows):
            return
        db.add(
            models.ExportedInvoice(
                sync_run_id=sync_run_id,
                unify_customer_name=customer_name,
                unify_order_ids=[order_id],
                zoho_invoice_id=invoice_id,
                status="success",
                message=message,
            )
        )

    def _record_run_company(self, db: Session, sync_run_id: int) -> None:
        """Legacy default-company runs remain compatible; other ownership fails closed."""
        try:
            db.add(models.SyncRunCompany(sync_run_id=sync_run_id, company_id=self.audit.company_id))
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("Analytics: company of sync run %s not recorded (%s)", sync_run_id, exc.__class__.__name__)
            if self.audit.company_id != models.DEFAULT_COMPANY_ID:
                # No orders have been processed yet. Do not expose this unowned run
                # through the legacy/default-company analytics fallback.
                db.query(models.SyncRun).filter(models.SyncRun.id == sync_run_id).delete()
                db.commit()
                raise ExportServiceError("Cannot establish the company's sync run ownership; no invoices were requested") from exc

    def _capture_order_value(
        self,
        db: Session,
        *,
        sync_run_id: int,
        order: UnifyOrder,
        line_items: List[Dict[str, Any]],
        invoice_id: str,
        invoice_date: Optional[str],
        customer_key: str,
        customer_name: str,
        source: str,
    ) -> None:
        """Best effort, after the export record is committed: store the value of the lines that
        were invoiced (the exact Zoho payload lines), for Analytics. A failure is logged and
        never changes the order's sync outcome. Existing values are never overwritten."""
        company_id = self.audit.company_id
        try:
            exists = db.execute(
                select(models.OrderFact.id).where(models.OrderFact.company_id == company_id, models.OrderFact.unify_order_id == order.order_id)
            ).first()
            if exists:
                return
            lines = analytics_lines(line_items, company_id, delivery_line_flags(order, line_items))
            fact = models.OrderFact(
                company_id=company_id,
                unify_order_id=order.order_id,
                sync_run_id=sync_run_id,
                zoho_invoice_id=invoice_id,
                customer_key=(customer_key or customer_name or "unknown")[:255],
                customer_name=(customer_name or None) and customer_name[:255],
                order_date=parse_iso_date(order.order_date),
                delivery_date=parse_iso_date(getattr(order, "delivery_date", None)),
                invoice_date=parse_iso_date(invoice_date) or parse_iso_date(order.order_date) or datetime.now(timezone.utc).date(),
                currency=ANALYTICS_CURRENCY,
                net_amount=sum((line.net_amount for line in lines), Decimal("0.00")),
                line_count=len(lines),
                source=source,
                lines=lines,
            )
            db.add(fact)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("Analytics: value of order %s not captured (%s)", order.order_id, exc.__class__.__name__)

    def _record_skip_if_absent(self, db: Session, sync_run_id: int, order_id: str, customer_name: str, reason: str) -> None:
        if self._get_export_record(db, order_id) is not None:
            return
        record = models.ExportedOrder(
            sync_run_id=sync_run_id,
            unify_order_id=order_id,
            customer_name=customer_name,
            status=EXPORT_STATUS_SKIPPED,
            message=reason,
        )
        db.add(record)
        try:
            db.flush()
            add_export_event(
                db,
                self.audit,
                unify_order_id=order_id,
                event_type="skipped",
                previous_status=None,
                new_status=EXPORT_STATUS_SKIPPED,
                sync_run_id=sync_run_id,
                export_record_id=record.id,
                detail=f"Not processed: preview status {reason}",
            )
            db.commit()
        except IntegrityError:
            db.rollback()

    def _record_stale_attempt(self, db: Session, sync_run_id: int, state: _OrderAttemptState, intended_status: str, message: str) -> None:
        """Evidence that an attempt finished after losing its claim (its outcome was not applied)."""
        try:
            add_export_event(
                db,
                self.audit,
                unify_order_id=state.order_id,
                event_type="stale_attempt_rejected",
                previous_status=EXPORT_STATUS_IN_PROGRESS,
                new_status=None,
                sync_run_id=sync_run_id,
                export_record_id=state.export_record_id,
                zoho_invoice_id=state.zoho_invoice_id,
                detail=f"Outcome {intended_status} not applied: record changed by another operation. {zoho_diagnostic(message) or ''}",
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not record stale_attempt_rejected event for order %s", state.order_id)

    def _record_blocked_attempt(self, db: Session, sync_run_id: int, order_id: str, claim: _ClaimResult, detail: Dict[str, Any]) -> None:
        try:
            add_export_event(
                db,
                self.audit,
                unify_order_id=order_id,
                event_type="sync_blocked",
                previous_status=claim.current_status,
                new_status=claim.current_status,
                sync_run_id=sync_run_id,
                export_record_id=claim.record_id,
                detail=detail.get("message"),
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not record sync_blocked event for order %s", order_id)

    def _unclaimed_order_result(self, order_id: str, current_status: Optional[str]) -> Tuple[Dict[str, Any], str]:
        if current_status == EXPORT_STATUS_EXPORTED:
            logger.info("Skipped duplicate Unify order %s", order_id)
            return {"order_id": order_id, "status": "already_exported", "message": "Already exported"}, "skipped"
        if current_status == EXPORT_STATUS_IN_PROGRESS:
            message = "Another sync is processing this order, or a previous attempt did not finish. Check Zoho before retrying."
            return {"order_id": order_id, "status": EXPORT_STATUS_IN_PROGRESS, "message": message}, "needs_verification"
        if current_status == EXPORT_STATUS_NEEDS_VERIFICATION:
            message = "A previous attempt may have created a Zoho invoice. Check Zoho before retrying."
            return {"order_id": order_id, "status": EXPORT_STATUS_NEEDS_VERIFICATION, "message": message}, "needs_verification"
        message = f"Order has export status '{current_status}'. Check Zoho before retrying."
        return {"order_id": order_id, "status": EXPORT_STATUS_NEEDS_VERIFICATION, "message": message}, "needs_verification"

    async def _verify_existing_zoho_invoice(
        self,
        order_id: str,
        *,
        contact_id: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> Tuple[str, Optional[str], str]:
        """Check Zoho for an invoice that already represents this order.

        Matching: reference number equal to the order id (case-insensitive, trimmed) and not
        void (Zoho's documented cancelled state), then customer identity: the Zoho customer_id
        when the contact is known (sync), otherwise the customer name (reconciliation).

        Returns (verdict, invoice_id, detail):
        - "none": no active invoice with this exact reference -> safe to create
        - "match": exactly one active invoice with this reference AND this customer -> link it
        - "ambiguous": anything else -> never create; needs review
        - "lookup_failed": lookup errored or its result may be incomplete -> never create
        """
        try:
            invoices = await self.zoho_service.find_invoices_by_reference_number(order_id)
        except ZohoServiceError as exc:
            return "lookup_failed", None, str(exc)

        wanted = order_id.strip().casefold()
        exact = [inv for inv in invoices if str(inv.get("reference_number") or "").strip().casefold() == wanted]
        active = [inv for inv in exact if str(inv.get("status") or "").strip().lower() != "void"]
        if not active:
            return "none", None, f"No active Zoho invoice with reference {order_id}"
        if len(active) == 1:
            candidate = active[0]
            invoice_id = str(candidate.get("invoice_id") or "").strip()
            if contact_id is not None:
                same_customer = str(candidate.get("customer_id") or "").strip() == str(contact_id).strip()
            else:
                wanted_name = self._normalize_customer_name(customer_name)
                same_customer = bool(wanted_name) and self._normalize_customer_name(candidate.get("customer_name")) == wanted_name
            if invoice_id and same_customer:
                return "match", invoice_id, f"Zoho invoice {invoice_id} matches reference {order_id} and the customer"
        summary = ", ".join(
            f"{inv.get('invoice_number') or inv.get('invoice_id')} (customer {inv.get('customer_name') or inv.get('customer_id')}, status {inv.get('status')})"
            for inv in active
        )
        return "ambiguous", None, f"Zoho has {len(active)} active invoice(s) with reference {order_id} that could not be matched safely: {summary}"

    async def _process_order(self, db: Session, sync_run_id: int, order: UnifyOrder, state: _OrderAttemptState) -> Tuple[Dict[str, Any], str]:
        """Export one order. Returns (detail, outcome) where outcome is one of
        created / skipped / failed / needs_verification."""
        order_id = order.order_id
        buyer_key = self._buyer_key(order)
        contact_name = self._resolve_customer_display_name(order)
        status = (getattr(order, "status", "confirmed") or "confirmed").strip().lower()
        preview_status = (getattr(order, "preview_status", "ready") or "ready").strip().lower()
        preview_reason = (getattr(order, "preview_reason", "") or "").strip()
        invoice_date = self._resolve_invoice_date(order)
        record_customer = contact_name or buyer_key
        state.order_id = order_id
        state.customer_name = record_customer

        self._log_customer_resolution(order, contact_name, contact_name)
        logger.info("Processing Unify order %s for buyer %s", order_id, buyer_key)
        logger.info(
            "Attempting export order_id=%s buyer_key=%s preview_status=%s preview_reason=%s invoice_date=%s contact_name=%s",
            order_id,
            buyer_key,
            preview_status,
            preview_reason,
            invoice_date,
            contact_name,
        )
        logger.info(
            "Invoice sync customer context order_id=%s buyer_id=%s resolved_customer_name=%s",
            order_id,
            buyer_key,
            contact_name,
        )

        if preview_status not in {"ready", "mapping_issue"}:
            reason = preview_reason or preview_status
            logger.info("Skipped Unify order %s due to preview status: %s", order_id, reason)
            self._record_skip_if_absent(db, sync_run_id, order_id, record_customer, reason)
            return {"order_id": order_id, "status": "skipped", "message": reason}, "skipped"
        if preview_status == "mapping_issue":
            logger.info("Order %s has mapping_issue preview status but remains eligible for export", order_id)

        if status and status not in {"confirmed", "received", "checked", "ready", "completed", "delivered"}:
            logger.info("Order %s has non-ready raw status %s but remains eligible because preview status is ready", order_id, status)

        claim = self._claim_order(db, sync_run_id, order_id, record_customer)
        if not claim.claimed:
            blocked_detail, blocked_outcome = self._unclaimed_order_result(order_id, claim.current_status)
            self._record_blocked_attempt(db, sync_run_id, order_id, claim, blocked_detail)
            return blocked_detail, blocked_outcome
        state.claimed = True
        state.claim_token = claim.token
        state.export_record_id = claim.record_id
        state.prior_status = claim.prior_status
        state.prior_message = claim.prior_message

        try:
            line_items, line_errors = await self._build_line_items(db, order)
        except ExportServiceError as exc:
            reason = str(exc)
            logger.error("Failed to build line items for order %s: %s", order_id, reason)
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_FAILED, reason), "failed"

        validation_errors = self._validate_invoice_payload(
            order,
            contact_name=contact_name,
            invoice_date=invoice_date,
            line_items=line_items,
            line_errors=line_errors,
        )
        if validation_errors:
            reason = "; ".join(validation_errors)
            logger.warning("Order %s failed validation: %s", order_id, reason)
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_FAILED, reason), "failed"

        try:
            contact_id = await self._resolve_contact_for_order(db, order, record_customer)
        except ZohoServiceError as exc:
            reason = str(exc)
            logger.error("Failed to resolve Zoho contact for order %s: %s", order_id, reason)
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_FAILED, reason), "failed"

        # Zoho duplicate check before EVERY creation (new orders, retries and resets).
        verdict, existing_invoice_id, verdict_detail = await self._verify_existing_zoho_invoice(order_id, contact_id=contact_id)
        logger.info(
            "Zoho duplicate check order_id=%s prior_status=%s verdict=%s invoice_id=%s detail=%s",
            order_id,
            claim.prior_status,
            verdict,
            existing_invoice_id,
            verdict_detail,
        )
        if verdict == "match" and existing_invoice_id:
            link_message = f"Linked existing Zoho invoice {existing_invoice_id}; no new invoice created"
            try:
                self._persist_exported(db, sync_run_id, state, existing_invoice_id, buyer_key, message=link_message)
            except _ClaimOwnershipLost as exc:
                logger.critical("Could not link Zoho invoice %s to order %s: %s", existing_invoice_id, order_id, exc)
                state.zoho_invoice_id = existing_invoice_id
                self._record_stale_attempt(db, sync_run_id, state, EXPORT_STATUS_EXPORTED, "link not applied")
                return {
                    "order_id": order_id,
                    "status": EXPORT_STATUS_NEEDS_VERIFICATION,
                    "zoho_invoice_id": existing_invoice_id,
                    "message": f"Existing Zoho invoice {existing_invoice_id} found but the export record changed concurrently; check History",
                }, "needs_verification"
            self._capture_order_value(
                db,
                sync_run_id=sync_run_id,
                order=order,
                line_items=line_items,
                invoice_id=existing_invoice_id,
                invoice_date=invoice_date or order.order_date,
                customer_key=buyer_key,
                customer_name=record_customer,
                source="invoice_linked",
            )
            return {
                "order_id": order_id,
                "status": "already_exported",
                "zoho_invoice_id": existing_invoice_id,
                "message": "An existing Zoho invoice was found for this order and linked; no new invoice was created",
            }, "skipped"
        if verdict == "ambiguous":
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_NEEDS_VERIFICATION, verdict_detail), "needs_verification"
        if verdict != "none":
            reason = f"Invoice not created: the Zoho duplicate check could not be completed ({verdict_detail})"
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_FAILED, reason), "failed"

        payload = await self._build_draft_invoice_payload(
            order,
            contact_id,
            line_items,
            invoice_date or order.order_date,
            record_customer,
        )
        payload_item_ids = [str(line.get("item_id")) for line in line_items if line.get("item_id")]
        payload_tax_ids = [str(line.get("tax_id")) for line in line_items if line.get("tax_id")]
        logger.info("Zoho payload sent order_id=%s payload=%s", order_id, payload)
        state.zoho_create_started = True
        try:
            invoice_id = await self.zoho_service.create_draft_invoice_from_payload(payload)
        except ZohoAmbiguousResponseError as exc:
            reason = str(exc)
            logger.error("Zoho invoice creation outcome unknown for order %s: %s", order_id, reason)
            message = f"Zoho invoice creation outcome unknown; check Zoho before retrying: {reason}"
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_NEEDS_VERIFICATION, message), "needs_verification"
        except ZohoServiceError as exc:
            reason = str(exc)
            logger.error("Zoho invoice creation failed for order %s: %s", order_id, reason)
            return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_FAILED, reason), "failed"

        state.zoho_invoice_id = invoice_id
        logger.info("Zoho response received order_id=%s invoice_id=%s", order_id, invoice_id)
        logger.info("Created draft invoice %s for order %s", invoice_id, order_id)
        logger.info(
            "Zoho invoice payload summary order_id=%s invoice_id=%s item_ids_used=%s tax_ids_used=%s",
            order_id,
            invoice_id,
            payload_item_ids,
            payload_tax_ids,
        )

        try:
            self._persist_exported(db, sync_run_id, state, invoice_id, buyer_key, message=f"Invoice {invoice_id} created")
        except _ClaimOwnershipLost as exc:
            logger.critical("Zoho invoice %s was created for order %s but the claim was lost: %s", invoice_id, order_id, exc)
            self._record_stale_attempt(db, sync_run_id, state, EXPORT_STATUS_EXPORTED, f"invoice {invoice_id} created after claim was lost")
            return {
                "order_id": order_id,
                "status": EXPORT_STATUS_NEEDS_VERIFICATION,
                "zoho_invoice_id": invoice_id,
                "message": f"Zoho invoice {invoice_id} was created but the export record changed concurrently; check History",
            }, "needs_verification"
        except Exception as exc:
            db.rollback()
            logger.critical(
                "Zoho invoice %s was created for order %s but could not be recorded: %s",
                invoice_id,
                order_id,
                exc,
            )
            message = f"Zoho invoice {invoice_id} was created but could not be recorded; check Zoho before retrying: {exc}"
            detail = self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_NEEDS_VERIFICATION, message)
            detail["zoho_invoice_id"] = invoice_id
            return detail, "needs_verification"

        self._capture_order_value(
            db,
            sync_run_id=sync_run_id,
            order=order,
            line_items=line_items,
            invoice_id=invoice_id,
            invoice_date=invoice_date or order.order_date,
            customer_key=buyer_key,
            customer_name=record_customer,
            source="invoice_created",
        )
        return {
            "order_id": order_id,
            "status": "created",
            "zoho_invoice_id": invoice_id,
            "message": "Draft invoice created",
        }, "created"

    def _handle_unexpected_order_error(self, db: Session, sync_run_id: int, order: UnifyOrder, state: _OrderAttemptState, exc: BaseException) -> Tuple[Dict[str, Any], str]:
        db.rollback()
        order_id = state.order_id or order.order_id
        logger.exception("Unexpected error while exporting order %s zoho_create_started=%s", order_id, state.zoho_create_started)
        if not state.claimed:
            try:
                add_export_event(
                    db,
                    self.audit,
                    unify_order_id=order_id,
                    event_type="sync_error",
                    sync_run_id=sync_run_id,
                    detail=f"Unexpected error before claim: {exc.__class__.__name__}",
                )
                db.commit()
            except Exception:
                db.rollback()
            return {"order_id": order_id, "status": EXPORT_STATUS_FAILED, "message": f"Unexpected error before the order was claimed: {exc}"}, "failed"
        if state.zoho_create_started:
            message = f"Unexpected error during Zoho invoice creation; check Zoho before retrying: {exc}"
            detail = self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_NEEDS_VERIFICATION, message)
            if state.zoho_invoice_id:
                detail["zoho_invoice_id"] = state.zoho_invoice_id
            return detail, "needs_verification"
        return self._finalize_claim(db, sync_run_id, state, EXPORT_STATUS_FAILED, f"Unexpected error: {exc}"), "failed"

    # ------------------------------------------------------------------
    # Crash recovery: explicit Zoho reconciliation of in_progress / needs_verification
    # ------------------------------------------------------------------

    async def reconcile_order(self, db: Session, order_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Resolve an in_progress or needs_verification record by checking Zoho.

        Never assumes failure and never creates an invoice. Requires the record to be old
        enough (in_progress: abandoned; needs_verification: time for Zoho to list a just-
        created invoice). The final write is conditional on the record being unchanged, so
        concurrent reconciliations and syncs cannot both act on it.
        """
        now = now or datetime.utcnow()
        record = self._get_export_record(db, order_id)
        if record is None:
            raise ReconcileError(404, "Order not found in export history")
        observed_status = record.status
        observed_message = record.message
        if observed_status not in SAFETY_EXPORT_STATUSES:
            raise ReconcileError(409, f"Order has export status '{observed_status}'; there is nothing to reconcile")

        last_event = latest_audit_time(observed_message)
        age = now - last_event if last_event else None
        if observed_status == EXPORT_STATUS_IN_PROGRESS and age is not None and age < STALE_IN_PROGRESS_AFTER:
            minutes = int(STALE_IN_PROGRESS_AFTER.total_seconds() // 60)
            raise ReconcileError(409, f"This order may still be processing. Try again after {minutes} minutes from the start of the attempt.")
        if observed_status == EXPORT_STATUS_NEEDS_VERIFICATION and age is not None and age < MIN_VERIFICATION_AGE:
            minutes = int(MIN_VERIFICATION_AGE.total_seconds() // 60)
            raise ReconcileError(409, f"Zoho may not list a just-created invoice yet. Try again in {minutes} minutes.")

        verdict, invoice_id, verdict_detail = await self._verify_existing_zoho_invoice(order_id, customer_name=record.customer_name)
        logger.warning(
            "Reconciliation check order_id=%s previous_status=%s verdict=%s invoice_id=%s detail=%s",
            order_id,
            observed_status,
            verdict,
            invoice_id,
            verdict_detail,
        )
        if verdict == "lookup_failed":
            try:
                add_export_event(
                    db,
                    self.audit,
                    unify_order_id=order_id,
                    event_type="reconcile_failed",
                    previous_status=observed_status,
                    new_status=observed_status,
                    sync_run_id=record.sync_run_id,
                    export_record_id=record.id,
                    detail=f"Zoho check failed; nothing changed: {zoho_diagnostic(verdict_detail)}",
                )
                db.commit()
            except Exception:
                db.rollback()
            raise ReconcileError(502, f"Zoho could not be checked; nothing was changed ({verdict_detail})")

        if verdict == "match" and invoice_id:
            new_status, result_label = EXPORT_STATUS_EXPORTED, "linked"
            message = f"Reconciled: linked existing Zoho invoice {invoice_id}"
        elif verdict == "none":
            new_status, result_label = EXPORT_STATUS_RESET, "reset"
            message = "Reconciled: no Zoho invoice found; the next sync will check Zoho again before creating one"
        else:
            new_status, result_label = EXPORT_STATUS_NEEDS_VERIFICATION, "ambiguous"
            message = f"Reconciled: review required in Zoho. {verdict_detail}"

        result = db.execute(
            update(models.ExportedOrder)
            .where(
                models.ExportedOrder.id == record.id,
                models.ExportedOrder.status == observed_status,
                models.ExportedOrder.message == observed_message,
            )
            .values(status=new_status, message=audit_text(message, record.sync_run_id, observed_status, observed_message))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            raise ReconcileError(409, "The order changed while it was being reconciled; refresh and try again")
        add_export_event(
            db,
            self.audit,
            unify_order_id=order_id,
            event_type="reconciled",
            previous_status=observed_status,
            new_status=new_status,
            sync_run_id=record.sync_run_id,
            export_record_id=record.id,
            zoho_invoice_id=invoice_id,
            detail=f"Reconciliation result: {result_label}",
        )
        if new_status == EXPORT_STATUS_EXPORTED:
            self._record_invoice_event(
                db,
                sync_run_id=record.sync_run_id,
                customer_name=record.customer_name or order_id,
                order_id=order_id,
                invoice_id=invoice_id,
                message="Linked during reconciliation",
            )
        db.commit()
        db.expire_all()
        return {
            "order_id": order_id,
            "previous_status": observed_status,
            "status": new_status,
            "result": result_label,
            "zoho_invoice_id": invoice_id,
            "message": message,
        }

    @staticmethod
    def _final_run_status(*, created: int, failed: int, not_exported: int) -> str:
        """Run status from per-order outcomes.

        failed counts failed and needs_verification orders; not_exported counts orders skipped
        because their preview status was not eligible. Orders that were already exported (or
        linked to an existing Zoho invoice) are a correct outcome and never count as problems.
        - success: new invoices were created and nothing failed or was left out
        - partial: some invoices were created, but some orders failed or were left out
        - failed:  orders failed or need verification and no invoice was created
        - skipped: nothing to do (every order was already exported, or not eligible)
        """
        if failed:
            return "partial" if created else "failed"
        if not_exported:
            return "partial" if created else "skipped"
        return "success" if created else "skipped"

    async def run_export(
        self,
        db: Session,
        date_from: str,
        date_to: str,
        orders: List[UnifyOrder],
        order_ids: List[str] | None = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        selected_ids = {order_id.strip() for order_id in (order_ids or []) if order_id and order_id.strip()}
        if selected_ids:
            orders = [order for order in orders if order.order_id in selected_ids]
        total_orders = len(orders)
        total_customers = len({self._buyer_key(order) for order in orders})
        sync_run = models.SyncRun(date_from=date_from, date_to=date_to, status="running")
        db.add(sync_run)
        db.commit()
        db.refresh(sync_run)
        self._record_run_company(db, sync_run.id)

        error_messages: List[str] = []
        structured = {"created": 0, "skipped": 0, "failed": 0, "needs_verification": 0, "details": []}
        total_invoices = 0
        attempted_orders = 0
        successful_orders = 0
        failed_orders = 0
        skipped_orders = 0
        already_exported_orders = 0  # already invoiced or linked to an existing Zoho invoice: correct, not a problem
        sync_run_finalized = False
        run_error: BaseException | None = None
        selected_order_ids = [order.order_id for order in orders]
        stage = "processing_orders"
        logger.info(
            "Starting export run for %s to %s with %s orders%s",
            date_from,
            date_to,
            len(orders),
            f" selected_ids={len(selected_ids)}" if selected_ids else "",
        )
        logger.info(
            "Export run input orders date_from=%s date_to=%s selected_order_ids=%s passed_order_ids=%s using_direct_tax_resolution=true",
            date_from,
            date_to,
            sorted(selected_ids) if selected_ids else [],
            [order.order_id for order in orders],
        )

        try:
            sync_run.total_orders = total_orders
            sync_run.total_customers = total_customers
            sync_run.total_invoices = 0
            db.commit()

            # Read once: touching sync_run after a failed flush (before rollback) would raise.
            sync_run_id = sync_run.id
            for order in orders:
                attempted_orders += 1
                stage = f"processing_order:{order.order_id}"
                attempt_state = _OrderAttemptState(order_id=order.order_id)
                try:
                    detail, outcome = await self._process_order(db, sync_run_id, order, attempt_state)
                except Exception as exc:  # isolate failures per order; never abort the batch
                    detail, outcome = self._handle_unexpected_order_error(db, sync_run_id, order, attempt_state, exc)

                structured["details"].append(detail)
                if outcome == "created":
                    total_invoices += 1
                    successful_orders += 1
                    structured["created"] += 1
                elif outcome == "skipped":
                    skipped_orders += 1
                    structured["skipped"] += 1
                    if detail.get("status") == "already_exported":
                        already_exported_orders += 1
                else:
                    failed_orders += 1
                    structured["failed"] += 1
                    if outcome == "needs_verification":
                        structured["needs_verification"] += 1
                    error_messages.append(f"{detail.get('order_id')}: {detail.get('message') or detail.get('status')}")

            stage = "finalizing_success"
            sync_run.finished_at = datetime.utcnow()
            sync_run.total_invoices = total_invoices
            sync_run.status = self._final_run_status(
                created=successful_orders,
                failed=failed_orders,
                not_exported=skipped_orders - already_exported_orders,
            )
            sync_run.errors = "\n".join(error_messages) if error_messages else None
            db.commit()
            sync_run_finalized = True

            logger.info(
                "Export run finished sync_run_id=%s status=%s attempted_orders=%s created=%s failed=%s skipped=%s total_invoices=%s total_orders=%s selected_order_ids=%s",
                sync_run.id,
                sync_run.status,
                attempted_orders,
                successful_orders,
                failed_orders,
                skipped_orders,
                sync_run.total_invoices,
                sync_run.total_orders,
                selected_order_ids,
            )
            if error_messages:
                logger.error("Export run %s finished with errors", sync_run.id)
            else:
                logger.info("Export run %s finished without order-level errors", sync_run.id)

            return {
                "sync_run_id": sync_run.id,
                "status": sync_run.status,
                "total_orders": sync_run.total_orders,
                "total_customers": sync_run.total_customers,
                "total_invoices": sync_run.total_invoices,
                "errors": sync_run.errors,
                "created": structured["created"],
                "skipped": structured["skipped"],
                "failed": structured["failed"],
                "needs_verification": structured["needs_verification"],
                "details": structured["details"],
            }
        except Exception as exc:
            run_error = exc
            logger.exception(
                "Export run failed stage=%s date_from=%s date_to=%s selected_order_ids=%s error=%s",
                stage,
                date_from,
                date_to,
                selected_order_ids,
                str(exc),
            )
            raise ExportServiceError(self._format_exception_message(exc)) from exc
        finally:
            if not sync_run_finalized:
                try:
                    self._persist_failed_sync_run(
                        db,
                        sync_run.id,
                        date_from=date_from,
                        date_to=date_to,
                        selected_order_ids=selected_order_ids,
                        error_message=self._format_exception_message(run_error),
                        total_orders=total_orders,
                        total_customers=total_customers,
                        total_invoices=total_invoices,
                        error_messages=error_messages,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist failed export state sync_run_id=%s date_from=%s date_to=%s selected_order_ids=%s stage=%s",
                        sync_run.id,
                        date_from,
                        date_to,
                        selected_order_ids,
                        stage,
                    )


def make_export_service(unify_svc: UnifyService, zoho_svc: ZohoService) -> ExportService:
    return ExportService(unify_svc, zoho_svc)
