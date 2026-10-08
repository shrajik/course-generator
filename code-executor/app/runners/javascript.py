"""JavaScript on Node.js: interpreted, one step."""

from __future__ import annotations

from .base import LanguageSpec, SandboxRunner, Step, write_source


class JavaScriptRunner(SandboxRunner):
    spec = LanguageSpec(
        id="javascript",
        label="JavaScript",
        highlight="javascript",
        file_extension="js",
        required_binaries=("node",),
        version_argv=("node", "--version"),
        aliases=("js", "node", "nodejs", "node.js", "ecmascript"),
    )

    def prepare(self, workdir: str, code: str) -> dict[str, str]:
        write_source(workdir, "main.js", code)
        return {}

    def steps(self, workdir: str, context: dict[str, str], memory_limit: int) -> list[Step]:
        return [
            Step(
                [
                    "node",
                    # Node's permission model, as a second wall behind the
                    # uid drop and the firewall: the program may read only its
                    # own workspace and may not spawn processes or workers.
                    "--experimental-permission",
                    f"--allow-fs-read={workdir}",
                    f"--max-old-space-size={memory_limit}",
                    "--no-warnings",
                    "main.js",
                ],
                "run",
                # V8 reserves a large virtual range at start-up and aborts
                # under RLIMIT_AS; the heap flag above bounds it instead.
                limit_address_space=False,
            )
        ]
