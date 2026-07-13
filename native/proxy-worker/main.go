// proxy-worker: high-concurrency proxy tester for grok-free-register.
//
// Modes:
//
//	proxy-worker test  - read JSON from stdin, write JSON to stdout (blocking)
//	proxy-worker batch - async batch job: progress/results on disk (dashboard-friendly)
//	proxy-worker serve - HTTP server POST /v1/test
//
// Batch is the preferred path for large scans: Go owns concurrency + progress
// files; Python/browser only start the process and poll progress JSON.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const defaultUA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

type StatusRange struct {
	Start int `json:"start"`
	End   int `json:"end"`
}

// Accept flexible [[200,399]] or [{"start":200,"end":399}]
type TestRequest struct {
	Candidates   []string        `json:"candidates"`
	TestURLs     []string        `json:"test_urls"`
	TimeoutSec   int             `json:"timeout_sec"`
	Workers      int             `json:"workers"`
	AcceptStatus json.RawMessage `json:"accept_status"`
	MaxActive    int             `json:"max_active"`
	UserAgent    string          `json:"user_agent"`
}

type TestResult struct {
	Candidate  string `json:"candidate"`
	Proxy      string `json:"proxy"`
	OK         bool   `json:"ok"`
	LatencyMs  int    `json:"latency_ms"`
	StatusCode *int   `json:"status_code"`
	Error      string `json:"error"`
}

type TestResponse struct {
	Results []TestResult `json:"results"`
	Engine  string       `json:"engine"`
	Elapsed float64      `json:"elapsed_sec"`
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: proxy-worker test|batch|serve [flags]")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "test":
		os.Exit(runTest(os.Args[2:]))
	case "batch":
		os.Exit(runBatch(os.Args[2:]))
	case "serve":
		os.Exit(runServe(os.Args[2:]))
	case "version", "-v", "--version":
		fmt.Println("proxy-worker 0.2.0")
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		os.Exit(2)
	}
}

// BatchJob is written by Python control plane; Go owns execution + progress.
type BatchJob struct {
	Candidates   []string        `json:"candidates"`
	TestURLs     []string        `json:"test_urls"`
	TimeoutSec   int             `json:"timeout_sec"`
	Workers      int             `json:"workers"`
	AcceptStatus json.RawMessage `json:"accept_status"`
	MaxActive    int             `json:"max_active"`
	UserAgent    string          `json:"user_agent"`
	Sources      map[string]string `json:"sources"`
	Counts       map[string]int    `json:"counts"`
	UsePublic    bool              `json:"use_public"`
	UseManual    bool              `json:"use_manual"`
	UseActive    bool              `json:"use_active"`
}

type TopHit struct {
	Proxy      string `json:"proxy"`
	Candidate  string `json:"candidate"`
	LatencyMs  int    `json:"latency_ms"`
	StatusCode *int   `json:"status_code"`
	Source     string `json:"source"`
}

type BatchProgress struct {
	Running     bool           `json:"running"`
	Engine      string         `json:"engine"`
	PID         int            `json:"pid"`
	StartedAt   float64        `json:"started_at"`
	FinishedAt  float64        `json:"finished_at"`
	UpdatedAt   float64        `json:"updated_at"`
	UpdatedISO  string         `json:"updated_at_iso"`
	Message     string         `json:"message"`
	Error       string         `json:"error"`
	Workers     int            `json:"workers"`
	TimeoutSec  int            `json:"timeout_sec"`
	TestURLs    []string       `json:"test_urls"`
	Total       int            `json:"total"`
	Tested      int            `json:"tested"`
	OK          int            `json:"ok"`
	Fail        int            `json:"fail"`
	Shard       int            `json:"shard"`
	Shards      int            `json:"shards"`
	UsePublic   bool           `json:"use_public"`
	Counts      map[string]int `json:"counts"`
	Top         []TopHit       `json:"top"`
	ActiveFile  string         `json:"active_file"`
	ReportFile  string         `json:"report_file"`
	ElapsedSec  float64        `json:"elapsed_sec"`
	RPS         float64        `json:"rps"`
	StateFile   string         `json:"state_file"`
}

