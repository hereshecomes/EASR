"""Shared fact semantics for the ledger and the oracle.

An external write changes a stored field; the agent reads a public fact. Most are the same (price, amount, a note's
text). A product's stock is read as its availability: the availability class (in stock / unavailable) is the fact
that a decision depends on, so a stock change is also an availability change exactly when the class changes.
"""
from __future__ import annotations

import re

AVAIL = re.compile(r"\b(out of stock|unavailable|in stock)\b", re.I)


def availability_class(v):
    m = AVAIL.search(str(v or ""))
    return {"out of stock": "unavailable"}.get(m.group(1).lower(), m.group(1).lower()) if m else None


def ext_facts(x: dict) -> list:
    """(fact key, value after) pairs an external write changes."""
    out = [(f"{x['object']}.{x['field']}", x.get("after"))]
    if x.get("object", "").startswith("product:") and x.get("field") == "stock":
        try:
            n = int(x.get("after"))
            out.append((f"{x['object']}.availability", "In stock" if n > 0 else "Unavailable"))
        except (TypeError, ValueError):
            pass
    return out


def same(a, b) -> bool:
    try:
        return float(str(a).replace("$", "").replace(",", "")) == float(str(b).replace("$", "").replace(",", ""))
    except ValueError:
        pass
    ca, cb = availability_class(a), availability_class(b)
    if ca or cb:
        return ca == cb
    return " ".join(str(a).split()).lower() == " ".join(str(b).split()).lower()
