import logging
import re
from datetime import datetime
import json
import traceback

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from typing import Any, List, Optional

from ..db import get_db
from ..security import COMPANY_ID_PATTERN, Identity, require_admin, require_identity
from .. import models
from ..schemas import (
    ConnectionTestResult,
    CsvPreviewResponse,
    ExportCsvRunRequest,
    ExportRunRequest,
    FetchOrdersRequest,
    FetchOrdersResponse,
    ResetSelectedRunsRequest,
    ResetSelectedRunsResponse,
    UnifyOrder,
    APIResponse,
    SettingsResponse,
)
from ..services.unify_service import UnifyService, make_unify_service, UnifyServiceError
from ..services import analytics_service, csv_parser, zoho_history_service
from ..reports import weekly as weekly_report
from ..reports.periods import ReportPeriodError, resolve_period
from ..services.zoho_service import ZohoService, make_zoho_service, ZohoServiceError
from ..services.export_service import (
    AuditContext,
    EXPORT_STATUS_RESET,
    ExportService,
    ExportServiceError,
    ReconcileError,
    make_export_service,
    soft_reset_export_records,
)

# Every route requires a signed identity from the Next.js server (see app/security.py).
router = APIRouter(dependencies=[Depends(require_identity)])
logger = logging.getLogger(__name__)
MASKED_SECRET_VALUE = "********"
MASKED_SECRET_PATTERN = re.compile(r"^[*\u2022\u25CF\u25AA\u25AB]+$")
PROTECTED_SETTING_KEYS = {
    "UNIFY_CLIENT_ID",
    "UNIFY_CLIENT_SECRET",
    "ZOHO_CLIENT_ID",
    "ZOHO_CLIENT_SECRET",
    "ZOHO_REFRESH_TOKEN",
    "ZOHO_ORG_ID",
    "UNIFY_API_TOKEN",
    "ZOHO_ACCESS_TOKEN",
}
VISIBLE_SETTING_KEYS = {
    "ZOHO_BASE_URL",
    "ZOHO_STANDARD_TAX_ID",
    "ZOHO_REDUCED_TAX_ID",
    "ZOHO_ZERO_TAX_ID",
}
SETTING_FLAG_NAMES = {
    "UNIFY_CLIENT_ID": "has_unify_client_id",
    "UNIFY_CLIENT_SECRET": "has_unify_client_secret",
    "ZOHO_CLIENT_ID": "has_zoho_client_id",
    "ZOHO_CLIENT_SECRET": "has_zoho_client_secret",
    "ZOHO_REFRESH_TOKEN": "has_zoho_refresh_token",
    "ZOHO_ORG_ID": "has_zoho_organization_id",
}


def _is_secret_key(key: str) -> bool:
    return key.upper() in PROTECTED_SETTING_KEYS


def _mask_setting_value(key: str, value: str) -> str:
    if _is_secret_key(key) and value:
        return MASKED_SECRET_VALUE
    return value


def _setting_has_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text == "":
        return False
    if text == MASKED_SECRET_VALUE:
        return False
    return MASKED_SECRET_PATTERN.fullmatch(text) is None


def _is_placeholder_secret_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text == "":
        return False
    return text == MASKED_SECRET_VALUE or MASKED_SECRET_PATTERN.fullmatch(text) is not None


def _normalize_request_date(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} is required")

    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{field_name} must be a valid date in yyyy-MM-dd format",
    )


def _settings_presence_snapshot(rows) -> dict:
    present_keys = []
    blank_keys = []
    flags = dict.fromkeys(SETTING_FLAG_NAMES.values(), False)

    for setting in rows:
        normalized_key = setting.key.upper()
        value_present = _setting_has_value(setting.value)
        if value_present:
            present_keys.append(normalized_key)
        else:
            blank_keys.append(normalized_key)
        flag_name = SETTING_FLAG_NAMES.get(normalized_key)
        if flag_name:
            flags[flag_name] = value_present

    return {
        "present_keys": present_keys,
        "blank_keys": blank_keys,
        "flags": flags,
    }


