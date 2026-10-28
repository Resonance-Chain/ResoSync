"""Configurazione dell'app: impostazioni generali + elenco dei giochi da sincronizzare."""
import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .backends import replace_retry
from .vault import Vault

APP_DIR = Path(os.environ.get("SAVESYNC_HOME", Path.home() / ".savesync"))

DEFAULT_EXCLUDES = ["*.tmp", "*.temp", "~*", "*.lock", "desktop.ini", "Thumbs.db", ".DS_Store"]

# Questi non finiscono mai in config.json: stanno nel portachiavi di sistema
SECRET_FIELDS = ("s3_secret_key", "gdrive_client_secret", "passphrase", "github_token")


def slugify(name: str) -> str:
    """ID stabile del gioco, uguale su tutti i PC se il nome è lo stesso."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "gioco"


def process_names(text: str) -> set:
    """'EldenRing.exe, eldenring' -> {'eldenring'}: niente maiuscole né .exe."""
    out = set()
    for p in re.split(r"[,;]", text or ""):
        p = p.strip().lower()
        if p.endswith(".exe"):
            p = p[:-4]
        if p:
            out.add(p)
    return out


@dataclass
class Game:
    id: str
    name: str
    path: str
    patterns: list = field(default_factory=lambda: ["*"])
    excludes: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    enabled: bool = True
    process: str = ""      # eseguibili del gioco, separati da virgola (es. eldenring.exe)
    launch: str = ""       # comando o URL per avviarlo (es. steam://rungameid/1245620)
    install_dir: str = ""  # cartella d'installazione: qualsiasi exe lì dentro = "in gioco"
    mode: str = "sync"     # sync = tra i PC | backup = solo copia sul cloud (gioco col suo cloud)
    max_cloud_mb: int = 0  # limite spazio delle versioni sul cloud (0 = nessun limite)


@dataclass
class Settings:
    backend: str = "folder"            # folder | s3 | gdrive | onedrive
    folder_path: str = ""              # es. cartella di Google Drive / OneDrive / Dropbox
    s3_bucket: str = ""
    s3_prefix: str = "savesync"
    s3_endpoint: str = ""              # vuoto = AWS; altrimenti R2, Backblaze B2, MinIO...
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    gdrive_client_id: str = ""
    gdrive_client_secret: str = ""
    onedrive_client_id: str = ""
    encrypt: bool = False              # cifra tutto prima di mandarlo sul cloud
    passphrase: str = ""
    device_name: str = field(default_factory=socket.gethostname)
    auto_sync: bool = True
    poll_interval: int = 60            # secondi tra un controllo completo e l'altro
    debounce_seconds: int = 5          # attesa dopo l'ultima modifica prima di caricare
    max_versions: int = 10             # versioni vecchie tenute per ogni file
    propagate_deletes: bool = False    # se True, cancellare un file lo cancella anche dal cloud
    sync_only_after_exit: bool = True  # mentre il gioco è aperto non carica nulla
    autostart: bool = False
    close_to_tray: bool = True
    snapshots_keep: int = 10           # istantanee della cartella tenute per gioco
    auto_update: bool = True
    update_repo: str = "Resonance-Chain/savesync"
    github_token: str = ""             # solo se il repository è privato


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    games: list = field(default_factory=list)
    app_dir: Path = APP_DIR

    def __post_init__(self):
        self.app_dir = Path(self.app_dir)
        self.vault = Vault(self.app_dir)

    @property
    def file(self) -> Path:
        return self.app_dir / "config.json"

    @classmethod
    def load(cls, app_dir: Path = APP_DIR) -> "Config":
        cfg = cls(app_dir=Path(app_dir))
        migrate = False
        if cfg.file.exists():
            data = json.loads(cfg.file.read_text(encoding="utf-8"))
            raw = data.get("settings", {})
            known = Settings.__dataclass_fields__
            cfg.settings = Settings(**{k: v for k, v in raw.items() if k in known})
            known_g = Game.__dataclass_fields__
            cfg.games = [Game(**{k: v for k, v in g.items() if k in known_g})
                         for g in data.get("games", [])]
            migrate = any(raw.get(k) for k in SECRET_FIELDS)  # versioni vecchie: segreti in chiaro
        for k in SECRET_FIELDS:
            if not getattr(cfg.settings, k):
                setattr(cfg.settings, k, cfg.vault.get(k))
        if migrate:
            cfg.save()
        return cfg

    def save(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
        settings = asdict(self.settings)
        for k in SECRET_FIELDS:
            self.vault.set(k, settings.pop(k))
        data = {"settings": settings, "games": [asdict(g) for g in self.games]}
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_retry(tmp, self.file)

    def get_game(self, game_id: str):
        return next((g for g in self.games if g.id == game_id), None)

    def find_game(self, text: str):
        """Per ID o per nome (usato da --launch)."""
        low = text.strip().lower()
        return self.get_game(text) or self.get_game(slugify(text)) or next(
            (g for g in self.games if g.name.strip().lower() == low), None)

    def cloud_identity(self) -> tuple:
        """Cosa identifica il cloud in uso: se cambia, è un altro cloud."""
        s = self.settings
        where = {"folder": os.path.normcase(os.path.normpath(s.folder_path or ".")),
                 "s3": (s.s3_endpoint, s.s3_bucket, s.s3_prefix),
                 "gdrive": s.gdrive_client_id, "onedrive": s.onedrive_client_id}.get(s.backend)
        return (s.backend, str(where))

    def reset_all_states(self) -> None:
        import shutil
        shutil.rmtree(self.app_dir / "state", ignore_errors=True)

    def is_configured(self) -> bool:
        s = self.settings
        return bool({"folder": s.folder_path, "s3": s.s3_bucket,
                     "gdrive": s.gdrive_client_id, "onedrive": s.onedrive_client_id}.get(s.backend))
