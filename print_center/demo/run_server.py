"""服务端启动入口：python -m print_center.demo.run_server"""
from __future__ import annotations

import os

import uvicorn

from print_center.server.app import app


def main() -> None:
    port = int(os.environ.get("PRINTCENTER_PORT", "8077"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
