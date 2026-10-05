import os, io, secrets, json as _json
from datetime import datetime, date, timedelta, timezone
from functools import wraps
from flask import Flask, request, session, redirect, jsonify, render_template, send_file, abort, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

app = Flask(__name__)

CAFE_TZ = float(os.getenv("CAFE_TZ_OFFSET", "5.75"))
LOCAL_TZ = timezone(timedelta(hours=CAFE_TZ))


def now():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


IS_PROD = bool(os.getenv("RENDER")) or os.getenv("FLASK_DEBUG", "0") != "1"

app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-change-me"),
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///./cafe_x.db"),
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 280},
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
db = SQLAlchemy(app)

CAFE = os.getenv("CAFE_NAME", "Cafe X")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")
PRINT_KEY = os.getenv("PRINT_API_KEY", "change-me-bridge-key")

if os.getenv("FLASK_DEBUG") == "1" and os.getenv("RENDER"):
    print("WARNING: DEBUG MODE ON IN PRODUCTION", flush=True)

STATUSES = ["new", "accepted", "preparing", "ready", "served", "completed", "cancelled"]
MSG = {
    "new": "Order received",
    "accepted": "Order accepted",
    "preparing": "Being prepared",
    "ready": "Ready!",
    "served": "Served",
    "completed": "Thank you for dining with us",
    "cancelled": "Cancelled",
}


# ============================================================
#   MODELS
# ============================================================

class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True)
    pw_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), default="staff")


class Table(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    status = db.Column(db.String(20), default="available")
    token = db.Column(db.String(32), unique=True, default=lambda: secrets.token_urlsafe(8))
    active = db.Column(db.Boolean, default=True)
    # Locked to one customer while they have an open order
    locked_to_customer_id = db.Column(db.Integer, nullable=True)


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80))
    phone = db.Column(db.String(20), unique=True, index=True)
    disabled = db.Column(db.Boolean, default=False)
    session_key = db.Column(db.String(32), default=lambda: secrets.token_urlsafe(8))
    device_token = db.Column(db.String(64), unique=True, nullable=True)
    created = db.Column(db.DateTime, default=now)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60))
    sort = db.Column(db.Integer, default=0)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.ForeignKey("category.id"))
    name = db.Column(db.String(100))
    description = db.Column(db.Text, default="")
    price = db.Column(db.Numeric(10, 2))
    image_url = db.Column(db.String(300), default="")
    veg = db.Column(db.Boolean, default=True)
    available = db.Column(db.Boolean, default=True)
    featured = db.Column(db.Boolean, default=False)
    category = db.relationship("Category")


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.ForeignKey("customer.id"), index=True, nullable=False)
    table_id = db.Column(db.ForeignKey("table.id"), nullable=False)
    subtotal = db.Column(db.Numeric(10, 2))
    discount = db.Column(db.Numeric(10, 2), default=0)
    total = db.Column(db.Numeric(10, 2))
    reward_used = db.Column(db.Boolean, default=False)
    promo_name = db.Column(db.String(80))
    payment_status = db.Column(db.String(10), default="unpaid")
    status = db.Column(db.String(20), default="new", index=True)
    created = db.Column(db.DateTime, default=now, index=True)
    customer = db.relationship("Customer")
    table = db.relationship("Table")
    items = db.relationship("OrderItem", backref="order")

    @property
    def number(self):
        return 1000 + self.id


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.ForeignKey("order.id"), index=True)
    name = db.Column(db.String(100))
    price = db.Column(db.Numeric(10, 2))
    qty = db.Column(db.Integer)


class PrintJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.ForeignKey("order.id"), index=True)
    status = db.Column(db.String(12), default="pending", index=True)
    attempts = db.Column(db.Integer, default=0)
    error = db.Column(db.Text)
    updated = db.Column(db.DateTime, default=now)


class Setting(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(100))


class LoyaltyTx(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.ForeignKey("customer.id"), index=True)
    delta = db.Column(db.Integer)
    note = db.Column(db.String(200))
    created = db.Column(db.DateTime, default=now)


LOYALTY_DEFAULTS = {"orders_required": "10", "percent": "50", "max_discount": "500", "min_order": "500"}


# ============================================================
#   HELPERS
# ============================================================

