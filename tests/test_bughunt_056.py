"""Caccia ai bug per la 0.5.6."""
import json
import threading
import time
from pathlib import Path

import pytest

import resosync.engine as em
from resosync import service
from resosync.backends import FolderBackend
from resosync.config import Config, Game, Settings
from resosync.engine import SyncEngine


def pc(tmp_path, name):
    eng = SyncEngine(Settings(device_name=name), FolderBackend(tmp_path / "cloud"),
                     tmp_path / f"app-{name}", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


def blob_of(tmp_path, rel):
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]
    return tmp_path / "cloud" / m[rel]["blob"]


# 1. verifica con una cartella cloud in ritardo: il salvataggio appena caricato da un altro PC
#    può non essere ancora arrivato (o essere a metà). Non è "rovinato": niente falsi allarmi
def test_verify_fresh_blob_still_arriving_is_not_broken(tmp_path):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    Path(ga.path, "s.sav").write_bytes(b"vecchio")
    a.sync_game(ga)
    b.sync_game(gb)
    Path(ga.path, "s.sav").write_bytes(b"nuovo, appena caricato da A")
    a.sync_game(ga)
    blob = blob_of(tmp_path, "s.sav")
    blob.write_bytes(b"nuovo, app")  # B lo vede ancora a metà
    rep = b.verify_cloud(gb)
    assert rep["broken"] == [] and rep["repaired"] == []
    assert rep["arriving"] == ["s.sav"]
    assert blob.read_bytes() == b"nuovo, app"  # non toccato


def test_verify_old_broken_blob_still_reported(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "A")
    Path(ga.path, "s.sav").write_bytes(b"x")
    a.sync_game(ga)
    Path(ga.path, "s.sav").write_bytes(b"altro")
    import shutil
    shutil.rmtree(tmp_path / "app-A/snapshots")
    blob_of(tmp_path, "s.sav").write_bytes(b"rovinato")
    monkeypatch.setattr(em, "GRACE", 0)  # caricato da più di 2 ore
    assert [r for r, _ in a.verify_cloud(ga)["broken"]] == ["s.sav"]


# 2. la verifica (lunga con salvataggi grandi) non deve bloccare il servizio: intanto deve
#    accorgersi che apri un gioco
def test_verify_does_not_block_game_detection(tmp_path, monkeypatch):
    procs = []
    monkeypatch.setattr(service, "running_processes", lambda with_paths=True: list(procs))
    monkeypatch.setattr(service, "VERIFY_EVERY", 0.3)
    eng, g = pc(tmp_path, "A")
    g.process = "game.exe"
    Path(g.path, "s.sav").write_bytes(b"x")
    eng.sync_game(g)
    cfg = Config(app_dir=eng.app_dir)
    cfg.settings = eng.settings
    cfg.games.append(g)
    slow = threading.Event()
    real = eng.backend.download_file

    def slow_download(key, path):
        slow.set()
        time.sleep(6)  # salvataggio da qualche GB
        return real(key, path)
    monkeypatch.setattr(eng.backend, "download_file", slow_download)
    events = []
    svc = service.SyncService(cfg, eng, lambda *a: events.append(a))
    svc.start()
    try:
        assert slow.wait(5), "la verifica non è partita"
        procs.append(("game", ""))
        end = time.time() + 4
        while time.time() < end and not svc.is_playing("g"):
            time.sleep(0.1)
        assert svc.is_playing("g"), "durante la verifica non si accorge del gioco aperto"
    finally:
        procs.clear()
        svc.stop(wait=10)


# 3. anteprima della prima sync per un gioco che salva nel registro: il conflitto sul registro
#    (giocato su tutti e due i PC prima di ResoSync) deve comparire nell'anteprima
def test_first_sync_plan_includes_registry(tmp_path):
    from test_registry import KEY, FakeRegistry
    regs = {}
    engines = {}
    for name in ("PC", "Nuovo"):
        regs[name] = FakeRegistry()
        engines[name] = SyncEngine(Settings(device_name=name), FolderBackend(tmp_path / "cloud"),
                                   tmp_path / f"app-{name}", log=lambda m: None,
                                   registry=regs[name])
    regs["PC"].set(KEY, "Progress", "dal PC")
    gp = Game("g", "G", str(tmp_path / "pc"), registry=[KEY])
    engines["PC"].sync_game(gp)
    regs["Nuovo"].set(KEY, "Progress", "dal PC nuovo")
    gn = Game("g", "G", str(tmp_path / "nuovo"), registry=[KEY])
    plan = engines["Nuovo"].first_sync_plan(gn)
    assert plan is not None
    assert [c["name"] for c in plan["conflicts"]] == [".resosync-registry/registry.json"]
