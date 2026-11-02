#pragma once

#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace melty {
enum class InputKind { Begin, Move, End, Cancel, CancelAll, Text, Backspace };

struct InputEvent {
    InputKind kind;
    uint64_t touch = 0;
    double timestamp = 0;
    double x = 0, y = 0, pressure = 0;
    bool pencil = false;
    std::string text{};
    double velocityX = 0, velocityY = 0;
};

// UIKit never acquires the GIL. Drain immediately before evaluating a frame.
class InputQueue {
public:
    explicit InputQueue(size_t limit = 2048) : limit_(limit < 2 ? 2 : limit) {}

    void push(InputEvent event) {
        std::lock_guard<std::mutex> lock(mutex_);
        append(std::move(event));
    }

    void pushBatch(std::vector<InputEvent> events) {
        // One UIKit callback is one publication. A concurrent drain must never
        // split its coalesced history and defer the latest position a frame.
        std::lock_guard<std::mutex> lock(mutex_);
        for (auto &event : events) append(std::move(event));
    }

    std::vector<InputEvent> drain() {
        std::lock_guard<std::mutex> lock(mutex_);
        std::vector<InputEvent> result;
        result.reserve(events_.size());
        while (!events_.empty()) {
            result.push_back(std::move(events_.front()));
            events_.pop_front();
        }
        return result;
    }

private:
    void append(InputEvent event) {
        if (events_.size() >= limit_) {
            // A stalled Python callback must not accumulate unbounded input or
            // leave a control pressed after dropping its corresponding release.
            events_.clear();
            events_.push_back({InputKind::CancelAll, 0, event.timestamp});
        }
        events_.push_back(std::move(event));
    }

    size_t limit_;
    std::mutex mutex_;
    std::deque<InputEvent> events_;
};
}  // namespace melty
