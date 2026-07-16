"""LogBuffer tests — snapshot ordering, ring cap, subscriber delivery."""

from __future__ import annotations

import asyncio

from app.supervisor.logbuffer import LogBuffer


def test_snapshot_preserves_order():
    buf = LogBuffer()
    for i in range(5):
        buf.append(f"line {i}")
    assert buf.snapshot() == [f"line {i}" for i in range(5)]


def test_ring_cap_drops_oldest():
    buf = LogBuffer(maxlen=3)
    for i in range(5):
        buf.append(f"l{i}")
    assert buf.snapshot() == [
        "[mcpelevator] 2 earlier log line(s) omitted (buffer limit)",
        "l2",
        "l3",
        "l4",
    ]


async def test_subscriber_receives_lines_appended_after_subscribe():
    buf = LogBuffer()
    q = buf.subscribe()
    buf.append("hello")
    buf.append("world")
    assert await asyncio.wait_for(q.get(), 1) == "hello"
    assert await asyncio.wait_for(q.get(), 1) == "world"


async def test_unsubscribe_stops_delivery():
    buf = LogBuffer()
    q = buf.subscribe()
    buf.append("a")
    assert await asyncio.wait_for(q.get(), 1) == "a"
    buf.unsubscribe(q)
    buf.append("b")  # must NOT reach the unsubscribed queue
    assert q.empty()
