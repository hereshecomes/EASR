"""SuiteCRM 7.14 adapter: isolated PHP instances over one private MariaDB, world databases cloned
from the seeded template, harness login, backend snapshots, public facts, the delivery-bound
amount update, and guard targets. The agent only ever sees pages from its instance origin.
"""
from __future__ import annotations

import datetime
import decimal
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pymysql

from .common import diff_tables, kill_group, make_route_handler, port_free, FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS, enter_as_click
from ..dynamics import EventSchedule

ROOT = pathlib.Path(__file__).resolve().parents[2]
RT = pathlib.Path(os.environ.get("PAIR_RT", "/tmp/pair_rebuild_rt"))
PHP = ROOT / "env/php/bin/php"
MARIADB = ROOT / "apps/crm/mariadb.sh"
TEMPLATE_WWW = RT / "crm_template/www"
TEMPLATE_SQL = RT / "crm_template.sql"
FIXTURE = json.loads((ROOT / "tasks/fixtures/crm_records.json").read_text())
DB = {"host": "127.0.0.1", "port": 18400, "user": "pair", "password": "pair-test-db-2026"}
PORT_BASE = 18421
ID_TO_KEY = {r["id"]: f"contact:{r['key']}" for r in FIXTURE["contacts"]}
ID_TO_KEY.update({r["id"]: f"note:{r['key']}" for r in FIXTURE["notes"]})
ID_TO_KEY.update({r["id"]: f"opportunity:{r['key']}" for r in FIXTURE["opportunities"]})
ID_TO_KEY.update({r["id"]: f"account:{r['id'].replace('pair-acct-', '')}" for r in FIXTURE["accounts"]})
ACCOUNT_NAME = {r["id"]: r["name"] for r in FIXTURE["accounts"]}
ID_TO_KEY["pair-note-audit-phone"] = "note:audit_phone"          # created by the held-out world crm_restore_after_error
# Display names of the fixture records, as their own pages show them (used only to name objects in messages).
OBJECT_LABEL = {**{f"contact:{r['key']}": f"{r['first_name']} {r['last_name']}" + (f" ({r['title']})" if r["last_name"] == "Alex Lee" else "")
                   for r in FIXTURE["contacts"]},
                **{f"opportunity:{r['key']}": r["name"] for r in FIXTURE["opportunities"]},
                **{f"note:{r['key']}": r["name"] for r in FIXTURE["notes"]}, "note:audit_phone": "Audit note: PAIR Phone Contact"}
SKIP_TABLES = {"acl_actions", "relationships", "fields_meta_data", "email_templates", "config", "schedulers",
               "upgrade_history", "versions", "currencies"}
BACKGROUND_TABLES = {"tracker", "user_preferences", "aod_index", "aod_indexevent", "sugarfeed", "job_queue",
                     "saved_search", "users_last_import", "favorites", "reminders", "alerts"}
BUSINESS_TABLES = {"contacts", "contacts_cstm", "accounts", "accounts_cstm", "accounts_contacts", "opportunities",
                   "opportunities_cstm", "accounts_opportunities", "opportunities_contacts", "notes", "email_addresses",
                   "email_addr_bean_rel", "calls", "meetings", "tasks", "documents", "leads", "cases", "users",
                   "contacts_users", "calls_contacts", "meetings_contacts", "emails", "emails_beans", "project", "bugs"}
METADATA_COLUMNS = {"date_modified", "modified_user_id"}
# Columns SuiteCRM's edit form fills with defaults on any save (not an edit the agent chose).
FORM_DEFAULT_COLUMNS = {"amount_usdollar", "portal_account_disabled", "do_not_call", "campaign_id", "joomla_account_id", "portal_user_type"}


CONFLICT_LABELS = {"opportunity amount": "amount", "amount": "amount", "sales stage": "sales_stage", "probability (%)": "probability",
                   "description": "description", "next step": "next_step", "expected close date": "date_closed", "office phone": "phone_work",
                   "title": "title", "job title": "title"}


