import json
from pathlib import Path
from app.services import embedding_ingestion_service


def test_ingest_configured_embedding_manifests_reads_embedding_files(tmp_path: Path, monkeypatch):
    embeddings_dir = tmp_path / "embeddings"
    embeddings_dir.mkdir(parents=True, exist_ok=True)
    (embeddings_dir / "sample.embeddings.json").write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "chunk_id": "sample:0000",
                        "chunk_index": 0,
                        "source_file": "sample.md",
                        "source_path": "/tmp/sample.md",
                        "content": "sample",
                        "char_count": 6,
                        "metadata": {"document_id": "sample"},
                        "embedding": [1.0, 2.0],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        embedding_ingestion_service,
        "_resolve_path",
        lambda _path: embeddings_dir,
    )
    monkeypatch.setattr(
        embedding_ingestion_service,
        "ingest_embedding_manifest",
        lambda payload: len(payload["chunks"]),
    )

    result = embedding_ingestion_service.ingest_configured_embedding_manifests()

    assert result == {"processed_files": 1, "processed_chunks": 1}
