// register-worker: concurrent xAI account registration (Go).
//
// Pipeline per worker:
//  1. Solve Turnstile via HTTP solver (Theyka/D3-vin compatible)
//  2. Create mailbox via MoeMail API or custom email API
//  3. CreateEmailValidationCode (grpc-web)
//  4. Poll mailbox for code
//  5. VerifyEmailValidationCode (grpc-web)
//  6. POST /sign-up Next.js server action with turnstileToken
//  7. Follow set-cookie URL, extract sso
//
// Requires env (or JSON bootstrap from Python):
//   TURNSTILE_API_URL, SITE_KEY (optional auto-fetch later)
//   ACTION_ID, STATE_TREE  (from Python config fetch)
//   EMAIL_MODE=moemail|custom, MOEMAIL_*, EMAIL_*
//
// Usage:
//   register-worker run --workers 4 --target 10
//   register-worker serve --port 18766
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	siteURL   = "https://accounts.x.ai"
	userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

type Config struct {
	Workers          int    `json:"workers"`
	Target           int    `json:"target"`
	TurnstileAPI     string `json:"turnstile_api_url"`
	SiteKey          string `json:"site_key"`
	ActionID         string `json:"action_id"`
	StateTree        string `json:"state_tree"`
	EmailMode        string `json:"email_mode"`
	MoeMailAPI       string `json:"moemail_api"`
	MoeMailKey       string `json:"moemail_api_key"`
	MoeMailDomain    string `json:"moemail_domain"`
	EmailAPI         string `json:"email_api"`
	EmailDomain      string `json:"email_domain"`
	Proxy            string `json:"proxy"`
	OutputFile       string `json:"output_file"`
	TurnstileTimeout int    `json:"turnstile_timeout_sec"`
}

type Stats struct {
	Started   int64 `json:"started"`
	Success   int64 `json:"success"`
	Failed    int64 `json:"failed"`
	Running   int64 `json:"running"`
	InFlight  int64 `json:"inflight"`
	LastError string
	mu        sync.Mutex
}

func (s *Stats) setErr(msg string) {
	s.mu.Lock()
	s.LastError = msg
	s.mu.Unlock()
}

func (s *Stats) snapshot() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	return map[string]any{
		"started":    atomic.LoadInt64(&s.Started),
		"success":    atomic.LoadInt64(&s.Success),
		"failed":     atomic.LoadInt64(&s.Failed),
		"running":    atomic.LoadInt64(&s.Running),
		"inflight":   atomic.LoadInt64(&s.InFlight),
		"last_error": s.LastError,
	}
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: register-worker run|serve|version")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "run":
		os.Exit(cmdRun(os.Args[2:]))
	case "serve":
		os.Exit(cmdServe(os.Args[2:]))
	case "version":
		fmt.Println("register-worker 0.1.0")
	default:
		fmt.Fprintf(os.Stderr, "unknown command %s\n", os.Args[1])
		os.Exit(2)
	}
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	v := strings.TrimSpace(os.Getenv(k))
	if v == "" {
		return def
	}
	var n int
	_, err := fmt.Sscanf(v, "%d", &n)
	if err != nil {
		return def
	}
	return n
}

func loadConfigFromEnv() Config {
	return Config{
		Workers:          envInt("GO_REGISTER_WORKERS", 4),
		Target:           envInt("TARGET", 0),
		TurnstileAPI:     envOr("TURNSTILE_API_URL", "http://127.0.0.1:5072"),
		SiteKey:          envOr("SITE_KEY", ""),
		ActionID:         envOr("ACTION_ID", ""),
		StateTree:        envOr("STATE_TREE", ""),
		EmailMode:        envOr("EMAIL_MODE", "moemail"),
		MoeMailAPI:       strings.TrimRight(envOr("MOEMAIL_API", "https://moemail.app"), "/"),
		MoeMailKey:       envOr("MOEMAIL_API_KEY", ""),
		MoeMailDomain:    envOr("MOEMAIL_DOMAIN", ""),
		EmailAPI:         strings.TrimRight(envOr("EMAIL_API", "http://127.0.0.1:8080"), "/"),
		EmailDomain:      envOr("EMAIL_DOMAIN", ""),
		Proxy:            envOr("REGISTER_PROXY", envOr("HTTPS_PROXY", "")),
		OutputFile:       envOr("GO_REGISTER_OUTPUT", "keys/accounts.txt"),
		TurnstileTimeout: envInt("TURNSTILE_API_TIMEOUT", 120),
	}
}

