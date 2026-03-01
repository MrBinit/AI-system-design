from app.services.chunking_service import chunk_configured_documents


def main():
    """Chunk all configured source documents and print the written output files."""
    output_paths = chunk_configured_documents()
    print(f"Chunked {len(output_paths)} document(s).")
    for output_path in output_paths:
        print(output_path)


if __name__ == "__main__":
    main()