def _customer_display_label(order: Any) -> str:
    for candidate in (
        getattr(order, "buyer_name", None),
        getattr(order, "customer_name", None),
        f"Customer {getattr(order, 'buyer_id', None)}" if getattr(order, "buyer_id", None) else None,
        f"Customer {getattr(order, 'order_id', '')}",
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return f"Customer {getattr(order, 'order_id', 'unknown')}"


def _build_preview_response_payload(orders, db: Session, debug_summary: dict | None = None, *, request_started_at: datetime | None = None, source: str = "unify") -> dict:
    export_statuses = {
        row[0]: row[1]
        for row in db.query(models.ExportedOrder.unify_order_id, models.ExportedOrder.status).all()
    }
    # Every record blocks re-export except a soft reset, whose next sync checks Zoho first.
    exported_order_ids = {order_id for order_id, record_status in export_statuses.items() if record_status != EXPORT_STATUS_RESET}

    customer_info: dict[str, dict[str, float | int]] = {}
    preview_count = len(orders)
    ready_count = 0
    blocked_in_range_count = 0

    for order in orders:
        # Any existing record keeps blocking re-export (unchanged); export_status says why.
        order.already_exported = order.order_id in exported_order_ids
        order.export_status = export_statuses.get(order.order_id)
        customer_key = _customer_display_label(order)
        info = customer_info.setdefault(customer_key, {"order_count": 0, "total": 0.0})
        info["order_count"] += 1
        info["total"] += float(getattr(order, "total", 0.0) or 0.0)

        if getattr(order, "preview_status", "ready") == "ready" and not order.already_exported:
            ready_count += 1
        elif not order.already_exported:
            blocked_in_range_count += 1

    summary = dict(debug_summary or {})
    summary.update(
        {
            "source": source,
            "preview_count": preview_count,
            "raw_preview_count": preview_count,
            "ready_count": ready_count,
            "blocked_in_range_count": blocked_in_range_count,
            "blocked_count": blocked_in_range_count,
            "total_scanned_orders": summary.get("total_scanned_orders", summary.get("rawOrdersCount", preview_count)),
            "total_in_range_orders": summary.get("total_in_range_orders", preview_count),
            "dropped_out_of_range_count": summary.get("dropped_out_of_range_count", 0),
            "missing_items_count": summary.get("missing_items_count", 0),
            "item_fetch_failed_count": summary.get("item_fetch_failed_count", 0),
            "mapping_issue_count": summary.get("mapping_issue_count", 0),
            "detailFallbackCalls": summary.get("detailFallbackAttempts", summary.get("detailFallbackCalls", 0)),
            "itemHydrationCalls": summary.get("itemHydrationCalls", 0),
            "already_synced_count": len([order for order in orders if getattr(order, "already_exported", False)]),
            "droppedOrders": summary.get("droppedOrders", []),
        }
    )
    if request_started_at is not None:
        summary["requestDurationSeconds"] = (datetime.utcnow() - request_started_at).total_seconds()
    if "totalDurationSeconds" in summary and "totalDurationMs" not in summary:
        summary["totalDurationMs"] = round(float(summary.get("totalDurationSeconds") or 0) * 1000, 2)

    return {
        "total_orders": preview_count,
        "total_customers": len(customer_info),
        "customers": customer_info,
        "orders": orders,
        "debug_summary": summary,
    }


async def _read_csv_payload(request: Request) -> str:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        preferred_keys = ("csv", "csv_file", "file", "upload", "content", "text", "csv_text")
        for key in preferred_keys:
            value = form.get(key)
            if value is None:
                continue
            if _is_upload_file_value(value):
                raw = await value.read()
                text = raw.decode("utf-8-sig", errors="replace")
                if text.strip():
                    return text
            if hasattr(value, "read") and callable(value.read):
                raw = await value.read()
                text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else str(raw)
                if text.strip():
                    return text
            text = str(value)
            if text.strip():
                return text

        for value in form.values():
            if _is_upload_file_value(value):
                raw = await value.read()
                text = raw.decode("utf-8-sig", errors="replace")
                if text.strip():
                    return text
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV file is required")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV content is required")

    if "application/json" in content_type:
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON payload: {exc}") from exc
        if isinstance(payload, dict):
            for key in ("csv_text", "csv", "content", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV content is required")

    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV content is required")
    return text


def _is_upload_file_value(value: object) -> bool:
    return hasattr(value, "filename") and callable(getattr(value, "read", None))


def _parse_csv_export_date(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    candidate = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return candidate


def _derive_csv_export_range(orders: List[UnifyOrder], requested_date_from: str | None, requested_date_to: str | None) -> tuple[str, str]:
    normalized_from = _parse_csv_export_date(requested_date_from)
    normalized_to = _parse_csv_export_date(requested_date_to)
    if normalized_from and normalized_to:
        return normalized_from, normalized_to

    candidates: List[str] = []
    for order in orders:
        for candidate in (order.order_date, order.delivery_date):
            normalized = _parse_csv_export_date(candidate)
            if normalized:
                candidates.append(normalized)

    if not normalized_from:
        normalized_from = min(candidates) if candidates else datetime.utcnow().date().isoformat()
    if not normalized_to:
        normalized_to = max(candidates) if candidates else normalized_from

    return normalized_from, normalized_to


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(db: Session = Depends(get_db)):
    settings = db.query(models.Setting).all()
    snapshot = _settings_presence_snapshot(settings)
    logger.info(
        "Settings GET snapshot present_keys=%s blank_keys=%s flags=%s",
        snapshot["present_keys"],
        snapshot["blank_keys"],
        snapshot["flags"],
    )
    response = {
        "ZOHO_BASE_URL": "https://www.zohoapis.eu",
        "ZOHO_STANDARD_TAX_ID": "",
        "ZOHO_REDUCED_TAX_ID": "",
        "ZOHO_ZERO_TAX_ID": "",
        "has_unify_client_id": False,
        "has_unify_client_secret": False,
        "has_zoho_client_id": False,
        "has_zoho_client_secret": False,
        "has_zoho_refresh_token": False,
        "has_zoho_organization_id": False,
    }

    for setting in settings:
        normalized_key = setting.key.upper()
        if normalized_key in VISIBLE_SETTING_KEYS:
            response[normalized_key] = setting.value
        elif _is_secret_key(normalized_key):
            flag_name = SETTING_FLAG_NAMES.get(normalized_key)
            if not flag_name:
                continue
            response[flag_name] = _setting_has_value(setting.value)

    return response


@router.post("/settings", dependencies=[Depends(require_admin)])
async def post_settings(payload: dict, db: Session = Depends(get_db)):
    logger.info(
        "Settings POST received keys=%s secret_keys=%s",
        sorted(payload.keys()),
        sorted([key.upper() for key, value in payload.items() if _is_secret_key(key) and _setting_has_value(value) and not _is_placeholder_secret_value(value)]),
    )
    for k, v in payload.items():
        normalized_key = k.upper()
        if _is_secret_key(normalized_key) and (not _setting_has_value(v) or _is_placeholder_secret_value(v)):
            continue
        item = db.query(models.Setting).filter(models.Setting.key == normalized_key).one_or_none()
        if item:
            item.value = v
        else:
            item = models.Setting(key=normalized_key, value=v)
            db.add(item)

    for legacy_key in ("UNIFY_API_TOKEN", "ZOHO_ACCESS_TOKEN"):
        legacy_item = db.query(models.Setting).filter(models.Setting.key == legacy_key).one_or_none()
        if legacy_item:
            db.delete(legacy_item)

    db.commit()
    refreshed_settings = db.query(models.Setting).all()
    snapshot = _settings_presence_snapshot(refreshed_settings)
    logger.info(
        "Settings POST snapshot present_keys=%s blank_keys=%s flags=%s",
        snapshot["present_keys"],
        snapshot["blank_keys"],
        snapshot["flags"],
    )
    return APIResponse(ok=True, message="Settings saved")


@router.post("/settings/unify/test", response_model=ConnectionTestResult, dependencies=[Depends(require_admin)])
async def test_unify(db: Session = Depends(get_db)):
    settings = {s.key: s.value for s in db.query(models.Setting).all()}
    svc = UnifyService(
        base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
        client_id=settings.get("UNIFY_CLIENT_ID", ""),
        client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
    )
    try:
        ok = await svc.test_connection()
        return ConnectionTestResult(ok=ok, message="Unify connection OK" if ok else "Unify connection failed")
    except UnifyServiceError as e:
        return ConnectionTestResult(ok=False, message=str(e))


@router.post("/settings/zoho/test", response_model=ConnectionTestResult, dependencies=[Depends(require_admin)])
async def test_zoho(db: Session = Depends(get_db)):
    settings = {s.key: s.value for s in db.query(models.Setting).all()}
    svc = ZohoService(
        base_url=settings.get("ZOHO_BASE_URL", "https://www.zohoapis.eu"),
        client_id=settings.get("ZOHO_CLIENT_ID", ""),
        client_secret=settings.get("ZOHO_CLIENT_SECRET", ""),
        refresh_token=settings.get("ZOHO_REFRESH_TOKEN", ""),
        org_id=settings.get("ZOHO_ORG_ID", ""),
    )
    try:
        ok = await svc.test_connection()
        return ConnectionTestResult(ok=ok, message="Zoho connection OK" if ok else "Zoho connection failed")
    except ZohoServiceError as e:
        return ConnectionTestResult(ok=False, message=str(e))


@router.post("/unify/fetch-orders")
async def fetch_orders(request: FetchOrdersRequest, db: Session = Depends(get_db)):
    try:
        logger.info("Unify fetch-orders route entered")
        logger.info("Unify fetch-orders request received date_from=%s date_to=%s", request.date_from, request.date_to)
        request_started_at = datetime.utcnow()
        date_from = _normalize_request_date(request.date_from, "date_from")
        date_to = _normalize_request_date(request.date_to, "date_to")
        logger.info("Unify fetch-orders normalized date_from=%s date_to=%s", date_from, date_to)

        settings = {s.key: s.value for s in db.query(models.Setting).all()}
        unify = UnifyService(
            base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
            client_id=settings.get("UNIFY_CLIENT_ID", ""),
            client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
        )
        preview_started_at = datetime.utcnow()
        orders = await unify.fetch_orders_preview(date_from, date_to)
        preview_duration_seconds = (datetime.utcnow() - preview_started_at).total_seconds()
        logger.info(
            "TEMP unify preview fetch success date_from=%s date_to=%s order_count=%s duration_seconds=%.3f",
            date_from,
            date_to,
            len(orders),
            preview_duration_seconds,
        )
        export_statuses = {
            row[0]: row[1]
            for row in db.query(models.ExportedOrder.unify_order_id, models.ExportedOrder.status).all()
        }
        exported_order_ids = {order_id for order_id, record_status in export_statuses.items() if record_status != EXPORT_STATUS_RESET}

        customer_info = {}
        for order in orders:
            order.already_exported = order.order_id in exported_order_ids
            order.export_status = export_statuses.get(order.order_id)
            info = customer_info.setdefault(order.customer_name, {"order_count": 0, "total": 0.0})
            info["order_count"] += 1
            info["total"] += order.total

        debug_summary = dict(getattr(unify, "last_fetch_debug", {}) or {})
        preview_count = len(orders)
        ready_count = len([order for order in orders if order.preview_status == "ready" and not order.already_exported])
        blocked_in_range_count = len([order for order in orders if order.preview_status != "ready" and not order.already_exported])
        debug_summary.update(
            {
                "preview_count": preview_count,
                "raw_preview_count": preview_count,
                "ready_count": ready_count,
                "blocked_in_range_count": blocked_in_range_count,
                "blocked_count": blocked_in_range_count,
                "total_scanned_orders": debug_summary.get("total_scanned_orders", debug_summary.get("rawOrdersCount", 0)),
                "total_in_range_orders": debug_summary.get("total_in_range_orders", preview_count),
                "dropped_out_of_range_count": debug_summary.get("dropped_out_of_range_count", 0),
                "missing_items_count": 0,
                "item_fetch_failed_count": 0,
                "mapping_issue_count": 0,
                "detailFallbackCalls": debug_summary.get("detailFallbackAttempts", 0),
                "itemHydrationCalls": debug_summary.get("itemHydrationCalls", 0),
                "already_synced_count": len([order for order in orders if order.already_exported]),
                "droppedOrders": debug_summary.get("droppedOrders", []),
                "requestDurationSeconds": (datetime.utcnow() - request_started_at).total_seconds(),
                "totalDurationMs": round(debug_summary.get("totalDurationSeconds", 0) * 1000, 2),
            }
        )

        response = FetchOrdersResponse(
            total_orders=preview_count,
            total_customers=len(customer_info),
            customers=customer_info,
            orders=orders,
            debug_summary=debug_summary,
        )
        logger.info(
            "Unify fetch-orders response returning total_orders=%s total_customers=%s total_scanned_orders=%s previewCount=%s readyCount=%s blockedInRangeCount=%s droppedOutOfRangeCount=%s previewTruncated=%s previewDurationSeconds=%.3f",
            response.total_orders,
            response.total_customers,
            debug_summary.get("total_scanned_orders"),
            debug_summary.get("preview_count"),
            debug_summary.get("ready_count"),
            debug_summary.get("blocked_in_range_count"),
            debug_summary.get("dropped_out_of_range_count"),
            debug_summary.get("previewTruncated"),
            preview_duration_seconds,
        )
        status_code = 206 if debug_summary.get("previewTruncated") else 200
        return JSONResponse(status_code=status_code, content=jsonable_encoder(response))
    except Exception as e:
        logger.exception("TEMP unify preview fetch failed date_from=%s date_to=%s", request.date_from, request.date_to)
        print("=== FETCH ORDERS ERROR ===")
        print(str(e))
        print(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "error": "Fetch orders failed",
                "details": str(e),
                "trace": traceback.format_exc(),
            },
        )


@router.post("/unify/preview-csv", response_model=CsvPreviewResponse)
async def preview_csv(request: Request, db: Session = Depends(get_db)):
    try:
        logger.info("Unify preview-csv route entered")
        request_started_at = datetime.utcnow()

        content_type = (request.headers.get("content-type") or "").lower()
        file_bytes = None
        filename = "upload.csv"
        if "multipart/form-data" in content_type:
            form = await request.form()
            # find file field
            for v in form.values():
                if _is_upload_file_value(v):
                    file_bytes = await v.read()
                    filename = getattr(v, "filename", filename) or filename
                    break
            # fallback to text fields
            if file_bytes is None:
                csv_text = await _read_csv_payload(request)
                file_bytes = csv_text.encode("utf-8")
        else:
            # raw body (text)
            csv_text = await _read_csv_payload(request)
            file_bytes = csv_text.encode("utf-8")

        if not file_bytes:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV file is required")

        try:
            orders_list, parser_debug = csv_parser.parse_unify_file_bytes(file_bytes, filename)
        except ValueError as ve:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve)) from ve
        except RuntimeError as rexc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(rexc)) from rexc

        # convert parsed dicts to UnifyOrder pydantic models
        parsed_orders: List[UnifyOrder] = []
        for o in orders_list:
            try:
                parsed = UnifyOrder.model_validate(o)
            except Exception:
                # best-effort fallback
                parsed = UnifyOrder(**{
                    "order_id": o.get("order_id"),
                    "customer_name": o.get("customer_name"),
                    "order_date": o.get("order_date", ""),
                    "delivery_date": o.get("delivery_date", ""),
                    "delivery_address": o.get("delivery_address"),
                    "lines": o.get("lines", []),
                    "total": o.get("total", 0.0),
                })
            parsed_orders.append(parsed)

        preview_payload = _build_preview_response_payload(parsed_orders, db, dict(parser_debug or {}), request_started_at=request_started_at, source="csv")
        response = CsvPreviewResponse(**preview_payload)
        return JSONResponse(content=jsonable_encoder(response))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unify preview-csv failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "CSV preview failed",
                "details": str(e),
                "trace": traceback.format_exc(),
            },
        )


