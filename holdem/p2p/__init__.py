"""
holdem.p2p -- P2P transport package.

The module-level ``_session`` variable is set by the lobby (onboarding.py)
when the user creates or joins a game, so other parts of the application
can access the active session without importing onboarding.
"""
from __future__ import annotations
from typing import Optional

# Set by the lobby when a game is created or joined.
_session: Optional[object] = None
