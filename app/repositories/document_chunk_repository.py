import json
from app.core.config import get_settings
from app.infra.postgres_client import get_postgres_pool

settings = get_settings()


def _qualified_chunk_table() -> str:
    """Return the fully qualified retrieval chunk table name from config."""
    return f"{settings.postgres.schema_name}.{settings.postgres.chunk_table}"


def _vector_literal(embedding: list[float]) -> str:
    """Convert a Python embedding list into pgvector literal syntax."""
    values = ",".join(f"{float(value):.8f}" for value in embedding)
    return f"[{values}]"


def ensure_document_chunk_table() -> None:
    """Create the retrieval chunk table and indexes if they do not already exist."""
    pool = get_postgres_pool()
    table_name = _qualified_chunk_table()
    embedding_dimensions = settings.postgres.embedding_dimensions
    sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL UNIQUE,
            chunk_index INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            source_path TEXT NOT NULL,
            content TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            embedding vector({embedding_dimensions}) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_doc_chunks_document_id
        ON {table_name}(document_id);

        CREATE INDEX IF NOT EXISTS idx_doc_chunks_metadata
        ON {table_name}
        USING GIN (metadata);

        CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
        ON {table_name}
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def upsert_document_chunk(chunk: dict) -> None:
    """Insert or update one embedded document chunk in the retrieval table."""
    embedding = chunk.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise ValueError("Chunk embedding is required for Postgres ingestion.")

    metadata = chunk.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    sql = f"""
        INSERT INTO {_qualified_chunk_table()} (
            document_id,
            chunk_id,
            chunk_index,
            source_file,
            source_path,
            content,
            char_count,
            metadata,
            embedding
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector)
        ON CONFLICT (chunk_id)
        DO UPDATE SET
            document_id = EXCLUDED.document_id,
            chunk_index = EXCLUDED.chunk_index,
            source_file = EXCLUDED.source_file,
            source_path = EXCLUDED.source_path,
            content = EXCLUDED.content,
            char_count = EXCLUDED.char_count,
            metadata = EXCLUDED.metadata,
            embedding = EXCLUDED.embedding,
            updated_at = now()
    """

    params = (
        str(metadata.get("document_id") or chunk.get("chunk_id", "").split(":", 1)[0]),
        str(chunk["chunk_id"]),
        int(chunk["chunk_index"]),
        str(chunk["source_file"]),
        str(chunk["source_path"]),
        str(chunk["content"]),
        int(chunk["char_count"]),
        json.dumps(metadata),
        _vector_literal(embedding),
    )

    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def ingest_embedding_manifest(payload: dict) -> int:
    """Upsert all embedded chunks from one embedding manifest into Postgres."""
    ensure_document_chunk_table()
    chunks = payload.get("chunks", [])
    if not isinstance(chunks, list):
        raise ValueError("Embedding manifest chunks must be a list.")

    count = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        upsert_document_chunk(chunk)
        count += 1
    return count
