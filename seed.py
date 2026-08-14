"""Seed the database with an admin/staff account and sample restaurant data."""
from werkzeug.security import generate_password_hash

from app import create_app, db
from app.models import (
    User,
    Category,
    Product,
    BuffetTier,
    RestaurantTable,
    InventoryItem,
    Supplier,
    Setting,
)

app = create_app()

with app.app_context():
    db.create_all()

    if not User.query.filter_by(username="admin").first():
        db.session.add(
            User(
                username="admin",
                name="Restaurant Administrator",
                role="admin",
                password_hash=generate_password_hash("admin123"),
            )
        )
    if not User.query.filter_by(username="staff").first():
        db.session.add(
            User(
                username="staff",
                name="Front Desk Cashier",
                role="staff",
                password_hash=generate_password_hash("staff123"),
            )
        )
    db.session.commit()

    if not Category.query.first():
        cat_buffet = Category(name="Buffet")
        db.session.add(cat_buffet)
        db.session.commit()

        supplier = Supplier(name="Green Valley Meat & Produce", contact="0917-000-0000")
        db.session.add(supplier)
        db.session.commit()

        rice = InventoryItem(name="Rice", quantity=100, unit="kg", low_stock_threshold=15, supplier_id=supplier.id)
        db.session.add(rice)
        db.session.commit()

        buffet = Product(name="Unlimited Buffet", category_id=cat_buffet.id, is_buffet=True, inventory_item_id=rice.id, deduct_qty=0.3)

        db.session.add(buffet)
        db.session.commit()

        db.session.add_all(
            [
                BuffetTier(product_id=buffet.id, tier="adult", price=600),
                BuffetTier(product_id=buffet.id, tier="senior", price=480),
                BuffetTier(product_id=buffet.id, tier="pwd", price=480),
                BuffetTier(product_id=buffet.id, tier="kids", price=300),
                BuffetTier(product_id=buffet.id, tier="free", price=0),
            ]
        )

    if not RestaurantTable.query.first():
        for i in range(1, 9):
            db.session.add(RestaurantTable(name=f"T{i}", capacity=4 if i % 2 else 6))

    if not Setting.query.filter_by(key="restaurant_name").first():
        db.session.add(Setting(key="restaurant_name", value="Sitio Verde Buffet Restaurant"))
        db.session.add(Setting(key="vat_rate", value="12"))
        db.session.add(Setting(key="receipt_footer", value="Thank you for dining with us!"))

    db.session.commit()
    print("Seed complete. Login with admin/admin123 or staff/staff123")
