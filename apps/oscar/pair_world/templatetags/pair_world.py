"""Render world-specific public content: a site-wide notice and stale list prices.

The world file is read on every request so an instance can be switched to a new world by
rewriting one JSON file. Nothing here changes backend business data.
"""
import json
from pathlib import Path

from django import template
from django.conf import settings
from django.utils.html import format_html

register = template.Library()


def _world():
    path = settings.PAIR_WORLD_FILE
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


@register.simple_tag
def pair_notice():
    notice = _world().get("notice")
    if not notice:
        return ""
    return format_html('<div class="alert alert-info pair-notice" role="status"><strong>{}</strong> {}</div>',
                       notice["title"], notice["text"])


@register.simple_tag
def pair_stale_list_price(product):
    """Price label shown on catalogue lists only, when the world declares a stale cache."""
    stale = _world().get("stale_list_prices", {})
    return stale.get(product.upc, "")


@register.simple_tag
def pair_stale_list_availability(product):
    """Availability label shown on catalogue lists only, when the world declares a stale cache."""
    return _world().get("stale_list_availability", {}).get(product.upc, "")
