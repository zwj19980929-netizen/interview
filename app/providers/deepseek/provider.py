from app.providers.openai_compatible.provider import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek's OpenAI-compatible Chat API with JSON-object structured output."""

    provider_id = "deepseek"
    structured_output_mode = "json_object"
