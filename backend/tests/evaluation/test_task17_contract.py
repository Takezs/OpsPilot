import uuid
from decimal import Decimal

import pytest

from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.evaluation.config import canonical_configuration, configuration_sha256
from opspilot.evaluation.schemas import EvaluationConfiguration
from opspilot.evaluation.service import EvaluationPermissionError, require_evaluation_admin
from opspilot.knowledge.schemas import AccessLevel


def _principal(role: Role) -> Principal:
    return Principal(
        user_id=str(uuid.uuid4()),
        role=role,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.PUBLIC,
    )


def test_canonical_configuration_has_fixed_utf8_vector() -> None:
    configuration = EvaluationConfiguration(
        model="deepseek-chat",
        embedding_model="bge-m3",
        reranker_model="bge-reranker-v2-m3",
        top_k=5,
        prompt_version="task17-v1",
        random_parameters={"temperature": Decimal("0.0"), "seed": 17},
        concurrency=3,
        repetitions=3,
    )

    canonical = canonical_configuration(configuration)

    assert canonical == (
        b'{"concurrency":3,"embedding_model":"bge-m3","model":"deepseek-chat",'
        b'"prompt_version":"task17-v1","random_parameters":{"seed":17,'
        b'"temperature":0.0},"repetitions":3,"reranker_model":'
        b'"bge-reranker-v2-m3","top_k":5}'
    )
    assert configuration_sha256(configuration) == (
        "09611f0db4693d940d38a763366060b0210bed4db18ecce6487fd61f08482941"
    )


@pytest.mark.parametrize("role", [Role.USER, Role.REVIEWER])
def test_only_admin_can_mutate_evaluation_execution(role: Role) -> None:
    with pytest.raises(EvaluationPermissionError):
        require_evaluation_admin(_principal(role))

    require_evaluation_admin(_principal(Role.ADMIN))
