"""Back-compat: `grok_register.proxy_batch_test` → `grok_register.proxy.batch_test`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.proxy.batch_test")
sys.modules[__name__] = _impl