@router.get("/unify/order-preview/{order_id}")
async def fetch_order_preview(order_id: str, db: Session = Depends(get_db)):
    try:
        logger.info("Unify order-preview route entered order_id=%s", order_id)
        detail_started_at = datetime.utcnow()
        settings = {s.key: s.value for s in db.query(models.Setting).all()}
        unify = UnifyService(
            base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
            client_id=settings.get("UNIFY_CLIENT_ID", ""),
            client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
        )
        order = await unify.fetch_order_with_items(order_id)
        detail_duration_seconds = (datetime.utcnow() - detail_started_at).total_seconds()
        logger.info(
            "TEMP unify order detail lazy-load success order_id=%s line_count=%s duration_seconds=%.3f",
            order_id,
            len(order.lines),
            detail_duration_seconds,
        )
        return JSONResponse(content=jsonable_encoder(order))
    except Exception as e:
        logger.exception("TEMP unify order detail lazy-load failed order_id=%s", order_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Order preview failed",
                "details": str(e),
                "trace": traceback.format_exc(),
            },
        )


@router.get("/unify/debug-samples", dependencies=[Depends(require_admin)])
async def debug_unify_samples(date_from: str, date_to: str, order_id: str | None = None, db: Session = Depends(get_db)):
    try:
        logger.info(
            "Unify debug-samples route entered date_from=%s date_to=%s order_id=%s",
            date_from,
            date_to,
            order_id,
        )
        settings = {s.key: s.value for s in db.query(models.Setting).all()}
        unify = UnifyService(
            base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
            client_id=settings.get("UNIFY_CLIENT_ID", ""),
            client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
        )

        buyers = await unify.fetch_buyers()
        products = await unify.fetch_products()
        previews = await unify.fetch_orders_preview(_normalize_request_date(date_from, "date_from"), _normalize_request_date(date_to, "date_to"))

        sample_order_id = order_id or (previews[0].order_id if previews else None)
        sample_detail = None
        sample_items = None
        if sample_order_id:
            sample_detail = await unify.fetch_order_detail(sample_order_id)
            sample_items = await unify.fetch_order_items(sample_order_id)

        return JSONResponse(
            content=jsonable_encoder(
                {
                    "buyers_count": len(buyers),
                    "products_count": len(products),
                    "preview_count": len(previews),
                    "sample_order_id": sample_order_id,
                    "sample_detail_keys": sorted(sample_detail.keys()) if isinstance(sample_detail, dict) else [],
                    "sample_items_count": len(sample_items) if isinstance(sample_items, list) else 0,
                    "debug_summary": getattr(unify, "last_fetch_debug", {}),
                }
            )
        )
    except Exception as e:
        logger.exception("TEMP unify debug samples failed date_from=%s date_to=%s order_id=%s", date_from, date_to, order_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Debug samples failed",
                "details": str(e),
                "trace": traceback.format_exc(),
            },
        )


