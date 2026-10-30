"""Funzioni della 0.4.0: istantanee, presenza, solo backup, spazio, anteprime, aggiornamenti."""
import io
import json
import os
import time
from pathlib import Path

import pytest

from resosync import gamedb, service, updater
from resosync.backends import FolderBackend
from resosync.config import Config, Game, Settings
from resosync.engine import SyncEngine
from resosync.snapshots import SnapshotStore


def pc(tmp_path, name, **settings):
    s = Settings(device_name=name, **settings)
    eng = SyncEngine(s, FolderBackend(tmp_path / "cloud"), tmp_path / f"app-{name}",
                     log=lambda m: None)
    game = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(game.path).mkdir(parents=True, exist_ok=True)
    return eng, game


def write(game, rel, data, mtime=None):
    p = Path(game.path, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))


# ---------- istantanee ----------
def test_snapshots_dedupe_and_restore(tmp_path):
    eng, g = pc(tmp_path, "A", snapshots_keep=3)
    write(g, "slot1/sav.dat", b"A" * 1000)
    write(g, "slot1/screenshot.png", b"img")
    first = eng.snapshot(g, "avvio del gioco")
    assert first and eng.snapshot(g, "di nuovo") is None  # identica: non la rifà
    write(g, "slot1/sav.dat", b"rotto")  # sessione andata male
    write(g, "slot2/sav.dat", b"nuovo")
    second = eng.snapshot(g, "dopo")
    store = SnapshotStore(eng.app_dir, "g")
    assert [s["id"] for s in store.list()] == [second, first]
    # screenshot uguale nelle due istantanee: un solo oggetto su disco
    objects = [p for p in (store.objects).rglob("*") if p.is_file()]
    assert len(objects) == 4  # A*1000, img, rotto, nuovo
    n = store.restore(g.path, first)
    assert n == 1 and Path(g.path, "slot1/sav.dat").read_bytes() == b"A" * 1000
    assert Path(g.path, "slot2/sav.dat").exists()  # i salvataggi nati dopo restano
    assert Path(g.path, "slot1/sav.dat").stat().st_mtime > time.time() - 60  # vince al sync


def test_snapshots_pruned_and_gc(tmp_path):
    eng, g = pc(tmp_path, "A", snapshots_keep=2)
    for i in range(5):
        write(g, "s.sav", b"v%d" % i)
        eng.snapshot(g, f"n{i}")
    store = SnapshotStore(eng.app_dir, "g")
    assert len(store.list()) == 2
    contents = sorted(p.read_bytes() for p in store.objects.rglob("*") if p.is_file())
    assert contents == [b"v3", b"v4"]


def test_restore_version_takes_snapshot_first(tmp_path):
    eng, g = pc(tmp_path, "A")
    write(g, "s.sav", b"buono", 1000)
    eng.sync_game(g)
    write(g, "s.sav", b"altro", 2000)
    eng.sync_game(g)
    v = next(v for v in eng.list_versions(g, "s.sav")
             if v[1].startswith("cloud") and Path(tmp_path, "cloud", v[2]).read_bytes() == b"buono")
    eng.restore_version(g, "s.sav", v[1], v[2])
    snaps = SnapshotStore(eng.app_dir, "g").list()
    assert snaps and "prima di ripristinare" in snaps[0]["reason"]


# ---------- solo backup ----------
def test_backup_mode_never_downloads(tmp_path):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    ga.mode = gb.mode = "backup"
    write(ga, "s.sav", b"da A", 1000)
    assert a.sync_game(ga)["up"] == 1
    assert b.sync_game(gb) == {"up": 0, "down": 0, "conflicts": 0, "deleted": 0, "errors": 0}
    assert not Path(gb.path, "s.sav").exists()  # non scarica mai
    write(gb, "s.sav", b"da B", 2000)
    assert b.sync_game(gb)["up"] == 1
    assert a.sync_game(ga)["down"] == 0
    assert Path(ga.path, "s.sav").read_bytes() == b"da A"
    # entrambe le versioni restano recuperabili sul cloud
    blobs = sorted(Path(tmp_path, "cloud", k).read_bytes() for k in a.backend.list("g/blobs/"))
    assert blobs == [b"da A", b"da B"]
    # cancellare in locale non cancella il cloud e non riscarica
    Path(ga.path, "s.sav").unlink()
    a.sync_game(ga)
    assert not Path(ga.path, "s.sav").exists() and a.backend.list("g/blobs/")