func cmdRun(args []string) int {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	workers := fs.Int("workers", 0, "concurrency")
	target := fs.Int("target", 0, "stop after N successes")
	configPath := fs.String("config", "", "optional JSON config path")
	_ = fs.Parse(args)

	cfg := loadConfigFromEnv()
	if *configPath != "" {
		b, err := os.ReadFile(*configPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		if err := json.Unmarshal(b, &cfg); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
	}
	if *workers > 0 {
		cfg.Workers = *workers
	}
	if *target > 0 {
		cfg.Target = *target
	}
	if cfg.Workers <= 0 {
		cfg.Workers = 4
	}
	if cfg.SiteKey == "" || cfg.ActionID == "" || cfg.StateTree == "" {
		fmt.Fprintln(os.Stderr, "[!] SITE_KEY / ACTION_ID / STATE_TREE required (export from Python fetch_config or --config)")
		return 2
	}
	if cfg.EmailMode == "moemail" && cfg.MoeMailKey == "" {
		fmt.Fprintln(os.Stderr, "[!] MOEMAIL_API_KEY required for moemail mode")
		return 2
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	stats := &Stats{}
	atomic.StoreInt64(&stats.Running, 1)
	runPool(ctx, cfg, stats)
	atomic.StoreInt64(&stats.Running, 0)
	fmt.Fprintf(os.Stderr, "[*] done success=%d failed=%d\n",
		atomic.LoadInt64(&stats.Success), atomic.LoadInt64(&stats.Failed))
	return 0
}

func cmdServe(args []string) int {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	host := fs.String("host", "127.0.0.1", "")
	port := fs.Int("port", 18766, "")
	_ = fs.Parse(args)

	var (
		mu     sync.Mutex
		cancel context.CancelFunc
		stats  = &Stats{}
		cfg    = loadConfigFromEnv()
	)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "engine": "go-register"})
	})
	mux.HandleFunc("/v1/status", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, stats.snapshot())
	})
	mux.HandleFunc("/v1/start", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", 405)
			return
		}
		body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		var override Config
		_ = json.Unmarshal(body, &override)
		mu.Lock()
		defer mu.Unlock()
		if atomic.LoadInt64(&stats.Running) == 1 {
			writeJSON(w, map[string]any{"ok": false, "error": "already running"})
			return
		}
		cfg = loadConfigFromEnv()
		mergeConfig(&cfg, override)
		ctx, c := context.WithCancel(context.Background())
		cancel = c
		atomic.StoreInt64(&stats.Running, 1)
		atomic.StoreInt64(&stats.Success, 0)
		atomic.StoreInt64(&stats.Failed, 0)
		atomic.StoreInt64(&stats.Started, 0)
		go func() {
			runPool(ctx, cfg, stats)
			atomic.StoreInt64(&stats.Running, 0)
		}()
		writeJSON(w, map[string]any{"ok": true, "workers": cfg.Workers, "target": cfg.Target})
	})
	mux.HandleFunc("/v1/stop", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if cancel != nil {
			cancel()
			cancel = nil
		}
		writeJSON(w, map[string]any{"ok": true})
	})

	addr := fmt.Sprintf("%s:%d", *host, *port)
	fmt.Fprintf(os.Stderr, "[register-worker] http://%s\n", addr)
	return serve(addr, mux)
}