@router.post("/export/run")
async def run_export(request: ExportRunRequest, db: Session = Depends(get_db), http_request: Request = None):
    logger.info("Unify export-run route entered")
    logger.info("Unify export-run request received date_from=%s date_to=%s", request.date_from, request.date_to)
    date_from = _normalize_request_date(request.date_from, "date_from")
    date_to = _normalize_request_date(request.date_to, "date_to")
    logger.info("Unify export-run normalized date_from=%s date_to=%s", date_from, date_to)

    settings = {s.key: s.value for s in db.query(models.Setting).all()}
    unify = UnifyService(
        base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
        client_id=settings.get("UNIFY_CLIENT_ID", ""),
        client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
    )
    zoho = ZohoService(
        base_url=settings.get("ZOHO_BASE_URL", "https://www.zohoapis.eu"),
        client_id=settings.get("ZOHO_CLIENT_ID", ""),
        client_secret=settings.get("ZOHO_CLIENT_SECRET", ""),
        refresh_token=settings.get("ZOHO_REFRESH_TOKEN", ""),
        org_id=settings.get("ZOHO_ORG_ID", ""),
    )

    export_service = make_export_service(unify, zoho)
    export_service.audit = _audit_context(db, http_request)

    try:
        sync_started_at = datetime.utcnow()
        orders = await unify.fetch_orders_for_export(date_from, date_to, request.order_ids)
        logger.info(
            "TEMP unify sync hydration success date_from=%s date_to=%s order_ids=%s order_count=%s duration_seconds=%.3f",
            date_from,
            date_to,
            request.order_ids,
            len(orders),
            (datetime.utcnow() - sync_started_at).total_seconds(),
        )
    except UnifyServiceError as e:
        logger.exception("TEMP unify sync hydration failed")
        raise HTTPException(status_code=400, detail=str(e))

    sync_started_at = datetime.utcnow()
    try:
        result = await export_service.run_export(
            db,
            date_from,
            date_to,
            orders,
            order_ids=request.order_ids,
            settings=settings,
        )
        logger.info(
            "TEMP unify single-order sync completed date_from=%s date_to=%s order_ids=%s status=%s duration_seconds=%.3f",
            date_from,
            date_to,
            request.order_ids,
            result.get("status"),
            (datetime.utcnow() - sync_started_at).total_seconds(),
        )
        return result
    except ExportServiceError as e:
        logger.exception(
            "Export run failed in route date_from=%s date_to=%s order_ids=%s error=%s",
            date_from,
            date_to,
            request.order_ids,
            str(e),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Export failed",
                "details": str(e),
            },
        )
    except Exception as e:
        logger.exception(
            "Unexpected export failure in route date_from=%s date_to=%s order_ids=%s",
            date_from,
            date_to,
            request.order_ids,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Export failed",
                "details": str(e),
            },
        )


