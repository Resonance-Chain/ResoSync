from pathlib import Path

from savesync import gamedb

RAW = {
    "Elden Ring": {"files": {"<winAppData>/EldenRing/<storeUserId>": {"tags": ["save"],
                                                                      "when": [{"os": "windows"}]},
                             "<winAppData>/EldenRing/GraphicsConfig.xml": {"tags": ["config"]}},
                   "launch": {"<base>/Game/eldenring.exe": [{"when": [{"store": "steam"}]}]},
                   "installDir": {"ELDEN RING": {}}, "steam": {"id": 1245620}},
    "Hollow Knight": {"files": {
        "<home>/AppData/LocalLow/Team Cherry/Hollow Knight/*.dat": {"when": [{"os": "windows"}]},
        "<xdgConfig>/unity3d/Team Cherry/Hollow Knight/*.dat": {"when": [{"os": "linux"}]}}},
    "Old Game [GOTY]": {"files": {"<base>/save": {"tags": ["save"]}},
                        "installDir": {"Old Game [GOTY]": {}}},
    "Solo config": {"files": {"<home>/x.ini": {"tags": ["config"]}}},
    "Not installed": {"files": {"<winDocuments>/Nope": {}}},
    "Too broad": {"files": {"<winDocuments>": {}}},
}


def fake_windows(monkeypatch, home):
    monkeypatch.setattr(gamedb, "SYSTEM", "windows")
    monkeypatch.setattr(gamedb, "placeholders", lambda: {
        "<home>": home, "<osUserName>": "u", "<winAppData>": home / "AppData/Roaming",
        "<winLocalAppData>": home / "AppData/Local", "<winDocuments>": home / "Documents",
        "<winPublic>": home / "Public", "<winProgramData>": home / "PD", "<winDir>": home / "W"})


def test_compact():
    c = gamedb.compact(RAW)
    assert "Solo config" not in c
    assert c["Elden Ring"]["f"] == [["<winAppData>/EldenRing/<storeUserId>", ["windows"]]]
    assert c["Elden Ring"]["x"] == [["eldenring.exe", []]]
    assert c["Elden Ring"]["s"] == 1245620


def test_detect_windows(monkeypatch, tmp_path):
    home = tmp_path / "home"
    er = home / "AppData/Roaming/EldenRing/76561198000000000"
    er.mkdir(parents=True)
    (er / "ER0000.sl2").write_text("x")
    hk = home / "AppData/LocalLow/Team Cherry/Hollow Knight"
    hk.mkdir(parents=True)
    (hk / "user1.dat").write_text("x")
    (hk / "output_log.txt").write_text("x")
    (home / "Documents").mkdir()
    (home / "Documents/a.txt").write_text("x")
    lib = tmp_path / "SteamLibrary"
    save = lib / "steamapps/common/Old Game [GOTY]/save"
    save.mkdir(parents=True)
    (save / "1.sav").write_text("x")
    fake_windows(monkeypatch, home)
    monkeypatch.setattr(gamedb, "steam_libraries", lambda: [lib])

    found = {d.name: d for d in gamedb.detect({"games": gamedb.compact(RAW)})}
    assert set(found) == {"Elden Ring", "Hollow Knight", "Old Game [GOTY]"}
    assert Path(found["Elden Ring"].folder) == er and found["Elden Ring"].patterns == ["*"]
    assert found["Elden Ring"].process == "eldenring.exe"
    # pattern del manifest mantenuto: i nuovi slot verranno presi
    assert Path(found["Hollow Knight"].folder) == hk
    assert found["Hollow Knight"].patterns == ["*.dat"]
    assert found["Hollow Knight"].files == 1
    assert Path(found["Old Game [GOTY]"].folder) == save


def test_install_dirs(monkeypatch, tmp_path):
    lib = tmp_path / "SteamLibrary"
    (lib / "steamapps/common/Cyberpunk 2077/bin/x64").mkdir(parents=True)
    monkeypatch.setattr(gamedb, "steam_libraries", lambda: [lib])
    monkeypatch.setattr(gamedb, "SYSTEM", "linux")  # niente Epic/GOG/registro nel test
    db = {"games": gamedb.compact({"Cyberpunk 2077": {
        "files": {"<home>/Saved Games/CD Projekt Red/Cyberpunk 2077": {"tags": ["save"]}},
        "installDir": {"Cyberpunk 2077": {}},
        "launch": {"<base>/redprelauncher.exe": [{"when": [{"os": "windows"}]}]}}})}
    installed = gamedb.installed_games()
    assert installed["cyberpunk 2077"] == [str(lib / "steamapps/common/Cyberpunk 2077")]
    assert gamedb.install_dir_for(db, "Cyberpunk 2077", installed) == \
        str(lib / "steamapps/common/Cyberpunk 2077")
    assert gamedb.install_dir_for(db, "Gioco inesistente", installed) == ""
    # anche "Rileva giochi" lo compila
    saves = tmp_path / "home/Saved Games/CD Projekt Red/Cyberpunk 2077"
    (saves / "ManualSave-1").mkdir(parents=True)
    (saves / "ManualSave-1/sav.dat").write_text("x")
    monkeypatch.setattr(gamedb, "placeholders", lambda: {"<home>": tmp_path / "home"})
    found = gamedb.detect(db)
    assert found[0].install_dir == str(lib / "steamapps/common/Cyberpunk 2077")
