"""Salvataggi nel registro di Windows: esportati in un file che viaggia come gli altri."""
import copy
import json
import os
import uuid
from pathlib import Path

import pytest

from resosync import gamedb, registry
from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.engine import SyncEngine

KEY = "HKEY_CURRENT_USER/Software/Studio/Gioco"


class FakeRegistry:
    """Come il registro vero: scrivere crea chiavi e valori, non cancella niente."""

    def __init__(self):
        self.data = {}

    def read(self, key):
        return copy.deepcopy(self.data.get(key.lower()))

    def exists(self, key):
        return key.lower() in self.data

    def mtime(self, key):
        return 1000.0

    def write(self, key, tree):
        def merge(dst, src):
            dst.setdefault("values", {}).update(copy.deepcopy(src.get("values", {})))
            for sub, t in src.get("keys", {}).items():
                merge(dst.setdefault("keys", {}).setdefault(sub, {"values": {}, "keys": {}}), t)
        merge(self.data.setdefault(key.lower(), {"values": {}, "keys": {}}), tree)

    def set(self, key, name, value, kind=1):
        self.write(key, {"values": {name: [kind, registry._enc(value)]}, "keys": {}})

    def get(self, key, name):
        v = (self.data.get(key.lower()) or {}).get("values", {}).get(name)
        return registry._dec(v[1]) if v else None


def device(tmp_path, name, **game_kw):
    reg = FakeRegistry()
    eng = SyncEngine(Settings(device_name=name), FolderBackend(tmp_path / "cloud"),
                     tmp_path / f"app-{name}", log=lambda m: None, registry=reg)
    g = Game("g", "Gioco", str(tmp_path / f"saves-{name}"), registry=[KEY], **game_kw)
    return eng, g, reg


def test_registry_save_travels_between_pcs(tmp_path):
    a, ga, ra = device(tmp_path, "A")
    b, gb, rb = device(tmp_path, "B")
    ra.set(KEY, "Level", 7, kind=4)              # REG_DWORD
    ra.set(KEY, "Data", b"\x00\x01binario", 3)   # REG_BINARY
    ra.write(KEY, {"values": {}, "keys": {"Slot1": {"values": {"hp": [1, "100"]}, "keys": {}}}})
    a.sync_game(ga)
    b.sync_game(gb)
    assert rb.get(KEY, "Level") == 7 and rb.get(KEY, "Data") == b"\x00\x01binario"
    assert rb.read(KEY)["keys"]["Slot1"]["values"]["hp"] == [1, "100"]
    rb.set(KEY, "Level", 8, kind=4)  # si gioca sul secondo PC
    b.sync_game(gb)
    a.sync_game(ga)
    assert ra.get(KEY, "Level") == 8


def test_registry_only_game_and_filters(tmp_path):
    """Gioco che salva solo nel registro, con un filtro sui file che non comprende il json."""
    a, ga, ra = device(tmp_path, "A", patterns=["*.sav"])
    ra.set(KEY, "Progress", "capitolo 3")
    a.sync_game(ga)  # la cartella non esiste ancora: la crea per il file esportato
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]
    assert list(m) == [".resosync-registry/registry.json"]


def test_restore_version_goes_back_into_registry(tmp_path):
    a, ga, ra = device(tmp_path, "A")
    ra.set(KEY, "Progress", "capitolo 1")
    a.sync_game(ga)
    ra.set(KEY, "Progress", "capitolo 2 (salvataggio rovinato)")
    a.sync_game(ga)
    rel = ".resosync-registry/registry.json"
    old = next(v for v in a.list_versions(ga, rel) if v[1].startswith("cloud") and
               b"capitolo 1" in (tmp_path / "cloud" / v[2]).read_bytes())
    a.restore_version(ga, rel, old[1], old[2])
    a.sync_game(ga)
    assert ra.get(KEY, "Progress") == "capitolo 1"
    a.sync_game(ga)  # e resta così (niente avanti e indietro)
    assert ra.get(KEY, "Progress") == "capitolo 1"


def test_game_saved_in_registry_wins_over_untouched_file(tmp_path):
    a, ga, ra = device(tmp_path, "A")
    ra.set(KEY, "Progress", "1")
    a.sync_game(ga)
    ra.set(KEY, "Progress", "2")
    st = a.sync_game(ga)
    assert st["up"] == 1
    data = json.loads(Path(ga.path, ".resosync-registry/registry.json").read_bytes())
    assert data[KEY]["values"]["Progress"] == [1, "2"]


