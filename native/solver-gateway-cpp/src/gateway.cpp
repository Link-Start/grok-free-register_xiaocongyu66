// solver-gateway-cpp — hybrid Turnstile control plane in pure C++.
//
//   C++ (this binary)  — HTTP API, job queue (oneTBB), worker IPC, memory watchdog
//   mimalloc           — default allocator (MI_MALLOC_OVERRIDE)
//   oneTBB             — concurrent_queue + worker threads
//   Python             — browser_worker.py ONLY (Chromium solve / token fetch)
//
// Compatible API with Go solver-gateway / Theyka / D3-vin:
//   GET  /turnstile?url=&sitekey=&action=&cdata=
//   GET  /result?id=
//   GET  /health  /stats  /  /v1/memory
//
// Build: make -C native/solver-gateway-cpp
// Run:   ./solver-gateway  (same env knobs as Go gateway)

// mimalloc as default allocator when linked with -lmimalloc
#if defined(SOLVER_USE_MIMALLOC)
#include <mimalloc.h>
#include <new>
// Prefer explicit new/delete override for reliability across distros.
void* operator new(std::size_t n) { return mi_malloc(n ? n : 1); }
void* operator new[](std::size_t n) { return mi_malloc(n ? n : 1); }
void operator delete(void* p) noexcept { mi_free(p); }
void operator delete[](void* p) noexcept { mi_free(p); }
void operator delete(void* p, std::size_t) noexcept { mi_free(p); }
void operator delete[](void* p, std::size_t) noexcept { mi_free(p); }
#endif

#include "http_tiny.hpp"
#include "json_tiny.hpp"
#include "solver_util.hpp"

#include <oneapi/tbb/concurrent_queue.h>
#include <oneapi/tbb/global_control.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <errno.h>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <string>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <unordered_map>
#include <utility>
#include <vector>

extern char** environ;

namespace fs = std::filesystem;

static constexpr const char* kVersion = "1.1.0-cpp";
static constexpr const char* kEngine = "hybrid-cpp";

// ---------- env helpers ----------

static std::string env_or(const char* k, const char* def) {
  const char* v = std::getenv(k);
  if (!v || !*v) return def;
  return v;
}

static int env_int(const char* k, int def) {
  const char* v = std::getenv(k);
  if (!v || !*v) return def;
  try {
    return std::stoi(v);
  } catch (...) {
    return def;
  }
}

static bool env_bool(const char* k, bool def) {
  const char* v = std::getenv(k);
  if (!v || !*v) return def;
  std::string s = v;
  for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  if (s == "1" || s == "true" || s == "yes" || s == "on") return true;
  if (s == "0" || s == "false" || s == "no" || s == "off") return false;
  return def;
}

static bool is_auto(const std::string& raw) {
  std::string v = raw;
  for (char& c : v) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return v.empty() || v == "auto" || v == "0";
}

static double now_sec() {
  using clock = std::chrono::system_clock;
  return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

static std::string new_id() {
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%lld%x",
                static_cast<long long>(std::chrono::steady_clock::now().time_since_epoch().count()),
                static_cast<unsigned>(getpid() & 0xffff));
  return buf;
}

// ---------- memory (cgroup-aware) ----------

static uint64_t read_uint_file(const char* path) {
  std::ifstream in(path);
  if (!in) return 0;
  std::string s;
  in >> s;
  if (s.empty() || s == "max") return 0;
  try {
    return std::stoull(s);
  } catch (...) {
    return 0;
  }
}

static int mem_host_total_mb() {
  uint64_t t = 0, a = 0;
  if (!solver_util::read_meminfo_kb(&t, &a)) return 0;
  return static_cast<int>(t / 1024);
}

static int mem_host_avail_mb() {
  uint64_t t = 0, a = 0;
  if (!solver_util::read_meminfo_kb(&t, &a)) return 0;
  return static_cast<int>(a / 1024);
}

struct ContMem {
  int total_mb = 0;
  int avail_mb = 0;
  int used_mb = 0;
  int pressure = 0;
  bool ok = false;
};

static ContMem container_memory() {
  ContMem m;
  int host_total = mem_host_total_mb();
  int host_avail = mem_host_avail_mb();
  uint64_t limit_b = read_uint_file("/sys/fs/cgroup/memory.max");
  if (!limit_b) limit_b = read_uint_file("/sys/fs/cgroup/memory/memory.limit_in_bytes");
  if (limit_b > (1ull << 50)) limit_b = 0;
  uint64_t cur_b = read_uint_file("/sys/fs/cgroup/memory.current");
  if (!cur_b) cur_b = read_uint_file("/sys/fs/cgroup/memory/memory.usage_in_bytes");

  std::string override_mb = env_or("SOLVER_MEMORY_LIMIT_MB", "");
  if (!is_auto(override_mb)) {
    try {
      int n = std::stoi(override_mb);
      if (n > 0) {
        limit_b = static_cast<uint64_t>(n) * 1024ull * 1024ull;
      }
    } catch (...) {
    }
  }

  if (limit_b > 0) {
    m.total_mb = static_cast<int>(limit_b / (1024 * 1024));
    if (m.total_mb < 256) m.total_mb = 256;
    m.used_mb = cur_b ? static_cast<int>(cur_b / (1024 * 1024)) : 0;
    if (m.used_mb > m.total_mb) m.used_mb = m.total_mb;
    m.avail_mb = m.total_mb - m.used_mb;
    if (host_avail > 0 && m.avail_mb > host_avail) {
      m.avail_mb = host_avail;
      m.used_mb = m.total_mb - m.avail_mb;
    }
    m.ok = true;
  } else if (host_total > 0) {
    m.total_mb = host_total;
    m.avail_mb = host_avail;
    m.used_mb = host_total > host_avail ? host_total - host_avail : 0;
    m.ok = true;
  }
  if (m.total_mb > 0) {
    m.pressure = static_cast<int>((static_cast<int64_t>(m.used_mb) * 100) / m.total_mb);
    if (m.pressure < 0) m.pressure = 0;
    if (m.pressure > 100) m.pressure = 100;
  } else {
    m.pressure = 100;
  }
  return m;
}

