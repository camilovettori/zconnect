from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, Date, Integer, Numeric, String, DateTime, Text, ForeignKey, ForeignKeyConstraint, Index, UniqueConstraint, JSON, func
from sqlalchemy.orm import relationship

from .db import Base


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="running")  # running/success/partial/failed
    date_from = Column(String(32), nullable=False)
    date_to = Column(String(32), nullable=False)
    total_orders = Column(Integer, default=0)
    total_customers = Column(Integer, default=0)
    total_invoices = Column(Integer, default=0)
    errors = Column(Text, nullable=True)

    exported_orders = relationship("ExportedOrder", back_populates="sync_run")
    exported_invoices = relationship("ExportedInvoice", back_populates="sync_run")


class ExportedOrder(Base):
    __tablename__ = "exported_orders"

    id = Column(Integer, primary_key=True, index=True)
    sync_run_id = Column(Integer, ForeignKey("sync_runs.id"), nullable=False)
    unify_order_id = Column(String(128), nullable=False)
    customer_name = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sync_run = relationship("SyncRun", back_populates="exported_orders")

    __table_args__ = (UniqueConstraint("unify_order_id", name="uq_unify_order_id"),)


class ExportedInvoice(Base):
    __tablename__ = "exported_invoices"

    id = Column(Integer, primary_key=True, index=True)
    sync_run_id = Column(Integer, ForeignKey("sync_runs.id"), nullable=False)
    unify_customer_name = Column(String(255), nullable=False)
    unify_order_ids = Column(JSON, nullable=False)
    zoho_invoice_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sync_run = relationship("SyncRun", back_populates="exported_invoices")


class CustomerMapping(Base):
    __tablename__ = "customer_mappings"

    id = Column(Integer, primary_key=True, index=True)
    unify_customer_name = Column(String(255), nullable=False, unique=True)
    zoho_contact_id = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ItemMapping(Base):
    __tablename__ = "item_mappings"

    id = Column(Integer, primary_key=True, index=True)
    unify_item_sku = Column(String(128), nullable=False, unique=True)
    zoho_item_id = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ZohoItemMapping(Base):
    __tablename__ = "zoho_item_mappings"

    id = Column(Integer, primary_key=True, index=True)
    unify_product_key = Column(String(128), nullable=False, unique=True, index=True)
    unify_product_name = Column(String(255), nullable=True)
    zoho_item_id = Column(String(128), nullable=False)
    zoho_item_name = Column(String(255), nullable=True)
    tax_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExportEvent(Base):
    """Append-only audit history of export operations (one row per event).

    The application only ever inserts rows; database triggers created by migration
    0002_export_events reject UPDATE and DELETE. Rows hold no credentials or invoice
    payloads: only identifiers, statuses and a short sanitised diagnostic.

    The production database is shared with other applications, so this table and its
    indexes, function and triggers carry a zconnect_ prefix. An unrelated table named
    export_events may exist there and is never used.
    """

    __tablename__ = "zconnect_export_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(String(64), nullable=False)
    unify_order_id = Column(String(128), nullable=False)
    export_record_id = Column(Integer, ForeignKey("exported_orders.id", ondelete="RESTRICT"), nullable=True)
    sync_run_id = Column(Integer, ForeignKey("sync_runs.id", ondelete="RESTRICT"), nullable=True)
    event_type = Column(String(48), nullable=False)
    previous_status = Column(String(32), nullable=True)
    new_status = Column(String(32), nullable=True)
    zoho_invoice_id = Column(String(128), nullable=True)
    actor_id = Column(String(128), nullable=True)
    detail = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now())

    __table_args__ = (
        Index("ix_zconnect_export_events_tenant_order", "tenant_id", "unify_order_id", "id"),
        Index("ix_zconnect_export_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_zconnect_export_events_sync_run", "sync_run_id"),
        Index("ix_zconnect_export_events_export_record", "export_record_id"),
    )


# ---------------------------------------------------------------------------
# Analytics (migration 0003_analytics). Company-scoped; the seven original tables are
# unchanged: a sync run's company is recorded in zconnect_sync_run_companies, and runs
# without a row (all runs before Analytics) belong to the default company.
# ---------------------------------------------------------------------------

DEFAULT_COMPANY_ID = "default"


class Company(Base):
    __tablename__ = "zconnect_companies"

    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now())


class SyncRunCompany(Base):
    __tablename__ = "zconnect_sync_run_companies"

    sync_run_id = Column(Integer, ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True)
    company_id = Column(String(64), ForeignKey("zconnect_companies.id", ondelete="RESTRICT"), nullable=False)

    __table_args__ = (
        Index("ix_zconnect_sync_run_companies_company", "company_id"),
        UniqueConstraint("sync_run_id", "company_id", name="uq_zconnect_run_company"),
    )


