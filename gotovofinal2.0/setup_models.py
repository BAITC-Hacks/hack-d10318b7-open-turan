"""Download model weights locally. Meeting data is never uploaded."""
import hashlib
import json
import os
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from launcher import BASE, OPENER, start_ollama

load_dotenv(BASE / ".env")
os.environ.setdefault("HF_HOME", str(BASE / "models" / "hf-cache"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
RELEASE = "v0.34.3"


def install_runtime():
    if (BASE / "tools" / "ollama" / "ollama.exe").exists():
        return
    print("Downloading official Ollama CPU runtime (archive about 1.5 GB)...", flush=True)
    with urllib.request.urlopen("https://api.github.com/repos/ollama/ollama/releases/tags/" + RELEASE, timeout=30) as response:
        release = json.load(response)
    asset = next(item for item in release["assets"] if item["name"] == "ollama-windows-amd64.zip")
    destination = BASE / "tools" / "ollama"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "runtime.zip.part"
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(4 * 1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        expected = asset.get("digest", "")
        if expected.startswith("sha256:") and digest.hexdigest() != expected.split(":", 1)[1]:
            raise RuntimeError("Ollama archive checksum mismatch. Please retry.")
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if any(part in name for part in ("cuda_v12/", "cuda_v13/", "vulkan/")):
                    continue
                target = (destination / name).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise RuntimeError("Unsafe archive path")
                bundle.extract(name, destination)
    finally:
        archive.unlink(missing_ok=True)


def main():
    from utils import OLLAMA_MODEL, OLLAMA_URL, WHISPER_CACHE_DIR, WHISPER_MODEL_SIZE, validate_local_ollama_url
    install_runtime()
    process = start_ollama()
    try:
        model = OLLAMA_MODEL
        print("Downloading local text model: " + model, flush=True)
        request = urllib.request.Request(validate_local_ollama_url(OLLAMA_URL) + "/api/pull",
                    data=json.dumps({"model": model, "stream": True}).encode(),
                    headers={"Content-Type": "application/json"})
        last = ""
        with OPENER.open(request, timeout=1800) as response:
            for line in response:
                item = json.loads(line)
                if item.get("error"):
                    raise RuntimeError(item["error"])
                status = item.get("status", "")
                if item.get("total"):
                    status += " " + str(int(100 * item.get("completed", 0) / item["total"])) + "%"
                if status != last:
                    print(status, flush=True)
                    last = status
        print("Downloading Whisper " + WHISPER_MODEL_SIZE + "...", flush=True)
        import httpx
        from huggingface_hub import set_client_factory
        from faster_whisper.utils import download_model
        set_client_factory(lambda: httpx.Client(verify=ssl.create_default_context(), timeout=60))
        download_model(WHISPER_MODEL_SIZE, cache_dir=str(WHISPER_CACHE_DIR))
        print("Both local models are ready. Start the app with start.bat.", flush=True)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Setup failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)

