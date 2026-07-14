"""Back-compat: `grok_register.proxy_auto` → `grok_register.proxy.auto`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.proxy.auto")
sys.modules[__name__] = _impl
