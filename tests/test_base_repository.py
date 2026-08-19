from app.repositories.base import BaseRepository


def test_build_insert_query():
    repo = BaseRepository()
    query, values = repo._build_insert("events", {"name": "test", "slug": "test-slug"})
    assert "INSERT INTO events" in query
    assert "$1" in query
    assert "$2" in query
    assert "test" in values
    assert "test-slug" in values


def test_build_insert_returns_all_values():
    repo = BaseRepository()
    data = {"a": 1, "b": 2, "c": 3}
    query, values = repo._build_insert("table", data)
    assert len(values) == 3
    assert "RETURNING *" in query
