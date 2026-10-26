"""Materialize one shop world from the template database.

Usage: PAIR_OSCAR_INDEX=DIR env/oscar/bin/python apps/oscar/build_world.py TEMPLATE_SQLITE OUT_SQLITE WORLD_JSON SPEC_JSON
SPEC fields: prices {upc: price}, initial_lines [[upc, qty]], prices_after_lines {upc: price},
notice {title, text} or null, stale_list_prices {upc: price}. Lines are added at the prices
in force when they are added, so a later price change shows Oscar's own price-change warning.
"""
import json
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path

template, out, world_file, spec = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), json.loads(Path(sys.argv[4]).read_text())
out.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm", "-journal"):
    Path(str(out) + suffix).unlink(missing_ok=True)
shutil.copyfile(template, out)
os.environ["PAIR_OSCAR_DB"] = str(out)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pair_shop.settings")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import django  # noqa: E402

django.setup()
from oscar.apps.partner.strategy import Selector  # noqa: E402
from oscar.core.loading import get_model  # noqa: E402

StockRecord = get_model("partner", "StockRecord")
Product = get_model("catalogue", "Product")
Basket = get_model("basket", "Basket")


def set_prices(prices):
    for upc, price in prices.items():
        StockRecord.objects.filter(product__upc=upc).update(price=Decimal(price))


set_prices(spec.get("prices", {}))
basket = Basket.objects.get(owner__username="shopper", status="Open")
basket.strategy = Selector().strategy()
for upc, qty in spec.get("initial_lines", []):
    basket.add_product(Product.objects.get(upc=upc), quantity=qty)
set_prices(spec.get("prices_after_lines", {}))
for upc, n in spec.get("stock", {}).items():          # applied after the lines, so a line can become unavailable
    StockRecord.objects.filter(product__upc=upc).update(num_in_stock=int(n))
for upc, text in spec.get("descriptions", {}).items():
    Product.objects.filter(upc=upc).update(description=text)
from django.core.management import call_command  # noqa: E402

call_command("rebuild_index", interactive=False, verbosity=0)
world = {"world": spec["world"], "notice": spec.get("notice"), "stale_list_prices": spec.get("stale_list_prices", {}),
         "stale_list_availability": spec.get("stale_list_availability", {}),
         "app_tier": spec.get("app_tier", "none")}
tmp = world_file.with_suffix(".tmp")
tmp.write_text(json.dumps(world, indent=1))
tmp.replace(world_file)
print(json.dumps({"db": str(out), "lines": [[l.product.upc, l.quantity, str(l.price_incl_tax)] for l in basket.all_lines()]}))
