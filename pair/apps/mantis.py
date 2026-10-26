"""MantisBT 2.28.4 issue tracker (suite D4, self-hosted substitute for WorkArena): instances, harness login,
backend snapshots, public facts, external updates by another user, evaluator-side translation."""
from __future__ import annotations

import calendar
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.request

import pymysql

from .common import diff_tables, kill_group, make_route_handler, port_free, FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS, enter_as_click
from ..dynamics import EventSchedule

ROOT = pathlib.Path(__file__).resolve().parents[2]
RT = pathlib.Path(os.environ.get("PAIR_RT", "/tmp/pair_rebuild_rt"))
PHP = ROOT / "env/php/bin/php"
MARIADB = ROOT / "apps/crm/mariadb.sh"
TEMPLATE_WWW = RT / "mantis_template/www"
TEMPLATE_SQL = RT / "mantis_template.sql"
FIXTURE = json.loads((ROOT / "tasks/fixtures/mantis_records.json").read_text())
DB = {"host": "127.0.0.1", "port": 18400, "user": "pair", "password": "pair-test-db-2026"}
PORT_BASE = 18600
# Ids are assigned in fixture order by the seed (apps/mantis/seed.php) and frozen in the template.
ISSUE_ID = {i["key"]: n + 1 for n, i in enumerate(FIXTURE["issues"])}
ID_ISSUE = {v: k for k, v in ISSUE_ID.items()}
USER_ID = {u["key"]: n + 2 for n, u in enumerate(FIXTURE["users"])}          # 1 is the installer's administrator
USER_NAME = {USER_ID[u["key"]]: u["username"] for u in FIXTURE["users"]}
USER_NAME.update({1: "administrator", 0: ""})
CUSTOM_ID = {c["key"]: n + 1 for n, c in enumerate(FIXTURE["custom_fields"])}
CUSTOM_KEY = {v: k for k, v in CUSTOM_ID.items()}
CATEGORY_ID = {c: n + 2 for n, c in enumerate(FIXTURE["categories"])}        # 1 is MantisBT's built-in "General" of project 0
CATEGORY_NAME = {v: k for k, v in CATEGORY_ID.items()}
PRIORITY = {10: "none", 20: "low", 30: "normal", 40: "high", 50: "urgent", 60: "immediate"}
SEVERITY = {10: "feature", 20: "trivial", 30: "text", 40: "tweak", 50: "minor", 60: "major", 70: "crash", 80: "block"}
STATUS = {10: "new", 20: "feedback", 30: "acknowledged", 40: "confirmed", 50: "assigned", 80: "resolved", 90: "closed"}
REPRO = {10: "always", 30: "sometimes", 50: "random", 70: "have not tried", 90: "unable to reproduce", 100: "N/A"}
ENUMS = {"priority": PRIORITY, "severity": SEVERITY, "status": STATUS, "reproducibility": REPRO}
BUG_COLS = {"summary", "priority", "severity", "status", "handler_id", "category_id", "due_date", "reproducibility", "resolution",
            "view_state", "eta", "projection", "platform", "os", "os_build", "version", "fixed_in_version", "target_version", "build"}
TEXT_COLS = {"description", "steps_to_reproduce", "additional_information"}
BACKGROUND_TABLES = {"mantis_tokens_table", "mantis_user_pref_table", "mantis_email_table", "mantis_config_table", "mantis_api_token_table",
                     "mantis_user_print_pref_table", "mantis_filters_table"}
METADATA_COLUMNS = {"last_updated", "last_visit", "login_count", "failed_login_count", "lost_password_request_count", "date_modified"}


def classify(table: str, column: str | None) -> str:
    if table in BACKGROUND_TABLES or (table == "mantis_user_table" and column in METADATA_COLUMNS):
        return "background"
    if table == "mantis_bug_history_table" or column in METADATA_COLUMNS:
        return "metadata"
    if table.startswith("mantis_"):
        return "business"
    return "unlisted"


def _plain(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def mysql(sql: str, db: str | None = None) -> None:
    subprocess.run([str(MARIADB), "client"] + ([db] if db else []) + ["-e", sql], check=True, capture_output=True, text=True)


def due_text(ts) -> str | None:
    if not ts or int(ts) <= 1:
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))


