"""`python -m ier.api` runs the API on 127.0.0.1:8000 (loopback only; see ier.api.main)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("ier.api.main:app", host=os.environ.get("IER_API_HOST", "127.0.0.1"),
                port=int(os.environ.get("IER_API_PORT", "8000")))
