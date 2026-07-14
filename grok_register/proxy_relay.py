"""Back-compat: `grok_register.proxy_relay` → `grok_register.proxy.relay`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.proxy.relay")
sys.modules[__name__] = _impl
