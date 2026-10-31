"""OAuth e backend HTTP contro server finti in memoria (niente rete)."""
import json
import re
import threading
import urllib.parse
import urllib.request

import pytest

from resosync import oauth
from resosync.backends import GoogleDriveBackend, OneDriveBackend
from resosync.vault import Vault


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.text = self.content.decode(errors="ignore")
        self.ok = status < 400

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if not self.ok:
            raise IOError(self.status_code)


def test_login_flow(tmp_path, monkeypatch):
    vault = Vault(tmp_path)
    client = oauth.OAuthClient("onedrive", "cid", vault)
    posted = []

    def fake_post(url, data, timeout):
        posted.append(data)
        if data["grant_type"] == "authorization_code":
            return FakeResp(200, {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600})
        return FakeResp(200, {"access_token": "A2", "refresh_token": "REFRESH-SEGRETO-ROTATO", "expires_in": 3600})
    monkeypatch.setattr(oauth.requests, "post", fake_post)

    browser_errors = []

    def browser(url):  # il "browser" segue il redirect come farebbe l'utente
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"]
        redirect = q["redirect_uri"][0].replace("localhost", "127.0.0.1")
        target = f"{redirect}/?code=CODE&state={q['state'][0]}"

        def follow():
            try:  # niente proxy: è il PC stesso, come per il browser vero
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                opener.open(target, timeout=10).read()
            except Exception as e:  # noqa: BLE001 - sennò si vedrebbe solo il timeout
                browser_errors.append(repr(e))
        threading.Thread(target=follow, daemon=True).start()

    try:
        client.login(timeout=10, open_browser=browser)
    except TimeoutError:
        raise AssertionError(f"login non completato; il browser ha visto: {browser_errors}")
    assert posted[0]["code"] == "CODE" and "code_verifier" in posted[0]
    assert client.access_token() == "A1"
    # token scaduto -> refresh, e i token restano cifrati su disco
    client._tokens["expires_at"] = 0
    assert client.access_token() == "A2"
    assert b"REFRESH-SEGRETO-ROTATO" not in (tmp_path / "tokens/onedrive.bin").read_bytes()
    fresh = oauth.OAuthClient("onedrive", "cid", vault)
    assert fresh.logged_in and fresh.access_token() == "A2"
    # client id cambiato -> serve un nuovo login
    with pytest.raises(oauth.NeedsLogin):
        oauth.OAuthClient("onedrive", "altro", vault).access_token()


class FakeOAuth:
    def access_token(self):
        return "tok"

    def invalidate(self):
        pass


class FakeOneDrive:
    """Simula Graph: percorsi sotto approot, upload semplice e a sessione."""
    ROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"

    def __init__(self):
        self.files = {}
        self.sessions = {}

    def request(self, method, url, headers=None, data=None, json=None, params=None, timeout=0):
        if url.startswith("https://upload/"):
            key, buf = self.sessions[url]
            start, end, total = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)",
                                                  headers["Content-Range"]).groups())
            assert start == len(buf) and "Authorization" not in headers
            buf.extend(data)
            if len(buf) == total:
                self.files[key] = bytes(buf)
                return FakeResp(201, {})
            return FakeResp(202, {})
        assert headers["Authorization"] == "Bearer tok"
        rest = urllib.parse.unquote(url[len(self.ROOT):].split("?")[0])
        m = re.match(r"^:/(.*):(/content|/createUploadSession|/children)?$", rest)
        if rest == "/children" or (m and m.group(2) == "/children"):
            folder = m.group(1) + "/" if m else ""
            names = {}
            for k in self.files:
                if k.startswith(folder):
                    first, _, more = k[len(folder):].partition("/")
                    names[first] = bool(more)
            if not names:
                return FakeResp(404, {})
            items = [{"name": n, **({"folder": {}} if d else {})} for n, d in names.items()]
            return FakeResp(200, {"value": items})
        key, op = m.group(1), m.group(2)
        if method == "GET" and op == "/content":
            return FakeResp(200, self.files[key]) if key in self.files else FakeResp(404, {})
        if method == "PUT" and op == "/content":
            assert len(data) <= OneDriveBackend.SIMPLE_LIMIT
            self.files[key] = data
            return FakeResp(201, {})
        if method == "POST" and op == "/createUploadSession":
            url = f"https://upload/{len(self.sessions)}"
            self.sessions[url] = (key, bytearray())
            return FakeResp(200, {"uploadUrl": url})
        if method == "DELETE":
            return FakeResp(204 if self.files.pop(key, None) is not None else 404, {})
        raise AssertionError((method, url))


