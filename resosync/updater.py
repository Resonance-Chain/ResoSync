"""Aggiornamento automatico dalle Release di GitHub.

Controlla l'ultima release; se è più nuova scarica l'installer e lo avvia in modalità
silenziosa (Inno Setup chiude l'app, aggiorna i file e la riapre).
Se il repository è privato serve un token GitHub in sola lettura (Impostazioni > Sistema).
"""
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import requests

from . import __version__


@dataclass
class Update:
    version: str
    notes: str
    page_url: str
    installer_url: str     # URL API dell'asset (funziona anche coi repo privati)
    installer_name: str


def parse_version(text: str) -> tuple:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def _headers(token: str, accept="application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": f"ResoSync/{__version__}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def check(repo: str, token: str = "", current: str = __version__):
    """Ritorna un Update se c'è una versione più nuova, altrimenti None.
    Solleva PermissionError se il repository non è raggiungibile (privato senza token)."""
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                     headers=_headers(token), timeout=20)
    if r.status_code == 404:
        raise PermissionError("Release non trovate: se il repository è privato serve un token "
                              "GitHub (Impostazioni > Sistema)")
    r.raise_for_status()
    rel = r.json()
    version = rel.get("tag_name", "").lstrip("v")
    if parse_version(version) <= parse_version(current):
        return None
    setup = next((a for a in rel.get("assets", [])
                  if a["name"].lower().startswith(("resosync-setup", "savesync-setup")) and
                  a["name"].lower().endswith(".exe")), None)
    return Update(version=version, notes=rel.get("body") or "", page_url=rel.get("html_url", ""),
                  installer_url=setup["url"] if setup else "",
                  installer_name=setup["name"] if setup else "")


def can_self_install() -> bool:
    """Solo la versione installata su Windows si può aggiornare da sola."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def download(update: Update, token: str = "", progress=lambda done, total: None) -> str:
    target = os.path.join(tempfile.gettempdir(), update.installer_name or "ResoSync-Setup.exe")
    with requests.get(update.installer_url, headers=_headers(token, "application/octet-stream"),
                      stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(target + ".part", "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    os.replace(target + ".part", target)
    return target


def install(installer_path: str) -> None:
    """Avvia l'installer in modalità silenziosa e restituisce subito: l'app deve chiudersi
    (l'installer la riapre a fine aggiornamento)."""
    subprocess.Popen([installer_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/CLOSEAPPLICATIONS"], close_fds=True)
