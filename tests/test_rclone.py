"""Backend rclone: con un rclone vero (remote "alias" su una cartella locale) e con un rclone
finto per gli errori."""
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from resosync.backends import RcloneBackend, RcloneError, make_backend
from resosync.config import Config, Game, Settings
from resosync.engine import SyncEngine

needs_rclone = pytest.mark.skipif(not shutil.which("rclone"), reason="rclone non installato")


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "remoto"
    root.mkdir()
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[test]\ntype = alias\nremote = {root.as_posix()}\n")
    return root, str(conf)


@needs_rclone
def test_backend_operations(remote, tmp_path):
    root, conf = remote
    b = RcloneBackend("test:ResoSync", config=conf)
    assert b.read_bytes("g/manifest.json") is None
    assert b.sizes("g/") == {}
    b.write_bytes("g/manifest.json", b'{"files": {}}')
    assert b.read_bytes("g/manifest.json") == b'{"files": {}}'
    src = tmp_path / "s.sav"
    src.write_bytes(b"x" * 1000)
    b.upload_file("g/blobs/s.sav/1__pc__abc", src)
    assert b.sizes("g/blobs/") == {"g/blobs/s.sav/1__pc__abc": 1000}
    assert b.list("g/blobs/s.sav/") == ["g/blobs/s.sav/1__pc__abc"]
    assert b.sizes("g/blobs/s.sav/1") == {"g/blobs/s.sav/1__pc__abc": 1000}  # prefisso parziale
    out = tmp_path / "out"
    b.download_file("g/blobs/s.sav/1__pc__abc", out)
    assert out.read_bytes() == b"x" * 1000
    with pytest.raises(FileNotFoundError):
        b.download_file("g/blobs/s.sav/manca", tmp_path / "out2")
    b.copy("g/manifest.json", "g/copia.json")
    assert b.read_bytes("g/copia.json") == b'{"files": {}}'
    b.delete("g/copia.json")
    b.delete("g/non-esiste.json")  # non è un errore
    assert b.read_bytes("g/copia.json") is None
    assert (root / "ResoSync/g/manifest.json").exists()


@needs_rclone
def test_stale_temp_file_not_reused(remote, tmp_path):
    """copyto salta la copia se la destinazione sembra uguale (stessa dimensione e data)."""
    root, conf = remote
    b = RcloneBackend("test:ResoSync", config=conf)
    src = tmp_path / "s.sav"
    src.write_bytes(b"giusto")
    b.upload_file("k", src)
    stale = tmp_path / "tmp"
    stale.write_bytes(b"vecchi")  # stessa dimensione
    m = (root / "ResoSync/k").stat().st_mtime
    os.utime(stale, (m, m))
    b.download_file("k", stale)
    assert stale.read_bytes() == b"giusto"


@needs_rclone
def test_two_pcs_sync_through_rclone(remote, tmp_path):
    root, conf = remote
    pcs = []
    for name in ("PC", "Deck"):
        s = Settings(device_name=name, backend="rclone", rclone_remote="test:ResoSync",
                     rclone_config=conf)
        eng = SyncEngine(s, make_backend(s), tmp_path / f"app-{name}", log=lambda m: None)
        g = Game("g", "G", str(tmp_path / f"saves-{name}"), group_by="folder")
        Path(g.path).mkdir()
        pcs.append((eng, g))
    (a, ga), (b, gb) = pcs
    Path(ga.path, "Slot1").mkdir()
    Path(ga.path, "Slot1/sav.dat").write_bytes(b"partita PC")
    Path(ga.path, "Slot1/meta.json").write_bytes(b"{}")
    assert a.sync_game(ga)["up"] == 2
    assert b.sync_game(gb)["down"] == 2
    assert Path(gb.path, "Slot1/sav.dat").read_bytes() == b"partita PC"
    Path(gb.path, "Slot1/sav.dat").write_bytes(b"partita Deck")
    b.sync_game(gb)
    a.sync_game(ga)
    assert Path(ga.path, "Slot1/sav.dat").read_bytes() == b"partita Deck"
    assert not (root / "ResoSync/g/lock.json").exists()


@needs_rclone
def test_encryption_over_rclone(remote, tmp_path):
    root, conf = remote
    s = Settings(device_name="PC", backend="rclone", rclone_remote="test:Cifrato",
                 rclone_config=conf, encrypt=True, passphrase="una passphrase lunga")
    eng = SyncEngine(s, make_backend(s), tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_bytes(b"segreto")
    eng.sync_game(g)
    blob = next((root / "Cifrato/g/blobs").rglob("*__*"))
    assert b"segreto" not in blob.read_bytes()


def fake_rclone(tmp_path, code, stderr="errore finto"):
    """Un 'rclone' che fallisce sempre con il codice indicato."""
    if os.name == "nt":
        exe = tmp_path / "rclone.bat"
        exe.write_text(f"@echo {stderr} 1>&2\r\n@exit /b {code}\r\n")
    else:
        exe = tmp_path / "rclone"
        exe.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {code}\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_network_error_is_not_an_empty_cloud(tmp_path):
    """Il punto più delicato: se rclone fallisce per la rete, il manifest NON deve sembrare
    assente (il cloud sembrerebbe vuoto e cancellato)."""
    b = RcloneBackend("x:ResoSync", exe=fake_rclone(tmp_path, 1, "connection refused"))
    with pytest.raises(RcloneError, match="connection refused"):
        b.read_bytes("g/manifest.json")
    with pytest.raises(RcloneError):
        b.sizes("g/blobs/")


def test_not_found_codes(tmp_path):
    b = RcloneBackend("x:ResoSync", exe=fake_rclone(tmp_path, 3))
    assert b.read_bytes("g/manifest.json") is None
    assert b.sizes("g/") == {}
    with pytest.raises(FileNotFoundError):
        b.download_file("g/blob", tmp_path / "out")


def test_network_error_does_not_touch_saves(tmp_path):
    a = tmp_path / "saves"
    a.mkdir()
    (a / "s.sav").write_bytes(b"progressi")
    s = Settings(device_name="PC", propagate_deletes=True)
    eng = SyncEngine(s, RcloneBackend("x:R", exe=fake_rclone(tmp_path, 1)), tmp_path / "app",
                     log=lambda m: None)
    with pytest.raises(RcloneError):
        eng.sync_game(Game("g", "G", str(a)))
    assert (a / "s.sav").read_bytes() == b"progressi"


def test_remote_must_have_colon(tmp_path):
    with pytest.raises(ValueError):
        RcloneBackend("ResoSync", exe=fake_rclone(tmp_path, 0))


def test_missing_rclone_has_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    with pytest.raises(FileNotFoundError, match="rclone"):
        RcloneBackend("x:R")


def test_config_identity_and_configured(tmp_path):
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.backend = "rclone"
    assert not cfg.is_configured()
    cfg.settings.rclone_remote = "proton:ResoSync"
    assert cfg.is_configured()
    before = cfg.cloud_identity()
    cfg.settings.rclone_remote = "proton:ResoSync/"
    assert cfg.cloud_identity() == before  # stessa cartella
    cfg.settings.rclone_remote = "gdrive:ResoSync"
    assert cfg.cloud_identity() != before
    cfg.save()
    assert json.loads((tmp_path / "app/config.json").read_text())["settings"]["rclone_remote"]


@needs_rclone
def test_list_remotes(remote):
    from resosync.backends import rclone_remotes
    assert rclone_remotes(config=remote[1]) == ["test:"]
