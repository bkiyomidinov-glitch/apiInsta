#!/usr/bin/env python3
"""InstaPI — entry point. Запуск: python3 server.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

if __name__ == "__main__":
    import uvicorn
    from backend.app import app
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
