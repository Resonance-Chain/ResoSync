"""Caccia ai bug per la 0.5.6."""
import json
import threading
import time
from pathlib import Path

import pytest

import resosync.engine as em
from resosync import service
from resosync.backends import FolderBackend
from resosync.config import Config, Game, Settings
from resosync.engine import SyncEngine


def pc(tmp_path, name):
    eng = SyncEngine(Settings(device_name=name), FolderBackend(tmp_path / "cloud"),
                     tmp_path / f"app-{name}", log=lambda m: None)
    g = Game("g", "G", str(tmp_path / f"saves-{name}"))
    Path(g.path).mkdir(parents=True, exist_ok=True)
    return eng, g


def blob_of(tmp_path, rel):
    m = json.loads((tmp_path / "cloud/g/manifest.json").read_bytes())["files"]
    return tmp_path / "cloud" / m[rel]["blob"]


# 1. verifica con una cartella cloud in ritardo: il salvataggio appena caricato da un altro PC
#    può non essere ancora arrivato (o essere a metà). Non è "rovinato": niente falsi allarmi
def test_verify_fresh_blob_still_arriving_is_not_broken(tmp_path):
    a, ga = pc(tmp_path, "A")
    b, gb = pc(tmp_path, "B")
    Path(ga.path, "s.sav").write_bytes(b"vecchio")
    a.sync_game(ga)
    b.sync_game(gb)
    Path(ga.path, "s.sav").write_bytes(b"nuovo, appena caricato da A")
    a.sync_game(ga)
    blob = blob_of(tmp_path, "s.sav")
    blob.write_bytes(b"nuovo, app")  # B lo vede ancora a metà
    rep = b.verify_cloud(gb)
    assert rep["broken"] == [] and rep["repaired"] == []
    assert rep["arriving"] == ["s.sav"]
    assert blob.read_bytes() == b"nuovo, app"  # non toccato


def test_verify_old_broken_blob_still_reported(tmp_path, monkeypatch):
    a, ga = pc(tmp_path, "A")
    Path(ga.path, "s.sav").write_bytes(b"x")
    a.sync_game(ga)
    Path(ga.path, "s.sav").write_bytes(b"altro")
    import shutil
    shutil.rmtree(tmp_path / "app-A/snapshots")
    blob_of(tmp_path, "s.sav").write_bytes(b"rovinato")
    monkeypatch.setattr(em, "GRACE", 0)  # caricato da più di 2 ore
    assert [r for r, _ in a.verify_cloud(ga)["broken"]] == ["s.sav"]

