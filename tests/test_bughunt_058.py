"""Caccia ai bug per la 0.5.8."""
import json
import time
from pathlib import Path

import pytest

import resosync.engine as em
from resosync.backends import FolderBackend
from resosync.config import Game, Settings
from resosync.engine import SyncEngine


@pytest.fixture
def clock(monkeypatch):
    """Salvataggi a ore di distanza (i nomi delle versioni portano la data)."""
    now = [time.time() - 10 * 3600]

    def stamp():
        return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now[0])) + "-000Z"
    monkeypatch.setattr(em, "_stamp", stamp)
    return now


def slot(eng, g, dat, meta, clock):
    Path(g.path, "Slot").mkdir(parents=True, exist_ok=True)
    if dat is not None:
        Path(g.path, "Slot/sav.dat").write_bytes(dat)
    if meta is not None:
        Path(g.path, "Slot/meta.json").write_bytes(meta)
    eng.sync_game(g)
    clock[0] += 3 * 3600


def current(g):
    return (Path(g.path, "Slot/sav.dat").read_bytes(), Path(g.path, "Slot/meta.json").read_bytes())


# 1. "Ripristina tutto il salvataggio": un file dello slot che in quel salvataggio non era
#    cambiato ha la versione di un salvataggio precedente (ore prima). Va preso lo stesso,
#    sennò lo slot ripristinato è mescolato
def test_restore_group_takes_unchanged_sibling(tmp_path, clock):
    eng = SyncEngine(Settings(device_name="PC"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"), group_by="folder")
    slot(eng, g, b"dat 1", b"meta 1", clock)
    slot(eng, g, b"dat 2", None, clock)        # il metadata non cambia
    slot(eng, g, b"dat 3", b"meta 3", clock)
    v2 = next(v for v in eng.list_versions(g, "Slot/sav.dat") if v[1].startswith("cloud")
              and (tmp_path / "cloud" / v[2]).read_bytes() == b"dat 2")
    eng.restore_group(g, "Slot/sav.dat", v2[1], v2[2])
    assert current(g) == (b"dat 2", b"meta 1")


# 2. se una delle versioni da rimettere è rovinata, non si ripristina niente (niente slot a
#    metà tra due salvataggi)
def test_restore_group_all_or_nothing(tmp_path, clock):
    eng = SyncEngine(Settings(device_name="PC"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"), group_by="folder")
    slot(eng, g, b"dat 1", b"meta 1", clock)
    slot(eng, g, b"dat 2", b"meta 2", clock)
    old = {Path(v[2]).parent.name: v for r in ("Slot/sav.dat", "Slot/meta.json")
           for v in eng.list_versions(g, r) if v[1].startswith("cloud")
           and (tmp_path / "cloud" / v[2]).read_bytes().endswith(b"1")}
    (tmp_path / "cloud" / old["meta.json"][2]).write_bytes(b"rovinat")
    with pytest.raises(Exception):
        eng.restore_group(g, "Slot/sav.dat", old["sav.dat"][1], old["sav.dat"][2])
    assert current(g) == (b"dat 2", b"meta 2")


def test_preview_shows_unchanged_screenshot(tmp_path, clock):
    """Anteprima: lo screenshot che in quel salvataggio non era cambiato si vede lo stesso."""
    eng = SyncEngine(Settings(device_name="PC"), FolderBackend(tmp_path / "cloud"),
                     tmp_path / "app", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / "saves"), group_by="folder")
    Path(g.path, "Slot").mkdir(parents=True)
    Path(g.path, "Slot/shot.png").write_bytes(b"PNG immagine")
    slot(eng, g, b"dat 1", b"{}", clock)
    slot(eng, g, b"dat 2", None, clock)  # screenshot e metadata uguali
    v2 = next(v for v in eng.list_versions(g, "Slot/sav.dat") if v[1].startswith("cloud")
              and (tmp_path / "cloud" / v[2]).read_bytes() == b"dat 2")
    assert eng.preview(g, "Slot/sav.dat", v2[1], v2[2])["image"] == b"PNG immagine"
