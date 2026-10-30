"""Caccia ai bug per la 0.5.5."""
from pathlib import Path

import pytest

from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.crypto import CryptoError, EncryptedBackend
from resosync.engine import SyncEngine

PASS = "passphrase di prova"


# 1. PC nuovo su un cloud cifrato, mentre il client cloud sta ancora scaricando la cartella:
#    crypto.json non è ancora arrivato. Il PC nuovo NON deve crearne uno con un sale nuovo
#    (sovrascriverebbe quello vero e tutto il cloud diventerebbe illeggibile)
def test_missing_crypto_meta_on_non_empty_cloud_is_not_recreated(tmp_path):
    cloud = tmp_path / "cloud"
    s = Settings(device_name="PC", encrypt=True, passphrase=PASS)
    eng = SyncEngine(s, EncryptedBackend(FolderBackend(cloud), PASS), tmp_path / "a",
                     log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_bytes(b"progressi")
    eng.sync_game(g)
    meta = (cloud / "crypto.json").read_bytes()
    (cloud / "crypto.json").rename(tmp_path / "in-arrivo")  # non ancora scaricato
    with pytest.raises(CryptoError, match="crypto.json"):
        EncryptedBackend(FolderBackend(cloud), PASS)
    assert not (cloud / "crypto.json").exists()
    (tmp_path / "in-arrivo").rename(cloud / "crypto.json")
    assert (cloud / "crypto.json").read_bytes() == meta
    other = SyncEngine(s, EncryptedBackend(FolderBackend(cloud), PASS), tmp_path / "b",
                       log=lambda m: None)
    g2 = Game("g", "G", str(tmp_path / "saves2"))
    other.sync_game(g2)
    assert Path(g2.path, "s.sav").read_bytes() == b"progressi"


def test_empty_cloud_still_gets_encryption(tmp_path):
    EncryptedBackend(FolderBackend(tmp_path / "nuovo"), PASS)
    assert (tmp_path / "nuovo/crypto.json").exists()


def test_turning_on_encryption_on_plain_cloud_still_works(tmp_path):
    cloud = tmp_path / "cloud"
    eng = SyncEngine(Settings(device_name="PC"), FolderBackend(cloud), tmp_path / "a",
                     log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"))
    Path(g.path).mkdir()
    Path(g.path, "s.sav").write_bytes(b"in chiaro")
    eng.sync_game(g)
    EncryptedBackend(FolderBackend(cloud), PASS)  # nessun errore: si attiva la cifratura
    assert (cloud / "crypto.json").exists()
