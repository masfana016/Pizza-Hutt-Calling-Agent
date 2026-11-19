"""Pizza Hut ordering tools: menu, orders, status, cancel.
Database: Supabase Postgres (or any SQL database) through SQLModel, via DB_URI.
"""
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import func
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

load_dotenv()

DB_URI = os.getenv("DB_URI")
if not DB_URI:
    raise ValueError("DB_URI is not set in the .env file")

# echo stays off on purpose: SQL logs would contain customer names and addresses
engine = create_engine(DB_URI, pool_pre_ping=True)

# Set these to the real store values
PICKUP_MINUTES = 20
DELIVERY_MINUTES = 45
MAX_QTY = 20


# =========================================================
# MODELS
# =========================================================

class MenuItem(SQLModel, table=True):
    __tablename__ = "menu_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    category: str  # pizza / sides / drinks
    size: str = Field(default="regular")
    price: float
    available: bool = Field(default=True)


class Order(SQLModel, table=True):
    __tablename__ = "orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_name: str
    phone: Optional[str] = None
    order_type: str  # pickup / delivery
    address: Optional[str] = None
    # received -> preparing -> ready / out_for_delivery -> delivered | cancelled
    # (staff update the status from the Supabase dashboard)
    status: str = Field(default="received")
    total: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OrderItem(SQLModel, table=True):
    __tablename__ = "order_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    name: str
    size: str
    quantity: int
    unit_price: float


# =========================================================
# SAMPLE MENU (placeholder data: replace with the real menu)
# =========================================================

SAMPLE_MENU = [
    ("Margherita", "pizza", "small", 700), ("Margherita", "pizza", "medium", 1200), ("Margherita", "pizza", "large", 1700),
    ("Pepperoni", "pizza", "small", 900), ("Pepperoni", "pizza", "medium", 1500), ("Pepperoni", "pizza", "large", 2100),
    ("Chicken Tikka", "pizza", "small", 950), ("Chicken Tikka", "pizza", "medium", 1600), ("Chicken Tikka", "pizza", "large", 2200),
    ("Veggie Supreme", "pizza", "small", 850), ("Veggie Supreme", "pizza", "medium", 1400), ("Veggie Supreme", "pizza", "large", 1950),
    ("Garlic Bread", "sides", "regular", 450),
    ("Chicken Wings", "sides", "regular", 750),
    ("Fries", "sides", "regular", 350),
    ("Pepsi", "drinks", "regular", 150),
    ("Mineral Water", "drinks", "regular", 100),
]


def init_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if session.exec(select(MenuItem)).first() is None:
            session.add_all(
                MenuItem(name=n, category=c, size=sz, price=p) for n, c, sz, p in SAMPLE_MENU
            )
            session.commit()


# =========================================================
# HELPERS
# =========================================================

def _norm(text) -> str:
    return (text or "").strip().lower()


def _find_item(session: Session, name: str, size: str):
    """Return (MenuItem, None) or (None, error message)."""
    n = _norm(name)
    if not n:
        return None, "An item name is missing."

    base = select(MenuItem).where(col(MenuItem.available).is_(True))
    rows = session.exec(base.where(func.lower(MenuItem.name) == n)).all()
    if not rows:
        rows = session.exec(base.where(func.lower(MenuItem.name).contains(n))).all()

    names = sorted({r.name for r in rows})
    if not rows:
        return None, f"'{name}' is not on the menu."
    if len(names) > 1:
        return None, f"'{name}' matches several items: {', '.join(names)}. Ask which one."

    by_size = {_norm(r.size): r for r in rows}
    s = _norm(size)
    if s in by_size:
        return by_size[s], None
    if not s and len(rows) == 1:
        return rows[0], None
    sizes = ", ".join(r.size for r in rows)
    return None, f"{names[0]} comes in these sizes: {sizes}. Ask the customer which size."


def _owned_order(session: Session, order_id, customer_name: str, phone: str):
    """An order is visible only to the caller whose phone or name matches."""
    try:
        order = session.get(Order, int(order_id))
    except (TypeError, ValueError):
        return None
    if not order:
        return None
    phone_ok = bool(phone) and phone == (order.phone or "")
    name_ok = bool(_norm(customer_name)) and _norm(customer_name) == _norm(order.customer_name)
    return order if phone_ok and name_ok else None


NOT_FOUND = {"error": "No order found with that number and name."}


# =========================================================
# TOOLS
# =========================================================

def get_menu(category: str = "") -> dict:
    """List available menu items with sizes and prices."""
    with Session(engine) as session:
        q = select(MenuItem).where(col(MenuItem.available).is_(True))
        if _norm(category):
            q = q.where(func.lower(MenuItem.category) == _norm(category))
        rows = session.exec(q.order_by(MenuItem.category, MenuItem.name, MenuItem.price)).all()
    if not rows:
        return {"error": "No items found. Categories are pizza, sides and drinks."}
    return {
        "currency": "PKR",
        "items": [
            {"category": r.category, "name": r.name, "size": r.size, "price": r.price}
            for r in rows
        ],
    }


