"""Meta Model API adapter (muse-spark). Meta serves an OpenAI-compatible Responses
API at https://api.meta.ai/v1, so this reuses the OpenAI adapter unchanged."""
from __future__ import annotations

from .openai_model import OpenAIModel


class MetaModel(OpenAIModel):
    BASE_URL = "https://api.meta.ai/v1"
