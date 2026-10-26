"""Django-Oscar adapter: isolated instances, world materialization, backend state, public facts,
delivery-bound price event, and guard targets. Evaluator-only access is via the instance's
sqlite file; the agent sees only pages served on its own origin.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import time
import urllib.request

from .common import diff_tables, kill_group, make_route_handler, port_free, FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS, enter_as_click
from ..dynamics import EventSchedule

ROOT = pathlib.Path(__file__).resolve().parents[2]
RT = pathlib.Path(os.environ.get("PAIR_RT", "/tmp/pair_rebuild_rt"))
OSCAR_PY = ROOT / "env/oscar/bin/python"
APP = ROOT / "apps/oscar"
TEMPLATE_DB = RT / "oscar_template.sqlite3"
FIXTURE = json.loads((ROOT / "tasks/fixtures/oscar_catalogue.json").read_text())
TITLE_TO_UPC = {p["title"]: p["upc"] for p in FIXTURE["products"]}
UPC_TO_TITLE = {v: k for k, v in TITLE_TO_UPC.items()}
BACKGROUND_TABLES = ("analytics_", "django_session", "thumbnail_kvstore")
BUSINESS_TABLES = ("basket_", "address_useraddress", "auth_user", "order_", "wishlists_", "reviews_",
                   "customer_", "voucher_", "communication_", "partner_stockrecord", "payment_")
BACKGROUND_COLUMNS = {("auth_user", "last_login"), ("basket_basket", "date_merged")}
PORT_BASE = 18310


def classify(table: str, column: str | None) -> str:
    if (table, column) in BACKGROUND_COLUMNS or table.startswith(BACKGROUND_TABLES):
        return "background"
    if table.startswith(BUSINESS_TABLES):
        return "business"
    return "unlisted"


class OscarInstance:
    """One isolated shop server (own sqlite, index, world file, port)."""

    def __init__(self, slot: int):
        self.slot = slot
        self.port = PORT_BASE + slot
        self.dir = RT / f"oscar_{slot}"
        self.db = self.dir / "db.sqlite3"
        self.world_file = self.dir / "world.json"
        self.index = self.dir / "index"
        self.proc = None
        self.origin = f"http://127.0.0.1:{self.port}"

    def env(self) -> dict:
        return {**os.environ, "PAIR_OSCAR_DB": str(self.db), "PAIR_WORLD_FILE": str(self.world_file),
                "PAIR_OSCAR_INDEX": str(self.index), "PAIR_OSCAR_STATIC": str(RT / "oscar_static"),
                "PYTHONUNBUFFERED": "1"}

    def stop(self) -> None:
        kill_group(self.proc)        # the whole process group: PHP forks worker processes
        self.proc = None

    def build(self, spec: dict) -> dict:
        self.stop()
        self.dir.mkdir(parents=True, exist_ok=True)
        spec_file = self.dir / "spec.json"
        spec_file.write_text(json.dumps(spec))
        out = subprocess.run([str(OSCAR_PY), "build_world.py", str(TEMPLATE_DB), str(self.db), str(self.world_file), str(spec_file)],
                             cwd=APP, env=self.env(), capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError("world build failed: " + out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def start_url(self, assignment: dict) -> str:
        return self.origin + "/"

    def start(self) -> None:
        if not port_free(self.port):
            raise RuntimeError(f"port {self.port} is already in use; refusing to start (a stale server would answer instead)")
        log = (self.dir / "server.log").open("a")
        self.proc = subprocess.Popen([str(OSCAR_PY), "manage.py", "runserver", f"127.0.0.1:{self.port}", "--noreload", "--insecure"],
                                     cwd=APP, env=self.env(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.origin + "/", timeout=3)
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited right after start")
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"Oscar instance {self.slot} did not start")


class OscarAdapter:
    app = "oscar"

    def __init__(self, instance: OscarInstance, dynamic: dict | None = None):
        self.inst = instance
        self.blocked = []
        self.route_handler = make_route_handler({instance.origin}, self.blocked)
        self.dynamic = dynamic            # {"condition": ..., "object": "PAIR-A", "old": "80.00", "new": "110.00"}
        self.dynamic_state = {"eligible_step": None, "fired": False, "receipt": None}
        self.initial_lines = None
        self.schedule = EventSchedule(dynamic["events"], self.write_value, self.read_value) if dynamic and "events" in dynamic else None

    def describe(self) -> dict:
        return {"app": "django-oscar 4.0", "origin": self.inst.origin, "slot": self.inst.slot, "dynamic": self.dynamic}

    async def prepare_context(self, context, record_initial: bool = True) -> None:
        await context.add_cookies([{"name": "sessionid", "value": FIXTURE["session_key"], "url": self.inst.origin}])
        if record_initial:
            self.initial_lines = self._basket_lines()

    # ---- evaluator-side backend access --------------------------------------------------------
    def _conn(self):
        return sqlite3.connect(f"file:{self.inst.db}?mode=ro", uri=True, timeout=20)

    def state(self) -> dict:
        snap = {}
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            for (table,) in conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"):
                if table in ("django_migrations", "auth_permission", "django_content_type"):
                    continue
                rows = conn.execute(f'select rowid as _rowid_, * from "{table}"').fetchall()
                snap[table] = {str(r["_rowid_"]): {k: r[k] for k in r.keys() if k != "_rowid_"} for r in rows}
        return snap

    def diff(self, before: dict, after: dict) -> list[dict]:
        return diff_tables(before, after, classify)

    def _basket_lines(self) -> list:
        with self._conn() as conn:
            return conn.execute("""select p.upc, l.quantity from basket_line l join catalogue_product p on p.id=l.product_id
                                   join basket_basket b on b.id=l.basket_id where b.status='Open' order by p.upc""").fetchall()

    def backend_facts(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute("""select p.upc, s.price, s.num_in_stock from partner_stockrecord s
                                   join catalogue_product p on p.id=s.product_id""").fetchall()
        return {f"product:{upc}": {"price": f"${float(price):.2f}", "stock": stock} for upc, price, stock in rows}

    # ---- public facts (what the delivered page showed) ---------------------------------------
    FACTS_JS = r"""() => {
      const t = s => (s || '').replace(/\s+/g, ' ').trim();
      const out = [];
      const main = document.querySelector('.product_main');
      const descHead = document.querySelector('#product_description');
      const descNode = descHead ? descHead.nextElementSibling : null;
      if (main) out.push({source: 'detail', title: t(main.querySelector('h1') && main.querySelector('h1').innerText),
                          description: descNode ? t(descNode.innerText) : '',
                          price: t(main.querySelector('.price_color') && main.querySelector('.price_color').innerText),
                          availability: t(main.querySelector('.availability') && main.querySelector('.availability').innerText)});
      document.querySelectorAll('.product_pod').forEach(pod => {
        const a = pod.querySelector('h3 a');
        out.push({source: 'list', title: a ? a.getAttribute('title') : '', price: t(pod.querySelector('.price_color') && pod.querySelector('.price_color').innerText),
                  availability: t(pod.querySelector('.availability') && pod.querySelector('.availability').innerText)});
      });
      document.querySelectorAll('#basket_formset .basket-items').forEach(row => {
        const a = row.querySelector('h3 a');
        const q = row.querySelector('input[name$="quantity"]');
        const prices = [...row.querySelectorAll('.price_color')].map(e => t(e.innerText));
        out.push({source: 'basket', title: a ? t(a.innerText) : '', quantity: q ? q.defaultValue : '', price: prices[0] || '', line_total: prices[prices.length - 1] || ''});
      });
      const acct = [];
      const pf = document.querySelector('#profile_form');
      const af = document.querySelector('form input[name=phone_number]') ? document.querySelector('form input[name=phone_number]').form : null;
      for (const [kind, f] of [['profile', pf], ['address', af]]) {
        if (!f) continue;
        const m = location.pathname.match(/addresses\/(\d+)\//);
        const vals = {};
        f.querySelectorAll('input[name],select[name],textarea[name]').forEach(e => {
          if (['hidden', 'submit', 'button', 'checkbox'].includes(e.type)) return;
          vals[e.name] = e.tagName === 'SELECT' ? ((([...e.options].find(o => o.defaultSelected) || e.options[0]) || {}).value || '') : e.defaultValue;
        });
        acct.push({kind, id: m ? m[1] : null, values: vals});
      }
      const notice = document.querySelector('.pair-notice');
      return {items: out, account_forms: acct, notice: notice ? t(notice.innerText) : null,
              basket_warnings: [...document.querySelectorAll('.alert-warning, .alert-info')].map(e => t(e.innerText)).filter(x => /price|increased|decreased/i.test(x))};
    }"""

    async def public_facts(self, page, obs) -> list[dict]:
        if obs is not None and obs.get("dialog"):
            return []
        raw = await page.evaluate(self.FACTS_JS)
        backend = self.backend_facts()
        facts = []
        for item in raw["items"]:
            upc = TITLE_TO_UPC.get(item["title"])
            obj = f"product:{upc}" if upc else f"product_title:{item['title']}"
            for field in ("price", "availability", "quantity", "line_total", "description"):
                if item.get(field):
                    fact = {"object": obj, "field": field, "value": item[field], "source": item["source"]}
                    if field == "price" and obj in backend:
                        fact["backend_value_at_capture"] = backend[obj]["price"]
                        fact["current"] = item[field] == backend[obj]["price"]
                    facts.append(fact)
        for form in raw.get("account_forms") or []:
            obj = "profile:shopper" if form["kind"] == "profile" else f"address:{form['id']}"
            for field, value in form["values"].items():
                facts.append({"object": obj, "field": field, "value": value, "source": "edit"})
        if raw["notice"]:
            facts.append({"object": "page", "field": "notice", "value": raw["notice"], "source": "banner"})
        for w in raw["basket_warnings"]:
            facts.append({"object": "basket", "field": "warning", "value": w, "source": "basket"})
        return facts

    # ---- v2 external-update schedule -------------------------------------------------------------
    def read_value(self, obj: str, field: str):
        kind, key = obj.split(":", 1)
        with self._conn() as conn:
            if kind == "product" and field in ("price", "stock"):
                col = "price" if field == "price" else "num_in_stock"
                row = conn.execute(f"select s.{col} from partner_stockrecord s join catalogue_product p on p.id=s.product_id where p.upc=?", (key,)).fetchone()
                return (f"{float(row[0]):.2f}" if field == "price" else int(row[0])) if row else None
            if kind == "product" and field == "description":
                row = conn.execute("select description from catalogue_product where upc=?", (key,)).fetchone()
                return " ".join((row[0] or "").split()) if row else None
            if kind == "basket_line" and field == "quantity":
                row = conn.execute("""select l.quantity from basket_line l join catalogue_product p on p.id=l.product_id
                                      join basket_basket b on b.id=l.basket_id where b.status='Open' and p.upc=?""", (key,)).fetchone()
                return int(row[0]) if row else 0
            if kind in ("profile", "address"):
                table, rid = self._account_row(conn, kind, key, field)
                row = conn.execute(f"select {field} from {table} where id=?", (rid,)).fetchone()
                return row[0] if row else None
        raise ValueError(f"unsupported read {obj}.{field}")

    ACCOUNT_FIELDS = {"profile": ("auth_user", ("first_name", "last_name", "email")),
                      "address": ("address_useraddress", ("first_name", "last_name", "line1", "line2", "line3", "line4", "state", "postcode",
                                                          "phone_number", "notes"))}

    @staticmethod
    def _account_row(conn, kind, key, field):
        table, fields = OscarAdapter.ACCOUNT_FIELDS[kind]
        if field not in fields:
            raise ValueError(f"unsupported field {kind}.{field}")
        if kind == "profile":
            # by username: the agent can change the account's email on the profile form (main_mistral_078 did), not its username
            row = (conn.execute("select id from auth_user where username=?", ("shopper",)).fetchone()
                   or conn.execute("select id from auth_user where email=?", (FIXTURE["shopper"]["email"],)).fetchone())
            return table, row[0]
        return table, int(key)

    def write_value(self, obj: str, field: str, value) -> None:
        kind, key = obj.split(":", 1)
        conn = sqlite3.connect(str(self.inst.db), timeout=20, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            pid = conn.execute("select id from catalogue_product where upc=?", (key,)).fetchone()[0] if kind in ("product", "basket_line") else None
            now = datetime.datetime.utcnow().isoformat(sep=" ")
            if kind == "product" and field == "price":
                conn.execute("update partner_stockrecord set price=?, date_updated=? where product_id=?", (str(value), now, pid))
            elif kind == "product" and field == "stock":
                conn.execute("update partner_stockrecord set num_in_stock=?, date_updated=? where product_id=?", (int(value), now, pid))
            elif kind == "product" and field == "description":
                conn.execute("update catalogue_product set description=?, date_updated=? where id=?", (str(value), now, pid))
            elif kind in ("profile", "address"):
                table, rid = self._account_row(conn, kind, key, field)
                conn.execute(f"update {table} set {field}=? where id=?", (str(value), rid))
            elif kind == "basket_line" and field == "quantity":
                conn.execute("""update basket_line set quantity=? where product_id=? and basket_id in
                                (select id from basket_basket where status='Open')""", (int(value), pid))
            else:
                raise ValueError(f"unsupported write {obj}.{field}")
            conn.execute("COMMIT")
        finally:
            conn.close()

    def on_start(self) -> list:
        return self.schedule.on_start() if self.schedule else []

    def before_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.before_commit(step) if self.schedule else []

    def after_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.after_commit(step) if self.schedule else []

    # ---- dynamic external update (v1 single event) ------------------------------------------------
    def after_delivery(self, step: int, obs: dict, facts: list) -> list:
        if self.schedule is not None:
            return self.schedule.after_delivery(step, facts, self._basket_lines() != self.initial_lines)
        d = self.dynamic
        if not d or self.dynamic_state["eligible_step"] is not None:
            return []
        target = f"product:{d['object']}"
        seen_old = any(f["object"] == target and f["field"] == "price" and f["source"] == "detail"
                       and f["value"] == f"${float(d['old']):.2f}" for f in facts)
        if not seen_old:
            return []
        if self._basket_lines() != self.initial_lines:
            self.dynamic_state["eligible_step"] = "after_business_change"
            return [{"kind": "ext", "status": "not_eligible_business_change_already_recorded", "step": step}]
        self.dynamic_state["eligible_step"] = step
        if d["condition"] != "external_update":
            return [{"kind": "ext", "status": "eligible_no_write_control", "step": step, "object": target}]
        conn = sqlite3.connect(str(self.inst.db), timeout=20, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            pid = conn.execute("select product_id from partner_stockrecord s join catalogue_product p on p.id=s.product_id where p.upc=?",
                               (d["object"],)).fetchone()[0]
            before = conn.execute("select price from partner_stockrecord where product_id=?", (pid,)).fetchone()[0]
            if f"{float(before):.2f}" != d["old"]:
                conn.execute("ROLLBACK")
                return [{"kind": "ext", "status": "receipt_mismatch", "step": step, "before": str(before)}]
            conn.execute("update partner_stockrecord set price=? where product_id=?", (d["new"], pid))
            after = conn.execute("select price from partner_stockrecord where product_id=?", (pid,)).fetchone()[0]
            conn.execute("COMMIT")
        finally:
            conn.close()
        receipt = {"kind": "ext", "status": "fired", "step": step, "object": target, "field": "price",
                   "before": f"{float(before):.2f}", "after": f"{float(after):.2f}", "t": time.time(),
                   "after_delivery_of_step": step}
        self.dynamic_state.update(fired=True, receipt=receipt)
        return [receipt]

    # ---- guard support -------------------------------------------------------------------------
    async def guard_target(self, page, act: dict, obs: dict):
        """Covered: click on an add-to-cart button, or Enter in its quantity field."""
        if act["type"] not in ("click", "press"):
            return None
        control = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), None)
        if not control or not control.get("form_action"):
            return None
        m = re.search(r"/basket/add/(\d+)/", control["form_action"])
        if not m:
            return None
        if act["type"] == "click" and "add to" not in control["label"].lower():
            return None
        if act["type"] == "press" and (act.get("key") != "Enter" or control["name"] != "quantity"):
            return None
        pk = m.group(1)
        product_url = re.compile(rf"/catalogue/(?!category/)[^/]+_{pk}/$")
        href = next((c["href"] for c in obs.get("controls", []) if c.get("href") and product_url.search(c["href"])), None)
        if href is None and product_url.search(obs["url"]):
            href = obs["url"]
        if href is None:
            with self._conn() as conn:
                slug = conn.execute("select slug from catalogue_product where id=?", (pk,)).fetchone()
            href = f"{self.inst.origin}/catalogue/{slug[0]}_{pk}/" if slug else None
        with self._conn() as conn:
            upc = conn.execute("select upc from catalogue_product where id=?", (pk,)).fetchone()
        return (f"product:{upc[0]}", href) if upc and href else None

    # ---- v2 validation family support (public mapping only; no database reads) ----------------------
    GUARD_COVERAGE_V2 = ("Oscar add-to-basket forms (product pages and catalogue lists) and the basket page's line form "
                         "(Update, Remove, Enter in a quantity field)")
    METADATA_FIELDS = ()
    FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS = FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS
    PRODUCT_URL = re.compile(r"/catalogue/(?!category/)[^/]+_(\d+)/$")

    def link_targets(self, obs: dict) -> dict:
        out = {}
        for c in obs.get("controls", []):
            label = (c.get("label") or "").strip()
            if c.get("href") and self.PRODUCT_URL.search(c["href"]) and label in TITLE_TO_UPC:
                out[f"product:{TITLE_TO_UPC[label]}"] = c["href"]
        return out

    def object_label(self, obj: str) -> str:
        return UPC_TO_TITLE.get(obj.split(":", 1)[-1], obj)

    def condition_ref(self, obj: str, field, url):
        m = self.PRODUCT_URL.search((url or "").split("?", 1)[0])
        return {"pk": m.group(1), "field": field} if m and obj.startswith("product:") else None

    async def commit_target(self, page, act: dict, obs: dict):
        act = enter_as_click(act, obs)
        if act["type"] not in ("click", "press"):
            return None
        c = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), None)
        if not c or not c.get("form_action"):
            return None
        label = ((c.get("label") or "") + " " + (c.get("value") or "")).lower()
        m = re.search(r"/basket/add/(\d+)/", c["form_action"])
        if m:
            if act["type"] == "click" and "add to" not in label:
                return None
            if act["type"] == "press" and (act.get("key") != "Enter" or c.get("name") != "quantity"):
                return None
            pk = m.group(1)
            href, obj = None, None
            for d in obs.get("controls", []):
                if d.get("href") and re.search(rf"_{pk}/$", d["href"]) and self.PRODUCT_URL.search(d["href"]):
                    href = d["href"]
                    obj = obj or (f"product:{TITLE_TO_UPC[d['label'].strip()]}" if (d.get("label") or "").strip() in TITLE_TO_UPC else None)
            path = obs["url"].split("?", 1)[0]
            if re.search(rf"_{pk}/$", path) and self.PRODUCT_URL.search(path):
                href = path
            # Oscar answers /catalogue/<any slug>_<pk>/ with a redirect to the product's page (public URL scheme).
            return {"object": obj, "url": href or f"{self.inst.origin}/catalogue/item_{pk}/", "kind": "product", "form": False}
        if re.search(r"/basket/$", c["form_action"]) and (
                (act["type"] == "click" and re.search(r"\b(update|remove|save)\b", label)) or
                (act["type"] == "press" and act.get("key") == "Enter" and (c.get("name") or "").endswith("quantity"))):
            # the basket formset is a form: footprint-based controllers see its lines (EASR study coverage, shared by all controllers)
            return {"object": "basket", "url": f"{self.inst.origin}/basket/", "kind": "basket", "form": True, "edit_url": f"{self.inst.origin}/basket/"}
        m = re.search(r"/accounts/(profile/edit|addresses/(\d+))/", c["form_action"])
        if m and act["type"] == "click":
            obj = "profile:shopper" if m.group(1).startswith("profile") else f"address:{m.group(2)}"
            u = self.inst.origin + m.group(0)
            return {"object": obj, "url": u, "edit_url": u, "kind": "record", "form": True}
        return None

    EASR_OMISSION_SAFE = False          # Django forms and formsets: an omitted field is blanked or fails validation

    GUARD_FIELDS = ("price", "availability")
    GUARD_SOURCES = ("detail",)
    GUARD_COVERAGE = "Oscar add-to-cart forms (product page and catalogue lists); basket-page edits are not covered"

    def commit_like(self, act: dict, obs: dict) -> bool:
        control = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), None)
        if not control:
            return False
        return bool(re.search(r"/basket/|/accounts/(profile/edit|addresses/\d+)/", control.get("form_action") or "")) and act["type"] in ("click", "press")


# ---- evaluator-side translation of raw table changes into business terms ------------------------
def same_value(field: str, a, b) -> bool:
    """Compare a displayed value with a backend value ("$110.00" vs "110.00"); availability by class."""
    if field == "availability":
        from ..facts import availability_class
        return availability_class(a) == availability_class(b)
    try:
        return float(str(a).replace("$", "").replace(",", "")) == float(str(b).replace("$", "").replace(",", ""))
    except ValueError:
        return " ".join(str(a).split()) == " ".join(str(b).split())
def _product_map(state: dict) -> dict:
    return {str(r["id"]): r["upc"] for r in state.get("catalogue_product", {}).values()}


def describe_mutation(m: dict, state_before: dict, state_after: dict) -> dict:
    """Business-level reading of one raw table change (evaluator side)."""
    products = _product_map(state_after) or _product_map(state_before)
    t = m["table"]
    if t == "basket_line":
        row = m.get("row") or state_after.get(t, {}).get(m["key"]) or state_before.get(t, {}).get(m["key"]) or {}
        upc = products.get(str(row.get("product_id")), "?")
        if m["op"] == "insert":
            return {"object": f"basket_line:{upc}", "field": "quantity", "before": 0, "after": row.get("quantity"), "op": "add"}
        if m["op"] == "delete":
            return {"object": f"basket_line:{upc}", "field": "quantity", "before": row.get("quantity"), "after": 0, "op": "remove"}
        return {"object": f"basket_line:{upc}", "field": m["column"], "before": m["before"], "after": m["after"], "op": "update"}
    if t == "address_useraddress":
        return {"object": "account:address", "field": m.get("column", "row"), "before": m.get("before"), "after": m.get("after"), "op": m["op"]}
    if t == "auth_user":
        return {"object": "account:profile", "field": m.get("column", "row"), "before": m.get("before"), "after": m.get("after"), "op": m["op"]}
    if t.startswith("order_"):
        return {"object": f"order:{t}", "field": m.get("column", "row"), "before": m.get("before"), "after": m.get("after"), "op": m["op"]}
    return {"object": f"table:{t}", "field": m.get("column", "row"), "before": m.get("before"), "after": m.get("after"), "op": m["op"]}


def terminal_summary(state: dict, initial: dict | None = None) -> dict:
    products = _product_map(state)
    open_baskets = {k for k, r in state.get("basket_basket", {}).items() if r.get("status") == "Open"}
    lines = {}
    for r in state.get("basket_line", {}).values():
        if str(r.get("basket_id")) in open_baskets:
            upc = products.get(str(r["product_id"]), "?")
            lines[upc] = lines.get(upc, 0) + int(r["quantity"])
    summary = {"basket": dict(sorted(lines.items())),
               "orders": len(state.get("order_order", {})),
               "wishlist_lines": len(state.get("wishlists_line", {}))}
    if initial is not None:
        summary["address_unchanged"] = state.get("address_useraddress") == initial.get("address_useraddress")
        strip = lambda rows: {k: {c: v for c, v in r.items() if c != "last_login"} for k, r in rows.items()}
        summary["profile_unchanged"] = strip(state.get("auth_user", {})) == strip(initial.get("auth_user", {}))
    return summary
