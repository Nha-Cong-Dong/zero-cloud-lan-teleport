"""Test `teleport.trust.store` theo kế hoạch §2.4, §2.5 và phụ lục điểm 9–13."""

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from teleport.trust.identity import PeerCert, load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import (
    STORE_VERSION,
    TRUSTED_FILE,
    TrustedDevice,
    TrustStore,
    TrustStoreError,
)

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 20, 8, 15, 0, 400_000, tzinfo=UTC)
NOW_STR = "2026-10-20T08:15:00Z"
NOW_TRUNC = datetime.datetime(2026, 10, 20, 8, 15, 0, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "cfg"


@pytest.fixture
def make_peer(tmp_path):
    counter = iter(range(1000))

    def _make() -> PeerCert:
        identity = load_or_create_identity(tmp_path / f"peer{next(counter)}")
        return peer_cert_from_pem(identity.cert_pem())

    return _make


@pytest.fixture
def peer(make_peer):
    return make_peer()


def entry(peer: PeerCert, name: str = "PC-Nha", paired_at: str = NOW_STR, **overrides) -> dict:
    d = {
        "device_id": peer.device_id,
        "name": name,
        "fingerprint": peer.fingerprint,
        "cert": peer.pem,
        "paired_at": paired_at,
    }
    d.update(overrides)
    return d


def write_store(cfg, obj, *, raw: str | None = None, encoding: str = "utf-8") -> bytes:
    cfg.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps(obj, indent=2, ensure_ascii=False)
    (cfg / TRUSTED_FILE).write_bytes(text.encode(encoding))
    return (cfg / TRUSTED_FILE).read_bytes()


def no_tmp_left(cfg) -> bool:
    return not cfg.exists() or not any(p.name.endswith(".tmp") for p in cfg.iterdir())


# --- Hằng số, kho rỗng -------------------------------------------------------------------------


def test_constants():
    assert TRUSTED_FILE == "trusted.json"
    assert STORE_VERSION == 1


def test_load_empty_dir_gives_empty_store_without_creating_file(cfg):
    cfg.mkdir()
    store = TrustStore.load(cfg)
    assert store.devices() == []
    assert store.cert_pems() == []
    assert store.path == cfg / "trusted.json"
    assert not store.path.exists()
    assert list(cfg.iterdir()) == []


def test_load_missing_dir_gives_empty_store_without_creating_file(cfg):
    store = TrustStore.load(cfg)
    assert store.devices() == []
    assert not (cfg / TRUSTED_FILE).exists()


def test_load_file_with_no_devices(cfg):
    write_store(cfg, {"version": 1, "devices": []})
    assert TrustStore.load(cfg).devices() == []


# --- add / load lại ----------------------------------------------------------------------------


def test_add_then_reload_keeps_everything(cfg, peer):
    store = TrustStore.load(cfg)
    device = store.add(peer, "Máy của Minh", now=NOW)
    assert isinstance(device, TrustedDevice)
    assert device.device_id == peer.device_id
    assert device.name == "Máy của Minh"
    assert device.fingerprint == peer.fingerprint
    assert device.cert_pem == peer.pem
    assert device.paired_at == NOW_TRUNC

    reloaded = TrustStore.load(cfg)
    assert reloaded.devices() == [device]
    assert store.devices() == [device]


def test_add_creates_config_dir(cfg, peer):
    assert not cfg.exists()
    TrustStore.load(cfg).add(peer, "PC", now=NOW)
    assert (cfg / TRUSTED_FILE).is_file()


def test_lookups_after_add(cfg, peer, make_peer):
    store = TrustStore.load(cfg)
    device = store.add(peer, "PC-Nha", now=NOW)
    other = make_peer()
    assert store.is_trusted(peer.fingerprint) is True
    assert store.is_trusted(other.fingerprint) is False
    assert store.cert_pems() == [peer.pem]
    assert store.get(peer.device_id) == device
    assert store.get(other.device_id) is None
    assert store.by_fingerprint(peer.fingerprint) == device
    assert store.by_fingerprint(other.fingerprint) is None


def test_trusted_device_is_frozen(cfg, peer):
    device = TrustStore.load(cfg).add(peer, "PC", now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        device.name = "khác"  # type: ignore[misc]


def test_add_same_cert_with_new_name_replaces(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "Tên cũ", now=NOW)
    store.add(peer, "Tên mới", now=NOW)
    assert [d.name for d in store.devices()] == ["Tên mới"]
    assert [d.name for d in TrustStore.load(cfg).devices()] == ["Tên mới"]


def test_add_same_device_id_new_fingerprint_replaces(cfg, make_cert):
    # Phụ lục điểm 13: cùng device_id (CN) nhưng khóa/cert khác → thay thế.
    device_id = "ab" * 16
    old = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))
    new = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))
    assert old.fingerprint != new.fingerprint
    store = TrustStore.load(cfg)
    store.add(old, "PC", now=NOW)
    store.add(new, "PC", now=NOW)
    assert [d.fingerprint for d in store.devices()] == [new.fingerprint]
    assert store.is_trusted(old.fingerprint) is False
    assert store.cert_pems() == [new.pem]
    assert [d.fingerprint for d in TrustStore.load(cfg).devices()] == [new.fingerprint]


def _pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def test_multiple_devices(cfg, make_peer):
    store = TrustStore.load(cfg)
    peers = [make_peer() for _ in range(3)]
    for i, p in enumerate(peers):
        store.add(p, f"May {i}", now=NOW)
    reloaded = TrustStore.load(cfg)
    assert len(reloaded.devices()) == 3
    assert sorted(reloaded.cert_pems()) == sorted(p.pem for p in peers)
    assert all(reloaded.is_trusted(p.fingerprint) for p in peers)


# --- remove ------------------------------------------------------------------------------------


def test_remove_persists(cfg, peer, make_peer):
    other = make_peer()
    store = TrustStore.load(cfg)
    device = store.add(peer, "A", now=NOW)
    store.add(other, "B", now=NOW)
    removed = store.remove(peer.device_id)
    assert removed == device
    assert store.get(peer.device_id) is None
    assert store.is_trusted(peer.fingerprint) is False
    reloaded = TrustStore.load(cfg)
    assert [d.device_id for d in reloaded.devices()] == [other.device_id]
    assert reloaded.cert_pems() == [other.pem]


