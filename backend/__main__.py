"""
RecoveryOS entry point.

Run with: python -m backend
"""

import uvicorn
from backend.config import config


def main():
    uvicorn.run(
        "backend.api.server:app",
        host=config.host,
        port=config.port,
        reload=config.is_development,
    )


if __name__ == "__main__":
    main()
