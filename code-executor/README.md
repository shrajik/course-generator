# Code executor

Runs the code in a course's **code cells** (`code_cell` blocks) in a sandbox.
It is deliberately a separate service: the backend holds the database
credentials and the model API key, and must never run untrusted code itself.

```
Editor ── Run ──▶ POST /api/code/execute ──▶ backend ──▶ code-executor ──▶ sandboxed process
                                              (limits)    (runner per language)
```

The editor only knows a **language id** and some **code**. Which runtime
handles it, and how, is this service's concern. There is no per-language
endpoint anywhere.

## Isolation

Defence in depth — no single layer is trusted on its own.

| Layer | What it does | Where |
|---|---|---|
| Separate container | No application code, no DB driver, **no env_file / secrets** | `docker-compose.yml` |
| Internal network | `internal: true` — no route to the internet | `docker-compose.yml` |
| Firewall rule | Drops every packet the runner uid sends, **including to the backend** that shares the network | `app/firewall.py` |
| Unprivileged uid | Each run drops to uid 10001, no groups, `no_new_privs` | `app/sandbox.py` |
| rlimits | CPU, address space, processes, open files, file size | `app/sandbox.py` |
| Wall-clock timeout | Kills the whole process group | `app/sandbox.py` |
| Output cap | A program printing forever is killed, not buffered | `app/sandbox.py` |
| Minimal environment | Nothing inherited from the service | `app/sandbox.py` |
| Private workspace | Fresh tmpfs directory per run, always deleted | `app/sandbox.py` |
| Read-only rootfs, dropped caps, `init` | Only the capabilities needed to demote and firewall | `docker-compose.yml` |
| Node permission model | JavaScript may read only its own workspace | `app/runners/javascript.py` |

The service **fails closed**: if the firewall rule cannot be installed and
verified, `/health` reports unavailable and `/execute` refuses to run anything.

Why the firewall rule matters: the backend must share a network with this
service to call it, so the network alone would let submitted code reach
`backend:8000` (some of its endpoints are unauthenticated). The rule is what
closes that. `tests/test_sandbox.py` proves both halves.

### What this is not

This is good isolation, **not a hard security boundary**. Runs share one
container, so they are isolated from your backend and database but not
perfectly from each other, and a kernel-level escape is out of scope. For a
multi-tenant public deployment add gVisor or Firecracker.

## Adding a language

1. Subclass `SandboxRunner` in `app/runners/<language>.py`: say how to write
   the source (`prepare`) and which commands compile/run it (`steps`).
2. Add one line to `_ALL` in `app/runners/__init__.py`.
3. Install its toolchain in the `Dockerfile`.
4. Add its acceptance program to `tests/test_runners.py` (`HELLO`,
   `RUNTIME_ERRORS`, `LOOPS`). A test fails if a registered language has none.
5. Add its label to `LANGUAGE_LABELS` in
   `backend/app/course/document/code_cell.py` (used by the PDF).

The editor, the API and the PDF renderer need no change. A language is only
offered when its toolchain is actually installed.

Supported today: Python, JavaScript (Node 22), C++ (g++), Java (JDK 17).

## Limits

Chosen by the **backend** (`code_execution_*` in `app/core/config.py`), never
by the browser. This service enforces hard ceilings on top (`app/config.py`).
Network access cannot be requested — `network_enabled: true` is refused.
Packages cannot be installed at run time; toolchains are fixed at build time.

## Tests

They run in the real container, with its real capabilities, because the
guarantees are properties of the kernel — a mock would prove nothing.

```bash
docker compose run --rm --no-deps code-executor python -m pytest -q -p no:cacheprovider
```
