"""Ensures a local Ollama server has flash attention + KV-cache quantization
before CrimsonForge sends it translation requests.

Both settings are process-env flags that only take effect when `ollama serve`
itself starts - they cannot be pushed to an already-running server through
the API, and there is no API to ask a live server what env it started with.
So: if a server we ourselves started this session is already up, it's known
quantized and left alone. Any other already-running instance (the tray app,
a leftover process, another tool) has to be treated as unquantized - we kill
it and start our own with the right env instead of trusting it.
"""

import os
import subprocess
import time

import psutil
import requests

from utils.logger import get_logger

logger = get_logger("ai.ollama_manager")

OLLAMA_ENV = {
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q4_0",
    # Without this, Ollama computes its own "VRAM-based default context"
    # (4096 on a 16GB card - confirmed in server startup logs) for any
    # request that doesn't carry an explicit per-request num_ctx override.
    # Per-request num_ctx (ai/provider_ollama.py) DOES work when it's sent,
    # but setting the server-level floor too means a malformed/bare request
    # can never silently reload the runner down to 4096 mid-session.
    # "OLLAMA_CONTEXT_LENGTH": "65536",
    # "OLLAMA_CONTEXT_LENGTH": "131072",
    "OLLAMA_CONTEXT_LENGTH": "32768",
}

# "ollama.exe" is just the manager process - the actual model runner is a
# separate "llama-server.exe" child. Killing only the parent orphans the
# runner, which keeps holding the model (and its VRAM) forever. Must kill
# both or restarts silently accumulate zombie models until VRAM is exhausted.
OLLAMA_PROCESS_NAMES = {
    "ollama.exe", "ollama app.exe", "ollama", "ollama app",
    "llama-server.exe", "llama-server", "ollama_llama_server.exe", "ollama_llama_server",
}

# PID of the server we started this session, if any. Lets later calls
# recognise "this is already our quantized instance" instead of killing and
# reloading the model on every single translation batch.
_managed_pid: "int | None" = None


def _is_up(host: str) -> bool:
    try:
        requests.get(f"{host}/api/tags", timeout=3)
        return True
    except Exception:
        return False


def host_from_base_url(base_url: str) -> str:
    """Strip the trailing /v1 OpenAI-compat suffix to get the bare Ollama host."""
    base_url = (base_url or "http://localhost:11434").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]
    return base_url


def _kill_ollama_processes(timeout: float = 5.0) -> None:
    """Kill every running Ollama process (tray app + server)."""
    procs = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name in OLLAMA_PROCESS_NAMES:
            try:
                proc.kill()
                procs.append(proc)
            except Exception as exc:
                logger.warning("Could not kill Ollama process %s: %s", proc.info.get("pid"), exc)
    if procs:
        logger.info("Killed existing Ollama process(es): %s", [p.pid for p in procs])
        psutil.wait_procs(procs, timeout=timeout)


def ensure_quantized_server(base_url: str, retries: int = 20, delay: float = 1.5) -> bool:
    """Make sure a quantized Ollama server is reachable at `base_url`.

    Returns True once a quantized server answers, False if Ollama could not
    be started at all.
    """
    global _managed_pid
    host = host_from_base_url(base_url)

    if _managed_pid is not None and psutil.pid_exists(_managed_pid) and _is_up(host):
        return True

    if _is_up(host):
        logger.info(
            "Ollama is running but wasn't started by CrimsonForge this session - "
            "restarting it quantized (flash attention + q4_0 KV-cache) since its "
            "current env can't be verified."
        )
        _kill_ollama_processes()
        for _ in range(retries):
            if not _is_up(host):
                break
            time.sleep(delay)

    logger.info("Starting Ollama with flash attention + q4_0 KV-cache.")
    env = {**os.environ, **OLLAMA_ENV}
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        logger.error("Could not start Ollama - 'ollama' is not on PATH.")
        return False

    for _ in range(retries):
        time.sleep(delay)
        if _is_up(host):
            _managed_pid = proc.pid
            logger.info("Ollama ready (flash attention + q4_0 KV-cache).")
            return True

    logger.error("Ollama did not respond after starting - check that ollama.exe works.")
    return False
