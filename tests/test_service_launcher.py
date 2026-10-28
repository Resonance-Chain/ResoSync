import os
import sys
import time
from pathlib import Path

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
    monkeypatch.setattr(service, "running_processes",
                        lambda with_paths=True: [(n, "") for n in running])
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

    def fake_running(with_paths=True):
        state["calls"] += 1
        return [("eldenring", "")] if 3 <= state["calls"] < 8 else []
    monkeypatch.setattr(launcher, "running_processes", fake_running)
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    launcher._wait_game(FakeUI(), Game("g", "G", ".", process="eldenring.exe"), proc)
    assert state["calls"] >= 8


def test_game_detected_by_install_folder_vortex_style(tmp_path, monkeypatch):
    """Cyberpunk avviato da Vortex: il database dice redprelauncher.exe, ma gira
    bin/x64/Cyberpunk2077.exe. Basta che l'exe stia nella cartella d'installazione."""
    install = tmp_path / "steamapps" / "common" / "Cyberpunk 2077"
    exe = os.path.normcase(str(install / "bin" / "x64" / "Cyberpunk2077.exe"))
    game = Game("cp", "Cyberpunk 2077", str(tmp_path / "saves"), process="redprelauncher.exe",
                install_dir=str(install))
    assert service.is_game_running(game, [("cyberpunk2077", exe)])
    assert service.is_game_running(game, [("redprelauncher", "")])
    # una cartella col nome che inizia uguale NON conta
    other = os.path.normcase(str(tmp_path / "steamapps" / "common" / "Cyberpunk 2077 Mods" / "x.exe"))
    assert not service.is_game_running(game, [("x", other), ("vortex", "")])
    assert not service.is_game_running(Game("x", "X", "."), [("cyberpunk2077", exe)])


def test_service_uses_install_folder(tmp_path, monkeypatch):
    install = tmp_path / "Cyberpunk 2077"
    exe = os.path.normcase(str(install / "bin" / "x64" / "Cyberpunk2077.exe"))
    procs = []
    monkeypatch.setattr(service, "running_processes", lambda with_paths=True: list(procs))
    cfg = Config(app_dir=tmp_path / "app")
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("cp", "Cyberpunk 2077", str(saves), process="redprelauncher.exe",
                          install_dir=str(install)))
    events = []
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: None)
    svc = service.SyncService(cfg, eng, lambda *a: events.append(a))
    try:
        svc.start()
        procs.append(("cyberpunk2077", exe))
        assert wait_until(lambda: svc.is_playing("cp"))
        procs.clear()
        assert wait_until(lambda: not svc.is_playing("cp"))
    finally:
        svc.stop()
    assert any("gioco avviato" in str(e) for e in events)
    assert any("gioco chiuso" in str(e) for e in events)


def test_running_processes_real():
    """Il vero psutil: deve almeno trovare questo interprete Python col suo percorso."""
    procs = service.running_processes()
    assert procs and all(isinstance(n, str) and isinstance(e, str) for n, e in procs)


def test_protected_process_matched_by_exe_name(tmp_path):
    """Processo di cui Windows non dà il percorso: si riconosce dai nomi degli exe
    nella cartella d'installazione, ma non dai nomi generici (crash handler & co.)."""
    install = tmp_path / "Elden Ring"
    (install / "Game").mkdir(parents=True)
    (install / "Game" / "eldenring.exe").write_text("")
    (install / "Game" / "start_protected_game.exe").write_text("")
    (install / "Game" / "UnityCrashHandler64.exe").write_text("")
    (install / "EasyAntiCheat_EOS_Setup.exe").write_text("")
    game = Game("er", "Elden Ring", ".", process="start_protected_game.exe",
                install_dir=str(install))
    assert service.is_game_running(game, [("eldenring", "")])
    assert not service.is_game_running(game, [("unitycrashhandler64", "")])
    assert not service.is_game_running(game, [("easyanticheat_eos_setup", "")])
    # se il percorso è noto e sta altrove, il solo nome non basta
    elsewhere = os.path.normcase(str(tmp_path / "altro" / "eldenring.exe"))
    assert not service.is_game_running(game, [("eldenring", elsewhere)])


def test_wine_exe_parsing():
    w = service.wine_exe(["Z:\\home\\deck\\.local\\share\\Steam\\steamapps\\common\\"
                          "Cyberpunk 2077\\bin\\x64\\Cyberpunk2077.exe", "--launcher-skip"])
    assert w[0] == "cyberpunk2077"
    assert w[1] == os.path.normcase(os.path.normpath(
        "/home/deck/.local/share/Steam/steamapps/common/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe"))
    assert service.wine_exe(["python3", "/x/proton", "waitforexitandrun",
                             "/home/deck/g/Game.EXE"]) == ("game", os.path.normcase("/home/deck/g/Game.EXE"))
    assert service.wine_exe(["C:\\windows\\system32\\services.exe"]) == ("services", "")
    assert service.wine_exe(["/usr/bin/bash"]) is None and service.wine_exe(None) is None


def test_deck_proton_game_detected_by_install_dir(tmp_path):
    install = "/home/deck/.local/share/Steam/steamapps/common/Cyberpunk 2077"
    game = Game("cp", "Cyberpunk 2077", ".", process="redprelauncher.exe", install_dir=install)
    w = service.wine_exe([
        "Z:\\home\\deck\\.local\\share\\Steam\\steamapps\\common\\Cyberpunk 2077\\bin\\x64\\Cyberpunk2077.exe"])
    procs = [("wine64-preloader", "/home/deck/.steam/proton/files/bin/wine64-preloader"), w]
    if os.name == "nt":
        return  # percorsi Linux: il confronto ha senso solo su Linux/macOS
    assert service.is_game_running(game, procs)


def test_running_processes_skips_own_ancestors():
    """Avviando da Steam, il padre (reaper/proton) ha il percorso del gioco nella riga di
    comando e vive finché vive il launcher: il launcher non deve contarlo come gioco aperto."""
    import subprocess
    repo = str(Path(__file__).resolve().parents[1])
    child = ("import sys; sys.path.insert(0, %r); from savesync import service; "
             "print(any(n == 'fintogiocopadre' for n, _ in service.running_processes()))" % repo)
    parent = ("import subprocess, sys; "
              "r = subprocess.run([sys.executable, '-c', %r], capture_output=True, text=True); "
              "print(r.stdout.strip() or r.stderr)" % child)
    out = subprocess.run([sys.executable, "-c", parent, "Z:\\giochi\\FintoGiocoPadre.exe"],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "False", out
