from __future__ import annotations

from datetime import datetime, date
from sqlalchemy import (
    String, Integer, Boolean, DateTime, Date, ForeignKey,
    UniqueConstraint, Text, Numeric
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    pin_hash: Mapped[str] = mapped_column(String(255))
    # Greek VAT number (ΑΦΜ)
    afm: Mapped[str] = mapped_column(String(20), default="")
    contact_person: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(160), default="")
    area_route: Mapped[str] = mapped_column(String(160), default="")
    delivery_days: Mapped[str] = mapped_column(String(60), default="")  # CSV like MON,TUE
    notes: Mapped[str] = mapped_column(Text, default="")
    label_key: Mapped[str] = mapped_column(String(160), default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    products = relationship("CustomerProduct", back_populates="customer", cascade="all, delete-orphan")



class CustomerSlugAlias(Base):
    __tablename__ = "customer_slug_aliases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    old_slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    customer: Mapped["Customer"] = relationship("Customer")


class Unit(Base):
    __tablename__ = "units"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    label_el: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    category: Mapped[str] = mapped_column(String(80), default="General")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CustomerProduct(Base):
    __tablename__ = "customer_products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price: Mapped[float] = mapped_column(Numeric(12, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    unit_override: Mapped[str | None] = mapped_column(String(10), nullable=True)

    customer = relationship("Customer", back_populates="products")

    __table_args__ = (UniqueConstraint("customer_id", "product_id", name="uq_customer_product"),)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    order_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(30), default="SUBMITTED")
    source: Mapped[str] = mapped_column(String(20), default="PORTAL")
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_by: Mapped[str] = mapped_column(String(80), default="")
    override_note: Mapped[str] = mapped_column(Text, default="")
    customer_comment: Mapped[str] = mapped_column(Text, default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    edit_used: Mapped[bool] = mapped_column(Boolean, default=False)
    edit_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    edited_by: Mapped[str] = mapped_column(String(80), default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_invoiced: Mapped[int] = mapped_column(Integer, default=0)
    invoiced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invoiced_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("customer_id", "order_date", name="uq_customer_orderdate"),)


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    # packed / weighed quantity after fulfillment (nullable until filled)
    packed_qty: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    packed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    packed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wh: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    unit_price_snapshot: Mapped[float] = mapped_column(Numeric(12, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("order_id", "product_id", name="uq_orderline"),)




class OrderEvent(Base):
    __tablename__ = "order_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(30), index=True)  # created / locked / unlocked / override
    actor: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Announcement(Base):
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Backward-compatible single-target (NULL => global)
    customer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("customers.id"), nullable=True, index=True)

    # Message content
    priority: Mapped[str] = mapped_column(String(20), default="info")  # info|warning|urgent
    message: Mapped[str] = mapped_column(Text, default="")
    dismissible: Mapped[bool] = mapped_column(Boolean, default=True)

    # V2 fields
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_priority: Mapped[int] = mapped_column(Integer, default=50)  # lower = higher

    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Legacy expiry (kept for compatibility)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", foreign_keys=[customer_id])


class AnnouncementTarget(Base):
    __tablename__ = "announcement_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), index=True)

    __table_args__ = (
        UniqueConstraint("announcement_id", "customer_id", name="uq_announcement_target"),
    )


class AnnouncementRead(Base):

    __tablename__ = "announcement_reads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("announcement_id", "customer_id", name="uq_announcement_read"),
    )


class PrintJob(Base):
    __tablename__ = "print_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id"), index=True)
    label_key: Mapped[str] = mapped_column(String(160))
    target_station: Mapped[str] = mapped_column(String(20))  # CENTRAL / WORKSHOP
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")  # QUEUED / PRINTED / FAILED / CANCELLED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    copies: Mapped[int] = mapped_column(Integer, default=1)

    customer = relationship("Customer", foreign_keys=[restaurant_id])


class AvailableLabel(Base):
    __tablename__ = "available_labels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station: Mapped[str] = mapped_column(String(20), index=True)  # CENTRAL / WORKSHOP
    name: Mapped[str] = mapped_column(String(160), index=True)  # label key without extension
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("station", "name", name="uq_available_label_station_name"),
    )
