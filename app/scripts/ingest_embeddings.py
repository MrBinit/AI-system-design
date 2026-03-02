from app.services.embedding_ingestion_service import ingest_configured_embedding_manifests


def main():
    """Ingest the configured embedding manifests into the Postgres retrieval table."""
    result = ingest_configured_embedding_manifests()
    print(
        f"Ingested {result['processed_chunks']} chunk(s) "
        f"from {result['processed_files']} embedding manifest(s)."
    )


if __name__ == "__main__":
    main()
