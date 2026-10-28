import json
import time
from pathlib import Path

import pytest

from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.crypto import CryptoError, EncryptedBackend, cloud_is_encrypted
from resosync.engine import CloudBusy, SyncEngine


def test_roundtrip_and_ciphertext(tmp_path):
    raw = FolderBackend(tmp_path / "cloud")
    enc = EncryptedBackend(raw, "passphrase-lunga")
    enc.write_bytes("g/manifest.json", b'{"segreto": 1}')
    assert enc.read_bytes("g/manifest.json") == b'{"segreto": 1}'
    assert b"segreto" not in (tmp_path / "cloud/g/manifest.json").read_bytes()
    src = tmp_path / "save.dat"
    src.write_bytes(b"x" * 100_000)
    enc.upload_file("g/files/save.dat", src)
    enc.copy("g/files/save.dat", "g/versions/save.dat/v1")
    out = tmp_path / "out.dat"
    enc.download_file("g/versions/save.dat/v1", out)
    assert out.read_bytes() == src.read_bytes()
    assert cloud_is_encrypted(raw)


def test_wrong_passphrase(tmp_path):
    raw = FolderBackend(tmp_path)
    EncryptedBackend(raw, "giusta-giusta")
    with pytest.raises(CryptoError):
        EncryptedBackend(raw, "sbagliata!!")


def test_legacy_plaintext_still_readable(tmp_path):
    raw = FolderBackend(tmp_path)
    raw.write_bytes("vecchio.json", b"in chiaro")
    assert EncryptedBackend(raw, "passphrase-lunga").read_bytes("vecchio.json") == b"in chiaro"


def _engine(tmp_path, name, backend):
    s = Settings(device_name=name)
    return SyncEngine(s, backend, tmp_path / name, log=lambda m: None)


def test_two_pcs_encrypted(tmp_path):
    raw = FolderBackend(tmp_path / "cloud")
    a = _engine(tmp_path, "A", EncryptedBackend(raw, "passphrase-lunga"))
    b = _engine(tmp_path, "B", EncryptedBackend(raw, "passphrase-lunga"))
    ga = Game("g", "G", str(tmp_path / "A-saves"))
    gb = Game("g", "G", str(tmp_path / "B-saves"))
    Path(ga.path).mkdir()
    Path(ga.path, "slot.sav").write_bytes(b"progressi")
    assert a.sync_game(ga)["up"] == 1
    assert b.sync_game(gb)["down"] == 1
    assert Path(gb.path, "slot.sav").read_bytes() == b"progressi"
    blobs = list((tmp_path / "cloud/g/blobs/slot.sav").iterdir())
    assert len(blobs) == 1 and b"progressi" not in blobs[0].read_bytes()


def test_lock(tmp_path):
    backend = FolderBackend(tmp_path)
    a = _engine(tmp_path, "A", backend)
    b = _engine(tmp_path, "B", backend)
    token = a.acquire_lock("g")
    with pytest.raises(CloudBusy, match="A"):
        b.acquire_lock("g")
    a.release_lock("g", token)
    b.release_lock("g", b.acquire_lock("g"))
    # lock scaduto (PC piantato): si può prendere
    backend.write_bytes("g/lock.json", json.dumps(
        {"token": "x", "device": "morto", "expires": time.time() - 1}).encode())
    assert b.acquire_lock("g")


def test_sync_waits_for_busy_lock(tmp_path):
    backend = FolderBackend(tmp_path / "cloud")
    a = _engine(tmp_path, "A", backend)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_text("1")
    a.acquire_lock("g")
    with pytest.raises(CloudBusy):
        _engine(tmp_path, "B", backend).sync_game(g)
