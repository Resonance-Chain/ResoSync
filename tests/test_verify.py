"""Verifica del cloud: ogni salvataggio attuale c'è ed è integro, sennò si ripara."""
import json
import os
from pathlib import Path

import pytest

from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.crypto import EncryptedBackend
from resosync.engine import SyncEngine


def setup(tmp_path, encrypt=False):
    s = Settings(device_name="PC", encrypt=encrypt, passphrase="passphrase di prova")
    backend = FolderBackend(tmp_path / "cloud")
    if encrypt:
        backend = EncryptedBackend(backend, s.passphrase)
    eng = SyncEngine(s, backend, tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "a.sav").write_bytes(b"slot a")
    Path(g.path, "b.sav").write_bytes(b"slot b")
    eng.sync_game(g)
    return eng, g


def blob(tmp_path, rel):
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes()) \
        if (tmp_path / "cloud/g/manifest.json").read_bytes().startswith(b"{") else None
    return tmp_path / "cloud" / m["files"][rel]["blob"]


def test_resync_does_not_reuse_corrupt_blob(tmp_path):
    """Ricaricando lo stesso contenuto, un blob rovinato con lo stesso hash nel nome non va
    riusato (punterebbe di nuovo alla copia rotta)."""
    eng, g = setup(tmp_path)
    good = blob(tmp_path, "a.sav")
    good.write_bytes(b"x")  # rovinato e di dimensione diversa
    Path(g.path, "a.sav").write_bytes(b"altro")
    eng.sync_game(g)
    Path(g.path, "a.sav").write_bytes(b"slot a")  # torna il contenuto di prima
    eng.sync_game(g)
    assert blob(tmp_path, "a.sav") != good
    assert blob(tmp_path, "a.sav").read_bytes() == b"slot a"
