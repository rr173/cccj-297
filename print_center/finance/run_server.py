"""财务服务独立启动入口：python -m print_center.finance.run_server"""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("FINANCE_PORT", "8078"))
    uvicorn.run("print_center.finance.app:app", host="127.0.0.1",
                port=port, log_level="warning")


if __name__ == "__main__":
    main()
