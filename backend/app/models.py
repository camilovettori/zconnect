from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, Integer, String, DateTime, Text, ForeignKey, Index, UniqueConstraint, JSON, func
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
    """

    __tablename__ = "export_events"

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
        Index("ix_export_events_tenant_order", "tenant_id", "unify_order_id", "id"),
        Index("ix_export_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_export_events_sync_run", "sync_run_id"),
        Index("ix_export_events_export_record", "export_record_id"),
    )
