from app.repositories import auth_user_repository as repo


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        _ = (exc_type, exc, tb)
        return False


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        _ = (exc_type, exc, tb)
        return False


class FakePool:
    def __init__(self, conn: FakeConnection):
        self._conn = conn

    def connection(self):
        return self._conn


def test_table_name_with_and_without_schema(monkeypatch):
    monkeypatch.setattr(repo.settings.postgres, "schema_name", "public")
    assert repo._table("auth_users") == "public.auth_users"

    monkeypatch.setattr(repo.settings.postgres, "schema_name", " ")
    assert repo._table("auth_users") == "auth_users"


def test_normalized_roles_defaults_and_cleanup():
    assert repo._normalized_roles(None) == ["user"]
    assert repo._normalized_roles([" admin ", "", "user"]) == ["admin", "user"]


def test_ensure_auth_user_table_executes_create_and_commit(monkeypatch):
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    monkeypatch.setattr(repo, "get_postgres_pool", lambda: FakePool(conn))

    repo.ensure_auth_user_table()

    assert conn.commits == 1
    executed_sql, _ = cursor.executed[0]
    assert "CREATE TABLE IF NOT EXISTS" in executed_sql
    assert "password_hash" in executed_sql


def test_get_auth_user_by_username_returns_none_for_blank():
    assert repo.get_auth_user_by_username("  ") is None


def test_get_auth_user_by_username_returns_dict_row(monkeypatch):
    cursor = FakeCursor(
        row={
            "username": "Binit.Sapkota",
            "user_id": "Binit.Sapkota",
            "password_hash": "hash-1",
            "roles": ["admin"],
            "is_active": True,
        }
    )
    conn = FakeConnection(cursor)
    monkeypatch.setattr(repo, "get_postgres_pool", lambda: FakePool(conn))
    monkeypatch.setattr(repo, "ensure_auth_user_table", lambda: None)

    row = repo.get_auth_user_by_username(" Binit.Sapkota ")

    assert isinstance(row, dict)
    assert row["user_id"] == "Binit.Sapkota"
    assert cursor.executed[0][1] == ("binit.sapkota",)


def test_get_auth_user_by_username_returns_none_for_non_dict_row(monkeypatch):
    cursor = FakeCursor(row=("bad", "shape"))
    conn = FakeConnection(cursor)
    monkeypatch.setattr(repo, "get_postgres_pool", lambda: FakePool(conn))
    monkeypatch.setattr(repo, "ensure_auth_user_table", lambda: None)

    assert repo.get_auth_user_by_username("admin") is None


def test_upsert_auth_user_validates_required_fields():
    try:
        repo.upsert_auth_user(username=" ", user_id="u", password_hash="h")
        assert False, "expected ValueError for username"
    except ValueError as exc:
        assert "username is required." in str(exc)

    try:
        repo.upsert_auth_user(username="u", user_id=" ", password_hash="h")
        assert False, "expected ValueError for user_id"
    except ValueError as exc:
        assert "user_id is required." in str(exc)

    try:
        repo.upsert_auth_user(username="u", user_id="u", password_hash=" ")
        assert False, "expected ValueError for password_hash"
    except ValueError as exc:
        assert "password_hash is required." in str(exc)


def test_upsert_auth_user_executes_insert_and_commit(monkeypatch):
    cursor = FakeCursor()
    conn = FakeConnection(cursor)
    monkeypatch.setattr(repo, "get_postgres_pool", lambda: FakePool(conn))
    monkeypatch.setattr(repo, "ensure_auth_user_table", lambda: None)

    repo.upsert_auth_user(
        username=" Binit.Sapkota ",
        user_id=" Binit.Sapkota ",
        password_hash=" hash-1 ",
        roles=["admin", "user"],
        is_active=True,
    )

    assert conn.commits == 1
    executed_sql, params = cursor.executed[0]
    assert "INSERT INTO" in executed_sql
    assert "ON CONFLICT (username_key) DO UPDATE SET" in executed_sql
    assert params[0] == "binit.sapkota"
    assert params[1] == "Binit.Sapkota"
    assert params[2] == "Binit.Sapkota"
    assert params[3] == "hash-1"
    assert params[4] == '["admin", "user"]'
    assert params[5] is True
