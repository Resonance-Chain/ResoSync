"""Anteprima della prima sincronizzazione su un PC nuovo (o su un cloud nuovo)."""
import os
import time
from pathlib import Path

from resosync import service
from resosync.backends import FolderBackend
from resosync.config import Config, Game, Settings
from resosync.dialogs import first_sync_text
from resosync.engine import SyncEngine
from resosync.snapshots import SnapshotStore


def setup(tmp_path):
    """Il PC ha già caricato due slot; il PC nuovo ne ha uno uguale, uno diverso e uno suo."""
    pc = SyncEngine(Settings(device_name="PC"), FolderBackend(tmp_path / "cloud"),
                    tmp_path / "pc", log=lambda m: None)
    gp = Game("g", "G", str(tmp_path / "pc-saves"))
    Path(gp.path).mkdir()
    for name, data, t in (("uguale.sav", b"x", 1000), ("diverso.sav", b"dal PC", 2000),
                          ("solo-cloud.sav", b"c", 1000)):
        Path(gp.path, name).write_bytes(data)
        os.utime(Path(gp.path, name), (t, t))
    pc.sync_game(gp)
    new = SyncEngine(Settings(device_name="Deck"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "deck", log=lambda m: None)
    gn = Game("g", "G", str(tmp_path / "deck-saves"))
    Path(gn.path).mkdir()
    for name, data, t in (("uguale.sav", b"x", 1000), ("diverso.sav", b"dal Deck", 3000),
                          ("solo-qui.sav", b"q", 1000)):
        Path(gn.path, name).write_bytes(data)
        os.utime(Path(gn.path, name), (t, t))
    return new, gn


def test_plan_lists_conflicts_and_matches_real_sync(tmp_path):
    new, gn = setup(tmp_path)
    plan = new.first_sync_plan(gn)
    assert plan["down"] == ["solo-cloud.sav"] and plan["up"] == ["solo-qui.sav"]
    assert [(c["name"], c["winner"]) for c in plan["conflicts"]] == [("diverso.sav", "local")]
    assert Path(gn.path, "diverso.sav").read_bytes() == b"dal Deck"  # non ha toccato niente
    new.sync_game(gn)  # la sync vera fa quello che il piano diceva
    assert Path(gn.path, "diverso.sav").read_bytes() == b"dal Deck"
    assert Path(gn.path, "solo-cloud.sav").exists()
    assert new.first_sync_plan(gn) is None  # già sincronizzato


def test_no_plan_without_conflicts(tmp_path):
    new, gn = setup(tmp_path)
    Path(gn.path, "diverso.sav").unlink()
    assert new.first_sync_plan(gn) is None


def test_snapshot_before_first_sync(tmp_path):
    new, gn = setup(tmp_path)
    new.sync_game(gn)
    snaps = SnapshotStore(new.app_dir, "g").list()
    assert [s["reason"] for s in snaps] == ["prima della prima sincronizzazione"]
    new.sync_game(gn)
    assert len(SnapshotStore(new.app_dir, "g").list()) == 1  # solo la prima volta


def test_service_waits_for_confirmation(tmp_path):
    new, gn = setup(tmp_path)
    cfg = Config(app_dir=new.app_dir)
    cfg.games.append(gn)
    events = []
    svc = service.SyncService(cfg, new, lambda *a: events.append(a))
    svc.start()
    try:
        end = time.time() + 5
        while time.time() < end and not any(e[0] == "confirm_first" for e in events):
            time.sleep(0.1)
        time.sleep(1)
        assert any(e[0] == "confirm_first" for e in events)
        assert not Path(gn.path, "solo-cloud.sav").exists(), "ha sincronizzato senza chiedere"
        assert sum(e[0] == "confirm_first" for e in events) == 1  # chiesto una volta sola
        svc.confirm_first_sync("g")
        end = time.time() + 5
        while time.time() < end and not Path(gn.path, "solo-cloud.sav").exists():
            time.sleep(0.1)
        assert Path(gn.path, "solo-cloud.sav").exists()
    finally:
        svc.stop()


def test_plan_text():
    text = first_sync_text("Elden Ring", {"down": ["a"], "up": [], "conflicts": [
        {"name": "ER0000.sl2", "winner": "cloud", "local_time": 1000, "remote_time": 2000,
         "device": "PC"}]})
    assert "ER0000.sl2" in text and "CLOUD (PC" in text and "1 da scaricare" in text
