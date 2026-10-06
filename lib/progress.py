#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/progress.py
# A progress bar for the builds that say nothing while they work
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
"""Run a command, and show how far along it is while it runs.

    progress.py --watch DIR --output LOG --totals DIR --name NAME \
                -- command args...

The problem this solves is specific.  Anolis's build cases end in
``make -j $job_num -s``; the -s is theirs and stays theirs, so between
their configure step and their verdict a compile prints nothing at all,
and for allyesconfig that is the best part of an hour of silence.  There
is nothing in the output to count.

There is something on disk to count, though: the object files the
compile is creating.  That rises in proportion to the work actually
done, which elapsed time does not -- configuring is quick and linking is
slow -- and their own progress markers in the log say which phase is
producing them.

The denominator is nowhere to be found.  It depends on the config being
built (allyesconfig and allnoconfig differ by four orders of magnitude),
on the architecture, and on the kernel, so a number written down here
would be wrong for the next series within the same release.  Each case
measures itself against what it built the last time it succeeded
instead: the first run of a case shows a live count and an honest lack
of a percentage, and every run after it shows a bar.

The command runs as a child so there is one invocation, no pid to pass
around, and the child's exit status is this program's.  Its output goes
to --output, which keeps the bar out of the file their verdict is read
from; the bar itself goes to stdout, as a line rewritten in place for a
terminal, and as something web/prci/jobs.py can parse when stdout is a
job log instead.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

#: Eighth-blocks, so the bar advances within a character instead of
#: jumping a whole one.  At 28 columns that is 224 distinguishable
#: positions rather than 28, which is the difference between a bar that
#: looks stalled during a long phase and one that visibly creeps.
EIGHTHS = ' \u258f\u258e\u258d\u258c\u258b\u258a\u2589'
FULL = '\u2588'

#: The phase has no percentage of its own, so it gets a spinner.  These
#: are the braille patterns, which turn rather than blink.
SPINNER = '\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f'

#: How often to walk the build tree.  A kernel tree is ~90k files and
#: the walk is the only expensive thing here, so it is done far less
#: often than the bar is redrawn.
COUNT_EVERY = 5.0

#: How often to redraw.  Fast enough that the spinner turns and the
#: clock ticks, which is what says "alive" while a phase produces no
#: objects at all.
DRAW_EVERY = 0.2

#: How long to go without a word when there is no terminal to draw on.
#: Only reached before the first object appears, and on a case's first
#: ever run.
QUIET_FOR = 45.0

#: Below this many objects a percentage would say nothing true.  Their
#: check_Kconfig compiles nothing at all -- it leaves eight objects
#: behind from their config tooling -- so measured against eight it
#: would read 99% within seconds of starting and stay there for the
#: minute that the check actually takes.  Their compiling cases produce
#: tens of thousands, so nothing real is lost by the floor; those cases
#: get the phase and the clock, which is all there is to tell them.
MIN_FOR_PERCENT = 200


def unicode_ok():
    """Whether the terminal can be expected to render the blocks."""
    encoding = (sys.stdout.encoding or '').lower()
    return 'utf' in encoding


def count_objects(where, since=0.0):
    """The object files under a tree that this run is responsible for.

    Counted rather than collected: a kernel build leaves tens of
    thousands and none of the names are wanted.  Symbolic links are not
    followed, because their anck_rpm_build links the kernel into the
    build harness and the tree would otherwise be counted twice.

    ``since`` is what makes the figure this run's rather than the
    tree's.  Anolis's cases re-clone and openEuler's build runs `make
    distclean`, but neither has done so in the first half-minute, and
    counting what was already there opened the bar at 99% and then
    dropped it to nothing.  An object older than the run did not come
    from it.
    """
    if not where or not os.path.isdir(where):
        return 0

    found = 0
    stack = [where]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name.endswith('.o'):
                            if since and entry.stat(
                                    follow_symlinks=False).st_mtime < since:
                                continue
                            found += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return found


class Phase(object):
    """What their script says it is doing, in their words.

    Their anck_build.sh announces each step -- "===> Clone kernel
    repository...", "===> Install related packages...", "== Build Kernel
    with allyesconfig ==" -- so the label follows their script rather
    than being a list here that goes stale when they add a step.

    The log only grows, so it is read from where the last read stopped.
    """

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.text = ''

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self.text
        if size < self.offset:        # truncated, start again
            self.offset = 0
        if size == self.offset:
            return self.text

        try:
            with open(self.path, 'rb') as handle:
                handle.seek(self.offset)
                fresh = handle.read()
                self.offset = handle.tell()
        except OSError:
            return self.text

        for line in fresh.decode('utf-8', 'replace').splitlines():
            line = line.strip()
            if line.startswith('===>'):
                self.text = line[4:].strip().rstrip('.')
            elif line.startswith('==') and line.endswith('=='):
                self.text = line.strip('=').strip()
            elif line.startswith('->'):
                # The VM cases have no objects here to count, so their
                # steps -- copying the suite over, running it there --
                # are the only thing there is to report.
                self.text = line[2:].strip()
            elif '*****' in line:
                # openEuler's, whose log_info comes from
                # openeuler-jenkins and wraps each step in stars:
                # "[ INFO ] ***** Start to download kernel of openeuler
                # *****".
                starred = line.split('*****')
                if len(starred) >= 3 and starred[1].strip():
                    self.text = starred[1].strip()
        return self.text


class Rate(object):
    """Objects per second over a trailing window, for the estimate.

    A whole-run average would be dragged down by the configure step at
    the start and would then under-report for the rest of the build, so
    only the recent past is used.
    """

    WINDOW = 90.0

    #: And no estimate at all until there is this much to base one on.
    #: The first samples land while the configure step is still running
    #: and the first handful of files are being compiled, where the rate
    #: bears no relation to the rate of the build that follows -- an
    #: estimate from them read half an hour for something that finished
    #: in twenty seconds.
    SETTLE = 20.0

    def __init__(self):
        self.seen = []

    def add(self, when, count):
        self.seen.append((when, count))
        while len(self.seen) > 2 and when - self.seen[0][0] > self.WINDOW:
            self.seen.pop(0)

    def per_second(self):
        # Only from the point objects started appearing.  Their clone
        # takes half a minute and their configure step a while longer,
        # and counting that as compiling time halves the rate -- which
        # is how a build with a minute to go claimed twenty-five.
        useful = [sample for sample in self.seen if sample[1] > 0]
        if len(useful) < 2:
            return 0.0
        (t0, c0), (t1, c1) = useful[0], useful[-1]
        if t1 - t0 < self.SETTLE or c1 <= c0:
            return 0.0
        return (c1 - c0) / (t1 - t0)


def clock(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return '%dh%02dm' % (seconds // 3600, seconds % 3600 // 60)
    return '%dm%02ds' % (seconds // 60, seconds % 60)


def bar(fraction, width, fancy):
    """A bar with eighth-of-a-character resolution."""
    if not fancy:
        filled = int(fraction * width)
        return '=' * filled + ' ' * (width - filled)

    exact = fraction * width
    whole = int(exact)
    part = int((exact - whole) * 8)
    out = FULL * whole
    if whole < width:
        out += EIGHTHS[part]
    return out.ljust(width, ' ')


class Display(object):
    """The bar, drawn where it makes sense to draw it.

    A terminal gets a line rewritten in place.  A job log gets one
    machine-readable line each time the percentage changes, because
    thousands of carriage returns in a file are no use to anybody and
    neither is a line every fifth of a second.
    """

    WIDTH = 28

    def __init__(self, stream, name):
        self.out = stream
        self.name = name
        self.tty = stream.isatty()
        self.fancy = self.tty and unicode_ok()
        self.last_pct = None
        self.last_phase = None
        self.last_said = 0.0
        self.frame = 0
        self.drawn = False

    def paint(self, pct, done, total, elapsed, phase, eta):
        if self.tty:
            self._paint_tty(pct, done, total, elapsed, phase, eta)
        else:
            self._paint_log(pct, done, total, elapsed, phase)

    def _paint_tty(self, pct, done, total, elapsed, phase, eta):
        self.frame += 1
        parts = []

        if pct is None:
            spin = SPINNER[self.frame % len(SPINNER)] if self.fancy else '*'
            parts.append('  %s' % spin)
            if done:
                parts.append('%s objects' % format(done, ','))
        else:
            parts.append('  \033[36m%s\033[0m' % bar(pct / 100.0,
                                                     self.WIDTH, self.fancy))
            parts.append('\033[1m%3d%%\033[0m' % pct)
            parts.append('%s/%s' % (format(done, ','), format(total, ',')))

        parts.append(clock(elapsed))
        if eta:
            parts.append('eta %s' % clock(eta))
        if phase:
            parts.append('\033[2m%s\033[0m' % phase[:46])

        self.out.write('\r' + '  '.join(parts) + '\033[K')
        self.out.flush()
        self.drawn = True

    def _paint_log(self, pct, done, total, elapsed, phase):
        # On a change, and otherwise rarely.  A line every fifth of a
        # second would be a hundred thousand of them in an hour's build
        # log, and a line only when the percentage moves would sit on
        # "Clone kernel repository" through the whole of the compile
        # that follows it.
        now = time.time()
        if (pct == self.last_pct and phase == self.last_phase
                and now - self.last_said < QUIET_FOR):
            return
        self.last_pct = pct
        self.last_phase = phase
        self.last_said = now
        self.out.write(
            '[prci-progress] pct=%s done=%d total=%d elapsed=%d phase=%s\n'
            % ('-' if pct is None else pct, done, total, int(elapsed),
               phase or self.name))
        self.out.flush()

    def clear(self):
        if self.tty and self.drawn:
            self.out.write('\r\033[K')
            self.out.flush()


def watch(running, args, display):
    """Draw for as long as running() says to."""
    phase = Phase(args.phases or args.output)
    rate = Rate()
    total = read_total(args)

    started = args.since or time.time()
    done = 0
    counted_at = 0.0

    while running():
        now = time.time()
        if now - counted_at >= COUNT_EVERY:
            done = count_objects(args.watch, started)
            counted_at = now
            rate.add(now, done)

        pct = None
        eta = 0
        if total >= MIN_FOR_PERCENT and done > 0:
            # Capped below 100: their verdict decides when a case is
            # finished, not a count measured against a remembered total
            # that this run can legitimately overshoot.
            pct = min(99, int(done * 100 / total))
            per_second = rate.per_second()
            if per_second > 0 and total > done:
                eta = (total - done) / per_second

        display.paint(pct, done, total, now - started,
                      phase.poll(), eta)
        time.sleep(DRAW_EVERY)

    display.clear()
    return done, started


def read_total(args):
    """What this case built last time it succeeded."""
    if not args.totals or not args.name:
        return 0
    try:
        with open(os.path.join(args.totals, args.name)) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 0


def write_total(args, count):
    """Remember it, so the next run of this case has a bar.

    Only ever called for a case that succeeded.  A build that stopped
    early left fewer objects behind than a whole one, and writing that
    down would make the next run's bar reach 100% and sit there.
    """
    if not args.totals or not args.name or count <= 0:
        return
    try:
        os.makedirs(args.totals, exist_ok=True)
        with open(os.path.join(args.totals, args.name), 'w') as handle:
            handle.write('%d\n' % count)
    except OSError:
        pass


def run_the_command(args, display, command):
    """Draw in front of a command, and answer with the command's status."""
    try:
        sink = open(args.output, 'w')
    except OSError as problem:
        sys.stderr.write('progress: cannot write %s: %s\n'
                         % (args.output, problem))
        return 1

    with sink:
        child = subprocess.Popen(command, stdout=sink,
                                 stderr=subprocess.STDOUT)

        # Ctrl-C and a stopped job have to reach the build, not just the
        # bar in front of it, or a cancelled run would leave a kernel
        # compile going with nothing watching it.
        def relay(number, _frame):
            try:
                child.send_signal(number)
            except OSError:
                pass

        for number in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(number, relay)
            except (ValueError, OSError):
                pass

        try:
            done, started = watch(lambda: child.poll() is None, args, display)
        except KeyboardInterrupt:
            display.clear()
            child.wait()
            return 130

        status = child.wait()

    if status == 0:
        # Counted once more now it has finished, so the figure that gets
        # remembered is the whole build rather than wherever the last
        # five-second sample happened to land.
        write_total(args, max(done, count_objects(args.watch, started)))
    return status


def just_watch(args, display):
    """Draw until told to stop, with the work going on elsewhere.

    openEuler's build is a shell function their runner sources rather
    than a script it can run, so there is no child to put the bar in
    front of -- it draws alongside instead, and whoever started it says
    when the build finished and whether to remember the count.
    """
    stop = threading.Event()

    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(number, lambda *_: stop.set())
        except (ValueError, OSError):
            pass

    try:
        watch(lambda: not stop.is_set(), args, display)
    except KeyboardInterrupt:
        pass
    display.clear()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Run a command and show how far along it is.')
    parser.add_argument('--watch', default='',
                        help='tree whose object files are counted')
    parser.add_argument('--output', default='/dev/null',
                        help="file the command's own output goes to")
    parser.add_argument('--phases', default='',
                        help='file to read the current phase from, when '
                             'the command writes its progress elsewhere '
                             'than --output (defaults to --output)')
    parser.add_argument('--totals', default='',
                        help='directory of remembered object counts')
    parser.add_argument('--name', default='',
                        help='what to remember this run as')
    parser.add_argument('--watch-only', action='store_true',
                        help='draw alongside work started elsewhere, '
                             'until stopped')
    parser.add_argument('--record', action='store_true',
                        help='remember what is there now and draw '
                             'nothing; for use after --watch-only')
    parser.add_argument('--since', type=float, default=0.0,
                        help='count only objects built after this time, '
                             'as seconds since the epoch (defaults to '
                             'when this starts)')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == '--':
        command = command[1:]

    if args.record:
        write_total(args, count_objects(args.watch, args.since))
        return 0

    if args.watch_only:
        return just_watch(args, Display(sys.stdout, args.name))

    if not command:
        parser.error('nothing to run')

    return run_the_command(args, Display(sys.stdout, args.name or command[0]),
                           command)


if __name__ == '__main__':
    sys.exit(main())
