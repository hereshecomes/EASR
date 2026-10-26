"""PAIR application-integrated conditional commit for the shop (repair study, app tier).

world.json "app_tier":
  none            no check (default)
  record_version  every add-to-basket form carries its product's stock record version (price, stock, last
                  update) at render; an add is rejected if the stock record changed since (optimistic concurrency
                  control on the form's target record). Basket-page line updates carry no version.
  conditional     an add or basket update is rejected if any condition posted with it (pair_condition: the
                  dependency values the agent saw) no longer holds.
A rejected request changes nothing and redirects to the product's page with an error message (an AJAX basket
update gets the same page change as a script the basket page runs). The check runs
in the request that writes, after the harness's last external write (the harness writes between requests).
"""
import json
import os
import re

from django.contrib import messages
from django.http import HttpResponseRedirect, JsonResponse
from oscar.core.loading import get_model

ADD = re.compile(r"^/basket/add/(\d+)/$")
FORM = re.compile(r'(<form[^>]*action="[^"]*/basket/add/(\d+)/"[^>]*>)')


def _world():
    try:
        with open(os.environ["PAIR_WORLD_FILE"]) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _record(pk):
    StockRecord = get_model("partner", "StockRecord")
    return StockRecord.objects.select_related("product").filter(product_id=int(pk)).first()


def _version(sr):
    return f"{sr.price:.2f}|{sr.num_in_stock}|{sr.date_updated.isoformat()}"


def _norm(field, v):
    s = " ".join(str(v if v is not None else "").split()).lower()
    if field == "price":
        try:
            return f"{float(s.replace('$', '').replace(',', '')):.2f}"
        except ValueError:
            return s
    if field == "availability":
        m = re.search(r"(out of stock|unavailable|in stock|available)", s)
        return {"available": "in stock", "unavailable": "out of stock"}.get(m.group(1), m.group(1)) if m else s
    return s


def _current(sr, field):
    if field == "price":
        return f"${sr.price:.2f}"
    if field == "availability":
        free = (sr.num_in_stock or 0) - (sr.num_allocated or 0)
        return "In stock" if free > 0 else "Out of stock"
    return None


class PairCommitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tier = _world().get("app_tier", "none")
        if request.method == "POST" and tier in ("record_version", "conditional"):
            rejected = self._check(request, tier)
            if rejected is not None:
                return rejected
        response = self.get_response(request)
        if (tier == "record_version" and request.method == "GET" and not getattr(response, "streaming", False)
                and "text/html" in response.get("Content-Type", "")):
            body = response.content.decode("utf-8")
            versions = {}

            def add_version(m):
                pk = m.group(2)
                if pk not in versions:
                    sr = _record(pk)
                    versions[pk] = _version(sr) if sr else ""
                return m.group(1) + f'<input type="hidden" name="pair_version" value="{versions[pk]}">'
            new = FORM.sub(add_version, body)
            if new != body:
                response.content = new.encode("utf-8")
                response["Content-Length"] = str(len(response.content))
        return response

    def _reject(self, request, pk, text):
        messages.error(request, text)
        sr = _record(pk)
        url = sr.product.get_absolute_url() if sr else "/basket/"
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            # The basket page posts line updates with AJAX and renders the JSON it gets back; a redirect would be
            # followed invisibly. Send the same page change as the redirect; the message is shown there.
            return JsonResponse({"content_html": "<script>window.location.assign(%s)</script>" % json.dumps(url + "?pair_rejected=1"),
                                 "messages": {}})
        return HttpResponseRedirect(url + "?pair_rejected=1")

    def _check(self, request, tier):
        m = ADD.match(request.path)
        if tier == "record_version" and m and "pair_version" in request.POST:
            sr = _record(m.group(1))
            posted = request.POST["pair_version"]
            if sr and posted != _version(sr):
                old = posted.split("|")
                diff = []
                if len(old) == 3 and _norm("price", old[0]) != _norm("price", sr.price):
                    diff.append(f"price ${float(old[0]):.2f} -> ${sr.price:.2f}")
                if len(old) == 3 and old[1] != str(sr.num_in_stock):
                    diff.append(f"stock {old[1]} -> {sr.num_in_stock}")
                return self._reject(request, m.group(1), "Your basket was not changed: this product was changed after the page was shown"
                                    + (" (" + "; ".join(diff) + ")" if diff else "") + ". Its current page is shown; decide again.")
        if tier == "conditional" and "pair_condition" in request.POST and (m or request.path == "/basket/"):
            try:
                conds = json.loads(request.POST["pair_condition"])
            except ValueError:
                conds = []
            failed, first = [], None
            for c in conds:
                sr = _record(c.get("pk", 0)) if str(c.get("pk", "")).isdigit() else None
                if sr is None:
                    continue
                cur = _current(sr, c.get("field"))
                if cur is not None and _norm(c["field"], c.get("value")) != _norm(c["field"], cur):
                    failed.append(f"{sr.product.title} {c['field']} {c.get('value')} -> {cur}")
                    first = first or c["pk"]
            if failed:
                return self._reject(request, first, "Your basket was not changed because information it depends on changed since you last saw it: "
                                    + "; ".join(failed) + ". The current page is shown; decide again.")
        return None
