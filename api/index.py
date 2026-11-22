"""Vercel entry point: serves the DataDoctor web app (see `datadoctor serve` for running it anywhere else)."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datadoctor.cli import connect  # noqa: E402
from datadoctor.config import load_config  # noqa: E402
from datadoctor.web import create_app  # noqa: E402

password = os.environ.get("DATADOCTOR_PASSWORD")
if not password:  # fail closed: a public URL must never serve DHIS2 data without a password
    raise RuntimeError("Set DATADOCTOR_PASSWORD in the Vercel project settings.")

config = os.environ.get("DATADOCTOR_CONFIG", "config/sierra-leone-demo.toml")
app = create_app(connect(), load_config(str(ROOT / config)), password=password)
