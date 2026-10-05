"""Extra features: promos, variants/add-ons, uploads, reports, table/customer detail,
audit log, CSRF, rate limits, payments, payment-QR generator."""
import os, time, uuid, secrets, io
from collections import deque
from datetime import datetime, date, timedelta, time as dtime
from flask import request, session, redirect, jsonify, render_template, send_file, abort
from app import (app, db, CAFE, STATUSES, need, Order, OrderItem, Item, Category,
                 Customer, Table, Setting, Admin, loyalty, LoyaltyTx, now,
                 free_table_if_done, PrintJob)
from payments import PROVIDERS
import qrcode

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
UP = os.path.join(app.static_folder, "uploads")


class Discount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80))
    kind = db.Column(db.String(10))
    value = db.Column(db.Numeric(10, 2))
    min_order = db.Column(db.Numeric(10, 2), default=0)
    max_discount = db.Column(db.Numeric(10, 2))
    start = db.Column(db.DateTime)
    end = db.Column(db.DateTime)
    category_id = db.Column(db.ForeignKey("category.id"))
    active = db.Column(db.Boolean, default=True)


class Variant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.ForeignKey("item.id"), index=True)
    name = db.Column(db.String(60))
    delta = db.Column(db.Numeric(10, 2), default=0)


class Addon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.ForeignKey("item.id"), index=True)
    name = db.Column(db.String(60))
    price = db.Column(db.Numeric(10, 2), default=0)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.ForeignKey("order.id"), index=True)
    method = db.Column(db.String(20))
    amount = db.Column(db.Numeric(10, 2))
    status = db.Column(db.String(10), default="pending")
    reference = db.Column(db.String(100))
    created = db.Column(db.DateTime, default=now)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer)
    action = db.Column(db.String(200))
    ip = db.Column(db.String(45))
    created = db.Column(db.DateTime, default=now, index=True)


@app.context_processor
def _ctx():
    def csrf_token():
        session.setdefault("csrf", secrets.token_urlsafe(24))
        return session["csrf"]
    return {"csrf_token": csrf_token}


_hits = {}
LIMITS = {"/admin/login": (10, 300), "/join": (20, 60), "/api/order": (20, 60)}


@app.before_request
def guard():
    session.setdefault("csrf", secrets.token_urlsafe(24))
    p = request.path
    if p.startswith("/api/print/") or p.startswith("/api/payments/") or request.method != "POST":
        return
    if p in LIMITS:
        n, win = LIMITS[p]
        q = _hits.setdefault((p, request.remote_addr), deque())
        t = time.time()
        while q and q[0] < t - win:
            q.popleft()
        if len(q) >= n:
            return "Too many requests — slow down.", 429
        q.append(t)
    tok = session.get("csrf", "")
    sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
    if not tok or not secrets.compare_digest(tok, sent):
        abort(400, "CSRF check failed")


@app.after_request
def audit(resp):
    if request.method == "POST" and request.path.startswith("/admin") \
            and session.get("aid") and resp.status_code < 400:
        db.session.add(AuditLog(admin_id=session["aid"], action=request.path, ip=request.remote_addr))
        db.session.commit()
    return resp


def opts(i):
    return {
        "variants": [{"id": v.id, "name": v.name, "delta": float(v.delta)}
                     for v in Variant.query.filter_by(item_id=i.id)],
        "addons": [{"id": a.id, "name": a.name, "price": float(a.price)}
                   for a in Addon.query.filter_by(item_id=i.id)],
    }


def build_lines(rows):
    lines, sub, bycat = [], 0.0, {}
    for r in rows:
        it = db.session.get(Item, int(r["id"]))
        q = max(1, min(int(r["qty"]), 50))
        if not it or not it.available:
            raise ValueError(f"'{it.name if it else 'Item'}' is sold out")
        price, label = float(it.price), it.name
        v = db.session.get(Variant, int(r["vid"])) if r.get("vid") else None
        if v and v.item_id == it.id:
            price += float(v.delta)
            label += f" ({v.name})"
        for a in Addon.query.filter(Addon.item_id == it.id,
                                    Addon.id.in_([int(x) for x in r.get("aids") or []])):
            price += float(a.price)
            label += f" +{a.name}"
        lines.append(OrderItem(name=label[:100], price=price, qty=q))
        sub += price * q
        bycat[it.category_id] = bycat.get(it.category_id, 0) + price * q
    if not lines:
        raise ValueError("Cart empty")
    return lines, sub, bycat


def best_promo(sub, bycat):
    t = now()
    best = (None, 0.0)
    for d in Discount.query.filter_by(active=True):
        if (d.start and d.start > t) or (d.end and d.end < t) or sub < float(d.min_order or 0):
            continue
        base = bycat.get(d.category_id, 0) if d.category_id else sub
        amt = base * float(d.value) / 100 if d.kind == "percent" else (float(d.value) if base else 0)
        if d.max_discount:
            amt = min(amt, float(d.max_discount))
        if min(amt, sub) > best[1]:
            best = (d.name, min(amt, sub))
    return best