# ---------- presenza ----------
def test_presence(tmp_path):
    a, ga = pc(tmp_path, "PC-Studio")
    b, gb = pc(tmp_path, "Steam Deck")
    a.set_playing(ga, True)
    assert b.others_playing(gb) == ["PC-Studio"]
    assert a.others_playing(ga) == []
    a.set_playing(ga, False)
    assert b.others_playing(gb) == []
    # presenza "scaduta" (PC spento senza chiudere): ignorata
    b.backend.write_bytes("g/playing/vecchio.json",
                          json.dumps({"device": "vecchio", "time": time.time() - 3600}).encode())
    assert b.others_playing(gb) == []


def test_service_snapshot_and_warning_on_game_start(tmp_path, monkeypatch):
    procs = []
    monkeypatch.setattr(service, "running_processes", lambda with_paths=True: list(procs))
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.device_name = "PC"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "s.sav").write_text("x")
    cfg.games.append(Game("g", "G", str(saves), process="game.exe"))
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: None)
    other, og = pc(tmp_path, "Steam Deck")
    other.set_playing(og, True)
    events = []
    svc = service.SyncService(cfg, eng, lambda *a: events.append(a))
    try:
        svc.start()
        procs.append(("game", ""))
        end = time.time() + 10
        while time.time() < end and not (any(e[0] == "notify" for e in events)
                                         and SnapshotStore(cfg.app_dir, "g").list()):
            time.sleep(0.1)  # l'istantanea si fa in un thread a parte
        assert eng.others_playing  # il PC ha segnalato la sua presenza
        assert other.others_playing(og) == ["PC"]
    finally:
        svc.stop()
    assert any(e[0] == "notify" and "Steam Deck" in e[1] for e in events)
    # la cartella è fotografata prima di giocare (se è identica all'istantanea della prima
    # sincronizzazione, quella dell'avvio non si duplica)
    assert {x["reason"] for x in SnapshotStore(cfg.app_dir, "g").list()} & {
        "avvio del gioco", "prima della prima sincronizzazione"}
    assert other.others_playing(og) == []  # chiudendo il servizio la presenza sparisce


# ---------- spazio ----------
def test_space_limit_and_cleanup(tmp_path, monkeypatch):
    import resosync.engine as em
    monkeypatch.setattr(em, "GRACE", 0)  # qui si prova la pulizia, non la protezione
    a, g = pc(tmp_path, "A")
    for i in range(6):
        write(g, "s.sav", bytes([i]) * 400_000, 1000 + i)
        a.sync_game(g)
    usage = a.cloud_usage(g)
    assert usage["current"] == 400_000 and usage["n_versions"] == 5
    g.max_cloud_mb = 1  # 1 MB: ci stanno l'attuale + 1 versione
    freed = a.enforce_space(g)
    assert freed == 4 * 400_000
    usage = a.cloud_usage(g)
    assert usage["n_versions"] == 1 and usage["current"] + usage["versions"] <= 1024 * 1024
    assert Path(g.path, "s.sav").read_bytes() == bytes([5]) * 400_000
    assert a.cleanup_versions(g, keep=0) == 400_000
    assert a.cloud_usage(g)["n_versions"] == 0
    # il salvataggio attuale è intatto e si scarica ancora
    b, gb = pc(tmp_path, "B")
    b.sync_game(gb)
    assert Path(gb.path, "s.sav").read_bytes() == bytes([5]) * 400_000


def test_history(tmp_path):
    a, g = pc(tmp_path, "A")
    write(g, "s.sav", b"1")
    a.sync_game(g)
    a.sync_game(g)  # niente da fare: non va in cronologia
    h = a.history(g)
    assert len(h) == 1 and h[0]["up"] == 1 and h[0]["uploaded"] == ["s.sav"]


