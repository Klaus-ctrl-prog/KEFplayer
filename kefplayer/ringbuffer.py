"""Begrenzter Byte-Ringpuffer fuer Live-Audio (nur Arbeitsspeicher, nichts auf Platte). Phase E1b.

Ueberlauf: aelteste Bytes werden verworfen (Live: Aktualitaet vor Vollstaendigkeit) und gezaehlt.
Unterlauf entscheidet der Verbraucher (Stille einfuegen); get() meldet nur 'nichts da' (b"").
"""
from __future__ import annotations

import threading
import time
from collections import deque


class ByteRing:
    """frame_bytes > 1 (z. B. 6 fuer Stereo-PCM-24): Kapazitaet, Ueberlauf und get() arbeiten nur in ganzen Frames,
    damit eine verworfene Teilmenge nie die Sample-/Kanalphase des Stroms verschiebt (F-03)."""

    def __init__(self, capacity: int, frame_bytes: int = 1):
        if capacity < 1:
            raise ValueError("capacity muss >= 1 sein")
        if frame_bytes < 1:
            raise ValueError("frame_bytes muss >= 1 sein")
        self.frame_bytes = frame_bytes
        self.capacity = max(frame_bytes, capacity - capacity % frame_bytes)  # auf ganze Frames abrunden (mind. 1 Frame)
        self._partial = b""  # angefangenes Frame am Ende der letzten put()-Daten
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._cv = threading.Condition()
        self._closed = False
        self.overrun_bytes = 0

    def __len__(self) -> int:
        with self._cv:
            return self._size

    def put(self, data: bytes) -> None:
        if not data:
            return
        with self._cv:
            if self._closed:
                return
            fb = self.frame_bytes
            if fb > 1:  # nur ganze Frames aufnehmen; Rest wartet auf die naechsten Daten
                data = self._partial + data
                cut = len(data) - len(data) % fb
                self._partial, data = data[cut:], data[:cut]
                if not data:
                    return
            if len(data) > self.capacity:  # nur die neuesten capacity Bytes behalten
                self.overrun_bytes += len(data) - self.capacity + self._size
                self._chunks.clear()
                self._size = 0
                data = data[-self.capacity:]
            self._chunks.append(data)
            self._size += len(data)
            while self._size > self.capacity:
                over = self._size - self.capacity  # ist bei frame_bytes > 1 ein Vielfaches, da alle Chunks ganze Frames sind
                head = self._chunks[0]
                if len(head) <= over:
                    self._chunks.popleft()
                    self._size -= len(head)
                    self.overrun_bytes += len(head)
                else:
                    self._chunks[0] = head[over:]
                    self._size -= over
                    self.overrun_bytes += over
            self._cv.notify_all()

    def get(self, n: int, timeout: float | None = None) -> bytes:
        """Bis zu n Bytes. Wartet hoechstens timeout auf Daten; b"" bei Zeitablauf oder wenn geschlossen und leer."""
        if self.frame_bytes > 1:  # nur ganze Frames ausgeben (mind. eines)
            n = max(self.frame_bytes, n - n % self.frame_bytes)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._size == 0:
                if self._closed:
                    return b""
                rem = None if deadline is None else deadline - time.monotonic()
                if rem is not None and rem <= 0:
                    return b""
                self._cv.wait(rem)
            out = bytearray()
            while self._chunks and len(out) < n:
                head = self._chunks[0]
                take = n - len(out)
                if len(head) <= take:
                    out += head
                    self._chunks.popleft()
                else:
                    out += head[:take]
                    self._chunks[0] = head[take:]
            self._size -= len(out)
            return bytes(out)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()
