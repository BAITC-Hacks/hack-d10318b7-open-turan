"""Launch the app and, when bundled, its local Ollama server."""
from __future__ import annotations
import importlib.metadata
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import json
import webbrowser
from pathlib import Path
from threading import Thread

BASE = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def dependencies_ready():
    try:
        for line in (BASE / "requirements.txt").read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            name, version = line.strip().split("==")
            if importlib.metadata.version(name.split("[")[0]) != version:
                return False
        return True
    except (ValueError, importlib.metadata.PackageNotFoundError):
        return False


def get_json(url):
    with OPENER.open(url, timeout=2) as response:
        return json.load(response)


def start_ollama():
    from utils import validate_local_ollama_url
    url = validate_local_ollama_url(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
    try:
        get_json(url + "/api/tags")
        return None
    except (OSError, ValueError):
        pass
    executable = BASE / "tools" / "ollama" / "ollama.exe"
    if not executable.exists():
        found = shutil.which("ollama")
        if not found:
            print("Ollama is not installed. Text review is available; run setup_models.bat for automatic analysis.")
            return None
        executable = Path(found)
    (BASE / "logs").mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["OLLAMA_HOST"] = url
    environment["OLLAMA_MODELS"] = str(BASE / "models" / "ollama")
    environment["OLLAMA_NO_CLOUD"] = "1"
    log = (BASE / "logs" / "ollama.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen([str(executable), "serve"], cwd=BASE, env=environment,
                                   stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    finally:
        log.close()
    for _ in range(60):
        if process.poll() is not None:
            print("Ollama did not start. See logs/ollama.log. Manual text review is still available.")
            return None
        try:
            get_json(url + "/api/tags")
            return process
        except (OSError, ValueError):
            time.sleep(0.25)
    process.terminate()
    process.wait(timeout=10)
    print("Ollama startup timed out. Manual text review is still available.")
    return None


def open_when_ready():
    for _ in range(120):
        try:
            get_json(URL + "/openapi.json")
            webbrowser.open(URL)
            return
        except (OSError, ValueError):
            time.sleep(0.25)


def main():
    if "--check-deps" in sys.argv:
        return 0 if dependencies_ready() else 1
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    os.chdir(BASE)
    try:
        existing = get_json(URL + "/openapi.json")
    except (OSError, ValueError):
        existing = None
    if existing and existing.get("info", {}).get("title") == "Meeting Notes":
        print("Meeting Notes is already running: " + URL)
        if "--no-browser" not in sys.argv:
            webbrowser.open(URL)
        return 0
    ollama_process = start_ollama()
    if "--no-browser" not in sys.argv:
        Thread(target=open_when_ready, daemon=True).start()
    try:
        import uvicorn
        uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="info")
    finally:
        if ollama_process is not None and ollama_process.poll() is None:
            ollama_process.terminate()
            ollama_process.wait(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