def test_remove_last_device_leaves_empty_store(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "A", now=NOW)
    store.remove(peer.device_id)
    assert TrustStore.load(cfg).devices() == []
    assert json.loads((cfg / TRUSTED_FILE).read_text(encoding="utf-8")) == {"version": 1, "devices": []}


def test_remove_missing_raises_key_error(cfg, peer):
    store = TrustStore.load(cfg)
    with pytest.raises(KeyError):
        store.remove(peer.device_id)
    store.add(peer, "A", now=NOW)
    before = (cfg / TRUSTED_FILE).read_bytes()
    with pytest.raises(KeyError):
        store.remove("0" * 32)
    assert (cfg / TRUSTED_FILE).read_bytes() == before


# --- Định dạng file ----------------------------------------------------------------------------


def test_file_format_matches_spec(cfg, peer):
    TrustStore.load(cfg).add(peer, "Máy của Minh", now=NOW)
    raw = (cfg / TRUSTED_FILE).read_bytes()
    text = raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(text)
    assert data == {
        "version": 1,
        "devices": [
            {
                "device_id": peer.device_id,
                "name": "Máy của Minh",
                "fingerprint": peer.fingerprint,
                "cert": peer.pem,
                "paired_at": NOW_STR,
            }
        ],
    }
    # ensure_ascii=False: tiếng Việt ghi nguyên, không thành \uXXXX.
    assert "Máy của Minh" in text
    assert "\\u" not in text
    # indent=2
    assert text.startswith('{\n  "')
    assert '\n  "devices": [' in text


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime.datetime(2026, 10, 20, 8, 15, 0, 400_000, tzinfo=UTC), "2026-10-20T08:15:00Z"),
        (datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T03:04:05Z"),
        (
            datetime.datetime(2026, 10, 20, 15, 15, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=7))),
            "2026-10-20T08:15:00Z",
        ),
        (
            datetime.datetime(2026, 1, 1, 0, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=1))),
            "2025-12-31T23:30:00Z",
        ),
    ],
)
def test_paired_at_written_as_utc_seconds_z(cfg, peer, now, expected):
    device = TrustStore.load(cfg).add(peer, "PC", now=now)
    stored = json.loads((cfg / TRUSTED_FILE).read_text(encoding="utf-8"))["devices"][0]["paired_at"]
    assert stored == expected
    assert device.paired_at.utcoffset() == datetime.timedelta(0)
    assert device.paired_at.microsecond == 0
    assert device.paired_at == datetime.datetime.strptime(expected, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def test_add_without_now_uses_current_utc_time(cfg, peer):
    before = datetime.datetime.now(UTC).replace(microsecond=0)
    device = TrustStore.load(cfg).add(peer, "PC")
    after = datetime.datetime.now(UTC)
    assert before <= device.paired_at <= after
    assert device.paired_at.microsecond == 0
    stored = json.loads((cfg / TRUSTED_FILE).read_text(encoding="utf-8"))["devices"][0]["paired_at"]
    assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", stored)


def test_add_with_naive_now_raises_value_error(cfg, peer):
    store = TrustStore.load(cfg)
    with pytest.raises(ValueError):
        store.add(peer, "PC", now=datetime.datetime(2026, 10, 20, 8, 15))
    assert store.devices() == []
    assert not (cfg / TRUSTED_FILE).exists()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-10-20T08:15:00Z", NOW_TRUNC),
        ("2026-10-20T08:15:00+00:00", NOW_TRUNC),
        ("2026-10-20T15:15:00+07:00", NOW_TRUNC),
        ("2026-10-20T08:15:00.987654Z", NOW_TRUNC),
        ("2026-10-20T03:15:00.5-05:00", NOW_TRUNC),
    ],
)
def test_paired_at_read_with_timezone_converted_to_utc(cfg, peer, stored, expected):
    write_store(cfg, {"version": 1, "devices": [entry(peer, paired_at=stored)]})
    device = TrustStore.load(cfg).devices()[0]
    assert device.paired_at == expected
    assert device.paired_at.utcoffset() == datetime.timedelta(0)
    assert device.paired_at.microsecond == 0


def test_no_tmp_file_left_after_add_and_remove(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    assert no_tmp_left(cfg)
    store.remove(peer.device_id)
    assert no_tmp_left(cfg)
    assert not (cfg / "trusted.json.tmp").exists()


def test_reads_utf8_with_bom(cfg, peer):
    write_store(cfg, {"version": 1, "devices": [entry(peer, name="Máy BOM")]}, encoding="utf-8-sig")
    assert (cfg / TRUSTED_FILE).read_bytes().startswith(b"\xef\xbb\xbf")
    assert [d.name for d in TrustStore.load(cfg).devices()] == ["Máy BOM"]


def test_unknown_fields_ignored_and_dropped_on_rewrite(cfg, peer, make_peer):
    write_store(
        cfg,
        {"version": 1, "future": {"x": 1}, "devices": [entry(peer, color="xanh", extra=[1, 2])]},
    )
    store = TrustStore.load(cfg)
    assert [d.device_id for d in store.devices()] == [peer.device_id]
    store.add(make_peer(), "B", now=NOW)
    data = json.loads((cfg / TRUSTED_FILE).read_text(encoding="utf-8"))
    assert set(data) == {"version", "devices"}
    for d in data["devices"]:
        assert set(d) == {"device_id", "name", "fingerprint", "cert", "paired_at"}


def test_cert_pems_returns_normalized_pem(cfg, peer):
    messy = "\n\n  " + peer.pem + "  \n"
    write_store(cfg, {"version": 1, "devices": [entry(peer, cert=messy)]})
    store = TrustStore.load(cfg)
    assert store.cert_pems() == [peer.pem]
    assert store.devices()[0].cert_pem == peer.pem


@pytest.mark.skipif(os.name != "posix", reason="quyền 0o600 chỉ áp dụng POSIX")
def test_file_permissions_are_0600_on_posix(cfg, peer, make_peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    assert (cfg / TRUSTED_FILE).stat().st_mode & 0o777 == 0o600
    store.add(make_peer(), "PC2", now=NOW)
    assert (cfg / TRUSTED_FILE).stat().st_mode & 0o777 == 0o600
    store.remove(peer.device_id)
    assert (cfg / TRUSTED_FILE).stat().st_mode & 0o777 == 0o600


# --- Sắp xếp và tìm kiếm -----------------------------------------------------------------------


def test_devices_sorted_by_casefolded_name_then_device_id(cfg, make_cert):
    def peer_with_id(device_id):
        return peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))

    store = TrustStore.load(cfg)
    store.add(peer_with_id("f" * 32), "bob", now=NOW)
    store.add(peer_with_id("2" * 32), "alice", now=NOW)
    store.add(peer_with_id("1" * 32), "Alice", now=NOW)
    store.add(peer_with_id("0" * 32), "Ärger", now=NOW)
    store.add(peer_with_id("3" * 32), "ALICE", now=NOW)
    expected = [("1" * 32, "Alice"), ("2" * 32, "alice"), ("3" * 32, "ALICE"), ("f" * 32, "bob")]
    expected.append(("0" * 32, "Ärger"))  # "ä" > "b" theo code point sau casefold
    for s in (store, TrustStore.load(cfg)):
        assert [(d.device_id, d.name) for d in s.devices()] == expected