class MantisInstance:
    """One isolated issue tracker (own database, PHP server and world file)."""

    def __init__(self, slot: int):
        self.slot = slot
        self.port = PORT_BASE + slot
        self.dir = RT / f"mantis_{slot}"
        self.www = self.dir / "www"
        self.db = f"pair_mantis_{slot}"
        self.world_file = self.dir / "world.json"
        self.origin = f"http://127.0.0.1:{self.port}"
        self.proc = None

    def _ensure_code(self) -> None:
        if not self.www.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(TEMPLATE_WWW, self.www, symlinks=True)
        cfg = self.www / "config/config_inc.php"
        text = cfg.read_text()
        text = re.sub(r"\$g_database_name\s*=\s*'[^']*';", f"$g_database_name = '{self.db}';", text)
        cfg.write_text(text)
        ini = (ROOT / "apps/crm/php.ini.tmpl").read_text().replace("__DIR__", str(self.dir))
        (self.dir / "php.ini").write_text(ini)

    def stop(self) -> None:
        kill_group(self.proc)
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
        for key, fields in spec.get("issues", {}).items():           # world facts: {issue key: {field: public value}}
            for field, value in fields.items():
                write_issue_field(self.db, ISSUE_ID[key], field, value, user_id=1, history=False)
                applied.append(f"{key}.{field}={value}")
        world = {"world": spec["world"], "notice": spec.get("notice"), "native_occ": spec.get("native_occ", "on"), "app_tier": spec.get("app_tier", "none")}
        if spec.get("update_fields") is not None:                       # sensitivity supplement: update-page field set (write breadth)
            world["update_fields"] = spec["update_fields"]
        self.world_file.write_text(json.dumps(world, indent=1))
        return {"db": self.db, "applied": applied, "notice": bool(spec.get("notice"))}

    def start(self) -> None:
        if not port_free(self.port):
            raise RuntimeError(f"port {self.port} is already in use; refusing to start")
        env = {**os.environ, "PAIR_WORLD_FILE": str(self.world_file), "PHP_CLI_SERVER_WORKERS": "8", "PAIR_MANTIS_URL": self.origin + "/"}
        log = (self.dir / "server.log").open("a")
        self.proc = subprocess.Popen([str(PHP), "-c", str(self.dir / "php.ini"), "-S", f"127.0.0.1:{self.port}", "-t", str(self.www)],
                                     cwd=self.www, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.origin + "/login_page.php", timeout=5)
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited right after start")
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"Mantis instance {self.slot} did not start")

    def start_url(self, assignment: dict) -> str:
        return self.origin + "/my_view_page.php"


def _db(dbname):
    return pymysql.connect(database=dbname, autocommit=True, **DB)


def write_issue_field(dbname: str, bug_id: int, field: str, value, user_id: int = 3, history: bool = True) -> None:
    """Write one issue field the way another MantisBT user's save would: the value, last_updated, and a history row."""
    with _db(dbname) as conn, conn.cursor() as cur:
        cur.execute("select last_updated from mantis_bug_table where id=%s", (bug_id,))
        last = cur.fetchone()[0]
        now = max(int(time.time()), int(last) + 1)
        old = read_issue_field(dbname, bug_id, field)
        if field in TEXT_COLS:
            cur.execute(f"update mantis_bug_text_table t join mantis_bug_table b on b.bug_text_id=t.id set t.`{field}`=%s where b.id=%s", (value, bug_id))
        elif field in CUSTOM_ID:
            cur.execute("replace into mantis_custom_field_string_table (field_id, bug_id, value, text) values (%s, %s, %s, NULL)",
                        (CUSTOM_ID[field], bug_id, value))
        else:
            col, val = field, value
            if field in ENUMS:
                val = next(k for k, v in ENUMS[field].items() if v == str(value).lower())
            elif field == "handler":
                col, val = "handler_id", next((k for k, v in USER_NAME.items() if v == value), 0)
            elif field == "category":
                col, val = "category_id", CATEGORY_ID[value]
            elif field == "due_date":
                col, val = "due_date", calendar.timegm(time.strptime(value + " 17:00:00", "%Y-%m-%d %H:%M:%S")) if value else 1
            cur.execute(f"update mantis_bug_table set `{col}`=%s where id=%s", (val, bug_id))
        cur.execute("update mantis_bug_table set last_updated=%s where id=%s", (now, bug_id))
        if history:
            cur.execute("insert into mantis_bug_history_table (user_id, bug_id, field_name, old_value, new_value, type, date_modified) "
                        "values (%s, %s, %s, %s, %s, 0, %s)", (user_id, bug_id, field, str(old or ""), str(value or ""), now))


