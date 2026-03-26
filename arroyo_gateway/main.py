"""Arroyo TEC Gateway — entry point."""

import logging
import uvicorn

from .config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
)


def run() -> None:
    cfg = load_config()
    uvicorn.run(
        "arroyo_gateway.app:app",
        host=cfg.gateway.host,
        port=cfg.gateway.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
