import os
import shutil
import subprocess
import sys
import time

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class KeepAwake:
    def __init__(self):
        self.active = False
        self._process = None

    @staticmethod
    def supported():
        if sys.platform.startswith("win"):
            return True
        if sys.platform == "darwin":
            return shutil.which("caffeinate") is not None
        return shutil.which("systemd-inhibit") is not None

    def enable(self, keep_display_on=False):
        if self.active:
            self.disable()
        if sys.platform.startswith("win"):
            import ctypes
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if keep_display_on else 0)
            if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
                raise OSError("Windows refused the keep-awake request")
        elif sys.platform == "darwin":
            args = ["caffeinate", "-i", "-w", str(os.getpid())]
            if keep_display_on:
                args.insert(1, "-d")
            self._process = subprocess.Popen(args)
        elif shutil.which("systemd-inhibit"):
            what = "idle:sleep" if keep_display_on else "sleep"
            self._process = subprocess.Popen(
                ["systemd-inhibit", f"--what={what}", "--who=GBIF Herbaria Data Pipeline",
                 "--why=Long-running download or measurement", "--mode=block", "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise OSError("Keeping the computer awake is not supported on this system")
        if self._process:
            time.sleep(0.3)
            if self._process.poll() is not None:
                self._process = None
                raise OSError("the system did not accept the request (no session power manager was found)")
        self.active = True

    def disable(self):
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self.active = False
