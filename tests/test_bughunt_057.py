"""Caccia ai bug per la 0.5.7."""
import pytest

from resosync.backends import GoogleDriveBackend
from test_oauth_http import FakeDrive, FakeOAuth


def drive_pair():
    server = FakeDrive()
    a, b = GoogleDriveBackend(FakeOAuth()), GoogleDriveBackend(FakeOAuth())
    a.http = b.http = server
    return server, a, b


# 1. Google Drive permette due file con lo stesso nome. Due PC che creano lo stesso file nello
#    stesso momento (prima sync, lock) non devono finire a usare due copie diverse: non si
#    vedrebbero più (e il lock sarebbe di tutti e due)
def test_gdrive_same_name_created_twice_converges(tmp_path):
    server, a, b = drive_pair()
    a.write_bytes("g/lock.json", b"token A")
    b._create("g/lock.json", b"token B")  # B aveva cercato prima che A lo creasse
    names = [f[0] for f in server.files.values()]
    assert names.count("g/lock.json") == 1, "due copie dello stesso file"
    assert a.read_bytes("g/lock.json") == b.read_bytes("g/lock.json") == b"token B"
    a.write_bytes("g/lock.json", b"token A2")
    assert b.read_bytes("g/lock.json") == b"token A2"


def test_gdrive_listing_picks_the_oldest_duplicate():
    """Duplicati già presenti (versioni vecchie): elenco e letture usano sempre lo stesso."""
    server, a, b = drive_pair()
    a.write_bytes("g/manifest.json", b"primo")
    server.files["id99"] = ["g/manifest.json", b"duplicato"]  # creato dopo
    assert a.list("g/") == ["g/manifest.json"]
    b.sizes("g/")  # il listing riempie la cache degli ID
    assert b.read_bytes("g/manifest.json") == b"primo"
