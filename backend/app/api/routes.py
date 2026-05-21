import logging
import re
from datetime import datetime
import json
import traceback

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from typing import Any, List

from ..db import get_db
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
from ..services import csv_parser
from ..services.zoho_service import ZohoService, make_zoho_service, ZohoServiceError
from ..services.export_service import make_export_service, ExportService, ExportServiceError

router = APIRouter()
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
    exported_order_ids = {
        row[0]
        for row in db.query(models.ExportedOrder.unify_order_id).all()
    }

    customer_info: dict[str, dict[str, float | int]] = {}
    preview_count = len(orders)
    ready_count = 0
    blocked_in_range_count = 0

    for order in orders:
        order.already_exported = order.order_id in exported_order_ids
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


@router.post("/settings")
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


@router.post("/settings/unify/test", response_model=ConnectionTestResult)
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


@router.post("/settings/zoho/test", response_model=ConnectionTestResult)
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
        exported_order_ids = {
            row[0]
            for row in db.query(models.ExportedOrder.unify_order_id).all()
        }

        customer_info = {}
        for order in orders:
            order.already_exported = order.order_id in exported_order_ids
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


@router.get("/unify/debug-samples")
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
async def run_export(request: ExportRunRequest, db: Session = Depends(get_db)):
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
async def run_export_csv(request: ExportCsvRunRequest, db: Session = Depends(get_db)):
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


@router.get("/export/history/{id}")
async def export_history_detail(id: int, db: Session = Depends(get_db)):
    run = db.query(models.SyncRun).filter(models.SyncRun.id == id).one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    orders = (
        db.query(models.ExportedOrder)
        .filter(models.ExportedOrder.sync_run_id == id)
        .all()
    )
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
        "orders": [
            {
                "unify_order_id": o.unify_order_id,
                "customer_name": o.customer_name,
                "status": o.status,
                "message": o.message,
                "created_at": o.created_at,
            }
            for o in orders
        ],
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


@router.post("/export/reset-sync-state", response_model=APIResponse)
async def reset_sync_state(db: Session = Depends(get_db)):
    logger.warning("Export sync-state reset requested")
    before_sync_runs = db.query(models.SyncRun).count()
    before_exported_orders = db.query(models.ExportedOrder).count()
    before_exported_invoices = db.query(models.ExportedInvoice).count()

    try:
        db.query(models.ExportedOrder).delete(synchronize_session=False)
        db.query(models.ExportedInvoice).delete(synchronize_session=False)

        db.query(models.SyncRun).update(
            {
                models.SyncRun.status: "pending",
                models.SyncRun.total_invoices: 0,
                models.SyncRun.errors: None,
                models.SyncRun.finished_at: None,
            },
            synchronize_session=False,
        )

        db.commit()

        after_sync_runs = db.query(models.SyncRun).count()
        after_exported_orders = db.query(models.ExportedOrder).count()
        after_exported_invoices = db.query(models.ExportedInvoice).count()

        logger.warning(
            "Export sync-state reset completed before_sync_runs=%s before_exported_orders=%s before_exported_invoices=%s after_sync_runs=%s after_exported_orders=%s after_exported_invoices=%s",
            before_sync_runs,
            before_exported_orders,
            before_exported_invoices,
            after_sync_runs,
            after_exported_orders,
            after_exported_invoices,
        )

        return APIResponse(
            ok=True,
            message="Sync state reset",
            data={
                "before": {
                    "exported_orders": before_exported_orders,
                    "exported_invoices": before_exported_invoices,
                    "sync_runs": before_sync_runs,
                },
                "after": {
                    "exported_orders": after_exported_orders,
                    "exported_invoices": after_exported_invoices,
                    "sync_runs": after_sync_runs,
                },
                "sync_run_status_set_to": "pending",
                "total_invoices_set_to": 0,
                "errors_cleared": True,
            },
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Export sync-state reset failed before_sync_runs=%s before_exported_orders=%s before_exported_invoices=%s", before_sync_runs, before_exported_orders, before_exported_invoices)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "ok": False,
                "message": "Sync state reset failed",
                "error": str(exc),
                "before": {
                    "exported_orders": before_exported_orders,
                    "exported_invoices": before_exported_invoices,
                    "sync_runs": before_sync_runs,
                },
            },
        )


