"""Back-compat: `grok_register.auth_service_protocol` → `grok_register.sso.auth_service`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.sso.auth_service")
sys.modules[__name__] = _impl
