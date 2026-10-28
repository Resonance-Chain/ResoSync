"""Da SaveSync a ResoSync: chi aveva già installato la versione vecchia non perde niente."""
import importlib
import subprocess
from pathlib import Path

from resosync import config, system, updater
from resosync.vault import Vault


def test_legacy_data_dir_is_kept(monkeypatch, tmp_path):
    monkeypatch.delenv("RESOSYNC_HOME", raising=False)
    monkeypatch.delenv("SAVESYNC_HOME", raising=False)
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
    assert config._default_app_dir() == tmp_path / ".resosync"  # installazione nuova
    (tmp_path / ".savesync").mkdir()
    assert config._default_app_dir() == tmp_path / ".savesync"  # dati di SaveSync: restano lì
    (tmp_path / ".resosync").mkdir()
    assert config._default_app_dir() == tmp_path / ".resosync"


def test_legacy_env_vars(monkeypatch, tmp_path):
    monkeypatch.delenv("RESOSYNC_HOME", raising=False)
    monkeypatch.setenv("SAVESYNC_HOME", str(tmp_path / "vecchio"))
    assert config._default_app_dir() == tmp_path / "vecchio"
    monkeypatch.setenv("RESOSYNC_HOME", str(tmp_path / "nuovo"))
    assert config._default_app_dir() == tmp_path / "nuovo"


class FakeKeyring:
    def __init__(self):
        self.store = {("SaveSync", "passphrase"): "segreta-vecchia"}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value


def test_keyring_secrets_migrated(tmp_path):
    v = Vault(tmp_path)
    v._keyring = FakeKeyring()  # come su Windows
    assert v.get("passphrase") == "segreta-vecchia"
    assert v._keyring.store[("ResoSync", "passphrase")] == "segreta-vecchia"
    assert v.get("non-esiste") == ""


def test_linux_service_and_autostart_migrated(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(system, "SYSTEM", "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(system.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(system, "_systemctl", lambda *a: calls.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    (tmp_path / "systemd/user").mkdir(parents=True)
    (tmp_path / "systemd/user/savesync.service").write_text("[Service]\nExecStart=vecchio\n")
    (tmp_path / "autostart").mkdir()
    (tmp_path / "autostart/savesync.desktop").write_text("[Desktop Entry]\n")
    done = system.migrate_from_savesync()
    assert set(done) == {"avvio automatico", "servizio in background"}
    assert ("disable", "--now", "savesync.service") in calls
    assert not (tmp_path / "systemd/user/savesync.service").exists()
    assert "--service" in (tmp_path / "systemd/user/resosync.service").read_text()
    assert not (tmp_path / "autostart/savesync.desktop").exists()
    assert (tmp_path / "autostart/resosync.desktop").exists()
    assert system.migrate_from_savesync() == []  # la seconda volta non fa niente


def test_updater_accepts_old_installer_name(monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"tag_name": "v0.5.0", "assets": [{"name": "SaveSync-Setup-0.5.0.exe",
                                                      "url": "u"}]}

        def raise_for_status(self):
            pass
    monkeypatch.setattr(updater.requests, "get", lambda url, headers, timeout: R())
    assert updater.check("o/r", current="0.4.0").installer_url == "u"


def test_install_script_mentions_compat():
    script = (Path(__file__).resolve().parents[1] / "install-steamdeck.sh").read_text()
    assert 'ln -sfn "$DEST" "$LEGACY"' in script and "savesync.service" in script


def test_module_importable_under_new_name():
    assert importlib.import_module("resosync").__version__