func mergeConfig(dst *Config, src Config) {
	if src.Workers > 0 {
		dst.Workers = src.Workers
	}
	if src.Target > 0 {
		dst.Target = src.Target
	}
	if src.TurnstileAPI != "" {
		dst.TurnstileAPI = src.TurnstileAPI
	}
	if src.SiteKey != "" {
		dst.SiteKey = src.SiteKey
	}
	if src.ActionID != "" {
		dst.ActionID = src.ActionID
	}
	if src.StateTree != "" {
		dst.StateTree = src.StateTree
	}
	if src.EmailMode != "" {
		dst.EmailMode = src.EmailMode
	}
	if src.MoeMailAPI != "" {
		dst.MoeMailAPI = src.MoeMailAPI
	}
	if src.MoeMailKey != "" {
		dst.MoeMailKey = src.MoeMailKey
	}
	if src.Proxy != "" {
		dst.Proxy = src.Proxy
	}
	if src.OutputFile != "" {
		dst.OutputFile = src.OutputFile
	}
}

func serve(addr string, h http.Handler) int {
	if err := http.ListenAndServe(addr, h); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func runPool(ctx context.Context, cfg Config, stats *Stats) {
	var wg sync.WaitGroup
	for i := 0; i < cfg.Workers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for {
				if ctx.Err() != nil {
					return
				}
				if cfg.Target > 0 && atomic.LoadInt64(&stats.Success) >= int64(cfg.Target) {
					return
				}
				atomic.AddInt64(&stats.InFlight, 1)
				atomic.AddInt64(&stats.Started, 1)
				err := registerOnce(ctx, cfg, id)
				atomic.AddInt64(&stats.InFlight, -1)
				if err != nil {
					atomic.AddInt64(&stats.Failed, 1)
					stats.setErr(err.Error())
					fmt.Fprintf(os.Stderr, "[W%d] fail: %v\n", id, err)
					select {
					case <-ctx.Done():
						return
					case <-time.After(2 * time.Second):
					}
					continue
				}
				n := atomic.AddInt64(&stats.Success, 1)
				fmt.Fprintf(os.Stderr, "[W%d] success #%d\n", id, n)
				if cfg.Target > 0 && n >= int64(cfg.Target) {
					return
				}
			}
		}(i)
	}
	wg.Wait()
}

func httpClient(proxy string) *http.Client {
	tr := &http.Transport{Proxy: nil, ForceAttemptHTTP2: false}
	if proxy != "" {
		if u, err := url.Parse(proxy); err == nil {
			tr.Proxy = http.ProxyURL(u)
		}
	}
	return &http.Client{Transport: tr, Timeout: 60 * time.Second}
}

func registerOnce(ctx context.Context, cfg Config, wid int) error {
	client := httpClient(cfg.Proxy)

	// 1) mailbox first — fail cheap before Turnstile (solver only when needed later)
	email, password, handle, err := createMailbox(ctx, client, cfg)
	if err != nil {
		return fmt.Errorf("mailbox: %w", err)
	}

	// 2) send + poll code (no captcha yet)
	if err := grpcCreateCode(ctx, client, email); err != nil {
		return fmt.Errorf("create code: %w", err)
	}
	code, err := pollCode(ctx, client, cfg, email, handle)
	if err != nil {
		return fmt.Errorf("poll code: %w", err)
	}
	if err := grpcVerifyCode(ctx, client, email, code); err != nil {
		return fmt.Errorf("verify: %w", err)
	}

	// 3) Turnstile only after mailbox+code succeed (on-demand solver usage)
	token, err := solveTurnstile(ctx, client, cfg)
	if err != nil {
		return fmt.Errorf("turnstile: %w", err)
	}

	// 4) signup + persist (accounts.txt only — no accounts.cpa.json)
	sso, err := signup(ctx, client, cfg, email, password, code, token)
	if err != nil {
		return fmt.Errorf("signup: %w", err)
	}
	if err := appendAccount(cfg.OutputFile, email, password, sso); err != nil {
		return err
	}
	return nil
}

