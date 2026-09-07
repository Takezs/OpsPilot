"""Opt-in isolated Compose acceptance with an in-memory control secret.

Run from backend with python -m tests.e2e.compose_acceptance PROJECT.
Only Payment mounts the temporary Linux secret volume, read-only at runtime.
The caller passes credentials to pytest children over anonymous stdin pipes.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from opspilot.config import Settings
from opspilot.demo_control import control_proof

ROOT = Path(__file__).parents[3]


def main(project: str) -> None:
    if re.fullmatch(r"opspilot_task18_rc_[a-z0-9_]+", project) is None:
        raise ValueError("isolated Task18 project required")
    settings = Settings()
    secret = secrets.token_bytes(32)
    orders: set[str] = set()
    env = os.environ | {
        "COMPOSE_PROJECT_NAME": project,
        "POSTGRES_HOST_PORT": "25432",
        "REDIS_HOST_PORT": "26379",
        "API_HOST_PORT": "28000",
        "PAYMENT_HOST_PORT": "28102",
        "WEB_HOST_PORT": "28088",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "OPSPILOT_DEMO_E2E": "true",
        "DEEPSEEK_API_KEY": settings.deepseek_api_key,
        "DEEPSEEK_BASE_URL": settings.deepseek_base_url,
        "DEEPSEEK_PROXY_URL": "http://host.docker.internal:7897",
        "BGE_BASE_URL": "http://host.docker.internal:8080/v1",
        "BGE_API_KEY": settings.bge_api_key,
        "BGE_EMBEDDING_MODEL": settings.bge_embedding_model,
        "BGE_RERANKER_MODEL": settings.bge_reranker_model,
    }
    env.pop("OPSPILOT_E2E_CONTROL_FILE", None)
    compose = ["docker", "compose", "--project-name", project]

    def inspect_canaries(raw: bytes) -> None:
        orders.update(re.findall(r"E2E-[0-9a-f]{32}", raw.decode(errors="replace")))
        values = [secret, secret.hex().encode()] + [
            control_proof(secret, method, action, order).encode()
            for order in orders
            for method, action in [("POST", "reset"), ("GET", "count"), ("POST", "fault")]
        ]
        if any(value in raw for value in values):
            raise RuntimeError("control credential canary detected; output suppressed")

    def run(command, *, data=None, child_env=None, cwd=ROOT, display=True):
        result = subprocess.run(
            command, input=data, capture_output=True, env=child_env or env, cwd=cwd
        )
        raw = result.stdout + result.stderr
        inspect_canaries(raw)
        if display:
            print(raw.decode(errors="replace"), end="", flush=True)
        if result.returncode:
            raise RuntimeError("acceptance command failed; see sanitized output")
        return result.stdout

    existing = run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        display=False,
    )
    existing_volumes = run(
        ["docker", "volume", "ls", "-q", "--filter", f"name=^{project}_"],
        display=False,
    )
    if existing.strip() or existing_volumes.strip():
        raise RuntimeError("refusing to reuse an existing project")
    run(compose + ["build"])
    try:
        run(compose + ["up", "-d", "postgres", "redis"])
        # The root helper is temporary. The service itself remains UID10001 and
        # mounts the same volume read-only; no key appears in Config.Env.
        init = (
            "import os,sys; from pathlib import Path; "
            "p=Path('/run/opspilot-e2e/control'); "
            "p.parent.mkdir(parents=True,exist_ok=True); "
            "value=sys.stdin.buffer.read(33); assert len(value)==32; "
            "p.write_bytes(value); os.chown(p,10001,10001); os.chmod(p,0o400)"
        )
        run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={project}",
                "--label",
                "com.docker.compose.volume=opspilot_e2e_control",
                f"{project}_opspilot_e2e_control",
            ]
        )
        run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--read-only",
                "--label",
                f"com.docker.compose.project={project}",
                "--user",
                "0",
                "--entrypoint",
                "python",
                "--mount",
                f"type=volume,source={project}_opspilot_e2e_control,target=/run/opspilot-e2e",
                f"{project}-payment",
                "-c",
                init,
            ],
            data=secret,
        )
        run(
            compose
            + ["run", "--rm", "--no-deps", "api", "alembic", "-c", "alembic.ini", "upgrade", "head"]
        )
        run(compose + ["up", "-d", "--wait"])
        run(
            compose
            + [
                "exec",
                "-T",
                "payment",
                "python",
                "-c",
                "import os,json; s=os.stat('/run/opspilot-e2e/control'); "
                "assert s.st_uid==10001 and s.st_mode & 0o777==0o400; "
                "print(json.dumps({'secret_owner_mode_correct':True}))",
            ]
        )
        ids = run(compose + ["ps", "-q"], display=False).decode().split()
        containers = json.loads(run(["docker", "inspect", *ids], display=False))
        snapshot = []
        for row in containers:
            service = row["Config"]["Labels"]["com.docker.compose.service"]
            mounts = [
                m for m in row["Mounts"] if m.get("Name") == f"{project}_opspilot_e2e_control"
            ]
            assert bool(mounts) == (service == "payment")
            assert not mounts or not mounts[0]["RW"]
            snapshot.append(
                {"service": service, "image": row["Image"], "started_at": row["State"]["StartedAt"]}
            )
        print("COMPOSE_IMAGES " + json.dumps(snapshot, sort_keys=True))
        print("CONTROL_SECRET_INSPECT_ABSENT true", flush=True)
        caller_env = env | {
            "DATABASE_URL": f"postgresql+asyncpg://opspilot:{env['POSTGRES_PASSWORD']}@127.0.0.1:25432/opspilot",
            "REDIS_URL": "redis://127.0.0.1:26379/0",
            "BGE_BASE_URL": "http://127.0.0.1:8080/v1",
            "DEEPSEEK_PROXY_URL": "http://127.0.0.1:7897",
            "OPSPILOT_LIVE_PROVIDER_E2E": "1",
            "OPSPILOT_LIVE_COMPOSE_E2E": "1",
            "OPSPILOT_LIVE_COMPOSE_API_URL": "http://127.0.0.1:28000",
            "OPSPILOT_LIVE_COMPOSE_PAYMENT_URL": "http://127.0.0.1:28102",
        }
        child = (
            "import sys; from tests.e2e import control; "
            "control.COMPOSE_SECRET=sys.stdin.buffer.read(32); "
            "import pytest; sys.exit(pytest.main(sys.argv[1:]))"
        )
        for label, target in [
            ("concurrent", "tests/e2e/test_control_concurrency.py"),
            (
                "single",
                "tests/e2e/test_task15_live_provider_flow.py::test_public_api_real_provider_refund_flow[0]",
            ),
            ("three", "tests/e2e/test_task15_live_provider_flow.py"),
        ]:
            print("ACCEPTANCE_STAGE " + label, flush=True)
            run(
                [
                    sys.executable,
                    "-c",
                    child,
                    "-q",
                    "-s",
                    "-p",
                    "no:cacheprovider",
                    "--tb=short",
                    f"--basetemp={ROOT / ('.pytest-' + project + '-' + label)}",
                    target,
                ],
                data=secret,
                child_env=caller_env,
                cwd=ROOT / "backend",
            )
        run(compose + ["logs", "--no-color"], display=False)
        print("CONTROL_SECRET_LOGS_AND_PYTEST_ABSENT true", flush=True)
        print(
            "SOURCE_DIFF_SHA "
            + hashlib.sha256(
                run(
                    [
                        "git",
                        "diff",
                        "HEAD",
                        "--",
                        "backend/src",
                        "demo-services",
                        "docker-compose.yml",
                    ],
                    display=False,
                )
            ).hexdigest()
        )
    finally:
        # Exact project only; unique ownership was checked before creation.
        try:
            run(compose + ["logs", "--no-color"], display=False)
        finally:
            run(compose + ["down", "--volumes"])


if __name__ == "__main__":
    main(sys.argv[1])
