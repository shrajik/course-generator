"""Python: interpreted, one step."""

from __future__ import annotations

from .base import LanguageSpec, SandboxRunner, Step, write_source


class PythonRunner(SandboxRunner):
    spec = LanguageSpec(
        id="python",
        label="Python",
        highlight="python",
        file_extension="py",
        required_binaries=("python3",),
        version_argv=("python3", "--version"),
        aliases=("py", "python3", "python 3"),
    )

    def prepare(self, workdir: str, code: str) -> dict[str, str]:
        write_source(workdir, "main.py", code)
        return {}

    def steps(self, workdir: str, context: dict[str, str], memory_limit: int) -> list[Step]:
        # -I: isolated mode - ignore PYTHON* variables and user site-packages.
        # -B: do not write .pyc files into the workspace.
        return [Step(["python3", "-I", "-B", "main.py"], "run")]