func solveTurnstile(ctx context.Context, client *http.Client, cfg Config) (string, error) {
	base := strings.TrimRight(cfg.TurnstileAPI, "/")
	q := url.Values{}
	q.Set("url", siteURL+"/sign-up")
	q.Set("sitekey", cfg.SiteKey)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/turnstile?"+q.Encode(), nil)
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var created map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&created); err != nil {
		return "", err
	}
	taskID, _ := created["task_id"].(string)
	if taskID == "" {
		if v, ok := created["taskId"].(string); ok {
			taskID = v
		}
	}
	if taskID == "" {
		return "", fmt.Errorf("no task id: %v", created)
	}
	deadline := time.Now().Add(time.Duration(cfg.TurnstileTimeout) * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(800 * time.Millisecond):
		}
		rreq, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/result?id="+url.QueryEscape(taskID), nil)
		rresp, err := client.Do(rreq)
		if err != nil {
			continue
		}
		body, _ := io.ReadAll(rresp.Body)
		rresp.Body.Close()
		var result map[string]any
		if err := json.Unmarshal(body, &result); err != nil {
			// maybe plain string
			s := strings.Trim(string(body), "\"\n ")
			if s == "CAPTCHA_NOT_READY" || s == "processing" {
				continue
			}
			if len(s) > 20 && !strings.Contains(s, "CAPTCHA") {
				return s, nil
			}
			continue
		}
		if st, _ := result["status"].(string); st == "processing" || st == "CAPTCHA_NOT_READY" {
			continue
		}
		if v, ok := result["value"].(string); ok {
			if v == "CAPTCHA_FAIL" || v == "CAPTCHA_NOT_READY" {
				if v == "CAPTCHA_FAIL" {
					return "", errors.New("captcha fail")
				}
				continue
			}
			if len(v) > 20 {
				return v, nil
			}
		}
		if sol, ok := result["solution"].(map[string]any); ok {
			if t, ok := sol["token"].(string); ok && len(t) > 20 {
				return t, nil
			}
		}
		if t, ok := result["token"].(string); ok && len(t) > 20 {
			return t, nil
		}
	}
	return "", errors.New("turnstile timeout")
}

func createMailbox(ctx context.Context, client *http.Client, cfg Config) (email, password, handle string, err error) {
	password = randomPassword(16)
	switch strings.ToLower(cfg.EmailMode) {
	case "moemail":
		return createMoeMail(ctx, client, cfg, password)
	case "custom":
		local := randomAlpha(10)
		email = local + "@" + cfg.EmailDomain
		return email, password, email, nil
	default:
		return "", "", "", fmt.Errorf("unsupported EMAIL_MODE for go worker: %s (use moemail|custom)", cfg.EmailMode)
	}
}

// moeMailHeaders matches Python register._moemail_headers: MoeMail expects X-API-Key
// (Bearer returns 未授权 on moemail.072168.xyz and similar deployments).
func moeMailHeaders(cfg Config, jsonBody bool) http.Header {
	h := make(http.Header)
	h.Set("Accept", "application/json")
	h.Set("X-API-Key", cfg.MoeMailKey)
	// Some forks accept either; keep Bearer as secondary without breaking X-API-Key
	h.Set("Authorization", "Bearer "+cfg.MoeMailKey)
	h.Set("User-Agent", userAgent)
	if jsonBody {
		h.Set("Content-Type", "application/json")
	}
	return h
}

func parseMoeMailDomains(cfgJSON map[string]any) string {
	if s, ok := cfgJSON["emailDomains"].(string); ok && strings.TrimSpace(s) != "" {
		for _, part := range strings.Split(s, ",") {
			part = strings.TrimSpace(part)
			if part != "" {
				return part
			}
		}
	}
	if arr, ok := cfgJSON["emailDomains"].([]any); ok && len(arr) > 0 {
		if d, ok := arr[0].(string); ok && d != "" {
			return d
		}
	}
	if arr, ok := cfgJSON["domains"].([]any); ok && len(arr) > 0 {
		if d, ok := arr[0].(string); ok && d != "" {
			return d
		}
	}
	return ""
}

