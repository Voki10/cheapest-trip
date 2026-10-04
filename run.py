"""Start the Cheapest Trip web app: python run.py  →  http://127.0.0.1:8770"""

import logging
import threading
import webbrowser

import uvicorn

from cheaptrip.config import settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://127.0.0.1:{settings.port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Cheapest Trip → {url}")
    uvicorn.run("cheaptrip.api:app", host="127.0.0.1", port=settings.port, log_level="warning")
