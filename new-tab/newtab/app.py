"""Terminal loop: reads keys, drives the state machine, performs its effects."""
from __future__ import annotations

import os
import select
import termios
import tty

from . import herdr, logic, render
from .keys import KeyParser

ESC_TIMEOUT = 0.05
POLL = 0.25


def _size(fd: int):
    try:
        cols, rows = os.get_terminal_size(fd)
        return rows, cols
    except OSError:
        return 10, 52


def _draw(out_fd: int, state: logic.State, rows: int, cols: int) -> None:
    lines = render.render(state, rows, cols)
    frame = ["\x1b[?25l"]
    for i, line in enumerate(lines[:rows]):
        frame.append(f"\x1b[{i + 1};1H\x1b[2K{line}{render.RESET}")
    os.write(out_fd, "".join(frame).encode())


def _submit(state: logic.State, out_fd: int, tty_fd: int) -> logic.State:
    rows, cols = _size(tty_fd)
    _draw(out_fd, state, rows, cols)
    try:
        fresh = herdr.list_workspaces()
    except herdr.HerdrError as err:
        return logic.on_list_failed(state, str(err))
    state, create = logic.on_listing(state, fresh)
    if create is None:
        return state
    try:
        herdr.create_tab(create.workspace_id, create.label)
    except herdr.HerdrError as err:
        return logic.on_create_failed(state, str(err))
    return logic.on_create_ok(state)


def run(in_fd: int = 0, out_fd: int = 1) -> int:
    saved = termios.tcgetattr(in_fd)
    try:
        tty.setraw(in_fd, termios.TCSANOW)
        return _loop(in_fd, out_fd)
    finally:
        termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)
        os.write(out_fd, b"\x1b[?25h\x1b[2J\x1b[H")


def _initial_state() -> logic.State:
    try:
        return logic.initial_state(herdr.list_workspaces(), herdr.context_workspace_id())
    except herdr.HerdrError as err:
        return logic.State(workspaces=(), dest_id=None, error=f"Could not read workspaces: {err}. Esc to close.")


def _loop(in_fd: int, out_fd: int) -> int:
    parser = KeyParser()
    state = _initial_state()
    last_size = _size(in_fd)
    _draw(out_fd, state, *last_size)
    while True:
        wait = ESC_TIMEOUT if parser.pending else POLL
        ready, _, _ = select.select([in_fd], [], [], wait)
        if ready:
            data = os.read(in_fd, 4096)
            if not data:
                return 0
            keys = parser.feed(data)
        elif parser.pending:
            keys = parser.flush()
        else:
            keys = []
        changed = bool(keys)
        for key in keys:
            state, effect = logic.update(state, key)
            if effect == logic.EFFECT_CANCEL:
                return 0
            if effect == logic.EFFECT_SUBMIT:
                state = _submit(state, out_fd, in_fd)
                if state.phase == logic.CLOSED:
                    return 0
                # Keys typed while creating must not trigger a second attempt.
                termios.tcflush(in_fd, termios.TCIFLUSH)
                parser = KeyParser()
                break
        size = _size(in_fd)
        if changed or size != last_size:
            last_size = size
            _draw(out_fd, state, *last_size)