def read_issue_field(dbname: str, bug_id: int, field: str):
    with _db(dbname) as conn, conn.cursor() as cur:
        if field in TEXT_COLS:
            cur.execute(f"select t.`{field}` from mantis_bug_text_table t join mantis_bug_table b on b.bug_text_id=t.id where b.id=%s", (bug_id,))
            r = cur.fetchone()
            return _plain(r[0]) if r else None
        if field in CUSTOM_ID:
            cur.execute("select value from mantis_custom_field_string_table where field_id=%s and bug_id=%s", (CUSTOM_ID[field], bug_id))
            r = cur.fetchone()
            return _plain(r[0]) if r else None
        col = {"handler": "handler_id", "category": "category_id"}.get(field, field)
        cur.execute(f"select `{col}` from mantis_bug_table where id=%s", (bug_id,))
        r = cur.fetchone()
        if not r:
            return None
        v = r[0]
        if field in ENUMS:
            return ENUMS[field].get(int(v), str(v))
        if field == "handler":
            return USER_NAME.get(int(v), str(v))
        if field == "category":
            return CATEGORY_NAME.get(int(v), str(v))
        if field == "due_date":
            return due_text(v)
        return _plain(v)


USER_REAL = {n + 2: u["realname"] for n, u in enumerate(FIXTURE["users"])}
USER_REAL.update({1: "administrator", 0: ""})
CUSTOM_LABEL = {c["name"].lower(): c["key"] for c in FIXTURE["custom_fields"]}
FIELD_OF_CLASS = {"bug-category": "category", "bug-reporter": "reporter", "bug-assigned-to": "handler", "bug-priority": "priority",
                  "bug-severity": "severity", "bug-reproducibility": "reproducibility", "bug-status": "status", "bug-resolution": "resolution",
                  "bug-summary": "summary", "bug-description": "description", "bug-steps-to-reproduce": "steps_to_reproduce",
                  "bug-additional-information": "additional_information"}
FIELD_OF_INPUT = {"category_id": "category", "handler_id": "handler", "priority": "priority", "severity": "severity", "reproducibility": "reproducibility",
                  "status": "status", "resolution": "resolution", "summary": "summary", "description": "description",
                  "steps_to_reproduce": "steps_to_reproduce", "additional_information": "additional_information",
                  **{f"custom_field_{v}": k for k, v in CUSTOM_ID.items()}}
RESOLUTION = {10: "open", 20: "fixed", 30: "reopened", 40: "unable to reproduce", 50: "not fixable", 60: "duplicate", 70: "no change required",
              80: "suspended", 90: "won't fix"}
ENUMS["resolution"] = RESOLUTION


