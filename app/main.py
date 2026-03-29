from __future__ import annotations

import os
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import select, text, inspect, func, delete
from apscheduler.schedulers.background import BackgroundScheduler

from .db import Base, engine, get_db
from .models import AdminUser, Customer, CustomerSlugAlias, Unit, Product, CustomerProduct, Order, OrderLine, AuditLog, Announcement, AnnouncementRead, PrintJob
from .security import hash_secret, verify_secret
from .auth import sign_session, get_admin_username, get_portal_customer
from .utils import slugify
from .telegram import send_telegram

app = FastAPI()
# Static assets (logos, etc.) – safe for Railway
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
try:
    os.makedirs(STATIC_DIR, exist_ok=True)
except Exception:
    pass
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

templates = Jinja2Templates(directory="app/templates")

# --- Print agent live label cache (in-memory) ---
AGENT_LABEL_CACHE = {
    "CENTRAL": {"labels": [], "last_seen": None},
    "WORKSHOP": {"labels": [], "last_seen": None},
}


DAYS = [
    ("MON", "Δευτέρα"),
    ("TUE", "Τρίτη"),
    ("WED", "Τετάρτη"),
    ("THU", "Πέμπτη"),
    ("FRI", "Παρασκευή"),
    ("SAT", "Σάββατο"),
    ("SUN", "Κυριακή"),
]

def parse_days_csv(s: str) -> set[str]:
    if not s:
        return set()
    return {p.strip().upper() for p in s.split(",") if p.strip()}

def days_to_csv(days: list[str]) -> str:
    uniq = []
    seen=set()
    for d in days:
        du=d.strip().upper()
        if du and du not in seen:
            uniq.append(du); seen.add(du)
    return ",".join(uniq)

TZ = os.getenv("TZ", "Europe/Athens")
tz = ZoneInfo(TZ)

# Order cutoff time (HH:MM, local time). Used for automatic locking (order_date - 1 day @ cutoff).
ORDER_CUTOFF_HHMM = os.getenv("ORDER_CUTOFF_HHMM", "23:59")

SMTP_HOST = os.getenv("SMTP_HOST", "sklavounosmeat.gr")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "info@sklavounosmeat.gr")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")


def get_cutoff_time() -> tuple[dtime, str]:
    s = (ORDER_CUTOFF_HHMM or "23:59").strip()
    try:
        hh, mm = s.split(":", 1)
        hh_i = int(hh)
        mm_i = int(mm)
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            raise ValueError
        return dtime(hh_i, mm_i, 0), f"{hh_i:02d}:{mm_i:02d}"
    except Exception:
        return dtime(23, 59, 0), "23:59"

def now_local() -> datetime:
    return datetime.now(tz)

def today_local_date() -> date:
    return now_local().date()


def tomorrow_local_date() -> date:
    return (now_local().date() + timedelta(days=1))

def audit(db: Session, actor: str, action: str, payload: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, payload=payload))
    db.commit()

def ensure_units(db: Session) -> None:
    defaults = [
        ("KG", "Kg", 0),
        ("PCS", "Τεμάχια", 1),
        ("BOX", "Κιβώτια", 2),
    ]
    for code, label, order in defaults:
        exists = db.execute(select(Unit).where(Unit.code == code)).scalar_one_or_none()
        if not exists:
            db.add(Unit(code=code, label_el=label, sort_order=order, is_active=True))
    db.commit()

def ensure_admins(db: Session) -> None:
    pairs = [("ADMIN1_USERNAME", "ADMIN1_PASSWORD"), ("ADMIN2_USERNAME", "ADMIN2_PASSWORD")]
    for u_key, p_key in pairs:
        u = os.getenv(u_key)
        p = os.getenv(p_key)
        if not u or not p:
            continue
        user = db.execute(select(AdminUser).where(AdminUser.username == u)).scalar_one_or_none()
        if not user:
            db.add(AdminUser(username=u, password_hash=hash_secret(p), is_active=True))
    db.commit()


def ensure_print_jobs_table() -> None:
    """Create print_jobs table if it doesn't exist (SQLite / Postgres)."""
    insp = inspect(engine)
    if "print_jobs" in insp.get_table_names():
        return

    if engine.dialect.name == "sqlite":
        ddl = """
CREATE TABLE print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    label_key VARCHAR(160) NOT NULL,
    target_station VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'QUEUED',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    printed_at DATETIME,
    error_message TEXT DEFAULT '',
    copies INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(restaurant_id) REFERENCES customers(id)
)
""".strip()
        idx_ddls = [
            "CREATE INDEX IF NOT EXISTS ix_print_jobs_restaurant_id ON print_jobs (restaurant_id)",
            "CREATE INDEX IF NOT EXISTS ix_print_jobs_target_station ON print_jobs (target_station)",
            "CREATE INDEX IF NOT EXISTS ix_print_jobs_status ON print_jobs (status)",
        ]
    else:
        ddl = """
CREATE TABLE print_jobs (
    id SERIAL PRIMARY KEY,
    restaurant_id INTEGER NOT NULL REFERENCES customers(id),
    label_key VARCHAR(160) NOT NULL,
    target_station VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'QUEUED',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    printed_at TIMESTAMP WITHOUT TIME ZONE,
    error_message TEXT DEFAULT '',
    copies INTEGER NOT NULL DEFAULT 1
)
""".strip()
        idx_ddls = [
            "CREATE INDEX IF NOT EXISTS ix_print_jobs_restaurant_id ON print_jobs (restaurant_id)",
            "CREATE INDEX IF NOT EXISTS ix_print_jobs_target_station ON print_jobs (target_station)",
            "CREATE INDEX IF NOT EXISTS ix_print_jobs_status ON print_jobs (status)",
        ]

    with engine.begin() as conn:
        conn.execute(text(ddl))
        for stmt in idx_ddls:
            conn.execute(text(stmt))


def ensure_print_jobs_extra_columns() -> None:
    """Add new nullable columns to print_jobs table if they don't exist.
    Works for SQLite and Postgres (Railway)."""
    insp = inspect(engine)
    if "print_jobs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("print_jobs")}

    # NOTE: SQLite does NOT support "ADD COLUMN IF NOT EXISTS", so we always check first.
    if engine.dialect.name == "sqlite":
        needed = [
            ("copies", "INTEGER NOT NULL DEFAULT 1"),
        ]
    else:
        needed = [
            ("copies", "INTEGER NOT NULL DEFAULT 1"),
        ]

    with engine.begin() as conn:
        for name, coltype in needed:
            if name in cols:
                continue
            conn.execute(text(f"ALTER TABLE print_jobs ADD COLUMN {name} {coltype}"))



def ensure_customer_extra_columns() -> None:
    """Add new nullable columns to customers table if they don't exist.
    Works for SQLite and Postgres (Railway)."""
    insp = inspect(engine)
    if "customers" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("customers")}

    # (name, sql type)
    # NOTE: SQLite does NOT support "ADD COLUMN IF NOT EXISTS", so we always check first.
    if engine.dialect.name == "sqlite":
        needed = [
            ("contact_person", "VARCHAR(120)"),
            ("phone", "VARCHAR(50)"),
            ("email", "VARCHAR(160)"),
            ("area_route", "VARCHAR(160)"),
            ("delivery_days", "VARCHAR(60)"),
            ("notes", "TEXT"),
            ("label_key", "VARCHAR(160) DEFAULT ''"),
            # booleans in SQLite are stored as INTEGER 0/1
            ("is_restaurant", "INTEGER DEFAULT 0"),
        ]
    else:
        needed = [
            ("contact_person", "VARCHAR(120)"),
            ("phone", "VARCHAR(50)"),
            ("email", "VARCHAR(160)"),
            ("area_route", "VARCHAR(160)"),
            ("delivery_days", "VARCHAR(60)"),
            ("notes", "TEXT"),
            ("label_key", "VARCHAR(160) DEFAULT ''"),
            ("is_restaurant", "BOOLEAN DEFAULT FALSE"),
        ]

    with engine.begin() as conn:
        for name, coltype in needed:
            if name in cols:
                continue
            conn.execute(text(f"ALTER TABLE customers ADD COLUMN {name} {coltype}"))


def ensure_orders_extra_columns() -> None:
    """Add new nullable columns to orders table if they don't exist.
    Works for SQLite and Postgres (Railway)."""
    insp = inspect(engine)
    if "orders" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("orders")}

    if engine.dialect.name == "sqlite":
        needed = [
            ("source", "VARCHAR(20)"),
            ("is_locked", "INTEGER DEFAULT 0"),
            ("locked_by", "VARCHAR(80)"),
            ("locked_at", "TIMESTAMP"),
            ("override_note", "TEXT"),
            ("submitted_at", "TIMESTAMP"),
        ]
    else:
        needed = [
            ("source", "VARCHAR(20)"),
            ("is_locked", "BOOLEAN DEFAULT FALSE"),
            ("locked_by", "VARCHAR(80)"),
            ("locked_at", "TIMESTAMP"),
            ("override_note", "TEXT"),
            ("submitted_at", "TIMESTAMP"),
        ]

    with engine.begin() as conn:
        for name, coltype in needed:
            if name in cols:
                continue
            conn.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {coltype}"))

def ensure_order_lines_extra_columns() -> None:
    """Add new nullable columns to order_lines table if they don't exist.
    Works for SQLite and Postgres (Railway)."""
    insp = inspect(engine)
    if "order_lines" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("order_lines")}

    # numeric types differ slightly; both Postgres and SQLite accept NUMERIC
    if engine.dialect.name == "sqlite":
        needed = [
            ("packed_qty", "NUMERIC(12,3)"),
            ("packed_by", "VARCHAR(255)"),
            ("packed_at", "TIMESTAMP"),
        ]
    else:
        needed = [
            ("packed_qty", "NUMERIC(12,3)"),
            ("packed_by", "VARCHAR(255)"),
            ("packed_at", "TIMESTAMP"),
        ]

    with engine.begin() as conn:
        for name, coltype in needed:
            if name in cols:
                continue
            conn.execute(text(f"ALTER TABLE order_lines ADD COLUMN {name} {coltype}"))

