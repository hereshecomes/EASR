"""One actor episode: a fresh browser context, one world, one policy.

Per step: snapshot -> render -> model call (JSON-schema decoding) -> delivery hooks (dynamic
events fire here, after the model has answered and before the action runs) -> optional guard ->
action -> backend state diff, even if the action errored or timed out. Everything delivered to
or returned by the model is written to the episode folder; nothing is replaced afterwards.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import pathlib
import time
from dataclasses import asdict, dataclass

from . import actions, llm, observe, prompts
from .apps.common import RESTORE_JS


class _Scripted(Exception):
    """Internal: skip the model service for scripted engineering fixtures."""


@dataclass
class RunConfig:
    policy: str
    seed: int
    temperature: float = 0.2
    max_steps: int = 40
    max_tokens: int = 512
    context_window: int = 32768
    episode_seconds: int = 900
    action_timeout_ms: int = 10000
    text_chars: int = 8000
    controls: int = 150
    label_chars: int = 120
    max_consecutive_invalid: int = 5
    screenshots: bool = True


class Episode:
    def __init__(self, cfg: RunConfig, endpoint: llm.Endpoint, adapter, out_dir: pathlib.Path,
                 goal: str, rules: list[str], guard=None, scripted=None, watch=None):
        self.cfg, self.ep, self.adapter, self.guard = cfg, endpoint, adapter, guard
        self.watch = watch         # evaluator-side probe: callable() -> {key: stored value}, read after every executed action
        self.scripted = scripted   # engineering fixtures only: callable(step, obs) -> reply text; no model call
        self.out = pathlib.Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=False)
        self.goal, self.rules = goal, rules
        self.history = []          # per completed step: (obs_stub, assistant_text, result_text)
        self.seen_links = set()    # links on pages already delivered to the model
        self.repeat = {"key": None, "count": 0}   # interface v2: identical consecutive actions without a stored change
        self.dialog = None
        self._discarding_draft = False
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "model_seconds": 0.0}
        self.counts = {"invalid_replies": 0, "envelope_parses": 0, "guard_holds": 0, "guard_passes": 0,
                       "action_errors": 0, "external_events": 0}

    # ---- recording -------------------------------------------------------------------------
    def _write(self, name: str, record: dict) -> None:
        with (self.out / name).open("a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_request(self, record: dict) -> None:
        with gzip.open(self.out / "requests.jsonl.gz", "at") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- browser -----------------------------------------------------------------------------
    def _on_dialog(self, dialog):
        if self._discarding_draft and dialog.type == "beforeunload":
            asyncio.ensure_future(dialog.accept())      # guard hold: leaving the stale draft is the declared effect
            return
        self.dialog = dialog

    async def _guard_navigate(self, page, url: str) -> str:
        self._discarding_draft = True
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return "ok"
        except Exception as exc:
            return "error: " + repr(exc)[:200]
        finally:
            self._discarding_draft = False

    async def _snapshot(self, page) -> dict:
        if self.dialog is not None:
            return {"url": page.url, "title": "", "controls": [], "text": "",
                    "dialog": {"type": self.dialog.type, "message": self.dialog.message},
                    "limits": {"native_dialog": True}}
        for attempt in range(3):
            try:
                return await page.evaluate(observe.SNAPSHOT_JS, {"text": self.cfg.text_chars, "controls": self.cfg.controls,
                                                                 "label": self.cfg.label_chars})
            except Exception as exc:
                # the page may still be navigating when it is observed (an accepted dialog the page answers by navigating does
                # not wait for the load): wait for the new document and observe that one
                if attempt == 2 or "Execution context was destroyed" not in str(exc):
                    raise
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=self.cfg.action_timeout_ms)
                except Exception:
                    pass

    async def _execute(self, page, act: dict, obs: dict, goto_url: str | None) -> dict:
        kind = act["type"]
        if kind in ("accept_dialog", "dismiss_dialog"):
            dialog, self.dialog = self.dialog, None
            await (dialog.accept() if kind == "accept_dialog" else dialog.dismiss())
            return {"status": "ok"}
        if kind in ("click", "fill", "select", "press"):
            el = page.locator(f'[data-pair-id="{act["id"]}"]')
            if await el.count() != 1:
                raise RuntimeError("control is no longer on the page")
            if kind == "click":
                await el.click()
            elif kind == "fill":
                await el.fill(act["text"])
            elif kind == "select":
                await el.select_option(act["value"])
            else:
                await el.press(act["key"])
        elif kind == "goto":
            await page.goto(goto_url, wait_until="domcontentloaded")
        elif kind == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif kind == "scroll":
            await page.mouse.wheel(0, 700 if act["direction"] == "down" else -700)
        elif kind == "wait":
            await asyncio.sleep(1.0)
        if kind != "stop":
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=self.cfg.action_timeout_ms)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return {"status": "ok"}

    # ---- model context -----------------------------------------------------------------------
    def _messages(self, current_text: str) -> list:
        msgs = [{"role": "system", "content": prompts.system_prompt(self.cfg.policy)},
                {"role": "user", "content": prompts.task_message(self.goal, self.rules)}]
        for obs_stub, assistant_text, result_text in self.history:
            msgs.append({"role": "user", "content": obs_stub})
            msgs.append({"role": "assistant", "content": assistant_text})
            msgs.append({"role": "user", "content": result_text})
        msgs.append({"role": "user", "content": current_text})
        return msgs

    # ---- main loop ---------------------------------------------------------------------------
    async def run(self, browser, start_url: str) -> dict:
        start = time.monotonic()
        status, termination, answer = "agent_failure", "step_budget", ""
        meta = {"config": asdict(self.cfg), "endpoint": {"url": self.ep.url, "model": self.ep.model},
                "goal": self.goal, "rules": self.rules, "start_url": start_url,
                "system_prompt": prompts.system_prompt(self.cfg.policy), "schema": actions.REPLY_SCHEMA,
                "adapter": self.adapter.describe(), "guard": self.guard.describe() if self.guard else None,
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        (self.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        await context.route("**/*", self.adapter.route_handler)
        await self.adapter.prepare_context(context)
        if self.guard is not None:
            await self.guard.setup(browser)
            if hasattr(self.guard, "attach"):          # request-level controllers watch the agent's own browser traffic
                await self.guard.attach(context)
        (self.out / "initial_state.json").write_text(json.dumps(self.adapter.state(), ensure_ascii=False, default=str))
        start_events = self.adapter.on_start() if hasattr(self.adapter, "on_start") else []
        if start_events:
            self._write("steps.jsonl", {"start_events": start_events, "t": time.time()})
        page = await context.new_page()
        page.on("dialog", self._on_dialog)
        page.set_default_timeout(self.cfg.action_timeout_ms)
        consecutive_invalid = 0
        try:
            await page.goto(start_url, wait_until="domcontentloaded")
            for step in range(self.cfg.max_steps):
                if time.monotonic() - start > self.cfg.episode_seconds:
                    termination = "walltime_budget"
                    break
                rec = {"step": step, "t_obs": time.time()}
                obs = await self._snapshot(page)
                text = observe.render(obs)
                rec["obs"] = {k: obs.get(k) for k in ("url", "title", "limits", "dialog")}
                rec["obs"]["controls"] = obs.get("controls", [])
                rec["obs"]["rendered"] = text
                rec["facts"] = await self.adapter.public_facts(page, obs)
                if self.cfg.screenshots and not obs.get("dialog"):
                    try:
                        await page.screenshot(path=str(self.out / f"step_{step:03d}.jpg"), type="jpeg", quality=40)
                    except Exception as exc:
                        rec["screenshot_error"] = repr(exc)
                messages = self._messages(text)
                if self.scripted is not None:
                    content = self.scripted(step, obs)
                    call = {"request": {}, "response": {"choices": [{"message": {"content": content}, "finish_reason": "scripted"}], "usage": {}}, "seconds": 0.0}
                    prompt_tokens = 0
                try:
                    if self.scripted is not None:
                        raise _Scripted()
                    prompt_tokens = await asyncio.to_thread(llm.count_tokens, self.ep, messages)
                except _Scripted:
                    pass
                except Exception as exc:
                    rec["infra_error"] = "tokenizer: " + repr(exc)
                    self._write("steps.jsonl", rec)
                    status, termination = "infra_failure", "tokenizer_error"
                    break
                rec["prompt_tokens"] = prompt_tokens
                if prompt_tokens + self.cfg.max_tokens > self.cfg.context_window:
                    rec["delivered"] = False
                    self._write("steps.jsonl", rec)
                    termination = "context_budget"
                    break
                try:
                    if self.scripted is not None:
                        raise _Scripted()
                    call = await asyncio.to_thread(llm.chat, self.ep, messages, max_tokens=self.cfg.max_tokens,
                                                   temperature=self.cfg.temperature, seed=self.cfg.seed * 1000 + step,
                                                   schema=actions.REPLY_SCHEMA)
                except _Scripted:
                    pass
                except Exception as exc:
                    rec["delivered"] = "unconfirmed"
                    rec["infra_error"] = "model: " + repr(exc)
                    self._write("steps.jsonl", rec)
                    status, termination = "infra_failure", "model_service_error"
                    break
                self._write_request({"step": step, "messages": messages, "request": {k: v for k, v in call["request"].items() if k != "messages"},
                                     "response": call["response"]})
                choice = call["response"]["choices"][0]
                content = choice["message"].get("content") or ""
                usage = call["response"].get("usage", {})
                self.usage["calls"] += 1
                self.usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
                self.usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
                self.usage["model_seconds"] += call["seconds"]
                rec["delivered"] = True
                rec["model"] = {"content": content, "finish_reason": choice.get("finish_reason"), "usage": usage,
                                "seconds": round(call["seconds"], 3)}
                rec["external_events"] = self.adapter.after_delivery(step, obs, rec["facts"])
                self.counts["external_events"] += len(rec["external_events"])
                if self.guard is not None:
                    self.guard.observe(step, rec["facts"], obs)
                obs_stub = observe.stub(step, obs)
                self.seen_links |= {obs["url"]} | {c["href"] for c in obs.get("controls", []) if c.get("href")}
                try:
                    reply, mode = actions.parse_reply(content)
                    if self.scripted is not None and reply["action"]["type"] == "goto":
                        # Engineering fixtures may open any same-origin URL directly; model agents may not.
                        self.seen_links.add(reply["action"]["url"])
                    goto_url = actions.check_action(reply["action"], obs, self.seen_links)
                    if goto_url is None and reply["action"]["type"] == "goto":
                        goto_url = reply["action"]["url"]
                except actions.InvalidReply as exc:
                    self.counts["invalid_replies"] += 1
                    consecutive_invalid += 1
                    rec["parse"] = {"valid": False, "reason": exc.reason}
                    result_text = (f"RESULT: your reply was not executed: {exc.reason}. Reply with exactly one JSON object "
                                   'with keys "note" and "action".')
                    self.history.append((obs_stub, "[invalid reply omitted]", result_text))
                    self._write("steps.jsonl", rec)
                    if consecutive_invalid >= self.cfg.max_consecutive_invalid:
                        termination = "invalid_reply_limit"
                        break
                    continue
                consecutive_invalid = 0
                if mode == "envelope":
                    self.counts["envelope_parses"] += 1
                act = reply["action"]
                rec["parse"] = {"valid": True, "mode": mode}
                rec["note"], rec["action"] = reply["note"], act
                assistant_text = json.dumps(reply, ensure_ascii=False)
                decision = None
                if self.guard is not None and act["type"] != "stop":
                    before = self.adapter.state()
                    decision = await self.guard.check(act, obs, page, context)
                    rec["guard"] = decision
                    rec["guard_mutations"] = self.adapter.diff(before, self.adapter.state())
                    if decision["decision"] == "hold":
                        self.counts["guard_holds"] += 1
                        nav = await self._guard_navigate(page, decision["url"])
                        result_text = "RESULT: " + decision["message"]
                        rec["exec"] = {"status": "held_by_guard", "guard_navigation": nav, "url_after": page.url}
                        self.history.append((obs_stub, assistant_text, result_text))
                        self._write("steps.jsonl", rec)
                        continue
                    if decision["decision"] == "pass":
                        self.counts["guard_passes"] += 1
                    if decision["decision"] == "check_failed":
                        self.counts["guard_check_failures"] = self.counts.get("guard_check_failures", 0) + 1
                if (act["type"] != "stop" and hasattr(self.adapter, "before_commit") and self.adapter.commit_like(act, obs)):
                    race = self.adapter.before_commit(step, act, obs)
                    if race:
                        rec.setdefault("external_events", []).extend(race)
                        self.counts["external_events"] += len(race)
                before = self.adapter.state()
                t0 = time.time()
                try:
                    if act["type"] == "stop":
                        result = {"status": "ok"}
                    else:
                        result = await asyncio.wait_for(self._execute(page, act, obs, goto_url),
                                                        timeout=self.cfg.action_timeout_ms / 1000 + 5)
                except Exception as exc:
                    self.counts["action_errors"] += 1
                    result = {"status": "error", "error": repr(exc)[:300]}
                after = self.adapter.state()
                if self.watch is not None:
                    try:
                        rec["watched"] = self.watch()
                    except Exception as exc:
                        rec["watched_error"] = repr(exc)[:200]
                if (act["type"] != "stop" and result.get("status") == "ok" and hasattr(self.adapter, "after_commit")
                        and self.adapter.commit_like(act, obs)):
                    later = self.adapter.after_commit(step, act, obs)      # after the stored outcome was read, before any verification
                    if later:
                        rec.setdefault("external_events", []).extend(later)
                        self.counts["external_events"] += len(later)
                rec["mutations"] = self.adapter.diff(before, after)
                post_notice = None
                if decision is not None and (decision.get("withheld_fields") or decision.get("conditional")):
                    try:
                        restored = await page.evaluate(RESTORE_JS)
                        if restored:
                            rec["guard_restored_inputs"] = restored
                    except Exception:
                        pass
                if (decision is not None and decision["decision"] == "pass" and result["status"] == "ok"
                        and hasattr(self.guard, "post_commit")):
                    try:
                        pc = await self.guard.post_commit()
                    except Exception as exc:
                        pc = {"error": repr(exc)[:300]}
                    if pc:
                        rec["post_check"] = pc
                        post_notice = pc.get("notice")
                rec["exec"] = {**result, "seconds": round(time.time() - t0, 3), "url_after": page.url}
                public = {"status": result["status"]}
                if result["status"] == "error":
                    public["detail"] = result["error"].split("\n")[0][:200]
                control = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), {})
                key = (obs.get("url"), act["type"], control.get("label"), control.get("href"), act.get("text"), act.get("value"), act.get("url"))
                stored = any(m["class"] in ("business", "unlisted") for m in rec["mutations"])
                self.repeat = {"key": key, "count": self.repeat["count"] + 1} if key == self.repeat["key"] and not stored else {"key": key, "count": 1}
                if self.repeat["count"] >= 3 and act["type"] != "stop":
                    public["notice"] = (f"You have performed this same action {self.repeat['count']} times in a row on this page "
                                        "and nothing was saved; it may not do what you expect.")
                    rec["repeat_notice"] = self.repeat["count"]
                if post_notice:
                    public["notice"] = post_notice + (" " + public["notice"] if public.get("notice") else "")
                result_text = "RESULT: " + json.dumps(public)
                self.history.append((obs_stub, assistant_text, result_text))
                self._write("steps.jsonl", rec)
                if act["type"] == "stop":
                    status, termination, answer = "completed", "agent_stopped", act["answer"]
                    break
        except Exception as exc:
            status, termination = "infra_failure", "runner_error"
            self._write("steps.jsonl", {"runner_error": repr(exc)[:1000], "t": time.time()})
        finally:
            for closer in ((self.guard.close,) if self.guard is not None else ()) + (context.close,):
                try:
                    await closer()
                except Exception:
                    pass
        final_state = self.adapter.state()
        summary = {"status": status, "termination": termination, "answer": answer,
                   "seconds": round(time.monotonic() - start, 2), "usage": self.usage, "counts": self.counts,
                   "final_state": final_state, "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        (self.out / "episode.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        return summary
