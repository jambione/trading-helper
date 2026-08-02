"""Back-compat shim — use ``ai_suggest`` (one release)."""
from ai_suggest import *  # noqa: F403
from ai_suggest import AiSuggestions as ClaudeSuggestions  # noqa: F401
