import asyncio
import json
import os
import secrets
import socket
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from opspilot.demo_control import control_proof
from tests.e2e import control

ROOT = Path(__file__).parents[3]


@pytest.mark.integration
async def test_two_independent_processes_preserve_each_orders_effect(tmp_path):
    compose = os.getenv("OPSPILOT_LIVE_COMPOSE_E2E") == "1"
    secret = control.COMPOSE_SECRET if compose else secrets.token_bytes(32)
    assert secret is not None
    server = None
    image_history = b""
    secret_path = tmp_path / "control-secret"
    if compose:
        url = os.environ["OPSPILOT_LIVE_COMPOSE_PAYMENT_URL"]

        async def docker_output(*args):
            process = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            assert process.returncode == 0
            return stdout

        container_id = (
            (
                await docker_output(
                    "compose",
                    "--project-name",
                    os.environ["COMPOSE_PROJECT_NAME"],
                    "ps",
                    "-q",
                    "payment",
                )
            )
            .decode()
            .strip()
        )
        image_id = (
            (await docker_output("inspect", "--format", "{{.Image}}", container_id))
            .decode()
            .strip()
        )
        image_history = await docker_output("image", "history", "--no-trunc", image_id)
    else:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        secret_path.write_bytes(secret)
        secret_path.chmod(0o600)
        env = os.environ | {
            "OPSPILOT_DEMO_E2E": "true",
            "OPSPILOT_E2E_CONTROL_FILE": str(secret_path),
        }
        server = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app-dir",
            str(ROOT / "demo-services/payment_service"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    orders = [f"E2E-{uuid.uuid4().hex}" for _ in range(2)]
    children = []
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(url + "/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("payment health unavailable")
        env = os.environ.copy()
        env.pop("OPSPILOT_E2E_CONTROL_FILE", None)
        for order, other in (orders, orders[::-1]):
            children.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tests.e2e.payment_control_probe",
                    url,
                    order,
                    other,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        outputs = await asyncio.wait_for(
            asyncio.gather(*(p.communicate(secret) for p in children)), 30
        )
        canaries = [secret.hex().encode()] + [
            control_proof(secret, method, action, order).encode()
            for order in orders
            for method, action in [("POST", "reset"), ("GET", "count"), ("POST", "fault")]
        ]
        assert not any(value in output for pair in outputs for output in pair for value in canaries)
        assert not any(value in image_history for value in canaries)
        if compose:
            print("CONTROL_IMAGE_HISTORY_CANARY_ABSENT true")
        assert all(p.returncode == 0 for p in children)
        results = sorted((json.loads(stdout) for stdout, _ in outputs), key=lambda x: x["start"])
        assert results[0]["process_id"] != results[1]["process_id"]
        assert results[0]["end"] <= results[1]["start"]
        assert all(r["refund_count"] == 1 and r["timeout_after_effect"] for r in results)
        print("CONTROL_CONCURRENCY " + json.dumps(results))
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
        if server is not None:
            server.terminate()
            stdout, stderr = await asyncio.wait_for(server.communicate(), 10)
            assert secret.hex().encode() not in stdout + stderr
        secret_path.unlink(missing_ok=True)
