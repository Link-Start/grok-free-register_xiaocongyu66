"""Package entrypoints.

Preferred:
  python -m grok_register.sso.export convert --formats cpa --limit 100
  python -m grok_register.sso.auth_service --once

Back-compat:
  python -m grok_register.sso_export convert …
"""
from grok_register.sso.export import main

if __name__ == "__main__":
    raise SystemExit(main())
