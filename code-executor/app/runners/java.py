"""Java: compiled to bytecode with javac, then run on the JVM."""

from __future__ import annotations

import re

from .base import LanguageSpec, SandboxRunner, Step, write_source

_PUBLIC_CLASS = re.compile(
    r"\bpublic\s+(?:(?:final|abstract|strictfp)\s+)*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)"
)
_ANY_CLASS = re.compile(r"\bclass\s+([A-Za-z_]\w*)")
_MAIN = re.compile(r"\bstatic\s+void\s+main\s*\(")


def main_class_name(code: str) -> str:
    """The class `java` should launch.

    javac requires a public top-level class to live in a file of the same
    name, so the file must be named after it - hard-coding `Main.java` would
    reject any course example that names its class something else. With no
    public class, fall back to the class that declares `main`, then to Main.
    """
    public = _PUBLIC_CLASS.search(code)
    if public:
        return public.group(1)

    main = _MAIN.search(code)
    if main:
        # The nearest class declaration before the main method owns it.
        owners = [m for m in _ANY_CLASS.finditer(code) if m.start() < main.start()]
        if owners:
            return owners[-1].group(1)

    return "Main"


class JavaRunner(SandboxRunner):
    spec = LanguageSpec(
        id="java",
        label="Java",
        highlight="java",
        file_extension="java",
        required_binaries=("javac", "java"),
        version_argv=("java", "-version"),
        aliases=("jdk", "openjdk"),
    )

    def prepare(self, workdir: str, code: str) -> dict[str, str]:
        name = main_class_name(code)
        write_source(workdir, f"{name}.java", code)
        return {"class": name}

    def steps(self, workdir: str, context: dict[str, str], memory_limit: int) -> list[Step]:
        name = context["class"]
        return [
            Step(
                ["javac", "-J-Xmx256m", "-encoding", "UTF-8", f"{name}.java"],
                "compile",
                limit_address_space=False,
            ),
            Step(
                [
                    "java",
                    f"-Xmx{memory_limit}m",
                    "-Xss1m",
                    "-XX:+UseSerialGC",
                    "-XX:TieredStopAtLevel=1",
                    # No /tmp/hsperfdata: the filesystem is otherwise read-only.
                    "-XX:-UsePerfData",
                    f"-Djava.io.tmpdir={workdir}",
                    "-cp",
                    workdir,
                    name,
                ],
                "run",
                # The JVM reserves address space far beyond its heap.
                limit_address_space=False,
            ),
        ]
