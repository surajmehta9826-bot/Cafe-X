"""Populate the database with demo data. Run once:  python seed_data.py"""
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash

from dotenv import load_dotenv
load_dotenv()

from app import app, db, Admin, Table, Category, Item, Customer, Order, OrderItem, PrintJob, Setting
import extras  # noqa: F401
from extras import Discount, Variant, Addon, Payment


def run():
    with app.app_context():
        db.create_all()

        if not Admin.query.filter_by(username="manager").first():
            db.session.add(Admin(username="manager",
                                 pw_hash=generate_password_hash("manager123"),
                                 role="manager"))
        if not Admin.query.filter_by(username="staff1").first():
            db.session.add(Admin(username="staff1",
                                 pw_hash=generate_password_hash("staff123"),
                                 role="staff"))

        cats = {}
        for name, sort in [("☕ Coffee", 1), ("🍵 Tea", 2), ("🥤 Cold Drinks", 3),
                           ("🍕 Snacks", 4), ("🍰 Desserts", 5), ("🍽️ Main Course", 6)]:
            c = Category.query.filter_by(name=name).first() or Category(name=name, sort=sort)
            db.session.add(c)
            db.session.flush()
            cats[name] = c

        items_data = [
            ("☕ Coffee", "Espresso", "Strong single shot of pure coffee.", 120, True, False),
            ("☕ Coffee", "Cappuccino", "Espresso with steamed milk foam.", 180, True, True),
            ("☕ Coffee", "Latte", "Smooth espresso with lots of milk.", 200, True, False),
            ("☕ Coffee", "Americano", "Espresso diluted with hot water.", 150, True, False),
            ("☕ Coffee", "Mocha", "Chocolate + espresso + milk.", 220, True, True),
            ("🍵 Tea", "Masala Chai", "Spiced Indian tea with milk.", 80, True, True),
            ("🍵 Tea", "Green Tea", "Fresh brewed organic green tea.", 90, True, False),
            ("🍵 Tea", "Lemon Tea", "Black tea with fresh lemon.", 90, True, False),
            ("🥤 Cold Drinks", "Iced Latte", "Chilled latte over ice.", 210, True, True),
            ("🥤 Cold Drinks", "Cold Coffee", "Iced, creamy, smooth.", 200, True, True),
            ("🥤 Cold Drinks", "Lemon Soda", "Fresh lemon soda with mint.", 120, True, False),
            ("🥤 Cold Drinks", "Mango Smoothie", "Fresh mango blended with yogurt.", 250, True, False),
            ("🍕 Snacks", "Veg Momo (8pcs)", "Steamed dumplings with chutney.", 180, True, True),
            ("🍕 Snacks", "Chicken Momo (8pcs)", "Steamed chicken dumplings.", 220, False, False),
            ("🍕 Snacks", "French Fries", "Crispy golden fries with dip.", 150, True, False),
            ("🍕 Snacks", "Chicken Wings", "Spicy grilled wings (6 pcs).", 320, False, False),
            ("🍕 Snacks", "Veg Sandwich", "Grilled sandwich with veggies & cheese.", 180, True, False),
            ("🍰 Desserts", "Chocolate Brownie", "Warm brownie with fudge sauce.", 220, True, True),
            ("🍰 Desserts", "Cheesecake Slice", "Classic New York style.", 260, True, False),
            ("🍰 Desserts", "Ice Cream (2 scoop)", "Choose any two flavours.", 180, True, False),
            ("🍽️ Main Course", "Chicken Biryani", "Aromatic basmati rice with chicken.", 420, False, True),
            ("🍽️ Main Course", "Veg Thali", "Rice, dal, sabzi, roti, salad.", 350, True, False),
            ("🍽️ Main Course", "Pasta Alfredo", "Creamy white sauce pasta.", 320, True, False),
            ("🍽️ Main Course", "Chicken Burger", "Grilled chicken patty burger with fries.", 340, False, False),
        ]
        for cat_name, name, desc, price, veg, feat in items_data:
            if not Item.query.filter_by(name=name).first():
                db.session.add(Item(category_id=cats[cat_name].id, name=name, description=desc,
                                    price=price, veg=veg, featured=feat))
        db.session.commit()

        for name in ["6", "7", "8", "Patio-1", "Patio-2"]:
            if not Table.query.filter_by(name=name).first():
                db.session.add(Table(name=name))
        db.session.commit()

        def by_name(n):
            return Item.query.filter_by(name=n).first()

        vdata = [("Espresso", "Single", 0), ("Espresso", "Double", 40),
                 ("Cappuccino", "Regular", 0), ("Cappuccino", "Large", 50),
                 ("Latte", "Regular", 0), ("Latte", "Large", 60),
                 ("Cold Coffee", "Regular", 0), ("Cold Coffee", "Large", 50)]
        for iname, vname, delta in vdata:
            it = by_name(iname)
            if it and not Variant.query.filter_by(item_id=it.id, name=vname).first():
                db.session.add(Variant(item_id=it.id, name=vname, delta=delta))

        adata = [("Cappuccino", "Extra Shot", 40), ("Latte", "Extra Shot", 40),
                 ("Latte", "Whipped Cream", 30), ("Latte", "Caramel Syrup", 25),
                 ("Cold Coffee", "Whipped Cream", 30), ("Cold Coffee", "Ice Cream Scoop", 60),
                 ("Chicken Burger", "Extra Cheese", 50), ("Chicken Burger", "Extra Patty", 80)]
        for iname, aname, price in adata:
            it = by_name(iname)
            if it and not Addon.query.filter_by(item_id=it.id, name=aname).first():
                db.session.add(Addon(item_id=it.id, name=aname, price=price))
        db.session.commit()

        cdata = [("Ramesh Sharma", "+9779801234567"), ("Sita Kumari", "+9779812345678"),
                 ("Bikash Thapa", "+9779823456789"), ("Anita Rai", "+9779834567890"),
                 ("Deepak Gurung", "+9779845678901")]
        for n, p in cdata:
            if not Customer.query.filter_by(phone=p).first():
                db.session.add(Customer(name=n, phone=p))
        db.session.commit()

        def cust(p): return Customer.query.filter_by(phone=p).first()
        def tab(n):  return Table.query.filter_by(name=n).first()

        if Order.query.count() == 0:
            o1 = Order(customer_id=cust("+9779801234567").id, table_id=tab("1").id,
                       subtotal=380, discount=0, total=380, payment_status="paid",
                       status="completed", created=datetime.now() - timedelta(days=2))
            db.session.add(o1); db.session.flush()
            for nm, pr, q in [("Cappuccino", 180, 1), ("Masala Chai", 80, 1), ("French Fries", 150, 1)]:
                db.session.add(OrderItem(order_id=o1.id, name=nm, price=pr, qty=q))
            db.session.add(PrintJob(order_id=o1.id, status="done", attempts=1))
            db.session.add(Payment(order_id=o1.id, method="cash", amount=380, status="paid"))

            o2 = Order(customer_id=cust("+9779812345678").id, table_id=tab("2").id,
                       subtotal=620, discount=0, total=620, payment_status="paid",
                       status="completed", created=datetime.now() - timedelta(days=1))
            db.session.add(o2); db.session.flush()
            for nm, pr, q in [("Chicken Biryani", 420, 1), ("Iced Latte", 210, 1)]:
                db.session.add(OrderItem(order_id=o2.id, name=nm, price=pr, qty=q))
            db.session.add(PrintJob(order_id=o2.id, status="done", attempts=1))
            db.session.add(Payment(order_id=o2.id, method="cash", amount=620, status="paid"))

            o3 = Order(customer_id=cust("+9779823456789").id, table_id=tab("3").id,
                       subtotal=740, discount=74, total=666, promo_name="10% Off Snacks",
                       payment_status="paid", status="preparing",
                       created=datetime.now() - timedelta(minutes=10))
            db.session.add(o3); db.session.flush()
            for nm, pr, q in [("Chicken Momo (8pcs)", 220, 2), ("Lemon Soda", 120, 1),
                              ("Chocolate Brownie", 220, 1)]:
                db.session.add(OrderItem(order_id=o3.id, name=nm, price=pr, qty=q))
            db.session.add(PrintJob(order_id=o3.id, status="done", attempts=1))
            db.session.add(Payment(order_id=o3.id, method="cash", amount=666, status="paid"))

            o4 = Order(customer_id=cust("+9779834567890").id, table_id=tab("5").id,
                       subtotal=400, discount=0, total=400, payment_status="unpaid",
                       status="new", created=datetime.now())
            db.session.add(o4); db.session.flush()
            for nm, pr, q in [("Cold Coffee", 200, 2)]:
                db.session.add(OrderItem(order_id=o4.id, name=nm, price=pr, qty=q))
            db.session.add(PrintJob(order_id=o4.id, status="pending"))
            db.session.add(Payment(order_id=o4.id, method="cash", amount=400, status="pending"))

            db.session.commit()

        for k, v in [("orders_required", "10"), ("percent", "50"),
                     ("max_discount", "500"), ("min_order", "500")]:
            if not db.session.get(Setting, k):
                db.session.add(Setting(key=k, value=v))
        db.session.commit()

        if Discount.query.count() == 0:
            now = datetime.now()
            db.session.add(Discount(name="10% Off Snacks", kind="percent", value=10,
                                    min_order=300, max_discount=200,
                                    start=now - timedelta(days=7), end=now + timedelta(days=30),
                                    category_id=cats["🍕 Snacks"].id))
            db.session.add(Discount(name="Rs.50 Off Coffee", kind="fixed", value=50,
                                    min_order=250,
                                    start=now - timedelta(days=7), end=now + timedelta(days=30),
                                    category_id=cats["☕ Coffee"].id))
            db.session.commit()

        print("✅ Seed complete.")
        print("   Login: admin / admin123       (superadmin)")
        print("          manager / manager123   (manager)")
        print("          staff1 / staff123      (staff)")


if __name__ == "__main__":
    run()