def ensure_announcements_v2_schema() -> None:
    """Schema for targeted announcements.

    This app has had multiple iterations in production with different column names
    inside `announcement_targets`. To keep production stable we ensure:

    - announcements has v2 columns (priority, is_dismissible, expires_at)
    - announcement_targets exists
    - BOTH customer_id and restaurant_customer_id exist in announcement_targets
      and customer_id is backfilled from restaurant_customer_id when needed
    """
    from sqlalchemy import text

    conn = engine.connect()
    try:
        dialect = conn.dialect.name

        # announcements columns (safe migrations)
        if dialect == "sqlite":
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(announcements)")).fetchall()]
            if "priority" not in cols:
                conn.execute(text("ALTER TABLE announcements ADD COLUMN priority TEXT DEFAULT 'INFO'"))
            if "is_dismissible" not in cols:
                conn.execute(text("ALTER TABLE announcements ADD COLUMN is_dismissible INTEGER DEFAULT 1"))
            if "expires_at" not in cols:
                conn.execute(text("ALTER TABLE announcements ADD COLUMN expires_at TEXT"))
        else:
            conn.execute(text("ALTER TABLE announcements ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'INFO'"))
            conn.execute(text("ALTER TABLE announcements ADD COLUMN IF NOT EXISTS is_dismissible BOOLEAN DEFAULT TRUE"))
            conn.execute(text("ALTER TABLE announcements ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP"))

        # announcement_targets table + compatibility columns
        if dialect == "sqlite":
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS announcement_targets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        announcement_id INTEGER NOT NULL,
                        customer_id INTEGER,
                        restaurant_customer_id INTEGER
                    )
                    """
                )
            )
            try:
                conn.execute(text("ALTER TABLE announcement_targets ADD COLUMN customer_id INTEGER"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE announcement_targets ADD COLUMN restaurant_customer_id INTEGER"))
            except Exception:
                pass
        else:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS announcement_targets (
                        id SERIAL PRIMARY KEY,
                        announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
                        restaurant_customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(text("ALTER TABLE announcement_targets ADD COLUMN IF NOT EXISTS customer_id INTEGER"))
            conn.execute(text("ALTER TABLE announcement_targets ADD COLUMN IF NOT EXISTS restaurant_customer_id INTEGER"))
            try:
                conn.execute(
                    text(
                        """
                        UPDATE announcement_targets
                        SET customer_id = restaurant_customer_id
                        WHERE customer_id IS NULL AND restaurant_customer_id IS NOT NULL
                        """
                    )
                )
            except Exception:
                pass

        conn.commit()
    finally:
        conn.close()
def lock_job():
    # TEMP PATCH: cutoff disabled
    return

    # Locks any order whose cutoff (order_date - 1 @ 23:59 Athens) has passed
    with next(get_db()) as db:  # type: ignore
        now = now_local()
        cutoff_time, _cutoff_hhmm = get_cutoff_time()

        orders = db.execute(select(Order).where(Order.locked_at.is_(None))).scalars().all()
        changed = 0
        for o in orders:
            cutoff_date = o.order_date - timedelta(days=1)
            cutoff_dt = datetime.combine(cutoff_date, cutoff_time, tzinfo=tz)
            if now >= cutoff_dt:
                o.is_locked = True
                o.locked_by = "system"
                o.locked_at = now
                o.status = "LOCKED"
                o.updated_at = datetime.utcnow()
                changed += 1
        if changed:
            db.commit()

@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    ensure_customer_extra_columns()
    ensure_print_jobs_table()
    ensure_print_jobs_extra_columns()
    ensure_orders_extra_columns()
    ensure_order_lines_extra_columns()
    ensure_announcements_v2_schema()
    with next(get_db()) as db:  # type: ignore
        ensure_units(db)
        ensure_admins(db)

    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(lock_job, "interval", minutes=1, id="auto_lock")
    scheduler.start()

def require_admin(request: Request) -> str:
    u = get_admin_username(request)
    if not u:
        raise HTTPException(status_code=401)
    return u



def _load_announcements_for_request(request: Request, db: Session) -> list[dict]:
    """Return announcements for this request as simple dicts for templates.
    - Admin routes: see all currently-active announcements.
    - Portal PIN page (/p/{slug}): never show announcements.
    - Portal order page (/p/{slug}/order): show only global + correctly targeted announcements,
      excluding dismissed ones.
    """
    path = (request.url.path or "").strip()
    now_local_naive = now_local().replace(tzinfo=None)

    def is_live(a: Announcement) -> bool:
        if not bool(a.is_active):
            return False
        if getattr(a, "start_at", None) and a.start_at > now_local_naive:
            return False
        if getattr(a, "end_at", None) and a.end_at < now_local_naive:
            return False
        if getattr(a, "expires_at", None) and a.expires_at < now_local_naive:
            return False
        return True

    # Portal pages are handled first so admin cookies can never leak into portal rendering.
    if path.startswith("/p/"):
        parts = [p for p in path.split("/") if p]
        is_order_page = len(parts) >= 3 and parts[0] == "p" and parts[2] == "order"
        if not is_order_page:
            return []

        path_slug = parts[1] if len(parts) >= 2 else None
        portal_slug = get_portal_customer(request)
        if not path_slug or not portal_slug or portal_slug != path_slug:
            return []

        customer = db.execute(
            select(Customer).where(Customer.slug == path_slug, Customer.is_active == True)
        ).scalar_one_or_none()
        if not customer:
            return []

        anns = [
            a for a in db.execute(select(Announcement).order_by(Announcement.id.desc())).scalars().all()
            if is_live(a)
        ]
        if not anns:
            return []

        # Load multi-target mappings, compatible with both customer_id and restaurant_customer_id.
        target_map: dict[int, set[int]] = {}
        try:
            from sqlalchemy import bindparam
            ann_ids = [a.id for a in anns]
            q_targets = text(
                """
                SELECT announcement_id, COALESCE(customer_id, restaurant_customer_id) AS cid
                FROM announcement_targets
                WHERE announcement_id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True))
            for row in db.execute(q_targets, {"ids": ann_ids}).fetchall():
                if row.cid is None:
                    continue
                target_map.setdefault(int(row.announcement_id), set()).add(int(row.cid))
        except Exception:
            target_map = {}

        dismissed_ids = set(
            r[0] for r in db.execute(
                select(AnnouncementRead.announcement_id).where(AnnouncementRead.customer_id == customer.id)
            ).all()
        )

        out = []
        for a in anns:
            if a.id in dismissed_ids:
                continue

            target_ids = target_map.get(a.id, set())
            if target_ids:
                visible = customer.id in target_ids
            elif a.customer_id is not None:
                visible = (a.customer_id == customer.id)
            else:
                visible = True

            if not visible:
                continue

            out.append({
                "id": a.id,
                "priority": (a.priority or "info"),
                "message": (a.message or ""),
                "dismissible": bool(a.dismissible),
                "dismiss_url": f"/announcements/{a.id}/dismiss",
                "customer_name": None,
            })
        return out

    admin_user = get_admin_username(request)
    if not admin_user:
        return []

    anns = [
        a for a in db.execute(select(Announcement).order_by(Announcement.id.desc())).scalars().all()
        if is_live(a)
    ]
    out = []
    for a in anns:
        cname = None
        if a.customer_id:
            c = db.execute(select(Customer).where(Customer.id == a.customer_id)).scalar_one_or_none()
            cname = c.name if c else None
        out.append({
            "id": a.id,
            "priority": (a.priority or "info"),
            "message": (a.message or ""),
            "dismissible": bool(a.dismissible),
            "dismiss_url": None,
            "customer_name": cname,
        })
    return out


@app.middleware("http")
async def announcements_middleware(request: Request, call_next):
    # Avoid extra work for static assets
    if request.url.path.startswith("/static"):
        return await call_next(request)

    with next(get_db()) as db:  # type: ignore
        try:
            request.state.announcements = _load_announcements_for_request(request, db)
        except Exception:
            request.state.announcements = []

    response = await call_next(request)
    return response


