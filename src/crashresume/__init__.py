"""crashresume - prove an agent survives crash/restart without losing state or replaying effects."""

from .core import (
    ConformanceReport,
    Context,
    Effect,
    EffectLog,
    FencedOut,
    Finding,
    InjectedCrash,
    InjectedRefusal,
    InjectedTimeout,
    KeyView,
    Repro,
    conform,
    explain,
    replay,
)
from .fs import RealFS, SimFS
from .process import SetupError, conform_process

__version__ = "0.3.0"
__author__ = "Calas Automations Inc."
__all__ = [
    "conform",
    "conform_process",
    "replay",
    "explain",
    "KeyView",
    "Context",
    "Effect",
    "EffectLog",
    "Finding",
    "InjectedCrash",
    "InjectedTimeout",
    "InjectedRefusal",
    "FencedOut",
    "ConformanceReport",
    "Repro",
    "SimFS",
    "RealFS",
    "SetupError",
    "__version__",
    "__author__",
]