// Memory-first defaults: prefer 1 small browser over thrashing under OOM.
static std::pair<int, int> auto_soft_hard_mb() {
  ContMem m = container_memory();
  int soft = 450, hard = 700;
  if (m.total_mb >= 28000) {
    soft = 700;
    hard = 1100;
  } else if (m.total_mb >= 14000) {
    soft = 550;
    hard = 900;
  } else if (m.total_mb >= 7000) {
    soft = 450;
    hard = 750;
  } else if (m.total_mb >= 3500) {
    soft = 380;
    hard = 620;
  } else {
    soft = 300;
    hard = 480;
  }
  // When free RAM is already tight, shrink soft/hard so recycle fires early.
  if (m.avail_mb > 0 && m.avail_mb < 2000) {
    soft = std::max(260, std::min(soft, m.avail_mb / 4));
    hard = std::max(soft + 120, soft * 3 / 2);
  }
  if (hard <= soft) hard = soft + 150;
  return {soft, hard};
}

static int auto_workers(int soft_mb) {
  ContMem m = container_memory();
  int cores = static_cast<int>(std::thread::hardware_concurrency());
  if (cores < 1) cores = 1;
  if (soft_mb <= 0) soft_mb = auto_soft_hard_mb().first;
  // Keep a larger reserve for OS + protocol register + moemail.
  int reserve = m.avail_mb < 2500 ? 1000 : (m.avail_mb < 5000 ? 1400 : 1800);
  int budget = m.avail_mb - reserve;
  if (budget < soft_mb) return 1;
  int by_mem = budget / soft_mb;
  int by_cpu = std::max(1, cores / 2);  // leave cores for register workers
  int cap_n = env_int("SOLVER_GATEWAY_WORKERS_MAX", 0);
  if (cap_n <= 0) {
    if (m.total_mb >= 28000) cap_n = std::min(cores, 4);
    else if (m.total_mb >= 14000) cap_n = std::min(cores, 3);
    else if (m.total_mb >= 7000) cap_n = 2;
    else cap_n = 1;
  }
  // Hard cap 1 when free RAM is critically low
  if (m.avail_mb > 0 && m.avail_mb < 1800) cap_n = 1;
  int n = std::min({by_mem, by_cpu, cap_n});
  return std::max(1, n);
}

static int auto_max_solves() {
  ContMem m = container_memory();
  // Fewer solves per browser life → lower peak RSS / fewer leaks
  if (m.avail_mb > 0 && m.avail_mb < 1500) return 1;
  if (m.total_mb >= 14000 && m.avail_mb >= 4000) return 4;
  if (m.total_mb >= 7000 && m.avail_mb >= 2500) return 3;
  return 2;
}

static int auto_timeout_sec() {
  ContMem m = container_memory();
  // Fail faster under pressure so workers recycle instead of stacking
  if (m.avail_mb > 0 && m.avail_mb < 1500) return 60;
  int cores = static_cast<int>(std::thread::hardware_concurrency());
  if (cores <= 2) return 90;
  if (cores <= 4) return 80;
  return 75;
}

static int auto_concurrency() {
  ContMem m = container_memory();
  // Multiple pages per browser burns RAM; keep 1 unless roomy.
  if (m.avail_mb >= 5000 && m.total_mb >= 14000) return 2;
  return 1;
}

static bool auto_prefetch() {
  ContMem m = container_memory();
  // Prefetch holds a warm chromium — skip when free RAM is low.
  return m.avail_mb >= 2500;
}

// ---------- job / result ----------

struct Job {
  std::string id;
  std::string url;
  std::string sitekey;
  std::string action;
  std::string cdata;
  std::string proxy;
  double created_at = 0;
};

struct Result {
  std::string id;
  std::string status;  // pending|success|fail|error
  std::string value;
  std::string error;
  double elapsed_sec = 0;
  int worker = 0;
  double updated_at = 0;
  bool recycled = false;
};

// ---------- worker process (Python token only) ----------

struct WorkerProc {
  int id = 0;
  pid_t pid = -1;
  int stdin_fd = -1;
  int stdout_fd = -1;
  int solves = 0;
  int fails = 0;
  bool alive = false;
  std::mutex mu;
};

struct Gateway {
  tbb::concurrent_queue<Job> queue;
  std::mutex results_mu;
  std::unordered_map<std::string, Result> results;

  std::atomic<bool> running{true};
  std::atomic<int64_t> pending{0};
  std::atomic<int64_t> solved{0};
  std::atomic<int64_t> failed{0};
  std::atomic<int64_t> recycles{0};
  std::atomic<int64_t> solve_sum_ms{0};
  std::atomic<int64_t> solve_count{0};
  std::atomic<int> alive_workers{0};

