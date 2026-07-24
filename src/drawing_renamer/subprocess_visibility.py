from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_window_options(
    *,
    creationflags: int = 0,
    startupinfo: Any | None = None,
) -> dict[str, Any]:
    """Return subprocess options that suppress Windows console windows."""

    if os.name != "nt":
        return {
            "creationflags": creationflags,
            **({"startupinfo": startupinfo} if startupinfo is not None else {}),
        }

    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    detached_process = getattr(subprocess, "DETACHED_PROCESS", 0)
    if not creationflags & (new_console | detached_process):
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if startupinfo is None:
        startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": creationflags,
        "startupinfo": startupinfo,
    }


def install_hidden_subprocess_policy() -> None:
    """Hide subprocesses started by OCR dependencies inside the worker."""

    if os.name != "nt":
        return
    current_popen = subprocess.Popen
    if getattr(current_popen, "_drawing_renamer_hidden_policy", False):
        return

    class HiddenWindowPopen(current_popen):  # type: ignore[misc, valid-type]
        _drawing_renamer_hidden_policy = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            options = hidden_window_options(
                creationflags=int(kwargs.pop("creationflags", 0)),
                startupinfo=kwargs.pop("startupinfo", None),
            )
            super().__init__(*args, **kwargs, **options)

    subprocess.Popen = HiddenWindowPopen  # type: ignore[assignment]