def test_casefold_used_for_sorting_and_search(cfg, make_cert):
    # "ß".casefold() == "ss" (lower() không làm vậy).
    a = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + "a" * 32)))
    b = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + "b" * 32)))
    store = TrustStore.load(cfg)
    store.add(a, "Straße", now=NOW)
    store.add(b, "strasse", now=NOW)
    assert {d.device_id for d in store.find_by_name("STRASSE")} == {a.device_id, b.device_id}
    assert [d.device_id for d in store.devices()] == [a.device_id, b.device_id]


def test_find_by_name_is_case_insensitive_and_exact(cfg, make_peer):
    store = TrustStore.load(cfg)
    p1, p2, p3 = make_peer(), make_peer(), make_peer()
    store.add(p1, "PC-Nha", now=NOW)
    store.add(p2, "pc-nha", now=NOW)
    store.add(p3, "PC-Nha-2", now=NOW)
    assert {d.device_id for d in store.find_by_name("PC-NHA")} == {p1.device_id, p2.device_id}
    assert [d.device_id for d in store.find_by_name("pc-nha-2")] == [p3.device_id]
    assert store.find_by_name("PC") == []
    assert store.find_by_name("PC-Nh") == []
    assert store.find_by_name("") == []
    assert store.find_by_name("Máy lạ") == []


@pytest.mark.parametrize(
    "bad",
    ["", "x", "0" * 64, "Z" * 64, " ", "\n", "teleport-" + "0" * 32],
)
def test_lookups_with_malformed_input_return_none_or_false(cfg, peer, bad):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    assert store.get(bad) is None
    assert store.by_fingerprint(bad) is None
    assert store.is_trusted(bad) is False