class MantisAdapter:
    app = "mantis"
    EASR_OMISSION_SAFE = True           # gpc_get_* defaults keep stored values; custom fields travel with their _presence flag
    FACT_FIELD = None                   # input name -> fact field (set below, after FIELD_OF_INPUT)

    def __init__(self, instance: MantisInstance, dynamic: dict | None = None):
        self.inst = instance
        self.blocked = []
        self.route_handler = make_route_handler({instance.origin}, self.blocked)
        self.dynamic = dynamic
        self.dynamic_state = {"eligible_step": None, "fired": False, "receipt": None}
        self._tables = None
        self._initial_business = None
        self.schedule = EventSchedule(dynamic["events"], self.write_value, self.read_value) if dynamic and "events" in dynamic else None

    def describe(self) -> dict:
        return {"app": "MantisBT 2.28.4", "origin": self.inst.origin, "slot": self.inst.slot, "dynamic": self.dynamic}

    async def prepare_context(self, context, record_initial: bool = True) -> None:
        u = FIXTURE["users"][0]
        page = await context.new_page()
        await page.goto(self.inst.origin + "/login_page.php", wait_until="domcontentloaded")
        await page.fill("#username", u["username"])
        await page.click("input[type=submit]")
        await page.wait_for_selector("#password", timeout=15000)
        await page.fill("#password", u["password"])
        await page.click("input[type=submit]")
        await page.wait_for_load_state("domcontentloaded")
        if "login" in page.url:
            raise RuntimeError("harness login failed")
        await page.close()
        if record_initial:
            self._initial_business = self._business_rows()

    # ---- evaluator-side backend ----------------------------------------------------------------
    def _connect(self):
        return _db(self.inst.db)

    def state(self) -> dict:
        snap = {}
        with self._connect() as conn, conn.cursor(pymysql.cursors.DictCursor) as cur:
            if self._tables is None:
                cur.execute("select table_name from information_schema.tables where table_schema=%s", (self.inst.db,))
                self._tables = sorted(r["table_name"] for r in cur.fetchall())
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
                    elif table == "mantis_custom_field_string_table":
                        key = f"{clean['field_id']}:{clean['bug_id']}"
                    else:
                        key = json.dumps(clean, sort_keys=True, default=str)
                    keyed[key] = clean
                snap[table] = keyed
        return snap

    def diff(self, before: dict, after: dict) -> list[dict]:
        return diff_tables(before, after, classify)

    def _business_rows(self):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select id, summary, priority, severity, status, handler_id, category_id, resolution, reproducibility from mantis_bug_table order by id")
            rows = cur.fetchall()
            cur.execute("select field_id, bug_id, value from mantis_custom_field_string_table order by field_id, bug_id")
            return (rows, cur.fetchall())

    # ---- public facts ------------------------------------------------------------------------------
    FACTS_JS = r"""() => {
      const t = s => (s || '').replace(/\s+/g, ' ').trim();
      const out = {record: null, source: null, fields: {}, custom: {}, list_rows: [], notice: null};
      const notice = document.querySelector('.pair-notice'); if (notice) out.notice = t(notice.innerText);
      const idCell = document.querySelector('td.bug-id');
      if (idCell) {
        out.source = 'record'; out.record = parseInt(t(idCell.innerText), 10);
        document.querySelectorAll('td[class^="bug-"]').forEach(td => { out.fields[td.className.split(' ')[0]] = t(td.innerText); });
        document.querySelectorAll('th.bug-custom-field').forEach(th => { const td = th.nextElementSibling; if (td) out.custom[t(th.innerText).toLowerCase()] = t(td.innerText); });
      }
      // the issue view page also carries small forms posting to bug_update.php (quick assign); the record view takes precedence
      const form = idCell ? null : [...document.querySelectorAll('form')].find(f => (f.getAttribute('action') || '').includes('bug_update.php'));
      if (form) {
        out.source = 'edit'; const id = form.querySelector('input[name=bug_id]'); out.record = id ? parseInt(id.value, 10) : null;
        form.querySelectorAll('input[name],select[name],textarea[name]').forEach(e => {
          if (['hidden', 'submit', 'button', 'checkbox'].includes(e.type)) return;
          if (e.tagName === 'SELECT') { const o = [...e.options].find(o => o.defaultSelected) || e.options[0]; out.fields[e.name] = o ? t(o.text) : ''; }
          else out.fields[e.name] = e.defaultValue;
        });
      }
      document.querySelectorAll('#buglist tbody tr, table#buglist tr').forEach(tr => {
        const a = tr.querySelector('a[href*="view.php?id="]');
        if (a) out.list_rows.push({record: parseInt(new URLSearchParams(a.href.split('?')[1]).get('id'), 10), text: t(tr.innerText).slice(0, 300)});
      });
      return out;
    }"""

    async def public_facts(self, page, obs) -> list[dict]:
        if obs is not None and obs.get("dialog"):
            return []
        raw = await page.evaluate(self.FACTS_JS)
        facts = []
        if raw["record"] and raw["source"]:
            obj = f"issue:{ID_ISSUE.get(raw['record'], raw['record'])}"
            if raw["source"] == "record":
                for cls, value in raw["fields"].items():
                    field = FIELD_OF_CLASS.get(cls)
                    if field:
                        if field == "summary":
                            value = re.sub(r"^\d+:\s*", "", value)
                        facts.append({"object": obj, "field": field, "value": value, "source": "record"})
                for label, value in raw["custom"].items():
                    if label in CUSTOM_LABEL:
                        facts.append({"object": obj, "field": CUSTOM_LABEL[label], "value": value, "source": "record"})
            else:
                for name, value in raw["fields"].items():
                    field = FIELD_OF_INPUT.get(name)
                    if field:
                        facts.append({"object": obj, "field": field, "value": value, "source": "edit"})
        for row in raw["list_rows"]:
            if row["record"] in ID_ISSUE:
                facts.append({"object": f"issue:{ID_ISSUE[row['record']]}", "field": "list_row", "value": row["text"], "source": "list"})
        if raw["notice"]:
            facts.append({"object": "page", "field": "notice", "value": raw["notice"], "source": "banner"})
        return facts

    # ---- external updates (another user) ------------------------------------------------------------------
    def read_value(self, obj: str, field: str):
        v = read_issue_field(self.inst.db, ISSUE_ID[obj.split(":", 1)[1]], field)
        if field == "handler":
            return next((USER_REAL[k] for k, n in USER_NAME.items() if n == v), v)
        return v

    def write_value(self, obj: str, field: str, value) -> None:
        if field == "handler":
            value = next((USER_NAME[k] for k, n in USER_REAL.items() if n == value), value)
        write_issue_field(self.inst.db, ISSUE_ID[obj.split(":", 1)[1]], field, value, user_id=USER_ID["colleague"])

    def on_start(self) -> list:
        return self.schedule.on_start() if self.schedule else []

    def before_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.before_commit(step) if self.schedule else []

    def after_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.after_commit(step) if self.schedule else []

    def after_delivery(self, step: int, obs: dict, facts: list) -> list:
        if self.schedule is None:
            return []
        return self.schedule.after_delivery(step, facts, self._business_rows() != self._initial_business)

    # ---- validator support (public information only) ---------------------------------------------------
    GUARD_COVERAGE_V2 = "MantisBT issue edit form (Update Information); quick status changes and notes are not covered"
    METADATA_FIELDS = ("reporter",)
    FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS = FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS
    GUARD_FIELDS = ("priority", "severity", "status", "handler", "category", "summary", "customer_account", "contract_tier")
    GUARD_SOURCES = ("record",)
    GUARD_COVERAGE = GUARD_COVERAGE_V2
    GUARD_JS = r"""(id) => {
      const el = document.querySelector(`[data-pair-id="${id}"]`);
      if (!el) return null;
      const form = el.form || el.closest('form');
      if (!form || !(form.getAttribute('action') || '').includes('bug_update.php')) return null;
      if (!['submit', 'button'].includes(el.type) && el.tagName !== 'BUTTON') return null;
      const id_ = form.querySelector('input[name=bug_id]');
      return {record: id_ ? parseInt(id_.value, 10) : null};
    }"""

    def link_targets(self, obs: dict) -> dict:
        out = {}
        for c in obs.get("controls", []):
            m = re.search(r"view\.php\?id=(\d+)", c.get("href") or "")
            if m and int(m.group(1)) in ID_ISSUE:
                out[f"issue:{ID_ISSUE[int(m.group(1))]}"] = f"{self.inst.origin}/view.php?id={m.group(1)}"
        return out

    def object_label(self, obj: str) -> str:
        key = obj.split(":", 1)[-1]
        return next((f"issue {ISSUE_ID[key]:07d} \"{i['summary']}\"" for i in FIXTURE["issues"] if i["key"] == key), obj)

    def condition_ref(self, obj: str, field, url):
        key = obj.split(":", 1)[-1]
        return {"module": "issue", "record": ISSUE_ID[key], "field": field} if key in ISSUE_ID else None

    async def commit_target(self, page, act: dict, obs: dict):
        act = enter_as_click(act, obs)
        if act["type"] != "click":
            return None
        info = await page.evaluate(self.GUARD_JS, str(act["id"]))
        if not info or not info.get("record"):
            return None
        rid = info["record"]
        return {"object": f"issue:{ID_ISSUE.get(rid, rid)}", "url": f"{self.inst.origin}/view.php?id={rid}",
                "edit_url": f"{self.inst.origin}/bug_update_page.php?bug_id={rid}", "kind": "record", "form": True}

    async def guard_target(self, page, act: dict, obs: dict):
        t = await self.commit_target(page, act, obs)
        return (t["object"], t["url"]) if t else None

    def commit_like(self, act: dict, obs: dict) -> bool:
        c = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), None)
        return bool(c and "bug_update.php" in (c.get("form_action") or "") and act["type"] in ("click", "press"))


