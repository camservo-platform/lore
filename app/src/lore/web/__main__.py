"""Run the web table: `python -m lore.web`. Set LORE_RELOAD=1 to reload on code changes."""

import logging
import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    reload = os.environ.get("LORE_RELOAD") == "1"
    uvicorn.run(
        "lore.web.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=reload,
        reload_dirs=[str(Path(__file__).parents[1])] if reload else None,
        proxy_headers=True,
    )
