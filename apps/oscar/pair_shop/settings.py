"""Django-Oscar 4.0 test shop for PAIR worlds.

Per-instance paths come from the environment so several isolated instances can run at once:
PAIR_OSCAR_DB (sqlite file) and PAIR_WORLD_FILE (world JSON read on every request).
"""
import os
from pathlib import Path

from oscar.defaults import *  # noqa: F401,F403

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "pair-rebuild-test-only-not-secret"
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "django.contrib.sites", "django.contrib.flatpages",
    "oscar.config.Shop",
    "oscar.apps.analytics.apps.AnalyticsConfig",
    "oscar.apps.checkout.apps.CheckoutConfig",
    "oscar.apps.address.apps.AddressConfig",
    "oscar.apps.shipping.apps.ShippingConfig",
    "oscar.apps.catalogue.apps.CatalogueConfig",
    "oscar.apps.catalogue.reviews.apps.CatalogueReviewsConfig",
    "oscar.apps.communication.apps.CommunicationConfig",
    "oscar.apps.partner.apps.PartnerConfig",
    "oscar.apps.basket.apps.BasketConfig",
    "oscar.apps.payment.apps.PaymentConfig",
    "oscar.apps.offer.apps.OfferConfig",
    "oscar.apps.order.apps.OrderConfig",
    "oscar.apps.customer.apps.CustomerConfig",
    "oscar.apps.search.apps.SearchConfig",
    "oscar.apps.voucher.apps.VoucherConfig",
    "oscar.apps.wishlists.apps.WishlistsConfig",
    "oscar.apps.dashboard.apps.DashboardConfig",
    "oscar.apps.dashboard.reports.apps.ReportsDashboardConfig",
    "oscar.apps.dashboard.users.apps.UsersDashboardConfig",
    "oscar.apps.dashboard.orders.apps.OrdersDashboardConfig",
    "oscar.apps.dashboard.catalogue.apps.CatalogueDashboardConfig",
    "oscar.apps.dashboard.offers.apps.OffersDashboardConfig",
    "oscar.apps.dashboard.partners.apps.PartnersDashboardConfig",
    "oscar.apps.dashboard.pages.apps.PagesDashboardConfig",
    "oscar.apps.dashboard.ranges.apps.RangesDashboardConfig",
    "oscar.apps.dashboard.reviews.apps.ReviewsDashboardConfig",
    "oscar.apps.dashboard.vouchers.apps.VouchersDashboardConfig",
    "oscar.apps.dashboard.communications.apps.CommunicationsDashboardConfig",
    "oscar.apps.dashboard.shipping.apps.ShippingDashboardConfig",
    "widget_tweaks", "haystack", "treebeard", "sorl.thumbnail", "django_tables2",
    "pair_world",
]
SITE_ID = 1
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "oscar.apps.basket.middleware.BasketMiddleware",
    "django.contrib.flatpages.middleware.FlatpageFallbackMiddleware",
    "pair_world.middleware.PairCommitMiddleware",
]
ROOT_URLCONF = "pair_shop.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "oscar.apps.search.context_processors.search_form",
        "oscar.apps.checkout.context_processors.checkout",
        "oscar.apps.communication.notifications.context_processors.notifications",
        "oscar.core.context_processors.metadata",
    ]},
}]
WSGI_APPLICATION = "pair_shop.wsgi.application"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": os.environ.get("PAIR_OSCAR_DB", str(BASE_DIR / "dev.sqlite3")),
    "OPTIONS": {"timeout": 20},
}}
AUTHENTICATION_BACKENDS = ("oscar.apps.customer.auth_backends.EmailBackend",
                           "django.contrib.auth.backends.ModelBackend")
# Oscar 4.0 browses the catalogue through haystack; a per-instance Whoosh index keeps it local.
HAYSTACK_CONNECTIONS = {"default": {"ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
                                   "PATH": os.environ.get("PAIR_OSCAR_INDEX", str(BASE_DIR / "whoosh_index"))}}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = os.environ.get("PAIR_OSCAR_STATIC", str(BASE_DIR / "static_collected"))
MEDIA_ROOT = os.environ.get("PAIR_OSCAR_MEDIA", str(BASE_DIR / "media"))
MEDIA_URL = "/media/"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 365
OSCAR_SHOP_NAME = "PAIR Test Shop"
OSCAR_SHOP_TAGLINE = "test application"
OSCAR_DEFAULT_CURRENCY = "USD"
OSCAR_ALLOW_ANON_CHECKOUT = False
PAIR_WORLD_FILE = os.environ.get("PAIR_WORLD_FILE", "")
LOGGING = {"version": 1, "disable_existing_loggers": False,
           "handlers": {"console": {"class": "logging.StreamHandler"}},
           "root": {"handlers": ["console"], "level": "WARNING"}}
