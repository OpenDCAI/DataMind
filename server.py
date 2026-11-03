"""Compatibility launcher for the shared Codex HTTP/UI host (DataMind 2.0)."""
from datamind.server import app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000)