def page(name, **k):
    return render_template("panel.html", page=name, cafe=CAFE, **k)


def day(s, end=False):
    if not s:
        return None
    d = datetime.strptime(s, "%Y-%m-%d")
    return d + timedelta(days=1, seconds=-1) if end else d


@app.post("/admin/item")
@need("manager")
def save_item():
    f = request.form
    img = request.form.get("image_url", "")
    file = request.files.get("image")
    if file and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext not in {"png", "jpg", "jpeg", "webp", "gif"}:
            abort(400, "Unsupported image type")
        os.makedirs(UP, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(UP, name))
        img = f"/static/uploads/{name}"
    c = Category.query.filter_by(name=f["category"]).first() or Category(name=f["category"])
    db.session.add(c)
    db.session.flush()
    db.session.add(Item(category_id=c.id, name=f["name"], description=f.get("description", ""),
                        price=f["price"], image_url=img, veg="veg" in f))
    db.session.commit()
    return redirect("/admin#menu")


@app.get("/admin/item/<int:iid>/options")
@need("manager")
def item_options(iid):
    return page("options", item=db.session.get(Item, iid),
                variants=Variant.query.filter_by(item_id=iid),
                addons=Addon.query.filter_by(item_id=iid))


@app.post("/admin/item/<int:iid>/<kind>")
@need("manager")
def add_option(iid, kind):
    f = request.form
    if kind == "variant":
        db.session.add(Variant(item_id=iid, name=f["name"], delta=f.get("price") or 0))
    elif kind == "addon":
        db.session.add(Addon(item_id=iid, name=f["name"], price=f.get("price") or 0))
    elif kind == "del":
        db.session.delete(db.session.get(Variant if f["t"] == "variant" else Addon, int(f["id"])))
    else:
        abort(404)
    db.session.commit()
    return redirect(f"/admin/item/{iid}/options")


@app.get("/admin/promos")
@need("manager")
def promos():
    return page("promos", discounts=Discount.query.all(), cats=Category.query.all())


@app.post("/admin/promos")
@need("manager")
def add_promo():
    f = request.form
    db.session.add(Discount(
        name=f["name"],
        kind=f["kind"] if f["kind"] in ("percent", "fixed") else "percent",
        value=f["value"],
        min_order=f.get("min_order") or 0,
        max_discount=f.get("max_discount") or None,
        start=day(f.get("start")),
        end=day(f.get("end"), True),
        category_id=int(f["category_id"]) if f.get("category_id") else None,
    ))
    db.session.commit()
    return redirect("/admin/promos")


@app.post("/admin/promos/<int:did>/toggle")
@need("manager")
def toggle_promo(did):
    d = db.session.get(Discount, did)
    d.active = not d.active
    db.session.commit()
    return redirect("/admin/promos")


@app.get("/admin/reports")
@need("manager")
def reports():
    return page("reports")


@app.get("/admin/api/reports")
@need("manager")
def reports_api():
    today = now().date()
    ok = Order.query.filter(Order.status != "cancelled").all()
    s = lambda f: round(sum(float(o.total) for o in ok if f(o.created.date())), 2)
    days = [today - timedelta(d) for d in range(13, -1, -1)]
    top = (db.session.query(OrderItem.name, db.func.sum(OrderItem.qty))
           .join(Order, Order.id == OrderItem.order_id)
           .filter(Order.status != "cancelled")
           .group_by(OrderItem.name)
           .order_by(db.func.sum(OrderItem.qty).desc())
           .limit(8).all())
    spend = {}
    for o in ok:
        if o.status == "completed":
            spend[o.customer.name] = spend.get(o.customer.name, 0) + float(o.total)
    allo = Order.query.all()
    return jsonify(
        labels=[d.strftime("%d %b") for d in days],
        sales=[s(lambda x, d=d: x == d) for d in days],
        top=[[n, int(q)] for n, q in top],
        week=s(lambda x: x > today - timedelta(7)),
        month=s(lambda x: (x.year, x.month) == (today.year, today.month)),
        year=s(lambda x: x.year == today.year),
        avg=round(sum(float(o.total) for o in ok) / len(ok), 2) if ok else 0,
        status={k: sum(1 for o in allo if o.status == k) for k in STATUSES},
        loyal=sorted(spend.items(), key=lambda kv: -kv[1])[:5],
        new_customers=Customer.query.filter(
            Customer.created >= datetime.combine(today, dtime.min)).count(),
        total_customers=Customer.query.count(),
    )


LIVE = ["new", "accepted", "preparing", "ready", "served"]


