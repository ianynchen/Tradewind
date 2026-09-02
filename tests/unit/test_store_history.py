"""Tests for tradewind.adapters.sqlite_store.SqliteSessionStore: history
retrieval (flat + tree), fork copy, and native import (task-5 brief).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tradewind.adapters.sqlite_store import SqliteSessionStore
from tradewind.domain.errors import SessionExists, SessionNotFound
from tradewind.domain.models import NormalizedMessage, SessionRow


def _row(session_id: str = "sess-1", **overrides: object) -> SessionRow:
    base = SessionRow(
        session_id=session_id,
        backend="claude",
        profile="default",
        options_snapshot={"system_prompt": "be helpful"},
    )
    return replace(base, **overrides) if overrides else base


def _store(tmp_path: Path) -> SqliteSessionStore:
    store = SqliteSessionStore(tmp_path / "sessions.db")
    store.migrate()
    return store


def _message(**overrides: object) -> NormalizedMessage:
    base = NormalizedMessage(role="user", kind="text", content={"text": "hi"})
    return replace(base, **overrides) if overrides else base


def _build_lineage(store: SqliteSessionStore) -> None:
    """root -> A (subagent, spawned_by set) -> {B, C} (siblings under A)."""
    store.create_session(_row("root"))
    store.begin_turn("root", "turn-root", None)
    store.append_message("root", "turn-root", _message(content={"text": "root msg"}))

    store.create_session(
        _row("root-a", parent_session_id="root", spawn_kind="subagent", spawned_by_message_id=1)
    )
    store.begin_turn("root-a", "turn-a", None)
    store.append_message("root-a", "turn-a", _message(content={"text": "a msg"}))

    store.create_session(_row("root-a-b", parent_session_id="root-a", spawn_kind="subagent"))
    store.begin_turn("root-a-b", "turn-b", None)
    store.append_message("root-a-b", "turn-b", _message(content={"text": "b msg"}))

    store.create_session(_row("root-a-c", parent_session_id="root-a", spawn_kind="subagent"))
    store.begin_turn("root-a-c", "turn-c", None)
    store.append_message("root-a-c", "turn-c", _message(content={"text": "c msg"}))


# --- history: flat mode (Step 1) ---


def test_history_flat_returns_only_own_session_messages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _build_lineage(store)

    messages = store.history("root")

    assert [m.session_id for m in messages] == ["root"]
    assert [m.content["text"] for m in messages] == ["root msg"]


def test_history_flat_excludes_child_session_messages(tmp_path: Path) -> None:
    # I-1 (child separation): flat retrieval on a session with descendants
    # must not leak their messages in — that's what makes flat-vs-tree a
    # pure ID-set question (ARCHITECTURE §4.1).
    store = _store(tmp_path)
    _build_lineage(store)

    messages = store.history("root")

    child_session_ids = {"root-a", "root-a-b", "root-a-c"}
    assert not any(m.session_id in child_session_ids for m in messages)


def test_history_flat_after_seq_and_limit_paginate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    for i in range(5):
        store.append_message("sess-1", "turn-1", _message(content={"text": f"msg {i}"}))

    page = store.history("sess-1", after_seq=2, limit=2)

    assert [m.seq for m in page] == [3, 4]


def test_history_flat_include_raw_false_returns_none_even_when_stored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _message(raw={"native": {"deeply": "nested payload"}}))

    messages = store.history("sess-1", include_raw=False)

    assert messages[0].raw is None


def test_history_flat_include_raw_true_returns_stored_raw(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _message(raw={"native": {"deeply": "nested payload"}}))

    messages = store.history("sess-1", include_raw=True)

    assert messages[0].raw == {"native": {"deeply": "nested payload"}}


# --- history: tree mode (Step 1) ---


def test_history_tree_returns_root_and_all_descendants_ordered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _build_lineage(store)

    messages = store.history("root", include_children=True)

    session_ids = {m.session_id for m in messages}
    assert session_ids == {"root", "root-a", "root-a-b", "root-a-c"}
    # Ordered (session_id, seq) per ARCHITECTURE §4.2: session_id groups
    # sort lexically, and each group is internally seq-ordered.
    assert [(m.session_id, m.seq) for m in messages] == sorted(
        (m.session_id, m.seq) for m in messages
    )


def test_history_tree_excludes_unrelated_sessions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _build_lineage(store)
    store.create_session(_row("unrelated"))
    store.begin_turn("unrelated", "turn-u", None)
    store.append_message("unrelated", "turn-u", _message())

    messages = store.history("root", include_children=True)

    assert "unrelated" not in {m.session_id for m in messages}


def test_history_tree_include_raw_false_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("root"))
    store.begin_turn("root", "turn-root", None)
    store.append_message("root", "turn-root", _message(raw={"x": 1}))

    messages = store.history("root", include_children=True, include_raw=False)

    assert messages[0].raw is None


def test_history_tree_include_raw_true_returns_stored_raw(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("root"))
    store.begin_turn("root", "turn-root", None)
    store.append_message("root", "turn-root", _message(raw={"x": 1}))

    messages = store.history("root", include_children=True, include_raw=True)

    assert messages[0].raw == {"x": 1}


# --- copy_history: fork (Step 1) ---


def test_copy_history_creates_fork_row_with_forced_lineage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("src"))

    # dst_row deliberately carries a different (wrong) spawn_kind/parent to
    # verify copy_history forces the fork lineage regardless (per brief).
    dst_row = _row("dst", spawn_kind="subagent", parent_session_id="someone-else")
    result = store.copy_history("src", dst_row)

    assert result.spawn_kind == "fork"
    assert result.parent_session_id == "src"
    fetched = store.get_session("dst")
    assert fetched is not None
    assert fetched.spawn_kind == "fork"
    assert fetched.parent_session_id == "src"


def test_copy_history_missing_src_raises_session_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SessionNotFound):
        store.copy_history("does-not-exist", _row("dst"))


def test_copy_history_existing_dst_raises_session_exists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("src"))
    store.create_session(_row("dst"))

    with pytest.raises(SessionExists):
        store.copy_history("src", _row("dst"))


def test_copy_history_copies_messages_up_to_seq_with_fresh_seq_and_no_turn(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create_session(_row("src"))
    store.begin_turn("src", "turn-1", None)
    for i in range(5):
        store.append_message("src", "turn-1", _message(content={"text": f"msg {i}"}))

    store.copy_history("src", _row("dst"), up_to_seq=3)

    copied = store.history("dst")
    assert [m.content["text"] for m in copied] == ["msg 0", "msg 1", "msg 2"]
    assert [m.seq for m in copied] == [1, 2, 3]
    assert all(m.turn_id is None for m in copied)


def test_copy_history_up_to_seq_none_copies_all_messages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("src"))
    store.begin_turn("src", "turn-1", None)
    for i in range(3):
        store.append_message("src", "turn-1", _message(content={"text": f"msg {i}"}))

    store.copy_history("src", _row("dst"), up_to_seq=None)

    copied = store.history("dst")
    assert len(copied) == 3


# --- import_native_items: dedupe on native_id (Step 1) ---


def test_import_native_items_dedupes_on_native_id_second_call_returns_zero(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    items = [
        _message(native_id="native-1", content={"text": "one"}),
        _message(native_id="native-2", content={"text": "two"}),
    ]

    first = store.import_native_items("sess-1", None, items)
    second = store.import_native_items("sess-1", None, items)

    assert first == 2
    assert second == 0


def test_import_native_items_keeps_multiple_items_sharing_one_native_id(tmp_path: Path) -> None:
    # Task-11 regression: one native transcript entry (e.g. a single
    # `AssistantMessage`/`SessionMessage` carrying both a thinking block and
    # a tool_use block) legitimately produces MULTIPLE `NormalizedMessage`
    # items stamped with the SAME `native_id` (`claude_backend.py`'s
    # `native_transcript_items`/`assistant_message_items`: "native_id is the
    # same for every item one transcript line produced"). Deduping against
    # native_ids inserted earlier in the SAME call (not just those already
    # in the DB before it started) silently dropped every item after the
    # first one sharing an id -- confirmed live this task via
    # `ResumePlanner.reconcile()` against a real Claude session.
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    items = [
        _message(native_id="native-1", kind="thinking", content={"text": "thinking..."}),
        _message(
            native_id="native-1", kind="tool_use", content={"id": "t1", "name": "x", "input": {}}
        ),
    ]

    inserted = store.import_native_items("sess-1", None, items)

    assert inserted == 2
    history = store.history("sess-1")
    assert [m.kind for m in history] == ["thinking", "tool_use"]
    assert all(m.native_id == "native-1" for m in history)

    # Idempotency across calls is unaffected: re-importing the exact same
    # batch a second time (both items already in the DB) imports nothing.
    second = store.import_native_items("sess-1", None, items)
    assert second == 0


def test_import_native_items_none_native_id_always_inserted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    items = [_message(native_id=None, content={"text": "no id"})]

    first = store.import_native_items("sess-1", None, items)
    second = store.import_native_items("sess-1", None, items)

    assert first == 1
    assert second == 1


def test_import_native_items_assigns_seq_after_current_tail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _message())  # seq 1

    store.import_native_items(
        "sess-1", None, [_message(native_id="native-1"), _message(native_id="native-2")]
    )

    messages = store.history("sess-1")
    assert [m.seq for m in messages] == [1, 2, 3]


def test_import_native_items_missing_session_raises_session_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SessionNotFound):
        store.import_native_items("does-not-exist", None, [_message()])


# --- last_native_id (Step 1) ---


def test_last_native_id_returns_highest_seq_native_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _message(native_id="native-1"))
    store.append_message("sess-1", "turn-1", _message(native_id=None))
    store.append_message("sess-1", "turn-1", _message(native_id="native-3"))

    assert store.last_native_id("sess-1") == "native-3"


def test_last_native_id_no_native_messages_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_row("sess-1"))
    store.begin_turn("sess-1", "turn-1", None)
    store.append_message("sess-1", "turn-1", _message(native_id=None))

    assert store.last_native_id("sess-1") is None


# --- perf: flat read without raw stays well under budget at scale (Step 1) ---


@pytest.mark.perf
def test_history_flat_no_raw_10k_messages_under_50ms(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_path)
    store.migrate()
    store.create_session(_row("perf-sess"))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO messages (session_id, turn_id, seq, role, kind, content_json, "
            "created_at, raw_json) VALUES (?, NULL, ?, 'user', 'text', ?, ?, ?)",
            [
                (
                    "perf-sess",
                    seq,
                    json.dumps({"text": f"msg {seq}"}),
                    "2024-01-01T00:00:00",
                    json.dumps({"raw": "x" * 200}),
                )
                for seq in range(1, 10_001)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    start = time.perf_counter()
    messages = store.history("perf-sess", include_raw=False)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(messages) == 10_000
    assert all(m.raw is None for m in messages)
    assert elapsed_ms < 50, f"flat history read took {elapsed_ms:.2f}ms (budget: 50ms)"