def test_lookups_do_not_normalize_input(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    for fp in (peer.fingerprint.upper(), " " + peer.fingerprint, peer.fingerprint + "\n"):
        assert store.is_trusted(fp) is False
        assert store.by_fingerprint(fp) is None
    for did in (peer.device_id.upper(), "teleport-" + peer.device_id, peer.device_id + " "):
        assert store.get(did) is None


# --- Tên sai -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", " PC", "PC ", "x" * 65, "\x1b[31m", "a\u202eb", "a\u200bb", "\ufeffPC"])
def test_add_with_invalid_name_raises_value_error_and_writes_nothing(cfg, peer, name):
    store = TrustStore.load(cfg)
    with pytest.raises(ValueError):
        store.add(peer, name, now=NOW)
    assert store.devices() == []
    assert not (cfg / TRUSTED_FILE).exists()


def test_add_with_invalid_name_keeps_existing_file(cfg, peer, make_peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    before = (cfg / TRUSTED_FILE).read_bytes()
    with pytest.raises(ValueError):
        store.add(make_peer(), " xấu", now=NOW)
    with pytest.raises(ValueError):
        store.add(peer, "", now=NOW)  # đổi tên mục cũ sang tên sai cũng không được
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert [d.name for d in store.devices()] == ["PC"]


@pytest.mark.parametrize("name", ["Máy của Minh", "x" * 64, "👨\u200d👩\u200d👧"])
def test_add_with_valid_unicode_name(cfg, peer, name):
    TrustStore.load(cfg).add(peer, name, now=NOW)
    assert TrustStore.load(cfg).devices()[0].name == name


# --- File hỏng → TrustStoreError, file không bị đụng tới ---------------------------------------


def _without(d: dict, key: str) -> dict:
    return {k: v for k, v in d.items() if k != key}


def _corrupt_cases(peer: PeerCert, other: PeerCert, mismatched: PeerCert):
    good = entry(peer)
    return {
        "json-hong": None,
        "rong": "",
        "mang": [],
        "chuoi": "trusted",
        "version-2": {"version": 2, "devices": []},
        "version-99": {"version": 99, "devices": [good]},
        "thieu-version": {"devices": []},
        "version-bool": {"version": True, "devices": []},
        "version-float": {"version": 1.0, "devices": []},
        "version-0": {"version": 0, "devices": []},
        "version-am": {"version": -1, "devices": []},
        "version-chuoi": {"version": "1", "devices": []},
        "thieu-devices": {"version": 1},
        "devices-dict": {"version": 1, "devices": {}},
        "devices-null": {"version": 1, "devices": None},
        "muc-khong-phai-dict": {"version": 1, "devices": ["x"]},
        "thieu-device_id": {"version": 1, "devices": [_without(good, "device_id")]},
        "thieu-name": {"version": 1, "devices": [_without(good, "name")]},
        "thieu-fingerprint": {
            "version": 1,
            "devices": [_without(good, "fingerprint")],
        },
        "thieu-cert": {"version": 1, "devices": [_without(good, "cert")]},
        "thieu-paired_at": {"version": 1, "devices": [_without(good, "paired_at")]},
        "name-so": {"version": 1, "devices": [entry(peer, name=5)]},
        "name-rong": {"version": 1, "devices": [entry(peer, name="")]},
        "name-khoang-trang": {"version": 1, "devices": [entry(peer, name=" PC")]},
        "name-dieu-khien": {"version": 1, "devices": [entry(peer, name="a\u202eb")]},
        "device_id-khac-CN": {"version": 1, "devices": [entry(peer, device_id=other.device_id)]},
        "device_id-hoa": {"version": 1, "devices": [entry(peer, device_id=peer.device_id.upper())]},
        "fingerprint-lech": {"version": 1, "devices": [entry(peer, fingerprint=other.fingerprint)]},
        "fingerprint-hoa": {"version": 1, "devices": [entry(peer, fingerprint=peer.fingerprint.upper())]},
        "fingerprint-so": {"version": 1, "devices": [entry(peer, fingerprint=1)]},
        "cert-rac": {"version": 1, "devices": [entry(peer, cert="rác")]},
        "cert-cua-may-khac": {"version": 1, "devices": [entry(peer, cert=other.pem)]},
        "cert-ca-true": {"version": 1, "devices": [entry(mismatched)]},
        "paired_at-naive": {"version": 1, "devices": [entry(peer, paired_at="2026-10-20T08:15:00")]},
        "paired_at-rac": {"version": 1, "devices": [entry(peer, paired_at="hôm qua")]},
        "paired_at-so": {"version": 1, "devices": [entry(peer, paired_at=1760948100)]},
        "paired_at-chi-ngay": {"version": 1, "devices": [entry(peer, paired_at="2026-10-20")]},
        "name-qua-dai": {"version": 1, "devices": [entry(peer, name="x" * 65)]},
        "cert-so": {"version": 1, "devices": [entry(peer, cert=1)]},
        "device_id-so": {"version": 1, "devices": [entry(peer, device_id=1)]},
        "muc-null": {"version": 1, "devices": [None]},
        "trung-device_id-va-fp": {"version": 1, "devices": [good, entry(peer, name="Khác")]},
        "mot-muc-tot-mot-muc-xau": {"version": 1, "devices": [entry(other), entry(peer, name="")]},
    }


CORRUPT_IDS = list(_corrupt_cases(*(PeerCert("a", "b", "c"),) * 3))


@pytest.fixture
def ca_true_peer(make_cert):
    """PeerCert 'giả' từ cert CA=true (dựng tay, vì peer_cert_from_pem sẽ từ chối nó)."""
    import hashlib

    cert = make_cert(ca=True)
    der = cert.public_bytes(serialization.Encoding.DER)
    cn = cert.subject.rfc4514_string().removeprefix("CN=teleport-")
    return PeerCert(device_id=cn, fingerprint=hashlib.sha256(der).hexdigest(), pem=_pem(cert))


def test_entry_with_key_cert_sign_cert_rejected_and_file_untouched(cfg, make_cert):
    import hashlib

    from cryptography import x509

    ku = x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )
    cert = make_cert(cn="teleport-" + "ee" * 16, extensions=[(ku, True)])
    der = cert.public_bytes(serialization.Encoding.DER)
    fake = PeerCert(device_id="ee" * 16, fingerprint=hashlib.sha256(der).hexdigest(), pem=_pem(cert))
    before = write_store(cfg, {"version": 1, "devices": [entry(fake)]})
    mtime = (cfg / TRUSTED_FILE).stat().st_mtime_ns
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert (cfg / TRUSTED_FILE).stat().st_mtime_ns == mtime
    assert sorted(p.name for p in cfg.iterdir()) == [TRUSTED_FILE]


@pytest.mark.parametrize("case", CORRUPT_IDS)
def test_corrupt_file_raises_and_is_left_untouched(cfg, peer, make_peer, ca_true_peer, case):
    obj = _corrupt_cases(peer, make_peer(), ca_true_peer)[case]
    if case == "json-hong":
        before = write_store(cfg, None, raw='{"version": 1, "devices": [')
    elif case == "rong":
        before = write_store(cfg, None, raw="")
    else:
        before = write_store(cfg, obj)
    mtime = (cfg / TRUSTED_FILE).stat().st_mtime_ns

    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)
    # Lần hai vẫn lỗi: file không bị tự sửa, xóa hay tạo lại.
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)

    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert (cfg / TRUSTED_FILE).stat().st_mtime_ns == mtime
    assert sorted(p.name for p in cfg.iterdir()) == [TRUSTED_FILE]


def test_duplicate_device_id_with_different_certs_rejected(cfg, make_cert):
    # Hai cert khác khóa nhưng cùng CN → cùng device_id, khác fingerprint: mỗi mục tự nó hợp lệ.
    device_id = "cd" * 16
    a = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))
    b = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))
    before = write_store(cfg, {"version": 1, "devices": [entry(a, name="A"), entry(b, name="B")]})
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)
    assert (cfg / TRUSTED_FILE).read_bytes() == before


def test_duplicate_fingerprint_with_different_device_id_rejected(cfg, peer):
    # Hai mục cùng cert (nên cùng fp); mục thứ hai khai device_id khác → vừa lệch CN vừa trùng fp.
    write_store(cfg, {"version": 1, "devices": [entry(peer), entry(peer, name="B")]})
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)


def test_non_utf8_file_raises_trust_store_error(cfg):
    cfg.mkdir()
    (cfg / TRUSTED_FILE).write_bytes(b'{"version": 1, "devices": [], "x": "\xff\xfe"}')
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)


def test_version_2_file_is_not_overwritten(cfg, peer):
    before = write_store(cfg, {"version": 2, "devices": [], "new_feature": True})
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)
    assert (cfg / TRUSTED_FILE).read_bytes() == before


# --- M1: chứng chỉ có GeneralName lạ (x400Address/EDIPartyName) ---------------------------------


