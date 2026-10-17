import re

from teleport.net.identity import format_fingerprint, load_or_create_identity


def test_creates_identity_files(tmp_path):
    identity = load_or_create_identity(tmp_path / "cfg")
    assert identity.cert_path.exists()
    assert identity.key_path.exists()
    assert re.fullmatch(r"[0-9a-f]{64}", identity.fingerprint)
    assert re.fullmatch(r"[0-9a-f]{32}", identity.device_id)


def test_reload_returns_same_identity(tmp_path):
    first = load_or_create_identity(tmp_path)
    second = load_or_create_identity(tmp_path)
    assert first == second


def test_each_device_gets_its_own_identity(tmp_path):
    a = load_or_create_identity(tmp_path / "a")
    b = load_or_create_identity(tmp_path / "b")
    assert a.fingerprint != b.fingerprint
    assert a.device_id != b.device_id


def test_format_fingerprint():
    assert format_fingerprint("abcd12") == "AB:CD:12"


def test_short_fingerprint_is_first_8_bytes(tmp_path):
    identity = load_or_create_identity(tmp_path)
    short = identity.short_fingerprint
    assert len(short) == 23  # 8 cặp hex + 7 dấu ":"
    assert short.replace(":", "").lower() == identity.fingerprint[:16]
