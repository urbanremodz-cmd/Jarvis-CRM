"""Double-click to open Remod Flow CRM. Starts the app in the background if needed, then opens the browser."""
import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"


def running():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


if not running():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = open(os.path.join(HERE, "remodflow.log"), "a")
    subprocess.Popen([exe, os.path.join(HERE, "app.py")], cwd=HERE, creationflags=flags,
                     stdout=log, stderr=log, close_fds=True)
    for _ in range(50):
        if running():
            break
        time.sleep(0.2)

webbrowser.open(URL)
