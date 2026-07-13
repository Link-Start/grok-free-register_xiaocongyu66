# proxy-worker (Go)

High-concurrency proxy connectivity tester for `grok-free-register`.

Python still owns registration / Turnstile / email. This binary only accelerates
**bulk proxy testing** (and can later grow scrape endpoints).

## Build

```bash
bash scripts/build-native.sh
# or
cd native/proxy-worker && go build -o proxy-worker .
```

Binary is written to `native/proxy-worker/proxy-worker` (gitignored if large).

## CLI test mode

```bash
printf '%s' '{
  "candidates": ["http://1.2.3.4:8080", "socks5://u:p@h:1080"],
  "test_urls": ["https://accounts.x.ai/sign-up?redirect=grok-com"],
  "timeout_sec": 10,
  "workers": 64,
  "accept_status": [[200, 399]],
  "max_active": 20
}' | ./proxy-worker test
```

## HTTP server mode

```bash
./proxy-worker serve --host 127.0.0.1 --port 18765
curl -s http://127.0.0.1:18765/healthz
curl -s -X POST http://127.0.0.1:18765/v1/test -d @req.json
```

## Python integration

Set either:

```env
PROXY_WORKER_BIN=native/proxy-worker/proxy-worker
# or
PROXY_WORKER_URL=http://127.0.0.1:18765
PROXY_WORKER_ENGINE=auto   # auto|go|python
```

When `auto` and a Go binary/URL is available, `proxy_auto.test_candidates` uses Go;
otherwise it falls back to the Python thread pool.
