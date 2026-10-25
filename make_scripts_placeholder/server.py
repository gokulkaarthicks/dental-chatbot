import os

import uvicorn

if __name__ == "__main__":
    # Container/module entry point. Startup creates missing tables and seeds an
    # empty database; restarts reuse the existing file in the Docker volume.
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
        proxy_headers=True,
    )
