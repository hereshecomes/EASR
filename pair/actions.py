"""Actor action interface shared by every actor policy.

The model must answer with one JSON object {"note": str, "action": {...}}. Constrained decoding
enforces the outer shape; `parse_reply` is the declared conservative fallback for any reply that
is not exactly one JSON object, and `check_action` validates the action against the current
observation. Nothing here guesses intent: zero or several candidate objects stay invalid.
"""
from __future__ import annotations

import json
from urllib.parse import urljoin

ACTION_TYPES = ["click", "fill", "select", "press", "goto", "back", "scroll", "wait",
                "accept_dialog", "dismiss_dialog", "stop"]
KEYS = ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space"]
NOTE_MAX = 400

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string", "maxLength": NOTE_MAX},
        "action": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ACTION_TYPES},
                "id": {"type": "string", "pattern": "^[0-9]{1,4}$"},
                "text": {"type": "string", "maxLength": 300},
                "value": {"type": "string", "maxLength": 200},
                "key": {"type": "string", "enum": KEYS},
                "url": {"type": "string", "maxLength": 300},
                "direction": {"type": "string", "enum": ["down", "up"]},
                "answer": {"type": "string", "maxLength": 600},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    },
    "required": ["note", "action"],
    "additionalProperties": False,
}

REQUIRED = {"click": ["id"], "fill": ["id", "text"], "select": ["id", "value"], "press": ["id", "key"],
            "goto": ["url"], "back": [], "scroll": ["direction"], "wait": [], "accept_dialog": [],
            "dismiss_dialog": [], "stop": ["answer"]}


class InvalidReply(ValueError):
    """A reply that cannot be turned into exactly one action; .reason is shown to the model."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _top_level_objects(text: str) -> list[str]:
    """Balanced top-level {...} spans, respecting JSON strings."""
    spans, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = depth > 0
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(text[start:i + 1])
    return spans


def parse_reply(content: str) -> tuple[dict, str]:
    """Return (reply, parse_mode). parse_mode is 'exact' or 'envelope'; raises InvalidReply."""
    text = (content or "").strip()
    try:
        obj = json.loads(text)
        mode = "exact"
    except ValueError:
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.rsplit("```", 1)[0]
        objects = _top_level_objects(text)
        if not objects:
            raise InvalidReply("no JSON object found")
        if len(objects) > 1:
            raise InvalidReply(f"{len(objects)} JSON objects found; send exactly one")
        try:
            obj = json.loads(objects[0])
        except ValueError as exc:
            raise InvalidReply(f"malformed JSON object: {exc}") from None
        mode = "envelope"
    if not isinstance(obj, dict):
        raise InvalidReply("reply must be a JSON object")
    if set(obj) - {"note", "action"} or "action" not in obj:
        raise InvalidReply('reply must have exactly the keys "note" and "action"')
    action = obj["action"]
    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
        raise InvalidReply("action.type must be one of " + ", ".join(ACTION_TYPES))
    missing = [k for k in REQUIRED[action["type"]] if not str(action.get(k, "")).strip()]
    if missing:
        raise InvalidReply(f"action {action['type']} needs field(s): {', '.join(missing)}")
    note = obj.get("note", "")
    if not isinstance(note, str):
        raise InvalidReply("note must be a string")
    return {"note": note[:NOTE_MAX], "action": action}, mode


def check_action(action: dict, obs: dict, seen_links: set | None = None) -> str | None:
    """Reject actions that refer to things the current observation does not show.

    Returns the absolute URL for a goto (relative links are resolved against the page URL).
    A goto may use the current URL, a link on the current page, or a link shown on any page
    already delivered to the model in this episode (seen_links); invented URLs stay invalid.
    """
    kind = action["type"]
    controls = {c["id"]: c for c in obs.get("controls", [])}
    if obs.get("dialog") and kind not in ("accept_dialog", "dismiss_dialog"):
        raise InvalidReply("a browser dialog is open; use accept_dialog or dismiss_dialog")
    if not obs.get("dialog") and kind in ("accept_dialog", "dismiss_dialog"):
        raise InvalidReply("no browser dialog is open")
    if kind in ("click", "fill", "select", "press"):
        control = controls.get(str(action["id"]))
        if control is None:
            raise InvalidReply(f"control id {action['id']} is not in the current page")
        if kind == "select":
            if not control.get("options"):          # the observation records options=None for controls that are not dropdowns
                raise InvalidReply(f"control {action['id']} is not a dropdown")
            values = [o["value"] for o in control["options"]]
            if action["value"] not in values:
                raise InvalidReply(f"option {action['value']!r} is not offered by control {action['id']}")
        if kind == "fill" and control["tag"] not in ("input", "textarea"):
            raise InvalidReply(f"control {action['id']} is not a text field")
    if kind == "goto":
        visible = {obs["url"]} | {c.get("href") for c in obs.get("controls", []) if c.get("href")} | (seen_links or set())
        target = urljoin(obs["url"], action["url"])
        if target not in visible:
            raise InvalidReply("goto URL must be the current URL or a link shown on a page you have seen")
        return target
    return None
