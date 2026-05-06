"""
PitchBot launcher.
Runs silently (no console window) — starts the Dash server and opens the browser.
Double-click this file, or invoke it via the Desktop shortcut.
"""
import os
import sys
import socket
import time
import subprocess
import webbrowser
import platform

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8050


def _venv_python():
    """Return path to the venv Python, falling back to system Python."""
    is_win = platform.system() == "Windows"
    exe = "python.exe" if is_win else "python"
    scripts = "Scripts" if is_win else "bin"
    candidate = os.path.join(SCRIPT_DIR, "venv", scripts, exe)
    return candidate if os.path.exists(candidate) else sys.executable


def _server_alive():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def _start_server(py_exe):
    os.makedirs(os.path.join(SCRIPT_DIR, "logs"), exist_ok=True)
    log = open(os.path.join(SCRIPT_DIR, "logs", "pitchbot.log"), "a")
    flags = 0
    if platform.system() == "Windows":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.Popen(
        [py_exe, os.path.join(SCRIPT_DIR, "app.py")],
        stdout=log,
        stderr=log,
        creationflags=flags,
        cwd=SCRIPT_DIR,
    )


def main():
    os.chdir(SCRIPT_DIR)
    py = _venv_python()

    if not _server_alive():
        _start_server(py)
        # Wait up to 20 s for server to become ready
        for _ in range(40):
            if _server_alive():
                break
            time.sleep(0.5)

    webbrowser.open(f"http://localhost:{PORT}")


main()