@app.post("/announcements/{ann_id}/dismiss")
def announcement_dismiss(ann_id: int, request: Request, db: Session = Depends(get_db)):
    # Only portal customers can dismiss
    slug = get_portal_customer(request)
    if not slug:
        return RedirectResponse(url="/", status_code=302)
    c = db.execute(select(Customer).where(Customer.slug == slug, Customer.is_active == True)).scalar_one_or_none()
    if not c:
        return RedirectResponse(url="/", status_code=302)

    # Insert ignore duplicates
    exists = db.execute(
        select(AnnouncementRead).where(AnnouncementRead.announcement_id == ann_id, AnnouncementRead.customer_id == c.id)
    ).scalar_one_or_none()
    if not exists:
        db.add(AnnouncementRead(announcement_id=ann_id, customer_id=c.id))
        db.commit()
    # return back
    ref = request.headers.get("referer") or f"/p/{slug}"
    return RedirectResponse(url=ref, status_code=303)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    u = get_admin_username(request)
    return RedirectResponse(url="/admin/dashboard" if u else "/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login_post(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(""),
    password: str = Form(...),
):
    user = db.execute(select(AdminUser).where(AdminUser.username == username, AdminUser.is_active == True)).scalar_one_or_none()
    if not user or not verify_secret(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Λάθος στοιχεία."}, status_code=401)

    resp = RedirectResponse(url="/admin/dashboard", status_code=302)
    resp.set_cookie("admin_session", sign_session({"u": username}), httponly=True, samesite="lax")
    audit(db, actor=f"admin:{username}", action="login")
    return resp

@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    u = get_admin_username(request) or "unknown"
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("admin_session")
    audit(db, actor=f"admin:{u}", action="logout")
    return resp

@app.get("/admin/restaurants", response_class=HTMLResponse)
def admin_restaurants(request: Request, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    customers = db.execute(select(Customer).order_by(Customer.name.asc())).scalars().all()
    return templates.TemplateResponse(
        "admin_restaurants.html",
        {"request": request, "admin_user": admin_user, "restaurants": customers, "DAYS": DAYS},
    )

@app.get("/admin/restaurants/{c_id}", response_class=HTMLResponse)
def admin_restaurant_edit(request: Request, c_id: int, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    c = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404)
    selected = parse_days_csv(getattr(c, "delivery_days", "") or "")
    restaurant = c

    agent_station = _pick_best_station()
    available_labels = [{"name": x} for x in AGENT_LABEL_CACHE.get(agent_station, {}).get("labels", [])]
    agent_online = _station_is_online(agent_station)

    return templates.TemplateResponse(
        "admin_restaurant_form.html",
        {
            "request": request,
            "admin_user": admin_user,
            "restaurant": restaurant,
            "customer": restaurant,
            "c": c,
            "selected_days": selected,
            "selected_days_csv": ",".join(selected),
            "DAYS": DAYS,
            "available_labels": available_labels,
            "label_agent_station": agent_station,
            "label_agent_online": agent_online,
        },
    )


@app.get("/admin/restaurants/{c_id}/card", response_class=HTMLResponse)
def admin_restaurant_card(
    request: Request,
    c_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    restaurant = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not restaurant:
        raise HTTPException(status_code=404)

    # Date range (default: current month)
    today = now_local().date()
    default_from = today.replace(day=1)
    default_to = today

    qs_from = (request.query_params.get("from") or "").strip()
    qs_to = (request.query_params.get("to") or "").strip()

    def parse_date_safe(s: str) -> date | None:
        try:
            return date.fromisoformat(s)
        except Exception:
            return None

    d_from = parse_date_safe(qs_from) or default_from
    d_to = parse_date_safe(qs_to) or default_to
    if d_to < d_from:
        d_from, d_to = d_to, d_from

    # Orders in range
    orders_rows = (
        db.execute(
            select(Order)
            .where(Order.customer_id == c_id)
            .where(Order.order_date >= d_from)
            .where(Order.order_date <= d_to)
            .order_by(Order.order_date.desc())
        )
        .scalars()
        .all()
    )
    order_ids = [o.id for o in orders_rows]

    lines_by_order: dict[int, list[OrderLine]] = {}
    if order_ids:
        all_lines = db.execute(select(OrderLine).where(OrderLine.order_id.in_(order_ids))).scalars().all()
        for ln in all_lines:
            lines_by_order.setdefault(ln.order_id, []).append(ln)

    def money2(x: float) -> str:
        try:
            return f"{float(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return "0,00"

    totals_net = 0.0
    orders = []
    for o in orders_rows:
        net = 0.0
        completed = True
        any_qty = False
        for ln in lines_by_order.get(o.id, []):
            q = float(ln.qty or 0)
            if q > 0:
                any_qty = True
                billed_q = float(ln.packed_qty) if (ln.packed_qty is not None) else q
                net += billed_q * float(ln.unit_price_snapshot or 0)
                pq = ln.packed_qty
                if pq is None or float(pq) != q:
                    completed = False
        if not any_qty:
            completed = False
        vat = net * 0.13
        gross = net + vat
        totals_net += net
        orders.append(
            {
                "id": o.id,
                "date_str": o.order_date.isoformat(),
                "date_disp": o.order_date.strftime("%d/%m/%Y"),
                "net": net,
                "vat": vat,
                "gross": gross,
                "net_fmt": money2(net),
                "vat_fmt": money2(vat),
                "gross_fmt": money2(gross),
                "locked": bool(getattr(o, "is_locked", False)),
                "completed": completed,
            }
        )

    totals_vat = totals_net * 0.13
    totals_gross = totals_net + totals_vat

    # Restaurant prices (per-customer)
    price_rows = (
        db.execute(
            select(CustomerProduct, Product, Unit)
            .join(Product, CustomerProduct.product_id == Product.id)
            .join(Unit, Product.unit_id == Unit.id)
            .where(CustomerProduct.customer_id == c_id)
            .where(CustomerProduct.is_active == True)
            .order_by(func.coalesce(Product.category, "").asc(), Product.name.asc())
        )
        .all()
    )
    prices = []
    for cp, p, u in price_rows:
        unit = (cp.unit_override or u.code or "").strip()
        prices.append(
            {
                "name": p.name,
                "unit": unit,
                "price": float(cp.price or 0),
                "price_fmt": money2(float(cp.price or 0)),
            }
        )

    selected = parse_days_csv(getattr(restaurant, "delivery_days", "") or "")
    day_labels = [lbl for code, lbl in DAYS if code in selected]
    delivery_days_label = ", ".join(day_labels)

    return templates.TemplateResponse(
        "admin_restaurant_card.html",
        {
            "request": request,
            "admin_user": admin_user,
            "restaurant": restaurant,
            "from_str": d_from.isoformat(),
            "to_str": d_to.isoformat(),
            "totals": {"net_fmt": money2(totals_net), "vat_fmt": money2(totals_vat), "gross_fmt": money2(totals_gross)},
            "orders": orders,
            "prices": prices,
            "delivery_days_label": delivery_days_label,
        },
    )


@app.post("/admin/restaurants/{c_id}")
def admin_restaurant_update(
    request: Request,
    c_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
    afm: str = Form(...),
    contact_person: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    area_route: str = Form(""),
    label_key: str = Form(""),
    notes: str = Form(""),
    delivery_days: list[str] = Form(default=[]),
):
    c = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404)
    c.afm = (afm or "").strip()
    c.contact_person = contact_person.strip()
    c.phone = phone.strip()
    c.email = email.strip()
    c.area_route = area_route.strip()
    c.label_key = (label_key or "").strip()
    c.notes = notes.strip()
    c.delivery_days = days_to_csv(delivery_days)
    db.commit()
    audit(db, admin_user, "restaurant.update", payload=f"customer_id={c_id}")
    return RedirectResponse(url=f"/admin/restaurants/{c_id}", status_code=303)

@app.post("/admin/restaurants/{c_id}/print-label")
def admin_restaurant_print_label(
    request: Request,
    c_id: int,
    station: str,
    copies: int = Form(1),
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    station = (station or "").strip().upper()
    if station not in ("CENTRAL", "WORKSHOP"):
        raise HTTPException(status_code=400, detail="Invalid station")

    try:
        copies = int(copies or 1)
    except Exception:
        copies = 1
    copies = max(1, min(copies, 50))

    c = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404)

    label_key = (getattr(c, "label_key", "") or "").strip()
    if not label_key:
        return RedirectResponse(url=request.headers.get("referer", f"/admin/restaurants/{c_id}"), status_code=303)

    job = PrintJob(
        restaurant_id=c.id,
        label_key=label_key,
        target_station=station,
        status="QUEUED",
        error_message="",
        copies=copies,
    )
    db.add(job)
    db.commit()
    audit(db, admin_user, "label.print.enqueue", payload=f"customer_id={c_id};station={station};label_key={label_key}")

    return RedirectResponse(url=request.headers.get("referer", f"/admin/restaurants/{c_id}"), status_code=303)


@app.post("/admin/restaurants/{c_id}/toggle")
def admin_restaurant_toggle(request: Request, c_id: int, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    c = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404)
    c.is_active = not bool(c.is_active)
    db.commit()
    audit(db, admin_user, "restaurant.toggle", payload=f"customer_id={c_id};is_active={c.is_active}")
    return RedirectResponse(url="/admin/restaurants", status_code=303)


@app.get("/admin/restaurants/{c_id}/products", response_class=HTMLResponse)
def admin_restaurant_products(request: Request, c_id: int, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    restaurant = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not restaurant:
        raise HTTPException(status_code=404)

    # Existing per-restaurant product settings
    cp_rows = db.execute(select(CustomerProduct).where(CustomerProduct.customer_id == c_id)).scalars().all()
    cp_map = {cp.product_id: cp for cp in cp_rows}

    # ALL active products from the catalogue + their default unit
    prod_rows = db.execute(
        select(Product, Unit)
        .join(Unit, Product.unit_id == Unit.id)
        .where(Product.is_active == True)
        .order_by(Product.category.asc(), Product.name.asc())
    ).all()

    items = []
    for p, u in prod_rows:
        cp = cp_map.get(p.id)
        enabled = bool(cp and cp.is_active)
        price_val = 0.0
        unit_override = None
        if cp:
            try:
                price_val = float(cp.price or 0)
            except Exception:
                price_val = 0.0
            unit_override = (cp.unit_override or "").strip() or None

        items.append({
            "product": p,
            "unit": u,
            "enabled": enabled,
            "price": price_val,
            "unit_override": unit_override,
        })

    return templates.TemplateResponse(
        "admin_restaurant_products.html",
        {"request": request, "admin_user": admin_user, "restaurant": restaurant, "items": items},
    )

@app.post("/admin/restaurants/{c_id}/products")
async def admin_restaurant_products_save(
    request: Request,
    c_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    restaurant = db.execute(select(Customer).where(Customer.id == c_id)).scalar_one_or_none()
    if not restaurant:
        raise HTTPException(status_code=404)

    form = await request.form()
    enabled_raw = form.getlist("enabled")
    enabled_ids: set[int] = set()
    for v in enabled_raw:
        try:
            enabled_ids.add(int(v))
        except Exception:
            continue

    # Fetch all active products (same ordering as GET)
    products = db.execute(
        select(Product).where(Product.is_active == True).order_by(Product.category.asc(), Product.name.asc())
    ).scalars().all()

    cp_rows = db.execute(select(CustomerProduct).where(CustomerProduct.customer_id == c_id)).scalars().all()
    cp_map = {cp.product_id: cp for cp in cp_rows}

    def parse_price(s: str | None) -> float:
        if not s:
            return 0.0
        ss = str(s).strip().replace(",", ".")
        try:
            return float(ss)
        except Exception:
            return 0.0

    for p in products:
        cp = cp_map.get(p.id)

        is_enabled = p.id in enabled_ids
        if is_enabled:
            price = parse_price(form.get(f"price_{p.id}"))
            unit_override = (form.get(f"unit_{p.id}") or "").strip()
            if unit_override == "":
                unit_override = None

            if cp is None:
                cp = CustomerProduct(customer_id=c_id, product_id=p.id, price=price, is_active=True, unit_override=unit_override)
                db.add(cp)
            else:
                cp.is_active = True
                cp.price = price
                cp.unit_override = unit_override
        else:
            if cp is not None:
                cp.is_active = False
                # Keep stored price/unit_override for convenience on re-enable

    db.commit()
    audit(db, admin_user, "restaurant.products.update", payload=f"customer_id={c_id}")
    return RedirectResponse(url=f"/admin/restaurants/{c_id}/products", status_code=303)

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db), date_str: str | None = None, show_all: int = 0):
    admin_u = require_admin(request)
    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    only_ordered = 0 if show_all else 1

    customers = db.execute(select(Customer).where(Customer.is_active == True).order_by(Customer.name)).scalars().all()
    has_packed_col = hasattr(OrderLine, "packed_qty")

    summaries = []
    for c in customers:
        o = db.execute(select(Order).where(Order.customer_id == c.id, Order.order_date == d)).scalar_one_or_none()

        has_order = False
        status = "EMPTY"
        locked = False
        comment = ""
        completed = False
        has_packed = False
        source = ""
        locked_by = ""
        override_note = ""
        order_id = None

        if o:
            status = o.status
            locked = (o.locked_at is not None) or bool(getattr(o, "is_locked", False))
            comment = o.customer_comment or ""
            source = (o.source or "")
            locked_by = (o.locked_by or "")
            override_note = (o.override_note or "")
            order_id = o.id

            if has_packed_col:
                lines = db.execute(select(OrderLine.qty, OrderLine.packed_qty).where(OrderLine.order_id == o.id)).all()
            else:
                lines = db.execute(select(OrderLine.qty).where(OrderLine.order_id == o.id)).all()

            # has_order: any qty > 0 OR comment
            qtys = []
            packed_pairs = []
            if has_packed_col:
                for q, p in lines:
                    qv = float(q) if q is not None else 0.0
                    qtys.append(qv)
                    packed_pairs.append((qv, None if p is None else float(p)))
            else:
                for (q,) in lines:
                    qv = float(q) if q is not None else 0.0
                    qtys.append(qv)

            has_order = any(q > 0 for q in qtys) or bool(comment.strip())

            # completed: explicit status OR (packed entered for all ordered lines)
            if str(status).upper() == "COMPLETED":
                completed = True
            elif has_packed_col and any(q > 0 for q in qtys):
                # Billing is always by packed kilograms.
                # "Completed" means every ordered line has a packed value entered (>0).
                ok = True
                any_packed = False
                for qv, pv in packed_pairs:
                    if qv <= 0:
                        continue
                    if pv is None or pv <= 0:
                        ok = False
                        break
                    any_packed = True
                has_packed = any_packed
                completed = ok and any_packed
            elif has_packed_col:
                has_packed = any((pv is not None and pv > 0) for _, pv in packed_pairs)

        if only_ordered and not has_order:
            continue

        summaries.append(
            {
                "customer": c,
                "has_order": has_order,
                "status": status,
                "locked": locked,
                "comment": comment,
                "completed": completed,
                "has_packed": has_packed,
                "source": source,
                "locked_by": locked_by,
                "override_note": override_note,
                "order_id": order_id,
            }
        )

    # Aggregate product totals for the selected day (quick prep view).
    # Uses ordered quantities (qty) across all active customers' orders.
    total_rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.category,
            Unit.code,
            func.sum(OrderLine.qty).label("total_qty"),
        )
        .select_from(OrderLine)
        .join(Order, Order.id == OrderLine.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderLine.product_id)
        .join(Unit, Unit.id == Product.unit_id)
        .where(
            Order.order_date == d,
            Customer.is_active == True,
            OrderLine.qty.is_not(None),
            OrderLine.qty > 0,
        )
        .group_by(Product.id, Product.name, Product.category, Unit.code)
        .order_by(func.coalesce(Product.category, "").asc(), Product.name.asc())
    ).all()

    def _fmt_qty(q: float, unit_code: str) -> str:
        qf = float(q or 0)
        uc = (unit_code or "").upper()
        if uc == "KG":
            return f"{qf:.2f}"
        # PCS / BOX etc: prefer integer display when possible
        if abs(qf - round(qf)) < 1e-9:
            return str(int(round(qf)))
        return f"{qf:.3f}".rstrip("0").rstrip(".")

    product_totals = []
    total_kg = 0.0
    for pid, pname, pcat, ucode, tqty in total_rows:
        t = float(tqty or 0)
        if (ucode or "").upper() == "KG":
            total_kg += t
        product_totals.append(
            {
                "product_id": pid,
                "name": pname,
                "category": pcat or "",
                "unit": (ucode or ""),
                "total_qty": t,
                "display_qty": _fmt_qty(t, ucode or ""),
            }
        )

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "admin_user": admin_u,
            "date": d,
            "summaries": summaries,
            "only_ordered": 1 if only_ordered else 0,
            "customers": customers,
            "product_totals": product_totals,
            "product_totals_total_kg": round(total_kg, 3),
        },
    )


