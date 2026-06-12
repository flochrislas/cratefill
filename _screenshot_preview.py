"""Dev helper: render the app with sample data and screenshot it to ui_preview.png."""
import tkinter as tk

from PIL import ImageGrab

from cratefill import CratefillApp, apply_dark_theme, enable_dark_title_bar, read_songs_csv

root = tk.Tk()
apply_dark_theme(root)
enable_dark_title_bar(root)
app = CratefillApp(root)

app.songs = read_songs_csv("sample.csv")
app.populate_song_tree()
app.song_tree.selection_set("0", "1")
app.csv_label.configure(text=f"sample.csv — {len(app.songs)} songs")

for label in ("Road trip  (42 tracks)", "Favorites  (118 tracks)", "Chill  (23 tracks)"):
    app.playlist_list.insert("end", label)
app.playlist_list.selection_set(0)
app.account_label.configure(text="Logged in")

app.log("Loaded 3 songs from sample.csv")
app.log("✓ Daft Punk — Harder Better Faster Stronger")
app.log("? Phoenix — Lisztomania: uncertain match → Phoenix — Lisztomania (Live)")

root.update_idletasks()
root.update()
x, y = root.winfo_rootx(), root.winfo_rooty()
w, h = root.winfo_width(), root.winfo_height()
ImageGrab.grab((x - 10, y - 40, x + w + 10, y + h + 10)).save("ui_preview.png")
root.destroy()
print("saved ui_preview.png")
