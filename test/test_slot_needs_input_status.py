"""Tests for the slot's ``needs_input`` status — "the agent asked you something".

A question card is a websocket broadcast with no transcript row, and the
``[OPTIONS:]`` fallback is a plain assistant message, so before this status
neither was visible anywhere outside the tab that received it: the sidebar, the
sessions board and the command palette all showed a quiet, finished-looking
session while the agent was waiting on an answer.

The status is deliberately NARROWER than ``waiting_for_input`` (true of every
finished turn, which is why it cannot carry a badge) and separate from
``pending_approval`` (a tool gate, answered allow/deny). These tests pin that
boundary in both directions, plus the record's whole lifecycle: who sets it, and
every path that must retire it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _state(*slot_keys: str) -> DashboardState:
    """A partially-constructed DashboardState owning real slots.

    Only the attributes the question paths touch are wired, matching the
    fixture style of the other question suites; ``push_slots_update`` is a mock
    so a status change can be asserted to have been PUSHED, not merely stored.
    """
    st = DashboardState.__new__(DashboardState)
    st._pending_questions = {}
    st._question_futures = {}
    st._slots = {k: _ChatSlot(k) for k in slot_keys}
    st.broadcast_ws_owners = MagicMock()  # type: ignore[method-assign]
    st.deliver_ws_owners = _AsyncReturn(1)  # type: ignore[method-assign]
    st.push_slots_update = MagicMock()  # type: ignore[method-assign]
    st._log = MagicMock()
    return st


class _AsyncReturn:
    """Awaitable stub standing in for ``deliver_ws_owners``'s client count."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.calls: list[tuple] = []

    async def __call__(self, *args, **kwargs) -> int:
        self.calls.append((args, kwargs))
        return self.value


def _questions() -> list[dict]:
    return [
        {
            "question": "Which approach?",
            "options": [{"label": "Option A", "description": ""}],
            "multiSelect": False,
        }
    ]


def _turn(slot: _ChatSlot, *rows: tuple[str, str]) -> _ChatSlot:
    for role, content in rows:
        slot.append(role, content, broadcast=False)
    return slot


# ── Derived from message state: the [OPTIONS:] fallback ──


def test_options_tag_reports_needs_input() -> None:
    """The fallback the model uses when no card can render still raises the status."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("user", "which one?"),
        ("assistant", "Both work.\n\n[OPTIONS: Merge it now | Show me the diff]"),
    )
    payload = slot.to_dict()
    assert payload["needs_input"] is True
    assert payload["needs_input_reason"] == "options"
    # The pre-existing field stays as it was: it excludes has_options by design,
    # which is exactly why it could not carry this signal.
    assert payload["waiting_for_input"] is False


def test_plain_finished_turn_does_not_report_needs_input() -> None:
    """An ordinary reply is not an ask — otherwise the status lights always."""
    slot = _turn(
        _ChatSlot("chat-1"), ("user", "do the thing"), ("assistant", "done, 3 files changed")
    )
    payload = slot.to_dict()
    assert payload["needs_input"] is False
    assert payload["needs_input_reason"] == ""
    assert payload["waiting_for_input"] is True


def test_user_reply_clears_the_options_status() -> None:
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", "Pick one.\n\n[OPTIONS: Merge it now | Skip the rebase]"),
    )
    assert slot.to_dict()["needs_input"] is True
    _turn(slot, ("user", "Merge it now"))
    assert slot.to_dict()["needs_input"] is False


def test_empty_slot_reports_nothing() -> None:
    payload = _ChatSlot("chat-1").to_dict()
    assert payload["needs_input"] is False
    assert payload["needs_input_reason"] == ""


# ── The recorded question card ──


def test_question_record_reports_needs_input_and_outranks_options() -> None:
    slot = _turn(
        _ChatSlot("chat-1"), ("assistant", "Pick one.\n\n[OPTIONS: Yes | No]")
    )
    slot._question_pending = {"ts": 0.0, "blocking": False}
    payload = slot.to_dict()
    assert payload["needs_input"] is True
    # The card is the live surface when both are somehow present, so it names the
    # reason — the label the frontend picks differs between the two.
    assert payload["needs_input_reason"] == "question"


def test_question_record_survives_further_assistant_output() -> None:
    """A card posted mid-turn must not be retired by the agent's own next line."""
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"ts": 0.0, "blocking": True}
    _turn(slot, ("assistant", "meanwhile, here is what I found"))
    assert slot.to_dict()["needs_input_reason"] == "question"


def test_user_message_retires_a_stateless_question_record() -> None:
    """Any turn-consuming input answers the ask, whatever entrance it came from."""
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"ts": 0.0, "blocking": False, "card_id": "card-a"}
    _turn(slot, ("user", "option A"))
    assert slot._question_pending is None
    assert slot.to_dict()["needs_input"] is False


def test_user_message_leaves_a_BLOCKING_record_standing() -> None:
    """Nothing a user row does resolves a parked wait.

    A channel-replayed reply, a queued message or a nudge all land as `user`
    rows while `request_question` is still blocked on its future. Clearing the
    record there would report the agent as working while its tool call cannot
    move — the record is the round-trip's to retire.
    """
    slot = _ChatSlot("chat-1")
    slot._question_pending = {"ts": 0.0, "blocking": True, "card_id": "ask-1"}
    _turn(slot, ("user", "unrelated reply from Slack"))
    assert slot.to_dict()["needs_input_reason"] == "question"


