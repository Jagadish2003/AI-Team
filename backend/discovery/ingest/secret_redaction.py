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

Safety over recall precision — but not at the cost of the corpus
----------------------------------------------------------------
A false positive redacts a non-secret (mildly reducing retrieval text); a false
negative leaks a credential into a searchable index. When the two trade off, this
scanner leans toward redacting. BUT the generic ``key = value`` rule additionally
requires the value to actually *look* like a credential (a symbol-bearing token or
a mixed letter+digit blob) — a variable merely *named* like a credential but
assigned ordinary code (``token = next_item``, ``password = get_input()``,
``secret = response.json``) is left untouched, so legitimate source is not
silently corrupted before indexing (AT-531 review).

It NEVER logs or returns the matched secret value — only the pattern-type name and
match counts — so a redaction is observable in run health without re-leaking the
secret it just removed.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Pattern, Tuple

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


# ---------------------------------------------------------------------------
# Value-shape gate for the generic "<secret-named key> = <value>" rule
# ---------------------------------------------------------------------------
# The key name (password / api_key / token / …) is a NECESSARY but not sufficient
# signal: real code assigns credential-named variables ordinary values all the
# time. Before redacting a value we require it to look like an actual credential,
# so a benign RHS is never silently rewritten to [REDACTED:...] in the corpus.

# Literal keywords that are never credentials.
_VALUE_LITERALS = frozenset(
    {"true", "false", "none", "null", "nil", "undefined", "yes", "no"}
)
# A bare number / decimal / underscore- or comma-grouped number.
_NUMERIC_VALUE = re.compile(r"^[+-]?\d[\d_,]*(?:\.\d+)?$")
# An ISO-8601 date or timestamp (the ``password_changed_at = 2026-...`` case).
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d(?:[ T][\d:.,+\-Zz]*)?$")
# A template / interpolation placeholder: ${VAR}, {{var}}, %(name)s, <value>, $VAR.
_PLACEHOLDER_VALUE = re.compile(
    r"^(?:[$%]?\{.*\}|%\([^)]*\)s|<[^>]+>|\$[A-Za-z_][A-Za-z0-9_]*)$"
)
# A plain identifier, dotted attribute access, or function call — i.e. code, not a
# secret: current_user, response.json, get_input(), datetime.now().
_ATTR_OR_CALL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:\(.*\))?$")


def _looks_like_secret_value(raw: str) -> bool:
    """True when an assignment RHS looks like a credential rather than ordinary code.

    Guards the generic ``key = value`` rule (AT-531 review): a variable *named*
    like a credential but assigned a benign value — ``token = next_item``,
    ``password = get_input()``, ``secret = response.json``, ``retries = 5``,
    ``changed_at = 2026-06-20`` — is NOT redacted. Only values that actually look
    secret-like are: a symbol-bearing token (``sk-live-…``, ``s3cr3t-p@ss``) or a
    mixed letter+digit blob (``hunter2xyz``, ``AbCd1234``). A single-case all-letter
    identifier is treated as code, matching the reviewer's guidance that the RHS
    must "look like a credential (not a regular identifier, number, or timestamp)".
    """
    v = raw.strip().strip("\"'")
    if len(v) < 6:
        return False
    if v.lower() in _VALUE_LITERALS:
        return False
    if (
        _NUMERIC_VALUE.match(v)
        or _ISO_TIMESTAMP.match(v)
        or _PLACEHOLDER_VALUE.match(v)
    ):
        return False
    # A dotted attribute access or a function call is code, never a secret.
    if ("." in v or "(" in v) and _ATTR_OR_CALL.match(v):
        return False
    has_lower = any(c.islower() for c in v)
    has_upper = any(c.isupper() for c in v)
    has_digit = any(c.isdigit() for c in v)
    # A symbol other than the identifier underscore (-, /, +, @, =, ., …) signals a
    # token / URL / credential rather than a plain identifier.
    has_symbol = any((not c.isalnum()) and c != "_" for c in v)
    class_count = has_lower + has_upper + has_digit
    return has_symbol or (has_digit and class_count >= 2)