@router.post("/export/reset-selected-runs", response_model=ResetSelectedRunsResponse)
async def reset_selected_runs(request: ResetSelectedRunsRequest, db: Session = Depends(get_db)):
    run_ids = list(dict.fromkeys(request.run_ids))
    logger.warning("Export selected-runs reset requested run_ids=%s", run_ids)

    if not run_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="run_ids is required")

    matching_runs = db.query(models.SyncRun).filter(models.SyncRun.id.in_(run_ids)).all()
    found_run_ids = [run.id for run in matching_runs]
    missing_run_ids = [run_id for run_id in run_ids if run_id not in found_run_ids]

    if not matching_runs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No matching runs found")

    before_exported_orders = db.query(models.ExportedOrder).filter(models.ExportedOrder.sync_run_id.in_(found_run_ids)).count()
    before_exported_invoices = db.query(models.ExportedInvoice).filter(models.ExportedInvoice.sync_run_id.in_(found_run_ids)).count()

    try:
        deleted_orders = (
            db.query(models.ExportedOrder)
            .filter(models.ExportedOrder.sync_run_id.in_(found_run_ids))
            .delete(synchronize_session=False)
        )
        deleted_invoices = (
            db.query(models.ExportedInvoice)
            .filter(models.ExportedInvoice.sync_run_id.in_(found_run_ids))
            .delete(synchronize_session=False)
        )

        updated_runs = (
            db.query(models.SyncRun)
            .filter(models.SyncRun.id.in_(found_run_ids))
            .update(
                {
                    models.SyncRun.status: "pending",
                    models.SyncRun.total_invoices: 0,
                    models.SyncRun.errors: None,
                    models.SyncRun.finished_at: None,
                },
                synchronize_session=False,
            )
        )

        db.commit()

        after_exported_orders = db.query(models.ExportedOrder).filter(models.ExportedOrder.sync_run_id.in_(found_run_ids)).count()
        after_exported_invoices = db.query(models.ExportedInvoice).filter(models.ExportedInvoice.sync_run_id.in_(found_run_ids)).count()

        logger.warning(
            "Export selected-runs reset completed requested_run_ids=%s found_run_ids=%s deleted_orders=%s deleted_invoices=%s updated_runs=%s",
            run_ids,
            found_run_ids,
            deleted_orders,
            deleted_invoices,
            updated_runs,
        )

        return ResetSelectedRunsResponse(
            ok=True,
            message="Selected runs reset",
            data={
                "requested_run_ids": run_ids,
                "found_run_ids": found_run_ids,
                "missing_run_ids": missing_run_ids,
                "deleted_exported_orders": deleted_orders,
                "deleted_exported_invoices": deleted_invoices,
                "updated_sync_runs": updated_runs,
                "before": {
                    "exported_orders": before_exported_orders,
                    "exported_invoices": before_exported_invoices,
                },
                "after": {
                    "exported_orders": after_exported_orders,
                    "exported_invoices": after_exported_invoices,
                },
                "sync_run_status_set_to": "pending",
                "total_invoices_set_to": 0,
                "errors_cleared": True,
                "finished_at_cleared": True,
            },
        )
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Export selected-runs reset failed requested_run_ids=%s found_run_ids=%s",
            run_ids,
            found_run_ids,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "ok": False,
                "message": "Selected runs reset failed",
                "error": str(exc),
                "data": {
                    "requested_run_ids": run_ids,
                    "found_run_ids": found_run_ids,
                    "missing_run_ids": missing_run_ids,
                },
            },
        )