@router.post("/export/run-csv")
async def run_export_csv(request: ExportCsvRunRequest, db: Session = Depends(get_db), http_request: Request = None):
    logger.info("Unify export-run-csv route entered order_count=%s", len(request.orders))
    if not request.orders:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="orders is required")

    settings = {s.key: s.value for s in db.query(models.Setting).all()}
    unify = UnifyService(
        base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
        client_id=settings.get("UNIFY_CLIENT_ID", ""),
        client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
    )
    zoho = ZohoService(
        base_url=settings.get("ZOHO_BASE_URL", "https://www.zohoapis.eu"),
        client_id=settings.get("ZOHO_CLIENT_ID", ""),
        client_secret=settings.get("ZOHO_CLIENT_SECRET", ""),
        refresh_token=settings.get("ZOHO_REFRESH_TOKEN", ""),
        org_id=settings.get("ZOHO_ORG_ID", ""),
    )
    export_service = make_export_service(unify, zoho)
    export_service.audit = _audit_context(db, http_request)
    date_from, date_to = _derive_csv_export_range(request.orders, request.date_from, request.date_to)
    logger.info(
        "Unify export-run-csv derived date range date_from=%s date_to=%s order_ids=%s",
        date_from,
        date_to,
        [order.order_id for order in request.orders],
    )

    try:
        result = await export_service.run_export(db, date_from, date_to, request.orders)
        logger.info(
            "Unify export-run-csv completed date_from=%s date_to=%s status=%s created=%s skipped=%s failed=%s",
            date_from,
            date_to,
            result.get("status"),
            result.get("created"),
            result.get("skipped"),
            result.get("failed"),
        )
        return result
    except ExportServiceError as e:
        logger.exception("Export run-csv failed date_from=%s date_to=%s", date_from, date_to)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Export failed",
                "details": str(e),
            },
        )
    except Exception as e:
        logger.exception("Unexpected export-run-csv failure date_from=%s date_to=%s", date_from, date_to)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Export failed",
                "details": str(e),
            },
        )


# ---------------------------------------------------------------------------
# Analytics (company-scoped; see services/analytics_service.py)
# ---------------------------------------------------------------------------

ANALYTICS_UNAVAILABLE = "Analytics is not available yet: the database needs migration 0003_analytics."


