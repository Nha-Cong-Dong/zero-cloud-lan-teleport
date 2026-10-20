import pytest

from teleport import __version__
from teleport.cli import main
from teleport.paths import ENV_HOME


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_identity_is_created_once_and_stays_stable(tmp_path, capsys):
    assert main(["--config-dir", str(tmp_path), "identity"]) == 0
    first = capsys.readouterr().out
    assert main(["--config-dir", str(tmp_path), "identity"]) == 0
    assert capsys.readouterr().out == first
    assert "Fingerprint:" in first


def test_identity_uses_teleport_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV_HOME, str(tmp_path))
    assert main(["identity"]) == 0
    assert str(tmp_path) in capsys.readouterr().out
    assert (tmp_path / "device.crt").exists()


def test_corrupt_identity_exits_1_with_message_not_traceback(tmp_path, capsys):
    assert main(["--config-dir", str(tmp_path), "identity"]) == 0
    (tmp_path / "device.crt").write_bytes(b"garbage")
    assert main(["--config-dir", str(tmp_path), "identity"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Lỗi: Danh tính thiết bị")
    assert "Traceback" not in err
