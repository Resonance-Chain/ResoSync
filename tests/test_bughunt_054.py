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