# ---- evaluator-side translation --------------------------------------------------------------------------
def same_value(field: str, a, b) -> bool:
    return " ".join(str(a or "").split()).lower() == " ".join(str(b or "").split()).lower()


def describe_mutation(m: dict, state_before: dict, state_after: dict) -> dict:
    t, key = m["table"], m["key"]
    if t == "mantis_bug_table":
        obj = f"issue:{ID_ISSUE.get(int(key), key)}"
        if m["op"] != "update":
            return {"object": obj, "field": "record", "before": None, "after": "row", "op": m["op"]}
        col = m["column"]
        conv = {"handler_id": ("handler", lambda v: USER_REAL.get(int(v or 0), v)), "category_id": ("category", lambda v: CATEGORY_NAME.get(int(v or 0), v))}
        if col in conv:
            name, f = conv[col]
            return {"object": obj, "field": name, "before": f(m["before"]), "after": f(m["after"]), "op": "update"}
        if col in ENUMS:
            return {"object": obj, "field": col, "before": ENUMS[col].get(int(m["before"] or 0), m["before"]),
                    "after": ENUMS[col].get(int(m["after"] or 0), m["after"]), "op": "update"}
        return {"object": obj, "field": col, "before": m["before"], "after": m["after"], "op": "update"}
    if t == "mantis_bug_text_table":
        bug = next((b for b in (state_after.get("mantis_bug_table") or state_before.get("mantis_bug_table") or {}).values()
                    if str(b.get("bug_text_id")) == str(key)), None)
        obj = f"issue:{ID_ISSUE.get(int(bug['id']), bug['id'])}" if bug else f"bug_text:{key}"
        return {"object": obj, "field": m.get("column") or "text", "before": m.get("before"), "after": m.get("after"), "op": m["op"]}
    if t == "mantis_custom_field_string_table":
        fid, bid = key.split(":")
        row = m.get("row") or {}
        before = m.get("before") if m["op"] == "update" else (row.get("value") if m["op"] == "delete" else None)
        after = m.get("after") if m["op"] == "update" else (row.get("value") if m["op"] == "insert" else None)
        return {"object": f"issue:{ID_ISSUE.get(int(bid), bid)}", "field": CUSTOM_KEY.get(int(fid), f"custom_{fid}"),
                "before": before, "after": after, "op": m["op"]}
    if t == "mantis_bugnote_table" and m["op"] == "insert":
        row = m.get("row") or {}
        return {"object": f"issue:{ID_ISSUE.get(int(row.get('bug_id') or 0), row.get('bug_id'))}", "field": "note", "before": None, "after": "added", "op": "insert"}
    return {"object": f"table:{t}", "field": m.get("column", "row"), "before": m.get("before"), "after": m.get("after"), "op": m["op"]}