def _hand_made_peer(cert) -> PeerCert:
    """PeerCert dựng tay từ một chứng chỉ mà `peer_cert_from_pem` sẽ từ chối."""
    der = cert.public_bytes(serialization.Encoding.DER)
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    return PeerCert(
        device_id=cn.removeprefix("teleport-"), fingerprint=hashlib.sha256(der).hexdigest(), pem=_pem(cert)
    )


def test_m1_entry_with_unsupported_general_name_cert_raises_and_file_untouched(
    cfg, peer, make_cert, unsupported_name_ext
):
    """Mục khớp nhau về device_id/fingerprint nhưng chứng chỉ không phân tích được → TrustStoreError."""
    bad = _hand_made_peer(make_cert(extensions=[(unsupported_name_ext, False)]))
    before = write_store(cfg, {"version": 1, "devices": [entry(peer, name="Tốt"), entry(bad, name="Xấu")]})
    mtime = (cfg / TRUSTED_FILE).stat().st_mtime_ns

    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)
    # Lần hai vẫn lỗi: file không bị tự sửa, xóa hay tạo lại.
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)

    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert (cfg / TRUSTED_FILE).stat().st_mtime_ns == mtime
    assert sorted(p.name for p in cfg.iterdir()) == [TRUSTED_FILE]


def test_m1_add_with_unparseable_cert_raises_value_error_and_writes_nothing(
    cfg, peer, make_cert, unsupported_name_ext
):
    """`add` kiểm lại chứng chỉ: không phân tích được → ValueError, file và RAM giữ nguyên."""
    store = TrustStore.load(cfg)
    device = store.add(peer, "PC", now=NOW)
    before = (cfg / TRUSTED_FILE).read_bytes()
    mtime = (cfg / TRUSTED_FILE).stat().st_mtime_ns
    bad = _hand_made_peer(make_cert(extensions=[(unsupported_name_ext, False)]))

    with pytest.raises(ValueError):
        store.add(bad, "Xấu", now=NOW)

    assert store.devices() == [device]
    assert store.get(bad.device_id) is None
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert (cfg / TRUSTED_FILE).stat().st_mtime_ns == mtime
    assert TrustStore.load(cfg).devices() == [device]


# --- Ghi nguyên tử, lỗi ghi --------------------------------------------------------------------


def test_permission_error_on_replace_retries_then_raises_and_keeps_state(cfg, peer, make_peer, monkeypatch):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    before = (cfg / TRUSTED_FILE).read_bytes()
    devices_before = store.devices()

    calls = []
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == TRUSTED_FILE:
            calls.append((src, dst))
            raise PermissionError(13, "giả lập file đang bị khóa")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    new_peer = make_peer()
    with pytest.raises(TrustStoreError) as exc:
        store.add(new_peer, "Mới", now=NOW)
    monkeypatch.undo()

    # Bọc lỗi gốc.
    chain = [exc.value.__cause__, exc.value.__context__]
    assert any(isinstance(e, PermissionError) for e in chain)
    # Thử lại nhiều lần (tối đa 5 lần thử lại).
    assert 2 <= len(calls) <= 6
    # RAM chỉ đổi khi ghi đĩa thành công.
    assert store.devices() == devices_before
    assert store.is_trusted(new_peer.fingerprint) is False
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert no_tmp_left(cfg)

    with pytest.raises(TrustStoreError):
        monkeypatch.setattr(os, "replace", failing_replace)
        store.remove(peer.device_id)
    monkeypatch.undo()
    assert store.get(peer.device_id) is not None
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert no_tmp_left(cfg)