def _analytics_company(identity: Identity, requested: Optional[str]) -> str:
    """Everyone sees their own company. Only platform admins may choose another one."""
    company_id = (requested or "").strip()
    if not company_id or company_id == identity.company_id:
        return identity.company_id
    if not identity.is_platform_admin:
        logger.warning("Rejected cross-company analytics request from user %s (company %s)", identity.user_id, identity.company_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own company's analytics")
    if not COMPANY_ID_PATTERN.match(company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company")
    return company_id


def _analytics_call(db: Session, fn):
    try:
        return fn()
    except analytics_service.AnalyticsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Analytics query failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=ANALYTICS_UNAVAILABLE) from exc


@router.post("/analytics/import-zoho-history", dependencies=[Depends(require_admin)])
async def import_zoho_history(
    dry_run: bool = True,
    identity: Identity = Depends(require_identity),
    db: Session = Depends(get_db),
):
    """Explicit admin-only historical backfill; no Zoho write operations.

    Intentionally disabled until the approved 0004 migration and feature flag
    have been deployed. Dry run validates every page and every invoice first.
    The current Zoho credentials are global, so only the default company can
    import them; never map one tenant's invoices to another tenant.
    """
    import os
    if os.environ.get("ZCONNECT_ENABLE_ZOHO_HISTORY_IMPORT") != "1":
        raise HTTPException(status_code=503, detail="Zoho history import is disabled")
    if identity.company_id != models.DEFAULT_COMPANY_ID:
        raise HTTPException(status_code=403, detail="History import is limited to the configured Zoho organisation's company")
    zoho = make_zoho_service()
    try:
        snapshot = await zoho_history_service.fetch_snapshot(zoho)
        if dry_run:
            return {"dry_run": True, "validated_invoices": len(snapshot), "saved": False}
        result = zoho_history_service.persist_snapshot(
            db, company_id=models.DEFAULT_COMPANY_ID, zoho_org_id=zoho.org_id, snapshot=snapshot
        )
        logger.info("Zoho history imported %s invoices for default company by %s", result["total"], identity.user_id)
        return {"dry_run": False, "saved": True, **result}
    except (zoho_history_service.HistoricalImportError, ZohoLookupError, ZohoServiceError) as exc:
        db.rollback()
        logger.warning("Zoho history validation failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=422, detail="Zoho invoice snapshot is incomplete or invalid; no history was saved") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Zoho history persistence error: %s", exc.__class__.__name__)
        raise HTTPException(status_code=503, detail="Historical invoice storage is unavailable; check migration 0004") from exc


@router.get("/analytics/summary")
async def analytics_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    granularity: Optional[str] = None,
    customer: Optional[str] = None,
    product: Optional[str] = None,
    company_id: Optional[str] = None,
    identity: Identity = Depends(require_identity),
    db: Session = Depends(get_db),
):
    company = _analytics_company(identity, company_id)
    return _analytics_call(
        db,
        lambda: analytics_service.compute_summary(
            db, analytics_service.build_filters(company, date_from, date_to, granularity, customer, product)
        ),
    )


@router.get("/analytics/filters")
async def analytics_filters(company_id: Optional[str] = None, identity: Identity = Depends(require_identity), db: Session = Depends(get_db)):
    company = _analytics_company(identity, company_id)
    return _analytics_call(db, lambda: analytics_service.filter_options(db, company))


@router.get("/analytics/companies")
async def analytics_companies(identity: Identity = Depends(require_identity), db: Session = Depends(get_db)):
    def load():
        companies = analytics_service.list_companies(db)
        if identity.is_platform_admin:
            return {"companies": companies, "can_switch": True, "current": identity.company_id}
        own = [c for c in companies if c["id"] == identity.company_id] or [{"id": identity.company_id, "name": identity.company_id}]
        return {"companies": own, "can_switch": False, "current": identity.company_id}

    return _analytics_call(db, load)


@router.get("/reports/weekly.pdf")
def weekly_report_pdf(
    period: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    company_id: Optional[str] = None,
    identity: Identity = Depends(require_identity),
    db: Session = Depends(get_db),
):
    """Branded PDF for one company and period (default: last full Monday-Sunday week, Irish time).
    Read-only: built from the same company-scoped calculation as the Analytics dashboard."""
    company = _analytics_company(identity, company_id)
    try:
        report_period = resolve_period(period, date_from, date_to)
    except ReportPeriodError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    model = _analytics_call(db, lambda: weekly_report.build_report_model(db, company, report_period))
    pdf = weekly_report.render_pdf(model)
    logger.info("Generated report for company %s (%s to %s) for user %s", company, report_period.start, report_period.end, identity.user_id)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{weekly_report.report_filename(model)}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/export/history", response_model=List[dict])
async def export_history(db: Session = Depends(get_db)):
    runs = db.query(models.SyncRun).order_by(models.SyncRun.started_at.desc()).all()
    history = []
    for run in runs:
        history.append(
            {
                "id": run.id,
                "date_from": run.date_from,
                "date_to": run.date_to,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "status": run.status,
                "total_orders": run.total_orders,
                "total_customers": run.total_customers,
                "total_invoices": run.total_invoices,
                "errors": run.errors,
            }
        )
    return history


# Outcome a sync run itself recorded for an order, from its processing events (status shown,
# summary). Later admin actions (reset, reconcile) are not part of the run's outcome.
_RUN_OUTCOME_EVENTS = {
    "invoice_created": ("exported", "Draft invoice created in this run."),
    "invoice_linked": ("exported", "Existing Zoho invoice linked in this run; no new invoice created."),
    "failed": ("failed", "Failed in this run."),
    "needs_verification": ("needs_verification", "Outcome unknown in this run; check Zoho before retrying."),
    "stale_attempt_rejected": ("needs_verification", "This run's outcome was not applied because the record changed concurrently."),
    "skipped": ("skipped", "Not exported in this run (preview status)."),
    "sync_blocked": ("skipped", "Not processed in this run."),
    "sync_error": ("failed", "Unexpected error in this run."),
    "claimed": ("in_progress", "Claimed in this run; no final outcome was recorded."),
}


