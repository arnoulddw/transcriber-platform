def normalize_openrouter_model(raw) -> str:
    if raw is None:
        raise ValueError("OpenRouter model is required.")
    value = str(raw).strip()
    if not value:
        raise ValueError("OpenRouter model is required.")
    if " " in value or "/" not in value or len(value) > 120:
        raise ValueError(
            "OpenRouter model must look like vendor/model "
            "(e.g. openai/gpt-transcribe)."
        )
    return value
