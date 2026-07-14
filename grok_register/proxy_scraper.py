"""Back-compat: `grok_register.proxy_scraper` → `grok_register.proxy.scraper`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.proxy.scraper")
sys.modules[__name__] = _impl
