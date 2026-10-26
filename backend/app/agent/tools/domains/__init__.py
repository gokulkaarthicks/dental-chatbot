"""Tool domains, collected into one catalogue.

``DOMAIN_MODULES`` is not alphabetical on purpose: it is the order the tools are
presented to the model, so identity and practice come before availability and
booking. A patient has to be verified and the clinic known before a search or a
hold makes sense, and a model that reads the list in order tends to follow it.

Each module owns its own concerns and exposes ``TOOLS``; nothing here imports
another domain, so a new domain is one file plus one entry in the tuple.
"""

from app.agent.tools.domains import (
    appointments,
    availability,
    booking,
    escalation,
    family,
    identity,
    practice,
)

DOMAIN_MODULES = (
    identity,
    practice,
    availability,
    booking,
    appointments,
    family,
    escalation,
)

DOMAIN_TOOLS = tuple(tool for module in DOMAIN_MODULES for tool in module.TOOLS)

__all__ = ["DOMAIN_MODULES", "DOMAIN_TOOLS"]