def test_needs_input_is_not_gated_on_running() -> None:
    """A blocking ask parks the turn: the slot is running AND waiting on the user."""
    slot = _ChatSlot("chat-1")
    # `running` is derived from the slot's task, so a live turn is simulated with
    # an unfinished one rather than by assigning the property.
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    assert slot.running is True
    slot._question_pending = {"ts": 0.0, "blocking": True}
    assert slot.to_dict()["needs_input"] is True


# ── mark / clear ──


@pytest.mark.asyncio
async def test_post_question_card_records_the_status_and_broadcasts_its_id() -> None:
    st = _state("chat-1")
    delivered = await st.post_question_card("chat-1", _questions())
    assert delivered == 1
    assert st._slots["chat-1"].to_dict()["needs_input_reason"] == "question"
    st.push_slots_update.assert_called()  # type: ignore[attr-defined]
    # The client needs the record's identity to dismiss it, so it rides the card.
    (args, _kwargs) = st.deliver_ws_owners.calls[0]  # type: ignore[attr-defined]
    payload = args[1]
    assert payload["card_id"] == st._slots["chat-1"]._question_pending["card_id"]
    assert payload["card_id"]


@pytest.mark.asyncio
async def test_card_with_no_client_attached_still_records_the_status() -> None:
    """Zero clients means no tab is open, not that the ask went away."""
    st = _state("chat-1")
    st.deliver_ws_owners = _AsyncReturn(0)  # type: ignore[method-assign]
    assert await st.post_question_card("chat-1", _questions()) == 0
    assert st._slots["chat-1"].to_dict()["needs_input"] is True


@pytest.mark.asyncio
async def test_status_is_recorded_before_delivery_is_awaited() -> None:
    """Ordering, not just the end state: the mark must precede the await.

    Websocket delivery can park on a backpressured socket. A user row landing in
    that window would find no record to retire, and a mark applied afterwards
    would then strand an already-answered session in needs_input. Asserted by
    observing the slot from INSIDE the delivery await.
    """
    st = _state("chat-1")
    seen: dict[str, object] = {}

    async def slow_delivery(*_args, **_kwargs) -> int:
        seen["reason"] = st._slots["chat-1"].to_dict()["needs_input_reason"]
        return 1

    st.deliver_ws_owners = slow_delivery  # type: ignore[method-assign]
    await st.post_question_card("chat-1", _questions())
    assert seen["reason"] == "question"


@pytest.mark.asyncio
async def test_mark_ignores_an_unknown_slot() -> None:
    st = _state()
    st.mark_question_pending("chat-404", blocking=False)  # must not raise
    assert st.clear_question_pending("chat-404") is False


def test_clear_reports_whether_anything_was_pending() -> None:
    st = _state("chat-1")
    assert st.clear_question_pending("chat-1") is False
    st.mark_question_pending("chat-1", blocking=False)
    assert st.clear_question_pending("chat-1") is True
    assert st.clear_question_pending("chat-1") is False


def test_clear_filters_on_the_blocking_flag() -> None:
    """The dismiss route may not retire a status whose tool call is still parked."""
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=True, card_id="ask-1")
    assert st.clear_question_pending("chat-1", blocking=False) is False
    assert st._slots["chat-1"].to_dict()["needs_input"] is True
    assert st.clear_question_pending("chat-1", blocking=True) is True


def test_clear_filters_on_the_card_identity() -> None:
    """A dismissal names the card it was clicked on, never just the slot.

    The request is a round-trip, so a newer ask can replace the card in between.
    A slot-only clear would retire the NEW card's status and leave it unanswered
    with nothing on any surface to say so.
    """
    st = _state("chat-1")
    st.mark_question_pending("chat-1", blocking=False, card_id="card-new")
    assert st.clear_question_pending("chat-1", blocking=False, card_id="card-old") is False
    assert st._slots["chat-1"].to_dict()["needs_input"] is True
    assert st.clear_question_pending("chat-1", blocking=False, card_id="card-new") is True


# ── The blocking round-trip's lifecycle ──


async def _await_registered(st: DashboardState, count: int) -> None:
    for _ in range(50):
        if len(st._question_futures) == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {len(st._question_futures)} question(s) registered")


@pytest.mark.asyncio
async def test_blocking_question_marks_then_retires_on_answer() -> None:
    st = _state("chat-1")
    task = asyncio.ensure_future(
        st.request_question("a1", "chat-1", _questions(), timeout=30)
    )
    await _await_registered(st, 1)
    assert st._slots["chat-1"].to_dict()["needs_input_reason"] == "question"

    st.resolve_question("a1", {"Which approach?": "Option A"})
    assert await task == {"Which approach?": "Option A"}
    assert st._slots["chat-1"].to_dict()["needs_input"] is False


@pytest.mark.asyncio
async def test_blocking_question_retires_on_timeout() -> None:
    st = _state("chat-1")
    assert await st.request_question("a2", "chat-1", _questions(), timeout=1) is None
    assert st._slots["chat-1"].to_dict()["needs_input"] is False


@pytest.mark.asyncio
async def test_one_question_exiting_leaves_another_ask_status_standing() -> None:
    """Two asks on one slot: retiring the first must not report the second as answered."""
    st = _state("chat-1")
    first = asyncio.ensure_future(
        st.request_question("a1", "chat-1", _questions(), timeout=30)
    )
    second = asyncio.ensure_future(
        st.request_question("a2", "chat-1", _questions(), timeout=30)
    )
    await _await_registered(st, 2)

    st.resolve_question("a1", {"Which approach?": "Option A"})
    await first
    assert st._slots["chat-1"].to_dict()["needs_input"] is True

    st.resolve_question("a2", {"Which approach?": "Option A"})
    await second
    assert st._slots["chat-1"].to_dict()["needs_input"] is False
