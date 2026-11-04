import pytest

from irb_watch.security.limits import TooLargeError, check_content_length, stream_with_cap
from irb_watch.security.url_guard import UnsafeURLError, validate_url


def fake_resolver(mapping):
    return lambda host: mapping[host]


PUBLIC = fake_resolver({"hospital.example": ["93.184.216.34"]})


def test_public_host_allowed():
    assert validate_url("https://hospital.example/irb", PUBLIC) == "https://hospital.example/irb"


@pytest.mark.parametrize(
    "url",
    ["ftp://hospital.example/x", "file:///etc/passwd", "javascript:alert(1)", "//hospital.example"],
)
def test_bad_scheme_rejected(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url, PUBLIC)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://224.0.0.1/",
    ],
)
def test_non_public_ip_literals_rejected(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url, PUBLIC)


def test_hostname_resolving_to_private_rejected():
    resolver = fake_resolver({"evil.example": ["10.0.0.5"]})
    with pytest.raises(UnsafeURLError):
        validate_url("https://evil.example/", resolver)


def test_any_private_record_rejects_host():
    resolver = fake_resolver({"mixed.example": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(UnsafeURLError):
        validate_url("https://mixed.example/", resolver)


def test_redirect_hop_to_internal_rejected():
    resolver = fake_resolver({"hospital.example": ["93.184.216.34"], "internal.example": ["10.1.1.1"]})
    validate_url("https://hospital.example/start", resolver)
    with pytest.raises(UnsafeURLError):
        validate_url("http://internal.example/admin", resolver)


def test_credentials_rejected():
    with pytest.raises(UnsafeURLError):
        validate_url("https://user:pw@hospital.example/", PUBLIC)


def test_missing_host_rejected():
    with pytest.raises(UnsafeURLError):
        validate_url("https:///path", PUBLIC)


def test_empty_resolution_rejected():
    with pytest.raises(UnsafeURLError):
        validate_url("https://none.example/", fake_resolver({"none.example": []}))


def test_declared_size_over_cap_rejected():
    with pytest.raises(TooLargeError):
        check_content_length("1000", 999)


def test_missing_or_bad_content_length_passes():
    check_content_length(None, 10)
    check_content_length("abc", 10)


def test_stream_aborts_over_cap():
    with pytest.raises(TooLargeError):
        stream_with_cap([b"a" * 6, b"b" * 6], 10)


def test_stream_within_cap_returns_bytes():
    assert stream_with_cap([b"ab", b"cd"], 4) == b"abcd"