def place_order(
    customer_name: str,
    order_type: str,
    items: list,
    confirmed: bool = False,
    address: str = "",
    phone: str = "",
) -> dict:
    """Save an order. Prices always come from the database, never from the caller."""
    if confirmed is not True:
        return {"error": "Not confirmed. Read the full order and total back, get a clear yes, then call again with confirmed=true."}

    order_type = _norm(order_type)
    if order_type not in ("pickup", "delivery"):
        return {"error": "order_type must be pickup or delivery."}
    if not _norm(customer_name):
        return {"error": "Customer name is missing."}
    if order_type == "delivery" and not _norm(address):
        return {"error": "A delivery address is required."}
    if not items:
        return {"error": "The order has no items."}

    with Session(engine) as session:
        lines, total = [], 0.0
        for it in items:
            try:
                qty = int(it.get("quantity", 1))
            except (TypeError, ValueError):
                return {"error": "Quantity must be a number."}
            if not 1 <= qty <= MAX_QTY:
                return {"error": f"Quantity must be between 1 and {MAX_QTY}."}
            menu_item, err = _find_item(session, it.get("name", ""), it.get("size", ""))
            if err:
                return {"error": err}
            lines.append((menu_item, qty))
            total += menu_item.price * qty

        order = Order(
            customer_name=customer_name.strip(),
            phone=phone or None,
            order_type=order_type,
            address=address.strip() if order_type == "delivery" else None,
            total=round(total, 2),
        )
        session.add(order)
        session.flush()  # assigns order.id
        for menu_item, qty in lines:
            session.add(OrderItem(
                order_id=order.id, name=menu_item.name, size=menu_item.size,
                quantity=qty, unit_price=menu_item.price,
            ))
        session.commit()
        return {
            "status": "success",
            "order_id": order.id,
            "total": order.total,
            "currency": "PKR",
            "order_type": order_type,
            "estimated_minutes": DELIVERY_MINUTES if order_type == "delivery" else PICKUP_MINUTES,
        }


def check_order_status(order_id: int, customer_name: str = "", phone: str = "") -> dict:
    with Session(engine) as session:
        order = _owned_order(session, order_id, customer_name, phone)
        if not order:
            return NOT_FOUND
        items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()
        return {
            "order_id": order.id,
            "status": order.status,
            "order_type": order.order_type,
            "total": order.total,
            "currency": "PKR",
            "items": [f"{i.quantity} x {i.name} ({i.size})" for i in items],
        }


def cancel_order(order_id: int, customer_name: str = "", phone: str = "") -> dict:
    with Session(engine) as session:
        order = _owned_order(session, order_id, customer_name, phone)
        if not order:
            return NOT_FOUND
        if order.status != "received":
            return {"error": f"This order is already '{order.status}' and cannot be cancelled by phone. Ask the customer to call the store."}
        order.status = "cancelled"
        session.add(order)
        session.commit()
        return {"status": "cancelled", "order_id": order.id}


def add_items_to_order(order_id: int, items: list, customer_name: str = "", phone: str = "") -> dict:
    """Add items to an existing order that has not started preparation."""
    if not items:
        return {"error": "No items to add."}
    with Session(engine) as session:
        order = _owned_order(session, order_id, customer_name, phone)
        if not order:
            return NOT_FOUND
        if order.status != "received":
            return {"error": f"This order is already '{order.status}', so items can no longer be added. A new order is needed."}
        lines, extra = [], 0.0
        for it in items:
            try:
                qty = int(it.get("quantity", 1))
            except (TypeError, ValueError):
                return {"error": "Quantity must be a number."}
            if not 1 <= qty <= MAX_QTY:
                return {"error": f"Quantity must be between 1 and {MAX_QTY}."}
            menu_item, err = _find_item(session, it.get("name", ""), it.get("size", ""))
            if err:
                return {"error": err}
            lines.append((menu_item, qty))
            extra += menu_item.price * qty
        for menu_item, qty in lines:
            session.add(OrderItem(order_id=order.id, name=menu_item.name, size=menu_item.size,
                                  quantity=qty, unit_price=menu_item.price))
        order.total = round(order.total + extra, 2)
        session.add(order)
        session.commit()
        return {"status": "updated", "order_id": order.id, "new_total": order.total, "currency": "PKR"}

FUNCTION_MAP = {
    "get_menu": get_menu,
    "place_order": place_order,
    "add_items_to_order": add_items_to_order,
    "check_order_status": check_order_status,
    "cancel_order": cancel_order,
}