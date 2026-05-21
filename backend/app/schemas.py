from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class SettingItem(BaseModel):
    key: str
    value: str


class SettingsResponse(BaseModel):
    ZOHO_BASE_URL: str
    ZOHO_STANDARD_TAX_ID: Optional[str] = None
    ZOHO_REDUCED_TAX_ID: Optional[str] = None
    ZOHO_ZERO_TAX_ID: Optional[str] = None
    has_unify_client_id: bool
    has_unify_client_secret: bool
    has_zoho_client_id: bool
    has_zoho_client_secret: bool
    has_zoho_refresh_token: bool
    has_zoho_organization_id: bool


class SettingsUpdate(BaseModel):
    unify_base_url: Optional[str]
    unify_api_token: Optional[str]
    zoho_base_url: Optional[str]
    zoho_standard_tax_id: Optional[str]
    zoho_reduced_tax_id: Optional[str]
    zoho_zero_tax_id: Optional[str]
    zoho_client_id: Optional[str]
    zoho_client_secret: Optional[str]
    zoho_refresh_token: Optional[str]
    zoho_org_id: Optional[str]


class ConnectionTestResult(BaseModel):
    ok: bool = Field(..., description="True if connection test succeeded")
    message: str = Field(..., description="Human-readable status message")


class FetchOrdersRequest(BaseModel):
    date_from: str
    date_to: str


class OrderLine(BaseModel):
    item_sku: str
    item_name: str
    quantity: float
    price: float
    unify_product_key: Optional[str] = None
    product_id: Optional[str] = None
    tax_percentage: Optional[float] = None
    line_type: str = "product"


class UnifyOrderPreview(BaseModel):
    order_id: str
    customer_name: str
    buyer_name: Optional[str] = None
    buyer_id: Optional[str] = None
    delivery_address: Optional[str] = None
    delivery_date: str = ""
    total: float
    status: str = "confirmed"
    preview_status: str = "ready"
    preview_reason: str = ""
    already_exported: bool = False


class UnifyOrder(BaseModel):
    order_id: str
    customer_name: str
    buyer_name: Optional[str] = None
    order_date: str
    delivery_date: str = ""
    delivery_address: Optional[str] = None
    lines: List[OrderLine]
    total: float
    buyer_id: Optional[str] = None
    status: str = "confirmed"
    preview_status: str = "ready"
    preview_reason: str = ""
    already_exported: bool = False
    total_net_amount: float = 0.0
    total_vat_amount: float = 0.0
    total_delivery_fee: float = 0.0


class FetchOrdersResponse(BaseModel):
    total_orders: int
    total_customers: int
    customers: Dict[str, Any]
    orders: List[UnifyOrderPreview]
    debug_summary: Optional[Dict[str, Any]] = None


class CsvPreviewResponse(BaseModel):
    total_orders: int
    total_customers: int
    customers: Dict[str, Any]
    orders: List[UnifyOrder]
    debug_summary: Optional[Dict[str, Any]] = None


class ExportCsvRunRequest(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    orders: List[UnifyOrder]


class CsvPreviewResponse(BaseModel):
    total_orders: int
    total_customers: int
    customers: Dict[str, Any]
    orders: List[UnifyOrder]
    debug_summary: Optional[Dict[str, Any]] = None


class ExportItemLine(BaseModel):
    item_sku: str
    item_name: str
    quantity: float
    price: float


class ExportCustomerBatch(BaseModel):
    customer_name: str
    orders: List[UnifyOrder]
    total: float


class ExportRunRequest(BaseModel):
    date_from: str
    date_to: str
    order_ids: Optional[List[str]] = None


class ExportCsvRunRequest(BaseModel):
    orders: List[UnifyOrder]
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class ResetSelectedRunsRequest(BaseModel):
    run_ids: List[int]


class ResetSelectedRunsResponse(BaseModel):
    ok: bool = Field(..., description="Success flag")
    message: str = Field(..., description="Human-readable status message")
    data: Dict[str, Any] = Field(default_factory=dict)


class ZohoInvoiceLineItem(BaseModel):
    name: str
    quantity: float
    rate: float
    tax_id: Optional[str] = None
    item_id: Optional[str] = None
    description: Optional[str] = None


class ZohoDraftInvoicePayload(BaseModel):
    customer_id: str
    reference_number: str
    date: str
    line_items: List[ZohoInvoiceLineItem]
    notes: str
    is_inclusive_tax: bool = False
    status: str = "draft"


class InvoiceValidationResult(BaseModel):
    ok: bool
    errors: List[str] = Field(default_factory=list)
    warning: Optional[str] = None


class ExportHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date_from: str
    date_to: str
    started_at: datetime
    finished_at: Optional[datetime]
    status: str
    total_orders: int
    total_customers: int
    total_invoices: int
    errors: Optional[str]


class ExportHistoryDetail(BaseModel):
    sync_run: ExportHistoryEntry
    invoices: List[Dict[str, Any]]
    orders: List[Dict[str, Any]]


class APIResponse(BaseModel):
    ok: bool = Field(..., description="Success flag")
    message: Optional[str] = Field(None, description="Optional response message")
    data: Optional[Any] = Field(None, description="Optional payload data")