@app.get("/admin/tables")
@need("manager")
def tables_status():
    rows = []
    for t in Table.query.all():
        cur = Order.query.filter(Order.table_id == t.id, Order.status.in_(LIVE)) \
                         .order_by(Order.id.desc()).first()
        rows.append((t, "occupied" if cur else ("disabled" if not t.active else t.status), cur))
    return page("tables", rows=rows)


@app.get("/admin/table/<int:tid>")
@need("manager")
def table_detail(tid):
    return page("table", t=db.session.get(Table, tid),
                orders=Order.query.filter_by(table_id=tid).order_by(Order.id.desc()).limit(50),
                live=LIVE)


@app.post("/admin/table/<int:tid>/status")
@need("manager")
def table_status(tid):
    t, s = db.session.get(Table, tid), request.form["status"]
    if s not in ("available", "reserved", "cleaning", "disabled"):
        abort(400)
    t.status, t.active = s, s != "disabled"
    db.session.commit()
    return redirect(f"/admin/table/{tid}")


# ==========================================================
#   CUSTOMER MANAGEMENT
# ==========================================================

@app.get("/admin/customers")
@need("manager")
def customers_list():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")

    query = Customer.query
    if q:
        query = query.filter(db.or_(
            Customer.name.ilike(f"%{q}%"),
            Customer.phone.like(f"%{q}%"),
        ))
    if status == "disabled":
        query = query.filter(Customer.disabled == True)
    elif status == "active":
        query = query.filter(Customer.disabled == False)

    customers = query.order_by(Customer.id.desc()).limit(500).all()

    rows = []
    for c in customers:
        orders = Order.query.filter_by(customer_id=c.id).all()
        total_spent = sum(float(o.total) for o in orders if o.status == "completed")
        last_order = max((o.created for o in orders), default=None)
        if status == "with_orders" and not orders:
            continue
        if status == "no_orders" and orders:
            continue
        rows.append({
            "id": c.id,
            "name": c.name,
            "phone": c.phone,
            "disabled": c.disabled,
            "created": c.created,
            "orders": len(orders),
            "spent": total_spent,
            "last_order": last_order,
        })

    return page("customers", rows=rows, q=q, status=status)


@app.get("/admin/customer/<int:cid>")
@need("manager")
def customer_detail(cid):
    c = db.session.get(Customer, cid)
    if not c:
        abort(404)
    orders = Order.query.filter_by(customer_id=cid).order_by(Order.id.desc()).all()
    return page("customer", c=c, orders=orders, loy=loyalty(c),
                spent=sum(float(o.total) for o in orders if o.status == "completed"),
                redeemed=sum(1 for o in orders if o.reward_used and o.status != "cancelled"),
                txs=LoyaltyTx.query.filter_by(customer_id=cid))


@app.post("/admin/customer/<int:cid>/<act>")
@need("manager")
def customer_act(cid, act):
    c = db.session.get(Customer, cid)
    if not c:
        abort(404)
    if act == "toggle":
        c.disabled = not c.disabled
    elif act == "adjust":
        db.session.add(LoyaltyTx(customer_id=cid, delta=int(request.form["delta"]),
                                 note=request.form.get("note", "")[:200]))
    else:
        abort(404)
    db.session.commit()
    return redirect(f"/admin/customer/{cid}")


@app.post("/admin/customer/<int:cid>/delete")
@need("superadmin")
def customer_delete(cid):
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify(error="Customer not found"), 404

    orders = Order.query.filter_by(customer_id=cid).all()
    for o in orders:
        OrderItem.query.filter_by(order_id=o.id).delete()
        PrintJob.query.filter_by(order_id=o.id).delete()
        Payment.query.filter_by(order_id=o.id).delete()
        db.session.delete(o)

    LoyaltyTx.query.filter_by(customer_id=cid).delete()
    db.session.delete(c)
    db.session.commit()
    return jsonify(ok=True, deleted_id=cid, orders_deleted=len(orders))


@app.post("/admin/customer/<int:cid>/force-logout")
@need("manager")
def customer_force_logout(cid):
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify(error="Customer not found"), 404
    c.session_key = secrets.token_urlsafe(8)
    db.session.commit()
    return jsonify(ok=True, new_key=c.session_key)


@app.post("/admin/customer/<int:cid>/merge-into/<int:target_id>")
@need("superadmin")
def customer_merge(cid, target_id):
    if cid == target_id:
        return jsonify(error="Cannot merge into self"), 400
    src = db.session.get(Customer, cid)
    tgt = db.session.get(Customer, target_id)
    if not src or not tgt:
        return jsonify(error="Customer not found"), 404

    Order.query.filter_by(customer_id=cid).update({"customer_id": target_id})
    LoyaltyTx.query.filter_by(customer_id=cid).update({"customer_id": target_id})
    db.session.delete(src)
    db.session.commit()
    return jsonify(ok=True, target_id=target_id)


