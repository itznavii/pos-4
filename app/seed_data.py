"""Idempotent database seeding logic — safe to call on every app startup.
Only creates rows that don't already exist (except buffet tier prices, which
are always kept in sync with PRICE_TABLE below so a code update updates
prices on your live database too, without needing a manual re-seed).
"""
from werkzeug.security import generate_password_hash

from app import db
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

# Buffet price per guest type. Edit here to change pricing; it will apply
# automatically the next time the app starts (no manual DB edit needed).
PRICE_TABLE = {
    "adult": 599.00,
    "senior": 479.20,
    "pwd": 479.20,
    "kids": 449.00,
    "free": 0.00,  # covers both "kids below 3 ft" and "birthday celebrant"
}


def run_seed():
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

        for tier, price in PRICE_TABLE.items():
            db.session.add(BuffetTier(product_id=buffet.id, tier=tier, price=price))

    # Always keep existing buffet tier prices in sync with PRICE_TABLE, even
    # if the product/tiers were created in an earlier deploy.
    for product in Product.query.filter_by(is_buffet=True).all():
        for tier in product.buffet_tiers:
            if tier.tier in PRICE_TABLE and tier.price != PRICE_TABLE[tier.tier]:
                tier.price = PRICE_TABLE[tier.tier]

    if not RestaurantTable.query.first():
        for i in range(1, 9):
            db.session.add(RestaurantTable(name=f"T{i}", capacity=4 if i % 2 else 6))

    if not Setting.query.filter_by(key="restaurant_name").first():
        db.session.add(Setting(key="restaurant_name", value="Sitio Verde Buffet Restaurant"))
        db.session.add(Setting(key="receipt_footer", value="Thank you for dining with us!"))

    db.session.commit()