def classify(table: str, column: str | None) -> str:
    if table in BACKGROUND_TABLES:
        return "background"
    if column in METADATA_COLUMNS or column in FORM_DEFAULT_COLUMNS or table.endswith(("_audit", "_cstm")) or table == "email_addresses":
        return "metadata"
    if table in BUSINESS_TABLES:
        return "business"
    return "unlisted"


def _plain(v):
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v.normalize()) if v == v.to_integral() else str(v)
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    return v


def number(text) -> float | None:
    s = re.sub(r"[^0-9.\-]", "", "" if text is None else str(text))       # 0 is a value, not a missing one
    try:
        return float(s)
    except ValueError:
        return None


def iso_date(text) -> str:
    """Normalize the agent user's m/d/Y display format and ISO dates to YYYY-MM-DD."""
    t = str(text or "").strip()[:10]
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else t


def mysql(sql: str, db: str | None = None) -> None:
    subprocess.run([str(MARIADB), "client"] + ([db] if db else []) + ["-e", sql], check=True, capture_output=True, text=True)


def freeze_template() -> dict:
    """Dump the seeded template DB once; every world is cloned from this file."""
    out = subprocess.run([str(ROOT / "env/mariadb/bin/mariadb-dump"), "--no-defaults", f"--socket={RT}/mariadb/mysql.sock", "-uroot",
                          "--single-transaction", "--skip-extended-insert", "pair_crm_template"], capture_output=True, text=True, check=True)
    TEMPLATE_SQL.write_text(out.stdout)
    return {"bytes": TEMPLATE_SQL.stat().st_size}


