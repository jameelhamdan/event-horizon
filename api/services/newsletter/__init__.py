"""Newsletter service facade.

This module exposes the high-level newsletter operations used by the rest of
the application. Implementation details live in submodules.
"""

from .generator import generate_newsletter
from .sender import send_newsletter

__all__ = ["generate_newsletter", "send_newsletter"]

