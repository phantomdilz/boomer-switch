"""Bound private foreground commands and accept only one exact reply."""

import os
from pathlib import Path
import select
import subprocess
import time


def private_command(command):
    if (not isinstance(command, list) or not 1 <= len(command) <= 16
            or any(not isinstance(item, str) or not item or len(item) > 4096 or "\0" in item for item in command)
            or not Path(command[0]).is_absolute()):
        raise ValueError("Configure an absolute private foreground command.")
    return tuple(command)


def exact_private_reply(command, data, expected, *, timeout=2):
    if not isinstance(data, bytes) or len(data)>4096:
        return False
    limit=len(expected)+1
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, bufsize=0)
        try:
            process.stdin.write(data)
            process.stdin.close()
            deadline, output = time.monotonic()+timeout, b''
            while len(output) < limit:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    return False
                if os.name == 'nt':
                    import ctypes
                    import msvcrt
                    available = ctypes.c_ulong()
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    peek = kernel.PeekNamedPipe
                    peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
                    peek.restype = ctypes.c_int
                    if not peek(msvcrt.get_osfhandle(process.stdout.fileno()), None, 0, None, ctypes.byref(available), None):
                        if ctypes.get_last_error() == 109:
                            break
                        return False
                    if not available.value:
                        time.sleep(min(0.01, remaining))
                        continue
                    length = min(available.value, limit-len(output))
                else:
                    if not select.select([process.stdout], [], [], remaining)[0]:
                        return False
                    length = limit-len(output)
                chunk = os.read(process.stdout.fileno(), length)
                if not chunk:
                    break
                output += chunk
            process.wait(timeout=max(0.001, deadline-time.monotonic()))
            return process.returncode == 0 and output == expected
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
    except Exception:
        return False
