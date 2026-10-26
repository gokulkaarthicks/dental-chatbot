"""HTTP route modules.

Every module here exposes one ``APIRouter``; ``app.main`` mounts them all under
``/api``. ``deps`` is support code rather than a router, so it is not exported.
"""

from app.api import chat, clinic, demo, health, scheduling, staff

__all__ = ["chat", "clinic", "demo", "health", "scheduling", "staff"]
