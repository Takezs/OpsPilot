import hashlib
import json

from opspilot.evaluation.schemas import frozen_dataset_identity


def test_identity_preflight_hashes_bytes_without_parsing_cases(tmp_path):
    root = tmp_path / "evaluation" / "datasets"
    root.mkdir(parents=True)
    generator = root.parent / "generate.py"
    generator.write_bytes(b"# fixture generator")
    # Deliberately not JSON. Preflight must establish artifact identity without
    # opening the cases through the dataset loader or consuming their contents.
    test_bytes = b"opaque test fixture"
    (root / "test.jsonl").write_bytes(test_bytes)
    (root / "agent_tasks.jsonl").write_bytes(b"opaque agent fixture")
    manifest = {
        "schema_version": "1.0.0",
        "corpus_version": "preflight-fixture",
        "generator": "evaluation/generate.py",
        "generator_sha256": hashlib.sha256(generator.read_bytes()).hexdigest(),
        "generation_seed": 1,
        "dev_count": 60,
        "test_count": 140,
        "agent_task_count": 60,
        "attack_count": 40,
        "test_frozen": True,
        "test_frozen_at": "2026-01-01T00:00:00Z",
        "test_sha256": hashlib.sha256(test_bytes).hexdigest(),
        "final_test_executed": False,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert frozen_dataset_identity(root)["test_sha256"] == manifest["test_sha256"]