func createMoeMail(ctx context.Context, client *http.Client, cfg Config, password string) (string, string, string, error) {
	domain := strings.TrimSpace(cfg.MoeMailDomain)
	if domain == "" {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(cfg.MoeMailAPI, "/")+"/api/config", nil)
		req.Header = moeMailHeaders(cfg, false)
		resp, err := client.Do(req)
		if err != nil {
			return "", "", "", err
		}
		defer resp.Body.Close()
		var cfgJSON map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&cfgJSON)
		domain = parseMoeMailDomains(cfgJSON)
		if domain == "" {
			return "", "", "", errors.New("moemail: no domain")
		}
	}
	name := randomAlpha(8)
	payload := map[string]any{
		"name":       name,
		"domain":     domain,
		"expiryTime": envInt("MOEMAIL_EXPIRY_MS", 3600000),
	}
	b, _ := json.Marshal(payload)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(cfg.MoeMailAPI, "/")+"/api/emails/generate", bytes.NewReader(b))
	req.Header = moeMailHeaders(cfg, true)
	resp, err := client.Do(req)
	if err != nil {
		return "", "", "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return "", "", "", fmt.Errorf("moemail create invalid json: %s", truncate(string(body), 200))
	}
	if errMsg, ok := out["error"].(string); ok && errMsg != "" && out["email"] == nil && out["id"] == nil {
		return "", "", "", fmt.Errorf("moemail create failed: %s", errMsg)
	}
	email, _ := out["email"].(string)
	id, _ := out["id"].(string)
	if email == "" {
		if e, ok := out["address"].(string); ok {
			email = e
		}
	}
	if email == "" {
		return "", "", "", fmt.Errorf("moemail create failed: %v", out)
	}
	if id == "" {
		id = email
	}
	return email, password, id, nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func pollCode(ctx context.Context, client *http.Client, cfg Config, email, handle string) (string, error) {
	deadline := time.Now().Add(90 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(3 * time.Second):
		}
		var code string
		var err error
		if strings.ToLower(cfg.EmailMode) == "moemail" {
			code, err = pollMoeMail(ctx, client, cfg, handle)
		} else {
			code, err = pollCustom(ctx, client, cfg, email)
		}
		if err == nil && code != "" {
			return code, nil
		}
	}
	return "", errors.New("code timeout")
}

func pollMoeMail(ctx context.Context, client *http.Client, cfg Config, id string) (string, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(cfg.MoeMailAPI, "/")+"/api/emails/"+url.PathEscape(id), nil)
	req.Header = moeMailHeaders(cfg, false)
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	// search ABC-DEF or 6 alnum
	text := string(body)
	if m := regexpFindCode(text); m != "" {
		return m, nil
	}
	return "", errors.New("no code yet")
}

func pollCustom(ctx context.Context, client *http.Client, cfg Config, email string) (string, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, cfg.EmailAPI+"/check/"+url.PathEscape(email), nil)
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if err := json.Unmarshal(body, &out); err == nil {
		if c, ok := out["code"].(string); ok && c != "" {
			return strings.ReplaceAll(c, "-", ""), nil
		}
	}
	if m := regexpFindCode(string(body)); m != "" {
		return m, nil
	}
	return "", errors.New("no code yet")
}

