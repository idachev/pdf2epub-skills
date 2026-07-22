"""Make the plugin's scripts importable without installing anything."""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "plugins/pdf2epub/skills/pdf2epub/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