def setting(k):
    s = db.session.get(Setting, k)
    return float(s.value if s else LOYALTY_DEFAULTS[k])


def loyalty(c):
    req = int(setting("orders_required")) or 1
    done = Order.query.filter_by(customer_id=c.id, status="completed").count() + int(
        db.session.query(db.func.coalesce(db.func.sum(LoyaltyTx.delta), 0))
        .filter_by(customer_id=c.id).scalar() or 0
    )
    used = Order.query.filter(
        Order.customer_id == c.id, Order.reward_used, Order.status != "cancelled"
    ).count()
    return {"completed": done, "required": req, "progress": done % req, "reward": done // req > used}


def free_table_if_done(table_id):
    """If no live orders remain on this table, free it: status available,
    rotate token, clear lock."""
    still_open = Order.query.filter(
        Order.table_id == table_id,
        Order.status.in_(["new", "accepted", "preparing", "ready", "served"]),
    ).count()
    if still_open == 0:
        t = db.session.get(Table, table_id)
        if t:
            t.status = "available"
            t.token = secrets.token_urlsafe(8)
            t.locked_to_customer_id = None
            return t
    return None


def ticket(o):
    w = 42
    L = ["=" * w, CAFE.center(w), f"ORDER #{o.number}".center(w), "=" * w,
         f"Table: {o.table.name}", f"Customer: {o.customer.name}", f"Phone: {o.customer.phone}", "-" * w]
    for i in o.items:
        L.append(f"{i.qty} x {i.name}"[:30].ljust(30) + f"{i.price * i.qty:>12,.0f}")
    L += ["-" * w,
          "Subtotal:".ljust(30) + f"{o.subtotal:>12,.0f}",
          "Discount:".ljust(30) + f"{o.discount:>12,.0f}",
          "TOTAL: Rs.".ljust(30) + f"{o.total:>12,.0f}",
          f"Time: {o.created:%d %b %Y %I:%M %p}", "Thank you!".center(w)]
    return "\n".join(L)


def serialize_order(o):
    return {
        "id": o.id,
        "number": o.number,
        "table": o.table.name,
        "customer": o.customer.name,
        "phone": o.customer.phone,
        "total": float(o.total),
        "discount": float(o.discount),
        "status": o.status,
        "time": o.created.strftime("%I:%M %p"),
        "paid": o.payment_status,
        "promo": o.promo_name,
        "items": [f"{i.qty} x {i.name}" for i in o.items],
    }


def resolve_customer():
    """Identify the current customer from session OR device_token cookie."""
    c = db.session.get(Customer, session.get("cid", 0))
    if c and not c.disabled:
        return c
    # Fallback: device token cookie
    token = request.cookies.get("device_token")
    if token:
        c = Customer.query.filter_by(device_token=token, disabled=False).first()
        if c:
            session["cid"] = c.id
            session["ckey"] = c.session_key
            return c
    return None


# ============================================================
#   BOOTSTRAP
# ============================================================

_bootstrapped = False


@app.before_request
def _bootstrap():
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        db.create_all()
        if not Admin.query.first():
            db.session.add(Admin(
                username="admin",
                pw_hash=generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin123")),
                role="superadmin",
            ))
            db.session.add_all([Table(name=str(n)) for n in range(1, 6)])
            k = Category(name="Coffee", sort=1)
            db.session.add(k)
            db.session.flush()
            db.session.add(Item(category_id=k.id, name="Cold Coffee",
                                description="Iced, creamy, smooth.", price=200, featured=True))
        for k, v in LOYALTY_DEFAULTS.items():
            if not db.session.get(Setting, k):
                db.session.add(Setting(key=k, value=v))
        db.session.commit()

        if Item.query.count() < 10:
            try:
                import seed_data
                seed_data.run()
                print("[bootstrap] menu auto-seeded", flush=True)
            except Exception as e:
                print(f"[bootstrap] auto-seed failed: {e}", flush=True)

        _bootstrapped = True
    except Exception as e:
        print(f"[bootstrap] {e}", flush=True)
        db.session.rollback()


# ============================================================
#   HEALTH + HOME
# ============================================================

@app.get("/healthz")
def healthz():
    return jsonify(status="ok"), 200


@app.route("/")
def home():
    return redirect("/menu")


# ============================================================
#   CUSTOMER FLOW — MENU, JOIN, LOGOUT
# ============================================================

@app.route("/menu")
def menu():
    t = request.args.get("t")

    # Step 1: identify the customer
    c = resolve_customer()

    # Step 2: handle QR scan
    if t:
        tb = Table.query.filter_by(token=t, active=True).first()
        if not tb:
            return "Invalid or inactive table QR code.", 404

        # Lock check: table locked to a DIFFERENT customer?
        if tb.locked_to_customer_id and (not c or tb.locked_to_customer_id != c.id):
            return render_template("table_locked.html", cafe=CAFE, table=tb), 403

        # OK — update session table
        session["table_id"] = tb.id
        session["table_token"] = tb.token

    tb = db.session.get(Table, session.get("table_id", 0))
    if not tb:
        return "Please scan the QR code on your table.", 400

    # Force-logout check
    if c and session.get("ckey") and c.session_key and session["ckey"] != c.session_key:
        session.pop("cid", None)
        session.pop("ckey", None)
        c = None

    return render_template("menu.html", cafe=CAFE, table=tb, customer=c)


@app.post("/join")
def join():
    name = request.form.get("name", "").strip()[:80]
    phone = "".join(ch for ch in request.form.get("phone", "") if ch.isdigit() or ch == "+")
    if not name or len(phone) < 7:
        return "Enter a valid name and phone.", 400

    c = Customer.query.filter_by(phone=phone).first()
    if not c:
        c = Customer(name=name, phone=phone)
    elif c.name != name and name:
        c.name = name

    if c.disabled:
        return "Account disabled.", 403

    if not c.session_key:
        c.session_key = secrets.token_urlsafe(8)
    if not c.device_token:
        c.device_token = secrets.token_urlsafe(32)

    # Lock the table to this customer if not already locked to someone else
    tb = db.session.get(Table, session.get("table_id", 0))
    if tb:
        if tb.locked_to_customer_id and tb.locked_to_customer_id != c.id:
            return render_template("table_locked.html", cafe=CAFE, table=tb), 403
        tb.locked_to_customer_id = c.id

    db.session.add(c)
    db.session.commit()

    session["cid"] = c.id
    session["ckey"] = c.session_key

    # Set long-lived device token cookie
    resp = redirect("/menu")
    resp.set_cookie(
        "device_token", c.device_token,
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="Lax",
        secure=IS_PROD,
    )
    return resp


@app.get("/logout-customer")
def logout_customer():
    # If this customer had locked a table but has no open orders, unlock it
    tb = db.session.get(Table, session.get("table_id", 0))
    if tb and tb.locked_to_customer_id == session.get("cid"):
        still_open = Order.query.filter(
            Order.table_id == tb.id,
            Order.status.in_(["new", "accepted", "preparing", "ready", "served"]),
        ).count()
        if still_open == 0:
            tb.locked_to_customer_id = None
            db.session.commit()

    session.pop("cid", None)
    session.pop("ckey", None)
    session.pop("table_id", None)
    session.pop("table_token", None)

    resp = redirect("/menu")
    resp.set_cookie("device_token", "", expires=0)   # clear device cookie too
    return resp


# ============================================================
#   CUSTOMER API
# ============================================================

@app.get("/api/menu")
def api_menu():
    import extras
    cats = [{"id": c.id, "name": c.name, "items": [
        {"id": i.id, "name": i.name, "desc": i.description, "price": float(i.price),
         "img": i.image_url, "veg": i.veg, "available": i.available, "featured": i.featured,
         **extras.opts(i)}
        for i in Item.query.filter_by(category_id=c.id)
    ]} for c in Category.query.order_by(Category.sort)]
    c = db.session.get(Customer, session.get("cid", 0))
    return jsonify(cats=cats, loyalty=loyalty(c) if c else None,
                   loyalty_cfg={k: setting(k) for k in LOYALTY_DEFAULTS})


@app.post("/api/order")
def place_order():
    import extras
    c = db.session.get(Customer, session.get("cid", 0))
    tb = db.session.get(Table, session.get("table_id", 0))
    if not c or not tb or not tb.active or c.disabled:
        abort(400)
    data = request.get_json(force=True)
    try:
        lines, sub, bycat = extras.build_lines(data.get("items", []))
    except ValueError as e:
        return jsonify(error=str(e)), 409
    promo, disc = extras.best_promo(sub, bycat)
    used = False
    if data.get("use_reward") and loyalty(c)["reward"] and sub >= setting("min_order"):
        disc += min((sub - disc) * int(setting("percent")) / 100, setting("max_discount"))
        used = True
    disc = min(disc, sub)
    o = Order(customer_id=c.id, table_id=tb.id, subtotal=sub, discount=disc,
              total=sub - disc, reward_used=used, promo_name=promo, items=lines)
    db.session.add(o)
    db.session.flush()
    db.session.add(PrintJob(order_id=o.id))
    db.session.add(extras.Payment(order_id=o.id, method="cash", amount=o.total))
    tb.status = "occupied"
    tb.locked_to_customer_id = c.id    # enforce lock
    db.session.commit()
    return jsonify(id=o.id, number=o.number, total=float(o.total))


@app.post("/api/order/<int:oid>/add")
def add_to_order(oid):
    import extras
    o = db.session.get(Order, oid)
    c = db.session.get(Customer, session.get("cid", 0))
    if not o or not c or o.customer_id != c.id:
        abort(404)
    if o.status not in ("new", "accepted"):
        return jsonify(error="Kitchen has already started — place a new order."), 409
    data = request.get_json(force=True)
    try:
        new_lines, _, _ = extras.build_lines(data.get("items", []))
    except ValueError as e:
        return jsonify(error=str(e)), 409
    for line in new_lines:
        db.session.add(OrderItem(order_id=o.id, name=line.name,
                                 price=line.price, qty=line.qty))
    db.session.flush()
    all_items = OrderItem.query.filter_by(order_id=o.id).all()
    o.subtotal = float(sum(i.price * i.qty for i in all_items))
    promo, disc = extras.best_promo(float(o.subtotal), {})
    if data.get("use_reward") and loyalty(c)["reward"] and o.subtotal >= setting("min_order"):
        disc += min(
            (o.subtotal - disc) * int(setting("percent")) / 100,
            setting("max_discount")
        )
        o.reward_used = True
    o.discount = min(disc, float(o.subtotal))
    o.total = float(o.subtotal) - float(o.discount)
    o.promo_name = promo
    db.session.commit()
    return jsonify(ok=True, total=float(o.total), number=o.number)


@app.get("/api/order/<int:oid>")
def order_status(oid):
    o = db.session.get(Order, oid)
    if not o or o.customer_id != session.get("cid"):
        abort(404)
    return jsonify(status=o.status, message=MSG[o.status], payment_status=o.payment_status)


@app.get("/api/my-orders")
def my_orders():
    c = db.session.get(Customer, session.get("cid", 0))
    tb = db.session.get(Table, session.get("table_id", 0))
    if not c or not tb:
        return jsonify(orders=[])
    rows = Order.query.filter(
        Order.customer_id == c.id,
        Order.table_id == tb.id,
    ).order_by(Order.id.desc()).limit(10).all()
    return jsonify(orders=[{
        "id": o.id,
        "number": o.number,
        "status": o.status,
        "paid": o.payment_status == "paid",
        "total": float(o.total),
        "items": [{"name": i.name, "qty": i.qty, "price": float(i.price)} for i in o.items],
        "created": o.created.strftime("%I:%M %p"),
    } for o in rows])


# ============================================================
#   CUSTOMER PROFILE
# ============================================================

@app.get("/profile")
def profile():
    c = resolve_customer()
    if not c:
        return redirect("/menu")

    orders = Order.query.filter_by(customer_id=c.id) \
                        .order_by(Order.id.desc()).all()

    OPEN = ["new", "accepted", "preparing", "ready", "served"]
    open_orders = [o for o in orders if o.status in OPEN]
    past_orders = [o for o in orders if o.status not in OPEN]

    completed = [o for o in orders if o.status == "completed"]
    total_spent = float(sum(o.total for o in completed))

    loy = loyalty(c)
    required = loy["required"] or 1
    completed_count = loy["completed"]
    tokens_earned = completed_count // required
    tokens_used = Order.query.filter(
        Order.customer_id == c.id,
        Order.reward_used == True,
        Order.status != "cancelled"
    ).count()
    tokens_available = max(0, tokens_earned - tokens_used)
    progress_in_cycle = completed_count % required

    fav_rows = (db.session.query(OrderItem.name, db.func.sum(OrderItem.qty))
                .join(Order, Order.id == OrderItem.order_id)
                .filter(Order.customer_id == c.id, Order.status == "completed")
                .group_by(OrderItem.name)
                .order_by(db.func.sum(OrderItem.qty).desc())
                .limit(5).all())
    favorites = [{"name": n, "qty": int(q)} for n, q in fav_rows]

    return render_template("profile.html",
                           cafe=CAFE,
                           table=db.session.get(Table, session.get("table_id", 0)),
                           customer=c,
                           open_orders=open_orders,
                           past_orders=past_orders,
                           total_spent=total_spent,
                           total_orders=len(orders),
                           completed_count=completed_count,
                           required=required,
                           progress_in_cycle=progress_in_cycle,
                           tokens_earned=tokens_earned,
                           tokens_used=tokens_used,
                           tokens_available=tokens_available,
                           favorites=favorites)


# ============================================================
#   ADMIN AUTH
# ============================================================

def need(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "aid" not in session:
                return redirect("/admin/login")
            if roles and session["role"] not in roles and session["role"] != "superadmin":
                abort(403)
            return f(*a, **k)
        return w
    return deco


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        a = Admin.query.filter_by(username=request.form["username"]).first()
        if a and check_password_hash(a.pw_hash, request.form["password"]):
            session.clear()
            session.update(aid=a.id, role=a.role)
            session.permanent = True
            return redirect("/admin")
        return render_template("login.html", cafe=CAFE, err="Wrong credentials")
    return render_template("login.html", cafe=CAFE, err="")


@app.get("/admin/logout")
def logout():
    session.clear()
    return redirect("/admin/login")


@app.get("/admin")
@need()
def admin():
    start = datetime.combine(now().date(), datetime.min.time())
    today = Order.query.filter(Order.created >= start)
    stats = {
        "orders": today.count(),
        "sales": float(sum(o.total for o in today if o.status != "cancelled")),
        "pending_orders": today.filter(
            Order.status.in_(["new", "accepted", "preparing"])
        ).count(),
        "pending_payments": today.filter(
            Order.payment_status == "unpaid",
            Order.status != "cancelled",
        ).count(),
        "total_customers": Customer.query.count(),
        "new_today": Customer.query.filter(Customer.created >= start).count(),
    }
    return render_template(
        "admin.html",
        cafe=CAFE,
        role=session["role"],
        statuses=STATUSES,
        stats=stats,
        tables=Table.query.all(),
        cats=Category.query.all(),
        items=Item.query.order_by(Item.category_id, Item.name).all(),
        customers=Customer.query.order_by(Customer.id.desc()).limit(200).all(),
        cfg={k: setting(k) for k in LOYALTY_DEFAULTS},
        failed=PrintJob.query.filter_by(status="failed").count(),
    )


# ============================================================
#   ADMIN API — ORDERS
# ============================================================

@app.get("/admin/api/orders")
@need()
def admin_orders():
    q = Order.query.order_by(Order.id.desc())
    if request.args.get("q"):
        s = request.args["q"]
        q = q.join(Customer).filter(
            db.or_(Customer.name.ilike(f"%{s}%"), Customer.phone.like(f"%{s}%"))
        )
    return jsonify([serialize_order(o) for o in q.limit(60)])


@app.get("/admin/api/orders/stream")
@need()
def admin_orders_stream():
    import time as _time

    def event_stream():
        last_sig = ""
        while True:
            try:
                orders = Order.query.order_by(Order.id.desc()).limit(30).all()
                sig = "|".join(f"{o.id}:{o.status}:{o.payment_status}" for o in orders)
                if sig != last_sig:
                    last_sig = sig
                    payload = _json.dumps([serialize_order(o) for o in orders])
                    yield f"data: {payload}\n\n"
                else:
                    yield ": keepalive\n\n"
            except Exception as e:
                yield f"event: error\ndata: {str(e)}\n\n"
            _time.sleep(3)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/admin/order/<int:oid>/status")
@need()
def set_status(oid):
    s = request.json["status"]
    if s not in STATUSES:
        abort(400)

    o = db.session.get(Order, oid)
    o.status = s

    if s in ("completed", "cancelled"):
        free_table_if_done(o.table_id)

    db.session.commit()
    return jsonify(ok=True)


@app.post("/admin/order/<int:oid>/reprint")
@need("manager")
def reprint(oid):
    db.session.add(PrintJob(order_id=oid))
    db.session.commit()
    return jsonify(ok=True)


@app.get("/admin/order/<int:oid>/print")
@need()
def print_bill(oid):
    o = db.session.get(Order, oid)
    if not o:
        abort(404)
    return render_template("bill_print.html", o=o, cafe=CAFE,
                           printed_at=now().strftime("%d %b %Y · %I:%M %p"))


# ============================================================
#   ADMIN TABLE MANAGEMENT
# ============================================================

@app.post("/admin/table")
@need("manager")
def add_table():
    db.session.add(Table(name=request.form["name"]))
    db.session.commit()
    return redirect("/admin#tables")


@app.post("/admin/table/<int:tid>/regen")
@need("manager")
def regen(tid):
    t = db.session.get(Table, tid)
    t.token = secrets.token_urlsafe(8)
    db.session.commit()
    return redirect("/admin#tables")


@app.post("/admin/table/<int:tid>/unlock")
@need("manager")
def unlock_table(tid):
    """Manually unlock a table (e.g. if the customer's phone died)."""
    t = db.session.get(Table, tid)
    if t:
        t.locked_to_customer_id = None
        t.status = "available"
        t.token = secrets.token_urlsafe(8)
        db.session.commit()
    return redirect(f"/admin/table/{tid}")


@app.get("/admin/qr/<int:tid>.png")
@need("manager")
def qr_png(tid):
    t = db.session.get(Table, tid)
    buf = io.BytesIO()
    qrcode.make(f"{BASE_URL}/menu?t={t.token}", box_size=10).save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"table-{t.name}.png")


@app.get("/admin/qr-sheet")
@need("manager")
def qr_sheet():
    return render_template("qrsheet.html", cafe=CAFE, tables=Table.query.filter_by(active=True))


# ============================================================
#   ADMIN ITEM / LOYALTY
# ============================================================

@app.post("/admin/item/<int:iid>/toggle")
@need("manager")
def toggle(iid):
    i = db.session.get(Item, iid)
    i.available = not i.available
    db.session.commit()
    return redirect("/admin#menu")


@app.post("/admin/loyalty")
@need("manager")
def save_loyalty():
    for k in LOYALTY_DEFAULTS:
        db.session.merge(Setting(key=k, value=request.form[k]))
    db.session.commit()
    return redirect("/admin#loyalty")


# ============================================================
#   PRINT QUEUE (bridge API)
# ============================================================

def bridge_auth():
    if not secrets.compare_digest(request.headers.get("X-Print-Key", ""), PRINT_KEY):
        abort(401)


@app.get("/api/print/next")
def print_next():
    bridge_auth()
    stale = now() - timedelta(seconds=60)
    PrintJob.query.filter(
        PrintJob.status == "printing", PrintJob.updated < stale
    ).update({"status": "pending"})
    j = PrintJob.query.filter_by(status="pending").order_by(PrintJob.id).first()
    if not j:
        return jsonify(job=None)
    j.status, j.attempts, j.updated = "printing", j.attempts + 1, now()
    db.session.commit()
    return jsonify(job={"id": j.id, "text": ticket(db.session.get(Order, j.order_id))})


@app.post("/api/print/<int:jid>/<result>")
def print_result(jid, result):
    bridge_auth()
    j = db.session.get(PrintJob, jid)
    if result == "done":
        j.status = "done"
    else:
        j.status = "failed" if j.attempts >= 5 else "pending"
        j.error = (request.get_json(silent=True) or {}).get("error")
    j.updated = now()
    db.session.commit()
    return jsonify(ok=True)


# ============================================================
#   SEED
# ============================================================

def seed():
    db.create_all()
    if not Admin.query.first():
        db.session.add(Admin(
            username="admin",
            pw_hash=generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin123")),
            role="superadmin",
        ))
        db.session.add_all([Table(name=str(n)) for n in range(1, 6)])
        k = Category(name="Coffee", sort=1)
        db.session.add(k)
        db.session.flush()
        db.session.add(Item(category_id=k.id, name="Cold Coffee",
                            description="Iced, creamy, smooth.", price=200, featured=True))
        db.session.commit()