@app.post("/admin/customers/bulk-delete-inactive")
@need("superadmin")
def bulk_delete_inactive():
    days = int(request.form.get("days", 90))
    cutoff = now() - timedelta(days=days)
    deleted = 0
    for c in Customer.query.all():
        last = db.session.query(db.func.max(Order.created)) \
            .filter(Order.customer_id == c.id).scalar()
        if last is None or last < cutoff:
            for o in Order.query.filter_by(customer_id=c.id).all():
                OrderItem.query.filter_by(order_id=o.id).delete()
                PrintJob.query.filter_by(order_id=o.id).delete()
                Payment.query.filter_by(order_id=o.id).delete()
                db.session.delete(o)
            LoyaltyTx.query.filter_by(customer_id=c.id).delete()
            db.session.delete(c)
            deleted += 1
    db.session.commit()
    return redirect("/admin/customers?deleted=" + str(deleted))


@app.get("/admin/audit")
@need()
def audit_page():
    if session["role"] != "superadmin":
        abort(403)
    names = {a.id: a.username for a in Admin.query}
    return page("audit", logs=AuditLog.query.order_by(AuditLog.id.desc()).limit(200), names=names)


# ==========================================================
#   PAYMENT QR
# ==========================================================

@app.get("/admin/pay-qr")
@need("manager")
def pay_qr_page():
    return page("payqr")


@app.get("/admin/pay-qr/<int:amount>.png")
@need("manager")
def pay_qr_png(amount):
    note = request.args.get("note", "")[:80]
    payload = f"{CAFE}\nAmount: Rs. {amount}\n{note}\nPay at counter"
    buf = io.BytesIO()
    qrcode.make(payload, box_size=12).save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png",
                     download_name=f"pay-{amount}.png")


# ==========================================================
#   MARK PAID — completes order + frees table
# ==========================================================

@app.post("/admin/order/<int:oid>/pay")
@need()
def mark_paid(oid):
    o = db.session.get(Order, oid)
    if not o:
        abort(404)

    o.payment_status = "paid"
    if o.status not in ("completed", "cancelled"):
        o.status = "completed"

    p = Payment.query.filter_by(order_id=oid).first() or Payment(order_id=oid, method="cash", amount=o.total)
    p.status = "paid"
    db.session.add(p)

    t = free_table_if_done(o.table_id)
    rotated = t is not None

    db.session.commit()
    return jsonify(ok=True, rotated=rotated,
                   new_token=t.token if t else None)


@app.post("/api/payments/<name>/callback")
def pay_callback(name):
    s = db.session.get(Setting, "online_payment")
    if not (s and s.value == "1") or name not in PROVIDERS:
        abort(403)
    try:
        oid, ref = PROVIDERS[name].verify(request.get_json(silent=True) or request.form)
    except NotImplementedError:
        return jsonify(error="provider not implemented"), 501
    o = db.session.get(Order, oid)
    o.payment_status = "paid"
    if o.status not in ("completed", "cancelled"):
        o.status = "completed"
    db.session.add(Payment(order_id=oid, method=name, amount=o.total, status="paid", reference=ref))
    free_table_if_done(o.table_id)
    db.session.commit()
    return jsonify(ok=True)


# ==========================================================
#   TEMPORARY: seed trigger (remove after use)
# ==========================================================

@app.get("/admin/seed/<secret>")
@need("manager")
def trigger_seed(secret):
    if secret != "please-seed-now-2025":
        abort(404)
    try:
        import seed_data
        seed_data.run()
        from app import Category, Item
        return (
            "<h2>Seed complete</h2>"
            f"<p>Categories: {Category.query.count()}</p>"
            f"<p>Items: {Item.query.count()}</p>"
            "<p><a href='/admin'>Back to dashboard</a> · "
            "<a href='/admin/qr-sheet'>QR sheet</a></p>"
        )
    except Exception:
        import traceback
        return f"<pre>{traceback.format_exc()}</pre>", 500


# ==========================================================
#   TEMPORARY: cleanup paid-but-open orders + free tables
# ==========================================================

@app.get("/admin/cleanup/close-paid-orders")
@need("manager")
def cleanup_paid_orders():
    fixed = 0
    for o in Order.query.filter(
        Order.payment_status == "paid",
        Order.status.in_(["new", "accepted", "preparing", "ready", "served"])
    ):
        o.status = "completed"
        fixed += 1
    db.session.commit()

    freed = 0
    for t in Table.query.all():
        live = Order.query.filter(
            Order.table_id == t.id,
            Order.status.in_(["new", "accepted", "preparing", "ready", "served"])
        ).count()
        if live == 0 and t.status != "available":
            t.status = "available"
            freed += 1
    db.session.commit()
    return f"Completed {fixed} paid orders; freed {freed} tables."