"""Google Gemini model adapter (google-genai SDK)."""
from __future__ import annotations

import asyncio
import base64
import io

from google import genai
from google.genai import types

from .base import BaseModel, Message, TextPart, ImagePart


class GoogleModel(BaseModel):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = None  # created lazily on first generate() call

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    f"Google API key not set. Add GOOGLE_API_KEY to .env"
                )
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def generate(self, conversation: list[Message]) -> str:
        system_parts: list[str] = []
        contents: list[types.Content] = []

        for msg in conversation:
            if msg.role == "system":
                system_parts.extend(
                    p.text for p in msg.parts if isinstance(p, TextPart)
                )
                continue

            sdk_parts: list[types.Part] = []
            for part in msg.parts:
                if isinstance(part, TextPart):
                    sdk_parts.append(types.Part.from_text(text=part.text))
                elif isinstance(part, ImagePart):
                    img_bytes = base64.b64decode(part.data)
                    sdk_parts.append(
                        types.Part.from_bytes(data=img_bytes, mime_type=part.media_type)
                    )

            role = "model" if msg.role == "assistant" else "user"
            contents.append(types.Content(role=role, parts=sdk_parts))

        config_kwargs: dict = dict(
            max_output_tokens=self.max_tokens,
            system_instruction="\n".join(system_parts) if system_parts else None,
            # Reasoning is always on (thinking models: gemini-2.5-* and later)
            thinking_config=types.ThinkingConfig(thinking_budget=10000, include_thoughts=True),
        )

        config = types.GenerateContentConfig(**config_kwargs)
        client = self._get_client()
        loop = asyncio.get_event_loop()

        self._last_reasoning = ""

        def _call() -> str:
            response = client.models.generate_content(
                model=self.model_id,
                contents=contents,
                config=config,
            )
            # Collect thought parts if present
            thoughts = []
            for part in (response.candidates[0].content.parts if response.candidates else []):
                if getattr(part, "thought", False):
                    thoughts.append(part.text or "")
            self._last_reasoning = "\n".join(thoughts)
            return response.text or ""

        return await loop.run_in_executor(None, _call)
