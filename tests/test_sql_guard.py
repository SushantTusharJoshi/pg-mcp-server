"""PG-F8: the sqlglot layer accepts plain reads and refuses everything else."""

import pytest

from pg_mcp.sql_guard import check_sql
from tests.sql_corpus import LIMIT_BYPASS, REFUSED

ALLOWED = [
    "SELECT * FROM books",
    "SELECT 1;",
    "select title from books where id = $1",
    "SELECT b.title, a.name FROM books b JOIN authors a ON a.id = b.author_id",
    "WITH top AS (SELECT * FROM books WHERE rating > 4) SELECT title FROM top",
    "SELECT 1 UNION ALL SELECT 2",
    "(SELECT 1)",
    "TABLE books",
    "SELECT created_at, copy_id, is_deleted, grant_date FROM t",
    "SELECT 1 /* ; DELETE FROM books */",
    "SELECT title FROM books WHERE title ILIKE '%of%' ORDER BY rating DESC LIMIT 5",
    "SELECT pg_sleep(1)",
    "SELECT current_setting('statement_timeout')",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_plain_reads_allowed(sql):
    assert check_sql(sql) is None


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_corpus_refused_by_parser(name):
    assert check_sql(REFUSED[name]) is not None


@pytest.mark.parametrize("name", sorted(LIMIT_BYPASS))
def test_limit_bypass_is_a_plain_read(name):
    """These parse as one SELECT on purpose; the row cap contains them."""
    assert check_sql(LIMIT_BYPASS[name]) is None


def test_reasons_are_specific():
    assert "exactly one statement" in check_sql("SELECT 1; SELECT 2")
    assert "DELETE" in check_sql("WITH x AS (DELETE FROM books RETURNING *) SELECT * FROM x")
    assert "pg_notify" in check_sql("SELECT pg_notify('a', 'b')")
    assert "INTO" in check_sql("SELECT * INTO t FROM books")


def test_unparseable_sql_is_refused():
    assert check_sql("SELEC 1") is not None
