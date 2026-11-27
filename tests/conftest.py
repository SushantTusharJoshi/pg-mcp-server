import pytest


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    """Keep test audit records out of the working directory."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("PG_MCP_AUDIT_LOG", str(path))
    return path
