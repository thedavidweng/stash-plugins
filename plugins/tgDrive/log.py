"""
Stash plugin logging module.
Transmits log messages via stderr with special character encoding for the Stash UI.
"""
import sys


def __prefix(level_char: bytes) -> str:
    start_level_char = b'\x01'
    end_level_char = b'\x02'
    return (start_level_char + level_char + end_level_char).decode()


def __log(level_char: bytes, s: str) -> None:
    if not level_char:
        return
    print(__prefix(level_char) + str(s) + "\n", file=sys.stderr, flush=True)


def LogTrace(s: str) -> None:
    __log(b't', s)


def LogDebug(s: str) -> None:
    __log(b'd', s)


def LogInfo(s: str) -> None:
    __log(b'i', s)


def LogWarning(s: str) -> None:
    __log(b'w', s)


def LogError(s: str) -> None:
    __log(b'e', s)


def LogProgress(p: float) -> None:
    """Log progress (0.0 to 1.0) to update the Stash progress bar."""
    progress = min(max(0.0, float(p)), 1.0)
    __log(b'p', f"{progress:.4f}")