@app.get("/admin/dashboard/live-status")
def admin_dashboard_live_status(
    request: Request,
    db: Session = Depends(get_db),
    date_str: str | None = None,
    show_all: int = 0,
):
    require_admin(request)

    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    only_ordered = 0 if show_all else 1
    customers = db.execute(
        select(Customer).where(Customer.is_active == True).order_by(Customer.name)
    ).scalars().all()
    has_packed_col = hasattr(OrderLine, "packed_qty")

    rows = []
    for c in customers:
        o = db.execute(
            select(Order).where(Order.customer_id == c.id, Order.order_date == d)
        ).scalar_one_or_none()

        has_order = False
        status = "EMPTY"
        locked = False
        comment = ""
        completed = False
        has_packed = False

        if o:
            status = o.status
            locked = (o.locked_at is not None) or bool(getattr(o, "is_locked", False))
            comment = o.customer_comment or ""

            if has_packed_col:
                lines = db.execute(
                    select(OrderLine.qty, OrderLine.packed_qty).where(OrderLine.order_id == o.id)
                ).all()
            else:
                lines = db.execute(
                    select(OrderLine.qty).where(OrderLine.order_id == o.id)
                ).all()

            qtys = []
            packed_pairs = []
            if has_packed_col:
                for q, p in lines:
                    qv = float(q) if q is not None else 0.0
                    qtys.append(qv)
                    packed_pairs.append((qv, None if p is None else float(p)))
            else:
                for (q,) in lines:
                    qv = float(q) if q is not None else 0.0
                    qtys.append(qv)

            has_order = any(q > 0 for q in qtys) or bool(comment.strip())

            if str(status).upper() == "COMPLETED":
                completed = True
            elif has_packed_col and any(q > 0 for q in qtys):
                ok = True
                any_packed = False
                for qv, pv in packed_pairs:
                    if qv <= 0:
                        continue
                    if pv is None or pv <= 0:
                        ok = False
                        break
                    any_packed = True
                has_packed = any_packed
                completed = ok and any_packed
            elif has_packed_col:
                has_packed = any((pv is not None and pv > 0) for _, pv in packed_pairs)

        if only_ordered and not has_order:
            continue

        rows.append(
            {
                "customer_id": c.id,
                "has_order": has_order,
                "status": status,
                "locked": locked,
                "comment": comment or "",
                "completed": completed,
                "has_packed": has_packed,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "date": d.isoformat(),
            "row_ids": [r["customer_id"] for r in rows],
            "rows": rows,
        }
    )



@app.get("/admin/dashboard/open-summary-status")
def admin_dashboard_open_summary_status(
    request: Request,
    ids: str = "",
    date_str: str | None = None,
    db: Session = Depends(get_db),
):
    require_admin(request)

    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    customer_ids: list[int] = []
    for part in (ids or "").split(","):
        s = (part or "").strip()
        if not s:
            continue
        try:
            customer_ids.append(int(s))
        except Exception:
            continue

    if not customer_ids:
        return JSONResponse({"ok": True, "date": d.isoformat(), "rows": []})

    orders = db.execute(
        select(Order.id, Order.customer_id, Order.updated_at)
        .where(Order.order_date == d, Order.customer_id.in_(customer_ids))
    ).all()

    order_map = {
        int(customer_id): {
            "order_id": int(order_id),
            "updated_at": (updated_at.isoformat() if updated_at else ""),
        }
        for order_id, customer_id, updated_at in orders
    }

    rows = []
    for cid in customer_ids:
        info = order_map.get(cid)
        if info:
            rows.append({
                "customer_id": cid,
                "has_order": True,
                "order_id": info["order_id"],
                "updated_at": info["updated_at"],
            })
        else:
            rows.append({
                "customer_id": cid,
                "has_order": False,
                "order_id": None,
                "updated_at": "",
            })

    return JSONResponse({"ok": True, "date": d.isoformat(), "rows": rows})


@app.get("/admin/orders")
def admin_orders_redirect(request: Request):
    # canonical admin orders view is the dashboard
    return RedirectResponse(url="/admin/dashboard", status_code=302)

@app.get("/admin/order")
def admin_order_legacy_redirect(request: Request):
    # Backwards compatibility for older links
    return RedirectResponse(url="/admin/dashboard", status_code=302)

@app.get("/admin/phone-order")
def admin_phone_order(request: Request, afm: str = "", date_str: str | None = None, db: Session = Depends(get_db)):
    """
    Admin-only entry point for PHONE orders by VAT (ΑΦΜ) lookup.
    Redirects to the canonical full order screen for the customer/date.
    """
    admin_u = require_admin(request)
    vat = (afm or "").strip()
    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    if not vat:
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

    c = db.execute(select(Customer).where(Customer.afm == vat, Customer.is_active == True)).scalar_one_or_none()
    if not c:
        # keep it simple; dashboard can show a small hint if needed
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

    return RedirectResponse(url=f"/admin/orders/{c.id}/full?date_str={d.isoformat()}", status_code=302)



@app.post("/admin/orders/lock-tomorrow")
def admin_lock_tomorrow(request: Request, db: Session = Depends(get_db)):
    admin = get_admin_username(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)
    d = tomorrow_local_date()
    orders = db.execute(select(Order).where(Order.order_date == d)).scalars().all()
    now = now_local()
    changed = 0
    for o in orders:
        if o.is_locked or o.locked_at is not None:
            continue
        o.is_locked = True
        o.locked_by = admin
        o.locked_at = now
        o.status = "LOCKED"
        o.updated_at = datetime.utcnow()
        changed += 1
    if changed:
        db.commit()
        audit(db, actor=f"admin:{admin}", action="lock_all_tomorrow", payload=f"date={d.isoformat()} changed={changed}")
    return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

def _get_order_for_customer_date(db: Session, customer_id: int, d: date) -> Order | None:
    return db.execute(select(Order).where(Order.customer_id == customer_id, Order.order_date == d)).scalar_one_or_none()

@app.post("/admin/orders/{customer_id}/lock")
def admin_lock_order(customer_id: int, request: Request, date_str: str = Form(...), db: Session = Depends(get_db)):
    admin = get_admin_username(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        d = today_local_date()
    o = _get_order_for_customer_date(db, customer_id, d)
    if not o:
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)
    if not (o.is_locked or o.locked_at is not None):
        o.is_locked = True
        o.locked_by = admin
        o.locked_at = now_local()
        o.status = "LOCKED"
        o.updated_at = datetime.utcnow()
        db.commit()
        audit(db, actor=f"admin:{admin}", action="lock", payload=f"order_id={o.id}")
    return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

@app.post("/admin/orders/{customer_id}/unlock")
def admin_unlock_order(customer_id: int, request: Request, date_str: str = Form(...), db: Session = Depends(get_db)):
    admin = get_admin_username(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        d = tomorrow_local_date()
    o = _get_order_for_customer_date(db, customer_id, d)
    if not o:
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

    # Allow unlock for all orders (PHONE/PORTAL). The override flow still exists when you want an audit note.

    if o.is_locked or o.locked_at is not None:
        o.is_locked = False
        o.locked_by = ""
        o.locked_at = None
        o.status = "SUBMITTED"
        o.updated_at = datetime.utcnow()
        db.commit()
        audit(db, actor=f"admin:{admin}", action="unlock", payload=f"order_id={o.id}")
    return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

@app.post("/admin/orders/{customer_id}/override-unlock")
def admin_override_unlock(customer_id: int, request: Request, date_str: str = Form(...), override_note: str = Form(""), db: Session = Depends(get_db)):
    admin = get_admin_username(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)
    note = (override_note or "").strip()
    if not note:
        # no note => no override
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            d = tomorrow_local_date()
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        d = tomorrow_local_date()
    o = _get_order_for_customer_date(db, customer_id, d)
    if not o:
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)
    o.is_locked = False
    o.locked_by = ""
    o.locked_at = None
    o.override_note = note
    o.status = "OVERRIDDEN"
    o.updated_at = datetime.utcnow()
    db.commit()
    audit(db, actor=f"admin:{admin}", action="override_unlock", payload=f"order_id={o.id} note={note}")
    return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=302)

@app.get("/admin/orders/{customer_id}/summary", response_class=HTMLResponse)
def admin_order_summary(customer_id: int, request: Request, db: Session = Depends(get_db), date_str: str | None = None):
    admin_u = require_admin(request)
    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    o = db.execute(select(Order).where(Order.customer_id == customer_id, Order.order_date == d)).scalar_one_or_none()
    if not o:
        return templates.TemplateResponse(
            "admin_order_summary.html",
            {"request": request, "admin_user": admin_u, "date": d, "customer_id": customer_id, "order": None, "items": []},
        )

    def _fmt_qty(q: float, unit_code: str) -> str:
        u = (unit_code or "").strip().upper()
        if u == "PCS":
            return str(int(round(q)))
        s = f"{q:.3f}".rstrip("0").rstrip(".")
        return s if s else "0"

    def _fmt_packed_kg(pq) -> str:
        """Packed is always treated/displayed as KG with exactly 2 decimals."""
        if pq is None:
            return ""
        try:
            return f"{float(pq):.2f}"
        except Exception:
            return ""

    def _fmt_money(x: float) -> str:
        return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    cols = [
        OrderLine.id,
        OrderLine.product_id,
        Product.name,
        Product.sku,
        func.coalesce(CustomerProduct.unit_override, Unit.code).label("unit_code"),
        OrderLine.qty,
        OrderLine.unit_price_snapshot,
    ]
    has_packed = hasattr(OrderLine, "packed_qty")
    if has_packed:
        cols.append(OrderLine.packed_qty)

    q = (
        select(*cols)
        .join(Product, Product.id == OrderLine.product_id)
        .join(Unit, Unit.id == Product.unit_id)
        .outerjoin(
            CustomerProduct,
            (CustomerProduct.product_id == Product.id)
            & (CustomerProduct.customer_id == o.customer_id)
        )
        .where(OrderLine.order_id == o.id)
        .order_by(Product.name.asc())
    )
    rows = db.execute(q).all()

    items: list[dict] = []
    subtotal = 0.0

    for r in rows:
        if has_packed:
            line_id, product_id, name, sku, unit_code, qty, unit_price, packed = r
        else:
            line_id, product_id, name, sku, unit_code, qty, unit_price = r
            packed = None

        qty_f = float(qty) if qty is not None else 0.0
        if qty_f <= 0:
            continue  # show only ordered items

        unit_price_f = float(unit_price) if unit_price is not None else 0.0
        packed_f = None if packed is None else float(packed)
        billed_qty = packed_f if (packed_f is not None) else qty_f
        line_total = billed_qty * unit_price_f
        subtotal += line_total

        items.append(
            {
                "line_id": line_id,
                "product_id": product_id,
                "name": name,
                "sku": sku,
                "unit": unit_code,
                "qty": qty_f,
                "qty_disp": _fmt_qty(qty_f, unit_code),
                "packed_qty": packed_f,
                "packed_disp": _fmt_packed_kg(packed_f),
                "unit_price_disp": _fmt_money(unit_price_f),
                "line_total_disp": _fmt_money(line_total),
            }
        )

    total_count = len(items)
    packed_count = 0
    for it in items:
        pq = it.get("packed_qty")
        if pq is None:
            continue
        packed_count += 1

    vat = subtotal * 0.13
    grand_total = subtotal + vat

    return templates.TemplateResponse(
        "admin_order_summary.html",
        {
            "request": request,
            "admin_user": admin_u,
            "date": d,
            "customer_id": customer_id,
            "order": o,
            "items": items,
            "packed_count": packed_count,
            "total_count": total_count,
            "subtotal_disp": _fmt_money(subtotal),
            "vat_disp": _fmt_money(vat),
            "grand_total_disp": _fmt_money(grand_total),
        },
    )


