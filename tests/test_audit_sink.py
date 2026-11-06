import pytest

from irb_watch.db.supabase_users import SupabaseAuditSink
from irb_watch.services.errors import AuditUnavailableError


class Table:
    def __init__(self, fail):
        self.fail, self.rows = fail, []

    def insert(self, row):
        self.rows.append(row)
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("database down")


class Client:
    def __init__(self, fail=False):
        self.audit = Table(fail)

    def table(self, name):
        assert name == "audit_logs"
        return self.audit


def test_record_ignores_a_failed_write():
    SupabaseAuditSink(Client(fail=True)).record("scan", "scan.finished", None)  # no exception


def test_record_required_raises_when_the_write_fails():
    with pytest.raises(AuditUnavailableError, match="not carried out"):
        SupabaseAuditSink(Client(fail=True)).record_required("user", "file.download_link", "u1")


def test_both_write_the_same_row_when_the_database_works():
    client = Client()
    sink = SupabaseAuditSink(client)
    sink.record("user", "a", "u1", title="t", source_id=1)
    sink.record_required("user", "a", "u1", title="t", source_id=1)
    assert len(client.audit.rows) == 2 and client.audit.rows[0] == client.audit.rows[1]
    assert client.audit.rows[0]["event_type"] == "a" and client.audit.rows[0]["payload"] == {}
