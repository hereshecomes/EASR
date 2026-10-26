"""Create the Oscar template database for all PAIR shop worlds.

Usage: env/oscar/bin/python apps/oscar/build_template.py OUT_SQLITE FIXTURE_JSON
The template holds the catalogue at Base prices, the shopper account with a default
address, a fixed logged-in session, and an empty open basket. World builders copy it.
"""
import json
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

out, fixture = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
if out.exists():
    raise SystemExit(f"refusing to overwrite {out}")
os.environ["PAIR_OSCAR_DB"] = str(out)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pair_shop.settings")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import django  # noqa: E402

django.setup()
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model  # noqa: E402
from django.contrib.sessions.models import Session  # noqa: E402
from django.contrib.sites.models import Site  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.utils import timezone  # noqa: E402

call_command("migrate", verbosity=0)
from oscar.core.loading import get_model  # noqa: E402

Category = get_model("catalogue", "Category")
Product = get_model("catalogue", "Product")
ProductClass = get_model("catalogue", "ProductClass")
ProductCategory = get_model("catalogue", "ProductCategory")
Partner = get_model("partner", "Partner")
StockRecord = get_model("partner", "StockRecord")
UserAddress = get_model("address", "UserAddress")
Country = get_model("address", "Country")
Basket = get_model("basket", "Basket")

Site.objects.update_or_create(id=1, defaults={"domain": "127.0.0.1", "name": "PAIR Test Shop"})
pclass = ProductClass.objects.create(name="Accessory", requires_shipping=True, track_stock=True)
category = Category.add_root(name=fixture["category"])
partner = Partner.objects.create(name="PAIR Partner")
for item in fixture["products"]:
    product = Product.objects.create(upc=item["upc"], title=item["title"], product_class=pclass,
                                     description=item["description"], structure="standalone")
    ProductCategory.objects.create(product=product, category=category)
    StockRecord.objects.create(product=product, partner=partner, partner_sku=item["upc"],
                               price=Decimal(item["price"]), price_currency="USD",
                               num_in_stock=item["stock"])

shopper = fixture["shopper"]
User = get_user_model()
user = User.objects.create_user(username="shopper", email=shopper["email"], password=shopper["password"],
                                first_name=shopper["first_name"], last_name=shopper["last_name"])
country, _ = Country.objects.get_or_create(iso_3166_1_a2="US", defaults={
    "iso_3166_1_a3": "USA", "iso_3166_1_numeric": "840", "printable_name": "United States",
    "name": "United States of America", "is_shipping_country": True})
addr = fixture["address"]
UserAddress.objects.create(user=user, first_name=addr["first_name"], last_name=addr["last_name"],
                           line1=addr["line1"], line4=addr["line4"], postcode=addr["postcode"],
                           country=country, is_default_for_shipping=True, is_default_for_billing=True)
Basket.objects.create(owner=user, status="Open")

session = Session(session_key=fixture["session_key"], expire_date=timezone.now() + timedelta(days=365))
session.session_data = Session.objects.encode({SESSION_KEY: str(user.pk),
                                               BACKEND_SESSION_KEY: "oscar.apps.customer.auth_backends.EmailBackend",
                                               HASH_SESSION_KEY: user.get_session_auth_hash()})
session.save()
print(json.dumps({"template": str(out), "products": Product.objects.count(), "user": user.pk}))
