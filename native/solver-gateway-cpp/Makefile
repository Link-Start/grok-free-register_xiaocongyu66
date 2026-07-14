# Pure C++ hybrid Turnstile gateway (oneTBB + mimalloc).
# Python browser_worker.py remains the only non-C++ piece (token fetch).

CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -pthread
CPPFLAGS += -Iinclude -I../solver-util -DSOLVER_USE_MIMALLOC
LDFLAGS  += -pthread

# oneTBB
TBB_LIBS := $(shell pkg-config --libs tbb 2>/dev/null)
ifeq ($(strip $(TBB_LIBS)),)
  TBB_LIBS := -ltbb
endif
TBB_CFLAGS := $(shell pkg-config --cflags tbb 2>/dev/null)

# mimalloc
MIMALLOC_LIBS := $(shell pkg-config --libs mimalloc 2>/dev/null)
ifeq ($(strip $(MIMALLOC_LIBS)),)
  # Debian: -lmimalloc or -lmimalloc.so.3
  ifneq ($(wildcard /usr/lib/*/libmimalloc.so*),)
    MIMALLOC_LIBS := -lmimalloc
  else
    MIMALLOC_LIBS := -lmimalloc
  endif
endif

TARGET := solver-gateway
SRCS   := src/http_tiny.cpp src/gateway.cpp
OBJS   := $(SRCS:.cpp=.o)

.PHONY: all clean install version

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS) $(TBB_LIBS) $(MIMALLOC_LIBS)
	@echo "built $@"
	@./$(TARGET) version || true

src/%.o: src/%.cpp include/*.hpp ../solver-util/solver_util.hpp
	$(CXX) $(CXXFLAGS) $(CPPFLAGS) $(TBB_CFLAGS) -c -o $@ $<

clean:
	rm -f $(OBJS) $(TARGET)

version: $(TARGET)
	./$(TARGET) version