# ---------- anteprima ----------
def png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_preview_finds_screenshot_and_metadata(tmp_path):
    a, g = pc(tmp_path, "A")
    img = png_bytes()
    write(g, "ManualSave-1/sav.dat", b"\x00binario")
    write(g, "ManualSave-1/screenshot.png", img)
    write(g, "ManualSave-1/metadata.9.json", json.dumps(
        {"Data": {"metadata": {"name": "ManualSave-1", "level": 25.0, "locationName": "Watson",
                               "isModded": False}}}).encode())
    a.sync_game(g)
    v = next(v for v in a.list_versions(g, "ManualSave-1/sav.dat") if v[1].startswith("cloud"))
    pv = a.preview(g, "ManualSave-1/sav.dat", v[1], v[2])
    assert pv["image"] == img and pv["image_from"] == "screenshot.png"
    info = dict(pv["info"])
    assert info["level"] == 25.0 and info["locationName"] == "Watson" and "isModded" not in info


# ---------- cloud dello store ----------
def test_store_cloud_in_db():
    db = {"games": gamedb.compact({
        "Cyberpunk 2077": {"files": {"<home>/x": {}}, "cloud": {"steam": True, "gog": True}},
        "Senza cloud": {"files": {"<home>/y": {}}}})}
    assert gamedb.store_cloud(db, "Cyberpunk 2077") == ["GOG", "Steam"]
    assert gamedb.store_cloud(db, "Senza cloud") == []
    assert gamedb.store_cloud(db, "Inesistente") == []


def test_old_db_cache_is_refreshed(tmp_path, monkeypatch):
    (tmp_path / "gamedb.json").write_text(json.dumps({"fetched": time.time(), "etag": "x",
                                                      "games": {}}))
    calls = []

    class R:
        status_code = 200
        content = b"Gioco:\n  files:\n    <home>/a: {}\n  cloud:\n    steam: true\n"
        headers = {"ETag": "y"}

        def raise_for_status(self):
            pass
    import requests
    monkeypatch.setattr(requests, "get", lambda url, headers, timeout: calls.append(headers) or R())
    db = gamedb.update(tmp_path)
    assert calls and "If-None-Match" not in calls[0]  # cache vecchia: niente ETag
    assert db["v"] == gamedb.DB_FORMAT and gamedb.store_cloud(db, "Gioco") == ["Steam"]


# ---------- aggiornamenti ----------
class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(self.status_code)


def test_updater(monkeypatch):
    assert updater.parse_version("v0.10.0") > updater.parse_version("0.9.3")
    assert updater.parse_version("0.4") == (0, 4, 0)
    release = {"tag_name": "v0.5.0", "html_url": "https://x/rel", "body": "note",
               "assets": [{"name": "ResoSync-portable.zip", "url": "u1"},
                          {"name": "ResoSync-Setup-0.5.0.exe", "url": "u2"}]}
    seen = {}

    def get(url, headers, timeout):
        seen.update(headers)
        return FakeResp(200, release)
    monkeypatch.setattr(updater.requests, "get", get)
    u = updater.check("o/r", token="tok", current="0.4.0")
    assert u.version == "0.5.0" and u.installer_url == "u2"
    assert seen["Authorization"] == "Bearer tok"
    assert updater.check("o/r", current="0.5.0") is None
    monkeypatch.setattr(updater.requests, "get", lambda url, headers, timeout: FakeResp(404, {}))
    with pytest.raises(PermissionError, match="token"):
        updater.check("o/r")


def test_folder_backend_sizes(tmp_path):
    b = FolderBackend(tmp_path / "c")
    b.write_bytes("g/blobs/a/1", b"12345")
    b.write_bytes("g/blobs/b/1", b"1")
    b.write_bytes("h/x", b"1")
    assert b.sizes("g/") == {"g/blobs/a/1": 5, "g/blobs/b/1": 1}
    assert b.list("g/blobs/a/") == ["g/blobs/a/1"]


def test_snapshots_same_millisecond_never_overwrite(tmp_path, monkeypatch):
    import resosync.snapshots as snapmod
    eng, g = pc(tmp_path, "A", snapshots_keep=10)
    monkeypatch.setattr(snapmod.time, "time", lambda: 1_800_000_000.123)  # orologio fermo
    for i in range(3):
        write(g, "s.sav", b"v%d" % i)
        eng.snapshot(g, f"n{i}")
    store = SnapshotStore(eng.app_dir, "g")
    assert len(store.list()) == 3
    restored = []
    for s in store.list():
        store.restore(g.path, s["id"])
        restored.append(Path(g.path, "s.sav").read_bytes())
    assert sorted(restored) == [b"v0", b"v1", b"v2"]