# -------------------------
# Admin Full Order (edit ordered + packed)
# -------------------------

def _to_float(s: str | None) -> float:
    try:
        if s is None:
            return 0.0
        s = str(s).strip().replace(",", ".")
        if s == "":
            return 0.0
        return float(s)
    except Exception:
        return 0.0

def _to_float_or_none(s: str | None) -> float | None:
    try:
        if s is None:
            return None
        s = str(s).strip().replace(",", ".")
        if s == "":
            return None
        return float(s)
    except Exception:
        return None

@app.get("/admin/orders/{customer_id}/full", response_class=HTMLResponse)
def admin_order_full_get(customer_id: int, request: Request, db: Session = Depends(get_db), date_str: str | None = None):
    admin_u = require_admin(request)
    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    customer = db.execute(select(Customer).where(Customer.id == customer_id)).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404)

    # Ensure order exists so the admin can work on it.
    o = _get_order_for_customer_date(db, customer_id, d)
    if not o:
        o = Order(
            customer_id=customer_id,
            order_date=d,
            status="SUBMITTED",
            source="PHONE",
            customer_comment="",
            submitted_at=now_local(),
            updated_at=datetime.utcnow(),
        )
        db.add(o)
        db.commit()
        audit(db, actor=f"admin:{admin_u}", action="order.create_admin", payload=f"order_id={o.id}")

    locked = bool(o.is_locked or o.locked_at is not None)
    _ct, cutoff_hhmm = get_cutoff_time()

    # Get restaurant catalogue (only active customer products)
    q = (
        select(CustomerProduct, Product, Unit)
        .join(Product, Product.id == CustomerProduct.product_id)
        .join(Unit, Unit.id == Product.unit_id)
        .where(CustomerProduct.customer_id == customer_id, CustomerProduct.is_active == True, Product.is_active == True)
        .order_by(Product.category.asc(), Product.name.asc())
    )
    rows = db.execute(q).all()

    # Map existing lines
    lines = db.execute(select(OrderLine).where(OrderLine.order_id == o.id)).scalars().all()
    qty_map = {ln.product_id: (float(ln.qty) if ln.qty is not None else 0.0) for ln in lines}
    packed_map = {ln.product_id: (float(ln.packed_qty) if ln.packed_qty is not None else None) for ln in lines}

    items = []
    for cp, p, u in rows:
        unit_label = u.label_el
        unit_code = u.code
        if cp.unit_override:
            uo = db.execute(select(Unit).where(Unit.code == cp.unit_override)).scalar_one_or_none()
            if uo:
                unit_label = uo.label_el
                unit_code = uo.code
        items.append({
            "cp": cp,
            "p": p,
            "unit_label": unit_label,
            "unit_code": unit_code,
        })

    return templates.TemplateResponse(
        "admin_order_full.html",
        {
            "request": request,
            "admin_user": admin_u,
            "customer": customer,
            "customer_id": customer_id,
            "date": d,
            "order": o,
            "locked": locked,
            "cutoff_hhmm": cutoff_hhmm,
            "items": items,
            "qty_map": qty_map,
            "packed_map": packed_map,
            "comment": o.customer_comment or "",
        },
    )



@app.post("/admin/orders/{order_id}/reset")
def admin_order_reset(order_id: int, request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)

    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    o.status = "DRAFT"
    o.is_locked = False
    o.locked_by = None
    o.override_note = None
    o.locked_at = None
    o.submitted_at = None
    o.customer_comment = ""
    o.updated_at = datetime.utcnow()

    lines = db.execute(select(OrderLine).where(OrderLine.order_id == o.id)).scalars().all()
    for ln in lines:
        ln.qty = 0
        if hasattr(ln, "packed_qty"):
            ln.packed_qty = None
        if hasattr(ln, "packed_by"):
            ln.packed_by = None
        if hasattr(ln, "packed_at"):
            ln.packed_at = None

    db.commit()
    audit(db, actor=f"admin:{admin_u}", action="order.reset", payload=f"order_id={o.id}")

    return RedirectResponse(
        url=f"/admin/orders/{o.customer_id}/full?date_str={o.order_date.isoformat()}",
        status_code=303,
    )

@app.post("/admin/orderlines/{line_id}/packed")
def admin_orderline_set_packed_inline(
    line_id: int,
    request: Request,
    packed_qty: str = Form(default=""),
    db: Session = Depends(get_db),
):
    require_admin(request)

    ol = db.get(OrderLine, line_id)
    if not ol:
        raise HTTPException(status_code=404, detail="Order line not found")

    val = None
    s = (packed_qty or "").strip()
    if s != "":
        try:
            s = s.replace(",", ".")
            val_f = float(s)
            if val_f < 0:
                val_f = 0.0
            val = val_f
        except Exception:
            val = ol.packed_qty

    ol.packed_qty = val

    o = db.get(Order, ol.order_id)
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    o.updated_at = datetime.utcnow()
    db.commit()

    return admin_order_summary(o.customer_id, request=request, db=db, date_str=o.order_date.isoformat())



@app.post("/admin/orders/{customer_id}/full")
async def admin_order_full_post(customer_id: int, request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)
    form = await request.form()

    # Optional submit intent (used by templates via button name="action")
    # Safe default avoids NameError and preserves existing behavior.
    action = (form.get("action") or "").strip()

    date_str = str(form.get("date_str") or "")
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        d = tomorrow_local_date()

    o = _get_order_for_customer_date(db, customer_id, d)
    if not o:
        # if somehow missing, create it
        o = Order(
            customer_id=customer_id,
            order_date=d,
            status="SUBMITTED",
            source="PHONE",
            customer_comment="",
            submitted_at=now_local(),
            updated_at=datetime.utcnow(),
        )
        db.add(o)
        db.commit()

    locked = bool(o.is_locked or o.locked_at is not None)
    override_lock = str(form.get("override_lock") or "") == "1"
    can_edit_ordered = (not locked) or override_lock

    # Build catalogue of allowed products
    cps = db.execute(
        select(CustomerProduct).where(CustomerProduct.customer_id == customer_id, CustomerProduct.is_active == True)
    ).scalars().all()
    allowed_pids = {cp.product_id for cp in cps}

    # Upsert lines
    existing = db.execute(select(OrderLine).where(OrderLine.order_id == o.id)).scalars().all()
    line_map = {ln.product_id: ln for ln in existing}

    changed = 0
    for pid in allowed_pids:
        qty_val = _to_float(form.get(f"qty_{pid}"))
        packed_val = _to_float_or_none(form.get(f"packed_{pid}"))

        # If order is locked (cutoff) and admin did not override, do not change ordered quantities.
        if not can_edit_ordered:
            prev = line_map.get(pid)
            qty_val = float(prev.qty) if (prev and prev.qty is not None) else 0.0

        ln = line_map.get(pid)
        if not ln:
            # If nothing to save, skip creating empty lines
            if (abs(qty_val) <= 1e-9) and (packed_val is None):
                continue
            # need snapshot price
            cp = next((x for x in cps if x.product_id == pid), None)
            if not cp:
                continue
            ln = OrderLine(
                order_id=o.id,
                product_id=pid,
                qty=qty_val,
                wh=0,
                unit_price_snapshot=float(cp.price) if cp.price is not None else 0.0,
                packed_qty=packed_val,
                packed_by=(f"admin:{admin_u}" if packed_val is not None else None),
                packed_at=(datetime.utcnow() if packed_val is not None else None),
            )
            db.add(ln)
            changed += 1
        else:
            # update qty
            old_qty = float(ln.qty) if ln.qty is not None else 0.0
            old_packed = float(ln.packed_qty) if ln.packed_qty is not None else None
            if abs(old_qty - qty_val) > 1e-9:
                ln.qty = qty_val
                changed += 1
            if (old_packed != packed_val):
                ln.packed_qty = packed_val
                ln.packed_by = (f"admin:{admin_u}" if packed_val is not None else None)
                ln.packed_at = (datetime.utcnow() if packed_val is not None else None)
                changed += 1

    comment = (str(form.get("comment") or "") or "").strip()
    if (o.customer_comment or "").strip() != comment:
        o.customer_comment = comment
        changed += 1

    if changed:
        o.updated_at = datetime.utcnow()
        db.commit()
        audit(db, actor=f"admin:{admin_u}", action="order.full_save", payload=f"order_id={o.id} changed={changed}")

    # Save & Close returns to dashboard; otherwise keep user on the same order page.
    if action in {"save_close", "close"}:
        return RedirectResponse(url=f"/admin/dashboard?date_str={d.isoformat()}", status_code=303)

    return RedirectResponse(url=f"/admin/orders/{customer_id}/full?date_str={d.isoformat()}", status_code=303)

@app.get("/admin/customers", response_class=HTMLResponse)
def admin_customers(request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)
    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    msg = (request.query_params.get("msg") or "").strip()
    return templates.TemplateResponse("admin_customers.html", {"request": request, "admin_user": admin_u, "customers": customers, "msg": msg})

@app.get("/admin/customers/{customer_id}/open-portal")
def admin_customers_open_portal(customer_id: int, request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)
    c = db.get(Customer, customer_id)
    if not c or not bool(c.is_active):
        raise HTTPException(404)
    resp = RedirectResponse(url=f"/p/{c.slug}/order", status_code=302)
    resp.set_cookie("portal_session", sign_session({"c": c.slug}), httponly=True, samesite="lax")
    audit(db, actor=f"admin:{admin_u}", action="customer_open_portal", payload=c.slug)
    return resp

@app.get("/admin/customers/{customer_id}/send-test-email")
def admin_customers_send_test_email(customer_id: int, request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404)

    target_email = (getattr(c, "email", "") or "").strip()
    if not target_email:
        return RedirectResponse(url="/admin/customers?msg=no_email", status_code=302)

    if not BREVO_API_KEY:
        print("BREVO ERROR: Missing BREVO_API_KEY")
        return RedirectResponse(url="/admin/customers?msg=error", status_code=302)

    try:
        payload = {
            "sender": {
                "email": FROM_EMAIL,
                "name": "Sklavounos Meat"
            },
            "to": [
                {"email": target_email}
            ],
            "subject": "Test email from Sklavounos Restaurants",
            "htmlContent": "<p>Αυτό είναι δοκιμαστικό email από την πλατφόρμα Sklavounos Restaurants.</p>"
        }

        headers = {
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json"
        }

        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers=headers,
            timeout=20
        )

        print("BREVO STATUS:", response.status_code)
        print("BREVO RESPONSE:", response.text)

        if response.status_code in (200, 201):
            audit(db, actor=f"admin:{admin_u}", action="customer_send_test_email", payload=f"{c.slug}:{target_email}")
            return RedirectResponse(url="/admin/customers?msg=sent", status_code=302)

        return RedirectResponse(url="/admin/customers?msg=error", status_code=302)

    except Exception as e:
        print("EMAIL ERROR:", repr(e))
        return RedirectResponse(url="/admin/customers?msg=error", status_code=302)

