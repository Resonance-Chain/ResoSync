"""Steam Deck / Linux: servizio senza finestre, launcher silenzioso, systemd."""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from resosync import config as config_mod
from resosync import headless, launcher, system
from resosync.config import Config, Game


def use_app_dir(monkeypatch, app_dir):
    """Config.load() senza argomenti (come nell'app vera) deve leggere la cartella del test."""
    original = Config.load.__func__
    monkeypatch.setattr(config_mod, "APP_DIR", app_dir)
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls, d=app_dir: original(cls, d)))


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    use_app_dir(monkeypatch, tmp_path / "app")
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.folder_path = str(tmp_path / "cloud")
    cfg.settings.device_name = "Steam Deck"
    cfg.settings.debounce_seconds = 1
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "Gioco", str(saves)))
    cfg.save()
    return cfg, saves


def wait_until(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def blobs(tmp_path):
    root = tmp_path / "cloud" / "g" / "blobs"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_headless_service_syncs_and_reloads_config(app_home, tmp_path):
    cfg, saves = app_home
    (saves / "slot.sav").write_text("partita")
    stop = threading.Event()
    t = threading.Thread(target=headless.run, args=(stop,), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: len(blobs(tmp_path)) == 1)
        # la finestra aggiunge un gioco: il servizio lo prende senza riavvio
        saves2 = tmp_path / "saves2"
        saves2.mkdir()
        (saves2 / "a.sav").write_text("x")
        cfg2 = Config.load(cfg.app_dir)
        cfg2.games.append(Game("h", "Altro", str(saves2)))
        time.sleep(1.1)  # data di modifica diversa
        cfg2.save()
        assert wait_until(lambda: (tmp_path / "cloud/h/blobs").exists())
    finally:
        stop.set()
        t.join(10)
    log = (cfg.app_dir / "resosync.log").read_text(encoding="utf-8")
    assert "Servizio avviato" in log and "Configurazione ricaricata" in log


def test_headless_not_configured(tmp_path, monkeypatch):
    use_app_dir(monkeypatch, tmp_path / "vuota")
    assert headless.run(threading.Event()) == 2


def test_quiet_launcher_full_cycle(app_home, tmp_path):
    """Come da Steam in modalità gioco: niente finestre, sync prima e dopo il gioco."""
    cfg, saves = app_home
    game_cmd = [sys.executable, "-c",
                f"open({str(saves / 'slot.sav')!r}, 'w').write('nuova partita')"]
    code = launcher.run("gioco", game_cmd, quiet=True)
    assert code == 0
    assert len(blobs(tmp_path)) == 1
    log = (cfg.app_dir / "launcher.log").read_text(encoding="utf-8")
    assert "avvio" in log and "chiuso" in log


def test_quiet_launcher_unknown_game_still_plays(app_home, tmp_path):
    marker = tmp_path / "giocato"
    code = launcher.run("non-esiste", [sys.executable, "-c", f"open({str(marker)!r},'w')"],
                        quiet=True)
    assert code == 0 and marker.exists()  # mai impedire di giocare


def test_quiet_launcher_cloud_down_still_plays(app_home, tmp_path):
    cfg, saves = app_home
    cfg.settings.folder_path = str(tmp_path / "non-montato" / "ResoSync")
    cfg.save()
    marker = tmp_path / "giocato"
    code = launcher.run("g", [sys.executable, "-c", f"open({str(marker)!r},'w')"], quiet=True)
    assert code == 0 and marker.exists()


def test_systemd_unit(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(system, "SYSTEM", "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(system, "_systemctl", lambda *a: calls.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    system.set_background_service(True)
    unit = (tmp_path / "systemd/user/resosync.service").read_text()
    assert "--service" in unit and "WantedBy=default.target" in unit
    assert ("enable", "--now", "resosync.service") in calls
    assert system.service_installed()
    system.set_background_service(False)
    assert not system.service_installed() and ("disable", "--now", "resosync.service") in calls


def test_game_mode_detection(monkeypatch):
    monkeypatch.setattr(system, "SYSTEM", "Linux")
    for k in ("XDG_CURRENT_DESKTOP", "GAMESCOPE_WAYLAND_DISPLAY", "KDE_FULL_SESSION",
              "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    assert system.in_game_mode()  # nessuno schermo
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("KDE_FULL_SESSION", "true")
    assert not system.in_game_mode()  # modalità desktop
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    assert system.in_game_mode()
    assert "--quiet" in system.steam_launch_options("g")
    monkeypatch.setattr(system, "SYSTEM", "Windows")
    assert not system.in_game_mode() and "--quiet" not in system.steam_launch_options("g")


def test_main_version():
    out = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"),
                          "--version"], capture_output=True, text=True, timeout=60).stdout
    assert out.startswith("ResoSync ")
