"""Starting the daemon and telling it to re-evaluate. Shared by every entry point."""

import errno
import fcntl
import os
import subprocess
import sys

from . import paths


def lock_path():
    return paths.in_state("daemon.lock")


def fifo_path():
    return paths.in_state("poke.fifo")


def acquire_lock():
    """Hold the singleton lock for the process lifetime, or return None if taken."""
    os.makedirs(paths.state_dir(), exist_ok=True)
    handle = open(lock_path(), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def daemon_running():
    probe = acquire_lock()
    if probe is None:
        return True
    probe.close()
    return False


def ensure_daemon():
    """Start the daemon unless one runs. Returns immediately.

    The lock probe only avoids a pointless spawn: the child takes the lock for
    real and exits quietly when it loses a race, so two daemons cannot coexist.
    """
    if daemon_running():
        return False
    script = os.path.join(paths.plugin_root(), "caffeinate.py")
    subprocess.Popen(
        [sys.executable, script, "--daemon"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=paths.plugin_root(),
    )
    return True


def poke():
    """Ask a running daemon to re-evaluate now. False when none is listening."""
    try:
        fd = os.open(fifo_path(), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    try:
        os.write(fd, b"x")
    except OSError:
        return False
    finally:
        os.close(fd)
    return True
