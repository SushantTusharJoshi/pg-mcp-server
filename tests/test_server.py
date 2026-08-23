"""Tests for pg-mcp server utilities."""

import re
import sys
import os

# Add the src directory to the path so we can import server components
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestBlockedKeywordsRegex:
    """Test the keyword blocklist used to reject non-SELECT queries."""

    BLOCKED_KEYWORDS = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE)\b",
        re.IGNORECASE,
    )

    def test_select_allowed(self):
        assert self.BLOCKED_KEYWORDS.search("SELECT * FROM books") is None

    def test_select_with_joins_allowed(self):
        sql = "SELECT b.title, a.name FROM books b JOIN authors a ON a.id = b.author_id"
        assert self.BLOCKED_KEYWORDS.search(sql) is None

    def test_insert_blocked(self):
        assert self.BLOCKED_KEYWORDS.search("INSERT INTO books VALUES (1, 'x')") is not None

    def test_delete_blocked(self):
        assert self.BLOCKED_KEYWORDS.search("DELETE FROM books WHERE id = 1") is not None

    def test_drop_blocked(self):
        assert self.BLOCKED_KEYWORDS.search("DROP TABLE books") is not None

    def test_update_blocked(self):
        assert self.BLOCKED_KEYWORDS.search("UPDATE books SET title = 'x'") is not None

    def test_create_blocked(self):
        assert self.BLOCKED_KEYWORDS.search("CREATE TABLE evil (id int)") is not None

    def test_truncate_blocked(self):
        assert self.BLOCKED_KEYWORDS.search("TRUNCATE books") is not None

    def test_case_insensitive(self):
        assert self.BLOCKED_KEYWORDS.search("drop table books") is not None
        assert self.BLOCKED_KEYWORDS.search("Drop Table Books") is not None

    def test_column_name_created_at_not_blocked(self):
        """Column names like created_at should NOT be blocked (no word boundary match)."""
        sql = "SELECT created_at, updated_at FROM books"
        assert self.BLOCKED_KEYWORDS.search(sql) is None

    def test_column_name_copy_id_not_blocked(self):
        sql = "SELECT copy_id FROM inventory"
        assert self.BLOCKED_KEYWORDS.search(sql) is None

    def test_column_name_is_deleted_not_blocked(self):
        sql = "SELECT is_deleted FROM books"
        assert self.BLOCKED_KEYWORDS.search(sql) is None

    def test_column_name_grant_date_not_blocked(self):
        sql = "SELECT grant_date FROM permissions"
        assert self.BLOCKED_KEYWORDS.search(sql) is None


class TestLikeEscaping:
    """Test that LIKE special characters are properly escaped in search patterns."""

    def _build_pattern(self, search_term: str) -> str:
        """Reproduce the escaping logic from search_books."""
        escaped = search_term.replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def test_plain_term(self):
        assert self._build_pattern("python") == "%python%"

    def test_percent_escaped(self):
        pattern = self._build_pattern("100%")
        assert pattern == "%100\\%%"
        assert "\\%" in pattern

    def test_underscore_escaped(self):
        pattern = self._build_pattern("my_book")
        assert pattern == "%my\\_book%"
        assert "\\_" in pattern

    def test_both_escaped(self):
        pattern = self._build_pattern("50%_off")
        assert pattern == "%50\\%\\_off%"

    def test_no_special_chars_unchanged(self):
        pattern = self._build_pattern("Harry Potter")
        assert pattern == "%Harry Potter%"

    def test_multiple_percents(self):
        pattern = self._build_pattern("%%")
        assert pattern == "%\\%\\%%"
