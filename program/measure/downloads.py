"""Downloading model files and installing packages, with progress, retries, resume and Cancel.

Used by the Model setup window and the command line. Nothing here runs on its own: a download or an
install starts only when a person asks for it. Both can be cancelled with a threading.Event.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from config import ADDONS_DIR, FROZEN, HEADERS, activate_addons

CONNECT_TIMEOUT_S, READ_TIMEOUT_S = 15, 30
REPORT_EVERY = 0.1                  # log progress every 10% of a file
PIP_LOG_STARTS = ("Collecting", "Downloading", "Installing collected", "Successfully", "ERROR")


class Cancelled(Exception):
    """The person pressed Cancel."""


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _wait(seconds, cancel):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _check(cancel)
        time.sleep(0.1)


def _size(n):
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB"


def fetch(url, dest, log=print, cancel=None, retries=4, chunk=1 << 18, backoff=4.0):
    """Download `url` to `dest` and return it. Resumes a partial download and retries on errors.

    The file is written to <dest>.part and renamed when it is complete, so an interrupted download
    never leaves something that looks finished. A server that answers 5xx (GitHub does this now and
    then) is retried with a growing pause.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    name, error = dest.name, ""
    for attempt in range(1, retries + 1):
        _check(cancel)
        have = part.stat().st_size if part.exists() else 0
        headers = {**HEADERS, **({"Range": f"bytes={have}-"} if have else {})}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                              allow_redirects=True) as response:
                if response.status_code == 416:                 # the partial file is not usable: start again
                    part.unlink(missing_ok=True)
                    raise OSError("the partial download was out of range")
                if response.status_code not in (200, 206):
                    raise OSError(f"the server answered HTTP {response.status_code}")
                if response.status_code == 200 and have:        # the server ignored the resume request
                    part.unlink(missing_ok=True)
                    have = 0
                total = have + int(response.headers.get("content-length") or 0)
                done, reported = have, have
                if attempt == 1 or have == 0:
                    log(f"Downloading {name}" + (f" ({_size(total)})" if total else "") + "...")
                with open(part, "ab" if have else "wb") as handle:
                    for block in response.iter_content(chunk):
                        _check(cancel)
                        handle.write(block)
                        done += len(block)
                        if total and done - reported >= REPORT_EVERY * total:
                            reported = done
                            log(f"    {name}: {100 * done / total:.0f}%")
            if total and part.stat().st_size < total:
                raise OSError("the connection closed early")
            part.replace(dest)
            return dest
        except Cancelled:
            raise
        except (requests.RequestException, OSError) as exc:
            error = str(exc)
            more = attempt < retries
            log(f"    {name}: {error}; " + (f"trying again in {backoff * attempt:.0f} s" if more else "giving up"))
            if more:
                _wait(backoff * attempt, cancel)
    raise RuntimeError(f"Could not download {name}: {error}")


def pip_command(args, python=None):
    """The command that runs `pip install <args>`.

    From source that is the Python the app runs from. The packaged app has no Python, so it runs itself with
    --pip (pip is bundled and run in that second process, which keeps Cancel working) and installs into
    ADDONS_DIR, only from wheels, because there is no compiler to build anything from source.
    """
    common = ["--progress-bar", "off", "--disable-pip-version-check", "--no-input"]
    if FROZEN:
        return [sys.executable, "--pip", "install", *common, "--only-binary=:all:", "--target", str(ADDONS_DIR), *args]
    return [python or sys.executable, "-m", "pip", "install", *common, *args]


def verify_imports(modules, log=print):
    """After an install in the packaged app: really import the packages, in a second process, so a package that
    installed but cannot load (a missing library, a blocked binary) is reported now and not as a green tick.
    From source there is nothing to check beyond what pip did."""
    if not FROZEN:
        return
    result = subprocess.run([sys.executable, "--import-check", *modules], capture_output=True, text=True,
                            timeout=300)
    if result.returncode:
        raise RuntimeError("installed, but it cannot be loaded:\n" + "\n".join(
            (result.stderr or result.stdout).strip().splitlines()[-6:]))
    log("Checked: " + ", ".join(modules) + " load correctly.")


def run_pip(args, log=print, cancel=None, python=None):
    """pip install <args> for the environment the app runs from, streaming the main lines to `log`."""
    command = pip_command(args, python)
    log("Installing " + " ".join(a for a in args if not a.startswith("-")) + "...")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    stop = threading.Event()

    def watch():
        while not stop.is_set() and process.poll() is None:
            if cancel is not None and cancel.is_set():
                process.terminate()
                return
            time.sleep(0.25)

    threading.Thread(target=watch, daemon=True).start()
    tail = []
    try:
        for line in process.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-12:]
            if line.startswith(PIP_LOG_STARTS):
                log("    " + line[:170])
        process.wait()
    finally:
        stop.set()
        if process.poll() is None:
            process.terminate()
    _check(cancel)
    if process.returncode:
        raise RuntimeError("pip failed:\n" + "\n".join(tail[-6:]))
    activate_addons()