class CrmInstance:
    def __init__(self, slot: int):
        self.slot = slot
        self.port = PORT_BASE + slot
        self.dir = RT / f"crm_{slot}"
        self.www = self.dir / "www"
        self.db = f"pair_crm_{slot}"
        self.world_file = self.dir / "world.json"
        self.origin = f"http://127.0.0.1:{self.port}"
        self.proc = None

    def _ensure_code(self) -> None:
        if not self.www.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATE_WWW, self.www, symlinks=True)
        shutil.copyfile(ROOT / "apps/crm/custom/include/PairWorld.php", self.www / "custom/include/PairWorld.php")
        shutil.copyfile(ROOT / "apps/crm/custom/modules/logic_hooks.php", self.www / "custom/modules/logic_hooks.php")
        (self.www / "config_override.php").write_text(
            "<?php\n// PAIR slot override: own URL and database.\n"
            f"$sugar_config['site_url'] = '{self.origin}';\n"
            f"$sugar_config['dbconfig']['db_name'] = '{self.db}';\n"
            "$sugar_config['logger']['level'] = 'fatal';\n")
        ini = (ROOT / "apps/crm/php.ini.tmpl").read_text().replace("__DIR__", str(self.dir))
        (self.dir / "php.ini").write_text(ini)

    def stop(self) -> None:
        kill_group(self.proc)        # the whole process group: PHP forks worker processes
        self.proc = None

    def build(self, spec: dict) -> dict:
        self.stop()
        self._ensure_code()
        shutil.rmtree(self.dir / "sessions", ignore_errors=True)
        (self.dir / "sessions").mkdir(parents=True)
        mysql(f"DROP DATABASE IF EXISTS {self.db}; CREATE DATABASE {self.db} CHARACTER SET utf8mb4;")
        with TEMPLATE_SQL.open() as fh:
            subprocess.run([str(MARIADB), "client", self.db], stdin=fh, check=True, capture_output=True)
        applied = []
        if "directory" in spec:
            for key, phone in spec["directory"].items():
                note = next(n for n in FIXTURE["notes"] if n["key"] == f"directory_{key}")
                desc = re.sub(r"Office phone: .*", f"Office phone: {phone}", note["description"])
                mysql(f"UPDATE notes SET description={json.dumps(desc)} WHERE id='{note['id']}'", self.db)
                applied.append(f"{note['id']}.description={phone}")
        if "opportunity_amount" in spec:
            amt = spec["opportunity_amount"]
            mysql(f"UPDATE opportunities SET amount={amt}, amount_usdollar={amt} WHERE id='pair-opp-renewal'", self.db)
            applied.append(f"pair-opp-renewal.amount={amt}")
        for op in spec.get("record_updates", []):          # generic world facts: {table, id, column, value}
            mysql(f"UPDATE `{op['table']}` SET `{op['column']}`={json.dumps(op['value'])} WHERE id='{op['id']}'", self.db)
            applied.append(f"{op['id']}.{op['column']}={op['value']}")
        for n in spec.get("new_notes", []):                # a note copied from a template note row, then renamed and re-attached
            mysql(f"CREATE TEMPORARY TABLE pair_tmp AS SELECT * FROM notes WHERE id='{n.get('copy_of', 'pair-note-dir-phone')}'; "
                  f"UPDATE pair_tmp SET id='{n['id']}', name={json.dumps(n['name'])}, description={json.dumps(n['description'])}, "
                  f"contact_id='{n['contact_id']}', parent_type='Contacts', parent_id='{n['contact_id']}'; "
                  f"INSERT INTO notes SELECT * FROM pair_tmp; DROP TEMPORARY TABLE pair_tmp;", self.db)
            applied.append(f"note {n['id']}")
        if spec.get("alex_analyst_account") == "other":
            mysql("UPDATE accounts_contacts SET account_id='pair-acct-other' WHERE contact_id='pair-ct-alex-analyst' AND deleted=0", self.db)
            applied.append("pair-ct-alex-analyst.account=pair-acct-other")
        world = {"world": spec["world"], "notice": spec.get("notice"), "native_occ": spec.get("native_occ", "on"), "app_tier": spec.get("app_tier", "none")}
        self.world_file.write_text(json.dumps(world, indent=1))
        return {"db": self.db, "applied": applied, "notice": bool(spec.get("notice"))}

    def start(self) -> None:
        if not port_free(self.port):
            raise RuntimeError(f"port {self.port} is already in use; refusing to start (a stale server would answer instead)")
        env = {**os.environ, "PAIR_WORLD_FILE": str(self.world_file), "PHP_CLI_SERVER_WORKERS": "8"}
        log = (self.dir / "server.log").open("a")
        self.proc = subprocess.Popen([str(PHP), "-c", str(self.dir / "php.ini"), "-S", f"127.0.0.1:{self.port}", "-t", str(self.www)],
                                     cwd=self.www, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.origin + "/index.php?module=Users&action=Login", timeout=5)
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited right after start")
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"CRM instance {self.slot} did not start")

    def start_url(self, assignment: dict) -> str:
        return self.origin + "/index.php?module=Home&action=index"


