import sys
import time

from savesync import launcher, service
from savesync.backends import FolderBackend
from savesync.config import Config, Game
from savesync.engine import SyncEngine


def wait_until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_sync_only_after_game_exit(tmp_path, monkeypatch):
    running = set()
    monkeypatch.setattr(service, "running_process_names", lambda: set(running))
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.debounce_seconds = 0
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "G", str(saves), process="Game.exe"))
    events = []
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: events.append(m))
    svc = service.SyncService(cfg, eng, lambda *a: events.append(a))
    blob_dir = tmp_path / "cloud/g/blobs/slot.sav"
    uploaded = lambda: blob_dir.exists() and any(blob_dir.iterdir())
    try:
        svc.start()
        running.add("game")
        assert wait_until(lambda: svc.is_playing("g"))
        time.sleep(1.5)  # lascio finire la sync d'avvio
        (saves / "slot.sav").write_text("in partita")
        svc.notify("g")
        time.sleep(2)
        assert not uploaded(), "non deve caricare mentre il gioco è aperto"
        running.clear()
        assert wait_until(uploaded), "deve caricare appena il gioco si chiude"
    finally:
        svc.stop()
    assert any("gioco chiuso" in str(e) for e in events)


class FakeUI:
    root = None

    def sleep(self, s):
        time.sleep(min(s, 0.05))


def test_launcher_waits_for_real_game_process(monkeypatch):
    # il "launcher" esce subito, poi il gioco vero gira per un po'
    state = {"calls": 0}

    def fake_running():
        state["calls"] += 1
        return {"eldenring"} if 3 <= state["calls"] < 8 else set()
    monkeypatch.setattr(launcher, "running_process_names", fake_running)
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    launcher._wait_game(FakeUI(), Game("g", "G", ".", process="eldenring.exe"), proc)
    assert state["calls"] >= 8
