"""The security guarantees, tested as properties of running code.

Each test submits a program that *tries* to do the forbidden thing and asserts
it could not. They must run in the real container: the network and identity
guarantees are properties of the kernel and the capabilities it grants, so a
mock would prove nothing.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading

import pytest

from app import config, firewall
from app.runners import registry
from app.sandbox import workspace

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="sandbox guarantees only apply when the service runs as root"
)


@pytest.fixture(scope="module", autouse=True)
def isolated_network() -> None:
    ready, detail = firewall.install()
    assert ready, f"firewall could not be installed in this container: {detail}"


async def run_python(code: str, *, timeout: int = 10):
    runner = registry.get("python")
    assert runner is not None
    return await runner.execute(code, timeout=timeout, memory_limit=256)


# --- identity ---------------------------------------------------------------


async def test_code_runs_as_the_unprivileged_user_not_root() -> None:
    result = await run_python("import os; print(os.getuid(), os.getgid(), os.getgroups())")
    assert result.status == "success", result
    assert result.stdout.strip() == f"{config.RUNNER_UID} {config.RUNNER_GID} []"


async def test_code_cannot_regain_root() -> None:
    result = await run_python("import os\ntry:\n    os.setuid(0)\n    print('ROOT')\nexcept PermissionError:\n    print('denied')")
    assert result.stdout.strip() == "denied"


async def test_no_new_privs_is_set() -> None:
    result = await run_python(
        "print([l for l in open('/proc/self/status') if l.startswith('NoNewPrivs')][0].strip())"
    )
    assert result.stdout.strip().endswith("1")


# --- secrets ----------------------------------------------------------------


async def test_executor_environment_does_not_leak_into_code(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-visible")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db/x")
    result = await run_python("import os; print(sorted(os.environ))")
    assert result.status == "success"
    assert "OPENAI" not in result.stdout
    assert "DATABASE" not in result.stdout
    assert "sk-should-never-be-visible" not in result.stdout


async def test_environment_is_minimal_and_explicit() -> None:
    result = await run_python("import os; print(sorted(os.environ))")
    visible = set(eval(result.stdout))
    assert visible <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}


async def test_no_backend_secrets_exist_on_the_filesystem() -> None:
    result = await run_python(
        "import os\nprint(any(os.path.exists(p) for p in ('/app/.env', '/.env', '/srv/.env', '/app/app')))"
    )
    assert result.stdout.strip() == "False"


# --- network ----------------------------------------------------------------


async def test_code_cannot_open_a_connection_even_to_loopback() -> None:
    """Stands in for 'cannot reach the backend or the internet': the rule drops
    every packet the runner uid sends, so a reachable local listener is the
    cleanest observable."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    accepted = threading.Event()

    def accept() -> None:
        server.settimeout(8)
        try:
            server.accept()
            accepted.set()
        except OSError:
            pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        result = await run_python(
            "import socket\n"
            "s = socket.socket()\n"
            "s.settimeout(3)\n"
            "try:\n"
            f"    s.connect(('127.0.0.1', {port}))\n"
            "    print('CONNECTED')\n"
            "except OSError as e:\n"
            "    print('blocked')\n"
        )
    finally:
        server.close()
        thread.join(timeout=2)

    assert result.stdout.strip() == "blocked", result
    assert not accepted.is_set()


async def test_dns_and_outbound_http_fail() -> None:
    result = await run_python(
        "import socket\n"
        "socket.setdefaulttimeout(3)\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80))\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )
    assert result.stdout.strip() == "blocked"


async def test_the_executor_itself_can_still_make_connections() -> None:
    """The rule must silence the *runner*, not the service: the executor has to
    keep answering the backend."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=3)
    client.close()
    server.close()


def test_firewall_install_is_idempotent() -> None:
    first, _ = firewall.install()
    second, _ = firewall.install()
    assert first and second


# --- filesystem -------------------------------------------------------------


async def test_code_cannot_write_outside_its_workspace() -> None:
    result = await run_python(
        "for p in ('/etc/pwned', '/srv/pwned', '/usr/pwned', '/pwned'):\n"
        "    try:\n"
        "        open(p, 'w').write('x')\n"
        "        print('WROTE', p)\n"
        "    except OSError:\n"
        "        pass\n"
        "print('done')"
    )
    assert result.stdout.strip() == "done"


async def test_workspace_is_removed_after_every_run() -> None:
    before = set(os.listdir(config.WORK_ROOT)) if os.path.isdir(config.WORK_ROOT) else set()
    await run_python("open('left_behind.txt', 'w').write('x')")
    after = set(os.listdir(config.WORK_ROOT))
    assert after == before


async def test_runs_do_not_share_a_workspace() -> None:
    await run_python("open('state.txt', 'w').write('first run')")
    result = await run_python(
        "import os\nprint(os.path.exists('state.txt'))"
    )
    assert result.stdout.strip() == "False"


def test_workspace_is_private_to_the_runner() -> None:
    with workspace() as path:
        stat = os.stat(path)
        assert stat.st_mode & 0o777 == 0o700
        assert stat.st_uid == config.RUNNER_UID


# --- resource limits ----------------------------------------------------------


async def test_memory_hog_is_stopped_not_the_host() -> None:
    result = await run_python("data = bytearray(2_000_000_000)\nprint(len(data))")
    assert result.status == "error"
    assert "Memory" in result.stderr or "MemoryError" in result.stderr or result.exit_code != 0


async def test_fork_bomb_is_contained() -> None:
    result = await run_python(
        "import os\n"
        "for _ in range(5000):\n"
        "    try:\n"
        "        if os.fork() == 0:\n"
        "            import time; time.sleep(30)\n"
        "            os._exit(0)\n"
        "    except OSError:\n"
        "        print('limit reached')\n"
        "        break\n",
        timeout=5,
    )
    assert "limit reached" in result.stdout or result.status in {"timeout", "error"}
    # Whatever it forked must be gone once the run ends.
    await asyncio.sleep(0.5)
    survivors = [p for p in os.listdir("/proc") if p.isdigit() and _uid(p) == config.RUNNER_UID]
    assert survivors == []


async def test_background_process_does_not_outlive_the_run() -> None:
    await run_python(
        "import subprocess\n"
        "subprocess.Popen(['sleep', '60'], start_new_session=False)\n"
        "print('spawned')"
    )
    await asyncio.sleep(0.5)
    survivors = [p for p in os.listdir("/proc") if p.isdigit() and _uid(p) == config.RUNNER_UID]
    assert survivors == []


async def test_endless_output_is_capped_and_killed() -> None:
    result = await run_python(
        "import sys\nwhile True:\n    sys.stdout.write('x' * 100000)\n    sys.stdout.flush()",
        timeout=8,
    )
    assert len(result.stdout) <= config.MAX_OUTPUT_BYTES
    assert result.truncated is True
    assert result.execution_time < 8


async def test_large_file_writes_are_limited() -> None:
    result = await run_python(
        "try:\n"
        "    with open('big', 'wb') as f:\n"
        "        f.write(b'x' * (200 * 1024 * 1024))\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('limited')\n"
    )
    assert "WROTE" not in result.stdout


def _uid(pid: str) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


# --- concurrency (acceptance test 9: independent cells) ----------------------


async def test_concurrent_runs_are_independent() -> None:
    codes = [f"import time\ntime.sleep(0.2)\nprint({n})" for n in range(4)]
    results = await asyncio.gather(*(run_python(code) for code in codes))
    assert [r.stdout.strip() for r in results] == ["0", "1", "2", "3"]
    assert all(r.status == "success" for r in results)
