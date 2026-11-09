from irb_watch.mail.unsubscribe import make_token, read_token

KEY = "k" * 64
USER = "0b3a4c6e-1111-4222-8333-944455556666"
OTHER = "1c4b5d7f-2222-4333-9444-a55566667777"


def test_a_token_gives_back_its_user():
    assert read_token(make_token(USER, KEY), KEY) == USER


def test_a_token_is_url_safe():
    token = make_token(USER, KEY)
    assert all(c.isalnum() or c in "-._" for c in token)


def test_the_wrong_key_does_not_read_it():
    assert read_token(make_token(USER, KEY), "x" * 64) is None


def test_a_token_cannot_be_moved_to_another_user():
    signature = make_token(USER, KEY).rpartition(".")[2]
    assert read_token(f"{OTHER}.{signature}", KEY) is None


def test_a_changed_signature_is_refused():
    token = make_token(USER, KEY)
    broken = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert read_token(broken, KEY) is None


def test_garbage_is_refused_and_never_raises():
    for bad in ("", ".", "abc", f"{USER}.", "not-a-uuid.sig", "x" * 500, f"{USER}.{'x' * 300}"):
        assert read_token(bad, KEY) is None


def test_an_empty_server_key_reads_nothing():
    assert read_token(make_token(USER, KEY), "") is None
