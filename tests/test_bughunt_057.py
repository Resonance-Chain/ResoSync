"""Caccia ai bug per la 0.5.7."""
import pytest

from resosync.backends import GoogleDriveBackend
from test_oauth_http import FakeDrive, FakeOAuth


def drive_pair():
    server = FakeDrive()
    a, b = GoogleDriveBackend(FakeOAuth()), GoogleDriveBackend(FakeOAuth())
    a.http = b.http = server
    return server, a, b


# 1. Google Drive permette due file con lo stesso nome. Due PC che creano lo stesso file nello
#    stesso momento (prima sync, lock) non devono finire a usare due copie diverse: non si
#    vedrebbero più (e il lock sarebbe di tutti e due)
def test_gdrive_same_name_created_twice_converges(tmp_path):
    server, a, b = drive_pair()
    a.write_bytes("g/lock.json", b"token A")
    b._create("g/lock.json", b"token B")  # B aveva cercato prima che A lo creasse
    names = [f[0] for f in server.files.values()]
    assert names.count("g/lock.json") == 1, "due copie dello stesso file"
    assert a.read_bytes("g/lock.json") == b.read_bytes("g/lock.json") == b"token B"
    a.write_bytes("g/lock.json", b"token A2")
    assert b.read_bytes("g/lock.json") == b"token A2"


def test_gdrive_listing_picks_the_oldest_duplicate():
    """Duplicati già presenti (versioni vecchie): elenco e letture usano sempre lo stesso."""
    server, a, b = drive_pair()
    a.write_bytes("g/manifest.json", b"primo")
    server.files["id99"] = ["g/manifest.json", b"duplicato"]  # creato dopo
    assert a.list("g/") == ["g/manifest.json"]
    b.sizes("g/")  # il listing riempie la cache degli ID
    assert b.read_bytes("g/manifest.json") == b"primo"


# 2. gioco rimosso e riaggiunto mentre il cloud non è raggiungibile (motore non ancora
#    collegato): lo stato vecchio deve sparire lo stesso, sennò i file che c'erano allora e ora
#    mancano sembrano cancellati (e con "propaga cancellazioni" spariscono dal cloud)
def test_readded_game_resets_state_even_offline(tmp_path, monkeypatch):
    import types
    gui = pytest.importorskip("resosync.gui")
    from resosync.config import Config, Game
    cfg = Config(app_dir=tmp_path / "app")
    state = tmp_path / "app/state/g.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"base": {"vecchio.sav": "abc"}}')
    app = types.SimpleNamespace(cfg=cfg, engine=None, service=None, _refresh=lambda: None)
    assert gui.App._save_game(app, None, Game("g", "G", str(tmp_path / "saves")), quiet=True)
    assert not state.exists()