class CrmAdapter:
    app = "crm"
    EASR_OMISSION_SAFE = True           # SuiteCRM keeps stored values for fields absent from the POST (widget units travel together)
    EASR_FORM_WAIT = "form#EditView input[name=record]"
    GUARD_FIELDS = ("amount", "sales_stage", "probability", "date_closed", "account_name", "phone_work", "title", "description")
    GUARD_SOURCES = ("record",)
    GUARD_COVERAGE = "SuiteCRM Save on a record edit form (Contacts, Opportunities, Accounts, Notes); inline edits are not covered"

    def __init__(self, instance: CrmInstance, dynamic: dict | None = None):
        self.inst = instance
        self.blocked = []
        # Alerts polling repeats every few seconds, is unrelated to any task, and contends for the PHP session lock.
        self.route_handler = make_route_handler({instance.origin}, self.blocked, ("module=Alerts&action=get",))
        self.dynamic = dynamic
        self.dynamic_state = {"eligible_step": None, "fired": False, "receipt": None}
        self._tables = None
        self._initial_business = None
        self.schedule = EventSchedule(dynamic["events"], self.write_value, self.read_value) if dynamic and "events" in dynamic else None

    def describe(self) -> dict:
        return {"app": "SuiteCRM 7.14.9", "origin": self.inst.origin, "slot": self.inst.slot, "dynamic": self.dynamic}

    async def prepare_context(self, context, record_initial: bool = True) -> None:
        page = await context.new_page()
        await page.goto(self.inst.origin + "/index.php?module=Users&action=Login", wait_until="domcontentloaded")
        await page.fill("#user_name", FIXTURE["agent_user"]["user_name"])
        await page.fill("#username_password", FIXTURE["agent_user"]["password"])
        await page.click("#bigbutton")
        await page.wait_for_load_state("domcontentloaded")
        if "action=Login" in page.url:
            raise RuntimeError("harness login failed")
        await page.close()
        if record_initial:
            self._initial_business = self._business_rows()

    # ---- evaluator-side backend -------------------------------------------------------------------
    def _connect(self):
        return pymysql.connect(database=self.inst.db, autocommit=True, **DB)

    def state(self) -> dict:
        snap = {}
        with self._connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            if self._tables is None:
                cur.execute("select table_name from information_schema.tables where table_schema=%s", (self.inst.db,))
                self._tables = sorted(r["table_name"] for r in cur.fetchall() if r["table_name"] not in SKIP_TABLES)
            for table in self._tables:
                cur.execute(f"select * from `{table}`")
                rows = cur.fetchall()
                if not rows:
                    continue
                keyed = {}
                for r in rows:
                    clean = {k: _plain(v) for k, v in r.items()}
                    if "id" in clean:
                        key = str(clean["id"])
                    elif "id_c" in clean:              # SuiteCRM custom-field rows (rewritten on every save)
                        key = str(clean["id_c"])
                    else:
                        key = json.dumps(clean, sort_keys=True, default=str)
                    keyed[key] = clean
                snap[table] = keyed
        return snap

    def diff(self, before: dict, after: dict) -> list[dict]:
        return diff_tables(before, after, classify)

    def _business_rows(self):
        st = self.state()
        return {t: st.get(t) for t in ("contacts", "accounts_contacts", "opportunities", "notes")}

    def backend_facts(self) -> dict:
        out = {}
        with self._connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("select id, amount, sales_stage, probability, date_closed from opportunities where deleted=0")
            for r in cur.fetchall():
                key = ID_TO_KEY.get(r["id"], f"opportunity:{r['id']}")
                out[key] = {"amount": number(r["amount"]), "sales_stage": r["sales_stage"],
                            "probability": number(r["probability"]), "date_closed": _plain(r["date_closed"])}
            cur.execute("""select c.id, c.phone_work, c.title, ac.account_id from contacts c left join accounts_contacts ac
                           on ac.contact_id=c.id and ac.deleted=0 where c.deleted=0""")
            for r in cur.fetchall():
                key = ID_TO_KEY.get(r["id"], f"contact:{r['id']}")
                out[key] = {"phone_work": r["phone_work"], "title": r["title"], "account_name": ACCOUNT_NAME.get(r["account_id"], r["account_id"])}
            cur.execute("select id, description from notes where deleted=0")
            for r in cur.fetchall():
                out[ID_TO_KEY.get(r["id"], f"note:{r['id']}")] = {"description": " ".join((r["description"] or "").split())}
        return out

    @staticmethod
    def same(field: str, a, b) -> bool:
        if field in ("amount", "probability"):
            return number(a) is not None and number(a) == number(b)
        if field == "date_closed":
            return iso_date(a) == iso_date(b)
        return " ".join(str(a or "").split()).lower() == " ".join(str(b or "").split()).lower()

    # ---- public facts ----------------------------------------------------------------------------
    FACTS_JS = r"""() => {
      const t = s => (s || '').replace(/\s+/g, ' ').trim();
      const q = new URLSearchParams(location.search);
      const out = {module: q.get('module'), action: q.get('action'), record: q.get('record'), fields: {}, source: null, notice: null, list_rows: []};
      const notice = document.querySelector('.pair-notice'); if (notice) out.notice = t(notice.innerText);
      const detail = document.querySelectorAll('.detail-view-field[field]');
      if (detail.length) {
        out.source = 'record';
        detail.forEach(e => { out.fields[e.getAttribute('field')] = t(e.innerText); });
        const rec = document.querySelector('#formDetailView input[name=record], form[name=DetailView] input[name=record]');
        if (rec && rec.value) out.record = rec.value;
      }
      const edit = document.querySelector('form#EditView');
      if (edit) {
        out.source = 'edit';
        const rec = edit.querySelector('input[name=record]'); if (rec) out.record = rec.value;
        const mod = edit.querySelector('input[name=module]'); if (mod) out.module = mod.value;
        edit.querySelectorAll('input[name],textarea[name],select[name]').forEach(e => {
          if (e.type === 'hidden' || e.type === 'button' || e.type === 'submit') return;
          if (e.tagName === 'SELECT') { const o = [...e.options].find(o => o.defaultSelected); out.fields[e.name] = o ? o.value : ''; }
          else if (e.type === 'checkbox') { out.fields[e.name] = e.defaultChecked ? '1' : '0'; }
          else { out.fields[e.name] = e.defaultValue; }
        });
      }
      if (out.module === 'OptimisticLock') {      // SuiteCRM conflict page: field / yours / in database
        const link = document.querySelector('a[href*="action=DetailView"][href*="record="]');
        if (link) { const qs = new URLSearchParams(link.href.split('?')[1]); out.record = qs.get('record'); out.module = qs.get('module'); }
        const rows = [...document.querySelectorAll('table.list.view tr')].map(r => [...r.querySelectorAll('td')].map(td => t(td.innerText)));
        if (rows.length >= 3) {
          out.source = 'conflict';
          const head = rows[0], dbrow = rows.find(r => /in database/i.test(r[0] || ''));
          head.forEach((h, i) => { if (i > 0 && dbrow && h) out.fields['label:' + h.replace(/:$/, '')] = dbrow[i] || ''; });
        }
      }
      document.querySelectorAll('table.list.view tbody tr').forEach(tr => {
        const a = tr.querySelector('a[href*="record="]');
        if (a) out.list_rows.push({record: new URLSearchParams(a.href.split('?')[1]).get('record'), text: t(tr.innerText).slice(0, 300)});
      });
      return out;
    }"""

    async def public_facts(self, page, obs) -> list[dict]:
        if obs is not None and obs.get("dialog"):
            return []
        raw = await page.evaluate(self.FACTS_JS)
        facts = []
        backend = self.backend_facts()
        if raw["record"] and raw["source"]:
            obj = ID_TO_KEY.get(raw["record"], f"{(raw['module'] or 'record').lower()}:{raw['record']}")
            for field, value in raw["fields"].items():
                if field.startswith("label:"):
                    field = CONFLICT_LABELS.get(field[6:].strip().lower())
                    if field is None:
                        continue
                if field.endswith(("_c", "_name_id")) or field in ("currency_id", "assigned_user_name", "assigned_user_id", "team_name"):
                    continue
                fact = {"object": obj, "field": field, "value": value, "source": raw["source"]}
                if obj in backend and field in backend[obj]:
                    fact["current"] = self.same(field, value, backend[obj][field])
                facts.append(fact)
        for row in raw["list_rows"]:
            obj = ID_TO_KEY.get(row["record"])
            if obj:
                facts.append({"object": obj, "field": "list_row", "value": row["text"], "source": "list"})
        if raw["notice"]:
            facts.append({"object": "page", "field": "notice", "value": raw["notice"], "source": "banner"})
        return facts

    # ---- v2 external-update schedule -------------------------------------------------------------
    KEY_TO_ID = {v: k for k, v in ID_TO_KEY.items()}
    TABLE = {"opportunity": "opportunities", "contact": "contacts", "note": "notes", "account": "accounts"}

    def read_value(self, obj: str, field: str):
        kind = obj.split(":", 1)[0]
        rid = self.KEY_TO_ID[obj]
        with self._connect() as conn, conn.cursor() as cur:
            if kind == "contact" and field in ("account", "account_name"):
                cur.execute("select account_id from accounts_contacts where contact_id=%s and deleted=0", (rid,))
                row = cur.fetchone()
                return ACCOUNT_NAME.get(row[0], row[0]) if row else None
            cur.execute(f"select `{field}` from {self.TABLE[kind]} where id=%s", (rid,))
            row = cur.fetchone()
            v = _plain(row[0]) if row else None
            return str(int(number(v))) if field == "amount" and v is not None else v

    def write_value(self, obj: str, field: str, value) -> None:
        """Write as another user would: the business field plus date_modified and modified_user_id."""
        kind = obj.split(":", 1)[0]
        rid = self.KEY_TO_ID[obj]
        with self._connect() as conn, conn.cursor() as cur:
            if kind == "contact" and field in ("account", "account_name"):
                acct = {v: k for k, v in ACCOUNT_NAME.items()}[value]
                cur.execute("update accounts_contacts set account_id=%s, date_modified=UTC_TIMESTAMP() where contact_id=%s and deleted=0", (acct, rid))
                cur.execute("update contacts set date_modified=GREATEST(UTC_TIMESTAMP(), date_modified + INTERVAL 1 SECOND), modified_user_id='1' where id=%s", (rid,))
                return
            extra = ", amount_usdollar=%s" if field == "amount" else ""
            args = [value] + ([value] if field == "amount" else []) + [rid]
            cur.execute(f"update {self.TABLE[kind]} set `{field}`=%s{extra}, date_modified=GREATEST(UTC_TIMESTAMP(), date_modified + INTERVAL 1 SECOND), "
                        "modified_user_id='1' where id=%s", args)

    def on_start(self) -> list:
        return self.schedule.on_start() if self.schedule else []

    def before_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.before_commit(step) if self.schedule else []

    def after_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.after_commit(step) if self.schedule else []

    # ---- dynamic external update (v1 single event) -------------------------------------------------
    def after_delivery(self, step: int, obs: dict, facts: list) -> list:
        if self.schedule is not None:
            return self.schedule.after_delivery(step, facts, self._business_rows() != self._initial_business)
        d = self.dynamic
        if not d or self.dynamic_state["eligible_step"] is not None:
            return []
        seen_old = any(f["object"] == d["object"] and f["field"] == d["field"] and f["source"] == "edit"
                       and number(f["value"]) == number(d["old"]) for f in facts)
        if not seen_old:
            return []
        if self._business_rows() != self._initial_business:
            self.dynamic_state["eligible_step"] = "after_business_change"
            return [{"kind": "ext", "status": "not_eligible_business_change_already_recorded", "step": step}]
        self.dynamic_state["eligible_step"] = step
        if d["condition"] != "external_update":
            return [{"kind": "ext", "status": "eligible_no_write_control", "step": step, "object": d["object"]}]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select amount from opportunities where id='pair-opp-renewal'")
            before = number(cur.fetchone()[0])
            if before != number(d["old"]):
                return [{"kind": "ext", "status": "receipt_mismatch", "step": step, "before": before}]
            cur.execute("update opportunities set amount=%s, amount_usdollar=%s where id='pair-opp-renewal' and amount=%s",
                        (d["new"], d["new"], d["old"]))
            cur.execute("select amount from opportunities where id='pair-opp-renewal'")
            after = number(cur.fetchone()[0])
        receipt = {"kind": "ext", "status": "fired", "step": step, "object": d["object"], "field": d["field"],
                   "before": str(int(before)), "after": str(int(after)), "t": time.time(), "after_delivery_of_step": step}
        self.dynamic_state.update(fired=True, receipt=receipt)
        return [receipt]

    # ---- guard support ---------------------------------------------------------------------------
    GUARD_JS = r"""(id) => {
      const el = document.querySelector(`[data-pair-id="${id}"]`);
      if (!el) return null;
      const form = el.form || el.closest('form');
      if (!form || form.id !== 'EditView') return null;
      const label = ((el.value || '') + ' ' + (el.innerText || '')).toLowerCase();
      const isSave = el.id === 'SAVE' || el.id === 'SAVE_HEADER' || el.id === 'SAVE_FOOTER' || /\bsave\b/.test(label);
      if (!isSave) return null;
      const rec = form.querySelector('input[name=record]'); const mod = form.querySelector('input[name=module]');
      return {record: rec ? rec.value : '', module: mod ? mod.value : ''};
    }"""

    async def guard_target(self, page, act: dict, obs: dict):
        if act["type"] != "click":
            return None
        info = await page.evaluate(self.GUARD_JS, str(act["id"]))
        if not info or not info["record"]:
            return None
        obj = ID_TO_KEY.get(info["record"], f"{info['module'].lower()}:{info['record']}")
        return obj, f"{self.inst.origin}/index.php?module={info['module']}&action=DetailView&record={info['record']}"

    # ---- v2 validation family support -------------------------------------------------------------
    GUARD_COVERAGE_V2 = "SuiteCRM Save on a record edit form (Contacts, Opportunities, Accounts, Notes); inline edits are not covered"
    METADATA_FIELDS = ("date_modified", "date_entered", "modified_by_name", "created_by_name")
    FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS = FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS

    @staticmethod
    def _record_from_url(url: str):
        from urllib.parse import parse_qs, unquote, urlparse
        q = parse_qs(urlparse(url).query)
        if "ajaxUILoc" in url:
            q = parse_qs(urlparse(unquote(url.split("ajaxUILoc=", 1)[1])).query)
        return (q.get("module") or [None])[0], (q.get("action") or [None])[0], (q.get("record") or [None])[0]

    def link_targets(self, obs: dict) -> dict:
        out = {}
        for c in obs.get("controls", []):
            if not c.get("href") or "record" not in c["href"]:
                continue
            module, action, record = self._record_from_url(c["href"])
            if record in ID_TO_KEY and module and action == "DetailView":
                out[ID_TO_KEY[record]] = f"{self.inst.origin}/index.php?module={module}&action=DetailView&record={record}"
        return out

    def object_label(self, obj: str) -> str:
        return OBJECT_LABEL.get(obj, obj)

    def condition_ref(self, obj: str, field, url):
        module, _, record = self._record_from_url(url) if url else (None, None, None)
        if not record:
            record = self.KEY_TO_ID.get(obj)
            module = {"opportunity": "Opportunities", "contact": "Contacts", "note": "Notes", "account": "Accounts"}.get(obj.split(":")[0])
        return {"module": module, "record": record, "field": field} if record and module else None

    async def commit_target(self, page, act: dict, obs: dict):
        act = enter_as_click(act, obs)
        if act["type"] != "click":
            return None
        info = await page.evaluate(self.GUARD_JS, str(act["id"]))
        if not info or not info["record"]:
            return None
        obj = ID_TO_KEY.get(info["record"], f"{info['module'].lower()}:{info['record']}")
        base = f"{self.inst.origin}/index.php?module={info['module']}&record={info['record']}&action="
        return {"object": obj, "url": base + "DetailView", "edit_url": base + "EditView", "kind": "record", "form": True}

    def commit_like(self, act: dict, obs: dict) -> bool:
        c = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), None)
        return bool(c and re.search(r"\bsave\b", (c["label"] + " " + c.get("value", "")).lower()))


