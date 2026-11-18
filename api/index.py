"""Vercel Edge/Serverless entrypoint.

Vercel imports this module and calls the ASGI `app`. The engine is stateless
per request, so it runs happily as a serverless function.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SYNTH_ALWAYS_ON", "true")

from synthesizer.api import app  # noqa: E402,F401

# Vercel's Python runtime looks for `app` (ASGI) or `handler`.
