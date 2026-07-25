"""Tests for pg-mcp-server SQL safety and search logic."""
import pytest
from pg_mcp.server import BLOCKED_KEYWORDS


class TestBlockedKeywords:
    """Test that dangerous SQL keywords are correctly blocked."""

    def test_blocks_insert(self):
        assert BLOCKED_KEYWORDS.search("INSERT INTO books VALUES (1, 'test')")

    def test_blocks_update(self):
        assert BLOCKED_KEYWORDS.search("UPDATE books SET title = 'test'")

    def test_blocks_delete(self):
        assert BLOCKED_KEYWORDS.search("DELETE FROM books")

    def test_blocks_drop(self):
        assert BLOCKED_KEYWORDS.search("DROP TABLE books")

    def test_blocks_alter(self):
        assert BLOCKED_KEYWORDS.search("ALTER TABLE books ADD COLUMN x int")

    def test_blocks_truncate(self):
        assert BLOCKED_KEYWORDS.search("TRUNCATE books")

    def test_blocks_create(self):
        assert BLOCKED_KEYWORDS.search("CREATE TABLE test (id int)")

    def test_blocks_grant(self):
        assert BLOCKED_KEYWORDS.search("GRANT ALL ON books TO public")

    def test_blocks_revoke(self):
        assert BLOCKED_KEYWORDS.search("REVOKE ALL ON books FROM public")

    def test_blocks_copy(self):
        assert BLOCKED_KEYWORDS.search("COPY books TO '/tmp/out.csv'")

    def test_blocks_execute(self):
        assert BLOCKED_KEYWORDS.search("EXECUTE some_function()")

    def test_allows_select(self):
        assert not BLOCKED_KEYWORDS.search("SELECT * FROM books WHERE id = 1")

    def test_allows_select_with_join(self):
        assert not BLOCKED_KEYWORDS.search(
            "SELECT b.title, a.name FROM books b JOIN authors a ON b.author_id = a.id"
        )

    def test_allows_count(self):
        assert not BLOCKED_KEYWORDS.search("SELECT COUNT(*) FROM books")

    def test_allows_aggregate(self):
        assert not BLOCKED_KEYWORDS.search(
            "SELECT genre, AVG(rating) FROM books GROUP BY genre"
        )

    def test_case_insensitive_drop(self):
        assert BLOCKED_KEYWORDS.search("drop table books")
        assert BLOCKED_KEYWORDS.search("Drop Table books")
        assert BLOCKED_KEYWORDS.search("DROP TABLE books")

    def test_case_insensitive_delete(self):
        assert BLOCKED_KEYWORDS.search("delete from books")
        assert BLOCKED_KEYWORDS.search("Delete From books")