def test_transient_permission_error_on_replace_is_retried(cfg, peer, monkeypatch):
    real_replace = os.replace
    failures = {"left": 2}

    def flaky_replace(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == TRUSTED_FILE and failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "giả lập antivirus giữ file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    monkeypatch.undo()

    assert failures["left"] == 0
    assert [d.device_id for d in TrustStore.load(cfg).devices()] == [peer.device_id]
    assert no_tmp_left(cfg)


def test_write_failure_when_target_is_directory_keeps_state(cfg, peer):
    store = TrustStore.load(cfg)
    cfg.mkdir(parents=True)
    (cfg / TRUSTED_FILE).mkdir()  # không thể thay thế thư mục bằng file
    with pytest.raises((TrustStoreError, OSError)):
        store.add(peer, "PC", now=NOW)
    assert store.devices() == []
    assert store.is_trusted(peer.fingerprint) is False
    assert (cfg / TRUSTED_FILE).is_dir()
    assert not (cfg / "trusted.json.tmp").exists()


def test_save_uses_tmp_then_replace(cfg, peer, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy_replace(src, dst, *args, **kwargs):
        seen.append((os.path.basename(os.fspath(src)), os.path.basename(os.fspath(dst))))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)
    TrustStore.load(cfg).add(peer, "PC", now=NOW)
    assert ("trusted.json.tmp", "trusted.json") in seen


def test_stores_on_same_dir_see_each_others_saved_changes(cfg, peer):
    a = TrustStore.load(cfg)
    a.add(peer, "PC", now=NOW)
    b = TrustStore.load(cfg)
    assert b.is_trusted(peer.fingerprint)
    b.remove(peer.device_id)
    assert TrustStore.load(cfg).devices() == []


# --- C1b: `rename` (spec đợt C §2.2, §2.5) -----------------------------------------------------


def test_rename_keeps_cert_and_paired_at(cfg, peer, make_peer):
    store = TrustStore.load(cfg)
    other = make_peer()
    store.add(peer, "PC-Cu", now=NOW)
    store.add(other, "Khac", now=NOW)
    before = store.get(peer.device_id)

    renamed = store.rename(peer.device_id, "PC-Moi")

    assert renamed == dataclasses.replace(before, name="PC-Moi")
    assert store.get(peer.device_id) == renamed
    assert store.find_by_name("PC-Cu") == []
    assert store.find_by_name("PC-Moi") == [renamed]
    # Ghi đĩa: đọc lại thấy tên mới, chứng chỉ / fingerprint / paired_at giữ nguyên; máy khác không đổi.
    reloaded = TrustStore.load(cfg)
    assert reloaded.get(peer.device_id) == renamed
    assert reloaded.get(peer.device_id).cert_pem == before.cert_pem
    assert reloaded.get(peer.device_id).fingerprint == peer.fingerprint
    assert reloaded.get(peer.device_id).paired_at == NOW_TRUNC
    assert reloaded.get(other.device_id).name == "Khac"
    assert reloaded.is_trusted(peer.fingerprint) and reloaded.is_trusted(other.fingerprint)
    assert no_tmp_left(cfg)


def test_rename_to_name_of_other_device_still_applies(cfg, peer, make_peer):
    """Mục 11 M2: đổi tên trùng tên máy khác vẫn cập nhật (tìm theo tên sẽ báo nhiều kết quả)."""
    store = TrustStore.load(cfg)
    other = make_peer()
    store.add(peer, "PC-Cu", now=NOW)
    store.add(other, "Trung", now=NOW)
    store.rename(peer.device_id, "Trung")
    assert {d.device_id for d in TrustStore.load(cfg).find_by_name("Trung")} == {
        peer.device_id,
        other.device_id,
    }


def test_rename_same_name_no_write(cfg, peer, monkeypatch):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    before = store.get(peer.device_id)
    writes = []
    real_replace = os.replace

    def spy_replace(src, dst, *args, **kwargs):
        writes.append(os.path.basename(os.fspath(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)
    mtime = (cfg / TRUSTED_FILE).stat().st_mtime_ns
    result = store.rename(peer.device_id, "PC")
    assert result == before
    assert TRUSTED_FILE not in writes
    assert (cfg / TRUSTED_FILE).stat().st_mtime_ns == mtime
    assert no_tmp_left(cfg)


@pytest.mark.parametrize("name", ["", " PC", "PC ", "x" * 65, "\x1b[31m", "a\u202eb", "\ufeffPC", 123, None])
def test_rename_unknown_keyerror_invalid_name(cfg, peer, make_peer, name):
    from teleport.core.messages import MessageError

    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    before = (cfg / TRUSTED_FILE).read_bytes()
    # Tên sai trên thiết bị có thật → MessageError, không ghi.
    with pytest.raises(MessageError):
        store.rename(peer.device_id, name)
    # Tên đúng trên thiết bị không có → KeyError, không ghi.
    with pytest.raises(KeyError):
        store.rename(make_peer().device_id, "Ten-hop-le")
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert [d.name for d in store.devices()] == ["PC"]


def test_rename_write_failure_raises_and_keeps_ram(cfg, peer, monkeypatch):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    before_bytes = (cfg / TRUSTED_FILE).read_bytes()
    before = store.get(peer.device_id)
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == TRUSTED_FILE:
            raise PermissionError(13, "giả lập file đang bị khóa")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(TrustStoreError):
        store.rename(peer.device_id, "Moi")
    monkeypatch.undo()
    assert store.get(peer.device_id) == before
    assert store.find_by_name("Moi") == []
    assert (cfg / TRUSTED_FILE).read_bytes() == before_bytes
    assert no_tmp_left(cfg)


# =================================================================================================
# F3a (specs/spec-f3a.md mục 2.2, 6.2, 10.4): trường tùy chọn `last_address` — S1–S9
# =================================================================================================

STORE_LOGGER = "teleport.trust.store"
IP = "192.168.1.5"
PORT = 47474


def _entries(cfg) -> list[dict]:
    return json.loads((cfg / TRUSTED_FILE).read_text(encoding="utf-8"))["devices"]


def _entry_of(cfg, device_id: str) -> dict:
    return next(e for e in _entries(cfg) if e["device_id"] == device_id)


def _replace_spy(monkeypatch) -> list[str]:
    """Ghi lại mọi lần `os.replace` vào `trusted.json` (mỗi lần ghi nguyên tử kết thúc bằng một lần)."""
    writes: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst, *args, **kwargs):
        writes.append(os.path.basename(os.fspath(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)
    return writes


def test_s_trusted_device_last_address_is_last_field_with_default_none():
    fields = dataclasses.fields(TrustedDevice)
    assert fields[-1].name == "last_address"
    assert fields[-1].default is None
    assert [f.name for f in fields[:5]] == ["device_id", "name", "fingerprint", "cert_pem", "paired_at"]


# --- S1: file cũ ------------------------------------------------------------------------------------


def test_s1_old_file_without_last_address_loads_none_and_rewrite_has_no_key(cfg, peer, make_peer, caplog):
    other = make_peer()
    write_store(cfg, {"version": 1, "devices": [entry(peer, "A"), entry(other, "B")]})
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        store = TrustStore.load(cfg)
    assert [d.last_address for d in store.devices()] == [None, None]
    assert caplog.records == [], "thiếu trường là bình thường, không WARNING"
    store.rename(peer.device_id, "A2")  # buộc ghi lại
    for e in _entries(cfg):
        assert "last_address" not in e, e
    assert all(d.last_address is None for d in TrustStore.load(cfg).devices())


def test_s1_add_writes_no_last_address_key(cfg, peer):
    device = TrustStore.load(cfg).add(peer, "PC", now=NOW)
    assert device.last_address is None
    assert "last_address" not in _entry_of(cfg, peer.device_id)


# --- S2: set_last_address -----------------------------------------------------------------------------


def test_s2_set_last_address_writes_object_after_paired_at_and_reloads(cfg, peer, make_peer):
    other = make_peer()
    store = TrustStore.load(cfg)
    store.add(peer, "A", now=NOW)
    store.add(other, "B", now=NOW)
    updated = store.set_last_address(peer.device_id, IP, PORT)
    assert isinstance(updated, TrustedDevice)
    assert updated.last_address == (IP, PORT)
    assert store.get(peer.device_id) == updated
    e = _entry_of(cfg, peer.device_id)
    assert e["last_address"] == {"ip": IP, "port": PORT}
    assert list(e) == ["device_id", "name", "fingerprint", "cert", "paired_at", "last_address"]
    assert type(e["last_address"]["port"]) is int
    assert "last_address" not in _entry_of(cfg, other.device_id), "mục khác không đổi"
    reloaded = TrustStore.load(cfg)
    assert reloaded.get(peer.device_id) == updated
    assert reloaded.get(other.device_id).last_address is None
    assert no_tmp_left(cfg)


def test_s2_same_value_does_not_write(cfg, peer, monkeypatch):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    first = store.set_last_address(peer.device_id, IP, PORT)
    before = (cfg / TRUSTED_FILE).read_bytes()
    mtime = (cfg / TRUSTED_FILE).stat().st_mtime_ns
    writes = _replace_spy(monkeypatch)
    again = store.set_last_address(peer.device_id, IP, PORT)
    assert again == first
    assert TRUSTED_FILE not in writes
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert (cfg / TRUSTED_FILE).stat().st_mtime_ns == mtime


def test_s2_new_value_replaces_old_one(cfg, peer, monkeypatch):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, IP, PORT)
    writes = _replace_spy(monkeypatch)
    store.set_last_address(peer.device_id, IP, 50123)
    assert writes.count(TRUSTED_FILE) == 1
    assert _entry_of(cfg, peer.device_id)["last_address"] == {"ip": IP, "port": 50123}
    store.set_last_address(peer.device_id, "10.0.0.7", 50123)
    assert writes.count(TRUSTED_FILE) == 2
    assert TrustStore.load(cfg).get(peer.device_id).last_address == ("10.0.0.7", 50123)


# --- S3: last_address hỏng: chỉ bỏ riêng trường đó ----------------------------------------------------

_HUGE_IP = "x" * 1_000_000
# (giá trị JSON của `last_address`, chuỗi không được xuất hiện trong log, id)
_S3_BAD = [
    ("1.2.3.4:5", "1.2.3.4", "chuoi"),
    (["1.2.3.4", 47474], "1.2.3.4", "mang"),
    ({"ip": 1, "port": 2}, None, "ip-so"),
    ({"ip": "1.2.3.4"}, "1.2.3.4", "thieu-port"),
    ({"port": 47474}, None, "thieu-ip"),
    ({"ip": "1.2.3.4", "port": 0}, "1.2.3.4", "port-0"),
    ({"ip": "1.2.3.4", "port": 70000}, "70000", "port-70000"),
    ({"ip": "1.2.3.4", "port": -1}, "1.2.3.4", "port-am"),
    (True, None, "true"),
    (47474, "47474", "so"),
    ({"ip": "1.2.3.4", "port": "47474"}, "47474", "port-chuoi"),
    ({"ip": "1.2.3.4", "port": True}, "1.2.3.4", "port-true"),
    ({"ip": "1.2.3.4", "port": 47474.0}, "47474", "port-float"),
    ({"ip": "::1", "port": 47474}, "::1", "ipv6"),
    ({"ip": "224.0.0.1", "port": 47474}, "224.0.0.1", "multicast"),
    ({"ip": "0.0.0.0", "port": 47474}, "0.0.0.0", "unspecified"),
    ({"ip": "255.255.255.255", "port": 47474}, "255.255.255.255", "broadcast"),
    ({"ip": "192.168.001.5", "port": 47474}, "192.168.001.5", "so-0-dau"),
    ({"ip": " 192.168.1.5", "port": 47474}, "192.168.1.5", "khoang-trang"),
    ({"ip": _HUGE_IP, "port": 47474}, "x" * 64, "chuoi-1MB"),
]


@pytest.mark.parametrize("bad, secret", [(b, s) for b, s, _ in _S3_BAD], ids=[i for _, _, i in _S3_BAD])
def test_s3_invalid_last_address_drops_only_that_field_and_warns_without_value(
    cfg, peer, make_peer, caplog, bad, secret
):
    good, broken = make_peer(), peer
    entries = [
        entry(good, "A", last_address={"ip": "10.0.0.7", "port": 50123}),
        entry(broken, "B", last_address=bad),
    ]
    write_store(cfg, {"version": 1, "devices": entries})
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        store = TrustStore.load(cfg)
    a, b = store.get(good.device_id), store.get(broken.device_id)
    assert a is not None and b is not None, "cả hai mục vẫn nạp"
    assert a.last_address == ("10.0.0.7", 50123), "mục khác giữ nguyên"
    assert b.last_address is None
    assert (b.name, b.fingerprint, b.cert_pem, b.paired_at) == (
        "B",
        broken.fingerprint,
        broken.pem,
        NOW_TRUNC,
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage()[:200] for r in caplog.records]
    text = warnings[0].getMessage()
    assert "mục 1" in text and "last_address" in text, text[:200]
    if secret is not None:
        assert secret not in text, f"WARNING không được chứa giá trị: {text[:200]!r}"
    assert len(text) < 200


def test_s3_next_write_cleans_invalid_field(cfg, peer, make_peer):
    good = make_peer()
    write_store(
        cfg,
        {
            "version": 1,
            "devices": [
                entry(good, "A", last_address={"ip": "10.0.0.7", "port": 50123}),
                entry(peer, "B", last_address="rác"),
            ],
        },
    )
    store = TrustStore.load(cfg)
    store.rename(peer.device_id, "B2")
    assert "last_address" not in _entry_of(cfg, peer.device_id)
    assert _entry_of(cfg, good.device_id)["last_address"] == {"ip": "10.0.0.7", "port": 50123}


def test_s3_invalid_last_address_with_otherwise_corrupt_entry_still_rejects_file(cfg, peer):
    """Khoan dung chỉ riêng `last_address`: mục sai ở trường khác vẫn làm cả file bị từ chối như cũ."""
    write_store(cfg, {"version": 1, "devices": [entry(peer, name="", last_address="rác")]})
    with pytest.raises(TrustStoreError):
        TrustStore.load(cfg)


# --- S4: rename / remove / add ----------------------------------------------------------------------


def test_s4_rename_keeps_last_address(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, IP, PORT)
    renamed = store.rename(peer.device_id, "Moi")
    assert renamed.last_address == (IP, PORT)
    assert TrustStore.load(cfg).get(peer.device_id).last_address == (IP, PORT)


def test_s4_remove_drops_last_address_and_readd_starts_from_none(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, IP, PORT)
    store.remove(peer.device_id)
    assert all(e["device_id"] != peer.device_id for e in _entries(cfg))
    again = store.add(peer, "PC", now=NOW)
    assert again.last_address is None, "hủy rồi ghép lại: bắt đầu từ None"
    assert "last_address" not in _entry_of(cfg, peer.device_id)


def test_s4_add_same_device_id_and_fingerprint_keeps_last_address(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, IP, PORT)
    later = NOW + datetime.timedelta(days=1)
    again = store.add(peer, "Ten-Moi", now=later)
    assert again.last_address == (IP, PORT)
    assert again.name == "Ten-Moi" and again.paired_at == later.replace(microsecond=0)
    assert TrustStore.load(cfg).get(peer.device_id).last_address == (IP, PORT)


def test_s4_add_same_device_id_new_fingerprint_drops_last_address(cfg, make_cert):
    device_id = "cd" * 16
    old = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))
    new = peer_cert_from_pem(_pem(make_cert(cn="teleport-" + device_id)))
    store = TrustStore.load(cfg)
    store.add(old, "PC", now=NOW)
    store.set_last_address(device_id, IP, PORT)
    replaced = store.add(new, "PC", now=NOW)
    assert replaced.last_address is None
    assert "last_address" not in _entry_of(cfg, device_id)


def test_s4_add_new_device_has_no_address_and_keeps_others(cfg, peer, make_peer):
    store = TrustStore.load(cfg)
    store.add(peer, "A", now=NOW)
    store.set_last_address(peer.device_id, IP, PORT)
    other = make_peer()
    added = store.add(other, "B", now=NOW)
    assert added.last_address is None
    assert store.get(peer.device_id).last_address == (IP, PORT)


# --- S5: lỗi của set_last_address ---------------------------------------------------------------------


def test_s5_set_last_address_unknown_device_raises_key_error(cfg, peer, make_peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    before = (cfg / TRUSTED_FILE).read_bytes()
    with pytest.raises(KeyError):
        store.set_last_address(make_peer().device_id, IP, PORT)
    assert (cfg / TRUSTED_FILE).read_bytes() == before


@pytest.mark.parametrize(
    "ip, port",
    [
        ("::1", PORT),
        ("0.0.0.0", PORT),
        ("224.0.0.251", PORT),
        ("255.255.255.255", PORT),
        ("192.168.001.5", PORT),
        ("1.2.3", PORT),
        (None, PORT),
        (b"1.2.3.4", PORT),
        (IP, 0),
        (IP, 65536),
        (IP, True),
        (IP, 47474.0),
        (IP, "47474"),
    ],
    ids=[
        "ipv6",
        "unspecified",
        "multicast",
        "broadcast",
        "so-0-dau",
        "thieu-o",
        "ip-none",
        "ip-bytes",
        "port-0",
        "port-65536",
        "port-true",
        "port-float",
        "port-chuoi",
    ],
)
def test_s5_set_last_address_invalid_value_raises_value_error(cfg, peer, ip, port):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, "10.0.0.7", 50123)
    before = (cfg / TRUSTED_FILE).read_bytes()
    with pytest.raises(ValueError):
        store.set_last_address(peer.device_id, ip, port)
    assert (cfg / TRUSTED_FILE).read_bytes() == before
    assert store.get(peer.device_id).last_address == ("10.0.0.7", 50123)


def test_s5_set_last_address_write_failure_raises_and_keeps_ram(cfg, peer, monkeypatch):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, "10.0.0.7", 50123)
    before_bytes = (cfg / TRUSTED_FILE).read_bytes()
    before = store.get(peer.device_id)
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == TRUSTED_FILE:
            raise PermissionError(13, "giả lập file đang bị khóa")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(TrustStoreError):
        store.set_last_address(peer.device_id, IP, PORT)
    monkeypatch.undo()
    assert store.get(peer.device_id) == before, "RAM không đổi khi ghi lỗi"
    assert (cfg / TRUSTED_FILE).read_bytes() == before_bytes
    assert no_tmp_left(cfg)


