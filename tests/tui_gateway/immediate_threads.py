"""Synchronous gateway workers without replacing Python's shared Thread class."""

import threading
import types

from agent import memory_provider


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, *, daemon=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = {} if kwargs is None else kwargs
        self._started = False
        self._alive = False
        self.daemon = daemon
        self.name = name

    def start(self):
        if self._started:
            raise RuntimeError("threads can only be started once")
        self._started = True
        self._alive = True
        try:
            if self._target is not None:
                self._target(*self._args, **self._kwargs)
        finally:
            self._alive = False

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        if not self._started:
            raise RuntimeError("cannot join thread before it is started")


def install_immediate_threads(monkeypatch, server, *, thread_factory=ImmediateThread):
    # The context-thread factory still captures ContextVars; subprocess reader
    # threads and other modules keep the real stdlib threading namespace.
    namespace = types.SimpleNamespace(**{**vars(threading), "Thread": thread_factory})
    monkeypatch.setattr(server, "threading", namespace)
    monkeypatch.setattr(memory_provider, "threading", namespace)
