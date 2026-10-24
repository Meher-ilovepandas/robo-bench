"""Anthropic (Claude) model adapter."""
from __future__ import annotations

import anthropic

from .base import BaseModel, Message, TextPart, ImagePart, REASONING_EFFORT


class AnthropicModel(BaseModel):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def generate(self, conversation: list[Message]) -> str:
        system_text = ""
        messages = []

        for msg in conversation:
            if msg.role == "system":
                system_text = "\n\n".join(
                    p.text for p in msg.parts if isinstance(p, TextPart)
                )
                continue

            content = []
            for part in msg.parts:
                if isinstance(part, TextPart):
                    content.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": part.data,
                        },
                    })
            messages.append({"role": msg.role, "content": content})

        kwargs: dict = dict(
            model=self.model_id, max_tokens=self.max_tokens, messages=messages,
            # display="summarized" returns a readable thinking summary (default: omitted)
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": REASONING_EFFORT},
        )
        if system_text:
            kwargs["system"] = system_text

        # Stream: long thinking turns can exceed the non-streaming HTTP timeout.
        async with self._client.messages.stream(**kwargs) as stream:
            response = await stream.get_final_message()

        self._last_reasoning = ""
        text_parts = []
        for block in response.content:
            if block.type == "thinking":
                self._last_reasoning += getattr(block, "thinking", "") or ""
            elif block.type == "text":
                text_parts.append(block.text)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"{self.model_id} refused the request "
                               f"(category={getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            # Truncated code would crash on the robot; fail loudly instead.
            raise RuntimeError(f"{self.model_id} hit max_tokens={self.max_tokens} — "
                               f"response truncated. Raise max_tokens in config.yaml.")
        return "".join(text_parts)
