from __future__ import annotations

from app.providers.anthropic import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.fake import FakeAdapter, FakeScript
from app.providers.gemini import GeminiAdapter
from app.providers.openai_compatible import OpenAICompatibleAdapter
from app.providers.openai_responses import OpenAIResponsesAdapter


class ProviderRegistry:
    def __init__(self, fake_script: FakeScript | None = None):
        self.fake_script = fake_script or FakeScript(delay_s=0.03)
        self._adapters: dict[str, ProviderAdapter] = {
            "openai_chat_compatible": OpenAICompatibleAdapter(),
            "openai_responses": OpenAIResponsesAdapter(),
            "anthropic_messages": AnthropicAdapter(),
            "gemini_generate_content": GeminiAdapter(),
            "fake": FakeAdapter(self.fake_script),
        }

    def get(self, protocol: str) -> ProviderAdapter:
        return self._adapters[protocol]

    def override(self, protocol: str, adapter: ProviderAdapter) -> None:
        self._adapters[protocol] = adapter
