"""C++: compiled - compile step, then run the produced executable."""

from __future__ import annotations

from .base import LanguageSpec, SandboxRunner, Step, write_source


class CppRunner(SandboxRunner):
    spec = LanguageSpec(
        id="cpp",
        label="C++",
        highlight="cpp",
        file_extension="cpp",
        required_binaries=("g++",),
        version_argv=("g++", "--version"),
        aliases=("c++", "cplusplus", "cxx", "c plus plus"),
    )

    def prepare(self, workdir: str, code: str) -> dict[str, str]:
        write_source(workdir, "main.cpp", code)
        return {}

    def steps(self, workdir: str, context: dict[str, str], memory_limit: int) -> list[Step]:
        return [
            # The compiler gets no address-space cap: cc1plus routinely maps
            # far more than a student's program will ever use.
            Step(
                ["g++", "-std=c++17", "-O1", "-pipe", "-o", "main", "main.cpp"],
                "compile",
                limit_address_space=False,
            ),
            Step(["./main"], "run"),
        ]
