"""PII redaction for text that arrives in arbitrary chunks.

Streaming strategy
------------------
Each pattern declares ``max_len`` (the longest possible match) and ``partial`` (a regex that
matches any text that could still grow into a match when it sits at the very end of the buffer).
On every feed we find the earliest start of such a partial tail, emit everything before it
(with complete matches replaced), and keep only the rest. The held-back tail is therefore
normally just the trailing word or number, and never longer than ``WINDOW`` (the longest
possible match), so memory is O(WINDOW) and the first token is delayed by at most one word.

Correctness argument: a match that starts before the cut either ends before the end of the
buffer (so no later text can change it) or would itself be a partial tail, which we hold back.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

REDACTED = "[REDACTED]"


def luhn_valid(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class PiiPattern:
    name: str
    regex: str
    max_len: int
    partial: str
    validator: Callable[[str], bool] | None = None


# Add a new pattern here; nothing else needs to change.
PATTERNS: tuple[PiiPattern, ...] = (
    PiiPattern(
        name="email",
        regex=r"[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,4}[A-Za-z]{2,24}",
        max_len=64 + 1 + 4 * 64 + 24,
        partial=r"[A-Za-z0-9._%+-]{1,64}(?:@[A-Za-z0-9.-]{0,280})?",
    ),
    PiiPattern(
        name="ssn",
        regex=r"(?<!\d)(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}(?!\d)",
        max_len=11,
        partial=r"\d[\d -]{0,10}",
    ),
    PiiPattern(
        # Structured shapes (4-4-4-x, 4-6-4/5, plain 13-19 digits) with one consistent
        # separator. A loose "digits and separators" run would swallow neighbouring numbers,
        # fail Luhn as a whole, and leave a real card unredacted.
        name="credit_card",
        regex=(
            r"(?<!\d)(?:"
            r"\d{4}(?P<sep_a>[ -]?)\d{4}(?P=sep_a)\d{4}(?P=sep_a)\d{1,7}"
            r"|\d{4}(?P<sep_b>[ -]?)\d{6}(?P=sep_b)\d{4,5}"
            r"|\d{13,19}"
            r")(?!\d)"
        ),
        max_len=22,
        partial=r"\d[\d -]{0,21}",
        validator=luhn_valid,
    ),
)

WINDOW = max(p.max_len for p in PATTERNS)

_COMBINED = re.compile("|".join(f"(?P<{p.name}>{p.regex})" for p in PATTERNS))
_PARTIAL_TAIL = re.compile("(?:" + "|".join(f"(?:{p.partial})" for p in PATTERNS) + r")\Z")
_VALIDATORS = {p.name: p.validator for p in PATTERNS if p.validator}


def _is_valid(match: re.Match[str]) -> bool:
    name = next(p.name for p in PATTERNS if match.group(p.name) is not None)
    validator = _VALIDATORS.get(name)
    return validator is None or validator(match.group())


def _iter_matches(s: str, pos: int = 0) -> Iterator[re.Match[str]]:
    """Validated, non-overlapping matches. A candidate that fails validation is skipped one
    character at a time so a real match inside it (e.g. a card after a stray number) is found."""
    while (match := _COMBINED.search(s, pos)) is not None:
        if _is_valid(match):
            yield match
            pos = match.end()
        else:
            pos = match.start() + 1


def redact_text(text: str) -> str:
    """Reference implementation: redact a complete string in one pass."""
    out: list[str] = []
    pos = 0
    for match in _iter_matches(text):
        out.append(text[pos : match.start()])
        out.append(REDACTED)
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


class StreamRedactor:
    """Incremental redactor. ``feed`` returns text that is safe to emit now."""

    def __init__(self) -> None:
        self._buf = ""
        self._prev = ""  # last emitted char, so lookbehind assertions still see it

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, text: str) -> str:
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        s = self._prev + self._buf
        offset = len(self._prev)
        if final:
            cut = len(s)
        else:
            tail = _PARTIAL_TAIL.search(s, max(offset, len(s) - WINDOW))
            cut = tail.start() if tail else len(s)
        if cut <= offset:
            return ""

        out: list[str] = []
        pos, consumed = offset, cut
        for match in _iter_matches(s, offset):
            if match.start() >= cut:
                break
            out.append(s[pos : match.start()])
            out.append(REDACTED)
            pos = match.end()
            consumed = max(consumed, pos)
        out.append(s[pos:consumed])

        self._prev = s[consumed - 1]
        self._buf = s[consumed:]
        return "".join(out)
