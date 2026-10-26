"""Development aid: for every EASR evaluation operation, open the page where its 'pre' events trigger and check that the trigger fact
(object, field, source) is delivered; also check that the watched keys can be read."""
import asyncio, json
from playwright.async_api import async_playwright
from pair import easr_eval as E, worker

PAGES = {k: o['edit_path'] for k, o in E.OPS.items()}


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        instances = {}
        for op_id, path in PAGES.items():
            a = E.assignment(op_id, "clean", "B0_vanilla", E.arms_spec(), 42)
            if a["app"] == "breakingweb":
                from pair.apps.breakingweb import BwSession
                instances["breakingweb"] = BwSession(a["build_spec"]["env"])
            inst = worker.instance_for(a["app"], 47, instances)
            inst.build(a["build_spec"]); inst.start()
            try:
                ad = worker.make_adapter(a["app"], 47, a, instances)
                for k, v in a["setup"]:
                    ad.write_value(*k.rsplit(".", 1), v)
                vals = {k: ad.read_value(*k.rsplit(".", 1)) for k in a["easr"]["watch"]}
                ctx = await b.new_context(); await ctx.route("**/*", ad.route_handler); await ad.prepare_context(ctx)
                page = await ctx.new_page()
                url = inst.url(path) if a["app"] == "breakingweb" else inst.origin + path
                await page.goto(url, wait_until="networkidle" if a["app"] == "breakingweb" else "domcontentloaded")
                if a["app"] == "crm":
                    await page.wait_for_selector("form#EditView input[name=record]", state="attached", timeout=20000)
                facts = await ad.public_facts(page, None)
                obj, field, src = E.OPS[op_id]["pre"]
                hit = [f for f in facts if f["object"] == obj and f["field"] == field and f["source"] == src]
                o = E.OPS[op_id]
                rm = o.get("read_map") or {}
                tgt0 = rm.get(str(vals[o["target"]]), vals[o["target"]])
                dep_ok = (not o["dep"]) or E.norm(vals[o["dep"]]) == E.norm(o["dep0"])
                want0 = E.rule_value(o["rule"], o.get("dep0"))
                change = E.norm(tgt0) != E.norm(want0)
                print(f"{op_id:26s} trigger={'OK' if hit else 'MISSING'} dep0={'OK' if dep_ok else 'MISMATCH ' + str(vals.get(o['dep']))} "
                      f"change_needed={change} target0={str(tgt0)[:25]!r} want0={str(want0)[:25]!r} untouched0={str(vals[o['untouched'][0]])[:20]!r}"
                      + ("" if hit else f" facts={sorted({(f['object'], f['field'], f['source']) for f in facts})[:10]}"), flush=True)
                await ctx.close()
            finally:
                inst.stop()
        await b.close()

asyncio.run(main())
