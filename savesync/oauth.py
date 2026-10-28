"""Login OAuth per app desktop: apre il browser, riceve il codice su 127.0.0.1 (PKCE).

Niente librerie Google/Microsoft: bastano `requests` e la libreria standard.
"""
import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests


class NeedsLogin(Exception):
    pass


PROVIDERS = {
    "gdrive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.appdata"],
        "redirect_host": "127.0.0.1",
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "onedrive": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["Files.ReadWrite.AppFolder", "offline_access"],
        "redirect_host": "localhost",  # Microsoft accetta qualsiasi porta su http://localhost
        "extra": {"prompt": "select_account"},
    },
}

_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>SaveSync</title>
<body style="font-family:sans-serif;text-align:center;padding-top:15vh">
<h2>{title}</h2><p>{msg}</p></body>"""


class OAuthClient:
    def __init__(self, provider: str, client_id: str, vault, client_secret: str = ""):
        self.name = provider
        self.p = PROVIDERS[provider]
        self.client_id = client_id
        self.client_secret = client_secret
        self.vault = vault
        self._lock = threading.Lock()
        self._tokens = None

    @property
    def logged_in(self) -> bool:
        return bool(self._load().get("refresh_token"))

    def _load(self) -> dict:
        if self._tokens is None:
            self._tokens = self.vault.load_tokens(self.name)
        return self._tokens

    def _store(self, data: dict) -> None:
        tokens = dict(self._load())
        tokens.update({k: v for k, v in data.items() if v})
        tokens["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        tokens["client_id"] = self.client_id
        self._tokens = tokens
        self.vault.save_tokens(self.name, tokens)

    def access_token(self) -> str:
        with self._lock:
            t = self._load()
            if not t.get("refresh_token") or t.get("client_id") != self.client_id:
                raise NeedsLogin("Accesso al cloud non effettuato: vai in Impostazioni > Accedi")
            if t.get("access_token") and t.get("expires_at", 0) - 60 > time.time():
                return t["access_token"]
            data = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                    "client_id": self.client_id}
            if self.client_secret:
                data["client_secret"] = self.client_secret
            if self.name == "onedrive":
                data["scope"] = " ".join(self.p["scopes"])
            r = requests.post(self.p["token_url"], data=data, timeout=30)
            if r.status_code in (400, 401):
                raise NeedsLogin(f"Accesso scaduto, rifai il login ({r.text[:200]})")
            r.raise_for_status()
            self._store(r.json())
            return self._tokens["access_token"]

    def invalidate(self) -> None:
        with self._lock:
            if self._tokens:
                self._tokens["expires_at"] = 0

    def logout(self) -> None:
        self._tokens = {}
        self.vault.delete_tokens(self.name)

    def login(self, timeout: int = 300, open_browser=webbrowser.open) -> None:
        """Bloccante: da chiamare in un thread. Solleva eccezione se l'utente non completa."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()) \
            .rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" not in q and "error" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in q and q.get("state", [""])[0] == state
                page = _DONE_PAGE.format(
                    title="Accesso completato" if ok else "Accesso non riuscito",
                    msg="Puoi chiudere questa finestra e tornare a SaveSync." if ok
                    else urllib.parse.unquote(q.get("error_description", q.get("error", [""]))[0]))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode())

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 1
        redirect = f"http://{self.p['redirect_host']}:{server.server_port}"
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect,
                  "scope": " ".join(self.p["scopes"]), "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256", **self.p["extra"]}
        open_browser(self.p["auth_url"] + "?" + urllib.parse.urlencode(params))
        deadline = time.time() + timeout
        try:
            while not result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not result:
            raise TimeoutError("Login non completato in tempo")
        if result.get("state") != state or "code" not in result:
            raise PermissionError(result.get("error_description") or result.get("error")
                                  or "risposta non valida")
        data = {"grant_type": "authorization_code", "code": result["code"],
                "redirect_uri": redirect, "client_id": self.client_id, "code_verifier": verifier}
        if self.client_secret:
            data["client_secret"] = self.client_secret
        r = requests.post(self.p["token_url"], data=data, timeout=30)
        if not r.ok:
            raise PermissionError(f"Scambio del codice fallito: {r.text[:300]}")
        self._tokens = {}
        self._store(r.json())
