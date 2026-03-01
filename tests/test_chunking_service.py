import json
from pathlib import Path

from app.services.chunking_service import (
    build_chunk_records,
    recursive_chunk_text,
    write_chunk_file,
)


def test_recursive_chunk_text_uses_recursive_boundaries_and_limits():
    text = (
        "Paragraph one explains the university research profile in detail.\n\n"
        "Paragraph two covers labs, professors, and coursework in artificial intelligence.\n\n"
        "Paragraph three covers funding, admissions, and program structure for graduate study."
    )

    chunks = recursive_chunk_text(
        text,
        chunk_size_chars=90,
        chunk_overlap_chars=15,
        separators=["\n\n", "\n", ". ", " ", ""],
        min_chunk_chars=20,
    )

    assert len(chunks) >= 2
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 90 for chunk in chunks)


def test_write_chunk_file_persists_chunk_manifest(tmp_path: Path):
    source_path = tmp_path / "university_test.md"
    source_path.write_text(
        "This university focuses on AI systems.\n\n"
        "It offers strong labs in robotics, ML infrastructure, and security.",
        encoding="utf-8",
    )

    chunk_records = build_chunk_records(
        source_path,
        source_path.read_text(encoding="utf-8"),
        chunk_size_chars=70,
        chunk_overlap_chars=10,
        separators=["\n\n", "\n", ". ", " ", ""],
        min_chunk_chars=10,
    )
    output_path = write_chunk_file(
        source_path,
        chunk_records,
        tmp_path / "chunks",
        chunk_size_chars=70,
        chunk_overlap_chars=10,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.name == "university_test.chunks.json"
    assert payload["source_file"] == "university_test.md"
    assert payload["chunk_count"] == len(chunk_records)
    assert payload["chunks"][0]["chunk_id"].startswith("university_test:")
