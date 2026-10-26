"""Practice tools: clinic policies, hours, prices and contact details.

Each tool is a plain LangChain tool: the argument model is the provider schema,
the injected ``deps`` carry the request state, and the return value is
``(content, artifact)`` so the graph can read cards without re-parsing prose.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from app.agent.context import TurnDeps
from app.agent.tools.results import (
    NoArguments,
    ToolArguments,
    tool_error,
    tool_result,
)
from app.services import practice


class PracticeInfoArgs(ToolArguments):
    topic: str | None = Field(
        default=None,
        description=(
            "Policy topic such as 'hours', 'location', 'insurance_accepted', "
            "'cleaning_cost'. Omit to list available topics."
        ),
    )


@tool(
    "get_practice_info",
    args_schema=PracticeInfoArgs,
    response_format="content_and_artifact",
    description=(
        "Look up an exact clinic policy: hours, location, contact, insurance accepted, "
        "prices, cancellation, pediatric care. Call without a topic to list topics. Always "
        "relay the returned answer; never invent prices or coverage."
    ),
)
def get_practice_info(
    deps: Annotated[TurnDeps, InjectedState("deps")],
    topic: str | None,
) -> tuple[str, dict[str, Any]]:
    if not topic:
        return tool_result(
            {
                "ok": True,
                "topics": practice.list_topics(deps.clinic),
                "contact": practice.front_desk_contact(deps.clinic),
                "insurance": practice.insurance_overview(deps.clinic),
            }
        )
    policy = practice.lookup_policy(deps.clinic, topic)
    if policy is None:
        return tool_result(
            tool_error(
                "unknown_topic",
                "That policy detail isn't in our knowledge base. Offer the front desk instead.",
                topics=deps.clinic.topics(),
            )
        )
    return tool_result(
        {"ok": True, **policy, "contact": practice.front_desk_contact(deps.clinic)}
    )


@tool(
    "get_practice_summary",
    args_schema=NoArguments,
    response_format="content_and_artifact",
    description=(
        "Get clinic address, hours, dentists, appointment types and insurance at once."
    ),
)
def get_practice_summary(
    deps: Annotated[TurnDeps, InjectedState("deps")],
) -> tuple[str, dict[str, Any]]:
    return tool_result({"ok": True, **practice.practice_summary(deps.session, deps.clinic)})


TOOLS = (get_practice_info, get_practice_summary)


__all__ = ["TOOLS", "PracticeInfoArgs"]
