// Minimal accept-loop + thread-per-connection HTTP/1.1 server.
#pragma once
#include <atomic>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace http_tiny {

struct Request {
  std::string method;
  std::string path;   // without query
  std::string query;  // without leading '?'
  std::string body;
  std::vector<std::pair<std::string, std::string>> headers;

  std::string header(const std::string& name) const;
};

struct Response {
  int status = 200;
  std::string content_type = "application/json";
  std::string body;
};

using Handler = std::function<Response(const Request&)>;

class Server {
 public:
  Server(std::string host, int port, Handler handler);
  ~Server();
  // Blocks until running becomes false or fatal error. Returns false on bind failure.
  bool listen_and_serve(std::atomic<bool>& running);
  void request_stop();

 private:
  std::string host_;
  int port_;
  Handler handler_;
  int fd_ = -1;
};

}  // namespace http_tiny
