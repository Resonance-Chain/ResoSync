"""Caccia ai bug dopo la 0.5.0: ogni test riproduce un problema trovato rileggendo il codice."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from resosync import gamedb, service, system
from resosync.backends import FolderBackend
from resosync.config import Config, Game, Settings
from resosync.engine import SyncEngine


def pc(tmp_path, name, cloud="cloud", **kw):
    eng = SyncEngine(Settings(device_name=name, **kw), FolderBackend(tmp_path / cloud),
                     tmp_path / f"app-{name}", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


def write(g, rel, data, mtime=None):
    p = Path(g.path, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))


# A. limite di spazio: non deve cancellare il salvataggio appena caricato da un altro PC
#    che questo PC ancora non vede (cartella cloud in ritardo)
def test_space_limit_spares_fresh_blobs_of_other_devices(tmp_path):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    for i in range(4):
        write(ga, "s.sav", bytes([i]) * 600_000, 1000 + i)
        a.sync_game(ga)
    b.sync_game(gb)
    stale_manifest = (tmp_path / "cloud/g/manifest.json").read_bytes()
    write(gb, "s.sav", b"B" * 600_000, 5000)
    b.sync_game(gb)  # B carica il salvataggio più nuovo
    fresh = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]["s.sav"]
    (tmp_path / "cloud/g/manifest.json").write_bytes(stale_manifest)  # A non lo vede ancora
    ga.max_cloud_mb = 1
    a.enforce_space(ga)
    assert (tmp_path / "cloud" / fresh["blob"]).exists(), "cancellato il salvataggio nuovo di B"


# B. nomi delle versioni: l'ordine deve essere quello vero anche tra PC con fusi diversi
@pytest.mark.skipif(not hasattr(time, "tzset"), reason="serve tzset (Linux/macOS)")
def test_version_order_across_time_zones(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "PC")
    b, gb = pc(tmp_path, "Deck")
    try:
        monkeypatch.setenv("TZ", "Europe/Rome")
        time.tzset()
        write(ga, "s.sav", b"vecchio", 1000)
        a.sync_game(ga)
        b.sync_game(gb)
        time.sleep(1.1)
        monkeypatch.setenv("TZ", "America/New_York")  # Deck con il fuso sbagliato
        time.tzset()
        write(gb, "s.sav", b"nuovo", 2000)
        b.sync_game(gb)
    finally:
        monkeypatch.undo()
        time.tzset()
    newest = a.list_versions(ga, "s.sav")[0]
    assert Path(tmp_path, "cloud", newest[2]).read_bytes() == b"nuovo"


# C. file temporanei rimasti da un crash non sono salvataggi
def test_leftover_temp_files_not_synced(tmp_path):
    a, ga = pc(tmp_path, "A")
    write(ga, "s.sav", b"ok")
    for leftover in ("s.sav.resosync-tmp", "s.sav.savesync-tmp", "x.partial"):
        write(ga, leftover, b"mezzo file")
    a.sync_game(ga)
    uploaded = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]
    assert set(uploaded) == {"s.sav"}


# D. "una sottocartella = un salvataggio" non va dato per scontato: una cartella Saves/
#    con slot indipendenti modificati su due PC non deve perdere le modifiche di nessuno
def test_independent_slots_in_one_folder_both_survive(tmp_path):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    write(ga, "Saves/slot1.sav", b"s1", 1000)
    write(ga, "Saves/slot2.sav", b"s2", 1000)
    a.sync_game(ga)
    b.sync_game(gb)
    ga.group_by = gb.group_by = SyncEngine.suggest_grouping(ga.path)
    write(ga, "Saves/slot1.sav", b"s1 di A", 2000)
    write(gb, "Saves/slot2.sav", b"s2 di B", 3000)
    a.sync_game(ga)
    b.sync_game(gb)
    a.sync_game(ga)
    for g in (ga, gb):
        assert Path(g.path, "Saves/slot1.sav").read_bytes() == b"s1 di A"
        assert Path(g.path, "Saves/slot2.sav").read_bytes() == b"s2 di B"


def test_default_grouping_is_per_file_and_migrated(tmp_path):
    assert Game("g", "G", ".").group_by == "none"
    cp = tmp_path / "cp"
    for slot in ("ManualSave-1", "AutoSave-0"):
        for part in ("sav.dat", "metadata.json"):
            (cp / slot).mkdir(parents=True, exist_ok=True)
            (cp / slot / part).write_text("x")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/config.json").write_text(json.dumps({"settings": {}, "games": [
        {"id": "cp", "name": "Cyberpunk 2077", "path": str(cp)},
        {"id": "x", "name": "X", "path": str(tmp_path / "piatta")}]}))
    cfg = Config.load(tmp_path / "app")  # config della 0.4.0: senza group_by
    assert cfg.get_game("cp").group_by == "folder"
    assert cfg.get_game("x").group_by == "none"


# E. "mentre giochi non carico nulla": i tentativi automatici dopo un errore non devono
#    caricare durante la partita
def test_retries_do_not_sync_while_playing(tmp_path, monkeypatch):
    procs = []
    monkeypatch.setattr(service, "running_processes", lambda with_paths=True: list(procs))
    cfg = Config(app_dir=tmp_path / "app")
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "G", str(saves), process="game.exe"))
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: None)
    calls = []
    monkeypatch.setattr(service, "BUSY_RETRY", 1)
    monkeypatch.setattr(service, "ERROR_RETRY", 1)

    def fake_sync(game, wait_lock=0.0):
        calls.append((time.time(), svc.is_playing("g")))
        return {"up": 0, "down": 0, "conflicts": 0, "deleted": 0, "errors": 1}
    monkeypatch.setattr(eng, "sync_game", fake_sync)
    svc = service.SyncService(cfg, eng, lambda *a: None)
    try:
        svc.start()
        time.sleep(1.5)
        procs.append(("game", ""))
        time.sleep(6)
    finally:
        svc.stop()
    while_playing = [c for c in calls if c[1]]
    assert len(while_playing) <= 1, f"{len(while_playing)} sync durante la partita"


# F. l'istantanea all'avvio del gioco (lenta la prima volta) non deve ritardare il download
def test_snapshot_does_not_delay_start_sync(tmp_path, monkeypatch):
    procs = []
    monkeypatch.setattr(service, "running_processes", lambda with_paths=True: list(procs))
    cfg = Config(app_dir=tmp_path / "app")
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "G", str(saves), process="game.exe"))
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: None)
    monkeypatch.setattr(eng, "snapshot", lambda g, reason: time.sleep(6))
    synced = threading.Event()
    real = eng.sync_game

    def watch(game, wait_lock=0.0):
        if svc.is_playing("g"):
            synced.set()
        return real(game, wait_lock)
    monkeypatch.setattr(eng, "sync_game", watch)
    svc = service.SyncService(cfg, eng, lambda *a: None)
    try:
        svc.start()
        time.sleep(1)
        procs.append(("game", ""))
        assert synced.wait(4.5), "il download all'avvio aspetta l'istantanea"
    finally:
        svc.stop()


# G. Steam Deck: Windows non distingue maiuscole/minuscole, Linux sì. Il database può
#    scrivere "eldenring" dove la cartella vera è "EldenRing"
def test_proton_detection_ignores_case(tmp_path, monkeypatch):
    steam = tmp_path / "Steam"
    pfx = steam / "steamapps/compatdata/1245620/pfx"
    save = pfx / "drive_c/users/steamuser/AppData/Roaming/EldenRing/7656/ER0000.sl2"
    save.parent.mkdir(parents=True)
    save.write_text("x")
    raw = {"Elden Ring": {"files": {"<winAppData>/eldenring/<storeUserId>": {
        "tags": ["save"], "when": [{"os": "windows"}]}}, "steam": {"id": 1245620}}}
    monkeypatch.setattr(gamedb, "SYSTEM", "linux")
    monkeypatch.setattr(gamedb, "steam_libraries", lambda: [steam])
    monkeypatch.setattr(gamedb, "placeholders", lambda: {"<home>": tmp_path / "home"})
    found = gamedb.detect({"games": gamedb.compact(raw)})
    assert [d.name for d in found] == ["Elden Ring"]
    assert Path(found[0].folder) == save.parent  # con le maiuscole vere


def test_file_patterns_ignore_case():
    g = Game("g", "G", ".", patterns=["*.SAV"], excludes=["*.TMP"])
    assert SyncEngine.matches(g, "slot1.sav")
    assert not SyncEngine.matches(g, "slot1.tmp") and not SyncEngine.matches(g, "a.txt")


# H. avviato dalla copia SaveSync.exe (vecchie opzioni Steam / vecchio avvio automatico):
#    i comandi nuovi devono usare ResoSync.exe
def test_app_command_prefers_new_name(tmp_path, monkeypatch):
    exe_name = "SaveSync.exe" if os.name == "nt" else "SaveSync"
    new_name = "ResoSync.exe" if os.name == "nt" else "ResoSync"
    (tmp_path / exe_name).write_text("")
    (tmp_path / new_name).write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / exe_name))
    assert system.app_command() == [str(tmp_path / new_name)]


# I. dopo il cambio di nome, riselezionare la vecchia cartella cloud non deve creare un
#    cloud nuovo e vuoto dentro quella vecchia
def test_reselecting_old_cloud_folder(tmp_path):
    from resosync.config import cloud_folder_for
    proton = tmp_path / "Proton Drive"
    (proton / "SaveSync").mkdir(parents=True)
    (proton / "SaveSync" / "games.json").write_text("{}")
    assert cloud_folder_for(proton) == proton / "SaveSync"            # scelgo la cartella sopra
    assert cloud_folder_for(proton / "SaveSync") == proton / "SaveSync"  # scelgo quella giusta
    nuovo = tmp_path / "OneDrive"
    nuovo.mkdir()
    assert cloud_folder_for(nuovo) == nuovo / "ResoSync"              # cloud nuovo
