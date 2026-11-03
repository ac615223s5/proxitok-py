"""ProxiTok - privacy-friendly alternative frontend for TikTok (Python/FastAPI rewrite)."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("proxitok")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "3.0.0"

__all__ = ["__version__"]
