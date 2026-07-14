"""Back-compat: `grok_register.account_inventory` → `grok_register.inventory.accounts`."""
from __future__ import annotations

import importlib
import sys

_impl = importlib.import_module("grok_register.inventory.accounts")
sys.modules[__name__] = _impl