@dataclass(frozen=True)
class _SecretPattern:
    """One named secret signature.

    ``regex`` locates the secret; ``value_group`` is the capture group holding the
    portion to replace (0 = the whole match — used for self-delimiting tokens like
    an AWS key; 2 = the value of a ``key = value`` assignment, so the key name and
    surrounding punctuation survive as context). ``value_predicate``, when set, is
    called with the captured value group and must return True for a redaction to
    happen — the gate that keeps the generic assignment rule from redacting benign
    code (AT-531 review).
    """

    name: str
    regex: Pattern[str]
    value_group: int = 0
    value_predicate: Optional[Callable[[str], bool]] = None


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
    # JSON Web Token — three base64url segments with realistic minimum lengths so a
    # short dotted identifier that happens to start with 'eyJ' (e.g. 'eyJa.b.c') is
    # not mistaken for a token (AT-531 review). Header ≥10, payload ≥20, sig ≥10.
    _SecretPattern(
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\b"
        ),
    ),
    # Generic "<secret-named key> = <value>" assignment. The KEY NAME is the
    # signature (password/secret/token/api_key/…); the VALUE (group 2) is redacted
    # ONLY when it looks like a credential (``value_predicate`` below), so a benign
    # value assigned to a credential-named variable is left untouched. The key +
    # operator + quoting is kept so the code still reads sensibly.
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
        value_predicate=_looks_like_secret_value,
    ),
)


@dataclass
class RedactionOutcome:
    """The result of scanning one piece of text.

    ``text`` is the (possibly) redacted content. ``matches`` records one pattern-
    type name PER redaction, in scan order (internal accounting for ``count``) —
    never the secret values. Callers should read the derived views:

      * :attr:`count`         — total number of secrets redacted;
      * :attr:`pattern_types` — the DISTINCT signature names that fired, sorted,
                                safe to log/record directly (AT-531 review: the
                                field no longer mis-reports N duplicates as N types);
      * :attr:`counts_by_type`— per-type counts, when a breakdown is wanted.
    """

    text: str
    matches: List[str] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.matches)

    @property
    def count(self) -> int:
        return len(self.matches)

    @property
    def pattern_types(self) -> List[str]:
        """Distinct pattern-type names that fired, sorted — deduplicated so a caller
        can record them directly without needing its own ``set()``."""
        return sorted(set(self.matches))

    @property
    def counts_by_type(self) -> Dict[str, int]:
        return dict(Counter(self.matches))


def _apply(pattern: _SecretPattern, text: str) -> Tuple[str, int]:
    """Apply one pattern to ``text``; return the redacted text and match count.

    A ``value_group`` pattern replaces only that group (keeping the key name /
    quoting as context); when a ``value_predicate`` is set, a match whose value
    fails the predicate is left entirely unchanged and NOT counted. A whole-match
    pattern (``value_group == 0``) replaces the full match. Counting is done in the
    replacer (not via ``subn``) because a predicate-rejected match must not count.
    """
    placeholder = _placeholder(pattern.name)
    count = 0

    def _repl(m: "re.Match") -> str:
        nonlocal count
        if pattern.value_group:
            value = m.group(pattern.value_group)
            if value is None:
                return m.group(0)
            if pattern.value_predicate and not pattern.value_predicate(value):
                return m.group(0)  # named like a secret, but the value is benign
            count += 1
            whole = m.group(0)
            rel_start = m.start(pattern.value_group) - m.start(0)
            rel_end = m.end(pattern.value_group) - m.start(0)
            return whole[:rel_start] + placeholder + whole[rel_end:]
        count += 1
        return placeholder

    return pattern.regex.sub(_repl, text), count


def scan_and_redact(text: str) -> RedactionOutcome:
    """Redact every recognised secret signature in ``text``.

    Applies each pattern in order (most-specific first), replacing matches with a
    typed placeholder. Returns the redacted text plus, per redaction, the pattern-
    type name that fired. Empty/blank text is returned unchanged with no
    redactions. The returned outcome never contains a secret value.
    """
    if not text:
        return RedactionOutcome(text=text or "")

    working = text
    matches: List[str] = []
    for pat in _PATTERNS:
        working, n = _apply(pat, working)
        if n:
            matches.extend([pat.name] * n)
    return RedactionOutcome(text=working, matches=matches)
