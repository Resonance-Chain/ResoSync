"""Caccia ai bug per la 0.5.4."""
import types

import pytest

from resosync.config import Config


class Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


def fake_dialog(cfg, **changes):
    values = dict(vars(cfg.settings), **changes)
    msgs = []
    app = types.SimpleNamespace(cfg=cfg, _start_service=lambda: msgs.append("start"))
    dlg = types.SimpleNamespace(v={k: Var(v) for k, v in values.items()}, app=app,
                                passphrase2=Var(values["passphrase"]),
                                bg_service=Var(False), destroy=lambda: msgs.append("chiusa"))
    return dlg, msgs


# 1. Impostazioni: se scegli un cloud nuovo ma incompleto, la finestra dice "completa la
#    configurazione" e resta aperta. Prima però aveva già cancellato lo stato delle sync e
#    cambiato le impostazioni in uso (senza salvarle)
def test_settings_incomplete_cloud_changes_nothing(tmp_path, monkeypatch):
    gui = pytest.importorskip("resosync.gui")
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(gui.system, "is_autostart_enabled", lambda: False)
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.folder_path = str(tmp_path / "cloud")
    cfg.save()
    state = tmp_path / "app/state/g.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"base": {"s.sav": "abc"}}')
    dlg, msgs = fake_dialog(cfg, backend="rclone", rclone_remote="")
    gui.SettingsDialog._save(dlg)
    assert "chiusa" not in msgs
    assert state.exists(), "stato delle sync cancellato"
    assert cfg.settings.backend == "folder", "impostazioni cambiate senza salvarle"


def test_restore_version_with_encryption(tmp_path):
    """Il ripristino controlla l'hash scritto nel nome della versione: con la cifratura il
    file sul cloud è cifrato, ma l'hash è quello del salvataggio in chiaro."""
    from pathlib import Path
    from resosync.backends import FolderBackend
    from resosync.config import Game, Settings
    from resosync.crypto import EncryptedBackend
    from resosync.engine import SyncEngine
    s = Settings(device_name="PC", encrypt=True, passphrase="passphrase di prova")
    eng = SyncEngine(s, EncryptedBackend(FolderBackend(tmp_path / "cloud"), s.passphrase),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_bytes(b"buono")
    eng.sync_game(g)
    Path(g.path, "s.sav").write_bytes(b"attuale, piu lungo")
    eng.sync_game(g)
    old = [v for v in eng.list_versions(g, "s.sav") if v[1].startswith("cloud")][-1]
    eng.restore_version(g, "s.sav", old[1], old[2])
    assert Path(g.path, "s.sav").read_bytes() == b"buono"


def test_service_loop_survives_unexpected_errors(tmp_path, monkeypatch):
    """Un errore imprevisto nel giro del servizio (qui nel controllo dei processi) non deve
    fermare per sempre le sincronizzazioni senza dire niente."""
    import time
    from pathlib import Path
    from resosync import service
    from resosync.backends import FolderBackend
    from resosync.config import Game
    from resosync.engine import SyncEngine
    calls = {"n": 0}

    def flaky(game, procs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("psutil ha restituito qualcosa di strano")
        return False
    monkeypatch.setattr(service, "is_game_running", flaky)
    monkeypatch.setattr(service, "running_processes", lambda with_paths=True: [])
    cfg = Config(app_dir=tmp_path / "app")
    cfg.settings.poll_interval = 1
    saves = tmp_path / "saves"
    saves.mkdir()
    cfg.games.append(Game("g", "G", str(saves), process="game.exe"))
    eng = SyncEngine(cfg.settings, FolderBackend(tmp_path / "cloud"), cfg.app_dir,
                     log=lambda m: None)
    events = []
    svc = service.SyncService(cfg, eng, lambda *a: events.append(a))
    try:
        svc.start()
        time.sleep(1)
        (saves / "s.sav").write_bytes(b"nuovo salvataggio")
        end = time.time() + 10
        while time.time() < end and not (tmp_path / "cloud/g/manifest.json").exists():
            time.sleep(0.2)
    finally:
        svc.stop()
    assert (tmp_path / "cloud/g/manifest.json").exists(), "il servizio si è fermato"
    assert any(e[0] == "log" and "psutil" in str(e) for e in events)
