import random
import time

import pytest

from task3_llm_guardrail.redactor import (
    REDACTED,
    WINDOW,
    StreamRedactor,
    luhn_valid,
    redact_text,
)

EMAIL = "john.doe@example.com"
SSN = "123-45-6789"
CARD = "4111 1111 1111 1111"
SAMPLES = {
    "email": EMAIL,
    "email_plus": "a.b+tag@mail.sub.example.co.uk",
    "ssn_dash": SSN,
    "ssn_space": "123 45 6789",
    "card_spaces": CARD,
    "card_dashes": "4111-1111-1111-1111",
    "card_plain": "4111111111111111",
    "card_13": "4222222222222",
    "card_amex": "3782 822463 10005",
}


def stream(chunks: list[str]) -> tuple[str, list[str], StreamRedactor]:
    redactor = StreamRedactor()
    emitted = [redactor.feed(c) for c in chunks]
    emitted.append(redactor.flush())
    return "".join(emitted), emitted, redactor


@pytest.mark.parametrize("name", SAMPLES)
def test_each_pattern_whole(name: str) -> None:
    text = f"before {SAMPLES[name]} after"
    assert redact_text(text) == f"before {REDACTED} after"
    assert stream([text])[0] == f"before {REDACTED} after"


@pytest.mark.parametrize("name", SAMPLES)
def test_each_pattern_split_at_every_boundary(name: str) -> None:
    text = f"Contact: {SAMPLES[name]}. Thanks"
    expected = f"Contact: {REDACTED}. Thanks"
    for i in range(1, len(text)):
        out, emitted, _ = stream([text[:i], text[i:]])
        assert out == expected, f"split at {i}"
        assert SAMPLES[name] not in "".join(emitted)


@pytest.mark.parametrize("name", SAMPLES)
def test_each_pattern_split_into_three_everywhere(name: str) -> None:
    text = f"x {SAMPLES[name]} y"
    expected = f"x {REDACTED} y"
    for i in range(1, len(text) - 1):
        for j in range(i + 1, len(text)):
            assert stream([text[:i], text[i:j], text[j:]])[0] == expected


@pytest.mark.parametrize("name", SAMPLES)
def test_one_character_at_a_time(name: str) -> None:
    text = f"hi {SAMPLES[name]} bye"
    assert stream(list(text))[0] == f"hi {REDACTED} bye"


def test_multiple_pii_in_one_chunk() -> None:
    text = f"{EMAIL}, {SSN}, {CARD} and {EMAIL}"
    assert stream([text])[0] == ", ".join([REDACTED] * 3) + f" and {REDACTED}"


@pytest.mark.parametrize("text", [f"{EMAIL} is mine", f"mine is {EMAIL}", EMAIL, SSN, CARD])
def test_pii_at_very_start_and_end_of_stream(text: str) -> None:
    for size in (1, 2, 5, 100):
        chunks = [text[i : i + size] for i in range(0, len(text), size)]
        assert stream(chunks)[0] == redact_text(text)
        assert SAMPLES["email"] not in stream(chunks)[0]


def test_non_luhn_sixteen_digit_number_is_not_redacted() -> None:
    text = "order 1234 5678 9012 3456 shipped"
    assert not luhn_valid("1234567890123456")
    assert redact_text(text) == text
    assert stream([text[:10], text[10:]])[0] == text


@pytest.mark.parametrize(
    "text",
    [
        "Hello, this text has no personal data at all.",
        "version 1.2.3 released on 2024-01-15 at 10:30",
        "SSN-like but invalid: 000-12-3456, 666-12-3456, 900-12-3456, 123-00-6789, 123-45-0000",
        "call 555-123-4567 now",
        "12345678901234567890123",  # long digit run: not a card
        "user @ example dot com",
        "",
    ],
)
def test_text_without_pii_is_unchanged(text: str) -> None:
    assert redact_text(text) == text
    for size in (1, 3, 7):
        assert stream([text[i : i + size] for i in range(0, len(text), size)])[0] == text


