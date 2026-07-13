# Vendored Turnstile Solvers

This directory vendors third-party Cloudflare Turnstile solver servers for optional use by `grok-free-register`.

## theyka/

- Upstream: https://github.com/Theyka/Turnstile-Solver
- License: Creative Commons Attribution-NonCommercial 4.0 (see `theyka/LICENSE`)
- Default API: `GET /turnstile`, `GET /result` (port 5000 upstream)

## d3vin/

- Upstream: https://github.com/D3-vin/Turnstile-Solver-NEW
- License: not provided by upstream at vendor time; treat as third-party educational code
- Default API: same `/turnstile` + `/result` contract (port 5072 upstream)
- Extra modules: `browser_configs.py`, `db_results.py` (SQLite)

## Integration notes

- Runtime state (results DB/JSON, logs, copied proxies) lives under `logs/turnstile-solver/<engine>/`, not inside `vendor/`.
- Managed process entrypoint: `grok_register/turnstile_solver.py` (or `python -m grok_register.turnstile_solver`).
- Prefer `d3vin` on headless servers; `theyka` requires a User-Agent when `--headless True` unless using Camoufox.