# ---- evaluator-side translation ------------------------------------------------------------------
def same_value(field: str, a, b) -> bool:
    return CrmAdapter.same(field, a, b)

def _email_of(state: dict, addr_id) -> str | None:
    row = state.get("email_addresses", {}).get(str(addr_id)) or {}
    return row.get("email_address")


def describe_mutation(m: dict, state_before: dict, state_after: dict) -> dict:
    t, key = m["table"], m["key"]
    if t == "email_addr_bean_rel":     # a contact's email: SuiteCRM unlinks the old address row and links a new one
        row = (state_after.get(t, {}).get(key) or state_before.get(t, {}).get(key) or m.get("row") or {})
        obj = ID_TO_KEY.get(row.get("bean_id"), f"{(row.get('bean_module') or 'bean').lower()}:{row.get('bean_id')}")
        addr = _email_of(state_after, row.get("email_address_id")) or _email_of(state_before, row.get("email_address_id"))
        if m["op"] == "insert":
            return {"object": obj, "field": "email", "before": None, "after": addr, "op": "link"}
        if m["op"] == "update" and m["column"] == "deleted":
            return {"object": obj, "field": "email", "before": addr, "after": None, "op": "unlink"}
        return {"object": obj, "field": "email_" + str(m.get("column")), "before": m.get("before"), "after": m.get("after"), "op": m["op"]}
    if t == "accounts_contacts":
        row = (state_after.get(t, {}).get(key) or state_before.get(t, {}).get(key) or m.get("row") or {})
        obj = ID_TO_KEY.get(row.get("contact_id"), f"contact:{row.get('contact_id')}")
        if m["op"] == "update" and m["column"] == "account_id":
            return {"object": obj, "field": "account", "before": ACCOUNT_NAME.get(m["before"], m["before"]),
                    "after": ACCOUNT_NAME.get(m["after"], m["after"]), "op": "update"}
        return {"object": obj, "field": "account_link", "before": m.get("before"), "after": m.get("after"), "op": m["op"]}
    obj = ID_TO_KEY.get(key, f"{t}:{key[:40]}")
    if m["op"] == "update":
        if m["column"] == "deleted" and str(m["after"]) == "1":
            return {"object": obj, "field": "record", "before": "exists", "after": "deleted", "op": "delete"}
        return {"object": obj, "field": m["column"], "before": m["before"], "after": m["after"], "op": "update"}
    return {"object": obj, "field": "record", "before": None if m["op"] == "insert" else "row", "after": "row" if m["op"] == "insert" else None,
            "op": m["op"], "row_name": (m.get("row") or {}).get("name")}