  int workers = 1;
  int concurrency = 1;
  int soft_mb = 700;
  int hard_mb = 1100;
  int max_solves = 8;
  int solve_timeout_sec = 90;
  double result_ttl_sec = 15 * 60;
  double started_at = 0;

  std::string python_bin;
  std::string worker_script;
  std::string work_dir;
  std::string browser_type = "chromium";
  bool headless = true;
  bool prefetch = true;
  std::string proxy_file;
  std::string api_token;

  std::vector<std::thread> loops;
  std::unique_ptr<http_tiny::Server> server;
};

static Gateway* g_gw = nullptr;

static void put_result(Gateway& g, Result r) {
  r.updated_at = now_sec();
  std::lock_guard<std::mutex> lock(g.results_mu);
  g.results[r.id] = std::move(r);
}

static Result get_result(Gateway& g, const std::string& id) {
  std::lock_guard<std::mutex> lock(g.results_mu);
  auto it = g.results.find(id);
  if (it == g.results.end()) return {};
  return it->second;
}

static void purge_expired(Gateway& g) {
  double cutoff = now_sec() - g.result_ttl_sec;
  std::lock_guard<std::mutex> lock(g.results_mu);
  for (auto it = g.results.begin(); it != g.results.end();) {
    if (it->second.updated_at > 0 && it->second.updated_at < cutoff &&
        it->second.status != "pending") {
      it = g.results.erase(it);
    } else {
      ++it;
    }
  }
}

static bool token_ok(const std::string& token) {
  return solver_util::token_shape_ok(token.c_str(), token.size());
}

static bool should_recycle(Gateway& g, uint64_t rss_kb, int pressure, int solves) {
  uint64_t soft = static_cast<uint64_t>(g.soft_mb) * 1024ull;
  uint64_t hard = static_cast<uint64_t>(g.hard_mb) * 1024ull;
  if (hard > 0 && rss_kb >= hard) return true;
  if (soft > 0 && rss_kb >= soft) return true;
  ContMem m = container_memory();
  if (solves > 0 && m.avail_mb > 0 && m.avail_mb < 800) return true;
  if (solves > 0 &&
      solver_util::should_recycle(rss_kb * 1024ull, static_cast<uint64_t>(g.soft_mb),
                                  static_cast<uint64_t>(g.hard_mb), pressure)) {
    if (rss_kb >= soft || m.avail_mb < 1200) return true;
  }
  return false;
}

