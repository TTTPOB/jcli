"""Package version helpers."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("jupyter-jcli")
except PackageNotFoundError:
    __version__ = "0+unknown"
