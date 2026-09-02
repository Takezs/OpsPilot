from pathlib import Path
from shutil import copy2

import pytest

from opspilot.evaluation.schemas import frozen_dataset_identity


def _copy_bundle(target: Path) -> Path:
    source = Path(__file__).parents[3] / "evaluation" / "datasets"
    target.mkdir()
    for name in ("manifest.json", "test.jsonl", "agent_tasks.jsonl"):
        copy2(source / name, target / name)
    return target


def test_identity_binds_test_and_agent_bytes(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path / "bundle")
    original = frozen_dataset_identity(bundle)

    with (bundle / "agent_tasks.jsonl").open("ab") as stream:
        stream.write(b"\n")
    changed = frozen_dataset_identity(bundle)
    assert changed["test_sha256"] == original["test_sha256"]
    assert changed["agent_tasks_sha256"] != original["agent_tasks_sha256"]

    with (bundle / "test.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="test dataset SHA-256 mismatch"):
        frozen_dataset_identity(bundle)
