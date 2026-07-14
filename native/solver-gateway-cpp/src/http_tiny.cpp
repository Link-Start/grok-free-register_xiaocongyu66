#include "http_tiny.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cctype>
#include <cerrno>
#include <cstring>
#include <sstream>
#include <thread>

namespace http_tiny {

std::string Request::header(const std::string& name) const {
  auto lower = [](std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
  };
  std::string want = lower(name);
  for (const auto& kv : headers) {
    if (lower(kv.first) == want) return kv.second;
  }
  return {};
}

Server::Server(std::string host, int port, Handler handler)
    : host_(std::move(host)), port_(port), handler_(std::move(handler)) {}

Server::~Server() {
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
}

void Server::request_stop() {
  if (fd_ >= 0) {
    ::shutdown(fd_, SHUT_RDWR);
  }
}

static bool read_request(int cfd, Request& out) {
  std::string buf;
  buf.reserve(4096);
  char tmp[2048];
  // read until headers end
  while (buf.find("\r\n\r\n") == std::string::npos && buf.size() < 1 << 20) {
    ssize_t n = ::recv(cfd, tmp, sizeof(tmp), 0);
    if (n <= 0) return false;
    buf.append(tmp, static_cast<size_t>(n));
  }
  size_t hdr_end = buf.find("\r\n\r\n");
  if (hdr_end == std::string::npos) return false;
  std::string head = buf.substr(0, hdr_end);
  std::string rest = buf.substr(hdr_end + 4);

  std::istringstream hs(head);
  std::string request_line;
  if (!std::getline(hs, request_line)) return false;
  if (!request_line.empty() && request_line.back() == '\r') request_line.pop_back();
  {
    std::istringstream rl(request_line);
    std::string target, version;
    rl >> out.method >> target >> version;
    size_t q = target.find('?');
    if (q == std::string::npos) {
      out.path = target;
      out.query.clear();
    } else {
      out.path = target.substr(0, q);
      out.query = target.substr(q + 1);
    }
  }
  std::string line;
  size_t content_length = 0;
  while (std::getline(hs, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    size_t colon = line.find(':');
    if (colon == std::string::npos) continue;
    std::string k = line.substr(0, colon);
    std::string v = line.substr(colon + 1);
    while (!v.empty() && (v.front() == ' ' || v.front() == '\t')) v.erase(v.begin());
    out.headers.emplace_back(k, v);
    std::string kl = k;
    for (char& c : kl) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (kl == "content-length") {
      try {
        content_length = static_cast<size_t>(std::stoul(v));
      } catch (...) {
        content_length = 0;
      }
    }
  }
  out.body = rest;
  while (out.body.size() < content_length && out.body.size() < 2 << 20) {
    ssize_t n = ::recv(cfd, tmp, sizeof(tmp), 0);
    if (n <= 0) break;
    out.body.append(tmp, static_cast<size_t>(n));
  }
  if (out.body.size() > content_length) out.body.resize(content_length);
  return true;
}

static void write_response(int cfd, const Response& resp) {
  std::ostringstream o;
  o << "HTTP/1.1 " << resp.status << " OK\r\n"
    << "Content-Type: " << resp.content_type << "\r\n"
    << "Content-Length: " << resp.body.size() << "\r\n"
    << "Connection: close\r\n"
    << "\r\n"
    << resp.body;
  std::string s = o.str();
  size_t off = 0;
  while (off < s.size()) {
    ssize_t n = ::send(cfd, s.data() + off, s.size() - off, MSG_NOSIGNAL);
    if (n <= 0) break;
    off += static_cast<size_t>(n);
  }
}

bool Server::listen_and_serve(std::atomic<bool>& running) {
  fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd_ < 0) return false;
  int yes = 1;
  ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(static_cast<uint16_t>(port_));
  if (host_ == "0.0.0.0" || host_.empty()) {
    addr.sin_addr.s_addr = INADDR_ANY;
  } else if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
    addr.sin_addr.s_addr = INADDR_ANY;
  }
  if (::bind(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
    ::close(fd_);
    fd_ = -1;
    return false;
  }
  if (::listen(fd_, 128) < 0) {
    ::close(fd_);
    fd_ = -1;
    return false;
  }

  while (running.load()) {
    sockaddr_in cli{};
    socklen_t clen = sizeof(cli);
    int cfd = ::accept(fd_, reinterpret_cast<sockaddr*>(&cli), &clen);
    if (cfd < 0) {
      if (!running.load()) break;
      if (errno == EINTR) continue;
      if (errno == EBADF || errno == EINVAL) break;
      continue;
    }
    std::thread([this, cfd]() {
      Request req;
      Response resp;
      if (!read_request(cfd, req)) {
        resp.status = 400;
        resp.body = R"({"ok":false,"error":"bad request"})";
      } else {
        try {
          resp = handler_(req);
        } catch (const std::exception& ex) {
          resp.status = 500;
          resp.body = std::string(R"({"ok":false,"error":")") + ex.what() + "\"}";
        } catch (...) {
          resp.status = 500;
          resp.body = R"({"ok":false,"error":"internal"})";
        }
      }
      write_response(cfd, resp);
      ::close(cfd);
    }).detach();
  }
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
  return true;
}

}  // namespace http_tiny