def test_only_the_games_keys_are_imported(tmp_path):
    """Il file arriva dal cloud: non deve poter scrivere chiavi che non sono del gioco."""
    a, ga, ra = device(tmp_path, "A")
    ra.set(KEY, "Progress", "1")
    a.sync_game(ga)
    f = Path(ga.path, ".resosync-registry/registry.json")
    data = json.loads(f.read_bytes())
    data["HKEY_CURRENT_USER/Software/Microsoft/Windows/CurrentVersion/Run"] = {
        "values": {"malware": [1, "c:/evil.exe"]}, "keys": {}}
    data[KEY]["values"]["Progress"] = [1, "2"]
    f.write_bytes(json.dumps(data).encode())
    a.sync_game(ga)
    assert ra.get(KEY, "Progress") == "2"
    assert not ra.exists("HKEY_CURRENT_USER/Software/Microsoft/Windows/CurrentVersion/Run")


def test_without_registry_the_file_is_just_kept(tmp_path):
    """Steam Deck / Linux: niente registro di Windows, il file si sincronizza come copia."""
    a, ga, ra = device(tmp_path, "PC")
    ra.set(KEY, "Progress", "1")
    a.sync_game(ga)
    deck = SyncEngine(Settings(device_name="Deck"), FolderBackend(tmp_path / "cloud"),
                      tmp_path / "deck", log=lambda m: None)
    deck.registry.store = None
    gd = Game("g", "Gioco", str(tmp_path / "deck-saves"), registry=[KEY])
    deck.sync_game(gd)
    assert Path(gd.path, ".resosync-registry/registry.json").exists()


def test_manifest_and_detection(tmp_path, monkeypatch):
    raw = {
        "Solo Registro": {"registry": {KEY: {"tags": ["save"]},
                                       "HKEY_CURRENT_USER/Software/Studio/Config": {
                                           "tags": ["config"]},
                                       "HKEY_LOCAL_MACHINE/Software/Studio": {}}},
        "Niente": {"registry": {"HKEY_CURRENT_USER/Software/Altro": {}}},
    }
    db = {"games": gamedb.compact(raw)}
    assert db["games"]["Solo Registro"]["r"] == [KEY]
    reg = FakeRegistry()
    reg.set(KEY, "x", "1")
    monkeypatch.setattr(gamedb, "steam_libraries", lambda: [])
    monkeypatch.setattr(gamedb, "placeholders", lambda: {"<home>": tmp_path})
    monkeypatch.setattr(gamedb, "installed_games", lambda: {})
    found = gamedb.detect(db, reg_store=reg)
    assert [(d.name, d.folder, d.registry) for d in found] == [("Solo Registro", "", [KEY])]


@pytest.mark.skipif(os.name != "nt", reason="registro vero solo su Windows")
def test_real_windows_registry_roundtrip():
    import winreg
    key = f"HKEY_CURRENT_USER/Software/ResoSyncTest/{uuid.uuid4().hex}"
    store = registry.WinRegistry()
    tree = {"values": {"s": [winreg.REG_SZ, "testo"], "n": [winreg.REG_DWORD, 42],
                       "b": [winreg.REG_BINARY, registry._enc(b"\x00\xff")],
                       "m": [winreg.REG_MULTI_SZ, ["a", "b"]]},
            "keys": {"Sotto": {"values": {"q": [winreg.REG_QWORD, 2 ** 40]}, "keys": {}}}}
    try:
        assert not store.exists(key) and store.read(key) is None
        store.write(key, tree)
        assert store.exists(key) and store.mtime(key) > 0
        assert store.read(key) == tree
    finally:
        sub = key.split("/", 1)[1].replace("/", "\\")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub + "\\Sotto")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)


def test_conflict_decided_by_real_registry_time(tmp_path):
    """Giocato su tutti e due i PC prima di ResoSync: vince il salvataggio fatto più di
    recente nel registro, non il file esportato per ultimo."""
    a, ga, ra = device(tmp_path, "A")
    b, gb, rb = device(tmp_path, "B")
    ra.mtime = lambda k: 2000.0   # A ha salvato dopo
    rb.mtime = lambda k: 1000.0
    ra.set(KEY, "Progress", "A, più avanti")
    rb.set(KEY, "Progress", "B, più indietro")
    b.sync_game(gb)
    a.sync_game(ga)  # prima sync di A: conflitto, vince A (salvato dopo)
    b.sync_game(gb)
    assert ra.get(KEY, "Progress") == rb.get(KEY, "Progress") == "A, più avanti"
    # e quello di B non è perso: è tra le versioni
    rel = ".resosync-registry/registry.json"
    assert any(b"B, pi" in (tmp_path / "cloud" / v[2]).read_bytes()
               for v in b.list_versions(gb, rel) if v[1].startswith("cloud"))
