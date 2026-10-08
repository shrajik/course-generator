"""Behaviour of each runtime through the normalised interface.

These run against the real toolchains inside the executor image, so they
double as proof that the image installed what the registry advertises.
"""

from __future__ import annotations

import pytest

from app.runners import registry
from app.runners.java import main_class_name

TIMEOUT = 10
MEMORY = 256


async def run(language: str, code: str, *, timeout: int = TIMEOUT):
    runner = registry.get(language)
    assert runner is not None, f"{language} runtime is not installed"
    return await runner.execute(code, timeout=timeout, memory_limit=MEMORY)


# --- acceptance tests 1-4: the same program in four languages ----------------

HELLO = {
    "python": "print(10 + 20)",
    "javascript": "console.log(10 + 20)",
    "cpp": '#include <iostream>\nint main() {\n    std::cout << 10 + 20;\n    return 0;\n}\n',
    "java": (
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        System.out.println(10 + 20);\n"
        "    }\n"
        "}\n"
    ),
}


@pytest.mark.parametrize("language", sorted(HELLO))
async def test_each_language_prints_30(language: str) -> None:
    result = await run(language, HELLO[language])
    assert result.status == "success", result
    assert result.stdout.strip() == "30"
    assert result.language == language
    assert result.exit_code == 0
    assert result.execution_time > 0


def test_every_advertised_language_is_covered_by_the_acceptance_programs() -> None:
    """A newly registered language must come with an acceptance program."""
    assert {runner.spec.id for runner in registry.available()} == set(HELLO)


# --- acceptance test 5: runtime errors are controlled -------------------------

RUNTIME_ERRORS = {
    "python": "print(1 / 0)",
    "javascript": "throw new Error('boom')",
    "cpp": '#include <stdexcept>\nint main() { throw std::runtime_error("boom"); }\n',
    "java": (
        "public class Main { public static void main(String[] a) "
        '{ throw new RuntimeException("boom"); } }\n'
    ),
}


@pytest.mark.parametrize("language", sorted(RUNTIME_ERRORS))
async def test_runtime_error_is_a_normalised_error(language: str) -> None:
    result = await run(language, RUNTIME_ERRORS[language])
    assert result.status == "error"
    assert result.phase == "run"
    assert result.exit_code != 0
    assert result.stderr.strip(), "a runtime error must explain itself"


async def test_output_before_a_crash_is_kept() -> None:
    result = await run("python", "print('before')\nraise SystemExit(3)")
    assert result.status == "error"
    assert result.stdout.strip() == "before"
    assert result.exit_code == 3


# --- acceptance test 6: compilation errors ------------------------------------


async def test_cpp_compile_error_reports_the_compiler_message() -> None:
    result = await run("cpp", "int main() { return missing_symbol; }\n")
    assert result.status == "error"
    assert result.phase == "compile"
    assert "missing_symbol" in result.stderr


async def test_java_compile_error_reports_the_compiler_message() -> None:
    result = await run(
        "java", "public class Main { public static void main(String[] a) { int x = ; } }\n"
    )
    assert result.status == "error"
    assert result.phase == "compile"
    assert "error" in result.stderr.lower()


async def test_a_program_that_never_ran_has_no_stdout() -> None:
    result = await run("cpp", "this is not c++")
    assert result.phase == "compile"
    assert result.stdout == ""


# --- acceptance test 7: timeouts ----------------------------------------------

LOOPS = {
    "python": "while True:\n    pass\n",
    "javascript": "while (true) {}",
    "cpp": "int main() { for (;;) {} }\n",
    "java": "public class Main { public static void main(String[] a) { while (true) {} } }\n",
}


@pytest.mark.parametrize("language", sorted(LOOPS))
async def test_infinite_loop_is_terminated(language: str) -> None:
    result = await run(language, LOOPS[language], timeout=2)
    assert result.status == "timeout"
    assert "timed out" in result.stderr.lower()
    # Killed near the limit, not left running.
    assert result.execution_time < 8


# --- naming and edge cases ----------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("public class Main { }", "Main"),
        ("public class Calculator { public static void main(String[] a) {} }", "Calculator"),
        ("public final class Greeter { }", "Greeter"),
        ("class Helper { }\nclass App { public static void main(String[] a) {} }", "App"),
        ("// no class at all", "Main"),
    ],
)
def test_java_main_class_detection(source: str, expected: str) -> None:
    assert main_class_name(source) == expected


async def test_java_class_need_not_be_called_main() -> None:
    result = await run(
        "java",
        "public class Greeter { public static void main(String[] a) { System.out.println(\"hi\"); } }\n",
    )
    assert result.status == "success", result
    assert result.stdout.strip() == "hi"


async def test_stdin_is_empty_not_inherited() -> None:
    """A program that reads input must see EOF, not block forever."""
    result = await run("python", "import sys\nprint(repr(sys.stdin.read()))")
    assert result.status == "success"
    assert result.stdout.strip() == "''"


async def test_unicode_round_trips() -> None:
    result = await run("python", "print('héllo → wörld')")
    assert result.status == "success"
    assert "héllo → wörld" in result.stdout


# --- registry -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("Python", "python"),
        ("PY", "python"),
        ("C++", "cpp"),
        ("c++", "cpp"),
        ("js", "javascript"),
        ("Node.js", "javascript"),
        ("JAVA", "java"),
    ],
)
def test_registry_resolves_aliases(alias: str, expected: str) -> None:
    assert registry.resolve(alias) == expected


def test_registry_rejects_unknown_languages() -> None:
    assert registry.resolve("brainfuck") is None
    assert registry.get("brainfuck") is None


def test_a_language_without_its_toolchain_is_not_offered() -> None:
    """Registered but uninstalled must be indistinguishable from unknown."""
    from app.runners.base import LanguageSpec
    from app.runners.python import PythonRunner

    class Missing(PythonRunner):
        spec = LanguageSpec(
            id="ghost",
            label="Ghost",
            highlight="text",
            file_extension="gh",
            required_binaries=("definitely-not-installed-binary",),
        )

    from app.runners import RuntimeRegistry

    local = RuntimeRegistry((Missing(),))
    assert local.resolve("ghost") == "ghost"
    assert local.get("ghost") is None
    assert local.available() == []