class OrderFact(Base):
    """Value of one invoiced order, captured from the exact line items sent to Zoho.

    It is the net (pre-tax) value of a DRAFT invoice: an invoiced amount, not paid revenue.
    One row per company and Unify order; orders invoiced before Analytics have no row."""

    __tablename__ = "zconnect_order_facts"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    company_id = Column(String(64), ForeignKey("zconnect_companies.id", ondelete="RESTRICT"), nullable=False)
    unify_order_id = Column(String(128), nullable=False)
    sync_run_id = Column(Integer, ForeignKey("sync_runs.id", ondelete="RESTRICT"), nullable=True)
    zoho_invoice_id = Column(String(128), nullable=True)
    customer_key = Column(String(255), nullable=False)
    customer_name = Column(String(255), nullable=True)
    order_date = Column(Date, nullable=True)
    delivery_date = Column(Date, nullable=True)
    invoice_date = Column(Date, nullable=False)
    currency = Column(String(3), nullable=False)
    net_amount = Column(Numeric(14, 2), nullable=False)
    line_count = Column(Integer, nullable=False)
    source = Column(String(32), nullable=False)  # invoice_created | invoice_linked
    captured_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now())

    lines = relationship("OrderLineFact", back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("company_id", "unify_order_id", name="uq_zconnect_order_facts_company_order"),
        UniqueConstraint("id", "company_id", name="uq_zconnect_fact_company"),
        ForeignKeyConstraint(["sync_run_id", "company_id"], ["zconnect_sync_run_companies.sync_run_id", "zconnect_sync_run_companies.company_id"], name="fk_zconnect_fact_run_company", ondelete="RESTRICT"),
        Index("ix_zconnect_order_facts_company_date", "company_id", "invoice_date"),
        Index("ix_zconnect_order_facts_company_customer", "company_id", "customer_key"),
    )


class OrderLineFact(Base):
    __tablename__ = "zconnect_order_lines"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    order_fact_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    company_id = Column(String(64), nullable=False)
    product_key = Column(String(255), nullable=False)
    product_name = Column(String(255), nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    unit_price = Column(Numeric(14, 4), nullable=False)
    net_amount = Column(Numeric(14, 2), nullable=False)

    order = relationship("OrderFact", back_populates="lines")

    __table_args__ = (
        ForeignKeyConstraint(["order_fact_id", "company_id"], ["zconnect_order_facts.id", "zconnect_order_facts.company_id"], name="fk_zconnect_line_fact_company", ondelete="CASCADE"),
        Index("ix_zconnect_order_lines_order", "order_fact_id"),
        Index("ix_zconnect_order_lines_company_product", "company_id", "product_key"),
    )



class HistoricalInvoice(Base):
    """Read-only snapshot of a Zoho Books invoice for business reporting.

    Unique per company and Zoho organisation. Kept separate from Zconnect order
    facts because one invoice can cover multiple Unify orders.
    """

    __tablename__ = "zconnect_historical_invoices"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    company_id = Column(String(64), ForeignKey("zconnect_companies.id", ondelete="RESTRICT"), nullable=False)
    zoho_org_id = Column(String(128), nullable=False)
    zoho_invoice_id = Column(String(128), nullable=False)
    invoice_number = Column(String(128), nullable=True)
    invoice_date = Column(Date, nullable=False)
    customer_id = Column(String(128), nullable=True)
    customer_name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False)
    currency = Column(String(3), nullable=False)
    net_amount = Column(Numeric(14, 2), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now())

    lines = relationship("HistoricalInvoiceLine", back_populates="invoice", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("company_id", "zoho_org_id", "zoho_invoice_id", name="uq_zconnect_hist_invoice"),
        UniqueConstraint("id", "company_id", name="uq_zconnect_hist_invoice_company"),
        Index("ix_zconnect_hist_invoice_date", "company_id", "invoice_date"),
        Index("ix_zconnect_hist_invoice_customer", "company_id", "customer_id"),
    )


class HistoricalInvoiceLine(Base):
    __tablename__ = "zconnect_historical_invoice_lines"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    invoice_id = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    company_id = Column(String(64), nullable=False)
    zoho_line_item_id = Column(String(128), nullable=True)
    product_key = Column(String(255), nullable=False)
    product_name = Column(String(255), nullable=False)
    quantity = Column(Numeric(14, 3), nullable=False)
    net_amount = Column(Numeric(14, 2), nullable=False)

    invoice = relationship("HistoricalInvoice", back_populates="lines")

    __table_args__ = (
        ForeignKeyConstraint(["invoice_id", "company_id"], ["zconnect_historical_invoices.id", "zconnect_historical_invoices.company_id"], ondelete="CASCADE", name="fk_zconnect_hist_line_company"),
        Index("ix_zconnect_hist_line_product", "company_id", "product_key"),
    )


ANALYTICS_TABLES = ("zconnect_companies", "zconnect_sync_run_companies", "zconnect_order_facts", "zconnect_order_lines", "zconnect_historical_invoices", "zconnect_historical_invoice_lines")
