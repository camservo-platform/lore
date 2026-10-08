"""Run the web table: `python -m lore.web`. Set LORE_RELOAD=1 to reload on code changes."""

import functools
import os
from pathlib import Path

import uvicorn

from lore.web.app import create_app
from lore.web.drain import Drain, Server

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    if os.environ.get("LORE_RELOAD") == "1":
        uvicorn.run(
            "lore.web.app:create_app", factory=True, host=host, port=port, proxy_headers=True,
            reload=True, reload_dirs=[str(Path(__file__).parents[1])],
        )
    else:
        drain = Drain(timeout=float(os.environ.get("LORE_DRAIN_SECONDS", "240")))
        config = uvicorn.Config(
            functools.partial(create_app, drain), factory=True, host=host, port=port, proxy_headers=True,
            # Backstop only: the drain has already let work finish before uvicorn shuts down.
            timeout_graceful_shutdown=5,
        )
        Server(config, drain).run()