# --- S6, S7, S8, S9 -----------------------------------------------------------------------------------


def test_s6_null_last_address_is_none_without_warning(cfg, peer, caplog):
    write_store(cfg, {"version": 1, "devices": [entry(peer, last_address=None)]})
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        store = TrustStore.load(cfg)
    assert store.get(peer.device_id).last_address is None
    assert caplog.records == []


def test_s7_unknown_keys_inside_last_address_are_ignored(cfg, peer):
    value = {"ip": IP, "port": PORT, "via": "mdns", "seen": 3}
    write_store(cfg, {"version": 1, "devices": [entry(peer, last_address=value)]})
    store = TrustStore.load(cfg)
    assert store.get(peer.device_id).last_address == (IP, PORT)
    store.rename(peer.device_id, "Moi")
    assert _entry_of(cfg, peer.device_id)["last_address"] == {"ip": IP, "port": PORT}


def test_s8_version_stays_1_after_set_last_address(cfg, peer):
    store = TrustStore.load(cfg)
    store.add(peer, "PC", now=NOW)
    store.set_last_address(peer.device_id, IP, PORT)
    assert json.loads((cfg / TRUSTED_FILE).read_text(encoding="utf-8"))["version"] == 1
    assert STORE_VERSION == 1


@pytest.mark.parametrize("ip", ["8.8.8.8", "127.0.0.1", "169.254.1.1"])
def test_s9_store_accepts_global_loopback_and_link_local_addresses(cfg, peer, ip, caplog):
    """10.4: `trusted.json` đọc theo `check_endpoint`: địa chỉ global hợp lệ về dạng vẫn nạp (chỉ không
    dùng)."""
    write_store(cfg, {"version": 1, "devices": [entry(peer, last_address={"ip": ip, "port": PORT})]})
    with caplog.at_level(logging.WARNING, logger=STORE_LOGGER):
        store = TrustStore.load(cfg)
    assert store.get(peer.device_id).last_address == (ip, PORT)
    assert caplog.records == []
    store.set_last_address(peer.device_id, ip, 50123)
    assert TrustStore.load(cfg).get(peer.device_id).last_address == (ip, 50123)