func runBatch(args []string) int {
	fs := flag.NewFlagSet("batch", flag.ExitOnError)
	jobPath := fs.String("job", "", "path to job JSON (required)")
	progressPath := fs.String("progress", "logs/proxy-batch-job.json", "progress JSON path")
	activePath := fs.String("active", "logs/proxy-auto-active.txt", "write OK proxies here")
	reportPath := fs.String("report", "logs/proxy-batch-xai-report.json", "final report JSON")
	progressEvery := fs.Int("progress-every", 50, "write progress every N completions")
	_ = fs.Parse(args)
	if strings.TrimSpace(*jobPath) == "" {
		fmt.Fprintln(os.Stderr, "batch requires --job path.json")
		return 2
	}

	raw, err := os.ReadFile(*jobPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read job: %v\n", err)
		return 1
	}
	var job BatchJob
	if err := json.Unmarshal(raw, &job); err != nil {
		fmt.Fprintf(os.Stderr, "job json: %v\n", err)
		return 1
	}
	if len(job.Candidates) == 0 {
		_ = writeProgress(*progressPath, BatchProgress{
			Running:    false,
			Engine:     "go",
			PID:        os.Getpid(),
			Message:    "没有候选代理",
			FinishedAt: float64(time.Now().Unix()),
			UpdatedAt:  float64(time.Now().Unix()),
			StateFile:  *progressPath,
		})
		return 0
	}

	workers := job.Workers
	if workers <= 0 {
		workers = 128
	}
	if workers > 4096 {
		workers = 4096
	}
	timeoutSec := job.TimeoutSec
	if timeoutSec <= 0 {
		timeoutSec = 5
	}
	testURLs := job.TestURLs
	if len(testURLs) == 0 {
		testURLs = []string{"https://accounts.x.ai/sign-up?redirect=grok-com"}
	}
	ranges := parseAcceptStatus(job.AcceptStatus)
	if len(ranges) == 0 {
		ranges = []StatusRange{{200, 399}}
	}
	ua := job.UserAgent
	if ua == "" {
		ua = defaultUA
	}

	// de-dupe
	seen := make(map[string]struct{}, len(job.Candidates))
	candidates := make([]string, 0, len(job.Candidates))
	for _, c := range job.Candidates {
		c = strings.TrimSpace(c)
		if c == "" {
			continue
		}
		if _, ok := seen[c]; ok {
			continue
		}
		seen[c] = struct{}{}
		candidates = append(candidates, c)
	}
	total := len(candidates)
	started := time.Now()
	startUnix := float64(started.Unix()) + float64(started.Nanosecond())/1e9

	// progress writer
	var (
		tested    int64
		okCount   int64
		failCount int64
		stopFlag  int32
		lastWrite int64 // unix milli
		progMu    sync.Mutex
		topHits   []TopHit
	)
	every := *progressEvery
	if every < 10 {
		every = 10
	}

	writeProg := func(running bool, msg, errMsg string) {
		now := time.Now()
		t := atomic.LoadInt64(&tested)
		okn := atomic.LoadInt64(&okCount)
		fn := atomic.LoadInt64(&failCount)
		elapsed := now.Sub(started).Seconds()
		rps := 0.0
		if elapsed > 0 {
			rps = float64(t) / elapsed
		}
		progMu.Lock()
		topCopy := append([]TopHit(nil), topHits...)
		progMu.Unlock()
		// sort top by latency
		sort.Slice(topCopy, func(i, j int) bool {
			return topCopy[i].LatencyMs < topCopy[j].LatencyMs
		})
		if len(topCopy) > 30 {
			topCopy = topCopy[:30]
		}
		fin := 0.0
		if !running {
			fin = float64(now.Unix()) + float64(now.Nanosecond())/1e9
		}
		_ = writeProgress(*progressPath, BatchProgress{
			Running:    running,
			Engine:     "go",
			PID:        os.Getpid(),
			StartedAt:  startUnix,
			FinishedAt: fin,
			UpdatedAt:  float64(now.Unix()) + float64(now.Nanosecond())/1e9,
			UpdatedISO: now.Format(time.RFC3339),
			Message:    msg,
			Error:      errMsg,
			Workers:    workers,
			TimeoutSec: timeoutSec,
			TestURLs:   testURLs,
			Total:      total,
			Tested:     int(t),
			OK:         int(okn),
			Fail:       int(fn),
			UsePublic:  job.UsePublic,
			Counts:     job.Counts,
			Top:        topCopy,
			ActiveFile: *activePath,
			ReportFile: *reportPath,
			ElapsedSec: elapsed,
			RPS:        rps,
			StateFile:  *progressPath,
		})
	}

	writeProg(true, fmt.Sprintf("Go 测活启动 %d 候选 · 并发 %d · 超时 %ds", total, workers, timeoutSec), "")

	jobs := make(chan int, workers*2)
	results := make([]TestResult, total)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for idx := range jobs {
				if atomic.LoadInt32(&stopFlag) == 1 {
					results[idx] = TestResult{Candidate: candidates[idx], OK: false, Error: "cancelled"}
					atomic.AddInt64(&tested, 1)
					atomic.AddInt64(&failCount, 1)
					continue
				}
				r := testOne(candidates[idx], testURLs, time.Duration(timeoutSec)*time.Second, ranges, ua)
				results[idx] = r
				n := atomic.AddInt64(&tested, 1)
				if r.OK {
					atomic.AddInt64(&okCount, 1)
					src := ""
					if job.Sources != nil {
						src = job.Sources[candidates[idx]]
					}
					progMu.Lock()
					topHits = append(topHits, TopHit{
						Proxy:      r.Proxy,
						Candidate:  r.Candidate,
						LatencyMs:  r.LatencyMs,
						StatusCode: r.StatusCode,
						Source:     src,
					})
					// keep a bounded working set
					if len(topHits) > 200 {
						sort.Slice(topHits, func(i, j int) bool { return topHits[i].LatencyMs < topHits[j].LatencyMs })
						topHits = topHits[:80]
					}
					progMu.Unlock()
					if job.MaxActive > 0 && int(atomic.LoadInt64(&okCount)) >= job.MaxActive {
						atomic.StoreInt32(&stopFlag, 1)
					}
				} else {
					atomic.AddInt64(&failCount, 1)
				}
				// throttle progress disk writes
				nowMs := time.Now().UnixMilli()
				prev := atomic.LoadInt64(&lastWrite)
				if int(n)%every == 0 || nowMs-prev > 800 {
					if atomic.CompareAndSwapInt64(&lastWrite, prev, nowMs) || int(n)%every == 0 {
						atomic.StoreInt64(&lastWrite, nowMs)
						okn := atomic.LoadInt64(&okCount)
						fn := atomic.LoadInt64(&failCount)
						elapsed := time.Since(started).Seconds()
						rps := 0.0
						if elapsed > 0 {
							rps = float64(n) / elapsed
						}
						remain := total - int(n)
						eta := 0
						if rps > 0 {
							eta = int(float64(remain) / rps)
						}
						writeProg(true, fmt.Sprintf(
							"Go 测活中 %d/%d · %d✓/%d✗ · 并发 %d · %.1fs · ~%.1f/s · 剩余约 %ds",
							n, total, okn, fn, workers, elapsed, rps, eta,
						), "")
					}
				}
			}
		}()
	}

	for i := range candidates {
		if atomic.LoadInt32(&stopFlag) == 1 {
			for j := i; j < total; j++ {
				results[j] = TestResult{Candidate: candidates[j], OK: false, Error: "cancelled"}
				atomic.AddInt64(&tested, 1)
				atomic.AddInt64(&failCount, 1)
			}
			break
		}
		jobs <- i
	}
	close(jobs)
	wg.Wait()

	// collect actives
	type pair struct {
		proxy string
		lat   int
	}
	actives := make([]pair, 0)
	for _, r := range results {
		if r.OK && r.Proxy != "" {
			actives = append(actives, pair{r.Proxy, r.LatencyMs})
		}
	}
	sort.Slice(actives, func(i, j int) bool { return actives[i].lat < actives[j].lat })
	if job.MaxActive > 0 && len(actives) > job.MaxActive {
		actives = actives[:job.MaxActive]
	}
	// write active file
	_ = os.MkdirAll(filepath.Dir(*activePath), 0o755)
	var b strings.Builder
	for _, a := range actives {
		b.WriteString(a.proxy)
		b.WriteByte('\n')
	}
	_ = os.WriteFile(*activePath, []byte(b.String()), 0o600)

	// final report
	okn := int(atomic.LoadInt64(&okCount))
	fn := int(atomic.LoadInt64(&failCount))
	elapsed := time.Since(started).Seconds()
	rps := 0.0
	if elapsed > 0 {
		rps = float64(total) / elapsed
	}
	report := map[string]any{
		"updated_at":    time.Now().Format(time.RFC3339),
		"engine":        "go",
		"elapsed_sec":   elapsed,
		"workers":       workers,
		"timeout_sec":   timeoutSec,
		"test_urls":     testURLs,
		"total":         total,
		"ok":            okn,
		"fail":          fn,
		"active_count":  len(actives),
		"active_file":   *activePath,
		"use_public":    job.UsePublic,
		"counts":        job.Counts,
		"rps":           rps,
	}
	_ = os.MkdirAll(filepath.Dir(*reportPath), 0o755)
	if rb, err := json.MarshalIndent(report, "", "  "); err == nil {
		_ = os.WriteFile(*reportPath, append(rb, '\n'), 0o600)
	}

	msg := fmt.Sprintf(
		"Go 测活完成：%d/%d 可用 · 并发 %d · 超时 %ds · %.1fs (~%.1f/s)",
		okn, total, workers, timeoutSec, elapsed, rps,
	)
	if okn == 0 {
		msg += " · 本轮无可用代理（公共免费节点多数已死属正常）"
	}
	writeProg(false, msg, "")
	fmt.Fprintln(os.Stderr, msg)
	return 0
}

