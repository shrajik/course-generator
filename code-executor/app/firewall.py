"""Network isolation for executed code.

Putting the executor on an `internal: true` Docker network stops code reaching
the internet, but not the *other containers on that network* - and the backend
must be on it to call us. An unauthenticated backend endpoint reachable from
inside the sandbox would let a submitted program spend the application's
OpenAI key. So the network is not trusted to do this alone.

Instead the kernel refuses to let the runner uid send a single packet:

    iptables -I OUTPUT -m owner --uid-owner <runner> -j DROP

`-m owner` matches locally generated traffic by the uid of the sending
process, which is precisely the property needed: the executor's own
(root) server process still answers the backend, while anything the sandbox
spawns - which always runs as the runner uid - is silenced, including
loopback. IPv6 gets the same rule so it cannot be used to route around v4.

This fails *closed*: if the rules cannot be installed and verified, `ready`
stays False and the service refuses to execute anything.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from . import config

log = logging.getLogger("executor.firewall")

_TOOLS = ("iptables", "ip6tables")


def _rule() -> list[str]:
    return ["-m", "owner", "--uid-owner", str(config.RUNNER_UID), "-j", "DROP"]


def _run(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tool, "-w", "5", *args], capture_output=True, text=True, timeout=15, check=False
    )


def install() -> tuple[bool, str]:
    """Install and verify the DROP rule for both IP families.

    Returns (ready, detail). Idempotent: an existing rule is not duplicated.
    """
    for tool in _TOOLS:
        if shutil.which(tool) is None:
            return False, f"{tool} is not installed"
        try:
            if _run(tool, "-C", "OUTPUT", *_rule()).returncode != 0:
                added = _run(tool, "-I", "OUTPUT", "1", *_rule())
                if added.returncode != 0:
                    return False, f"{tool} could not add the rule: {added.stderr.strip()}"
            if _run(tool, "-C", "OUTPUT", *_rule()).returncode != 0:
                return False, f"{tool} rule was not present after install"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{tool} failed: {exc}"

    log.info("Network isolation active for uid %s", config.RUNNER_UID)
    return True, "ok"
