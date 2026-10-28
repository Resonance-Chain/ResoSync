import json

from savesync.config import Config, Game, process_names


def test_secrets_not_in_json(tmp_path):
    cfg = Config(app_dir=tmp_path)
    cfg.settings.s3_secret_key = "SEGRETO"
    cfg.settings.passphrase = "pass-segreta"
    cfg.games.append(Game("g", "G", "C:/x", process="Game.exe"))
    cfg.save()
    text = (tmp_path / "config.json").read_text()
    assert "SEGRETO" not in text and "pass-segreta" not in text
    again = Config.load(tmp_path)
    assert again.settings.s3_secret_key == "SEGRETO"
    assert again.settings.passphrase == "pass-segreta"
    assert again.games[0].process == "Game.exe"


def test_migrates_plaintext_secret(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"settings": {"backend": "s3", "s3_bucket": "b", "s3_secret_key": "VECCHIO",
                      "campo_sconosciuto": 1},
         "games": [{"id": "g", "name": "G", "path": "/x"}]}))
    cfg = Config.load(tmp_path)
    assert cfg.settings.s3_secret_key == "VECCHIO"
    assert "VECCHIO" not in (tmp_path / "config.json").read_text()
    assert cfg.games[0].process == ""


def test_process_names():
    assert process_names("EldenRing.exe, eldenring ; Other.EXE") == {"eldenring", "other"}
