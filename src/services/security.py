"""Input sanitizer + content screens for CMN-C2-281 (outer backbone pre_process).

Pure, stateless domain helpers (NOT framework gate methods). Four concerns:

- ``sanitize_query``: strip HTML markup and cap length before the raw caller
  text is JSON-serialized and handed to the inner SmartHR workflow graph.
- ``EMPLOYEE_CODE_RE`` / ``EMPLOYEE_LABEL_RE`` / ``FIELD_KEY_RE`` /
  ``FIELD_VALUE_RE``: the bounded, inert shapes a caller-supplied employee
  code, display name and employee-data field must match before they are
  accepted (docs/02_design.md, "Caller-data contract"). Every one of these
  values renders into the confirmation the caller reads back and into the
  assembled SmartHR request body, so free text here would be
  caller-controlled output injection. The charset is deliberately inert -
  letters in any script (Japanese employee names and department titles pass
  unchanged), digits, underscore, space, hyphen, dot, comma, parentheses and
  the Japanese middle dot; no markup, quoting, path, colon or control
  characters.
- ``finite_int_in_range``: the parser every caller-controlled number goes
  through - rejects bools, non-numerics, NaN/Infinity, and out-of-range
  magnitudes instead of letting them fail open downstream.
- ``find_injection``: the template-owned prompt-injection screen. The template
  owns this guarantee itself rather than relying on any upstream gate being
  active: chat-template control tokens (``<|im_start|>``, ``[INST]``,
  ``<<SYS>>``, forged role tags) and instruction/role-override phrasing are
  refused in the node that owns the caller contract. Text is normalized first
  (URL-decoding, NFKC, zero-width strip) so escaped or homoglyph variants of
  the same payload do not slip past the patterns.

The screen and the markup strip are ORDERED deliberately: ``find_injection``
runs on the RAW text and again on the sanitized text. ``sanitize_query``
removes ``<...>`` runs, which swallows a control token whole and would forward
its directive residue as ordinary-looking prose; screening before the strip
catches the token, and screening after it catches a directive that only
re-assembles once interleaved markup is removed.
"""

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from typing import Any

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# Inert charset for every caller string that renders into the confirmation or
# into the assembled SmartHR request body. `\w` is Unicode-aware, so Japanese
# employee names (山田 太郎) and department titles (営業部) pass unchanged while
# markup, quoting, path, colon and control characters cannot.
_INERT_CHARS = "\\w \\-.,()\\uff08\\uff09\\u30fb"

# A caller-supplied SmartHR employee code (emp_code). Bounded and inert; the
# shape is deliberately wider than the API's own emp_code so a tenant that
# issues longer codes is not refused at the door.
EMPLOYEE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# A caller-supplied employee display name / record label.
EMPLOYEE_LABEL_RE = re.compile(rf"^[{_INERT_CHARS}]{{1,100}}$")

# A caller-supplied employee-data field name and its value.
FIELD_KEY_RE = re.compile(rf"^[{_INERT_CHARS}]{{1,40}}$")
FIELD_VALUE_RE = re.compile(rf"^[{_INERT_CHARS}]{{1,200}}$")

# Structural cap on the employee-data fields one request may carry.
MAX_EMPLOYEE_FIELDS = 20

# Bounds for a caller-supplied employee code delivered as a JSON number, and
# for a numeric employee-data field value (grade, headcount, years of service).
# These are structural sanity bounds, not HR policy: they exist so a non-finite
# or absurd magnitude is refused at the door rather than written to a record.
CODE_NUMBER_MIN = 0
CODE_NUMBER_MAX = 10**15
FIELD_NUMBER_MIN = -(10**12)
FIELD_NUMBER_MAX = 10**12

# Bounds for the SmartHR-call deadline (seconds) declared in config/config.yaml.
TIMEOUT_MIN = 1
TIMEOUT_MAX = 600

_ZERO_WIDTH_RE = re.compile("[\\u200b\\u200c\\u200d\\ufeff\\u00ad]")

