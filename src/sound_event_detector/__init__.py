"""Detect short, predefined sound events in live system audio or in WAV recordings."""

from importlib.metadata import PackageNotFoundError, version

from .audio import load_wav, save_wav
from .capture import (
    AppSource,
    CaptureError,
    DeviceSource,
    FileSource,
    Source,
    list_apps,
    list_devices,
    open_source,
    record,
)
from .detector import SoundDetector, Stats
from .features import DEFAULT_PARAMS, FeatureParams
from .library import Library
from .matching import Detection, Matcher, ScanResult, SoundEvent, Template, scan, suggest_threshold

try:
    __version__ = version("sound-event-detector")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_PARAMS",
    "AppSource",
    "CaptureError",
    "Detection",
    "DeviceSource",
    "FeatureParams",
    "FileSource",
    "Library",
    "Matcher",
    "ScanResult",
    "SoundDetector",
    "SoundEvent",
    "Source",
    "Stats",
    "Template",
    "__version__",
    "list_apps",
    "list_devices",
    "load_wav",
    "open_source",
    "record",
    "save_wav",
    "scan",
    "suggest_threshold",
]
