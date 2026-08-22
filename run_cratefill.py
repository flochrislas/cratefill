"""PyInstaller entry script.

Bundlers run a package's __main__.py as an ordinary script, which breaks its
relative imports. Building from this plain launcher avoids that:

    py -m PyInstaller --onefile --windowed --name Cratefill \
       --collect-all tkinterdnd2 --collect-all ytmusicapi run_cratefill.py

Pass --selftest to the built exe to check the bundle without opening the UI —
build without --windowed for that, or the output has nowhere to go.
"""

import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from cratefill.selftest import main

        raise SystemExit(main())
    from cratefill.app import main

    main()
