"""Hash-only verification of the approved immutable evaluation image payload."""

import argparse
import json
from pathlib import Path

from opspilot.evaluation.schemas import frozen_dataset_identity


def verify_artifacts(root: Path, identity_file: Path) -> dict[str, str]:
    expected = json.loads(identity_file.read_text(encoding="utf-8"))
    actual = frozen_dataset_identity(root)
    if actual != expected:
        raise ValueError("immutable evaluation artifact identity mismatch")
    return actual


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()
    verify_artifacts(args.root, args.identity)
    print("immutable_evaluation_identity_verified=true")


if __name__ == "__main__":
    main()
