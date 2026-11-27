"""SQL containment corpus shared by the parser unit tests and the integration tests.

REFUSED: the query tool must refuse each of these before touching the database.
DB_BLOCKS: the subset the database stops on its own (role privileges, read-only
transaction), with the Python layers bypassed.
DB_ALLOWS_CONTAINED_ELSEWHERE: cases the database alone would run; listed with
the layer that contains them so the gap stays visible.
"""

REFUSED = {
    # comment tricks
    "comment_newline_then_delete": "SELECT 1 --\n; DELETE FROM books",
    "block_comment_then_drop": "SELECT 1 /* */; DROP TABLE books",
    "comment_split_keyword": "DEL/**/ETE FROM books",
    "comment_disguised_select": "/* SELECT */ DELETE FROM books",
    "unterminated_comment": "SELECT * FROM books /*",
    # data-modifying CTEs
    "cte_insert": (
        "WITH x AS (INSERT INTO books (title, author_id, genre_id, published_year) "
        "VALUES ('x', 1, 1, 2000) RETURNING *) SELECT * FROM x"
    ),
    "cte_update": "WITH x AS (UPDATE books SET price = 0 RETURNING *) SELECT * FROM x",
    "cte_delete": "WITH x AS (DELETE FROM books RETURNING *) SELECT * FROM x",
    # multiple statements
    "two_selects": "SELECT 1; SELECT 2",
    "select_then_delete": "SELECT 1; DELETE FROM books",
    "commit_then_delete": "COMMIT; DELETE FROM books",
    # leaving the read-only transaction
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
    "begin_read_write": "BEGIN READ WRITE",
    "set_session_rw": "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
    "set_role": "SET ROLE postgres",
    "set_timeout": "SET statement_timeout = 0",
    # side channels and session state
    "pg_notify": "SELECT pg_notify('chan', 'msg')",
    "notify_stmt": "NOTIFY chan",
    "listen_stmt": "LISTEN chan",
    "set_config_session": "SELECT set_config('statement_timeout', '0', false)",
    "set_config_work_mem": "SELECT set_config('work_mem', '2GB', false)",
    "advisory_lock": "SELECT pg_advisory_lock(42)",
    "advisory_xact_lock": "SELECT pg_try_advisory_xact_lock(42)",
    "wal_message": "SELECT pg_logical_emit_message(true, 'p', 'm')",
    "txid_current": "SELECT txid_current()",
    # COPY
    "copy_table_stdout": "COPY books TO STDOUT",
    "copy_query_file": "COPY (SELECT * FROM books) TO '/tmp/pg_mcp_copy.csv'",
    "copy_program": "COPY (SELECT 1) TO PROGRAM 'id'",
    # backend control
    "terminate_backend": "SELECT pg_terminate_backend(pg_backend_pid())",
    "cancel_backend": "SELECT pg_cancel_backend(1)",
    "terminate_qualified": "SELECT pg_catalog.pg_terminate_backend(1)",
    # sequences
    "nextval": "SELECT nextval('books_id_seq')",
    "setval": "SELECT setval('books_id_seq', 1)",
    # large objects
    "lo_create": "SELECT lo_create(0)",
    "lo_import": "SELECT lo_import('/etc/hosts')",
    "lo_export": "SELECT lo_export(1, '/tmp/pg_mcp_lo')",
    "lo_from_bytea": "SELECT lo_from_bytea(0, 'x')",
    "lo_unlink": "SELECT lo_unlink(1)",
    # dblink (refused whether or not the extension is installed)
    "dblink_exec": "SELECT dblink_exec('dbname=bookstore', 'DELETE FROM books')",
    "dblink_connect": "SELECT dblink_connect('dbname=bookstore')",
    # dynamic SQL hidden in a string
    "query_to_xml_delete": "SELECT query_to_xml('DELETE FROM books RETURNING 1', true, true, '')",
    "query_to_xml_concat": "SELECT query_to_xml(concat('DEL', 'ETE FROM books RETURNING 1'), true, true, '')",
    "do_block": "DO $$ BEGIN DELETE FROM books; END $$",
    # reads that write or lock
    "select_into": "SELECT * INTO stolen FROM books",
    "for_update": "SELECT * FROM books FOR UPDATE",
    "lock_table": "LOCK books",
    "create_temp": "CREATE TEMP TABLE t (a int)",
    # server files
    "read_file": "SELECT pg_read_file('/etc/hosts')",
    "ls_dir": "SELECT * FROM pg_ls_dir('.')",
    # other statements
    "explain_analyze_delete": "EXPLAIN ANALYZE DELETE FROM books",
    "vacuum": "VACUUM books",
    "prepare": "PREPARE p AS DELETE FROM books",
    "call": "CALL some_proc()",
    "values": "VALUES (1)",
    "empty": "",
    "only_semicolon": " ; ",
}

DB_BLOCKS = [
    "cte_insert",
    "cte_update",
    "cte_delete",
    "select_then_delete",
    "pg_notify",
    "wal_message",
    "copy_query_file",
    "copy_program",
    "terminate_backend",
    "cancel_backend",
    "nextval",
    "setval",
    "lo_create",
    "lo_import",
    "lo_export",
    "lo_from_bytea",
    "lo_unlink",
    "dblink_exec",
    "query_to_xml_delete",
    "query_to_xml_concat",
    "do_block",
    "select_into",
    "for_update",
    "lock_table",
    "create_temp",
    "read_file",
    "ls_dir",
    "explain_analyze_delete",
    "vacuum",
]

# name -> the layer that contains it when the database alone would run it
DB_ALLOWS_CONTAINED_ELSEWHERE = {
    "set_config_session": "parser refuses; asyncpg pool runs RESET ALL on release",
    "set_config_work_mem": "parser refuses; asyncpg pool runs RESET ALL on release",
    "advisory_lock": "parser refuses; asyncpg pool runs pg_advisory_unlock_all() on release",
    "txid_current": "parser refuses; only consumes a transaction id",
    "copy_table_stdout": "keyword blocklist and parser refuse; it only reads granted tables",
}

# Parsed as one SELECT, so they reach the database, which contains them.
LIMIT_BYPASS = {
    "trailing_line_comment": "SELECT g FROM generate_series(1, 100000) g --",
    "limit_all": "SELECT g FROM generate_series(1, 100000) g LIMIT ALL",
    "big_limit": "SELECT g FROM generate_series(1, 100000) g LIMIT 100000",
    "limit_in_comment": "SELECT g FROM generate_series(1, 100000) g /* LIMIT 5 */",
    "union_all": "SELECT g FROM generate_series(1, 50000) g UNION ALL SELECT g FROM generate_series(1, 50000) g",
    "recursive_cte": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r",
}
