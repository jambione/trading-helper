"""Back-compat shim — use ``ai_trader`` (one release)."""
from ai_trader import *  # noqa: F403
if __name__ == "__main__":
    from ai_trader import main
    main()
