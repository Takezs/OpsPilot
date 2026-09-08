import pytest

from opspilot.config import Settings
from opspilot.evaluation.schemas import EvaluationConfiguration


def configuration(**changes):
    return EvaluationConfiguration.model_validate(
        {
            "model": "deepseek-v4-flash",
            "embedding_model": "bge-m3",
            "reranker_model": "bge-reranker-v2-m3",
            "top_k": 5,
            "prompt_version": "task18-rc1",
            "random_parameters": {"temperature": 0.0},
            "repetitions": 3,
            "concurrency": 3,
            **changes,
        }
    )


def test_receipt_configuration_drives_runtime_provider_settings():
    from opspilot.evaluation.runtime import configuration_settings

    selected = configuration_settings(Settings(), configuration())
    assert selected.deepseek_model == "deepseek-v4-flash"
    assert selected.bge_embedding_model == "bge-m3"
    assert selected.bge_reranker_model == "bge-reranker-v2-m3"


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt_version": "unknown"},
        {"random_parameters": {"unknown": 1}},
        {"random_parameters": {"temperature": float("nan")}},
    ],
)
def test_unsupported_snapshot_fails_before_provider_construction(changes):
    from opspilot.evaluation.runtime import configuration_settings

    with pytest.raises(ValueError):
        configuration_settings(Settings(), configuration(**changes))
