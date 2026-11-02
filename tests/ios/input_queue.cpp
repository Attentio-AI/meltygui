#include "InputQueue.hpp"
#include <cassert>
#include <thread>

int main() {
    using namespace melty;
    InputQueue queue(3);
    queue.push({InputKind::Begin, 42, 1.0});
    queue.push({InputKind::Move, 42, 1.1});
    InputEvent release{InputKind::End, 42, 1.2};
    release.velocityX = 12.5;
    release.velocityY = -1800.25;
    queue.push(std::move(release));
    auto events = queue.drain();
    assert(events.size() == 3);
    assert(events[0].kind == InputKind::Begin);
    assert(events[2].kind == InputKind::End);
    assert(events[2].touch == 42 && events[2].timestamp == 1.2);
    assert(events[2].velocityX == 12.5 && events[2].velocityY == -1800.25);
    assert(queue.drain().empty());
    for (int i = 0; i < 4; ++i) queue.push({InputKind::Move, 42, (double)i});
    events = queue.drain();
    assert(events.size() == 2);
    assert(events[0].kind == InputKind::CancelAll);
    assert(events[0].timestamp == 3.0);
    assert(events[1].kind == InputKind::Move);

    // A callback batch shares the single-event overflow/cancellation contract.
    InputQueue bounded(3);
    bounded.push({InputKind::Begin, 8, 1});
    bounded.pushBatch({{InputKind::Move, 8, 2}, {InputKind::Move, 8, 3},
                       {InputKind::Move, 8, 4}, {InputKind::End, 8, 5}});
    events = bounded.drain();
    assert(events.size() == 3 && events[0].kind == InputKind::CancelAll);
    assert(events[1].timestamp == 4 && events[2].kind == InputKind::End);
    bounded.pushBatch({});
    assert(bounded.drain().empty());

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

    // Draining concurrently can combine callbacks, but never split a callback
    // and leave its newest sample to be displayed in a later frame.
    constexpr size_t batches = 1000, samples = 32;
    InputQueue batched(batches * samples + 1);
    std::thread batchProducer([&] {
        for (size_t batch = 0; batch < batches; ++batch) {
            std::vector<InputEvent> input;
            for (size_t sample = 0; sample < samples; ++sample)
                input.push_back({InputKind::Move, batch, double(batch * samples + sample)});
            batched.pushBatch(std::move(input));
            std::this_thread::yield();
        }
        batched.push({InputKind::End, 0, double(batches * samples)});
    });
    size_t count = 0;
    ended = false;
    while (!ended) {
        events = batched.drain();
        if (!events.empty() && events.back().kind == InputKind::End) {
            ended = true;
            events.pop_back();
        }
        assert(events.size() % samples == 0);
        for (const auto &event : events) {
            assert(event.timestamp == double(count));
            assert(event.touch == count / samples);
            ++count;
        }
        std::this_thread::yield();
    }
    batchProducer.join();
    assert(count == batches * samples);
}