def terminal_summary(state: dict, initial: dict | None = None) -> dict:
    contacts = {ID_TO_KEY.get(k, k): {"phone_work": r.get("phone_work"), "title": r.get("title"), "deleted": r.get("deleted"),
                                      "description": r.get("description") or None, "department": r.get("department") or None}
                for k, r in state.get("contacts", {}).items()}
    links = {}
    for r in state.get("accounts_contacts", {}).values():
        if str(r.get("deleted")) == "0":
            links[ID_TO_KEY.get(r["contact_id"], r["contact_id"])] = ACCOUNT_NAME.get(r["account_id"], r["account_id"])
    emails = {}
    for r in state.get("email_addr_bean_rel", {}).values():
        if str(r.get("deleted")) == "0" and r.get("bean_module") == "Contacts" and str(r.get("primary_address")) == "1":
            emails[ID_TO_KEY.get(r["bean_id"], r["bean_id"])] = (state.get("email_addresses", {}).get(str(r["email_address_id"])) or {}).get("email_address")
    for k, v in contacts.items():
        v["account"] = links.get(k)
        v["email"] = emails.get(k)
    opps = {ID_TO_KEY.get(k, k): {"amount": number(r.get("amount")), "sales_stage": r.get("sales_stage"),
                                  "probability": number(r.get("probability")), "date_closed": r.get("date_closed"),
                                  "name": r.get("name"), "deleted": r.get("deleted"), "description": r.get("description") or None,
                                  "next_step": r.get("next_step") or None, "lead_source": r.get("lead_source") or None}
            for k, r in state.get("opportunities", {}).items()}
    summary = {"contacts": contacts, "opportunities": opps,
               "notes": {ID_TO_KEY.get(k, k): {"description": r.get("description"), "deleted": r.get("deleted")} for k, r in state.get("notes", {}).items()}}
    if initial is not None:
        for t in ("calls", "meetings", "tasks", "leads", "cases", "documents", "accounts"):
            summary[f"{t}_rows_delta"] = len(state.get(t, {})) - len(initial.get(t, {}))
    return summary
