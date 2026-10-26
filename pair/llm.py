"""Client for the locally served OpenAI-compatible vLLM endpoint.

Every request and response is returned verbatim to the caller for logging. Structured output
is requested through vLLM's `structured_outputs` field (JSON schema); the server's decoding
backend is recorded with each response.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class Endpoint:
    url: str = "http://127.0.0.1:18500/v1/chat/completions"
    model: str = "pair-actor"
    timeout: int = 300
    extra_body: dict = field(default_factory=dict)
    # chat templates that require strict user/assistant alternation (Mistral-Nemo): consecutive messages of one role are sent as one
    # message, joined by a blank line; the content is unchanged
    merge_consecutive: bool = False

    @property
    def base(self) -> str:
        p = urlsplit(self.url)
        return f"{p.scheme}://{p.netloc}"


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _shape(ep: Endpoint, messages: list) -> list:
    if not ep.merge_consecutive:
        return messages
    out = []
    for m in messages:
        if out and out[-1]["role"] == m["role"] and m["role"] != "system":
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(dict(m))
    return out


def chat(ep: Endpoint, messages: list, *, max_tokens: int, temperature: float, seed: int,
         schema: dict | None = None, top_p: float = 1.0) -> dict:
    """One chat completion. Returns {"request", "response", "seconds"}; raises on HTTP failure."""
    body = {"model": ep.model, "messages": _shape(ep, messages), "max_tokens": max_tokens, "temperature": temperature,
            "top_p": top_p, "seed": seed, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False}, **ep.extra_body}
    if schema is not None:
        # Compact JSON only: free whitespace let the grammar wander into degenerate strings.
        body["structured_outputs"] = {"json": schema, "disable_any_whitespace": True}
    start = time.monotonic()
    response = _post(ep.url, body, ep.timeout)
    return {"request": body, "response": response, "seconds": time.monotonic() - start}


def count_tokens(ep: Endpoint, messages: list) -> int:
    """Prompt length as the server's tokenizer and chat template count it."""
    body = {"model": ep.model, "messages": _shape(ep, messages), "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False}}
    result = _post(ep.base + "/tokenize", body, 60)
    value = result.get("count")
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid tokenizer count {value!r}")
    return value


def served_models(ep: Endpoint) -> list:
    with urllib.request.urlopen(ep.base + "/v1/models", timeout=20) as resp:
        return json.load(resp)["data"]