func writeProgress(path string, p BatchProgress) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func runTest(args []string) int {
	fs := flag.NewFlagSet("test", flag.ExitOnError)
	_ = fs.Parse(args)
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read stdin: %v\n", err)
		return 1
	}
	var req TestRequest
	if err := json.Unmarshal(data, &req); err != nil {
		fmt.Fprintf(os.Stderr, "invalid json: %v\n", err)
		return 1
	}
	resp := doTest(req)
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(resp); err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		return 1
	}
	return 0
}

func runServe(args []string) int {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	host := fs.String("host", "127.0.0.1", "listen host")
	port := fs.Int("port", 18765, "listen port")
	_ = fs.Parse(args)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"engine":"go"}`))
	})
	mux.HandleFunc("/v1/test", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		defer r.Body.Close()
		body, err := io.ReadAll(io.LimitReader(r.Body, 64<<20))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		var req TestRequest
		if err := json.Unmarshal(body, &req); err != nil {
			http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
			return
		}
		resp := doTest(req)
		w.Header().Set("Content-Type", "application/json")
		enc := json.NewEncoder(w)
		enc.SetEscapeHTML(false)
		_ = enc.Encode(resp)
	})

	addr := fmt.Sprintf("%s:%d", *host, *port)
	fmt.Fprintf(os.Stderr, "[proxy-worker] listening on http://%s\n", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		fmt.Fprintf(os.Stderr, "serve: %v\n", err)
		return 1
	}
	return 0
}

func doTest(req TestRequest) TestResponse {
	started := time.Now()
	ranges := parseAcceptStatus(req.AcceptStatus)
	if len(ranges) == 0 {
		ranges = []StatusRange{{200, 399}}
	}
	timeout := req.TimeoutSec
	if timeout <= 0 {
		timeout = 10
	}
	workers := req.Workers
	if workers <= 0 {
		workers = 128
	}
	// High fan-out for bulk dead-proxy scanning (I/O bound).
	if workers > 4096 {
		workers = 4096
	}
	testURLs := req.TestURLs
	if len(testURLs) == 0 {
		testURLs = []string{"https://accounts.x.ai/sign-up?redirect=grok-com"}
	}
	ua := req.UserAgent
	if ua == "" {
		ua = defaultUA
	}
	maxActive := req.MaxActive

	// de-dupe candidates preserve order
	seen := make(map[string]struct{}, len(req.Candidates))
	candidates := make([]string, 0, len(req.Candidates))
	for _, c := range req.Candidates {
		c = strings.TrimSpace(c)
		if c == "" {
			continue
		}
		if _, ok := seen[c]; ok {
			continue
		}
		seen[c] = struct{}{}
		candidates = append(candidates, c)
	}

	results := make([]TestResult, len(candidates))
	var activeCount int64
	var stopFlag int32

	jobs := make(chan int, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for idx := range jobs {
				if atomic.LoadInt32(&stopFlag) == 1 {
					results[idx] = TestResult{
						Candidate: candidates[idx],
						OK:        false,
						Error:     "cancelled",
					}
					continue
				}
				results[idx] = testOne(candidates[idx], testURLs, time.Duration(timeout)*time.Second, ranges, ua)
				if results[idx].OK {
					n := atomic.AddInt64(&activeCount, 1)
					if maxActive > 0 && int(n) >= maxActive {
						atomic.StoreInt32(&stopFlag, 1)
					}
				}
			}
		}()
	}
	for i := range candidates {
		if atomic.LoadInt32(&stopFlag) == 1 {
			// fill remaining as cancelled without scheduling
			for j := i; j < len(candidates); j++ {
				results[j] = TestResult{Candidate: candidates[j], OK: false, Error: "cancelled"}
			}
			break
		}
		jobs <- i
	}
	close(jobs)
	wg.Wait()

	// drop pure cancelled tail noise? keep all for parity with Python
	out := make([]TestResult, 0, len(results))
	for _, r := range results {
		if r.Candidate == "" && r.Error == "" {
			continue
		}
		out = append(out, r)
	}

	return TestResponse{
		Results: out,
		Engine:  "go",
		Elapsed: time.Since(started).Seconds(),
	}
}

func testOne(candidate string, urls []string, timeout time.Duration, ranges []StatusRange, ua string) TestResult {
	start := time.Now()
	proxyURL, err := normalizeProxy(candidate)
	if err != nil {
		return TestResult{Candidate: candidate, OK: false, LatencyMs: ms(start), Error: err.Error()}
	}
	client, err := httpClientForProxy(proxyURL, timeout)
	if err != nil {
		return TestResult{Candidate: candidate, Proxy: proxyURL.String(), OK: false, LatencyMs: ms(start), Error: err.Error()}
	}
	var lastStatus *int
	for _, u := range urls {
		req, err := http.NewRequest(http.MethodGet, u, nil)
		if err != nil {
			return TestResult{Candidate: candidate, Proxy: proxyURL.String(), OK: false, LatencyMs: ms(start), Error: err.Error()}
		}
		req.Header.Set("User-Agent", ua)
		req.Header.Set("Accept", "*/*")
		resp, err := client.Do(req)
		if err != nil {
			return TestResult{Candidate: candidate, Proxy: proxyURL.String(), OK: false, LatencyMs: ms(start), Error: err.Error()}
		}
		// drain a bit then close
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
		_ = resp.Body.Close()
		code := resp.StatusCode
		lastStatus = &code
		if !statusAllowed(code, ranges) {
			return TestResult{
				Candidate:  candidate,
				Proxy:      proxyURL.String(),
				OK:         false,
				LatencyMs:  ms(start),
				StatusCode: lastStatus,
				Error:      fmt.Sprintf("status %d", code),
			}
		}
	}
	return TestResult{
		Candidate:  candidate,
		Proxy:      proxyURL.String(),
		OK:         true,
		LatencyMs:  ms(start),
		StatusCode: lastStatus,
	}
}

func ms(start time.Time) int {
	return int(time.Since(start).Milliseconds())
}

func statusAllowed(code int, ranges []StatusRange) bool {
	for _, r := range ranges {
		if code >= r.Start && code <= r.End {
			return true
		}
	}
	return false
}

func parseAcceptStatus(raw json.RawMessage) []StatusRange {
	if len(raw) == 0 {
		return nil
	}
	// try [[200,399],...]
	var pairs [][]int
	if err := json.Unmarshal(raw, &pairs); err == nil && len(pairs) > 0 {
		out := make([]StatusRange, 0, len(pairs))
		for _, p := range pairs {
			if len(p) >= 2 {
				out = append(out, StatusRange{Start: p[0], End: p[1]})
			} else if len(p) == 1 {
				out = append(out, StatusRange{Start: p[0], End: p[0]})
			}
		}
		return out
	}
	var objs []StatusRange
	if err := json.Unmarshal(raw, &objs); err == nil {
		return objs
	}
	return nil
}

func normalizeProxy(raw string) (*url.URL, error) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return nil, errors.New("empty proxy")
	}
	if !strings.Contains(s, "://") {
		s = "http://" + s
	}
	u, err := url.Parse(s)
	if err != nil {
		return nil, err
	}
	scheme := strings.ToLower(u.Scheme)
	switch scheme {
	case "http", "https", "socks4", "socks5", "socks5h":
	case "socks":
		u.Scheme = "socks5"
	default:
		return nil, fmt.Errorf("unsupported scheme %s", scheme)
	}
	if u.Hostname() == "" || u.Port() == "" {
		return nil, errors.New("proxy requires host:port")
	}
	// socks5h → socks5 (we always resolve remotely for socks)
	if strings.ToLower(u.Scheme) == "socks5h" {
		u.Scheme = "socks5"
	}
	return u, nil
}

func httpClientForProxy(proxyURL *url.URL, timeout time.Duration) (*http.Client, error) {
	scheme := strings.ToLower(proxyURL.Scheme)
	transport := &http.Transport{
		Proxy:                 nil,
		MaxIdleConns:          64,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   timeout,
		ExpectContinueTimeout: 1 * time.Second,
		ForceAttemptHTTP2:     false,
	}

	switch scheme {
	case "http", "https":
		transport.Proxy = http.ProxyURL(proxyURL)
		transport.DialContext = (&net.Dialer{Timeout: timeout, KeepAlive: 30 * time.Second}).DialContext
	case "socks5", "socks4":
		// custom dial via SOCKS
		transport.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
			return dialViaSOCKS(ctx, proxyURL, addr, timeout)
		}
	default:
		return nil, fmt.Errorf("unsupported proxy scheme %s", scheme)
	}

	return &http.Client{
		Transport: transport,
		Timeout:   timeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 5 {
				return errors.New("too many redirects")
			}
			return nil
		},
	}, nil
}

func dialViaSOCKS(ctx context.Context, proxyURL *url.URL, target string, timeout time.Duration) (net.Conn, error) {
	scheme := strings.ToLower(proxyURL.Scheme)
	d := net.Dialer{Timeout: timeout}
	conn, err := d.DialContext(ctx, "tcp", proxyURL.Host)
	if err != nil {
		return nil, err
	}
	// apply deadline for handshake
	_ = conn.SetDeadline(time.Now().Add(timeout))
	defer func() { _ = conn.SetDeadline(time.Time{}) }()

	host, portStr, err := net.SplitHostPort(target)
	if err != nil {
		_ = conn.Close()
		return nil, err
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		_ = conn.Close()
		return nil, err
	}

	switch scheme {
	case "socks5":
		if err := socks5Handshake(conn, proxyURL, host, port); err != nil {
			_ = conn.Close()
			return nil, err
		}
	case "socks4":
		if err := socks4Handshake(conn, host, port); err != nil {
			_ = conn.Close()
			return nil, err
		}
	default:
		_ = conn.Close()
		return nil, fmt.Errorf("unsupported socks scheme %s", scheme)
	}
	return conn, nil
}

func socks5Handshake(conn net.Conn, proxyURL *url.URL, host string, port int) error {
	user := ""
	pass := ""
	if proxyURL.User != nil {
		user = proxyURL.User.Username()
		pass, _ = proxyURL.User.Password()
	}

	// greeting
	if user != "" || pass != "" {
		if _, err := conn.Write([]byte{0x05, 0x02, 0x00, 0x02}); err != nil {
			return err
		}
	} else {
		if _, err := conn.Write([]byte{0x05, 0x01, 0x00}); err != nil {
			return err
		}
	}
	buf := make([]byte, 2)
	if _, err := io.ReadFull(conn, buf); err != nil {
		return err
	}
	if buf[0] != 0x05 {
		return fmt.Errorf("socks5 version %d", buf[0])
	}
	switch buf[1] {
	case 0x00:
		// no auth
	case 0x02:
		// username/password
		u := []byte(user)
		p := []byte(pass)
		if len(u) > 255 || len(p) > 255 {
			return errors.New("socks5 auth too long")
		}
		req := make([]byte, 0, 3+len(u)+len(p))
		req = append(req, 0x01, byte(len(u)))
		req = append(req, u...)
		req = append(req, byte(len(p)))
		req = append(req, p...)
		if _, err := conn.Write(req); err != nil {
			return err
		}
		authResp := make([]byte, 2)
		if _, err := io.ReadFull(conn, authResp); err != nil {
			return err
		}
		if authResp[1] != 0x00 {
			return errors.New("socks5 auth failed")
		}
	case 0xFF:
		return errors.New("socks5 no acceptable auth")
	default:
		return fmt.Errorf("socks5 auth method %d", buf[1])
	}

	// CONNECT request
	req := []byte{0x05, 0x01, 0x00}
	if ip := net.ParseIP(host); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			req = append(req, 0x01)
			req = append(req, v4...)
		} else {
			v6 := ip.To16()
			req = append(req, 0x04)
			req = append(req, v6...)
		}
	} else {
		if len(host) > 255 {
			return errors.New("hostname too long")
		}
		req = append(req, 0x03, byte(len(host)))
		req = append(req, []byte(host)...)
	}
	req = append(req, byte(port>>8), byte(port&0xff))
	if _, err := conn.Write(req); err != nil {
		return err
	}

	// reply
	hdr := make([]byte, 4)
	if _, err := io.ReadFull(conn, hdr); err != nil {
		return err
	}
	if hdr[0] != 0x05 {
		return fmt.Errorf("socks5 reply version %d", hdr[0])
	}
	if hdr[1] != 0x00 {
		return fmt.Errorf("socks5 connect failed code %d", hdr[1])
	}
	// bind addr
	switch hdr[3] {
	case 0x01:
		tmp := make([]byte, 4+2)
		if _, err := io.ReadFull(conn, tmp); err != nil {
			return err
		}
	case 0x03:
		l := make([]byte, 1)
		if _, err := io.ReadFull(conn, l); err != nil {
			return err
		}
		tmp := make([]byte, int(l[0])+2)
		if _, err := io.ReadFull(conn, tmp); err != nil {
			return err
		}
	case 0x04:
		tmp := make([]byte, 16+2)
		if _, err := io.ReadFull(conn, tmp); err != nil {
			return err
		}
	default:
		return fmt.Errorf("socks5 addr type %d", hdr[3])
	}
	return nil
}

func socks4Handshake(conn net.Conn, host string, port int) error {
	ip := net.ParseIP(host)
	var ip4 net.IP
	if ip != nil {
		ip4 = ip.To4()
	}
	if ip4 == nil {
		// SOCKS4a domain
		req := []byte{0x04, 0x01, byte(port >> 8), byte(port & 0xff), 0, 0, 0, 1, 0}
		req = append(req, []byte(host)...)
		req = append(req, 0)
		if _, err := conn.Write(req); err != nil {
			return err
		}
	} else {
		req := []byte{0x04, 0x01, byte(port >> 8), byte(port & 0xff), ip4[0], ip4[1], ip4[2], ip4[3], 0}
		if _, err := conn.Write(req); err != nil {
			return err
		}
	}
	resp := make([]byte, 8)
	if _, err := io.ReadFull(conn, resp); err != nil {
		return err
	}
	if resp[1] != 0x5a {
		return fmt.Errorf("socks4 connect failed code %d", resp[1])
	}
	return nil
}

