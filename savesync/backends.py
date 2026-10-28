"""Backend di storage cloud. Tutti lavorano con chiavi tipo "gioco/files/slot1.sav"."""
import os
import shutil
import threading
import time
import urllib.parse
from pathlib import Path


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
        raise NotImplementedError


class FolderBackend(Backend):
    """Usa una cartella come "cloud". Puntala dentro Google Drive, OneDrive, Dropbox,
    Syncthing o un disco di rete e ci pensa il loro client a mandarla online."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.root.joinpath(*key.split("/"))

    def read_bytes(self, key):
        p = self._p(key)
        return p.read_bytes() if p.exists() else None

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
        shutil.copyfile(self._p(key), path)

    def copy(self, src, dst):
        d = self._p(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._p(src), d)

    def delete(self, key):
        p = self._p(key)
        if p.exists():
            p.unlink()

    def list(self, prefix):
        base = self._p(prefix.rstrip("/"))
        if not base.exists():
            return []
        out = []
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith(".partial"):
                    continue
                rel = Path(dirpath, f).relative_to(self.root).as_posix()
                out.append(rel)
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

    def list(self, prefix):
        out = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._k(prefix)):
            for obj in page.get("Contents", []):
                out.append(obj["Key"][len(self.prefix):])
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

    def list(self, prefix):
        out, token = [], None
        while True:
            params = {"spaces": "appDataFolder", "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name)", "q": "trashed = false"}
            if token:
                params["pageToken"] = token
            r = self._req("GET", f"{self.API}/files", params=params).json()
            for f in r.get("files", []):
                with self._lock:
                    self._ids.setdefault(f["name"], f["id"])
                if f["name"].startswith(prefix):
                    out.append(f["name"])
            token = r.get("nextPageToken")
            if not token:
                return sorted(set(out))


class OneDriveBackend(_HttpBackend):
    """OneDrive (personale o lavoro), nella cartella App/SaveSync creata da Microsoft:
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

    def list(self, prefix):
        folder = prefix.rstrip("/")
        out = []
        self._walk(folder, out)
        return sorted(k for k in out if k.startswith(prefix))

    def _walk(self, folder, out):
        url = (self._item(folder) if folder else self.ROOT) + "/children?$select=name,folder&$top=999"
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
                    out.append(key)
            url = data.get("@odata.nextLink")


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
    else:
        base = FolderBackend(settings.folder_path)

    if settings.encrypt:
        return EncryptedBackend(base, settings.passphrase)
    if cloud_is_encrypted(base):
        raise PermissionError("I dati sul cloud sono cifrati: attiva la cifratura nelle "
                              "impostazioni e inserisci la stessa passphrase degli altri PC")
    return base
