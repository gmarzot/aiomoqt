from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("aiomoqt")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

del _pkg_version, PackageNotFoundError