@app.post("/admin/customers/create")
def admin_customers_create(request: Request, afm: str = Form(""), db: Session = Depends(get_db), name: str = Form(""), pin: str = Form(...)):
    admin_u = require_admin(request)
    slug = slugify(name)
    base_slug = slug
    i = 2
    while db.execute(select(Customer).where(Customer.slug == slug)).scalar_one_or_none():
        slug = f"{base_slug}-{i}"
        i += 1
    c = Customer(name=name.strip(), slug=slug, pin_hash=hash_secret(pin.strip()))
    c.afm = (afm or "").strip() or None
    db.add(c)
    db.commit()
    audit(db, actor=f"admin:{admin_u}", action="customer_create", payload=slug)
    return RedirectResponse(url="/admin/customers", status_code=302)

@app.post("/admin/customers/{customer_id}/rotate-pin")
def admin_customers_rotate_pin(customer_id: int, request: Request, db: Session = Depends(get_db), new_pin: str = Form(...)):
    admin_u = require_admin(request)
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404)
    c.pin_hash = hash_secret(new_pin.strip())
    db.commit()
    audit(db, actor=f"admin:{admin_u}", action="customer_rotate_pin", payload=c.slug)
    return RedirectResponse(url="/admin/customers", status_code=302)


@app.post("/admin/customers/{customer_id}/toggle_active")
def admin_customers_toggle_active(customer_id: int, request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404)
    c.is_active = not bool(c.is_active)
    db.commit()
    audit(db, actor=f"admin:{admin_u}", action="customer_toggle_active", payload=f"{c.slug}:{c.is_active}")
    return RedirectResponse(url="/admin/customers", status_code=302)




@app.post("/admin/customers/{customer_id}/update")
def admin_customers_update(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    slug: str = Form(""),
    afm: str = Form(""),
):
    admin_u = require_admin(request)
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404)

    # Normalize fields
    new_name = (name or "").strip() or (c.name or "").strip()
    if not new_name:
        raise HTTPException(400)

    # Name uniqueness (except self)
    existing = db.execute(select(Customer).where(Customer.name == new_name, Customer.id != customer_id)).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Το όνομα χρησιμοποιείται ήδη από άλλον πελάτη.")

    desired_slug = slugify((slug or "").strip() or new_name)
    if not desired_slug:
        raise HTTPException(400)

    # If slug is changing, preserve old slug as alias (so old links keep working)
    old_slug = c.slug
    if desired_slug != old_slug:
        base_slug = desired_slug
        i = 2
        # Ensure slug doesn't collide with existing customers or aliases
        while True:
            coll_c = db.execute(select(Customer).where(Customer.slug == desired_slug, Customer.id != customer_id)).scalar_one_or_none()
            coll_a = db.execute(select(CustomerSlugAlias).where(CustomerSlugAlias.old_slug == desired_slug)).scalar_one_or_none()
            if not coll_c and not coll_a:
                break
            desired_slug = f"{base_slug}-{i}"
            i += 1

        # Insert alias for previous slug if not already stored
        if old_slug:
            exists_alias = db.execute(select(CustomerSlugAlias).where(CustomerSlugAlias.old_slug == old_slug)).scalar_one_or_none()
            if not exists_alias:
                db.add(CustomerSlugAlias(customer_id=customer_id, old_slug=old_slug))

        c.slug = desired_slug

    c.name = new_name
    c.afm = (afm or "").strip() or None

    db.commit()
    audit(db, actor=f"admin:{admin_u}", action="customer_update", payload=f"{old_slug}->{c.slug}")
    return RedirectResponse(url="/admin/customers", status_code=302)


@app.post("/admin/customers/{customer_id}/delete")
def admin_customers_delete(customer_id: int, request: Request, db: Session = Depends(get_db)):
    admin_u = require_admin(request)
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404)

    # Hard-delete only if safe (no orders). Otherwise, deactivate + rename to free name/slug.
    orders_count = db.execute(select(func.count()).select_from(Order).where(Order.customer_id == customer_id)).scalar_one()
    old_slug = c.slug
    old_name = c.name

    if int(orders_count or 0) == 0:
        # Remove aliases too
        db.execute(delete(CustomerSlugAlias).where(CustomerSlugAlias.customer_id == customer_id))
        db.delete(c)
        db.commit()
        audit(db, actor=f"admin:{admin_u}", action="customer_delete", payload=f"{old_slug}")
        return RedirectResponse(url="/admin/customers", status_code=302)

    # Keep old slug as alias so old links can still resolve (optional but safe)
    if old_slug:
        exists_alias = db.execute(select(CustomerSlugAlias).where(CustomerSlugAlias.old_slug == old_slug)).scalar_one_or_none()
        if not exists_alias:
            db.add(CustomerSlugAlias(customer_id=customer_id, old_slug=old_slug))

    # Deactivate + rename/slug to free unique constraints
    c.is_active = False
    c.name = f"{old_name} (DELETED {customer_id})"
    base_slug = slugify(f"deleted-{customer_id}-{old_slug or old_name}")
    desired_slug = base_slug
    i = 2
    while db.execute(select(Customer).where(Customer.slug == desired_slug, Customer.id != customer_id)).scalar_one_or_none():
        desired_slug = f"{base_slug}-{i}"
        i += 1
    c.slug = desired_slug

    db.commit()
    audit(db, actor=f"admin:{admin_u}", action="customer_deactivate", payload=f"{old_slug}->{c.slug}")
    return RedirectResponse(url="/admin/customers", status_code=302)

# -------------------------
# Admin Products (catalogue)
# -------------------------

