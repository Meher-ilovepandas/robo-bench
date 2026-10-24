"""OpenAI-compatible Responses API adapter (OpenAI's GPT models, and Meta's Muse
via MetaModel, which only changes base_url).

The Responses API (not Chat Completions) is used because it is the only endpoint
that returns reasoning summaries on both OpenAI and Meta; Chat Completions returns
no reasoning text at all.
"""
from __future__ import annotations

from openai import AsyncOpenAI

from .base import BaseModel, Message, TextPart, ImagePart, REASONING_EFFORT


class OpenAIModel(BaseModel):
    BASE_URL: str | None = None   # None = OpenAI

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client = AsyncOpenAI(api_key=self.api_key or "missing-key", base_url=self.BASE_URL)

    async def generate(self, conversation: list[Message]) -> str:
        instructions: list[str] = []
        items = []

        for msg in conversation:
            if msg.role == "system":
                instructions.extend(p.text for p in msg.parts if isinstance(p, TextPart))
                continue

            text_type = "output_text" if msg.role == "assistant" else "input_text"
            content = []
            for part in msg.parts:
                if isinstance(part, TextPart):
                    content.append({"type": text_type, "text": part.text})
                elif isinstance(part, ImagePart):
                    content.append({
                        "type": "input_image",
                        "image_url": f"data:{part.media_type};base64,{part.data}",
                        # "high" rather than "auto": the model must be able to read the
                        # field in every frame, as the other providers can.
                        "detail": "high",
                    })
            items.append({"role": msg.role, "content": content})

        # max_output_tokens covers reasoning tokens too.
        kwargs: dict = dict(model=self.model_id, input=items,
                            max_output_tokens=self.max_tokens,
                            reasoning={"effort": REASONING_EFFORT, "summary": "auto"})
        if instructions:
            kwargs["instructions"] = "\n\n".join(instructions)

        # Stream: high-effort reasoning turns can outlast the default HTTP timeout.
        async with self._client.responses.stream(**kwargs) as stream:
            response = await stream.get_final_response()

        summaries = []
        for item in response.output:
            if item.type == "reasoning":
                summaries.extend(s.text for s in (item.summary or []) if getattr(s, "text", None))
        self._last_reasoning = "\n\n".join(summaries)

        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", None)
            raise RuntimeError(f"{self.model_id} response incomplete ({reason}) — "
                               f"if max_output_tokens, raise max_tokens in config.yaml.")
        return response.output_text or ""