def _run_order_outcomes(db: Session, run_id: int) -> dict:
    """Last processing event per order in this run (runs before the audit trail have none)."""
    outcomes: dict = {}
    events = (
        db.query(models.ExportEvent)
        .filter(models.ExportEvent.sync_run_id == run_id, models.ExportEvent.event_type.in_(list(_RUN_OUTCOME_EVENTS)))
        .order_by(models.ExportEvent.id)
        .all()
    )
    for event in events:
        outcomes[event.unify_order_id] = event
    return outcomes


def _history_order_entry(order_id: str, record, event) -> dict:
    """One order line of a run's history: this run's own outcome when the audit trail recorded
    it, otherwise (older runs) the export record as stored."""
    if event is None:
        return {
            "unify_order_id": order_id,
            "customer_name": record.customer_name,
            "status": record.status,
            "message": record.message,
            "created_at": record.created_at,
        }
    status, summary = _RUN_OUTCOME_EVENTS[event.event_type]
    parts = [summary]
    if event.zoho_invoice_id:
        parts.append(f"Zoho invoice {event.zoho_invoice_id}.")
    if event.detail and event.event_type not in ("invoice_created", "invoice_linked"):
        parts.append(event.detail)
    if record is not None and record.status != status:
        parts.append(f"Current status: {record.status}.")
    return {
        "unify_order_id": order_id,
        "customer_name": record.customer_name if record is not None else None,
        "status": status,
        "message": " ".join(parts),
        "created_at": event.created_at,
    }


@router.get("/export/history/{id}")
async def export_history_detail(id: int, db: Session = Depends(get_db)):
    run = db.query(models.SyncRun).filter(models.SyncRun.id == id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    records = (
        db.query(models.ExportedOrder)
        .filter(models.ExportedOrder.sync_run_id == id)
        .all()
    )
    outcomes = _run_order_outcomes(db, id)
    orders = [_history_order_entry(r.unify_order_id, r, outcomes.get(r.unify_order_id)) for r in records]
    # Orders this run processed whose export record was first created by an earlier run (retries).
    listed = {r.unify_order_id for r in records}
    for order_id, event in outcomes.items():
        if order_id not in listed:
            record = db.query(models.ExportedOrder).filter(models.ExportedOrder.unify_order_id == order_id).one_or_none()
            orders.append(_history_order_entry(order_id, record, event))
    invoices = (
        db.query(models.ExportedInvoice)
        .filter(models.ExportedInvoice.sync_run_id == id)
        .all()
    )

    return {
        "sync_run": {
            "id": run.id,
            "date_from": run.date_from,
            "date_to": run.date_to,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "status": run.status,
            "total_orders": run.total_orders,
            "total_customers": run.total_customers,
            "total_invoices": run.total_invoices,
            "errors": run.errors,
        },
        "orders": orders,
        "invoices": [
            {
                "unify_customer_name": i.unify_customer_name,
                "unify_order_ids": i.unify_order_ids,
                "zoho_invoice_id": i.zoho_invoice_id,
                "status": i.status,
                "message": i.message,
                "created_at": i.created_at,
            }
            for i in invoices
        ],
    }


def _audit_context(db: Session, http_request: Request | None) -> AuditContext:
    """Tenant + actor for export events.

    tenant_id: TENANT_ID setting, else the configured Zoho organisation id, else "default"
    (each deployment serves one company). actor_id: the user id from the request's verified,
    signed identity (app/security.py); no client-supplied header is trusted.
    """
    configured = {
        row.key: (row.value or "").strip()
        for row in db.query(models.Setting).filter(models.Setting.key.in_(["TENANT_ID", "ZOHO_ORG_ID"])).all()
    }
    tenant_id = (configured.get("TENANT_ID") or configured.get("ZOHO_ORG_ID") or "default")[:64]
    identity = getattr(getattr(http_request, "state", None), "identity", None)
    actor_id = identity.user_id[:128] if isinstance(identity, Identity) else None
    company_id = identity.company_id if isinstance(identity, Identity) else models.DEFAULT_COMPANY_ID
    return AuditContext(tenant_id=tenant_id, actor_id=actor_id, company_id=company_id)


def _export_record_counts(db: Session) -> dict:
    return {
        "exported_orders": db.query(models.ExportedOrder).count(),
        "exported_invoices": db.query(models.ExportedInvoice).count(),
        "sync_runs": db.query(models.SyncRun).count(),
    }


def _soft_reset_response_data(outcome: dict, before: dict, after: dict) -> dict:
    # Soft reset: nothing is deleted and sync runs are not rewritten. The legacy
    # deleted_* / updated_sync_runs keys are kept (always 0) for older clients.
    return {
        "reset_exported_orders": len(outcome["reset"]),
        "already_reset_orders": len(outcome["already_reset"]),
        "blocked_orders": len(outcome["blocked"]),
        "blocked_order_ids": outcome["blocked"],
        "deleted_exported_orders": 0,
        "deleted_exported_invoices": 0,
        "updated_sync_runs": 0,
        "before": before,
        "after": after,
    }


@router.post("/export/reset-sync-state", response_model=APIResponse, dependencies=[Depends(require_admin)])
async def reset_sync_state(db: Session = Depends(get_db), http_request: Request = None):
    """Soft-reset every export record. Nothing is deleted; invoices, run totals and
    history are kept; records that need Zoho verification are left unchanged."""
    logger.warning("Export sync-state soft reset requested")
    before = _export_record_counts(db)
    try:
        records = db.query(models.ExportedOrder).all()
        outcome = soft_reset_export_records(db, records, source="reset all", audit=_audit_context(db, http_request))
        after = _export_record_counts(db)
        logger.warning(
            "Export sync-state soft reset completed reset=%s already_reset=%s blocked=%s before=%s after=%s",
            len(outcome["reset"]),
            len(outcome["already_reset"]),
            outcome["blocked"],
            before,
            after,
        )
        return APIResponse(ok=True, message="Sync state reset; existing records were kept", data=_soft_reset_response_data(outcome, before, after))
    except Exception as exc:
        db.rollback()
        logger.exception("Export sync-state soft reset failed before=%s", before)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"ok": False, "message": "Sync state reset failed", "error": str(exc), "before": before},
        )


