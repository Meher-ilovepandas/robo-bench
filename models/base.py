"""Abstract base class for all model adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextPart:
    text: str
    type: str = "text"


@dataclass
class ImagePart:
    data: str          # base64-encoded image bytes
    media_type: str    # e.g. "image/jpeg"
    type: str = "image"


# Reasoning is always on, at the same effort level for every provider (benchmark fairness)
REASONING_EFFORT = "high"


# A message has a role and a list of content parts
@dataclass
class Message:
    role: str          # "user" | "assistant" | "system"
    parts: list[Any] = field(default_factory=list)


class BaseModel(ABC):
    """
    Common interface for Opus / Astra / Muse (and their underlying APIs).

    All model adapters must implement `generate()`.  The conversation is
    maintained externally as a list of Message objects and passed in on
    every call so that each adapter can build its native API payload.
    """

    def __init__(self, name: str, model_id: str, api_key: str, max_tokens: int):
        self.name = name
        self.model_id = model_id
        self.api_key = api_key
        self.max_tokens = max_tokens
        self._last_reasoning: str = ""  # reasoning summary from the last generate()

    @abstractmethod
    async def generate(self, conversation: list[Message]) -> str:
        """
        Send the full conversation to the model and return the assistant reply
        (raw text — the generated Python code). Always reasons at REASONING_EFFORT.
        """
