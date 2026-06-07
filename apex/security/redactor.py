"""Redactor — fail-closed PII/secret scrubbing before any log or context inject.

Implements INV-5 (§8.3). Borrows Agent System's audit pattern: replace with a
non-reversible fingerprint that records the *type* and a hint hash, so events
stay auditable without exposing the secret. On internal error, fail closed
(return a fully-masked string rather than risk leaking).
"""
from __future__ import annotations

import hashlib
import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai_key",  re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("groq_key",    re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("aws_key",     re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer",      re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}")),
    ("github_pat",  re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("email",       re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone_cn",    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("home_path",   re.compile(r"/Users/[^/\s]+")),
    ("home_path_linux", re.compile(r"/home/[^/\s]+")),
]


def _fingerprint(kind: str, value: str) -> str:
    h = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"«REDACTED:{kind}:{h}»"


def redact(text: str) -> str:
    """Scrub secrets/PII. Fail closed on any error."""
    if text is None:
        return ""
    try:
        out = text
        for kind, pat in _PATTERNS:
            out = pat.sub(lambda m, k=kind: _fingerprint(k, m.group(0)), out)
        return out
    except Exception:
        # fail-closed: never leak on redactor failure
        return "«REDACTED:error:00000000»"


def redact_dict(d: dict) -> dict:
    """Recursively redact string values in a payload dict."""
    def _walk(v):
        if isinstance(v, str):
            return redact(v)
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_walk(x) for x in v]
        return v
    return _walk(d)
