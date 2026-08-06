"""Neo: a local, provider-neutral coding agent."""

from .agent import Agent, AgentError
from .config import Config, load_config

__all__ = ["Agent", "AgentError", "Config", "load_config"]
__version__ = "0.1.0"

