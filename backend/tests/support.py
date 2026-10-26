"""Test plumbing shared by the agent tests.

Four pieces, all deliberately thin:

* ``ScriptedChatModel`` is a real ``BaseChatModel`` that replays canned
  assistant messages. Because it implements ``bind_tools`` the same way a
  provider-backed model does, the graph's binding and tool-call handling are
  exercised rather than bypassed.
* ``deps_for`` and ``invoke_tool`` run a registered tool through LangGraph's
  ``ToolNode`` with a real ``TurnDeps``, which is the same path the graph uses:
  schema validation, ``InjectedState`` injection, and the content/artifact
  result contract.
* ``patch_tool`` swaps one tool's implementation while keeping its schema, so a
  test can drive a turn onto a result the database cannot produce on demand.
* ``seed_clinic`` and ``tenant_session`` give a module that builds its own
  database the tenant every other row has to belong to. Multi-tenancy made an
  unbound session unable to write tenant rows at all, so a world that skips this
  fails on its first insert rather than quietly reading another clinic.

``invoke_tool`` compiles a two-node graph rather than calling the tool directly.
``ToolNode`` injects ``deps`` from graph state, so a bare ``node.invoke`` has no
state to inject and fails; running the real node is what keeps the test honest.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import time
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode
from pydantic import Field, PrivateAttr
from sqlalchemy.orm import Session, sessionmaker

from app import tenancy
from app.agent.context import TurnDeps
from app.agent.tools import TOOL_BY_NAME
from app.db import bind_tenant, require_clinic_id
from app.tenancy import ClinicContext
from app.timeutil import SchedulingConfig

# The grid every seeded test clinic gets. Written out rather than imported from
# ``app.timeutil.LEGACY_CLINIC_SCHEDULING``: tests must be able to state a
# second clinic with different hours, and the lint gate bans that import.
TIMEZONE = "America/Los_Angeles"
OPEN_TIME = time(8, 0)
CLOSE_TIME = time(18, 0)
SLOT_MINUTES = 30
OPEN_WEEKDAYS = frozenset({0, 1, 2, 3, 4, 5})


# The same grid as a value, for the tests that exercise scheduling arithmetic
# without a database at all -- date phrases and time windows are pure once the
# clinic's hours are an argument, which is the point of the refactor.
TEST_CONFIG = SchedulingConfig(
    timezone_name=TIMEZONE,
    slot_minutes=SLOT_MINUTES,
    open_time=OPEN_TIME,
    close_time=CLOSE_TIME,
    open_weekdays=OPEN_WEEKDAYS,
)


def seed_clinic(
    session: Session,
    *,
    slug: str = "test-clinic",
    name: str = "Test Clinic",
    slot_minutes: int = SLOT_MINUTES,
    open_time: time = OPEN_TIME,
    close_time: time = CLOSE_TIME,
    open_weekdays: frozenset[int] = OPEN_WEEKDAYS,
    timezone: str = TIMEZONE,
) -> ClinicContext:
    """Insert one tenant and return the snapshot runtime code reads.

    Run on an *unbound* session: resolving a tenant is the one read that must
    not be filtered by a tenant, and ``tenancy.create_clinic`` refuses a slug
    that already exists, so a world that calls it twice gets an error instead of
    two clinics that a later lookup could not tell apart.

    Committed here, because the clinic is the row every later tenant-bound
    session has to find: a rolled-back one would leave the world bound to a
    clinic id that no longer exists.
    """
    row = tenancy.create_clinic(
        session,
        slug=slug,
        name=name,
        city="Portside",
        state="OR",
        timezone=timezone,
        slot_minutes=slot_minutes,
        open_time=open_time,
        close_time=close_time,
        open_weekdays=open_weekdays,
        postal_code="97000",
        phone="+16505550100",
        email="front.desk@example.test",
        address_line1="12 Harbour Street",
    )
    session.commit()
    return tenancy.context_from(session, row)


def clinic_of(session: Session) -> ClinicContext:
    """The tenant a bound session was opened for, as the runtime reads it.

    Test helpers take a session rather than a clinic, and this is the same
    resolution ``app.api.deps`` performs for a request, so a helper that needs
    the tenant's grid gets it from the binding instead of from a module constant.
    """
    clinic_id = require_clinic_id(session)
    row = tenancy.load_clinic_row(session, clinic_id)
    if row is None:
        raise AssertionError(f"session is bound to a missing clinic {clinic_id}")
    return tenancy.context_from(session, row)


@contextmanager
def tenant_session(factory: sessionmaker[Session], clinic_id: int) -> Iterator[Session]:
    """A session bound to one tenant, closed on exit.

    Every write in these tests goes through a session like this, because the
    tenant binding is what ``app.models.base`` uses to filter reads and reject
    rows stamped for somebody else.
    """
    session = bind_tenant(factory(), clinic_id)
    try:
        yield session
    finally:
        session.close()


class ScriptedChatModel(BaseChatModel):
    """Replays scripted turns and records every round the graph made.

    ``tools_offered`` is the list of tool names bound for each round, so a test
    can assert on the policy (which tools the model could see) instead of on a
    boolean "were tools passed".
    """

    turns: list[AIMessage] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    tools_offered: list[list[str]] = Field(default_factory=list)
    _bound: list[str] | None = PrivateAttr(default=None)
    _error: Exception | None = PrivateAttr(default=None)

    def __init__(self, turns: list[AIMessage] | None = None, *, error: Exception | None = None):
        super().__init__(turns=list(turns or []))
        self._error = error

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs: Any):  # noqa: ANN001, ANN201
        del kwargs
        self._bound = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):  # noqa: ANN001, ANN201
        del stop, run_manager, kwargs
        if self._error is not None:
            raise self._error
        self.calls.append(list(messages))
        self.tools_offered.append(self._bound or [])
        self._bound = None
        turn = self.turns.pop(0) if self.turns else AIMessage(content="All set.")
        return ChatResult(generations=[ChatGeneration(message=turn)])


def deps_for(session, requester) -> TurnDeps:  # noqa: ANN001
    """Request-scoped dependencies for a direct tool call.

    No model is needed: a tool under test never reaches one. The clinic travels
    with the requester, because a tool that answers a clinic fact reads it from
    the dependency rather than from an argument the provider could fill in.
    """
    return TurnDeps(
        session=session,
        conversation=requester.conversation,
        patient_session=requester.patient_session,
        llm=None,  # type: ignore[arg-type]
        clinic=requester.clinic,
    )


def patch_tool(monkeypatch, name: str, payload: dict[str, Any]) -> None:  # noqa: ANN001
    """Replace one tool's implementation, keeping its schema and injection.

    The compiled graph holds the same tool objects, so patching the function is
    enough: ``ToolNode`` still validates and injects, and the test decides only
    what the tool answers.
    """

    def handler(deps: Annotated[Any, InjectedState("deps")], **arguments: Any):  # noqa: ANN202
        del deps, arguments
        return json.dumps(payload), payload

    monkeypatch.setattr(TOOL_BY_NAME[name], "func", handler)


class _ToolState(TypedDict, total=False):
    deps: Any
    messages: Annotated[list[Any], add_messages]


def _tool_graph(tool):  # noqa: ANN001, ANN202
    """A one-node graph whose only job is to run ``tool`` the way the turn does."""
    graph = StateGraph(_ToolState)
    graph.add_node("tools", ToolNode([tool], handle_tool_errors=False))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


def invoke_tool(
    deps: TurnDeps, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one tool through ``ToolNode`` and return its artifact payload."""
    tool = TOOL_BY_NAME[name]
    call = {"name": name, "args": arguments or {}, "id": f"call_{name}", "type": "tool_call"}
    produced = _tool_graph(tool).invoke(
        {"deps": deps, "messages": [AIMessage(content="", tool_calls=[call])]}
    )
    message = produced["messages"][-1]
    if isinstance(message.artifact, dict):
        return message.artifact
    content = message.content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise AssertionError(f"{name} returned no structured result: {content!r}")


__all__ = [
    "CLOSE_TIME",
    "OPEN_TIME",
    "OPEN_WEEKDAYS",
    "SLOT_MINUTES",
    "TEST_CONFIG",
    "ScriptedChatModel",
    "TIMEZONE",
    "deps_for",
    "invoke_tool",
    "clinic_of",
    "patch_tool",
    "seed_clinic",
    "tenant_session",
]
