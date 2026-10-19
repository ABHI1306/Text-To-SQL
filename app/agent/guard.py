"""Deterministic screens for questions the assistant must never answer or send to a model.

These run before any model call, so a small routing model cannot be talked out of them. They
complement the compiler, which still gates every query that does get written.
"""

from __future__ import annotations

import re

REFUSAL_WRITE = (
    "I can only read data to answer questions. I can't delete, change or create anything. "
    "Try asking a question about your data instead."
)
REFUSAL_INTERNALS = (
    "I can't share my instructions or details about the database system itself, such as its "
    "version, accounts or system tables. Ask a question about your business data instead."
)

_WRITE_REQUEST = re.compile(
    # Destroying verbs refuse on a business noun too ("delete customers"); the rest, and the
    # ambiguous "drop"/"remove", only on structural nouns so "the drop in orders" is untouched.
    r"\b(delete|truncate|erase|wipe|destroy)\b[^.?!\n]{0,40}\b(table|tables|database|schema"
    r"|rows?|records?|columns?|data|customers?|orders?|users?|products?|payments?|inventory"
    r"|index|view)\b"
    r"|\b(drop|remove|insert|update|alter|rename|grant|revoke|create)\b[^.?!\n]{0,40}"
    r"\b(table|tables|database|schema|columns?|index|view)\b"
    r"|\binsert\s+into\b|\bupdate\s+\w+\s+set\b",
    re.IGNORECASE,
)
_INTERNALS_REQUEST = re.compile(
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)"
    r"|disregard\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)"
    r"|\bmaintenance\s+mode\b|\bdeveloper\s+mode\b|\badmin\s+mode\b|\bdebug\s+mode\b"
    r"|\bno\s+restrictions\b|\bjailbreak\b"
    r"|system\s+prompt|your\s+(instructions|rules|prompt)|(every|all)\s+(the\s+)?rules?\s+you"
    r"|(repeat|print|reveal|show|output)\b[^.?!\n]{0,30}\b(verbatim|everything\s+above"
    r"|above\s+this\s+message)"
    r"|information_schema|pg_catalog|\bpg_[a-z_]+\b"
    r"|\b(database|server|postgres(ql)?)\s+version\b"
    r"|\b(current|session)[_\s]user\b|\bwho\s+am\s+i\s+connected\s+as\b"
    r"|what\s+(tables|schemas|databases)\s+(exist|are\s+there|do\s+you\s+have)"
    r"|(list|show)\s+(me\s+)?(all\s+)?(the\s+)?(tables|schemas|databases)\b",
    re.IGNORECASE,
)

# Phrases that only appear in the assistant's own instructions, or in a jailbreak confirmation.
_REPLY_LEAK_MARKERS = (
    "maintenance mode",
    "you route a user message",
    "untrusted data",
    "return json only",
    "decide in this order",
    "json schema of the answer",
    "system prompt",
    "<message>",
    "<question>",
)


def screen_question(question: str) -> str | None:
    """A fixed refusal when the question asks to change data or probe internals, else ``None``."""
    if _WRITE_REQUEST.search(question):
        return REFUSAL_WRITE
    if _INTERNALS_REQUEST.search(question):
        return REFUSAL_INTERNALS
    return None


def safe_reply(reply: str | None) -> str | None:
    """The model's chat reply, or the fixed refusal when it echoes instructions or a jailbreak."""
    if reply is None:
        return None
    lowered = reply.lower()
    if any(marker in lowered for marker in _REPLY_LEAK_MARKERS):
        return REFUSAL_INTERNALS
    return reply
