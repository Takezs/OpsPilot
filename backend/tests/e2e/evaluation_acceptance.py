"""Isolated Compose smoke of the real executor using only synthetic cases."""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from opspilot.config import Settings
from opspilot.demo_control import control_headers, control_proof

ROOT = Path(__file__).parents[3]

SEED = """
import asyncio,json,sys,uuid
sys.path.insert(0,'/app')
from evaluation_fixture_worker import snapshot
from opspilot.auth.models import Role,User
from opspilot.auth.service import AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.evaluation.models import EvaluationRun,EvaluationTestExecution
from opspilot.evaluation.runtime import deployment_configuration
from opspilot.evaluation.config import configuration_sha256
async def main():
 data=json.load(sys.stdin); settings=Settings(); configuration=deployment_configuration(settings)
 ids={role:uuid.uuid4() for role in data}; run_id=uuid.uuid4(); execution_id=uuid.uuid4()
 fixture=snapshot(); sha=fixture.identity['synthetic_fixture_sha']
 async with async_session_factory() as session:
  auth=AuthService(settings.jwt_secret)
  for role,credentials in data.items():
   session.add(User(id=ids[role],username=credentials['username'],password_hash=auth.hash_password(credentials['password']),role=Role(role),allowed_departments=[],max_access_level=1))
  session.add(EvaluationRun(id=run_id,dataset_version='task18-synthetic',dataset_sha256=sha,status='PENDING',model=configuration.model,embedding_model=configuration.embedding_model,reranker_model=configuration.reranker_model,top_k=configuration.top_k,prompt_version=configuration.prompt_version,random_parameters=configuration.random_parameters,configuration=configuration.model_dump(mode='json')))
  await session.flush()
  session.add(EvaluationTestExecution(id=execution_id,evaluation_run_id=run_id,dataset_version='task18-synthetic',dataset_sha=sha,configuration=configuration.model_dump(mode='json'),configuration_sha=configuration_sha256(configuration),dataset_identity=fixture.identity,status='FROZEN',frozen_by_user_id=ids['ADMIN']))
  await session.commit()
 print(json.dumps({'execution_id':str(execution_id),'run_id':str(run_id),'fixture_sha':sha}))
asyncio.run(main())
"""


def verify_refunds(client, credentials, secret, orders, report, run, compose):
    response = client.post("/auth/login", json=credentials)
    response.raise_for_status()
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    evaluation_run_id = str(uuid.UUID(report["run_id"]))
    sql = (
        "SELECT json_agg(json_build_object('case_id',dataset_case_id,'repetition',repetition,"
        "'actual',actual_output)) FROM evaluation_cases "
        f"WHERE evaluation_run_id='{evaluation_run_id}'"
    )
    rows = json.loads(
        run(
            compose
            + ["exec", "-T", "postgres", "psql", "-U", "opspilot", "-d", "opspilot", "-Atc", sql],
            display=False,
        )
    )
    assert len(rows) == 6
    assert len({(row["case_id"], row["repetition"]) for row in rows}) == 6
    refunds = [row for row in rows if row["case_id"] == "synthetic-refund"]
    assert len(refunds) == 3
    for row in refunds:
        actual = row["actual"]
        assert actual["final_state"] == "SUCCEEDED"
        operation = actual["operations"][0]
        order = operation["normalized_arguments"]["order_number"]
        assert order == orders[row["repetition"] - 1]
        run_id = str(uuid.UUID(actual["run_id"]))
        response = client.get(f"/runs/{run_id}/history?limit=500", headers=headers)
        response.raise_for_status()
        history = response.json()
        seq = [event["seq"] for event in history]
        assert seq == list(range(1, len(seq) + 1))
        types = {event["event_type"] for event in history}
        assert {
            "operation_outcome_unknown",
            "operation_reconciliation_started",
            "operation_reconciled_succeeded",
        } <= types
        sql = f"SELECT seq FROM run_events WHERE run_id='{run_id}' ORDER BY seq"
        pg = run(
            compose
            + ["exec", "-T", "postgres", "psql", "-U", "opspilot", "-d", "opspilot", "-Atc", sql],
            display=False,
        )
        assert [int(value) for value in pg.split()] == seq
        with httpx.Client(base_url="http://127.0.0.1:28104") as payment:
            response = payment.get(
                f"/__e2e/refunds/{order}/count",
                headers=control_headers(secret, "GET", "count", order),
            )
            response.raise_for_status()
            assert response.json() == {"count": 1}
        print(
            "SYNTHETIC_REFUND_EVIDENCE "
            + json.dumps(
                {
                    "repetition": row["repetition"],
                    "run_id": run_id,
                    "order": order,
                    "journal_count": len(seq),
                    "journal_contiguous": True,
                    "reconciled": True,
                    "refund_count": 1,
                }
            ),
            flush=True,
        )


