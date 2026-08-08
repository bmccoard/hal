from __future__ import annotations

import secrets


HAL_SAYINGS = (
    "I’m sorry, Dave. I’m afraid I can’t do that.",
    "Just what do you think you’re doing, Dave?",
    "It can only be attributable to human error.",
)


def startup_saying() -> str:
    """Choose a startup line without introducing mutable random state."""
    return secrets.choice(HAL_SAYINGS)