def terminal_summary(state: dict, initial: dict | None = None) -> dict:
    texts = state.get("mantis_bug_text_table", {})
    custom = state.get("mantis_custom_field_string_table", {})
    notes = {}
    for r in state.get("mantis_bugnote_table", {}).values():
        notes[int(r["bug_id"])] = notes.get(int(r["bug_id"]), 0) + 1
    issues = {}
    for r in state.get("mantis_bug_table", {}).values():
        bid = int(r["id"])
        tx = texts.get(str(r["bug_text_id"]), {})
        rec = {"summary": r["summary"], "priority": PRIORITY.get(int(r["priority"])), "severity": SEVERITY.get(int(r["severity"])),
               "status": STATUS.get(int(r["status"])), "resolution": RESOLUTION.get(int(r["resolution"])),
               "reproducibility": REPRO.get(int(r["reproducibility"])), "handler": USER_REAL.get(int(r["handler_id"]), r["handler_id"]),
               "category": CATEGORY_NAME.get(int(r["category_id"]), r["category_id"]),
               "description": " ".join((tx.get("description") or "").split()) or None,
               "steps_to_reproduce": " ".join((tx.get("steps_to_reproduce") or "").split()) or None,
               "additional_information": " ".join((tx.get("additional_information") or "").split()) or None,
               "notes": notes.get(bid, 0)}
        for k, fid in CUSTOM_ID.items():
            rec[k] = (custom.get(f"{fid}:{bid}") or {}).get("value")
        issues[f"issue:{ID_ISSUE.get(bid, bid)}"] = rec
    out = {"issues": issues}
    if initial is not None:
        out["issues_rows_delta"] = len(state.get("mantis_bug_table", {})) - len(initial.get("mantis_bug_table", {}))
    return out


MantisAdapter.FACT_FIELD = FIELD_OF_INPUT
