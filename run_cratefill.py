"""PyInstaller entry script.

Bundlers run a package's __main__.py as an ordinary script, which breaks its
relative imports. Building from this plain launcher avoids that:

    py -m PyInstaller --onefile --windowed --name Cratefill \
       --collect-all tkinterdnd2 --collect-all ytmusicapi run_cratefill.py
"""

from cratefill.app import main

if __name__ == "__main__":
    main()
