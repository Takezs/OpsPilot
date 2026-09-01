"""Canonical, hash-stable evaluation configuration serialization."""

import hashlib
import json
from decimal import Decimal
from enum import Enum

from opspilot.evaluation.schemas import EvaluationConfiguration


def _normalize(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported configuration value: {type(value).__name__}")


def canonical_configuration(configuration: EvaluationConfiguration) -> bytes:
    normalized = _normalize(configuration.model_dump(mode="python"))
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def configuration_sha256(configuration: EvaluationConfiguration) -> str:
    return hashlib.sha256(canonical_configuration(configuration)).hexdigest()