func regexpFindCode(text string) string {
	// ABC-DEF
	for i := 0; i+7 <= len(text); i++ {
		// naive scan for pattern
	}
	// use simple contains loop with bytes
	up := strings.ToUpper(text)
	// find XXX-XXX
	for i := 0; i < len(up)-6; i++ {
		if isALNUM(up[i]) && isALNUM(up[i+1]) && isALNUM(up[i+2]) && up[i+3] == '-' &&
			isALNUM(up[i+4]) && isALNUM(up[i+5]) && isALNUM(up[i+6]) {
			return up[i:i+3] + up[i+4:i+7]
		}
	}
	// find 6 consecutive
	for i := 0; i < len(up)-5; i++ {
		ok := true
		for j := 0; j < 6; j++ {
			if !isALNUM(up[i+j]) {
				ok = false
				break
			}
		}
		if ok {
			// avoid long hex strings by requiring surrounding non-alnum roughly
			return up[i : i+6]
		}
	}
	return ""
}

func isALNUM(b byte) bool {
	return (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

func pbVarint(n int) []byte {
	var parts []byte
	for n > 0x7f {
		parts = append(parts, byte((n&0x7f)|0x80))
		n >>= 7
	}
	parts = append(parts, byte(n))
	return parts
}

func pbStr(fid int, val string) []byte {
	vb := []byte(val)
	out := []byte{byte((fid << 3) | 2)}
	out = append(out, pbVarint(len(vb))...)
	out = append(out, vb...)
	return out
}

func grpcFrame(inner []byte) []byte {
	frame := make([]byte, 5+len(inner))
	frame[0] = 0
	binary.BigEndian.PutUint32(frame[1:5], uint32(len(inner)))
	copy(frame[5:], inner)
	return frame
}

func grpcCreateCode(ctx context.Context, client *http.Client, email string) error {
	inner := pbStr(1, email)
	frame := grpcFrame(inner)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost,
		siteURL+"/auth_mgmt.AuthManagement/CreateEmailValidationCode", bytes.NewReader(frame))
	req.Header.Set("content-type", "application/grpc-web+proto")
	req.Header.Set("x-grpc-web", "1")
	req.Header.Set("x-user-agent", "connect-es/2.1.1")
	req.Header.Set("origin", siteURL)
	req.Header.Set("referer", siteURL+"/sign-up?redirect=grok-com")
	req.Header.Set("user-agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	st := resp.Header.Get("grpc-status")
	if st != "" && st != "0" {
		return fmt.Errorf("grpc-status %s", st)
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("http %d", resp.StatusCode)
	}
	return nil
}

func grpcVerifyCode(ctx context.Context, client *http.Client, email, code string) error {
	inner := append(pbStr(1, email), pbStr(2, code)...)
	frame := grpcFrame(inner)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost,
		siteURL+"/auth_mgmt.AuthManagement/VerifyEmailValidationCode", bytes.NewReader(frame))
	req.Header.Set("content-type", "application/grpc-web+proto")
	req.Header.Set("x-grpc-web", "1")
	req.Header.Set("x-user-agent", "connect-es/2.1.1")
	req.Header.Set("origin", siteURL)
	req.Header.Set("referer", siteURL+"/sign-up?redirect=grok-com")
	req.Header.Set("user-agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	st := resp.Header.Get("grpc-status")
	if st != "" && st != "0" {
		return fmt.Errorf("grpc-status %s", st)
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("http %d", resp.StatusCode)
	}
	return nil
}

func signup(ctx context.Context, client *http.Client, cfg Config, email, password, code, token string) (string, error) {
	given := []string{"James", "John", "Robert", "Michael", "William", "David"}
	family := []string{"Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia"}
	payload := []map[string]any{{
		"emailValidationCode": code,
		"createUserAndSessionRequest": map[string]any{
			"email":              email,
			"givenName":          given[intn(len(given))],
			"familyName":         family[intn(len(family))],
			"clearTextPassword":  password,
			"tosAcceptedVersion": "$undefined",
		},
		"turnstileToken":        token,
		"promptOnDuplicateEmail": true,
	}}
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, siteURL+"/sign-up", bytes.NewReader(body))
	req.Header.Set("accept", "text/x-component")
	req.Header.Set("content-type", "text/plain;charset=UTF-8")
	req.Header.Set("next-router-state-tree", cfg.StateTree)
	req.Header.Set("next-action", cfg.ActionID)
	req.Header.Set("origin", siteURL)
	req.Header.Set("referer", siteURL+"/sign-up?redirect=grok-com")
	req.Header.Set("user-agent", userAgent)
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	text, _ := io.ReadAll(resp.Body)
	if bytes.Contains(bytes.ToLower(text), []byte("rate")) && bytes.Contains(bytes.ToLower(text), []byte("limit")) {
		return "", errors.New("rate limited")
	}
	// find set-cookie URL or sso in body
	sso := extractSSO(resp, text)
	if sso != "" {
		return sso, nil
	}
	// try set-cookie links
	if u := findSetCookieURL(string(text)); u != "" {
		sso, err = followSetCookie(ctx, client, u)
		if err == nil && sso != "" {
			return sso, nil
		}
	}
	return "", fmt.Errorf("no sso status=%d body=%d", resp.StatusCode, len(text))
}

func extractSSO(resp *http.Response, body []byte) string {
	for _, c := range resp.Cookies() {
		if c.Name == "sso" && c.Value != "" {
			return c.Value
		}
	}
	// raw set-cookie headers
	for _, h := range resp.Header.Values("Set-Cookie") {
		if strings.HasPrefix(h, "sso=") {
			part := strings.SplitN(h, ";", 2)[0]
			return strings.TrimPrefix(part, "sso=")
		}
	}
	// body search
	s := string(body)
	if i := strings.Index(s, "sso="); i >= 0 {
		// weak fallback
	}
	return ""
}

func findSetCookieURL(text string) string {
	// look for auth.*.com/set-cookie
	idx := strings.Index(text, "set-cookie?q=")
	if idx < 0 {
		return ""
	}
	// walk back to http
	start := idx
	for start > 0 && text[start-1] != '"' && text[start-1] != '\'' && text[start-1] != ' ' {
		start--
	}
	end := idx
	for end < len(text) && text[end] != '"' && text[end] != '\'' && text[end] != ' ' && text[end] != '<' {
		end++
	}
	u := text[start:end]
	u = strings.ReplaceAll(u, "\\u0026", "&")
	u = strings.ReplaceAll(u, "\\/", "/")
	if strings.HasPrefix(u, "http") {
		return u
	}
	return ""
}

func followSetCookie(ctx context.Context, client *http.Client, rawURL string) (string, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	req.Header.Set("user-agent", userAgent)
	req.Header.Set("referer", siteURL+"/sign-up?redirect=grok-com")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	return extractSSO(resp, nil), nil
}

func appendAccount(path, email, password, sso string) error {
	if path == "" {
		path = "keys/accounts.txt"
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	// Match Python legacy format: email:password:sso (CLIProxy convert / inventory)
	line := fmt.Sprintf("%s:%s:%s\n", email, password, sso)
	_, err = f.WriteString(line)
	// Also append protocol audit line to accounts.protocol.log
	logPath := filepath.Join(filepath.Dir(path), "accounts.protocol.log")
	if lf, e2 := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); e2 == nil {
		_, _ = lf.WriteString(fmt.Sprintf("%s\t%s\t%s\n", time.Now().UTC().Format(time.RFC3339), email, sso[:min(24, len(sso))]))
		_ = lf.Close()
	}
	return err
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func randomPassword(n int) string {
	const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%"
	b := make([]byte, n)
	for i := range b {
		b[i] = chars[intn(len(chars))]
	}
	return string(b)
}

func randomAlpha(n int) string {
	const chars = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, n)
	for i := range b {
		b[i] = chars[intn(len(chars))]
	}
	return string(b)
}

func intn(n int) int {
	v, err := rand.Int(rand.Reader, big.NewInt(int64(n)))
	if err != nil {
		return 0
	}
	return int(v.Int64())
}

// silence unused import if any
var _ = base64.StdEncoding
