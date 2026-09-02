import json
from pathlib import Path
from shutil import copy2

import pytest

from opspilot.evaluation.schemas import capture_frozen_dataset, frozen_dataset_identity


def _copy_bundle(target: Path) -> Path:
    source = Path(__file__).parents[3] / "evaluation" / "datasets"
    evaluation_root = target / "evaluation"
    target = evaluation_root / "datasets"
    target.mkdir(parents=True)
    for name in ("manifest.json", "test.jsonl", "agent_tasks.jsonl"):
        copy2(source / name, target / name)
    copy2(Path(__file__).parents[3] / "evaluation" / "generate_datasets.py", evaluation_root)
    return target


def test_identity_binds_test_and_agent_bytes(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path / "bundle")
    original = frozen_dataset_identity(bundle)

    agent_path = bundle / "agent_tasks.jsonl"
    agent_path.write_bytes(agent_path.read_bytes().replace(b"agent-001", b"agent-901", 1))
    changed = frozen_dataset_identity(bundle)
    assert changed["test_sha256"] == original["test_sha256"]
    assert changed["agent_tasks_sha256"] != original["agent_tasks_sha256"]

    with (bundle / "test.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="test dataset SHA-256 mismatch"):
        frozen_dataset_identity(bundle)


def test_identity_rejects_changed_generator_bytes(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path / "project")
    generator = bundle.parent / "generate_datasets.py"
    generator.write_bytes(generator.read_bytes() + b"\n# changed\n")

    with pytest.raises(ValueError, match="generator SHA-256 mismatch"):
        frozen_dataset_identity(bundle)


@pytest.mark.parametrize("generator", ["../outside.py", "C:/outside.py", "/outside.py"])
def test_identity_rejects_generator_path_escape(tmp_path: Path, generator: str) -> None:
    bundle = _copy_bundle(tmp_path / "project")
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["generator"] = generator
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="generator path"):
        frozen_dataset_identity(bundle)


def test_captured_snapshot_is_not_reopened_after_validation(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path / "project")
    snapshot = capture_frozen_dataset(bundle)
    original_ids = tuple(case.case_id for case in snapshot.test_cases)
    (bundle / "test.jsonl").write_text('{"case_id":"replacement"}\n', encoding="utf-8")

    assert tuple(case.case_id for case in snapshot.test_cases) == original_ids
    assert "replacement" not in original_ids


def test_identity_rejects_generator_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _copy_bundle(tmp_path / "project")
    generator = bundle.parent / "generate_datasets.py"
    real_is_symlink = Path.is_symlink

    def report_generator_symlink(path: Path) -> bool:
        return path == generator or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_generator_symlink)

    with pytest.raises(ValueError, match="symlink"):
        frozen_dataset_identity(bundle)
