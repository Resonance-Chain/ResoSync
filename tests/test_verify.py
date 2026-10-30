"""Verifica del cloud: ogni salvataggio attuale c'è ed è integro, sennò si ripara."""
import json
import os
from pathlib import Path

import pytest

from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.crypto import EncryptedBackend
from resosync.engine import SyncEngine


@pytest.fixture(autouse=True)
def old_uploads(monkeypatch):
    """Qui i salvataggi sono caricati "da tempo": quelli appena caricati sono in arrivo e
    non si giudicano (vedi test_bughunt_056)."""
    import resosync.engine as em
    monkeypatch.setattr(em, "GRACE", 0)


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


def test_all_good(tmp_path):
    eng, g = setup(tmp_path)
    assert eng.verify_cloud(g) == {"ok": 2, "repaired": [], "broken": [], "arriving": []}


def test_corrupt_blob_repaired_from_local_file(tmp_path):
    eng, g = setup(tmp_path)
    bad = blob(tmp_path, "a.sav")
    bad.write_bytes(b"rovinato")
    rep = eng.verify_cloud(g)
    assert rep["repaired"] == ["a.sav"] and rep["ok"] == 1
    assert not bad.exists(), "la copia rovinata resta tra le versioni"
    assert blob(tmp_path, "a.sav").read_bytes() == b"slot a"
    assert eng.verify_cloud(g)["ok"] == 2


def test_missing_blob_repaired_from_local_backup(tmp_path):
    """Il file qui è già cambiato: la copia giusta è nei backup locali."""
    eng, g = setup(tmp_path)
    eng._backup_local(g, "a.sav")
    Path(g.path, "a.sav").write_bytes(b"modificato, non ancora sincronizzato")
    blob(tmp_path, "a.sav").unlink()
    assert eng.verify_cloud(g)["repaired"] == ["a.sav"]
    assert blob(tmp_path, "a.sav").read_bytes() == b"slot a"


def test_repaired_from_snapshot(tmp_path):
    eng, g = setup(tmp_path)
    eng.snapshot(g, "test")
    Path(g.path, "a.sav").write_bytes(b"altro")
    blob(tmp_path, "a.sav").unlink()
    assert eng.verify_cloud(g)["repaired"] == ["a.sav"]
    assert blob(tmp_path, "a.sav").read_bytes() == b"slot a"


def test_unrepairable_is_reported(tmp_path):
    eng, g = setup(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "app/snapshots")  # nessuna copia da nessuna parte
    Path(g.path, "a.sav").write_bytes(b"altro")
    blob(tmp_path, "a.sav").write_bytes(b"rovinato")
    rep = eng.verify_cloud(g)
    assert rep["broken"] == [("a.sav", "è rovinato")] and rep["ok"] == 1


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


def test_verify_with_encryption(tmp_path):
    eng, g = setup(tmp_path, encrypt=True)
    assert eng.verify_cloud(g)["ok"] == 2
    keys = [k for k in eng.backend.list("g/blobs/a.sav/")]
    (tmp_path / "cloud" / keys[0]).write_bytes(b"SSE1" + os.urandom(40))
    assert eng.verify_cloud(g)["repaired"] == ["a.sav"]
    assert eng.verify_cloud(g)["ok"] == 2


def test_service_verifies_periodically(tmp_path, monkeypatch):
    import time
    from resosync import service
    from resosync.config import Config
    eng, g = setup(tmp_path)
    cfg = Config(app_dir=eng.app_dir)
    cfg.settings = eng.settings
    cfg.games.append(g)
    assert eng.verify_due(g, 7) and not eng.verify_due(g, 0)
    blob(tmp_path, "a.sav").write_bytes(b"rovinato")
    monkeypatch.setattr(service, "VERIFY_EVERY", 0.5)
    events = []
    svc = service.SyncService(cfg, eng, lambda *a: events.append(a))
    svc.start()
    try:
        end = time.time() + 8
        while time.time() < end and not eng._verified().get("g"):
            time.sleep(0.2)
    finally:
        svc.stop()
    assert blob(tmp_path, "a.sav").read_bytes() == b"slot a"
    assert not eng.verify_due(g, 7)  # fatto: la prossima tra una settimana
    assert any(e[0] == "log" and "1 riparati" in e[1] for e in events)
