"""Back-compat: `grok_register.sso_export` → `grok_register.sso.export`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.sso.export")
sys.modules[__name__] = _impl