@app.get("/admin/products", response_class=HTMLResponse)
def admin_products(request: Request, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    # Query params
    category = (request.query_params.get("category") or "all").strip()
    sort = (request.query_params.get("sort") or "sku").strip().lower()
    direction = (request.query_params.get("dir") or "asc").strip().lower()
    if sort not in {"sku", "name"}:
        sort = "sku"
    if direction not in {"asc", "desc"}:
        direction = "asc"

    # Categories for filter dropdown (Postgres-safe ordering)
    categories = (
        db.execute(
            select(Product.category)
            .where(Product.category.isnot(None))
            .group_by(Product.category)
            .order_by(Product.category.asc())
        )
        .scalars()
        .all()
    )

    q = select(Product)
    if category and category.lower() != "all":
        q = q.where(Product.category == category)

    # Sorting
    sort_col = Product.sku if sort == "sku" else Product.name
    q = q.order_by(
        Product.is_active.desc(),
        sort_col.desc() if direction == "desc" else sort_col.asc(),
        Product.category.asc(),
        Product.name.asc(),
    )

    products = db.execute(q).scalars().all()
    units = db.execute(select(Unit).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
    unit_map = {u.id: f"{u.label_el} ({u.code})" for u in units}
    return templates.TemplateResponse(
        "admin_products.html",
        {
            "request": request,
            "admin_user": admin_user,
            "products": products,
            "unit_map": unit_map,
            "categories": categories,
            "current_category": category,
            "sort": sort,
            "dir": direction,
        },
    )


@app.get("/admin/products/new", response_class=HTMLResponse)
def admin_product_new(request: Request, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    units = db.execute(select(Unit).where(Unit.is_active == True).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
    return templates.TemplateResponse(
        "admin_product_form.html",
        {"request": request, "admin_user": admin_user, "product": None, "units": units, "error": ""},
    )


@app.post("/admin/products/new")
def admin_product_create(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
    sku: str = Form(...),
    name: str = Form(""),
    category: str = Form("General"),
    unit_id: int = Form(...),
    is_active: str | None = Form(None),
):
    sku_clean = (sku or "").strip()
    name_clean = (name or "").strip()
    if not sku_clean or not name_clean:
        units = db.execute(select(Unit).where(Unit.is_active == True).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
        return templates.TemplateResponse(
            "admin_product_form.html",
            {"request": request, "admin_user": admin_user, "product": None, "units": units, "error": "SKU and Name are required."},
        )

    exists = db.execute(select(Product).where(Product.sku == sku_clean)).scalar_one_or_none()
    if exists:
        units = db.execute(select(Unit).where(Unit.is_active == True).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
        return templates.TemplateResponse(
            "admin_product_form.html",
            {"request": request, "admin_user": admin_user, "product": None, "units": units, "error": "SKU already exists. Use a unique SKU."},
        )

    p = Product(
        sku=sku_clean,
        name=name_clean,
        category=(category or "General").strip() or "General",
        unit_id=unit_id,
        is_active=True if is_active else False,
    )
    db.add(p)
    db.commit()
    audit(db, actor=f"admin:{admin_user}", action="product_create", payload=f"{p.id}:{p.sku}")
    return RedirectResponse(url="/admin/products", status_code=302)


@app.get("/admin/products/{p_id}", response_class=HTMLResponse)
def admin_product_edit(request: Request, p_id: int, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    p = db.execute(select(Product).where(Product.id == p_id)).scalar_one_or_none()
    if not p:
        return RedirectResponse(url="/admin/products", status_code=302)
    units = db.execute(select(Unit).where(Unit.is_active == True).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
    return templates.TemplateResponse(
        "admin_product_form.html",
        {"request": request, "admin_user": admin_user, "product": p, "units": units, "error": ""},
    )


@app.post("/admin/products/{p_id}")
def admin_product_update(
    request: Request,
    p_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
    sku: str = Form(...),
    name: str = Form(""),
    category: str = Form("General"),
    unit_id: int = Form(...),
    is_active: str | None = Form(None),
):
    p = db.execute(select(Product).where(Product.id == p_id)).scalar_one_or_none()
    if not p:
        return RedirectResponse(url="/admin/products", status_code=302)

    sku_clean = (sku or "").strip()
    name_clean = (name or "").strip()
    if not sku_clean or not name_clean:
        units = db.execute(select(Unit).where(Unit.is_active == True).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
        return templates.TemplateResponse(
            "admin_product_form.html",
            {"request": request, "admin_user": admin_user, "product": p, "units": units, "error": "SKU and Name are required."},
        )

    exists = db.execute(select(Product).where(Product.sku == sku_clean, Product.id != p.id)).scalar_one_or_none()
    if exists:
        units = db.execute(select(Unit).where(Unit.is_active == True).order_by(Unit.sort_order.asc(), Unit.code.asc())).scalars().all()
        return templates.TemplateResponse(
            "admin_product_form.html",
            {"request": request, "admin_user": admin_user, "product": p, "units": units, "error": "SKU already exists. Use a unique SKU."},
        )

    p.sku = sku_clean
    p.name = name_clean
    p.category = (category or "General").strip() or "General"
    p.unit_id = unit_id
    p.is_active = True if is_active else False
    db.commit()
    audit(db, actor=f"admin:{admin_user}", action="product_update", payload=f"{p.id}:{p.sku}")
    return RedirectResponse(url="/admin/products", status_code=302)





@app.get("/admin/announcements", response_class=HTMLResponse)
def admin_announcements(request: Request, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    customers = db.execute(select(Customer).where(Customer.is_active == True).order_by(Customer.name.asc())).scalars().all()
    announcements = db.execute(select(Announcement).order_by(Announcement.id.desc())).scalars().all()
    # Load target restaurant names (if any) for each announcement.
    # Keep compatibility with both possible column names in announcement_targets.
    try:
        if announcements:
            ann_ids = [a.id for a in announcements]
            from sqlalchemy import bindparam
            q = text(
                """
                SELECT at.announcement_id AS aid, c.name AS cname
                FROM announcement_targets at
                JOIN customers c
                  ON c.id = COALESCE(at.customer_id, at.restaurant_customer_id)
                WHERE at.announcement_id IN :ids
                ORDER BY c.name ASC
                """
            ).bindparams(bindparam("ids", expanding=True))
            rows = db.execute(q, {"ids": ann_ids}).fetchall()
            m = {}
            for r in rows:
                m.setdefault(r.aid, []).append(r.cname)
            for a in announcements:
                a._target_names = m.get(a.id, [])
        else:
            for a in announcements:
                a._target_names = []
    except Exception:
        # If announcement_targets doesn't exist (or during partial migrations),
        # do not break the page.
        for a in announcements:
            a._target_names = []
    # hydrate customer names for table
    cust_map = {c.id: c.name for c in customers}
    for a in announcements:
        setattr(a, "_customer_name", cust_map.get(a.customer_id) if a.customer_id else None)
    return templates.TemplateResponse(
        "admin_announcements.html",
        {"request": request, "admin_user": admin_user, "customers": customers, "announcements": announcements},
    )

@app.post("/admin/announcements/create")
def admin_announcements_create(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
    customer_id: str = Form(""),
    scope_mode: str = Form("global"),
    target_customer_ids: list[str] = Form([]),
    priority: str = Form("info"),
    expires_at: str = Form(""),
    dismissible: str | None = Form(None),
    is_active: str | None = Form(None),
    message: str = Form(""),
):
    msg = (message or "").strip()
    if not msg:
        return RedirectResponse(url="/admin/announcements", status_code=302)

    cid = None
    if (customer_id or "").strip():
        try:
            cid = int(customer_id)
        except Exception:
            cid = None

    scope_mode = (scope_mode or "global").strip().lower()
    target_ids: list[int] = []
    if scope_mode == "selected":
        for v in (target_customer_ids or []):
            try:
                target_ids.append(int(str(v).strip()))
            except Exception:
                pass
        target_ids = [i for i in target_ids if i > 0]

    # Multi-target: keep announcements.customer_id NULL and use announcement_targets.
    if scope_mode == "selected" and target_ids:
        cid = None

    exp = None
    if (expires_at or "").strip():
        try:
            # input type=datetime-local -> "YYYY-MM-DDTHH:MM"
            exp = datetime.fromisoformat(expires_at)
        except Exception:
            exp = None

    pr = (priority or "info").strip().lower()
    if pr not in ("info", "warning", "urgent"):
        pr = "info"

    a = Announcement(
        customer_id=cid,
        priority=pr,
        message=msg,
        dismissible=True if dismissible else False,
        is_active=True if is_active else False,
        expires_at=exp,
        created_by=admin_user,
    )
    db.add(a)
    db.commit()

    # If this is a multi-target announcement, persist targets in announcement_targets
    if scope_mode == "selected" and target_ids:
        try:
            for tid in target_ids:
                db.execute(
                    text("INSERT INTO announcement_targets (announcement_id, customer_id) VALUES (:aid, :cid)"),
                    {"aid": a.id, "cid": tid},
                )
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    audit(db, actor=f"admin:{admin_user}", action="announcement_create", payload=f"id={a.id}")
    return RedirectResponse(url="/admin/announcements", status_code=303)

@app.post("/admin/announcements/{ann_id}/toggle")
def admin_announcements_toggle(ann_id: int, request: Request, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    a = db.execute(select(Announcement).where(Announcement.id == ann_id)).scalar_one_or_none()
    if a:
        a.is_active = not bool(a.is_active)
        db.commit()
        audit(db, actor=f"admin:{admin_user}", action="announcement_toggle", payload=f"id={ann_id}:{a.is_active}")
    return RedirectResponse(url="/admin/announcements", status_code=303)

@app.post("/admin/announcements/{ann_id}/delete")
def admin_announcements_delete(ann_id: int, request: Request, db: Session = Depends(get_db), admin_user: str = Depends(require_admin)):
    a = db.execute(select(Announcement).where(Announcement.id == ann_id)).scalar_one_or_none()
    if a:
        db.delete(a)
        db.commit()
        audit(db, actor=f"admin:{admin_user}", action="announcement_delete", payload=f"id={ann_id}")
    return RedirectResponse(url="/admin/announcements", status_code=303)



def _portal_customer_by_slug_or_alias(db: Session, slug: str) -> tuple[Customer | None, str | None]:
    """Returns (customer, canonical_slug). If not found returns (None, None)."""
    c = db.execute(select(Customer).where(Customer.slug == slug, Customer.is_active == True)).scalar_one_or_none()
    if c:
        return c, c.slug
    a = db.execute(select(CustomerSlugAlias).where(CustomerSlugAlias.old_slug == slug)).scalar_one_or_none()
    if a:
        c2 = db.get(Customer, a.customer_id)
        if c2 and bool(c2.is_active):
            return c2, c2.slug
    return None, None


@app.post("/p/{slug}/update-contact")
def update_contact(
    slug: str,
    request: Request,
    email: str = Form(""),
    phone: str = Form(""),
    date_str: str = Form(""),
    db: Session = Depends(get_db),
):
    c = db.query(Customer).filter(Customer.slug == slug).first()
    if not c:
        raise HTTPException(404)

    c.email = (email or "").strip()
    c.phone = (phone or "").strip()
    db.commit()

    qs = f"?date_str={date_str}&msg=contact_saved" if (date_str or "").strip() else "?msg=contact_saved"
    return RedirectResponse(url=f"/p/{slug}/order{qs}", status_code=302)

@app.get("/p/{slug}", response_class=HTMLResponse)
def portal_pin_get(slug: str, request: Request, db: Session = Depends(get_db)):
    c, canon = _portal_customer_by_slug_or_alias(db, slug)
    if not c:
        raise HTTPException(404)
    if canon != slug:
        return RedirectResponse(url=f"/p/{canon}", status_code=302)
    return templates.TemplateResponse("portal_pin.html", {"request": request, "customer": c})

@app.post("/p/{slug}")
def portal_pin_post(slug: str, request: Request, db: Session = Depends(get_db), pin: str = Form(...)):
    c, canon = _portal_customer_by_slug_or_alias(db, slug)
    if not c:
        raise HTTPException(404)
    if canon != slug:
        return RedirectResponse(url=f"/p/{canon}", status_code=302)
    if not verify_secret(pin.strip(), c.pin_hash):
        return templates.TemplateResponse("portal_pin.html", {"request": request, "customer": c, "error": "Λάθος PIN."}, status_code=401)

    resp = RedirectResponse(url=f"/p/{slug}/order", status_code=302)
    resp.set_cookie("portal_session", sign_session({"c": slug}), httponly=True, samesite="lax")
    audit(db, actor=f"portal:{slug}", action="portal_login")
    return resp

@app.get("/p/{slug}/order", response_class=HTMLResponse)
def portal_order_get(slug: str, request: Request, db: Session = Depends(get_db), date_str: str | None = None, empty_error: int = 0, pwd_msg: str | None = None, pwd_err: str | None = None):
    c, canon = _portal_customer_by_slug_or_alias(db, slug)
    if not c:
        raise HTTPException(404)
    if canon != slug:
        q = ("?" + str(request.url.query)) if request.url.query else ""
        return RedirectResponse(url=f"/p/{canon}/order{q}", status_code=302)
    if get_portal_customer(request) != canon:
        return RedirectResponse(url=f"/p/{slug}", status_code=302)

    d = today_local_date()
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            pass

    # block past dates
    today = today_local_date()
    if d < today:
        d = today

    cps = db.execute(
        select(CustomerProduct, Product, Unit)
        .join(Product, CustomerProduct.product_id == Product.id)
        .join(Unit, Product.unit_id == Unit.id)
        .where(CustomerProduct.customer_id == c.id, CustomerProduct.is_active == True, Product.is_active == True)
        .order_by(Product.name.asc())
    ).all()

    o = db.execute(select(Order).where(Order.customer_id == c.id, Order.order_date == d)).scalar_one_or_none()
    locked = bool(o and (o.is_locked or o.locked_at is not None))
    submitted = bool(o and getattr(o, "submitted_at", None) is not None)
    comment = o.customer_comment if o else ""

    qty_map = {}
    if o:
        lines = db.execute(select(OrderLine).where(OrderLine.order_id == o.id)).scalars().all()
        for ln in lines:
            qty_map[ln.product_id] = float(ln.qty or 0)

    qty_map_disp = qty_map  # backwards-compatible alias

    # History orders for expand/copy (last 10, excluding current date)
    history_orders = []
    try:
        hos = (
            db.execute(
                select(Order)
                .where(Order.customer_id == c.id, Order.order_date != d)
                .order_by(Order.order_date.desc())
                .limit(10)
            )
            .scalars()
            .all()
        )
        for ho in hos:
            ln_rows = (
                db.execute(
                    select(OrderLine, Product, Unit)
                    .join(Product, Product.id == OrderLine.product_id)
                    .join(Unit, Unit.id == Product.unit_id)
                    .where(OrderLine.order_id == ho.id)
                    .order_by(Product.category, Product.name)
                )
                .all()
            )
            lines = []
            copy_parts = []
            for ln, p, u in ln_rows:
                q = float(ln.qty or 0)
                if q <= 0:
                    continue
                lines.append(
                    {
                        "product_id": p.id,
                        "product_name": p.name,
                        "unit_code": u.code,
                        "qty": q,
                    }
                )
                # pid=qty format, qty trimmed for integers
                if abs(q - round(q)) < 1e-9:
                    q_str = str(int(round(q)))
                else:
                    q_str = ("%.3f" % q).rstrip("0").rstrip(".")
                copy_parts.append(f"{p.id}={q_str}")
            history_orders.append(
                {
                    "id": ho.id,
                    "order_date": ho.order_date,
                    "status": ho.status,
                    "lines": lines,
                    "copy": ",".join(copy_parts),
                }
            )
    except Exception:
        history_orders = []

    return templates.TemplateResponse("portal_order.html", {"request": request, "customer": c, "date": d, "items": cps, "qty_map": qty_map_disp, "qty_map_disp": qty_map_disp, "locked": locked, "submitted": submitted, "comment": (comment or ""), "history_orders": history_orders, "empty_error": bool(empty_error), "message": pwd_msg, "pwd_error": pwd_err})

@app.post("/p/{slug}/change-pin")
def portal_change_pin(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    current_pin: str = Form(...),
    new_pin: str = Form(...),
    confirm_pin: str = Form(...),
    current_date: str = Form(""),
):
    c, canon = _portal_customer_by_slug_or_alias(db, slug)
    if not c:
        raise HTTPException(404)
    if canon != slug:
        return RedirectResponse(url=f"/p/{canon}/order", status_code=302)
    if get_portal_customer(request) != canon:
        raise HTTPException(status_code=401)

    date_q = f"?date_str={current_date}" if (current_date or '').strip() else ''

    cur = (current_pin or '').strip()
    new = (new_pin or '').strip()
    conf = (confirm_pin or '').strip()

    if not verify_secret(cur, c.pin_hash):
        return RedirectResponse(url=f"/p/{slug}/order{date_q}{'&' if date_q else '?'}pwd_err=Λάθος+τρέχον+PIN.", status_code=303)
    if len(new) < 4:
        return RedirectResponse(url=f"/p/{slug}/order{date_q}{'&' if date_q else '?'}pwd_err=Το+νέο+PIN+πρέπει+να+έχει+τουλάχιστον+4+χαρακτήρες.", status_code=303)
    if new != conf:
        return RedirectResponse(url=f"/p/{slug}/order{date_q}{'&' if date_q else '?'}pwd_err=Το+νέο+PIN+και+η+επιβεβαίωση+δεν+ταιριάζουν.", status_code=303)
    if cur == new:
        return RedirectResponse(url=f"/p/{slug}/order{date_q}{'&' if date_q else '?'}pwd_err=Το+νέο+PIN+πρέπει+να+είναι+διαφορετικό+από+το+τρέχον.", status_code=303)

    c.pin_hash = hash_secret(new)
    db.commit()
    audit(db, actor=f"portal:{slug}", action="portal_change_pin")
    return RedirectResponse(url=f"/p/{slug}/order{date_q}{'&' if date_q else '?'}pwd_msg=Το+PIN+άλλαξε+επιτυχώς.", status_code=303)

@app.post("/p/{slug}/order")
async def portal_order_post(slug: str, request: Request, db: Session = Depends(get_db), date_str: str = Form(...), comment: str = Form("")):
    c, canon = _portal_customer_by_slug_or_alias(db, slug)
    if not c:
        raise HTTPException(404)
    if canon != slug:
        return RedirectResponse(url=f"/p/{canon}/order", status_code=302)
    if get_portal_customer(request) != canon:
        raise HTTPException(status_code=401)

    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        d = tomorrow_local_date()

    o = db.execute(select(Order).where(Order.customer_id == c.id, Order.order_date == d)).scalar_one_or_none()
    if o and (o.is_locked or o.locked_at is not None):
        return RedirectResponse(url=f"/p/{slug}/order?date_str={d.isoformat()}", status_code=302)

    # One-time submit: if already submitted, do not accept resubmission
    if o and getattr(o, "submitted_at", None) is not None:
        return RedirectResponse(url=f"/p/{slug}/order?date_str={d.isoformat()}", status_code=302)

    cps = db.execute(
        select(CustomerProduct, Product)
        .join(Product, CustomerProduct.product_id == Product.id)
        .where(CustomerProduct.customer_id == c.id, CustomerProduct.is_active == True, Product.is_active == True)
    ).all()

    form = await request.form()
    parsed_qtys: dict[int, float] = {}
    total_items = 0
    clean_comment = (comment or "").strip()

    for cp, p in cps:
        key = f"qty_{p.id}"
        raw = str(form.get(key, "")).strip().replace(",", ".")
        try:
            qty = float(raw) if raw else 0.0
        except ValueError:
            qty = 0.0

        parsed_qtys[p.id] = qty
        if qty > 0:
            total_items += 1

    # Block completely empty portal submissions.
    # Keep comment-only orders allowed, exactly as the UI message suggests.
    if total_items == 0 and not clean_comment:
        return RedirectResponse(url=f"/p/{slug}/order?date_str={d.isoformat()}&empty_error=1", status_code=303)

    if not o:
        o = Order(
            customer_id=c.id,
            order_date=d,
            status="SUBMITTED",
            source="PORTAL",
            is_locked=False,
            customer_comment=clean_comment,
            submitted_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(o)
        db.commit()
    else:
        o.customer_comment = clean_comment
        if getattr(o, "submitted_at", None) is None:
            o.submitted_at = datetime.utcnow()
        o.updated_at = datetime.utcnow()
        db.commit()

    for cp, p in cps:
        qty = parsed_qtys.get(p.id, 0.0)
        ln = db.execute(select(OrderLine).where(OrderLine.order_id == o.id, OrderLine.product_id == p.id)).scalar_one_or_none()
        if not ln:
            ln = OrderLine(order_id=o.id, product_id=p.id, qty=qty, wh=0, unit_price_snapshot=float(cp.price))
            db.add(ln)
        else:
            ln.qty = qty

    db.commit()

    # Auto-lock portal orders after first submit
    o.is_locked = True
    o.locked_by = f"portal:{slug}"
    o.locked_at = now_local()
    o.status = "LOCKED"
    o.updated_at = datetime.utcnow()
    db.commit()

    audit(db, actor=f"portal:{slug}", action="order_submit", payload=f"{d.isoformat()} items={total_items}")

    def _fmt_qty_for_telegram(q: float, unit_code: str) -> str:
        u = (unit_code or "").strip().upper()
        if u == "KG":
            return f"{q:.2f}"
        if u == "PCS":
            return str(int(round(q)))
        if abs(q - round(q)) < 1e-9:
            return str(int(round(q)))
        return f"{q:.2f}"

    lines: list[str] = []
    try:
        q_lines = (
            select(
                Product.name,
                func.coalesce(CustomerProduct.unit_override, Unit.code).label("unit_code"),
                func.coalesce(func.sum(OrderLine.qty), 0),
            )
            .join(Product, Product.id == OrderLine.product_id)
            .join(Unit, Unit.id == Product.unit_id)
            .outerjoin(
                CustomerProduct,
                (CustomerProduct.product_id == Product.id)
                & (CustomerProduct.customer_id == o.customer_id)
            )
            .where(OrderLine.order_id == o.id)
            .group_by(
                Product.name,
                func.coalesce(CustomerProduct.unit_override, Unit.code)
            )
            .having(func.sum(OrderLine.qty) > 0)
            .order_by(Product.name.asc())
        )
        for pname, ucode, ssum in db.execute(q_lines).all():
            qv = float(ssum or 0)
            if qv <= 0:
                continue
            lines.append(f"• {pname}: {_fmt_qty_for_telegram(qv, ucode)} {ucode}")
    except Exception:
        lines = []

    order_comment = (o.customer_comment or "").strip()

    msg_parts = [
        "📩 Νέα παραγγελία",
        str(c.name),
        f"Ημερομηνία: {d.strftime('%d-%m-%Y')}",
    ]
    if order_comment:
        msg_parts.append(f"💬 Σχόλιο: {order_comment}")
    if lines:
        msg_parts.append("\n".join(lines))
    else:
        msg_parts.append(f"Items: {total_items}")

    send_telegram("\n".join(msg_parts))
    return RedirectResponse(url=f"/p/{slug}/order?date_str={d.isoformat()}", status_code=302)



def _require_agent(request: Request, station: str) -> None:
    station = (station or "").strip().upper()
    if station not in ("CENTRAL", "WORKSHOP"):
        raise HTTPException(status_code=400, detail="Invalid station")
    hdr = (request.headers.get("x-agent-token") or "").strip()
    env_key = "PRINT_AGENT_TOKEN_CENTRAL" if station == "CENTRAL" else "PRINT_AGENT_TOKEN_WORKSHOP"
    expected = (os.getenv(env_key) or "").strip()
    if not expected or hdr != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


class AgentLabelsPayload(BaseModel):
    labels: list[str] = []

def _cache_station_labels(station: str, labels: list[str]) -> None:
    station = station.upper()
    if station not in AGENT_LABEL_CACHE:
        return
    clean = []
    seen = set()
    for x in (labels or []):
        s = (x or "").strip()
        if not s:
            continue
        if s.lower().endswith(".lbx"):
            s = s[:-4]
        if s not in seen:
            clean.append(s)
            seen.add(s)
    clean.sort(key=lambda a: a.casefold())
    AGENT_LABEL_CACHE[station]["labels"] = clean
    AGENT_LABEL_CACHE[station]["last_seen"] = datetime.utcnow()

def _station_is_online(station: str, max_age_seconds: int = 120) -> bool:
    d = AGENT_LABEL_CACHE.get(station.upper())
    if not d or not d.get("last_seen"):
        return False
    age = (datetime.utcnow() - d["last_seen"]).total_seconds()
    return age <= max_age_seconds

def _pick_best_station() -> str:
    cand = []
    for st in ("CENTRAL", "WORKSHOP"):
        last = AGENT_LABEL_CACHE[st]["last_seen"]
        if last:
            cand.append((last, st))
    if not cand:
        return "CENTRAL"
    cand.sort(reverse=True)
    best = cand[0][1]
    if len(cand) > 1 and cand[0][0] == cand[1][0]:
        return "CENTRAL"
    return best

@app.post("/api/print-agent/labels")
def api_print_agent_labels(request: Request, station: str, payload: AgentLabelsPayload):
    _require_agent(request, station)
    station = station.upper()
    _cache_station_labels(station, payload.labels or [])
    return {"ok": True, "station": station, "count": len(AGENT_LABEL_CACHE[station]["labels"])}

@app.get("/api/print-agent/status")
def api_print_agent_status():
    out = {"ok": True, "stations": {}}
    for st in ("CENTRAL", "WORKSHOP"):
        last = AGENT_LABEL_CACHE[st]["last_seen"]
        out["stations"][st] = {
            "online": _station_is_online(st),
            "last_seen": last.isoformat() if last else None,
            "count": len(AGENT_LABEL_CACHE[st]["labels"]),
        }
    return out

@app.get("/api/print-agent/labels")
def api_print_agent_labels_get(station: str | None = None):
    st = (station or "").strip().upper()
    if st not in ("CENTRAL", "WORKSHOP"):
        st = _pick_best_station()
    return {
        "ok": True,
        "station": st,
        "online": _station_is_online(st),
        "labels": AGENT_LABEL_CACHE[st]["labels"],
    }

@app.get("/api/print-jobs/next")
def api_print_jobs_next(
    request: Request,
    station: str,
    db: Session = Depends(get_db),
):
    _require_agent(request, station)
    station = station.upper()

    job = (
        db.execute(
            select(PrintJob)
            .where(PrintJob.target_station == station)
            .where(PrintJob.status == "QUEUED")
            .order_by(PrintJob.id.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if not job:
        return {"ok": True, "job": None}

    return {
        "ok": True,
        "job": {
            "id": job.id,
            "restaurant_id": job.restaurant_id,
            "label_key": job.label_key,
            "target_station": job.target_station,
            "copies": int(getattr(job, "copies", 1) or 1),
            "created_at": job.created_at.isoformat() if job.created_at else None,
        },
    }


@app.post("/api/print-jobs/{job_id}/done")
def api_print_jobs_done(
    request: Request,
    job_id: int,
    station: str,
    db: Session = Depends(get_db),
):
    _require_agent(request, station)
    station = station.upper()

    job = db.execute(select(PrintJob).where(PrintJob.id == job_id)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404)
    if job.target_station != station:
        raise HTTPException(status_code=403)

    job.status = "PRINTED"
    job.printed_at = datetime.utcnow()
    job.error_message = ""
    db.commit()
    return {"ok": True}


@app.post("/api/print-jobs/{job_id}/fail")
def api_print_jobs_fail(
    request: Request,
    job_id: int,
    station: str,
    error_message: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_agent(request, station)
    station = station.upper()

    job = db.execute(select(PrintJob).where(PrintJob.id == job_id)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404)
    if job.target_station != station:
        raise HTTPException(status_code=403)

    job.status = "FAILED"
    job.printed_at = datetime.utcnow()
    job.error_message = (error_message or "")[:500]
    db.commit()
    return {"ok": True}
