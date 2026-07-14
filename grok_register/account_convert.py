"""Back-compat: `grok_register.account_convert` → `grok_register.inventory.convert`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.inventory.convert")
sys.modules[__name__] = _impl
