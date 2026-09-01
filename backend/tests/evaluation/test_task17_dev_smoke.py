from pathlib import Path

from opspilot.evaluation.dev_smoke import load_dev_smoke_cases


def test_dev_smoke_reads_only_dev_split(monkeypatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_load(path: Path, model: object):
        seen.append(path.name)
        return tuple()

    monkeypatch.setattr("opspilot.evaluation.dev_smoke.load_jsonl", fake_load)
    assert load_dev_smoke_cases(tmp_path) == ()
    assert seen == ["dev.jsonl"]
