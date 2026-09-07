import asyncio
import sys

import pytest

from opspilot.config import Settings
from tests.e2e import control


@pytest.mark.integration
async def test_lost_real_lock_connection_cancels_work(monkeypatch) -> None:
    original_connect = control.asyncpg.connect
    connected = asyncio.Event()
    connections = []
    continued = False

    async def capture_connection(*args, **kwargs):
        connection = await original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(control.asyncpg, "connect", capture_connection)

    async def work():
        nonlocal continued
        async with control.payment_control_lock(Settings().database_url):
            connected.set()
            await asyncio.sleep(5)
            continued = True

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(connected.wait(), 10)
        connections[0].terminate()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert not continued
        async with control.payment_control_lock(Settings().database_url, wait_seconds=1):
            pass
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.integration
async def test_advisory_lock_is_cross_process_and_times_out() -> None:
    script = """
import asyncio,sys
from tests.e2e.control import payment_control_lock
async def main():
    try:
        async with payment_control_lock(sys.argv[1], wait_seconds=float(sys.argv[2])):
            print('LOCKED', flush=True)
            await asyncio.to_thread(sys.stdin.readline)
    except TimeoutError:
        print('TIMEOUT', flush=True)
asyncio.run(main())
"""

    async def spawn(wait_seconds):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            Settings().database_url,
            str(wait_seconds),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    first = await spawn(2)
    try:
        assert (await asyncio.wait_for(first.stdout.readline(), 10)).strip() == b"LOCKED"
        second = await spawn(0.2)
        out, _ = await asyncio.wait_for(second.communicate(), 10)
        assert out.strip() == b"TIMEOUT"
        first.stdin.write(b"done\n")
        await first.stdin.drain()
        await asyncio.wait_for(first.wait(), 10)
        third = await spawn(2)
        out, _ = await asyncio.wait_for(third.communicate(b"done\n"), 10)
        assert out.strip() == b"LOCKED"
    finally:
        if first.returncode is None:
            first.kill()
            await first.wait()
