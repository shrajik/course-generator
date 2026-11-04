"""Cutover safety net: the pgvector capability check and the filesystem snapshot."""

from __future__ import annotations

import pytest

from app.db.pgvector_check import PgvectorStatus, parse_version
from app.scripts import kb_snapshot


@pytest.mark.parametrize(
    ("version", "hnsw", "iterative"),
    [("0.4.4", False, False), ("0.5.0", True, False), ("0.7.4", True, False), ("0.8.0", True, True), ("0.8", True, True), ("1.0.0", True, True)],
)
def test_pgvector_capabilities_follow_the_version(version, hnsw, iterative):
    status = PgvectorStatus(installed=True, version=version)
    assert (status.supports_hnsw, status.supports_iterative_scan) == (hnsw, iterative)


def test_a_missing_or_unreadable_pgvector_supports_nothing():
    assert parse_version(None) == () and parse_version("dev") == ()
    for status in (PgvectorStatus(installed=False), PgvectorStatus(installed=True, version="weird")):
        assert not status.supports_hnsw and not status.supports_iterative_scan


def _tree(root):
    (root / "sources" / "global" / "src_a").mkdir(parents=True)
    (root / "sources" / "global" / "src_a" / "chunks.json").write_text("[1]", encoding="utf-8")
    (root / "sources" / "global" / "src_a" / "vectors.bin").write_bytes(b"\x00\x01\x02")
    (root / "courses.txt").write_text("c", encoding="utf-8")


def test_a_snapshot_verifies_and_detects_every_kind_of_drift(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    archive = tmp_path / "snap.tar.gz"
    manifest = kb_snapshot.create(data, archive)
    assert set(manifest) == {"sources/global/src_a/chunks.json", "sources/global/src_a/vectors.bin", "courses.txt"}
    assert kb_snapshot.verify(archive, against=data) == []

    (data / "courses.txt").write_text("changed", encoding="utf-8")
    (data / "sources" / "global" / "src_a" / "vectors.bin").unlink()
    (data / "new.txt").write_text("n", encoding="utf-8")
    problems = kb_snapshot.verify(archive, against=data)
    assert any(p.startswith("changed since the snapshot: courses.txt") for p in problems)
    assert any(p.startswith("missing from") and "vectors.bin" in p for p in problems)
    assert any(p == "added since the snapshot: new.txt" for p in problems)
    assert kb_snapshot.verify(archive) == []  # the archive itself is still intact


def test_a_tampered_archive_or_missing_manifest_fails_verification(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    archive = tmp_path / "snap.tar.gz"
    kb_snapshot.create(data, archive)
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("x", encoding="utf-8")
    forged = tmp_path / "forged.tar.gz"
    kb_snapshot.create(other, forged)
    # The forged archive with the original archive's manifest no longer matches.
    (tmp_path / "forged.tar.gz.manifest.json").write_text((tmp_path / "snap.tar.gz.manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert kb_snapshot.verify(forged)
    (tmp_path / "snap.tar.gz.manifest.json").unlink()
    assert kb_snapshot.verify(archive) == [f"manifest missing: {archive}.manifest.json"]


def test_create_refuses_to_overwrite_and_to_snapshot_a_missing_directory(tmp_path):
    data = tmp_path / "data"
    _tree(data)
    archive = tmp_path / "snap.tar.gz"
    kb_snapshot.create(data, archive)
    with pytest.raises(SystemExit):
        kb_snapshot.create(data, archive)
    with pytest.raises(SystemExit):
        kb_snapshot.create(tmp_path / "nope", tmp_path / "x.tar.gz")
