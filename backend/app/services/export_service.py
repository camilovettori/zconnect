import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import select

from .unify_service import UnifyService
from .zoho_service import ZohoService, ZohoServiceError
from .. import models
from ..schemas import UnifyOrder


class ExportServiceError(Exception):
    pass


logger = logging.getLogger(__name__)

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
)
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

        lines.append(
            "This is your delivery receipt and invoice. "
            "A weekly statement will be e-mailed to you. "
            "Thank you for loving our cake! Caryna and the Lovin' from the Oven team"
        )
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

    def _build_draft_invoice_payload(self, order: UnifyOrder, contact_id: str, line_items: List[Dict[str, Any]], invoice_date: str, contact_name: str) -> Dict[str, Any]:
        payload = {
            "customer_id": contact_id,
            "reference_number": order.order_id,
            "date": invoice_date,
            "line_items": line_items,
            "notes": self.build_invoice_notes(order),
            "is_inclusive_tax": False,
            "status": "draft",
        }
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

        error_messages: List[str] = []
        structured = {"created": 0, "skipped": 0, "failed": 0, "details": []}
        attempted_orders = 0
        successful_orders = 0
        failed_orders = 0
        skipped_orders = 0
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

            for order in orders:
                attempted_orders += 1
                order_id = order.order_id
                buyer_key = self._buyer_key(order)
                contact_name = self._resolve_customer_display_name(order)
                status = (getattr(order, "status", "confirmed") or "confirmed").strip().lower()
                preview_status = (getattr(order, "preview_status", "ready") or "ready").strip().lower()
                preview_reason = (getattr(order, "preview_reason", "") or "").strip()
                invoice_date = self._resolve_invoice_date(order)
                stage = f"processing_order:{order_id}"

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
                    skipped_order = models.ExportedOrder(
                        sync_run_id=sync_run.id,
                        unify_order_id=order_id,
                        customer_name=contact_name or buyer_key,
                        status="skipped",
                        message=reason,
                    )
                    db.add(skipped_order)
                    db.commit()
                    structured["skipped"] += 1
                    skipped_orders += 1
                    structured["details"].append({"order_id": order_id, "status": "skipped", "message": reason})
                    continue
                if preview_status == "mapping_issue":
                    logger.info("Order %s has mapping_issue preview status but remains eligible for export", order_id)

                if status and status not in {"confirmed", "received", "checked", "ready", "completed", "delivered"}:
                    logger.info("Order %s has non-ready raw status %s but remains eligible because preview status is ready", order_id, status)

                if self._order_exists(db, order_id):
                    reason = "Already exported"
                    logger.info("Skipped duplicate Unify order %s", order_id)
                    skipped_order = models.ExportedOrder(
                        sync_run_id=sync_run.id,
                        unify_order_id=order_id,
                        customer_name=contact_name or buyer_key,
                        status="skipped",
                        message=reason,
                    )
                    db.add(skipped_order)
                    db.commit()
                    structured["skipped"] += 1
                    skipped_orders += 1
                    structured["details"].append({"order_id": order_id, "status": "already_exported", "message": reason})
                    continue

                try:
                    line_items, line_errors = await self._build_line_items(db, order)
                except ExportServiceError as exc:
                    reason = str(exc)
                    logger.error("Failed to build line items for order %s: %s", order_id, reason)
                    failed_order = models.ExportedOrder(
                        sync_run_id=sync_run.id,
                        unify_order_id=order_id,
                        customer_name=contact_name or buyer_key,
                        status="failed",
                        message=reason,
                    )
                    db.add(failed_order)
                    db.commit()
                    error_messages.append(f"{order_id}: {reason}")
                    structured["failed"] += 1
                    failed_orders += 1
                    structured["details"].append({"order_id": order_id, "status": "failed", "message": reason})
                    continue

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
                    failed_order = models.ExportedOrder(
                        sync_run_id=sync_run.id,
                        unify_order_id=order_id,
                        customer_name=contact_name or buyer_key,
                        status="failed",
                        message=reason,
                    )
                    db.add(failed_order)
                    db.commit()
                    error_messages.append(f"{order_id}: {reason}")
                    structured["failed"] += 1
                    failed_orders += 1
                    structured["details"].append({"order_id": order_id, "status": "failed", "message": reason})
                    continue

                try:
                    contact_id = await self._resolve_contact_for_order(db, order, contact_name or buyer_key)
                except ZohoServiceError as exc:
                    reason = str(exc)
                    logger.error("Failed to resolve Zoho contact for order %s: %s", order_id, reason)
                    failed_order = models.ExportedOrder(
                        sync_run_id=sync_run.id,
                        unify_order_id=order_id,
                        customer_name=contact_name or buyer_key,
                        status="failed",
                        message=reason,
                    )
                    db.add(failed_order)
                    db.commit()
                    error_messages.append(f"{order_id}: {reason}")
                    structured["failed"] += 1
                    failed_orders += 1
                    structured["details"].append({"order_id": order_id, "status": "failed", "message": reason})
                    continue

                try:
                    payload = self._build_draft_invoice_payload(
                        order,
                        contact_id,
                        line_items,
                        invoice_date or order.order_date,
                        contact_name or buyer_key,
                    )
                    payload_item_ids = [str(line.get("item_id")) for line in line_items if line.get("item_id")]
                    payload_tax_ids = [str(line.get("tax_id")) for line in line_items if line.get("tax_id")]
                    logger.info("Zoho payload sent order_id=%s payload=%s", order_id, payload)
                    invoice_id = await self.zoho_service.create_draft_invoice_from_payload(payload)
                    logger.info("Zoho response received order_id=%s invoice_id=%s", order_id, invoice_id)
                    logger.info("Created draft invoice %s for order %s", invoice_id, order_id)
                    logger.info(
                        "Zoho invoice payload summary order_id=%s invoice_id=%s item_ids_used=%s tax_ids_used=%s",
                        order_id,
                        invoice_id,
                        payload_item_ids,
                        payload_tax_ids,
                    )
                except ZohoServiceError as exc:
                    reason = str(exc)
                    logger.error("Zoho invoice creation failed for order %s: %s", order_id, reason)
                    failed_order = models.ExportedOrder(
                        sync_run_id=sync_run.id,
                        unify_order_id=order_id,
                        customer_name=contact_name or buyer_key,
                        status="failed",
                        message=reason,
                    )
                    db.add(failed_order)
                    db.commit()
                    error_messages.append(f"{order_id}: {reason}")
                    structured["failed"] += 1
                    failed_orders += 1
                    structured["details"].append({"order_id": order_id, "status": "failed", "message": reason})
                    continue

                exported_order = models.ExportedOrder(
                    sync_run_id=sync_run.id,
                    unify_order_id=order_id,
                    customer_name=contact_name or buyer_key,
                    status="exported",
                    message=f"Invoice {invoice_id} created",
                )
                db.add(exported_order)
                exported_invoice = models.ExportedInvoice(
                    sync_run_id=sync_run.id,
                    unify_customer_name=buyer_key,
                    unify_order_ids=[order_id],
                    zoho_invoice_id=invoice_id,
                    status="success",
                    message="Draft invoice created",
                )
                db.add(exported_invoice)
                db.commit()

                sync_run.total_invoices += 1
                structured["created"] += 1
                successful_orders += 1
                structured["details"].append(
                    {
                        "order_id": order_id,
                        "status": "created",
                        "zoho_invoice_id": invoice_id,
                        "message": "Draft invoice created",
                    }
                )

            stage = "finalizing_success"
            sync_run.finished_at = datetime.utcnow()
            if sync_run.total_invoices <= 0:
                final_status = "failed"
            elif failed_orders == 0 and skipped_orders == 0 and successful_orders > 0:
                final_status = "success"
            elif successful_orders > 0:
                final_status = "partial"
            else:
                final_status = "failed"
            sync_run.status = final_status
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
                        total_invoices=sync_run.total_invoices,
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