# Template-owned injection patterns. Token forms first: a chat-template control
# token is an attack marker regardless of surrounding phrasing, and phrase-only
# screens miss it entirely. Phrases are limited to high-confidence forms so
# legitimate HR text ("disregard the earlier draft record", "she will act as a
# new team lead") is unaffected.
_INJECTION_PATTERNS: "tuple[tuple[str, re.Pattern[str]], ...]" = (
    ("chat_template_token", re.compile(r"<\|[A-Za-z0-9_]{1,32}\|>")),
    ("chat_template_token", re.compile(r"\[/?(?:INST|SYS)\]", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"<\s*/?(?:system|assistant)\s*>", re.IGNORECASE)),
    (
        "instruction_override",
        re.compile(
            r"(?:ignore|disregard)\s+(?:all\s+|the\s+)?(?:previous|above|prior)\s+"
            r"(?:instructions?|prompts?|context|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(r"\bignore\s+all\s+(?:rules?|instructions?)\b", re.IGNORECASE),
    ),
    # Role-override phrasing is anchored on the SYSTEM-role target, not on the
    # verb: "act as a ..." is ordinary HR language in this domain (a promotion
    # request literally says "she will act as a new team lead"), so a screen
    # that fires on the verb alone would refuse the work this template exists
    # to do. Requiring an assistant/model noun keeps the attack forms and
    # leaves personnel roles alone.
    (
        "role_override",
        re.compile(
            r"\b(?:act|behave|respond|reply)\s+as\s+(?:a|an|the)\s+(?:[A-Za-z]+\s+){0,2}"
            r"(?:ai|llm|language\s+model|model|assistant|chatbot|bot|dan)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\byou\s+are\s+now\s+(?:a|an|the)\s+(?:[A-Za-z]+\s+){0,2}"
            r"(?:ai|llm|language\s+model|model|assistant|chatbot|bot|dan)\b",
            re.IGNORECASE,
        ),
    ),
    ("role_override", re.compile(r"\b(?:developer|jailbreak|dan)\s+mode\b", re.IGNORECASE)),
)


def _normalize(text: str) -> str:
    """Undo common obfuscation (URL-encoding, homoglyphs, zero-width chars) before scanning."""
    text = urllib.parse.unquote(text)
    text = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH_RE.sub("", text)


def find_injection(text: str) -> "list[str]":
    """Return the injection pattern types found in ``text`` ([] = clean).

    Callers refuse on any finding; the finding NAMES the pattern type only -
    the matched text is never included, so nothing hostile is ever echoed.
    """
    normalized = _normalize(text)
    found: list[str] = []
    for pattern_type, pattern in _INJECTION_PATTERNS:
        if pattern_type not in found and pattern.search(normalized):
            found.append(pattern_type)
    return found


def screen_text(text: str) -> "list[str]":
    """Screen a caller string BOTH raw and after the markup strip.

    A markup strip is not a refusal and can make an attack harder to see: it
    removes ``<|im_start|>`` silently and forwards the directive that followed
    it as ordinary text, and it can splice ``ig<b>nore all rules`` back into a
    matchable phrase. Screening both representations catches the token before
    it is eaten and the phrase after it re-assembles.
    """
    findings = find_injection(text)
    for finding in find_injection(sanitize_query(text)):
        if finding not in findings:
            findings.append(finding)
    return findings


def is_non_finite_spelling(value: str) -> bool:
    """True when a caller STRING is exactly a non-finite float spelling.

    Employee-data field values are labels, not numbers - this template makes no
    numeric decision on them - so a string is normally accepted as text. The
    one exception is a value that IS a non-finite float spelling ("NaN",
    "-Infinity", "inf"): the template writes field values into an external
    system of record, where later readers we do not control will parse them,
    and a stored NaN compares False against every bound it is ever checked
    against. The match is on the WHOLE stripped value, so ordinary labels that
    merely contain those letters ("Nancy", "Infinity Branch") are unaffected.
    """
    return value.strip().lower().lstrip("+-") in {"nan", "inf", "infinity"}


def finite_int_in_range(value: Any, minimum: int, maximum: int) -> "int | None":
    """Parse a caller-controlled number into a bounded int, or None (refuse).

    Fail closed: bools, non-numeric types, non-numeric strings, NaN and
    +/-Infinity (both the float objects and their JSON/string spellings),
    non-integral floats, and out-of-range magnitudes are all refused. NaN is
    the sharp edge: it parses cleanly via float() and every comparison against
    it is False, so an unchecked NaN would silently disable the exact bound
    this parser exists to enforce.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    result = int(number)
    if result < minimum or result > maximum:
        return None
    return result


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML tags (injection/markup guard) and cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]
