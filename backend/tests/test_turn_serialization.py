"""One conversation runs one turn at a time.

Two requests over one thread are not merely wasteful. They share the same
SQLAlchemy session and the same rows, so a double tap or a client retry can hold
one time twice, interleave two transcripts, and have two replies compete for the
card slot the other just wrote. The lock is taken in :func:`run_turn` and the
route answers it with 409, so the browser keeps the reply it is already waiting
for instead of starting a second one.

The same hazard exists *inside* a turn: ``ToolNode`` runs one round's calls in a
thread pool, and those calls share the session too. The runner asks the graph for
a single worker, which is pinned here as well.
"""

from __future__ import annotations

import threading

import pytest
from langchain_core.messages import AIMessage

from app.agent import runner
from app.agent.runner import TurnInProgress, _serialized_turn
from app.services import conversations
from tests.support import ScriptedChatModel

BUSY = 7
OTHER = 8


def _blocking_model(started: threading.Event, release: threading.Event) -> ScriptedChatModel:
    """Signals that the turn reached the model, then waits to be let go."""

    class Blocking(ScriptedChatModel):
        def _generate(  # noqa: ANN001, ANN002, ANN201
            self, messages, stop=None, run_manager=None, **kwargs
        ):
            started.set()
            assert release.wait(timeout=10), "the held turn was never released"
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    return Blocking([AIMessage(content="One moment, I'm still looking.")])


def _start(scenario, text: str, model) -> threading.Thread:  # noqa: ANN001, ANN202
    session, requester = scenario
    return threading.Thread(
        target=runner.run_turn,
        args=(session, requester.clinic),
        kwargs={
            "conversation": requester.conversation,
            "patient_session": requester.patient_session,
            "patient_text": text,
            "llm": model,
        },
    )


def _second_turn(scenario, text: str = "second") -> None:
    session, requester = scenario
    runner.run_turn(
        session,
        requester.clinic,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        patient_text=text,
        llm=ScriptedChatModel([AIMessage(content="Must not run.")]),
    )


# --- the lock itself -------------------------------------------------------


def test_a_conversation_can_hold_only_one_turn_at_a_time() -> None:
    with _serialized_turn(BUSY):
        with pytest.raises(TurnInProgress):
            with _serialized_turn(BUSY):
                pass


def test_another_conversation_is_not_blocked_by_this_one() -> None:
    with _serialized_turn(BUSY):
        with _serialized_turn(OTHER):
            pass


def test_the_lock_is_released_when_the_turn_finishes() -> None:
    with _serialized_turn(BUSY):
        pass

    with _serialized_turn(BUSY):
        pass


def test_a_turn_that_dies_still_releases_the_lock() -> None:
    with pytest.raises(ZeroDivisionError):
        with _serialized_turn(BUSY):
            raise ZeroDivisionError("the turn died")

    with _serialized_turn(BUSY):
        pass


# --- the turn --------------------------------------------------------------


def test_run_turn_refuses_a_second_concurrent_turn(scenario) -> None:
    started = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def first_turn() -> None:
        try:
            session, requester = scenario
            runner.run_turn(
                session,
                requester.clinic,
                conversation=requester.conversation,
                patient_session=requester.patient_session,
                patient_text="I need a cleaning",
                llm=_blocking_model(started, release),
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    thread = threading.Thread(target=first_turn)
    thread.start()
    assert started.wait(timeout=10), "the first turn never reached the model"

    try:
        with pytest.raises(TurnInProgress):
            _second_turn(scenario)
    finally:
        release.set()
        thread.join(timeout=10)

    assert failures == []
    assert not thread.is_alive()
    assert runner._busy_conversations == set(), "the lock leaked"


def test_the_refused_turn_persisted_nothing_of_its_own(scenario) -> None:
    """A busy answer must not leave half a patient message in the thread."""
    session, requester = scenario
    started = threading.Event()
    release = threading.Event()

    thread = _start(scenario, "first", _blocking_model(started, release))
    thread.start()
    assert started.wait(timeout=10)

    with pytest.raises(TurnInProgress):
        _second_turn(scenario)

    release.set()
    thread.join(timeout=10)

    texts = [
        row.content
        for row in conversations.history(session, requester.conversation)
        if row.role == "patient"
    ]
    assert texts == ["first"]


def test_a_finished_conversation_can_run_again(scenario) -> None:
    for text in ("hello", "and then?"):
        result = runner.run_turn(
            scenario[0],
            scenario[1].clinic,
            conversation=scenario[1].conversation,
            patient_session=scenario[1].patient_session,
            patient_text=text,
            llm=ScriptedChatModel([AIMessage(content="Sure.")]),
        )
        assert result.degraded is False


# --- inside one turn -------------------------------------------------------


def test_the_runner_asks_the_graph_for_one_worker(scenario, monkeypatch) -> None:
    """``max_concurrency=1`` is what keeps a round's tools off the same session."""
    seen: list[dict] = []
    real = runner.TURN_GRAPH

    class SingleWorker:
        def invoke(self, state, *, config=None):  # noqa: ANN001, ANN202
            seen.append(config or {})
            return real.invoke(state, config=config)

    monkeypatch.setattr(runner, "TURN_GRAPH", SingleWorker())

    runner.run_turn(
        scenario[0],
        scenario[1].clinic,
        conversation=scenario[1].conversation,
        patient_session=scenario[1].patient_session,
        patient_text="what are my options?",
        llm=ScriptedChatModel([AIMessage(content="Let me look.")]),
    )

    assert seen and seen[0].get("max_concurrency") == 1
