from .base import BaseModel
from .anthropic_model import AnthropicModel
from .openai_model import OpenAIModel
from .google_model import GoogleModel
from .meta_model import MetaModel


def create_model(cfg: dict, name: str) -> BaseModel:
    """Instantiate the correct model adapter from config."""
    model_cfg = cfg["models"][name]
    api_type = model_cfg["api_type"]
    api_key = cfg["api_keys"].get(api_type, "")

    if api_type == "anthropic":
        return AnthropicModel(name=name, model_id=model_cfg["model_id"],
                              api_key=api_key, max_tokens=model_cfg.get("max_tokens", 8096))
    if api_type == "openai":
        return OpenAIModel(name=name, model_id=model_cfg["model_id"],
                           api_key=api_key, max_tokens=model_cfg.get("max_tokens", 8096))
    if api_type == "google":
        return GoogleModel(name=name, model_id=model_cfg["model_id"],
                           api_key=api_key, max_tokens=model_cfg.get("max_tokens", 8096))
    if api_type == "meta":
        return MetaModel(name=name, model_id=model_cfg["model_id"],
                         api_key=api_key, max_tokens=model_cfg.get("max_tokens", 8096))
    raise ValueError(f"Unknown api_type '{api_type}' for model '{name}'")


__all__ = ["BaseModel", "AnthropicModel", "OpenAIModel", "GoogleModel", "MetaModel", "create_model"]
