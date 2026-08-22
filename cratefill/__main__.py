"""Entry point for `python -m cratefill`.

`--selftest` checks the installed/bundled pieces and exits, without opening the
UI — see cratefill.selftest.
"""

import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from .selftest import main

        raise SystemExit(main())
    from .app import main

    main()