@router.post("/export/reset-order/{order_id}", response_model=APIResponse, dependencies=[Depends(require_admin)])
async def reset_order(order_id: str, db: Session = Depends(get_db), http_request: Request = None):
    """Soft-reset one order (Unsync). The record, its invoice id and its history are kept;
    the next sync checks Zoho first and links an existing invoice instead of creating one."""
    logger.warning("Export single-order soft reset requested order_id=%s", order_id)
    exported_order = (
        db.query(models.ExportedOrder)
        .filter(models.ExportedOrder.unify_order_id == order_id)
        .one_or_none()
    )
    if not exported_order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found in export history")

    previous_status = exported_order.status
    outcome = soft_reset_export_records(db, [exported_order], source="unsync", audit=_audit_context(db, http_request))
    if order_id in outcome["blocked"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This order may already have a Zoho invoice and must be checked in Zoho before it can be reset",
        )
    logger.warning("Export single-order soft reset completed order_id=%s previous_status=%s", order_id, previous_status)
    return APIResponse(
        ok=True,
        message="Order reset; the next sync will check Zoho before creating an invoice",
        data={"order_id": order_id, "previous_status": previous_status, "status": EXPORT_STATUS_RESET},
    )


@router.post("/export/reset-selected-runs", response_model=ResetSelectedRunsResponse, dependencies=[Depends(require_admin)])
async def reset_selected_runs(request: ResetSelectedRunsRequest, db: Session = Depends(get_db), http_request: Request = None):
    """Soft-reset the export records of the selected runs. Nothing is deleted and the runs
    themselves (status, totals, timestamps) are not rewritten."""
    run_ids = list(dict.fromkeys(request.run_ids))
    logger.warning("Export selected-runs soft reset requested run_ids=%s", run_ids)

    if not run_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="run_ids is required")

    matching_runs = db.query(models.SyncRun).filter(models.SyncRun.id.in_(run_ids)).all()
    found_run_ids = [run.id for run in matching_runs]
    missing_run_ids = [run_id for run_id in run_ids if run_id not in found_run_ids]

    if not matching_runs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No matching runs found")

    before = _export_record_counts(db)
    try:
        records = db.query(models.ExportedOrder).filter(models.ExportedOrder.sync_run_id.in_(found_run_ids)).all()
        outcome = soft_reset_export_records(db, records, source=f"history reset of runs {found_run_ids}", audit=_audit_context(db, http_request))
        after = _export_record_counts(db)
        logger.warning(
            "Export selected-runs soft reset completed requested_run_ids=%s found_run_ids=%s reset=%s already_reset=%s blocked=%s",
            run_ids,
            found_run_ids,
            len(outcome["reset"]),
            len(outcome["already_reset"]),
            outcome["blocked"],
        )
        data = _soft_reset_response_data(outcome, before, after)
        data.update({"requested_run_ids": run_ids, "found_run_ids": found_run_ids, "missing_run_ids": missing_run_ids})
        return ResetSelectedRunsResponse(ok=True, message="Selected runs reset; existing records were kept", data=data)
    except Exception as exc:
        db.rollback()
        logger.exception("Export selected-runs soft reset failed requested_run_ids=%s found_run_ids=%s", run_ids, found_run_ids)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "ok": False,
                "message": "Selected runs reset failed",
                "error": str(exc),
                "data": {"requested_run_ids": run_ids, "found_run_ids": found_run_ids, "missing_run_ids": missing_run_ids},
            },
        )


def _make_settings_export_service(db: Session) -> ExportService:
    settings = {s.key: s.value for s in db.query(models.Setting).all()}
    unify = UnifyService(
        base_url=settings.get("UNIFY_BASE_URL", "https://api.unifyordering.com"),
        client_id=settings.get("UNIFY_CLIENT_ID", ""),
        client_secret=settings.get("UNIFY_CLIENT_SECRET", ""),
    )
    zoho = ZohoService(
        base_url=settings.get("ZOHO_BASE_URL", "https://www.zohoapis.eu"),
        client_id=settings.get("ZOHO_CLIENT_ID", ""),
        client_secret=settings.get("ZOHO_CLIENT_SECRET", ""),
        refresh_token=settings.get("ZOHO_REFRESH_TOKEN", ""),
        org_id=settings.get("ZOHO_ORG_ID", ""),
    )
    return make_export_service(unify, zoho)


@router.post("/export/reconcile-order/{order_id}", response_model=APIResponse, dependencies=[Depends(require_admin)])
async def reconcile_order(order_id: str, db: Session = Depends(get_db), http_request: Request = None):
    """Check Zoho for an order that is in_progress (abandoned) or needs_verification.
    Read-only towards Zoho: links an existing invoice, marks the order reset if none exists,
    or keeps it under review. Never creates an invoice."""
    logger.warning("Export reconciliation requested order_id=%s", order_id)
    export_service = _make_settings_export_service(db)
    export_service.audit = _audit_context(db, http_request)
    try:
        result = await export_service.reconcile_order(db, order_id)
    except ReconcileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    logger.warning("Export reconciliation completed order_id=%s result=%s", order_id, result)
    return APIResponse(ok=True, message=result["message"], data=result)
