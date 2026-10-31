"""crashresume - prove an agent survives crash/restart without losing state or replaying effects."""

from .core import (
    ConformanceReport,
    Context,
    Effect,
    EffectLog,
    Finding,
    InjectedCrash,
    conform,
)

__version__ = "0.1.1"
__all__ = [
    "conform",
    "Context",
    "Effect",
    "EffectLog",
    "Finding",
    "InjectedCrash",
    "ConformanceReport",
    "__version__",
]
