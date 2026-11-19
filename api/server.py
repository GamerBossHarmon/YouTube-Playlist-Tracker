"""uvicorn launcher. Blocks until the server is stopped (Ctrl+C / SIGTERM)."""

from __future__ import annotations

import logging
import sys

import uvicorn
from fastapi import FastAPI

from core.config import Config

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def run_server(app: FastAPI, config: Config) -> None:
    host, port = config.server.host, config.server.port
    if host not in _LOOPBACK_HOSTS:
        # No built-in auth.
        message = (
            f"WARNING: API bound to {host} - anyone who can reach this port can add/remove sources "
            "and read every archived file. There is no authentication; put it behind Tailscale or "
            "a reverse proxy."
        )
        logger.warning(message)
        print(message, file=sys.stderr, flush=True)  # logging isn't configured before uvicorn starts
    # SSE streams never end on their own. They poll app.state.should_stop (wired to uvicorn's exit
    # flags below) and finish within ~1s of Ctrl+C; timeout_graceful_shutdown is only the backstop
    # for a client that is slow to go away.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="debug" if config.verbose else "info",
            timeout_graceful_shutdown=3,
        )
    )
    app.state.should_stop = lambda: server.should_exit or server.force_exit
    server.run()
