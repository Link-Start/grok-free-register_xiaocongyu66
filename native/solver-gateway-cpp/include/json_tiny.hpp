// Minimal JSON helpers for solver-gateway-cpp (no external JSON lib).
#pragma once
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <sstream>
#include <string>
#include <variant>
#include <vector>

namespace jx {

struct Value;
using Object = std::map<std::string, Value>;
using Array = std::vector<Value>;
using Null = std::nullptr_t;

struct Value {
  using Var = std::variant<Null, bool, double, std::string, Array, Object>;
  Var v{nullptr};

  Value() = default;
  Value(Null) : v(nullptr) {}
  Value(bool b) : v(b) {}
  Value(int n) : v(static_cast<double>(n)) {}
  Value(int64_t n) : v(static_cast<double>(n)) {}
  Value(double d) : v(d) {}
  Value(const char* s) : v(std::string(s ? s : "")) {}
  Value(std::string s) : v(std::move(s)) {}
  Value(Array a) : v(std::move(a)) {}
  Value(Object o) : v(std::move(o)) {}

  bool is_null() const { return std::holds_alternative<Null>(v); }
  bool is_bool() const { return std::holds_alternative<bool>(v); }
  bool is_num() const { return std::holds_alternative<double>(v); }
  bool is_str() const { return std::holds_alternative<std::string>(v); }
  bool is_obj() const { return std::holds_alternative<Object>(v); }
  bool is_arr() const { return std::holds_alternative<Array>(v); }

  bool as_bool(bool def = false) const {
    if (auto* p = std::get_if<bool>(&v)) return *p;
    return def;
  }
  double as_num(double def = 0) const {
    if (auto* p = std::get_if<double>(&v)) return *p;
    return def;
  }
  int64_t as_i64(int64_t def = 0) const { return static_cast<int64_t>(as_num(static_cast<double>(def))); }
  const std::string& as_str(const std::string& def = empty()) const {
    if (auto* p = std::get_if<std::string>(&v)) return *p;
    return def;
  }
  const Object& as_obj() const {
    static const Object kEmpty;
    if (auto* p = std::get_if<Object>(&v)) return *p;
    return kEmpty;
  }
  Object& as_obj_mut() {
    if (!std::holds_alternative<Object>(v)) v = Object{};
    return std::get<Object>(v);
  }

  Value& operator[](const std::string& k) { return as_obj_mut()[k]; }
  Value get(const std::string& k) const {
    const auto& o = as_obj();
    auto it = o.find(k);
    return it == o.end() ? Value{} : it->second;
  }

 private:
  static const std::string& empty() {
    static const std::string e;
    return e;
  }
};

inline void escape(std::ostringstream& o, const std::string& s) {
  o << '"';
  for (unsigned char c : s) {
    switch (c) {
      case '"': o << "\\\""; break;
      case '\\': o << "\\\\"; break;
      case '\b': o << "\\b"; break;
      case '\f': o << "\\f"; break;
      case '\n': o << "\\n"; break;
      case '\r': o << "\\r"; break;
      case '\t': o << "\\t"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          o << buf;
        } else {
          o << static_cast<char>(c);
        }
    }
  }
  o << '"';
}

inline void dump(std::ostringstream& o, const Value& v) {
  if (std::holds_alternative<Null>(v.v)) {
    o << "null";
  } else if (auto* b = std::get_if<bool>(&v.v)) {
    o << (*b ? "true" : "false");
  } else if (auto* d = std::get_if<double>(&v.v)) {
    if (std::isfinite(*d) && std::floor(*d) == *d && std::abs(*d) < 1e15) {
      o << static_cast<long long>(*d);
    } else {
      o << *d;
    }
  } else if (auto* s = std::get_if<std::string>(&v.v)) {
    escape(o, *s);
  } else if (auto* a = std::get_if<Array>(&v.v)) {
    o << '[';
    for (size_t i = 0; i < a->size(); ++i) {
      if (i) o << ',';
      dump(o, (*a)[i]);
    }
    o << ']';
  } else if (auto* m = std::get_if<Object>(&v.v)) {
    o << '{';
    bool first = true;
    for (const auto& kv : *m) {
      if (!first) o << ',';
      first = false;
      escape(o, kv.first);
      o << ':';
      dump(o, kv.second);
    }
    o << '}';
  }
}

inline std::string dumps(const Value& v) {
  std::ostringstream o;
  dump(o, v);
  return o.str();
}

// Very small recursive-descent parser (enough for worker IPC + request bodies).
class Parser {
 public:
  explicit Parser(std::string s) : s_(std::move(s)) {}
  Value parse() {
    skip();
    Value v = parse_value();
    return v;
  }

 private:
  std::string s_;
  size_t i_ = 0;

