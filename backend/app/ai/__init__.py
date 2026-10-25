"""Single conversational agent: DeepSeek + tool calling.

One model, one agent, no router. FastAPI owns every state change: the model may
only ask for tools, and each tool validates its own arguments with Pydantic
before touching a service.
"""

from app.ai.deepseek import (
    AssistantTurn,
    DeepSeekBadResponse,
    DeepSeekClient,
    DeepSeekError,
    DeepSeekNotConfigured,
    DeepSeekTimeout,
    DeepSeekUnavailable,
    ToolCall,
)

__all__ = [
    "AssistantTurn",
    "DeepSeekBadResponse",
    "DeepSeekClient",
    "DeepSeekError",
    "DeepSeekNotConfigured",
    "DeepSeekTimeout",
    "DeepSeekUnavailable",
    "ToolCall",
]
