"""A real in-process server (same event loop as the test) for streaming and browser tests."""
import asyncio

import uvicorn

import osutil
from api import create_app


class LiveServer:
    def __init__(self, db, hub, token, supervisor=None, port=None):
        self.db, self.hub, self.token, self.supervisor = db, hub, token, supervisor
        self.port = port or osutil.free_port()
        self.server = self.task = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        import main

        cls = main._make_server_class()
        cfg = uvicorn.Config(create_app(self.db, self.hub, self.token, self.supervisor), host="127.0.0.1", port=self.port, log_level="warning",
                          timeout_graceful_shutdown=2)
        self.server = cls(cfg)
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                return self
            await asyncio.sleep(0.05)
        raise RuntimeError("server did not start")

    async def stop(self):
        if self.server:
            self.server.should_exit = True
            await asyncio.wait_for(self.task, 10)
            self.server = None