@pytest.mark.parametrize("neighbour", [SSN, "123 45 6789", "12", "170-86-2457", EMAIL])
@pytest.mark.parametrize("card", ["4012888888881881", CARD, "5500-0000-0000-0004"])
def test_card_next_to_other_numbers_is_still_redacted(card: str, neighbour: str) -> None:
    for text in (f"{card} {neighbour}", f"{neighbour} {card}", f"{card}-{neighbour}"):
        out = redact_text(text)
        assert card not in out, text
        assert stream(list(text))[0] == out


def test_hold_back_is_only_the_trailing_token() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("Hello wor") == "Hello "
    assert redactor.buffered == len("wor")
    assert redactor.feed("ld! ") == "world! "
    assert redactor.buffered == 0


def test_lookbehind_context_survives_the_cut() -> None:
    text = "ref " + "9" * 40 + " done"
    for size in (1, 5, 13):
        out = stream([text[i : i + size] for i in range(0, len(text), size)])[0]
        assert out == redact_text(text)


def _random_text(rng: random.Random) -> tuple[str, list[str]]:
    words = ["hello", "world", "the", "order", "is", "ready", "x@y", "12", "3-4", "4111", "-", "@"]
    pii = [
        f"user{rng.randint(0, 999)}@mail{rng.randint(0, 9)}.example.com",
        f"{rng.randint(100, 665)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
        rng.choice(["4111 1111 1111 1111", "5500-0000-0000-0004", "4012888888881881"]),
    ]
    parts: list[str] = []
    planted: list[str] = []
    for _ in range(rng.randint(1, 25)):
        if rng.random() < 0.3:
            p = rng.choice(pii)
            planted.append(p)
            parts.append(p)
        else:
            parts.append(rng.choice(words))
    return " ".join(parts), planted


def test_random_chunkings_match_whole_text_redaction_and_never_leak() -> None:
    rng = random.Random(1234)
    for _ in range(600):
        text, planted = _random_text(rng)
        chunks, i = [], 0
        while i < len(text):
            step = rng.randint(1, 30)
            chunks.append(text[i : i + step])
            i += step
        out, emitted, _ = stream(chunks)
        assert out == redact_text(text)
        joined = "".join(emitted)
        for item in planted:
            assert item not in joined, (item, chunks)
            assert all(item not in piece for piece in emitted)


def test_memory_is_bounded_on_a_five_megabyte_stream() -> None:
    rng = random.Random(7)
    pieces = ["lorem ipsum dolor sit amet ", f"mail {EMAIL} ", f"ssn {SSN} ", f"card {CARD} "]
    total, parts = 0, []
    while total < 5 * 1024 * 1024:
        p = rng.choice(pieces)
        parts.append(p)
        total += len(p)
    text = "".join(parts)

    redactor = StreamRedactor()
    largest, peak, emitted = 0, 0, []
    i = 0
    while i < len(text):
        step = rng.randint(1, 64)
        chunk = text[i : i + step]
        i += step
        largest = max(largest, len(chunk))
        emitted.append(redactor.feed(chunk))
        peak = max(peak, redactor.buffered)
    emitted.append(redactor.flush())

    assert peak <= WINDOW + largest
    assert peak <= WINDOW
    out = "".join(emitted)
    assert out == redact_text(text)
    assert EMAIL not in out and SSN not in out and CARD not in out


@pytest.mark.parametrize(
    "unit",
    ["a", "a.", "1 ", "1-", "a@", "a@a.", "a@a-a.", "1234 5678 "],
)
def test_adversarial_input_keeps_buffer_bounded_and_fast(unit: str) -> None:
    text = unit * (200_000 // len(unit))
    redactor = StreamRedactor()
    peak = 0
    start = time.perf_counter()
    for i in range(0, len(text), 100):
        redactor.feed(text[i : i + 100])
        peak = max(peak, redactor.buffered)
    redactor.flush()
    assert peak <= WINDOW
    assert time.perf_counter() - start < 15
