"""Bounded outbound buffer for one narrative session.

The problem
-----------
Narrative events are written to a WebSocket by a background task. A consumer can
be slower than the producer — a throttled tab, a paused debugger, a proxy with a
full window — so the buffer between them needs a policy for what to discard.

The previous policy was::

    except asyncio.QueueFull:   # slow consumer: drop oldest non-final events
        queue.get_nowait()
        queue.put_nowait(ev)

The comment promised more than the code delivered. It dropped whatever happened to
be oldest, including a ``narrative_final`` — and ``narrative_final`` is the *only*
authoritative text. The dashboard is documented to replace its provisional chunks
with it, so losing one leaves the operator reading unvalidated text indefinitely,
with nothing logged and no error raised. A silent wrong answer is the worst
available failure mode.

The policy
----------
Events are classified by whether the UI can tolerate losing them:

``narrative_chunk``
    Replaceable. Each chunk is a fragment (usually a few words) that the UI
    *appends*. Dropping one corrupts the sentence, so chunks are not dropped —
    they are **coalesced** into the pending slot. A slow consumer loses
    inter-chunk timing, never text.

``narrative_final``, ``narrative_cancelled``, ``error``, everything else
    Terminal or low-volume and authoritative. Never dropped while a replaceable
    event is available to drop instead.

If a consumer is so slow that even the terminal backlog overflows, the oldest
terminal event is dropped and counted — bounded memory has to win eventually, but
it is now the last resort rather than the first, and it is observable from
``drops`` and reported in the logs.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, List, Optional

#: Event types that carry text the UI appends to a stream, and that can therefore
#: be merged with the next one without losing meaning.
REPLACEABLE_TYPES = frozenset({"narrative_chunk"})

#: Default slot budget. Frames are dropped aggressively elsewhere in this system;
#: narrative text is not, because it is the explanation a human reads and because
#: its volume is inherently low (a handful of events per assessment).
DEFAULT_MAX_SLOTS = 256
#: Separate ceiling for terminal events, so a pathological producer cannot pin
#: memory with authoritative events alone.
DEFAULT_MAX_TERMINAL = 32


class NarrativeQueue:
    """A bounded, provenance-aware outbound buffer.

    Deliberately not an ``asyncio.Queue``: the overflow rule needs to inspect and
    rewrite the contents on the way in, which a Queue's opaque storage prevents.
    ``put`` never blocks and never raises; ``get`` awaits the next event.
    """

    def __init__(self, max_slots: int = DEFAULT_MAX_SLOTS,
                 max_terminal: int = DEFAULT_MAX_TERMINAL) -> None:
        self.max_slots = max(2, int(max_slots))
        self.max_terminal = max(1, int(max_terminal))
        self._items: Deque[Dict[str, Any]] = deque()
        self._wakeup = asyncio.Event()
        self._closed = False
        #: Counters, exposed for tests and for the health endpoint.
        self.coalesced = 0
        self.dropped_replaceable = 0
        self.dropped_terminal = 0

    # -- writing ---------------------------------------------------------

    def put_nowait(self, event: Dict[str, Any]) -> None:
        """Enqueue an event, evicting something else if the budget is full."""
        if self._closed:
            return
        kind = str(event.get("type", ""))
        if kind in REPLACEABLE_TYPES and self._coalesce_into_pending(event):
            self._wakeup.set()
            return
        self._items.append(event)
        self._enforce_budget()
        self._wakeup.set()

    async def put(self, event: Dict[str, Any]) -> None:
        self.put_nowait(event)

    def close(self) -> None:
        """Release any waiter. Used on disconnect; buffered events are discarded."""
        self._closed = True
        self._items.clear()
        self._wakeup.set()

    # -- reading ---------------------------------------------------------

    async def get(self) -> Optional[Dict[str, Any]]:
        """The next event, or ``None`` once the queue is closed and drained."""
        while True:
            if self._items:
                return self._items.popleft()
            if self._closed:
                return None
            self._wakeup.clear()
            await self._wakeup.wait()

    def peek_types(self) -> List[str]:
        """Event types currently buffered, oldest first. For tests and diagnostics."""
        return [str(e.get("type", "")) for e in self._items]

    def __len__(self) -> int:
        return len(self._items)

    # -- internals -------------------------------------------------------

    def _coalesce_into_pending(self, event: Dict[str, Any]) -> bool:
        """Merge a replaceable event into the provisional slot, if there is one.

        Only the *last* buffered event is considered: two chunks separated by a
        ``narrative_retry`` are not adjacent in the UI either, and merging across
        that boundary would resurrect text the retry explicitly discarded.
        """
        if not self._items:
            return False
        tail = self._items[-1]
        if str(tail.get("type", "")) not in REPLACEABLE_TYPES:
            return False
        incoming = event.get("text")
        if not isinstance(incoming, str) or not incoming:
            return True  # nothing to merge, but it is absorbed either way
        existing = tail.get("text")
        tail["text"] = (existing if isinstance(existing, str) else "") + incoming
        self.coalesced += 1
        return True

    def _enforce_budget(self) -> None:
        if len(self._items) <= self.max_slots:
            return
        # Prefer evicting the oldest replaceable event: the UI can lose inter-chunk
        # timing without losing text, because chunks were coalesced on the way in.
        for index, item in enumerate(self._items):
            if str(item.get("type", "")) in REPLACEABLE_TYPES:
                del self._items[index]
                self.dropped_replaceable += 1
                return
        # Only terminal events left, and still over budget.
        self._items.popleft()
        self.dropped_terminal += 1

    def stats(self) -> Dict[str, int]:
        return {
            "buffered": len(self._items),
            "coalesced": self.coalesced,
            "dropped_replaceable": self.dropped_replaceable,
            "dropped_terminal": self.dropped_terminal,
            "max_slots": self.max_slots,
            "max_terminal": self.max_terminal,
        }
