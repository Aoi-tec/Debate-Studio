"""Rough token estimation used only for pre-flight budgeting.

Estimates are always labelled ``estimated``; provider-reported usage wins.
"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = 0
    other = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            other += 1
    # ~4 ASCII chars/token; CJK is roughly 1 token per char (conservative)
    return int(ascii_chars / 4 + other * 1.0) + 1


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content", "")) + 4 for m in messages) + 2
