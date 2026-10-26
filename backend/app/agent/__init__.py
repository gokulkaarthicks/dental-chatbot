"""Agent package: prompts, the turn graph, and the tool registry.

``run_turn`` is the only entry point the HTTP layer needs. Everything below it
(the graph, the visibility policy, the ``@tool`` functions) is an implementation
detail of one patient turn.
"""

from app.agent.prompts import build_greeting, build_system_prompt
from app.agent.runner import TurnResult, run_turn

__all__ = ["TurnResult", "build_greeting", "build_system_prompt", "run_turn"]
