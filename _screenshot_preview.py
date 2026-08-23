"""Dev helper: render the app with sample data and screenshot it to ui_preview.png.

Needs a real X server. Pillow's ImageGrab cannot capture under Wayland/XWayland,
so on a Wayland desktop run it against a private display instead:

    DISPLAY=:99 sh -c 'Xvfb :99 -screen 0 1280x800x24 & sleep 2; \
        .venv/bin/python _screenshot_preview.py'

Copy the result over docs/screenshot.png when the UI changes — it is the image
the README and the PyPI project page both show.
"""
import tkinter as tk

from PIL import ImageGrab

from cratefill.app import CratefillApp, apply_dark_theme, enable_dark_title_bar
from cratefill.matching import choose_match
from cratefill.storage import read_songs_csv
from cratefill.youtube import _decision_line

root = tk.Tk()
apply_dark_theme(root)
enable_dark_title_bar(root)
app = CratefillApp(root)

app.songs = read_songs_csv("sample.csv")
app.populate_song_tree()
app.song_tree.selection_set(*app.song_tree.get_children())   # arms the Add button
app.csv_label.configure(text=f"sample.csv — {len(app.songs)} songs")

for label in ("Road trip  (42 tracks)", "Favorites  (118 tracks)", "Chill  (23 tracks)"):
    app.playlist_list.insert("end", label)
app.playlist_list.selection_set(0)
app.account_label.configure(text="Logged in")
app.refresh_add_button()

# Real decisions through the real matcher, so the Messages pane can't drift out
# of date: one confident, one offered-but-different-recording, one no-match.
SEARCH_RESULTS = {
    "Harder Better Faster Stronger": [
        {"videoId": "v1", "title": "Harder, Better, Faster, Stronger",
         "artists": [{"name": "Daft Punk"}]}],
    "Lisztomania": [
        {"videoId": "v2", "title": "Lisztomania (Live at Madison Square Garden)",
         "artists": [{"name": "Phoenix"}]}],
    "La Femme d'Argent": [
        {"videoId": "v3", "title": "Sexy Boy", "artists": [{"name": "Air"}]}],
}
app.log("Found 3 playlists.")
app.log(f"Loaded {len(app.songs)} songs from sample.csv")
app.log(f"--- Matching {len(app.songs)} song(s) for 1 playlist(s) ---")
for artist, title, _station in app.songs:
    app.log(_decision_line(artist, title, choose_match(artist, title, SEARCH_RESULTS[title])))

root.update_idletasks()
root.update()
# Exactly the window, no margin for decoration: a bare X server (Xvfb) has no
# window manager to draw a title bar, so reserving space for one just adds a
# black band to the image.
x, y = root.winfo_rootx(), root.winfo_rooty()
ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save("ui_preview.png")
root.destroy()
print("saved ui_preview.png")
