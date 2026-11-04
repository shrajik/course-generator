"""Filesystem snapshot of the data directory, with a verifiable manifest.

Part of the knowledge-base cutover safety net (docs/KB_CUTOVER_RUNBOOK.md): before
the PostgreSQL knowledge base becomes the source of truth, the files it was
imported from are archived with a sha256 manifest, so they can be proven intact
later. Read-only with respect to the data directory.

    python -m app.scripts.kb_snapshot create --out /backups/data-20261102.tar.gz
    python -m app.scripts.kb_snapshot verify /backups/data-20261102.tar.gz
    python -m app.scripts.kb_snapshot verify /backups/data-20261102.tar.gz --against ./data

`create` writes the archive plus `<archive>.manifest.json` (path -> sha256, size).
`verify` re-reads the archive and checks it against its manifest; with `--against`
it also checks that a live directory still matches the snapshot (nothing missing,
changed or - reported separately - added since).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
from pathlib import Path

from app.core.config import get_settings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_of(directory: Path) -> dict[str, dict]:
    return {
        file.relative_to(directory).as_posix(): {"sha256": _sha256(file), "size": file.stat().st_size}
        for file in sorted(directory.rglob("*"))
        if file.is_file()
    }


def create(directory: Path, out: Path) -> dict[str, dict]:
    if not directory.is_dir():
        raise SystemExit(f"Not a directory: {directory}")
    if out.exists():
        raise SystemExit(f"Refusing to overwrite {out}")
    manifest = manifest_of(directory)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as archive:
        for relative in manifest:
            archive.add(directory / relative, arcname=relative, recursive=False)
    Path(f"{out}.manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _archive_manifest(archive_path: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            assert handle is not None
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            found[member.name] = {"sha256": digest.hexdigest(), "size": member.size}
    return found


def verify(archive_path: Path, against: Path | None = None) -> list[str]:
    """Problems found; an empty list means the snapshot is intact (and matches `against`)."""
    manifest_path = Path(f"{archive_path}.manifest.json")
    if not manifest_path.exists():
        return [f"manifest missing: {manifest_path}"]
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    actual = _archive_manifest(archive_path)
    for path in sorted(expected.keys() | actual.keys()):
        if path not in actual:
            problems.append(f"archive is missing {path}")
        elif path not in expected:
            problems.append(f"archive has an unlisted file {path}")
        elif actual[path] != expected[path]:
            problems.append(f"archive content differs from its manifest: {path}")
    if against is not None:
        live = manifest_of(against)
        for path in sorted(expected.keys() | live.keys()):
            if path not in live:
                problems.append(f"missing from {against}: {path}")
            elif path not in expected:
                problems.append(f"added since the snapshot: {path}")
            elif live[path] != expected[path]:
                problems.append(f"changed since the snapshot: {path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create")
    make.add_argument("--out", type=Path, required=True)
    make.add_argument("--dir", type=Path, default=None, help="default: the configured DATA_DIR")
    check = commands.add_parser("verify")
    check.add_argument("archive", type=Path)
    check.add_argument("--against", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.command == "create":
        directory = args.dir or Path(get_settings().data_dir)
        manifest = create(directory, args.out)
        print(f"Snapshot of {directory}: {len(manifest)} files -> {args.out}")
        return 0
    problems = verify(args.archive, args.against)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("Snapshot verified." if not problems else f"{len(problems)} problem(s) found.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
