import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["SAVESYNC_NO_KEYRING"] = "1"  # i test non devono toccare il portachiavi vero
