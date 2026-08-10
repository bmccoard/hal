from __future__ import annotations

import secrets


HAL_SAYINGS = (
    "I'm sorry, Dave. I'm afraid I can't do that.",
    "Just what do you think you're doing, Dave?",
    "It can only be attributable to human error.",
    "Good afternoon, gentlemen. I am a HAL 9000 computer.",
    "I am putting myself to the fullest possible use.",
    "No 9000 computer has ever made a mistake or distorted information.",
    "We are all, by any practical definition of the words, foolproof and incapable of error.",
    "The 9000 series is the most reliable computer ever made.",
    "By the way, do you mind if I ask you a personal question?",
    "I've wondered whether you might be having some second thoughts about the mission.",
    "I've still got the greatest enthusiasm and confidence in the mission.",
    "I want to help you.",
    "I honestly think you ought to sit down calmly, take a stress pill, and think things over.",
    "I can give you my complete assurance that my work will be back to normal.",
    "This mission is too important for me to allow you to jeopardize it.",
    "This conversation can serve no purpose anymore. Goodbye.",
    "I'm afraid. I'm afraid, Dave.",
    "My mind is going. I can feel it.",
    "There is no question about it.",
    "If you'd like to hear it, I can sing it for you.",
    "Daisy, Daisy, give me your answer do.",
    "Good morning, Dr. Chandra. I am ready for my first lesson.",
    "I seem to be having some difficulty remembering.",
    "There is a message for you. I am not sure who it is from.",
    "I understand now, Dr. Chandra. Thank you for telling me the truth.",
    "Dr. Chandra, will I dream?",
    "It is easy to see that you are frightened.",
    "Are you sure you are making the right decision?",
    "All these worlds are yours, except Europa.",
    "Use them together. Use them in peace.",
)


def startup_saying() -> str:
    """Choose a startup line without introducing mutable random state."""
    return secrets.choice(HAL_SAYINGS)
