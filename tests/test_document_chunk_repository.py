from app.repositories import document_chunk_repository


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self):
        return _FakeConnection(self._cursor)


def test_ingest_embedding_manifest_upserts_chunks(monkeypatch):
    cursor = _FakeCursor()
    monkeypatch.setattr(document_chunk_repository, "get_postgres_pool", lambda: _FakePool(cursor))

    count = document_chunk_repository.ingest_embedding_manifest(
        {
            "chunks": [
                {
                    "chunk_id": "university_1:0000",
                    "chunk_index": 0,
                    "source_file": "university_1.md",
                    "source_path": "/tmp/university_1.md",
                    "content": "Sample embedded chunk",
                    "char_count": 21,
                    "metadata": {"document_id": "university_1", "country": "Germany"},
                    "embedding": [0.1, 0.2, 0.3],
                }
            ]
        }
    )

    assert count == 1
    assert len(cursor.calls) == 2
    assert "CREATE TABLE IF NOT EXISTS" in cursor.calls[0][0]
    assert "INSERT INTO" in cursor.calls[1][0]
    params = cursor.calls[1][1]
    assert params[0] == "university_1"
    assert params[1] == "university_1:0000"
    assert params[-1] == "[0.10000000,0.20000000,0.30000000]"
