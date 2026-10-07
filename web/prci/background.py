# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/background.py
# Expensive answers, worked out once and off the request
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
"""An answer that takes seconds, asked for every two.

Two of the things the page shows are scripts, not lookups: whether the
series is prepared is ``<distro>/ready.sh``, and whether the host can
build the tree is ``lib/hostcheck.sh``.  Running the script is the only
honest way to answer either -- it is the same script the run itself
will consult -- and on a ten-patch series against a full kernel mirror
the first of them takes the better part of a minute.

A cache was there from the start and it was not enough, because of what
happens in the gap before the first answer lands.  The page polls every
two seconds; the answer takes thirty-five; so the poll that misses the
cache starts a run, and so does the next one, and the one after that.
Seventeen copies of the same script, each on its own core, each forking
git at a mirror of Linux.  The machine the browser is on is the machine
that is doing it, so the whole interface goes to treacle -- and the
harder the user clicks, the worse it gets.

So: one run at a time, and no page waits for it.  A caller that misses
the cache is handed the last answer there was and the run happens
behind it; the next poll, two seconds later, picks up the new one.  The
places that must not guess -- the gate in front of starting a job --
ask to wait, and they wait for the same single run rather than adding
another.

What the cache is keyed on does the rest, and it is worth keeping it
that way: every input to these answers is on disk, so a stamp made of
the right mtimes means the script is re-run when something has actually
changed and not on a timer.
"""

import threading
import time


class Answer:
    """One expensive answer per key, kept fresh without being waited on.

    ``ttl`` is a backstop for inputs the stamp cannot see; the stamp is
    the real mechanism.
    """

    def __init__(self, ttl):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._answers = {}
        self._busy = {}

    def get(self, key, stamp, work, wait=True, force=False, unknown=None):
        """The answer for ``key``, running ``work()`` if it is stale.

        With ``wait`` false this never blocks: the previous answer comes
        back, or ``unknown`` if there has not been one yet, and the run
        happens in the background.
        """
        # Three passes at most.  A second is needed when the run that
        # was already in flight turns out to have been for an older
        # stamp -- the tree moved while it ran -- and a third would mean
        # it is moving faster than it can be read, which is not
        # something to keep a request waiting on.
        for _ in range(3):
            with self._lock:
                held = self._answers.get(key)
                fresh = (held is not None and held[0] == stamp
                         and time.time() - held[1] < self._ttl)
                if fresh and not force:
                    return held[2]
                done = self._busy.get(key)
                mine = done is None
                if mine:
                    done = self._busy[key] = threading.Event()

            if mine:
                threading.Thread(target=self._work,
                                 args=(key, stamp, work, done),
                                 daemon=True).start()

            if not wait:
                return held[2] if held is not None else unknown

            done.wait()
            force = False

        with self._lock:
            held = self._answers.get(key)
        return held[2] if held is not None else unknown

    def _work(self, key, stamp, work, done):
        try:
            answer = work()
        except Exception:                      # noqa: BLE001
            # The callers catch what their own script can do to them;
            # anything left is a bug here, and swallowing it silently
            # would leave every waiter on this key blocked for good.
            answer = None
        finally:
            with self._lock:
                if answer is not None:
                    self._answers[key] = (stamp, time.time(), answer)
                self._busy.pop(key, None)
            done.set()

    def forget(self, key=None):
        """Drop what is held, after something that could change it."""
        with self._lock:
            if key is None:
                self._answers.clear()
            else:
                self._answers.pop(key, None)