static void kill_tree(pid_t pid) {
  if (pid <= 0) return;
  // process group
  ::kill(-pid, SIGTERM);
  ::kill(pid, SIGTERM);
  for (int i = 0; i < 30; ++i) {
    int st = 0;
    pid_t r = ::waitpid(pid, &st, WNOHANG);
    if (r == pid || (r < 0 && errno == ECHILD)) return;
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  ::kill(-pid, SIGKILL);
  ::kill(pid, SIGKILL);
  int st = 0;
  ::waitpid(pid, &st, 0);
}

static void stop_worker(Gateway& g, WorkerProc& wp) {
  std::lock_guard<std::mutex> lock(wp.mu);
  if (!wp.alive) return;
  // graceful shutdown JSON
  if (wp.stdin_fd >= 0) {
    const char* msg = "{\"cmd\":\"shutdown\"}\n";
    (void)::write(wp.stdin_fd, msg, std::strlen(msg));
    ::close(wp.stdin_fd);
    wp.stdin_fd = -1;
  }
  if (wp.stdout_fd >= 0) {
    ::close(wp.stdout_fd);
    wp.stdout_fd = -1;
  }
  if (wp.pid > 0) {
    // wait briefly
    bool done = false;
    for (int i = 0; i < 40; ++i) {
      int st = 0;
      pid_t r = ::waitpid(wp.pid, &st, WNOHANG);
      if (r == wp.pid || (r < 0 && errno == ECHILD)) {
        done = true;
        break;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    if (!done) kill_tree(wp.pid);
  }
  wp.pid = -1;
  wp.alive = false;
  g.alive_workers.fetch_sub(1);
  g.recycles.fetch_add(1);
}

static bool start_worker(Gateway& g, WorkerProc& wp) {
  std::lock_guard<std::mutex> lock(wp.mu);
  int in_pipe[2], out_pipe[2];
  if (::pipe(in_pipe) != 0 || ::pipe(out_pipe) != 0) return false;

  std::vector<std::string> args_store;
  args_store.push_back(g.python_bin);
  args_store.push_back(g.worker_script);
  args_store.push_back("--worker-id");
  args_store.push_back(std::to_string(wp.id));
  args_store.push_back("--browser");
  args_store.push_back(g.browser_type);
  args_store.push_back("--soft-mb");
  args_store.push_back(std::to_string(g.soft_mb));
  args_store.push_back("--hard-mb");
  args_store.push_back(std::to_string(g.hard_mb));
  args_store.push_back("--max-solves");
  args_store.push_back(std::to_string(g.max_solves));
  args_store.push_back("--concurrency");
  args_store.push_back(std::to_string(std::max(1, g.concurrency)));
  if (g.headless) args_store.push_back("--headless");
  if (!g.proxy_file.empty()) {
    args_store.push_back("--proxy-file");
    args_store.push_back(g.proxy_file);
  }

  std::vector<char*> argv;
  for (auto& s : args_store) argv.push_back(s.data());
  argv.push_back(nullptr);

  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  // child stdin = in_pipe[0], stdout = out_pipe[1]
  posix_spawn_file_actions_adddup2(&fa, in_pipe[0], STDIN_FILENO);
  posix_spawn_file_actions_adddup2(&fa, out_pipe[1], STDOUT_FILENO);
  posix_spawn_file_actions_addclose(&fa, in_pipe[1]);
  posix_spawn_file_actions_addclose(&fa, out_pipe[0]);
  // keep stderr

  posix_spawnattr_t attr;
  posix_spawnattr_init(&attr);
  // new process group for tree kill
#if defined(POSIX_SPAWN_SETPGROUP)
  posix_spawnattr_setpgroup(&attr, 0);
  posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP);
#endif

  // env: unbuffered python, strip outer proxies
  std::vector<std::string> env_store;
  for (char** e = environ; e && *e; ++e) {
    std::string line = *e;
    std::string up = line;
    for (char& c : up) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    if (up.rfind("HTTP_PROXY=", 0) == 0 || up.rfind("HTTPS_PROXY=", 0) == 0 ||
        up.rfind("ALL_PROXY=", 0) == 0) {
      continue;
    }
    env_store.push_back(line);
  }
  env_store.push_back("PYTHONUNBUFFERED=1");
  env_store.push_back("PYTHONDONTWRITEBYTECODE=1");
  env_store.push_back("PYTHONMALLOC=malloc");
  env_store.push_back("TURNSTILE_SOLVER=local");
  env_store.push_back("SOLVER_REUSE=0");
  // Memory-first worker knobs (browser_worker reads these)
  ContMem cm = container_memory();
  if (cm.avail_mb > 0 && cm.avail_mb < 1800) {
    env_store.push_back("SOLVER_RECYCLE_EVERY=1");
  } else if (cm.avail_mb > 0 && cm.avail_mb < 3000) {
    env_store.push_back("SOLVER_RECYCLE_EVERY=2");
  }
  env_store.push_back("SOLVER_IDLE_RECYCLE_SEC=" + std::to_string(cm.avail_mb < 2000 ? 25 : 45));
  std::vector<char*> envp;
  for (auto& s : env_store) envp.push_back(s.data());
  envp.push_back(nullptr);

  pid_t pid = 0;
  int rc = ::posix_spawnp(&pid, g.python_bin.c_str(), &fa, &attr, argv.data(), envp.data());
  posix_spawn_file_actions_destroy(&fa);
  posix_spawnattr_destroy(&attr);
  ::close(in_pipe[0]);
  ::close(out_pipe[1]);
  if (rc != 0) {
    ::close(in_pipe[1]);
    ::close(out_pipe[0]);
    std::fprintf(stderr, "[gateway-cpp] spawn worker %d failed: %s\n", wp.id, std::strerror(rc));
    return false;
  }
  wp.pid = pid;
  wp.stdin_fd = in_pipe[1];
  wp.stdout_fd = out_pipe[0];
  wp.alive = true;
  wp.solves = 0;
  wp.fails = 0;
  g.alive_workers.fetch_add(1);
  std::fprintf(stderr, "[gateway-cpp] worker %d started pid=%d\n", wp.id, static_cast<int>(pid));

  // Optional warm-up only when host has spare RAM (worker may still skip)
  if (g.prefetch) {
    // non-blocking: fire-and-forget after unlock would race; skip here.
    // Prefetch is handled if worker CLI gets --prefetch via future flag.
  }
  return true;
}

static bool ensure_worker(Gateway& g, WorkerProc& wp) {
  if (wp.alive && wp.pid > 0) {
    if (::kill(wp.pid, 0) == 0) return true;
  }
  if (wp.alive) stop_worker(g, wp);
  return start_worker(g, wp);
}

static bool write_line(int fd, const std::string& line) {
  std::string s = line;
  if (s.empty() || s.back() != '\n') s.push_back('\n');
  size_t off = 0;
  while (off < s.size()) {
    ssize_t n = ::write(fd, s.data() + off, s.size() - off);
    if (n < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    off += static_cast<size_t>(n);
  }
  return true;
}

static bool read_line_timeout(int fd, std::string& out, int timeout_ms) {
  out.clear();
  char buf[1];
  auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
  while (true) {
    auto now = std::chrono::steady_clock::now();
    if (now >= deadline) return false;
    int remain = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count());
    pollfd pfd{fd, POLLIN, 0};
    int pr = ::poll(&pfd, 1, remain);
    if (pr == 0) return false;
    if (pr < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    ssize_t n = ::read(fd, buf, 1);
    if (n == 0) return false;
    if (n < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    if (buf[0] == '\n') return true;
    if (buf[0] != '\r') out.push_back(buf[0]);
    if (out.size() > 1 << 20) return false;
  }
}

struct WorkerResp {
  bool ok = false;
  std::string id;
  std::string value;
  std::string error;
  double elapsed = 0;
  bool recycled = false;
};

static WorkerResp solve_on_worker(Gateway& g, WorkerProc& wp, const Job& job) {
  std::lock_guard<std::mutex> lock(wp.mu);
  WorkerResp resp;
  resp.id = job.id;
  if (!wp.alive || wp.stdin_fd < 0 || wp.stdout_fd < 0) {
    resp.error = "worker dead";
    return resp;
  }
  jx::Object req;
  req["cmd"] = jx::Value("solve");
  req["id"] = jx::Value(job.id);
  req["url"] = jx::Value(job.url);
  req["sitekey"] = jx::Value(job.sitekey);
  if (!job.action.empty()) req["action"] = jx::Value(job.action);
  if (!job.cdata.empty()) req["cdata"] = jx::Value(job.cdata);
  if (!job.proxy.empty()) req["proxy"] = jx::Value(job.proxy);
  std::string line = jx::dumps(jx::Value(req));
  if (!write_line(wp.stdin_fd, line)) {
    wp.alive = false;
    resp.error = "worker write failed";
    wp.fails++;
    return resp;
  }
  std::string raw;
  if (!read_line_timeout(wp.stdout_fd, raw, g.solve_timeout_sec * 1000 + 5000)) {
    wp.alive = false;
    resp.error = "solve timeout";
    wp.fails++;
    return resp;
  }
  jx::Value v = jx::loads(raw);
  resp.ok = v.get("ok").as_bool(false);
  resp.value = v.get("value").as_str();
  resp.error = v.get("error").as_str();
  resp.elapsed = v.get("elapsed_sec").as_num(0);
  resp.recycled = v.get("recycled").as_bool(false);
  wp.solves++;
  if (!resp.ok) wp.fails++;
  return resp;
}

static void worker_loop(Gateway* g, int id) {
  WorkerProc wp;
  wp.id = id;
  while (g->running.load()) {
    Job job;
    if (!g->queue.try_pop(job)) {
      std::this_thread::sleep_for(std::chrono::milliseconds(15));
      continue;
    }
    if (!ensure_worker(*g, wp)) {
      g->failed.fetch_add(1);
      g->pending.fetch_sub(1);
      put_result(*g, Result{job.id, "error", "", "spawn worker failed", 0, id, now_sec(), false});
      std::this_thread::sleep_for(std::chrono::milliseconds(400));
      continue;
    }
    // recycle check
    if (wp.pid > 0) {
      uint64_t rss = solver_util::process_rss_kb(wp.pid);
      ContMem m = container_memory();
      bool force = (g->max_solves > 0 && wp.solves >= g->max_solves) || (wp.fails >= 3 && wp.solves > 0);
      if (should_recycle(*g, rss, m.pressure, wp.solves) || force) {
        std::fprintf(stderr,
                     "[gateway-cpp] recycle worker %d rss_kb=%llu pressure=%d solves=%d fails=%d\n",
                     id, static_cast<unsigned long long>(rss), m.pressure, wp.solves, wp.fails);
        stop_worker(*g, wp);
        if (!ensure_worker(*g, wp)) {
          g->failed.fetch_add(1);
          g->pending.fetch_sub(1);
          put_result(*g, Result{job.id, "error", "", "respawn failed", 0, id, now_sec(), true});
          continue;
        }
      }
    }

    auto t0 = std::chrono::steady_clock::now();
    WorkerResp resp = solve_on_worker(*g, wp, job);
    double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    g->pending.fetch_sub(1);

    if (resp.ok && !resp.value.empty() && token_ok(resp.value)) {
      g->solved.fetch_add(1);
      g->solve_sum_ms.fetch_add(static_cast<int64_t>(elapsed * 1000));
      g->solve_count.fetch_add(1);
      put_result(*g, Result{job.id, "success", resp.value, "", elapsed, id, now_sec(), resp.recycled});
      // Under pressure, recycle even after success to free chromium ASAP
      ContMem after = container_memory();
      if (wp.alive && (resp.recycled || after.avail_mb < 1200 || g->max_solves <= 2)) {
        stop_worker(*g, wp);
      }
    } else {
      g->failed.fetch_add(1);
      std::string err = resp.error.empty() ? "CAPTCHA_FAIL" : resp.error;
      put_result(*g, Result{job.id, "fail", "CAPTCHA_FAIL", err, elapsed, id, now_sec(), false});
      // Always kill browser after fail — prevents zombie chromium growth
      if (wp.alive) stop_worker(*g, wp);
    }
  }
  if (wp.alive) stop_worker(*g, wp);
}

// ---------- HTTP handlers ----------

static bool authorized(const Gateway& g, const http_tiny::Request& req) {
  if (g.api_token.empty()) return true;
  if (req.path == "/health" || req.path == "/api/health" || req.path == "/") return true;
  auto auth = req.header("Authorization");
  if (auth.size() > 7) {
    std::string low = auth.substr(0, 7);
    for (char& c : low) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (low == "bearer " && auth.substr(7) == g.api_token) return true;
  }
  if (req.header("X-API-Key") == g.api_token || req.header("X-Solver-Token") == g.api_token) return true;
  if (jx::query_get(req.query, "token") == g.api_token) return true;
  return false;
}

static http_tiny::Response json_resp(int status, const jx::Value& v) {
  http_tiny::Response r;
  r.status = status;
  r.content_type = "application/json";
  r.body = jx::dumps(v) + "\n";
  return r;
}

static void enqueue(Gateway& g, Job job) {
  g.pending.fetch_add(1);
  put_result(g, Result{job.id, "pending", "", "", 0, 0, now_sec(), false});
  g.queue.push(std::move(job));
}

static jx::Value stats_json(Gateway& g) {
  ContMem m = container_memory();
  int64_t sc = g.solve_count.load();
  double avg = sc > 0 ? (static_cast<double>(g.solve_sum_ms.load()) / static_cast<double>(sc) / 1000.0) : 0;
  jx::Object o;
  o["engine"] = jx::Value(kEngine);
  o["version"] = jx::Value(kVersion);
  o["queue_depth"] = jx::Value(0);  // concurrent_queue has no size; report pending
  o["pending"] = jx::Value(static_cast<double>(g.pending.load()));
  o["solved"] = jx::Value(static_cast<double>(g.solved.load()));
  o["failed"] = jx::Value(static_cast<double>(g.failed.load()));
  o["recycles"] = jx::Value(static_cast<double>(g.recycles.load()));
  o["workers"] = jx::Value(g.workers);
  o["worker_alive"] = jx::Value(g.alive_workers.load());
  o["concurrency"] = jx::Value(g.concurrency);
  o["effective_slots"] = jx::Value(g.workers * g.concurrency);
  o["cpu_cores"] = jx::Value(static_cast<int>(std::thread::hardware_concurrency()));
  o["avg_solve_sec"] = jx::Value(avg);
  o["uptime_sec"] = jx::Value(now_sec() - g.started_at);
  o["host_pressure"] = jx::Value(m.pressure);
  o["host_available_mb"] = jx::Value(m.avail_mb);
  o["allocator"] = jx::Value("mimalloc");
  o["scheduler"] = jx::Value("oneTBB");
  return jx::Value(o);
}

static http_tiny::Response handle(Gateway& g, const http_tiny::Request& req) {
  if (!authorized(g, req)) {
    return json_resp(401, jx::Object{{"ok", jx::Value(false)}, {"error", jx::Value("unauthorized")}});
  }

  if (req.path == "/" || req.path == "/health" || req.path == "/api/health") {
    if (req.path == "/") {
      jx::Object o;
      o["service"] = jx::Value("solver-gateway");
      o["engine"] = jx::Value(kEngine);
      o["version"] = jx::Value(kVersion);
      o["endpoints"] = jx::Value(jx::Array{
          jx::Value("/turnstile"), jx::Value("/result"), jx::Value("/health"),
          jx::Value("/stats"), jx::Value("/v1/memory")});
      o["token_worker"] = jx::Value("python");
      o["control_plane"] = jx::Value("c++");
      return json_resp(200, o);
    }
    return json_resp(200, jx::Object{
                              {"ok", jx::Value(true)},
                              {"engine", jx::Value(kEngine)},
                              {"version", jx::Value(kVersion)},
                          });
  }

  if (req.path == "/stats") {
    return json_resp(200, stats_json(g));
  }

  if (req.path == "/v1/memory") {
    ContMem m = container_memory();
    jx::Object host;
    host["total_kb"] = jx::Value(m.total_mb * 1024.0);
    host["available_kb"] = jx::Value(m.avail_mb * 1024.0);
    host["used_kb"] = jx::Value(m.used_mb * 1024.0);
    host["pressure"] = jx::Value(m.pressure);
    jx::Object o;
    o["host"] = jx::Value(host);
    o["soft_mb"] = jx::Value(g.soft_mb);
    o["hard_mb"] = jx::Value(g.hard_mb);
    o["recycles"] = jx::Value(static_cast<double>(g.recycles.load()));
    o["util_ok"] = jx::Value(true);
    o["watchdog"] = jx::Value("builtin-cpp");
    o["allocator"] = jx::Value("mimalloc");
    return json_resp(200, o);
  }

  if (req.path == "/turnstile" || req.path == "/v1/solve") {
    std::string url = jx::query_get(req.query, "url");
    std::string sitekey = jx::query_get(req.query, "sitekey");
    std::string action = jx::query_get(req.query, "action");
    std::string cdata = jx::query_get(req.query, "cdata");
    std::string proxy = jx::query_get(req.query, "proxy");
    if ((url.empty() || sitekey.empty()) && !req.body.empty()) {
      jx::Value body = jx::loads(req.body);
      if (url.empty()) url = body.get("url").as_str();
      if (sitekey.empty()) sitekey = body.get("sitekey").as_str();
      if (action.empty()) action = body.get("action").as_str();
      if (cdata.empty()) cdata = body.get("cdata").as_str();
      if (proxy.empty()) proxy = body.get("proxy").as_str();
    }
    if (url.empty() || sitekey.empty()) {
      return json_resp(400, jx::Object{{"error", jx::Value("url and sitekey required")}});
    }
    Job job;
    job.id = new_id();
    job.url = url;
    job.sitekey = sitekey;
    job.action = action;
    job.cdata = cdata;
    job.proxy = proxy;
    job.created_at = now_sec();
    enqueue(g, job);
    return json_resp(200, jx::Object{{"task_id", jx::Value(job.id)}, {"id", jx::Value(job.id)}});
  }

  if (req.path == "/result") {
    std::string id = jx::query_get(req.query, "id");
    if (id.empty()) id = jx::query_get(req.query, "task_id");
    if (id.empty()) return json_resp(400, jx::Object{{"error", jx::Value("id required")}});
    Result res = get_result(g, id);
    if (res.id.empty()) {
      return json_resp(200, jx::Object{
                                {"status", jx::Value("error")},
                                {"value", jx::Value("CAPTCHA_NOT_READY")},
                                {"error", jx::Value("not found")},
                            });
    }
    if (res.status == "pending") {
      return json_resp(200, jx::Object{
                                {"status", jx::Value("process")},
                                {"value", jx::Value("CAPTCHA_NOT_READY")},
                                {"elapsed_time", jx::Value(0)},
                            });
    }
    if (res.status == "success") {
      return json_resp(200, jx::Object{
                                {"status", jx::Value("success")},
                                {"value", jx::Value(res.value)},
                                {"elapsed_time", jx::Value(res.elapsed_sec)},
                            });
    }
    return json_resp(200, jx::Object{
                              {"status", jx::Value("fail")},
                              {"value", jx::Value(res.value.empty() ? "CAPTCHA_FAIL" : res.value)},
                              {"error", jx::Value(res.error)},
                              {"elapsed_time", jx::Value(res.elapsed_sec)},
                          });
  }

  return json_resp(404, jx::Object{{"error", jx::Value("not found")}});
}

static void on_signal(int) {
  if (g_gw) g_gw->running.store(false);
  if (g_gw && g_gw->server) g_gw->server->request_stop();
}

static std::string find_path(const std::vector<std::string>& cands) {
  for (const auto& c : cands) {
    if (!c.empty() && fs::exists(c)) return c;
  }
  return {};
}

int main(int argc, char** argv) {
  if (argc > 1) {
    std::string a = argv[1];
    if (a == "version" || a == "-v" || a == "--version") {
      std::printf("solver-gateway %s\n", kVersion);
      return 0;
    }
  }

#if defined(SOLVER_USE_MIMALLOC)
  // Ensure mimalloc is linked/initialized
  mi_version();
#endif

  // oneTBB global thread limit = hardware
  int cores = static_cast<int>(std::thread::hardware_concurrency());
  if (cores < 1) cores = 1;
  tbb::global_control gc(tbb::global_control::max_allowed_parallelism, static_cast<size_t>(cores));

  Gateway gw;
  g_gw = &gw;
  gw.started_at = now_sec();

  std::string host = env_or("SOLVER_GATEWAY_HOST", env_or("HOST", "0.0.0.0").c_str());
  int port = env_int("PORT", env_int("SOLVER_GATEWAY_PORT", 5080));

  auto soft_hard = auto_soft_hard_mb();
  std::string soft_raw = env_or("SOLVER_WATCHDOG_SOFT_MB", "auto");
  std::string hard_raw = env_or("SOLVER_WATCHDOG_HARD_MB", "auto");
  gw.soft_mb = is_auto(soft_raw) ? soft_hard.first : std::stoi(soft_raw);
  gw.hard_mb = is_auto(hard_raw) ? soft_hard.second : std::stoi(hard_raw);
  if (gw.hard_mb <= gw.soft_mb) gw.hard_mb = gw.soft_mb + 200;

  std::string workers_raw = env_or("SOLVER_GATEWAY_WORKERS", "auto");
  gw.workers = is_auto(workers_raw) ? auto_workers(gw.soft_mb) : std::max(1, std::stoi(workers_raw));
  std::string conc_raw = env_or("SOLVER_WORKER_CONCURRENCY", "auto");
  gw.concurrency = is_auto(conc_raw) ? auto_concurrency() : std::max(1, std::stoi(conc_raw));
  // Hard clamp concurrency — multi-page browsers explode RAM
  if (gw.concurrency > 2) gw.concurrency = 2;
  ContMem m0 = container_memory();
  if (m0.avail_mb > 0 && m0.avail_mb < 2500) gw.concurrency = 1;

  std::string to_raw = env_or("SOLVER_GATEWAY_TIMEOUT", "auto");
  gw.solve_timeout_sec = is_auto(to_raw) ? auto_timeout_sec() : std::max(20, std::stoi(to_raw));
  std::string ms_raw = env_or("SOLVER_WORKER_MAX_SOLVES", "auto");
  gw.max_solves = is_auto(ms_raw) ? auto_max_solves() : std::max(1, std::stoi(ms_raw));

  gw.python_bin = env_or("SOLVER_PYTHON", env_or("PYTHON", "python3").c_str());
  gw.headless = env_bool("TURNSTILE_SOLVER_HEADLESS", true);
  // Prefetch default: auto-off under low free RAM
  {
    const char* pref = std::getenv("SOLVER_WORKER_PREFETCH");
    if (!pref || !*pref || is_auto(pref)) {
      gw.prefetch = auto_prefetch();
    } else {
      gw.prefetch = env_bool("SOLVER_WORKER_PREFETCH", false);
    }
  }
  gw.browser_type = env_or("TURNSTILE_SOLVER_BROWSER", "chromium");
  gw.proxy_file = env_or("TURNSTILE_SOLVER_PROXY_FILE", "");
  gw.api_token = env_or("SOLVER_API_TOKEN", env_or("TURNSTILE_SOLVER_TOKEN", "").c_str());

  // resolve paths relative to binary
  char exe[4096];
  ssize_t n = ::readlink("/proc/self/exe", exe, sizeof(exe) - 1);
  std::string root = ".";
  if (n > 0) {
    exe[n] = 0;
    fs::path p(exe);
    // native/solver-gateway-cpp/solver-gateway → project root = ../../
    root = p.parent_path().parent_path().parent_path().string();
    if (root.empty()) root = ".";
  }
  std::string project_root = env_or("PROJECT_ROOT", root.c_str());

  gw.worker_script = env_or(
      "SOLVER_WORKER_SCRIPT",
      (fs::path(project_root) / "native/solver-hybrid/browser_worker.py").string().c_str());
  if (!fs::exists(gw.worker_script)) {
    gw.worker_script = find_path({
        (fs::path(exe).parent_path() / "../solver-hybrid/browser_worker.py").string(),
        "native/solver-hybrid/browser_worker.py",
    });
  }
  gw.work_dir = env_or("SOLVER_GATEWAY_WORK_DIR",
                       (fs::path(project_root) / "logs/turnstile-solver/hybrid").string().c_str());
  fs::create_directories(gw.work_dir);

  ContMem m = container_memory();
  std::fprintf(stderr,
               "[gateway-cpp] plan: cpus=%d mem=%dMB (used=%d avail=%d) workers=%d conc=%d "
               "soft=%dMB hard=%dMB max_solves=%d timeout=%ds prefetch=%d "
               "mimalloc=on tbb=on token=python lowmem=1\n",
               cores, m.total_mb, m.used_mb, m.avail_mb, gw.workers, gw.concurrency, gw.soft_mb,
               gw.hard_mb, gw.max_solves, gw.solve_timeout_sec, gw.prefetch ? 1 : 0);

  if (gw.worker_script.empty() || !fs::exists(gw.worker_script)) {
    std::fprintf(stderr, "[gateway-cpp] browser_worker.py missing: %s\n", gw.worker_script.c_str());
    return 1;
  }

  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);
  std::signal(SIGPIPE, SIG_IGN);

  // worker loops (oneTBB-friendly std::thread pool; jobs via concurrent_queue)
  gw.loops.reserve(static_cast<size_t>(gw.workers));
  for (int i = 0; i < gw.workers; ++i) {
    if (i > 0) std::this_thread::sleep_for(std::chrono::milliseconds(150 * i));
    gw.loops.emplace_back(worker_loop, &gw, i + 1);
  }

  // background purge + memory log + orphan chromium sweep
  std::thread bg([&gw]() {
    while (gw.running.load()) {
      for (int i = 0; i < 12 && gw.running.load(); ++i)
        std::this_thread::sleep_for(std::chrono::seconds(1));
      if (!gw.running.load()) break;
      purge_expired(gw);
      ContMem cm = container_memory();
      if (cm.avail_mb > 0 && cm.avail_mb < 1500) {
        std::fprintf(stderr, "[gateway-cpp] low free RAM avail_mb=%d pressure=%d pending=%lld\n",
                     cm.avail_mb, cm.pressure,
                     static_cast<long long>(gw.pending.load()));
      }
      // Reap stray chromium left by timed-out workers (best-effort)
      if (cm.avail_mb > 0 && cm.avail_mb < 2000) {
        int rc = ::system(
            "ps -eo pid,cmd 2>/dev/null | "
            "awk '/playwright_chromiumdev_profile|solver-chrome-/ && !/awk/ {print $1}' | "
            "while read p; do kill -9 \"$p\" 2>/dev/null; done");
        (void)rc;
      }
    }
  });

  // builtin watchdog thread (replaces Rust solver-watchdog for gateway process)
  std::thread wd([&gw]() {
    while (gw.running.load()) {
      for (int i = 0; i < 8 && gw.running.load(); ++i)
        std::this_thread::sleep_for(std::chrono::seconds(1));
      if (!gw.running.load()) break;
      uint64_t rss = solver_util::process_rss_kb(static_cast<int>(::getpid()));
      ContMem cm = container_memory();
      // dry-run only: never kill self; log if over budget
      uint64_t soft = static_cast<uint64_t>(gw.soft_mb * gw.workers + 400) * 1024ull;
      if (rss > soft && cm.avail_mb < 1000) {
        std::fprintf(stderr,
                     "[gateway-cpp] watchdog note: gateway rss_kb=%llu pressure=%d avail_mb=%d\n",
                     static_cast<unsigned long long>(rss), cm.pressure, cm.avail_mb);
      }
      // write status file for control plane
      try {
        fs::path status = fs::path(gw.work_dir) / "watchdog-status.json";
        jx::Object o;
        o["ok"] = jx::Value(true);
        o["engine"] = jx::Value("cpp");
        o["pid"] = jx::Value(static_cast<double>(::getpid()));
        o["rss_kb"] = jx::Value(static_cast<double>(rss));
        o["pressure"] = jx::Value(cm.pressure);
        o["available_mb"] = jx::Value(cm.avail_mb);
        o["action"] = jx::Value("observe");
        std::ofstream(status.string()) << jx::dumps(jx::Value(o)) << "\n";
      } catch (...) {
      }
    }
  });

  gw.server = std::make_unique<http_tiny::Server>(
      host, port, [&gw](const http_tiny::Request& r) { return handle(gw, r); });

  std::fprintf(stderr,
               "[gateway-cpp] hybrid turnstile listening on http://%s:%d workers=%d "
               "(C++ control + Python token)\n",
               host.c_str(), port, gw.workers);

  if (!gw.server->listen_and_serve(gw.running)) {
    std::fprintf(stderr, "[gateway-cpp] bind failed on %s:%d\n", host.c_str(), port);
    gw.running.store(false);
  }

  gw.running.store(false);
  for (auto& t : gw.loops) {
    if (t.joinable()) t.join();
  }
  if (bg.joinable()) bg.join();
  if (wd.joinable()) wd.join();
  std::fprintf(stderr, "[gateway-cpp] shutdown complete\n");
  return 0;
}
