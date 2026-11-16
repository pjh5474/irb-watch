"""scripts/move/copy_storage.py (moving to another Supabase project), with fake buckets."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "copy_storage", Path(__file__).resolve().parents[1] / "scripts" / "move" / "copy_storage.py"
)
copy_storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(copy_storage)


class FakeBucket:
    def __init__(self, files: dict[str, bytes], fail: set[str] = frozenset()) -> None:
        self.files = dict(files)
        self.fail = fail

    def list(self, prefix, options):
        depth = prefix.count("/") + 1 if prefix else 0
        names = {}
        for path in sorted(self.files):
            if prefix and not path.startswith(prefix + "/"):
                continue
            parts = path.split("/")[depth:]
            names.setdefault(parts[0], len(parts) == 1)
        rows = [{"name": n, "id": "x" if is_file else None, "metadata": {"mimetype": "application/pdf"} if is_file else None}
                for n, is_file in names.items()]
        return rows[options["offset"]: options["offset"] + options["limit"]]

    def download(self, path):
        return self.files[path]

    def upload(self, path, data, options):
        if path in self.fail:
            raise RuntimeError("payload too large")
        self.files[path] = data


class FakeClient:
    def __init__(self, buckets):
        self.storage = self
        self.buckets = buckets

    def from_(self, name):
        return self.buckets.setdefault(name, FakeBucket({}))


def run(monkeypatch, capsys, old, new, *args):
    clients = {"https://old": FakeClient(old), "https://new": FakeClient(new)}
    monkeypatch.setattr(copy_storage, "create_client", lambda url, key: clients[url])
    monkeypatch.setattr("sys.argv", ["copy_storage.py", *args])
    for k, v in {"OLD_SUPABASE_URL": "https://old", "OLD_SERVICE_KEY": "k1", "NEW_SUPABASE_URL": "https://new",
                 "NEW_SERVICE_KEY": "k2"}.items():
        monkeypatch.setenv(k, v)
    code = copy_storage.main()
    return code, capsys.readouterr().out


def test_walks_folders_copies_and_skips_what_is_there(monkeypatch, capsys):
    old = {"documents": FakeBucket({"s4/a/1.pdf": b"1", "s4/b/2.pdf": b"2", "top.pdf": b"t"})}
    new = {"documents": FakeBucket({"top.pdf": b"t"})}
    code, out = run(monkeypatch, capsys, old, new)
    assert code == 0
    assert new["documents"].files == old["documents"].files
    assert "documents: old 3, already in new 1, copied 2, failed 0" in out
    assert "k1" not in out and "k2" not in out  # keys never printed


def test_check_writes_nothing_and_a_failure_is_reported(monkeypatch, capsys):
    old = {"pipeline-files": FakeBucket({"p1/s1/x/a.pdf": b"a", "p1/s1/y/b.pdf": b"b"})}
    new = {"pipeline-files": FakeBucket({})}
    code, out = run(monkeypatch, capsys, old, new, "--check")
    assert code == 0 and new["pipeline-files"].files == {}
    assert "2 would be copied" in out
    new = {"pipeline-files": FakeBucket({}, fail={"p1/s1/y/b.pdf"})}
    code, out = run(monkeypatch, capsys, old, new)
    assert code == 2
    assert "FAILED p1/s1/y/b.pdf: RuntimeError: payload too large" in out
    assert list(new["pipeline-files"].files) == ["p1/s1/x/a.pdf"]


def test_refuses_the_same_project_twice(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["copy_storage.py"])
    for k, v in {"OLD_SUPABASE_URL": "https://same", "OLD_SERVICE_KEY": "a", "NEW_SUPABASE_URL": "https://same/",
                 "NEW_SERVICE_KEY": "b"}.items():
        monkeypatch.setenv(k, v)
    assert copy_storage.main() == 1
    assert "same project" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["OLD_SERVICE_KEY", "NEW_SUPABASE_URL"])
def test_names_a_missing_setting(monkeypatch, capsys, missing):
    for k in ("OLD_SUPABASE_URL", "OLD_SERVICE_KEY", "NEW_SUPABASE_URL", "NEW_SERVICE_KEY"):
        monkeypatch.setenv(k, "x" if k != missing else "")
    monkeypatch.setattr("sys.argv", ["copy_storage.py"])
    assert copy_storage.main() == 1
    assert missing in capsys.readouterr().out
