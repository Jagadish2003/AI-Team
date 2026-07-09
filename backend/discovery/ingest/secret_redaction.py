"""R18-A2 / AT-531 — secret-pattern scan + redaction before substrate hand-off.

Repository content commonly contains committed credentials. Before ANY content
reaches the retrieval substrate, this scanner redacts key/token/password
signatures so a leaked credential is never indexed into a retrievable store
(R18-A2 §1, "Redact before index, always"). Indexing a secret would turn a
discovery tool into a secret-search engine.

Content-source-agnostic on purpose
-----------------------------------
The scanner operates on plain text and knows nothing about Git. Every content
producer that feeds ``retrieval.ingest.ingest_content`` — Git file bodies and
commit messages today (AT-531 deps T2 + T4); Slack/Confluence/SharePoint later —
can redact through the SAME choke point instead of reinventing detection. The Git
connector wires it into its unconditional ``_secret_scan`` seam.

Safety over recall precision
----------------------------
A false positive redacts a non-secret (mildly reducing retrieval text); a false
negative leaks a credential into a searchable index. When the two trade off, this
scanner redacts. It NEVER logs or returns the matched secret value — only the
pattern-type name and match counts — so a redaction is observable in run health
without re-leaking the secret it just removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Pattern, Tuple

#: How many characters of a matched secret can survive redaction: none. The whole
#: match (or, for a ``key = value`` assignment, the value) is replaced by a typed
#: placeholder so the surrounding text stays intelligible for retrieval while the
#: credential itself is gone.
_PLACEHOLDER_FMT = "[REDACTED:{name}]"


def _placeholder(name: str) -> str:
    return _PLACEHOLDER_FMT.format(name=name)


# A value already collapsed to a placeholder must never be re-matched (that would
# double-redact and inflate the count). Every value-capturing pattern guards on
# this negative lookahead.
_NOT_PLACEHOLDER = r"(?!\[REDACTED:)"


@dataclass(frozen=True)
class _SecretPattern:
    """One named secret signature.

    ``regex`` locates the secret; ``value_group`` is the capture group holding the
    portion to replace (0 = the whole match — used for self-delimiting tokens like
    an AWS key; 2 = the value of a ``key = value`` assignment, so the key name and
    surrounding punctuation survive as context).
    """

    name: str
    regex: Pattern[str]
    value_group: int = 0


# ---------------------------------------------------------------------------
# The signature set — key / token / password (R18-A2 §1, AT-531)
# ---------------------------------------------------------------------------
# Ordered most-specific first so a provider token is caught by its own precise
# rule before the broad "key = value" assignment rule sees it. Every pattern is
# anchored on a real signature (a provider prefix, a PEM banner, or a secret-named
# assignment key) — never bare high-entropy guessing, which would redact hashes,
# UUIDs and content SHAs indiscriminately.
_PATTERNS: Tuple[_SecretPattern, ...] = (
    # PEM private key blocks (RSA/EC/OPENSSH/DSA/PGP or bare "PRIVATE KEY").
    _SecretPattern(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
            r".*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # AWS access key id.
    _SecretPattern("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # GitHub personal-access / OAuth / app / refresh tokens and fine-grained PATs.
    _SecretPattern(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    # Slack tokens (bot/user/app/refresh/legacy).
    _SecretPattern("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Google API key.
    _SecretPattern("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # JSON Web Token (three base64url segments).
    _SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    ),
    # Generic "<secret-named key> = <value>" assignment. The KEY NAME is the
    # signature (password/secret/token/api_key/…); the VALUE (group 2) is redacted,
    # the key + operator + quoting kept so the code still reads sensibly.
    _SecretPattern(
        "secret_assignment",
        re.compile(
            r"(?i)\b("
            r"passwd|password|pwd|"
            r"secret(?:[_-]?key)?|"
            r"api[_-]?key|apikey|"
            r"access[_-]?key|"
            r"auth[_-]?token|access[_-]?token|refresh[_-]?token|bearer[_-]?token|"
            r"client[_-]?secret|"
            r"token"
            r")\b\s*[:=]\s*['\"]?" + _NOT_PLACEHOLDER + r"([^\s'\"]{6,})['\"]?",
        ),
        value_group=2,
    ),
)


@dataclass
class RedactionOutcome:
    """The result of scanning one piece of text.

    ``text`` is the (possibly) redacted content. ``pattern_types`` lists one
    pattern-type name PER redaction in scan order — never the secret values — so a
    caller can record what kind of secret was removed and how many, without ever
    handling the secret again.
    """

    text: str
    pattern_types: List[str] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.pattern_types)

    @property
    def count(self) -> int:
        return len(self.pattern_types)


def _replace_group(placeholder: str, group: int) -> Callable[[re.Match], str]:
    """Build a ``re.sub`` replacement that swaps only ``group`` for ``placeholder``.

    For ``group == 0`` the whole match is replaced. For a value group, the prefix
    (key name, operator, opening quote) and any suffix (closing quote) are kept so
    the redaction leaves readable ``password = [REDACTED:secret_assignment]``.
    """

    def _repl(m: "re.Match") -> str:
        if group == 0 or m.group(group) is None:
            return placeholder
        whole = m.group(0)
        rel_start = m.start(group) - m.start(0)
        rel_end = m.end(group) - m.start(0)
        return whole[:rel_start] + placeholder + whole[rel_end:]

    return _repl


def scan_and_redact(text: str) -> RedactionOutcome:
    """Redact every recognised secret signature in ``text``.

    Applies each pattern in order (most-specific first), replacing matches with a
    typed placeholder. Returns the redacted text plus the list of pattern-type
    names that fired (one per match). Empty/blank text is returned unchanged with
    no redactions. The returned outcome never contains a secret value.
    """
    if not text:
        return RedactionOutcome(text=text or "")

    working = text
    hits: List[str] = []
    for pat in _PATTERNS:
        placeholder = _placeholder(pat.name)
        working, n = pat.regex.subn(
            _replace_group(placeholder, pat.value_group), working
        )
        if n:
            hits.extend([pat.name] * n)
    return RedactionOutcome(text=working, pattern_types=hits)