  void skip() {
    while (i_ < s_.size() && std::isspace(static_cast<unsigned char>(s_[i_]))) ++i_;
  }
  char peek() const { return i_ < s_.size() ? s_[i_] : '\0'; }
  char get() { return i_ < s_.size() ? s_[i_++] : '\0'; }
  bool match(const char* lit) {
    size_t n = std::strlen(lit);
    if (s_.compare(i_, n, lit) != 0) return false;
    i_ += n;
    return true;
  }

  Value parse_value() {
    skip();
    char c = peek();
    if (c == '{') return parse_object();
    if (c == '[') return parse_array();
    if (c == '"') return parse_string();
    if (c == 't' && match("true")) return Value(true);
    if (c == 'f' && match("false")) return Value(false);
    if (c == 'n' && match("null")) return Value(nullptr);
    if (c == '-' || std::isdigit(static_cast<unsigned char>(c))) return parse_number();
    return Value{};
  }

  Value parse_object() {
    get();  // {
    Object o;
    skip();
    if (peek() == '}') {
      get();
      return Value(std::move(o));
    }
    while (true) {
      skip();
      if (peek() != '"') break;
      std::string key = parse_string().as_str();
      skip();
      if (get() != ':') break;
      o.emplace(std::move(key), parse_value());
      skip();
      char c = get();
      if (c == '}') break;
      if (c != ',') break;
    }
    return Value(std::move(o));
  }

  Value parse_array() {
    get();  // [
    Array a;
    skip();
    if (peek() == ']') {
      get();
      return Value(std::move(a));
    }
    while (true) {
      a.push_back(parse_value());
      skip();
      char c = get();
      if (c == ']') break;
      if (c != ',') break;
    }
    return Value(std::move(a));
  }

  Value parse_string() {
    get();  // "
    std::string out;
    while (i_ < s_.size()) {
      char c = get();
      if (c == '"') break;
      if (c == '\\') {
        char e = get();
        switch (e) {
          case '"':
          case '\\':
          case '/': out.push_back(e); break;
          case 'b': out.push_back('\b'); break;
          case 'f': out.push_back('\f'); break;
          case 'n': out.push_back('\n'); break;
          case 'r': out.push_back('\r'); break;
          case 't': out.push_back('\t'); break;
          case 'u': {
            unsigned code = 0;
            for (int k = 0; k < 4; ++k) {
              char h = get();
              code <<= 4;
              if (h >= '0' && h <= '9') code |= h - '0';
              else if (h >= 'a' && h <= 'f') code |= h - 'a' + 10;
              else if (h >= 'A' && h <= 'F') code |= h - 'A' + 10;
            }
            if (code < 0x80) out.push_back(static_cast<char>(code));
            else if (code < 0x800) {
              out.push_back(static_cast<char>(0xC0 | (code >> 6)));
              out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
            } else {
              out.push_back(static_cast<char>(0xE0 | (code >> 12)));
              out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
              out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
            }
            break;
          }
          default: out.push_back(e); break;
        }
      } else {
        out.push_back(c);
      }
    }
    return Value(std::move(out));
  }

  Value parse_number() {
    size_t start = i_;
    if (peek() == '-') get();
    while (std::isdigit(static_cast<unsigned char>(peek()))) get();
    if (peek() == '.') {
      get();
      while (std::isdigit(static_cast<unsigned char>(peek()))) get();
    }
    if (peek() == 'e' || peek() == 'E') {
      get();
      if (peek() == '+' || peek() == '-') get();
      while (std::isdigit(static_cast<unsigned char>(peek()))) get();
    }
    try {
      return Value(std::stod(s_.substr(start, i_ - start)));
    } catch (...) {
      return Value(0.0);
    }
  }
};

inline Value loads(const std::string& s) {
  try {
    return Parser(s).parse();
  } catch (...) {
    return Value{};
  }
}

inline std::string query_get(const std::string& qs, const std::string& key) {
  // qs without leading '?'
  size_t i = 0;
  while (i < qs.size()) {
    size_t amp = qs.find('&', i);
    if (amp == std::string::npos) amp = qs.size();
    std::string pair = qs.substr(i, amp - i);
    size_t eq = pair.find('=');
    std::string k = eq == std::string::npos ? pair : pair.substr(0, eq);
    std::string v = eq == std::string::npos ? "" : pair.substr(eq + 1);
    // crude URL decode of + and %XX for common cases
    auto decode = [](std::string s) {
      std::string o;
      for (size_t j = 0; j < s.size(); ++j) {
        if (s[j] == '+') o.push_back(' ');
        else if (s[j] == '%' && j + 2 < s.size()) {
          auto hex = [](char c) -> int {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return -1;
          };
          int hi = hex(s[j + 1]), lo = hex(s[j + 2]);
          if (hi >= 0 && lo >= 0) {
            o.push_back(static_cast<char>((hi << 4) | lo));
            j += 2;
          } else o.push_back(s[j]);
        } else o.push_back(s[j]);
      }
      return o;
    };
    if (decode(k) == key) return decode(v);
    i = amp + 1;
  }
  return {};
}

}  // namespace jx