class FakeDrive:
    """Simula Drive v3 con appDataFolder (nomi piatti, ID opachi, upload resumable)."""

    def __init__(self):
        self.files = {}  # id -> [name, data]
        self.pending = {}
        self.n = 0

    def _new_id(self):
        self.n += 1
        return f"id{self.n}"

    def request(self, method, url, headers=None, data=None, json=None, params=None, timeout=0):
        api, up = GoogleDriveBackend.API, GoogleDriveBackend.UPLOAD
        if url.startswith("https://resumable/"):
            assert "Authorization" not in headers
            fid, name = self.pending.pop(url)
            fid = fid or self._new_id()
            self.files[fid] = [name if name else self.files[fid][0], data]
            return FakeResp(200, {"id": fid})
        assert headers["Authorization"] == "Bearer tok"
        if method == "GET" and url == f"{api}/files":
            q = params.get("q", "")
            m = re.match(r"name = '((?:[^'\\]|\\.)*)'", q)
            # come Drive: l'ordine dell'elenco non è garantito (qui: dal più nuovo), solo
            # orderBy=createdTime lo fissa
            items = [{"id": i, "name": f[0], "createdTime": f"{int(i[2:]):010d}"}
                     for i, f in reversed(list(self.files.items()))]
            if params.get("orderBy") == "createdTime":
                items.sort(key=lambda x: x["createdTime"])
            if m:
                want = re.sub(r"\\(.)", r"\1", m.group(1))
                items = [x for x in items if x["name"] == want]
            return FakeResp(200, {"files": items})
        if method == "POST" and url == f"{up}/files?uploadType=resumable":
            loc = f"https://resumable/{len(self.pending)}-{self.n}"
            self.pending[loc] = (None, json["name"])
            return FakeResp(200, {}, {"Location": loc})
        m = re.match(re.escape(up) + r"/files/(\w+)\?uploadType=resumable", url)
        if method == "PATCH" and m:
            if m.group(1) not in self.files:
                return FakeResp(404, {})
            loc = f"https://resumable/p{len(self.pending)}-{self.n}"
            self.pending[loc] = (m.group(1), None)
            return FakeResp(200, {}, {"Location": loc})
        m = re.match(re.escape(api) + r"/files/(\w+)(/copy)?$", url)
        if m and method == "GET":
            f = self.files.get(m.group(1))
            return FakeResp(200, f[1]) if f else FakeResp(404, {})
        if m and method == "POST" and m.group(2):
            fid = self._new_id()
            self.files[fid] = [json["name"], self.files[m.group(1)][1]]
            return FakeResp(200, {"id": fid})
        if m and method == "DELETE":
            self.files.pop(m.group(1), None)
            return FakeResp(204, {})
        raise AssertionError((method, url))


@pytest.mark.parametrize("make", ["onedrive", "gdrive"])
def test_http_backend_contract(make, tmp_path):
    if make == "onedrive":
        b, server = OneDriveBackend(FakeOAuth()), FakeOneDrive()
        b.CHUNK = 320 * 1024
    else:
        b, server = GoogleDriveBackend(FakeOAuth()), FakeDrive()
    b.http = server
    assert b.read_bytes("g/manifest.json") is None
    b.write_bytes("g/manifest.json", b"{}")
    b.write_bytes("g/manifest.json", b'{"v":2}')
    assert b.read_bytes("g/manifest.json") == b'{"v":2}'
    big = bytes(range(256)) * 20000  # ~5 MB: su OneDrive passa dalla upload session
    src = tmp_path / "big.sav"
    src.write_bytes(big)
    b.upload_file("g/files/sub/big.sav", src)
    b.copy("g/files/sub/big.sav", "g/versions/sub/big.sav/20260101-000000-000__PC")
    out = tmp_path / "out"
    b.download_file("g/versions/sub/big.sav/20260101-000000-000__PC", out)
    assert out.read_bytes() == big
    b.write_bytes("it's/strano.json", b"1")  # apostrofo nella query di Drive
    assert b.read_bytes("it's/strano.json") == b"1"
    assert b.list("g/versions/sub/big.sav/") == ["g/versions/sub/big.sav/20260101-000000-000__PC"]
    assert sorted(b.list("g/")) == ["g/files/sub/big.sav", "g/manifest.json",
                                     "g/versions/sub/big.sav/20260101-000000-000__PC"]
    b.delete("g/manifest.json")
    b.delete("g/manifest.json")
    assert b.read_bytes("g/manifest.json") is None
