"""Explicit evaluation deployment configuration; no silent model fallback."""

import math
from pathlib import Path

from opspilot.config import Settings
from opspilot.evaluation.config import configuration_sha256
from opspilot.evaluation.schemas import EvaluationConfiguration


def configuration_settings(settings: Settings, configuration: EvaluationConfiguration) -> Settings:
    if configuration.prompt_version != "task18-rc1":
        raise ValueError("unsupported evaluation prompt version")
    if set(configuration.random_parameters) != {"temperature"}:
        raise ValueError("unsupported evaluation random parameters")
    temperature = configuration.random_parameters["temperature"]
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not 0 <= temperature <= 2
    ):
        raise ValueError("unsupported evaluation temperature")
    return settings.model_copy(
        update={
            "deepseek_model": configuration.model,
            "bge_embedding_model": configuration.embedding_model,
            "bge_reranker_model": configuration.reranker_model,
        }
    )


def deployment_configuration(settings: Settings) -> EvaluationConfiguration | None:
    if not settings.evaluation_configuration_file:
        return None
    configuration = EvaluationConfiguration.model_validate_json(
        Path(settings.evaluation_configuration_file).read_bytes()
    )
    configuration_settings(settings, configuration)
    if configuration_sha256(configuration) != settings.evaluation_configuration_sha:
        raise ValueError("evaluation deployment configuration identity mismatch")
    return configuration
