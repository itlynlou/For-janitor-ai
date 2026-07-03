"""
Configuration for the OpenAI -> NVIDIA NIM proxy.

Loads settings from environment variables (or a .env file) and defines
the mapping between OpenAI model names and NVIDIA NIM model names.
"""
import json
import os
from functools import lru_cache
from typing import Dict, Optional

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator

# ---------------------------------------------------------------------------
# Curated NVIDIA NIM "Free Endpoint" models (checked against build.nvidia.com/
# models on 2026-07-03). NVIDIA's free catalog changes often — models get
# added, renamed, or deprecated with little notice — so treat this as a
# convenient starting point, not a permanent guarantee. Re-check
# https://build.nvidia.com/models?filters=free_endpoint before relying on it
# for anything long-lived, or call GET /free-models on this proxy, which
# serves this same list.
# ---------------------------------------------------------------------------
FREE_NIM_MODELS: Dict[str, Dict[str, str]] = {
    "z-ai/glm-5.2": {
        "description": "Flagship LLM for agentic workflows, coding, and long-horizon reasoning.",
        "category": "chat",
    },
    "z-ai/glm-5.1": {
        "description": "Prior-gen flagship: agentic workflows, coding, long-horizon reasoning.",
        "category": "chat",
    },
    "deepseek-ai/deepseek-v4-pro": {
        "description": "1M-token context MoE model, strong at coding and agentic tasks.",
        "category": "chat",
    },
    "deepseek-ai/deepseek-v4-flash": {
        "description": "284B MoE, 1M-token context, optimized for fast coding/agents.",
        "category": "chat",
    },
    "moonshotai/kimi-k2.6": {
        "description": "1T-param multimodal MoE for long-horizon coding, tool use, image/video understanding.",
        "category": "chat",
    },
    "minimaxai/minimax-m3": {
        "description": "Multimodal MoE with strong reasoning, coding, and tool-calling.",
        "category": "chat",
    },
    "stepfun-ai/step-3.7-flash": {
        "description": "Sparse MoE multimodal reasoning model for enterprise/agentic/coding tasks.",
        "category": "chat",
    },
    "mistralai/mistral-medium-3.5-128b": {
        "description": "General-purpose text generation, coding, and agentic use cases.",
        "category": "chat",
    },
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "description": "Hybrid Mamba-Transformer MoE, 1M context; agentic reasoning, planning, tool calling.",
        "category": "chat",
    },
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": {
        "description": "Omni-modal reasoning model — understands images, video, speech, and text.",
        "category": "chat",
    },
    "google/diffusiongemma-26b-a4b-it": {
        "description": "Diffusion-based 26B LLM enabling parallel token generation for real-time apps.",
        "category": "chat",
    },
}

# Sensible default: a strong, general-purpose free chat model.
DEFAULT_FREE_MODEL = "z-ai/glm-5.2"

# Friendly OpenAI-style aliases -> free NIM models, so `gpt-4o` etc. "just work"
# out of the box without the user editing MODEL_MAP_JSON themselves.
DEFAULT_MODEL_MAP: Dict[str, str] = {
    "gpt-4o": "z-ai/glm-5.2",
    "gpt-4o-mini": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "gpt-4-turbo": "deepseek-ai/deepseek-v4-pro",
    "gpt-4.1": "deepseek-ai/deepseek-v4-pro",
    "gpt-4.1-mini": "mistralai/mistral-medium-3.5-128b",
    "gpt-3.5-turbo": "stepfun-ai/step-3.7-flash",
    "o1": "nvidia/nemotron-3-ultra-550b-a55b",
    "o1-mini": "moonshotai/kimi-k2.6",
}


class Settings(BaseSettings):
    # Where NIM actually lives. Defaults to NVIDIA's hosted "integrate" endpoint,
    # but point this at a self-hosted NIM container instead, e.g.
    # http://localhost:8001/v1
    nim_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1",
        description="Base URL of the target NIM OpenAI-compatible API",
    )

    # NVIDIA API key used to authenticate to NIM. Get a free one (no credit
    # card required) at https://build.nvidia.com -> API Keys. Required even
    # for the "free" models — free means no charge, not unauthenticated.
    nim_api_key: Optional[str] = Field(default=None, description="NVIDIA NIM API key")

    # If set, incoming requests must present this key via Authorization: Bearer <key>
    # or the x-api-key header. Leave unset to accept any/no client key (e.g. local dev).
    proxy_api_key: Optional[str] = Field(default=None)

    # JSON string mapping OpenAI model names -> NIM model names. Empty by
    # default so DEFAULT_MODEL_MAP (free NIM models) is used out of the box;
    # set this env var to override/extend it.
    model_map_json: str = Field(default="")

    # If an incoming model name isn't in the map, pass it through unchanged
    # (useful when callers already send native NIM model names like "z-ai/glm-5.2").
    passthrough_unmapped_models: bool = True

    # Default model to use if the client omits "model" entirely. Defaults to
    # a free NIM model so the proxy works with zero configuration beyond an API key.
    default_model: str = DEFAULT_FREE_MODEL

    # Networking
    request_timeout_seconds: float = 120.0
    max_retries: int = 2

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    class Config:
        env_file = ".env"
        env_prefix = ""

    @field_validator("default_model", mode="before")
    @classmethod
    def _fallback_default_model(cls, v: Optional[str]) -> str:
        # An explicitly-blank DEFAULT_MODEL= in .env should still mean
        # "use the built-in free-model default", not an empty model string.
        return v if v else DEFAULT_FREE_MODEL

    @property
    def model_map(self) -> Dict[str, str]:
        """OpenAI-name -> NIM-name mapping actually in effect.

        If MODEL_MAP_JSON is unset, ship with DEFAULT_MODEL_MAP (free models)
        so the proxy is useful immediately. If it's set, it fully replaces
        the default — set it to override, not append.
        """
        if not self.model_map_json:
            return dict(DEFAULT_MODEL_MAP)
        try:
            return json.loads(self.model_map_json)
        except json.JSONDecodeError:
            return dict(DEFAULT_MODEL_MAP)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_model(openai_model: str, settings: Settings) -> str:
    """Translate an OpenAI-style model name into the NIM model name to call."""
    mapping = settings.model_map
    if openai_model in mapping:
        return mapping[openai_model]
    if settings.passthrough_unmapped_models:
        return openai_model
    raise ValueError(f"No NIM mapping configured for model '{openai_model}'")
