"""inferroute CLI — launch Claude Code through inferroute with one command."""
from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed package version from pyproject.
    __version__ = version("inferroute")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+dev"
