from translator.engines.base import (
    ENGLISH_CODES, Engine, EngineError, RateLimitedError, placeholders,
)
from translator.engines.llm import AppleEngine, ClaudeEngine, GeminiEngine, OllamaEngine
from translator.engines.mt import AzureEngine, DeepLEngine, GoogleCloudEngine, GoogleFreeEngine

ENGINES: dict[str, type[Engine]] = {
    engine.name: engine
    for engine in (GoogleFreeEngine, DeepLEngine, AzureEngine, GoogleCloudEngine,
                   ClaudeEngine, GeminiEngine, OllamaEngine, AppleEngine)
}

# These run on the user's own machine, so the web server cannot offer them
LOCAL_ENGINES = {OllamaEngine.name, AppleEngine.name}


def create_engine(name: str, source: str, target: str, **options) -> Engine:
    if name not in ENGINES:
        raise EngineError(f"Unknown translation engine: {name}")
    return ENGINES[name](source, target, **options)


__all__ = [
    "ENGINES", "ENGLISH_CODES", "LOCAL_ENGINES", "Engine", "EngineError", "RateLimitedError",
    "create_engine", "placeholders",
]
