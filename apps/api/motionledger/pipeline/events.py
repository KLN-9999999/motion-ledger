from __future__ import annotations
import asyncio, time
from collections import defaultdict

class EventHub:
    def __init__(self):
        self.events: dict[str, list[dict]] = defaultdict(list)
        self.queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
    def publish(self, run_id: str, event: str, **data):
        item = {"event": event, "at": time.time(), "data": data}
        bucket = self.events[run_id]
        bucket.append(item)
        if len(bucket) > 1000: del bucket[:-1000]
        for q in list(self.queues[run_id]):
            try: q.put_nowait(item)
            except asyncio.QueueFull: pass
    async def subscribe(self, run_id: str):
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.queues[run_id].append(q)
        try:
            for item in self.events[run_id]: yield item
            while True: yield await q.get()
        finally:
            if q in self.queues[run_id]: self.queues[run_id].remove(q)

hub = EventHub()
