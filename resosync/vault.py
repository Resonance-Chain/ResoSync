"""Segreti (password, chiavi S3, passphrase) nel portachiavi di sistema.

Su Windows finiscono in Gestione credenziali, su macOS nel Portachiavi. Su Linux e Steam
Deck in un file della cartella dell'app leggibile solo dal tuo utente: il portachiavi
(KWallet/GNOME) non è attivo in modalità gioco né per i servizi di sistema.
I token OAuth sono troppo lunghi per Gestione credenziali di Windows (max 2560 byte), quindi
vanno in un file cifrato la cui chiave sta nel portachiavi.
"""
import json
import os
import sys
from pathlib import Path

SERVICE = "ResoSync"
LEGACY_SERVICE = "SaveSync"  # nome del progetto fino alla 0.4.0


class Vault:
    def __init__(self, app_dir: Path):
        self.app_dir = Path(app_dir)
        self._fallback_file = self.app_dir / "secrets.json"
        self._keyring = None
        # Linux/Steam Deck: il portachiavi (KWallet/GNOME) non è attivo in modalità gioco né
        # per i servizi di sistema, quindi file protetto (leggibile solo dal tuo utente).
        from .config import env
        linux = sys.platform.startswith("linux") and not env("USE_KEYRING")
        if not env("NO_KEYRING") and not linux:
            try:
                import keyring
                from keyring.backends.fail import Keyring as FailKeyring
                if not isinstance(keyring.get_keyring(), FailKeyring):
                    self._keyring = keyring
            except Exception:
                self._keyring = None

    @property
    def secure(self) -> bool:
        return self._keyring is not None

    # ---------- segreti brevi ----------
    def get(self, name: str) -> str:
        if self._keyring:
            try:
                value = self._keyring.get_password(SERVICE, name)
                if value is None:
                    # salvato quando il progetto si chiamava SaveSync: lo sposto sotto il nome nuovo
                    value = self._keyring.get_password(LEGACY_SERVICE, name)
                    if value:
                        self._keyring.set_password(SERVICE, name, value)
                return value or ""
            except Exception:
                pass
        return self._read_fallback().get(name, "")

    def set(self, name: str, value: str) -> None:
        if not value:
            return self.delete(name)
        if self._keyring:
            try:
                self._keyring.set_password(SERVICE, name, value)
                return
            except Exception:
                pass
        data = self._read_fallback()
        data[name] = value
        self._write_fallback(data)

    def delete(self, name: str) -> None:
        if self._keyring:
            try:
                self._keyring.delete_password(SERVICE, name)
            except Exception:
                pass
        data = self._read_fallback()
        if data.pop(name, None) is not None:
            self._write_fallback(data)

    def _read_fallback(self) -> dict:
        try:
            return json.loads(self._fallback_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_fallback(self, data: dict) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._fallback_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data))
        os.replace(tmp, self._fallback_file)

    # ---------- token OAuth (lunghi) ----------
    def _fernet(self):
        from cryptography.fernet import Fernet
        key = self.get("token-key")
        if not key:
            key = Fernet.generate_key().decode()
            self.set("token-key", key)
        return Fernet(key.encode())

    def load_tokens(self, name: str) -> dict:
        p = self.app_dir / "tokens" / f"{name}.bin"
        if not p.exists():
            return {}
        try:
            return json.loads(self._fernet().decrypt(p.read_bytes()))
        except Exception:
            return {}  # chiave persa o file rovinato: si rifà il login

    def save_tokens(self, name: str, tokens: dict) -> None:
        p = self.app_dir / "tokens" / f"{name}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._fernet().encrypt(json.dumps(tokens).encode()))

    def delete_tokens(self, name: str) -> None:
        (self.app_dir / "tokens" / f"{name}.bin").unlink(missing_ok=True)
