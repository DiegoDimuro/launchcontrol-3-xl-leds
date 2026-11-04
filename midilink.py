"""MIDI I/O on a worker thread, so a wedged driver can't freeze the LED loop.

On Windows a MIDI call (opening, sending SysEx, closing) can block forever when a USB device
drops out halfway through. Every mido call for a device therefore runs on that device's own
worker thread. The main loop only touches queues and flags, so it never blocks.

A watchdog (run from poll(), on the main thread) times each blocking call. When one takes
longer than HANG_SECONDS, the worker is abandoned (a thread stuck in C can't be killed, so it is
left behind) and a fresh one takes over scanning for the device. Fresh workers back off longer
after each hang so a device that stays wedged doesn't pile up stuck threads.
"""

import queue
import threading
import time

import mido

from fxcore import log

HANG_SECONDS = 3.0      # a single MIDI call taking longer than this counts as hung
SCAN_EVERY = 1.0        # look for the device this often while it's missing
CHECK_EVERY = 2.0       # check the open device is still listed this often
OUT_QUEUE_MAX = 4096    # pending outgoing messages; beyond this the device is clearly stuck


class _Worker:
    """One thread owning one pair of ports. Talks to the main thread only through queues/flags."""

    def __init__(self, label, find, delay):
        self.label, self.find = label, find
        self.out_q = queue.Queue(OUT_QUEUE_MAX)
        self.in_q = queue.Queue()
        self.out = self.inp = None
        self.connected = False
        self.generation = 0          # +1 on every successful open
        self.last_names = []         # MIDI outputs seen by the last scan
        self.busy = None             # (what, since) while inside a MIDI call
        self.stop = False            # asked to finish normally
        self.abandoned = False       # replaced after a hang: if the call ever returns, just close up and exit
        self.next_scan = time.monotonic() + delay
        self.thread = threading.Thread(target=self._run, name=f"midi-{label}", daemon=True)
        self.thread.start()

    # Every blocking call goes through here so the watchdog can time it.
    def _call(self, what, fn, *args):
        self.busy = (what, time.monotonic())
        try:
            return fn(*args)
        finally:
            self.busy = None

    def _run(self):
        checked_at = 0.0
        while not self.stop and not self.abandoned:
            now = time.monotonic()
            if not self.connected:
                if now >= self.next_scan:
                    self.next_scan = now + SCAN_EVERY
                    self._open()
                    checked_at = time.monotonic()
                else:
                    time.sleep(0.05)
                continue

            try:
                msg = self.out_q.get(timeout=0.005)
            except queue.Empty:
                msg = None
            try:
                while msg is not None and not self.abandoned:
                    self._call("send", self.out.send, msg)
                    try:
                        msg = self.out_q.get_nowait()
                    except queue.Empty:
                        msg = None
                if self.inp and not self.abandoned:
                    for m in self._call("read", lambda: list(self.inp.iter_pending())):
                        self.in_q.put(m)
                if now - checked_at > CHECK_EVERY and not self.abandoned:
                    checked_at = now
                    if self.find(self._call("list ports", mido.get_output_names)) is None:
                        self._close()
            except Exception:
                self._close()
        self._close()      # also frees the ports if an abandoned call finally returned

    def _open(self):
        try:
            outs = self._call("list ports", mido.get_output_names)
            self.last_names = outs
            out_name = self.find(outs)
            if out_name is None:
                return
            in_name = self.find(self._call("list ports", mido.get_input_names))
            self.out = self._call(f"open {out_name}", mido.open_output, out_name)
            self.inp = self._call(f"open {in_name}", mido.open_input, in_name) if in_name else None
        except Exception:
            self._close()
            return
        if self.abandoned or self.stop:
            return         # _run closes the ports on the way out
        self._drain(self.out_q)
        self._drain(self.in_q)
        self.connected = True
        self.generation += 1
        log(f"{self.label} connected. Output: {out_name}  Input: {in_name or '(none - controls disabled)'}")

    def _close(self):
        self.connected = False
        for port in (self.out, self.inp):
            try:
                if port:
                    self._call("close", port.close)
            except Exception:
                pass
        self.out = self.inp = None
        self._drain(self.out_q)    # stale LED updates are useless after a reconnect

    @staticmethod
    def _drain(q):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return


class MidiLink:
    """Non-blocking handle on a device's ports, for the main thread.

    find(names) -> the port name to use from a list of port names, or None.
    Call poll() once per frame: it runs the watchdog and returns True right after the device
    (re)connects, which is the cue to put it in DAW mode and repaint everything.
    """

    stuck_threads = 0     # workers abandoned in a hung MIDI call, across all links

    def __init__(self, label, find):
        self.label, self.find = label, find
        self.hangs = 0
        self.worker = _Worker(label, find, delay=0.0)
        self.seen_generation = 0

    @property
    def connected(self):
        return self.worker.connected

    @property
    def last_names(self):
        return self.worker.last_names

    def send(self, msg):
        """Queue a message for the device. Dropped while it isn't connected."""
        if not self.worker.connected:
            return
        try:
            self.worker.out_q.put_nowait(msg)
        except queue.Full:
            pass

    def pending(self):
        out = []
        while True:
            try:
                out.append(self.worker.in_q.get_nowait())
            except queue.Empty:
                return out

    def poll(self):
        """Watchdog + reconnect notice. Returns True once per new connection."""
        w = self.worker
        busy = w.busy
        if busy and time.monotonic() - busy[1] > HANG_SECONDS:
            self.hangs += 1
            MidiLink.stuck_threads += 1
            delay = min(60.0, 2.0 * 2 ** (self.hangs - 1))
            log(f"{self.label}: MIDI call '{busy[0]}' hung for over {HANG_SECONDS:.0f} s - abandoning the "
                f"port and retrying in {delay:.0f} s (stuck threads so far: {MidiLink.stuck_threads}). "
                "Replugging the device usually clears this.")
            w.abandoned = True
            w.connected = False
            self.worker = _Worker(self.label, self.find, delay=delay)
            self.seen_generation = 0
            return False
        if w.generation != self.seen_generation and w.connected:
            self.seen_generation = w.generation
            return True
        return False

    def wait(self, timeout):
        """Wait up to timeout for a connection (startup only). Returns whether it's connected."""
        end = time.monotonic() + timeout
        while not self.worker.connected and time.monotonic() < end:
            time.sleep(0.02)
        return self.worker.connected

    def close(self, timeout=1.0):
        """Send what's queued (for up to timeout), then close the ports. Never blocks past timeout."""
        w = self.worker
        end = time.monotonic() + timeout
        while w.connected and not w.out_q.empty() and time.monotonic() < end:
            time.sleep(0.01)
        w.stop = True
        w.thread.join(max(0.0, end - time.monotonic()) + 0.5)
        if w.thread.is_alive():
            MidiLink.stuck_threads += 1
