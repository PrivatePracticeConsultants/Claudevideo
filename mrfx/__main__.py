"""`python -m mrfx …` entry point — mirrors the `mrfx` console script.
Used by the auto-restart supervisor (mrfx serve --supervise) to relaunch a
clean child process."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
