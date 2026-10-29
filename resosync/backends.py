"""Backend di storage cloud. Tutti lavorano con chiavi tipo "gioco/files/slot1.sav"."""
import os
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path


def retry_locked(fn, attempts=8):
    """Su Windows il client cloud (Proton Drive, OneDrive, Dropbox) o l'antivirus possono
    tenere un file aperto per un attimo mentre lo caricano: riprovo invece di fallire."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.25 * (i + 1))


def replace_retry(src, dst, attempts=8):
    """os.replace, ma su Windows riprova: antivirus, indicizzatore o il client di
    Dropbox/OneDrive possono tenere il file aperto per qualche istante."""
    for i in range(attempts):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.25 * (i + 1))


class Backend:
    def read_bytes(self, key: str):
        """Ritorna i byte o None se la chiave non esiste."""
        raise NotImplementedError

    def write_bytes(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def upload_file(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def download_file(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def copy(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def list(self, prefix: str) -> list:
        return sorted(self.sizes(prefix))

    def sizes(self, prefix: str) -> dict:
        """{chiave: dimensione in byte} per tutte le chiavi che iniziano con prefix."""
        raise NotImplementedError


class FolderBackend(Backend):
    """Usa una cartella come "cloud". Puntala dentro Google Drive, OneDrive, Dropbox,
    Syncthing o un disco di rete e ci pensa il loro client a mandarla online."""

    def __init__(self, root: str):
        if not str(root).strip():
            raise ValueError("Cartella del cloud non impostata")
        self.root = Path(root)
        if not self.root.exists() and not self.root.parent.exists():
            # Es. Proton Drive/OneDrive non montato o disco di rete scollegato: NON creo
            # una cartella "finta" in locale, sennò si sincronizzerebbe nel vuoto
            raise FileNotFoundError(f"Cartella del cloud non disponibile: {self.root.parent}")
        self.root.mkdir(exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.root.joinpath(*key.split("/"))

    def read_bytes(self, key):
        p = self._p(key)
        return retry_locked(p.read_bytes) if p.exists() else None

    def write_bytes(self, key, data):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".partial")
        tmp.write_bytes(data)
        replace_retry(tmp, p)

    def upload_file(self, key, path):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".partial")
        shutil.copyfile(path, tmp)
        replace_retry(tmp, p)

    def download_file(self, key, path):
        retry_locked(lambda: shutil.copyfile(self._p(key), path))

    def copy(self, src, dst):
        d = self._p(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._p(src), d)

    def delete(self, key):
        p = self._p(key)
        if p.exists():
            p.unlink()

    def sizes(self, prefix):
        base = self._p(prefix.rstrip("/"))
        if not base.exists():
            return {}
        out = {}
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith(".partial"):
                    continue
                p = Path(dirpath, f)
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    try:
                        out[key] = p.stat().st_size
                    except OSError:
                        pass
        return out


class S3Backend(Backend):
    """Qualsiasi storage compatibile S3: AWS S3, Cloudflare R2, Backblaze B2, MinIO, Wasabi..."""

    def __init__(self, bucket, prefix="", endpoint="", region="", access_key="", secret_key=""):
        import boto3  # import lazy: serve solo a chi usa S3

        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            region_name=region or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        )

    def _k(self, key):
        return self.prefix + key

    def read_bytes(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def write_bytes(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)

    def upload_file(self, key, path):
        self.s3.upload_file(str(path), self.bucket, self._k(key))

    def download_file(self, key, path):
        self.s3.download_file(self.bucket, self._k(key), str(path))

    def copy(self, src, dst):
        self.s3.copy_object(Bucket=self.bucket, Key=self._k(dst),
                            CopySource={"Bucket": self.bucket, "Key": self._k(src)})

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))

    def sizes(self, prefix):
        out = {}
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._k(prefix)):
            for obj in page.get("Contents", []):
                out[obj["Key"][len(self.prefix):]] = obj.get("Size", 0)
        return out


class _HttpBackend(Backend):
    """Base per le API REST con login OAuth: ritenta su 429/5xx e rinnova il token su 401."""

    def __init__(self, oauth):
        import requests
        self.oauth = oauth
        self.http = requests.Session()

    def _req(self, method, url, auth=True, ok=(200, 201, 204), **kw):
        headers = kw.pop("headers", {})
        for attempt in range(6):
            if auth:
                headers["Authorization"] = "Bearer " + self.oauth.access_token()
            r = self.http.request(method, url, headers=headers, timeout=120, **kw)
            if r.status_code == 401 and auth and attempt == 0:
                self.oauth.invalidate()
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(int(r.headers.get("Retry-After", 2 ** attempt)), 30))
                continue
            if r.status_code not in ok:
                raise IOError(f"{method} {url.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
            return r
        raise IOError(f"{method} {url.split('?')[0]}: troppi tentativi ({r.status_code})")

    def upload_file(self, key, path):
        self.write_bytes(key, Path(path).read_bytes())


class GoogleDriveBackend(_HttpBackend):
    """Google Drive, nella cartella dati nascosta dell'app (appDataFolder): l'app vede solo
    i suoi file e il resto del Drive non viene toccato. Struttura piatta: nome = chiave."""
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, oauth):
        super().__init__(oauth)
        self._ids = {}
        self._lock = threading.Lock()

    def _find(self, key, refresh=False):
        with self._lock:
            if key in self._ids and not refresh:
                return self._ids[key]
        name = key.replace("\\", "\\\\").replace("'", "\\'")
        r = self._req("GET", f"{self.API}/files", params={
            "spaces": "appDataFolder", "orderBy": "createdTime", "pageSize": 10,
            "q": f"name = '{name}' and 'appDataFolder' in parents and trashed = false",
            "fields": "files(id,name)"})
        files = r.json().get("files", [])
        fid = files[0]["id"] if files else None
        with self._lock:
            if fid:
                self._ids[key] = fid
            else:
                self._ids.pop(key, None)
        return fid

    def read_bytes(self, key):
        for refresh in (False, True):
            fid = self._find(key, refresh)
            if not fid:
                return None
            r = self._req("GET", f"{self.API}/files/{fid}", params={"alt": "media"},
                          ok=(200, 404))
            if r.status_code == 200:
                return r.content
        return None

    def write_bytes(self, key, data):
        fid = self._find(key)
        if fid:
            r = self._req("PATCH", f"{self.UPLOAD}/files/{fid}?uploadType=resumable",
                          json={}, ok=(200, 404))
            if r.status_code == 404:  # cancellato da un altro PC nel frattempo
                fid = self._find(key, refresh=True)
                return self.write_bytes(key, data) if fid else self._create(key, data)
            r = self._req("PUT", r.headers["Location"], data=data, auth=False)
        else:
            self._create(key, data)

    def _create(self, key, data):
        r = self._req("POST", f"{self.UPLOAD}/files?uploadType=resumable",
                      json={"name": key, "parents": ["appDataFolder"]})
        r = self._req("PUT", r.headers["Location"], data=data, auth=False)
        with self._lock:
            self._ids[key] = r.json()["id"]

    def download_file(self, key, path):
        data = self.read_bytes(key)
        if data is None:
            raise FileNotFoundError(key)
        Path(path).write_bytes(data)

    def copy(self, src, dst):
        fid = self._find(src)
        if not fid:
            raise FileNotFoundError(src)
        r = self._req("POST", f"{self.API}/files/{fid}/copy",
                      json={"name": dst, "parents": ["appDataFolder"]})
        with self._lock:
            self._ids[dst] = r.json()["id"]

    def delete(self, key):
        fid = self._find(key)
        if fid:
            self._req("DELETE", f"{self.API}/files/{fid}", ok=(200, 204, 404))
            with self._lock:
                self._ids.pop(key, None)

    def sizes(self, prefix):
        out, token = {}, None
        while True:
            params = {"spaces": "appDataFolder", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,size)", "q": "trashed = false"}
            if token:
                params["pageToken"] = token
            r = self._req("GET", f"{self.API}/files", params=params).json()
            for f in r.get("files", []):
                with self._lock:
                    self._ids.setdefault(f["name"], f["id"])
                if f["name"].startswith(prefix):
                    out[f["name"]] = int(f.get("size") or 0)
            token = r.get("nextPageToken")
            if not token:
                return out


class OneDriveBackend(_HttpBackend):
    """OneDrive (personale o lavoro), nella cartella App/ResoSync creata da Microsoft:
    l'app non può vedere il resto del tuo OneDrive."""
    ROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"
    SIMPLE_LIMIT = 4 * 1024 * 1024
    CHUNK = 32 * 320 * 1024  # 10 MiB, multiplo di 320 KiB come vuole l'API

    def _item(self, key):
        return f"{self.ROOT}:/{urllib.parse.quote(key, safe='/')}:"

    def read_bytes(self, key):
        r = self._req("GET", self._item(key) + "/content", ok=(200, 404))
        return r.content if r.status_code == 200 else None

    def write_bytes(self, key, data):
        if len(data) <= self.SIMPLE_LIMIT:
            self._req("PUT", self._item(key) + "/content", data=data)
            return
        r = self._req("POST", self._item(key) + "/createUploadSession",
                      json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        url = r.json()["uploadUrl"]
        for start in range(0, len(data), self.CHUNK):
            chunk = data[start:start + self.CHUNK]
            end = start + len(chunk) - 1
            self._req("PUT", url, auth=False, data=chunk, ok=(200, 201, 202),
                      headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"})

    def download_file(self, key, path):
        data = self.read_bytes(key)
        if data is None:
            raise FileNotFoundError(key)
        Path(path).write_bytes(data)

    def copy(self, src, dst):
        data = self.read_bytes(src)
        if data is None:
            raise FileNotFoundError(src)
        self.write_bytes(dst, data)

    def delete(self, key):
        self._req("DELETE", self._item(key), ok=(200, 204, 404))

    def sizes(self, prefix):
        folder = prefix.rstrip("/")
        out = {}
        self._walk(folder, out)
        return {k: v for k, v in out.items() if k.startswith(prefix)}

    def _walk(self, folder, out):
        url = (self._item(folder) if folder else self.ROOT) + \
            "/children?$select=name,folder,size&$top=999"
        while url:
            r = self._req("GET", url, ok=(200, 404))
            if r.status_code == 404:
                return
            data = r.json()
            for item in data.get("value", []):
                key = f"{folder}/{item['name']}" if folder else item["name"]
                if "folder" in item:
                    self._walk(key, out)
                else:
                    out[key] = int(item.get("size") or 0)
            url = data.get("@odata.nextLink")


class RcloneError(IOError):
    pass


def find_rclone(configured=""):
    """Il programma rclone: quello indicato, poi il PATH, poi i posti dove lo mette chi lo
    installa senza permessi di amministratore (sullo Steam Deck il servizio systemd non ha
    ~/.local/bin nel PATH)."""
    exe = "rclone.exe" if os.name == "nt" else "rclone"
    candidates = [configured] if configured else []
    candidates.append(shutil.which("rclone") or "")
    home = Path.home()
    candidates += [str(home / ".local" / "bin" / exe), str(home / "bin" / exe),
                   str(Path(sys.executable).parent / exe)]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise FileNotFoundError("rclone non trovato: installalo (rclone.org/downloads) o indica "
                            "dove si trova nelle impostazioni")


def rclone_remotes(exe="", config=""):
    """I remote configurati in rclone (es. ['proton:', 'gdrive:'])."""
    import subprocess
    cmd = [find_rclone(exe), "listremotes"] + (["--config", config] if config else [])
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    out = subprocess.run(cmd, capture_output=True, timeout=30, **kw)
    if out.returncode:
        raise RcloneError(out.stderr.decode("utf-8", "replace").strip() or "rclone listremotes")
    return [r.strip() for r in out.stdout.decode("utf-8", "replace").splitlines() if r.strip()]


class RcloneBackend(Backend):
    """Qualsiasi cloud supportato da rclone (oltre 70: Proton Drive, Dropbox, pCloud, Mega,
    Box, SFTP, WebDAV...). Il remote si configura una volta con `rclone config`; qui basta
    "nome:cartella"."""

    MISSING = (3, 4)  # rclone: cartella / file non trovati. Tutto il resto è un errore vero

    def __init__(self, remote: str, exe: str = "", config: str = ""):
        remote = remote.strip()
        if ":" not in remote:
            raise ValueError("Remote rclone non valido: scrivilo come nome:cartella "
                             "(es. proton:ResoSync)")
        self.root = remote if remote.endswith(":") else remote.rstrip("/") + "/"
        self.exe = find_rclone(exe)
        self.config = config

    def _r(self, key):
        return self.root + key

    def _run(self, args, data=None, timeout=300, missing_ok=False):
        import subprocess
        cmd = [self.exe, *args, "--retries", "2", "--low-level-retries", "3"]
        if self.config:
            cmd += ["--config", self.config]
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: niente finestre nere
        try:
            p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired:
            raise RcloneError(f"rclone non risponde ({args[0]})")
        if p.returncode == 0:
            return p.stdout
        if missing_ok and p.returncode in self.MISSING:
            return None
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RcloneError(f"rclone {args[0]}: {err[-1] if err else p.returncode}")

    def read_bytes(self, key):
        # Solo "non trovato" vale come file assente: un errore di rete NON deve far sembrare
        # vuoto il cloud (sembrerebbe che qualcuno abbia cancellato tutto)
        return self._run(["cat", self._r(key)], missing_ok=True)

    def write_bytes(self, key, data):
        self._run(["rcat", self._r(key)], data=data)

    def upload_file(self, key, path):
        self._run(["copyto", str(path), self._r(key)])

    def download_file(self, key, path):
        # copyto salta la copia se trova un file con stessa dimensione e data: un temporaneo
        # rimasto da un crash non deve passare per quello giusto
        Path(path).unlink(missing_ok=True)
        if self._run(["copyto", self._r(key), str(path)], missing_ok=True) is None:
            raise FileNotFoundError(key)
        if not Path(path).is_file():  # rclone copyto non segnala sempre la sorgente assente
            raise FileNotFoundError(key)

    def copy(self, src, dst):
        if self._run(["copyto", self._r(src), self._r(dst)], missing_ok=True) is None:
            raise FileNotFoundError(src)

    def delete(self, key):
        try:
            self._run(["deletefile", self._r(key)], missing_ok=True)
        except RcloneError:
            if self.read_bytes(key) is not None:  # c'è ancora: errore vero
                raise

    def sizes(self, prefix):
        import json
        folder = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        out = self._run(["lsjson", "-R", "--files-only", "--no-mimetype", "--no-modtime",
                         self._r(folder)], timeout=120, missing_ok=True)
        if out is None:
            return {}
        result = {}
        for item in json.loads(out or b"[]"):
            key = f"{folder}/{item['Path']}" if folder else item["Path"]
            if key.startswith(prefix) and not key.endswith(".partial"):
                result[key] = int(item.get("Size") or 0)
        return result


def make_backend(settings, vault=None) -> Backend:
    """Crea il backend scelto, cifrato se richiesto."""
    from .crypto import EncryptedBackend, cloud_is_encrypted
    from .oauth import OAuthClient

    if settings.backend == "s3":
        base = S3Backend(settings.s3_bucket, settings.s3_prefix, settings.s3_endpoint,
                         settings.s3_region, settings.s3_access_key, settings.s3_secret_key)
    elif settings.backend == "gdrive":
        base = GoogleDriveBackend(OAuthClient("gdrive", settings.gdrive_client_id, vault,
                                              settings.gdrive_client_secret))
    elif settings.backend == "onedrive":
        base = OneDriveBackend(OAuthClient("onedrive", settings.onedrive_client_id, vault))
    elif settings.backend == "rclone":
        base = RcloneBackend(settings.rclone_remote, settings.rclone_path,
                             settings.rclone_config)
    else:
        base = FolderBackend(settings.folder_path)

    if settings.encrypt:
        return EncryptedBackend(base, settings.passphrase)
    if cloud_is_encrypted(base):
        raise PermissionError("I dati sul cloud sono cifrati: attiva la cifratura nelle "
                              "impostazioni e inserisci la stessa passphrase degli altri PC")
    return base
