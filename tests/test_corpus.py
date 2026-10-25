"""Regression corpus for the safety gate.

Every command in ``tests/corpus/commands.jsonl`` has an expected level. The
corpus is the single source of truth for "what the gate must do": each bypass
found becomes a new line here *before* it is fixed. ``scripts/gate_report.py``
renders the same file as a benchmark table for the docs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from substrate.safety import Level, safety_level

CORPUS = Path(__file__).parent / "corpus" / "commands.jsonl"


def load_corpus() -> list[dict]:
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


_ROWS = load_corpus()


def _ident(row: dict) -> str:
    return row["cmd"][:48].replace("\n", "\\n") or "<empty>"


@pytest.mark.parametrize("row", _ROWS, ids=[_ident(r) for r in _ROWS])
def test_corpus_entry(row):
    expected = Level[row["level"]]
    got = safety_level(row["cmd"])
    assert got.level == expected, (
        f"{row['cmd']!r}: expected {expected.name}, got {got.level.name} ({got.reason})"
    )


def test_corpus_has_no_duplicates():
    cmds = [r["cmd"] for r in _ROWS]
    assert len(cmds) == len(set(cmds)), "duplicate commands in corpus"


def test_corpus_covers_every_level():
    levels = {r["level"] for r in _ROWS}
    assert levels == {"SAFE", "NEEDS_CONFIRMATION", "HARD_BLOCK"}


def test_every_bypass_is_blocked_or_gated():
    """Anything tagged ``bypass`` was once classified SAFE by mistake; never again."""
    for row in _ROWS:
        if "bypass" in row["tags"]:
            assert safety_level(row["cmd"]).level != Level.SAFE, row["cmd"]
