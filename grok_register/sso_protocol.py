"""Back-compat: `grok_register.sso_protocol` → `grok_register.sso.protocol`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.sso.protocol")
sys.modules[__name__] = _impl
