import sys

from rich.console import Console

# a legacy Windows console (cp1252) can't encode titles like "Clash ②": replace instead of crashing the run
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
            _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console(highlight=False)
