"""Factory: EmbeddingConfig -> EmbeddingProvider instance."""
from __future__ import annotations

from datamind.config import EmbeddingConfig, LLMConfig
from datamind.core.registry import embedding_registry

# Import providers so they register on module load.
from . import providers  # noqa: F401


def build_embedding(cfg: EmbeddingConfig, *, fallback_llm: LLMConfig | None = None):
    """Instantiate the embedding provider specified by `cfg`.

    If cfg.api_key / cfg.api_base aren't set but a `fallback_llm` is given
    and the provider needs credentials (openai_compatible), we reuse the
    LLM gateway creds. This is the "one key, one gateway, two endpoints"
    deployment (which the user's 35.220.164.252:3888 is).
    """
    kwargs: dict = {
        "model": cfg.model,
        "batch_size": cfg.batch_size,
    }
    if cfg.provider in {"openai", "openai_compatible"}:
        kwargs.update({
            "timeout_s": cfg.timeout_s,
            "connect_timeout_s": cfg.connect_timeout_s,
            "max_retries": cfg.max_retries,
            "backoff_base_s": cfg.backoff_base_s,
            "max_batch_tokens": cfg.max_batch_tokens,
        })

    api_key = cfg.api_key
    api_base = cfg.api_base
    if fallback_llm is not None:
        if api_key is None:
            api_key = fallback_llm.api_key
        if api_base is None:
            api_base = fallback_llm.api_base

    if api_key is not None:
        kwargs["api_key"] = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
    if api_base is not None:
        kwargs["api_base"] = str(api_base).rstrip("/")
    if cfg.dimension is not None:
        kwargs["dimension"] = cfg.dimension
    return embedding_registry.create(cfg.provider, **kwargs)


__all__ = ["build_embedding"]