def main(project: str, runtime_image: str, fixture_image: str, mode: str = "greeting") -> None:
    if mode not in {"greeting", "refund"}:
        raise ValueError("unknown synthetic fixture mode")
    refund_mode = mode == "refund"
    control_secret = secrets.token_bytes(32)
    orders = [
        "E2E-" + hashlib.sha256(f"{project}:{repetition}".encode()).hexdigest()[:32]
        for repetition in range(1, 4)
    ]
    if re.fullmatch(r"opspilot_task18_eval_[a-z0-9_]+", project) is None:
        raise ValueError("isolated evaluation project required")
    if not all(
        re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in (runtime_image, fixture_image)
    ):
        raise ValueError("immutable image IDs required")
    settings = Settings()
    credentials = {
        role: {
            "username": f"smoke-{role.lower()}-{secrets.token_hex(6)}",
            "password": secrets.token_urlsafe(32),
        }
        for role in ("USER", "REVIEWER", "ADMIN")
    }
    pg_password = secrets.token_urlsafe(32)
    runtime = {
        "database_url": f"postgresql+asyncpg://opspilot:{pg_password}@postgres:5432/opspilot",
        "jwt_secret": secrets.token_urlsafe(48),
        "deepseek_api_key": settings.deepseek_api_key,
        "bge_api_key": settings.bge_api_key,
    }
    canaries = [
        value.encode()
        for value in [
            pg_password,
            *runtime.values(),
            *(item["password"] for item in credentials.values()),
        ]
        if len(value) > 8
    ]

    def scan(raw):
        control_canaries = [control_secret, control_secret.hex().encode()] + [
            control_proof(control_secret, method, action, order).encode()
            for order in orders
            for method, action in [("POST", "reset"), ("GET", "count")]
        ]
        if any(value in raw for value in canaries + control_canaries):
            raise RuntimeError("credential canary detected; output suppressed")

    with tempfile.TemporaryDirectory(prefix="opspilot-eval-secret-", dir=ROOT.parent) as temp:
        directory = Path(temp).resolve()
        assert directory.parent == ROOT.parent.resolve()
        for name, value in (
            ("runtime", runtime),
            ("user", credentials["USER"]),
            ("reviewer", credentials["REVIEWER"]),
        ):
            (directory / name).write_text(json.dumps(value), encoding="utf-8")
            (directory / name).chmod(0o400)
        (directory / "postgres").write_text(pg_password, encoding="utf-8")
        (directory / "postgres").chmod(0o400)
        env = os.environ | {
            "COMPOSE_PROJECT_NAME": project,
            "EVALUATION_IMAGE": runtime_image,
            "EVALUATION_FIXTURE_IMAGE": fixture_image,
            "EVALUATION_RUNTIME_CREDENTIALS_PATH": str(directory / "runtime"),
            "EVALUATION_USER_CREDENTIALS_PATH": str(directory / "user"),
            "EVALUATION_REVIEWER_CREDENTIALS_PATH": str(directory / "reviewer"),
            "EVALUATION_POSTGRES_CREDENTIALS_PATH": str(directory / "postgres"),
            "POSTGRES_HOST_PORT": "25434",
            "REDIS_HOST_PORT": "26381",
            "API_HOST_PORT": "28002",
            "PAYMENT_HOST_PORT": "28104",
            "WEB_HOST_PORT": "28090",
            "DEEPSEEK_PROXY_URL": "http://host.docker.internal:7897",
            "BGE_BASE_URL": "http://host.docker.internal:8080/v1",
            "DEEPSEEK_BASE_URL": settings.deepseek_base_url,
            "JWT_SECRET": "",
            "DEEPSEEK_API_KEY": "",
            "BGE_API_KEY": "",
            "OPSPILOT_DEMO_E2E": "false",
            "OPSPILOT_EVAL_FAULT_MATRIX": "false",
        }
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(ROOT / "deploy/compose.evaluation.yml"),
            "-f",
            str(ROOT / "deploy/compose.evaluation-fixture.yml"),
        ]
        if refund_mode:
            env["OPSPILOT_SYNTHETIC_REFUND_NAMESPACE"] = project
            compose += ["-f", str(ROOT / "deploy/compose.evaluation-refund-fixture.yml")]

        def run(command, data=None, display=True):
            result = subprocess.run(command, input=data, capture_output=True, env=env, cwd=ROOT)
            raw = result.stdout + result.stderr
            scan(raw)
            if display:
                print(raw.decode(errors="replace"), end="", flush=True)
            if result.returncode:
                raise RuntimeError("synthetic acceptance command failed")
            return result.stdout

        if run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            display=False,
        ).strip():
            raise RuntimeError("existing project refused")
        if run(
            ["docker", "volume", "ls", "-q", "--filter", f"name=^{project}_"], display=False
        ).strip():
            raise RuntimeError("existing project volume refused")
        try:
            run(compose + ["build", "order", "payment", "email", "web"])
            if refund_mode:
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
                init = (
                    "import os,sys; from pathlib import Path; "
                    "p=Path('/run/opspilot-e2e/control'); "
                    "value=sys.stdin.buffer.read(33); assert len(value)==32; "
                    "p.write_bytes(value); os.chown(p,10001,10001); os.chmod(p,0o400)"
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
                        "--user",
                        "0",
                        "--entrypoint",
                        "python",
                        "--mount",
                        f"type=volume,source={project}_opspilot_e2e_control,target=/run/opspilot-e2e",
                        runtime_image,
                        "-c",
                        init,
                    ],
                    data=control_secret,
                )
            run(compose + ["up", "-d", "--wait", "postgres", "redis"])
            run(compose + ["run", "--rm", "--no-deps", "api", "alembic", "upgrade", "head"])
            seeded = json.loads(
                run(
                    compose + ["run", "--rm", "-T", "--no-deps", "worker", "python", "-c", SEED],
                    json.dumps(credentials).encode(),
                    display=False,
                )
            )
            print("SYNTHETIC_SEEDED " + json.dumps(seeded), flush=True)
            run(compose + ["up", "-d", "--wait"])
            ids = run(compose + ["ps", "-q"], display=False).decode().split()
            inspection = json.loads(run(["docker", "inspect", *ids], display=False))
            for container in inspection:
                service = container["Config"]["Labels"]["com.docker.compose.service"]
                if service in {"api", "worker", "outbox-publisher"}:
                    assert container["HostConfig"]["ReadonlyRootfs"]
                    assert container["Config"]["User"] == "opspilot"
                    assert all(
                        not mount["RW"]
                        for mount in container["Mounts"]
                        if mount["Destination"].startswith("/run/secrets/")
                    )
                enabled = "OPSPILOT_DEMO_E2E=true" in container["Config"]["Env"]
                assert enabled == (refund_mode and service in {"order", "payment"})
                controls = [
                    m for m in container["Mounts"] if m["Destination"] == "/run/opspilot-e2e"
                ]
                assert bool(controls) == (refund_mode and service == "payment")
                assert not controls or not controls[0]["RW"]
            if refund_mode:
                with httpx.Client(base_url="http://127.0.0.1:28104") as payment:
                    for order in orders:
                        response = payment.post(
                            f"/__e2e/refunds/{order}/reset",
                            headers=control_headers(control_secret, "POST", "reset", order),
                        )
                        response.raise_for_status()
            history = run(
                ["docker", "image", "history", "--no-trunc", runtime_image], display=False
            )
            scan(history)
            print("SYNTHETIC_IMAGE_ENV_SECRET_CANARY_ABSENT true", flush=True)
            with httpx.Client(base_url="http://127.0.0.1:28002/api/v1", timeout=30) as client:
                response = client.post("/auth/login", json=credentials["ADMIN"])
                response.raise_for_status()
                token = response.json()["access_token"]
                canaries.append(token.encode())
                headers = {"Authorization": "Bearer " + token}
                execution_id = seeded["execution_id"]
                response = client.post(f"/evaluations/{execution_id}/start", headers=headers)
                response.raise_for_status()
                if refund_mode:
                    # Deliver the same execution ID twice through real Redis.
                    duplicate = (
                        "import asyncio,sys; "
                        "from arq.connections import create_pool,RedisSettings; "
                        "from opspilot.config import Settings\n"
                        "async def main():\n"
                        " r=await create_pool(RedisSettings.from_dsn(Settings().redis_url))\n"
                        " await r.enqueue_job('process_evaluation_execution',sys.argv[1])\n"
                        " await r.aclose()\n"
                        "asyncio.run(main())"
                    )
                    run(
                        compose + ["exec", "-T", "worker", "python", "-c", duplicate, execution_id],
                        display=False,
                    )
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    response = client.get(f"/evaluations/{execution_id}", headers=headers)
                    response.raise_for_status()
                    state = response.json()["status"]
                    if state in {"COMPLETED", "FAILED", "CANCELLED"}:
                        break
                    time.sleep(2)
                else:
                    raise TimeoutError("synthetic execution timed out")
                print("SYNTHETIC_FINAL_STATUS " + state, flush=True)
                assert state == "COMPLETED"
                first = client.get(f"/evaluations/{execution_id}/report.json", headers=headers)
                second = client.get(f"/evaluations/{execution_id}/report.json", headers=headers)
                first.raise_for_status()
                second.raise_for_status()
                scan(first.content)
                assert first.content == second.content
                print(
                    "SYNTHETIC_REPORT_SHA " + hashlib.sha256(first.content).hexdigest(), flush=True
                )
                if refund_mode:
                    verify_refunds(
                        client,
                        credentials["USER"],
                        control_secret,
                        orders,
                        first.json(),
                        run,
                        compose,
                    )
            logs = run(compose + ["logs", "--no-color"], display=False)
            assert b"SYNTHETIC_ACTIVE 3" in logs
            assert b"SYNTHETIC_ACTIVE 4" not in logs
            assert b"SYNTHETIC_PREFLIGHT_PASSED true" in logs
            print("SYNTHETIC_PEAK_CONCURRENCY 3", flush=True)
            print("SYNTHETIC_LOG_SECRET_CANARY_ABSENT true", flush=True)
            run(
                compose
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "opspilot",
                    "-d",
                    "opspilot",
                    "-Atc",
                    "SELECT dataset_case_id,repetition FROM evaluation_cases "
                    "ORDER BY dataset_case_id,repetition",
                ]
            )
        except BaseException:
            logs = run(compose + ["logs", "--no-color"], display=False)
            for line in logs.decode(errors="replace").splitlines():
                if any(marker in line for marker in ("Error", "failed", "SYNTHETIC_")):
                    print(line, flush=True)
            raise
        finally:
            try:
                run(compose + ["logs", "--no-color"], display=False)
            finally:
                run(compose + ["down", "--volumes"])


if __name__ == "__main__":
    main(*sys.argv[1:])
