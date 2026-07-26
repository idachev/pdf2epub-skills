"""Make the plugin's scripts importable without installing anything."""

import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "plugins/pdf2epub/skills"
SCRIPTS_DIR = SKILLS_DIR / "pdf2epub/scripts"
TRANSLATE_SCRIPTS_DIR = SKILLS_DIR / "epub-translate/scripts"
for path in (SCRIPTS_DIR, TRANSLATE_SCRIPTS_DIR):
    sys.path.insert(0, str(path))
