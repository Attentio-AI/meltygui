#include "InputQueue.hpp"
#include <cassert>
#include <thread>

int main() {
    using namespace melty;
    InputQueue queue(3);
    queue.push({InputKind::Begin, 42, 1.0});
    queue.push({InputKind::Move, 42, 1.1});
    queue.push({InputKind::End, 42, 1.2});
    auto events = queue.drain();
    assert(events.size() == 3);
    assert(events[0].kind == InputKind::Begin);
    assert(events[2].kind == InputKind::End);
    assert(events[2].touch == 42 && events[2].timestamp == 1.2);
    assert(queue.drain().empty());
    for (int i = 0; i < 4; ++i) queue.push({InputKind::Move, 42, (double)i});
    events = queue.drain();
    assert(events.size() == 2);
    assert(events[0].kind == InputKind::CancelAll);
    assert(events[0].timestamp == 3.0);
    assert(events[1].kind == InputKind::Move);

    InputQueue concurrent(10000);
    std::thread producer([&] {
        for (int i = 0; i < 1000; ++i) concurrent.push({InputKind::Move, 7, (double)i});
        concurrent.push({InputKind::End, 7, 1000});
    });
    std::vector<InputEvent> received;
    bool ended = false;
    while (!ended) {
        for (auto &event : concurrent.drain()) {
            received.push_back(event);
            ended |= event.kind == InputKind::End;
        }
        std::this_thread::yield();
    }
    producer.join();
    assert(received.size() == 1001);
    for (size_t i = 0; i < received.size(); ++i) assert(received[i].timestamp == (double)i);
}
