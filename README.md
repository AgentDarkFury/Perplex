# Perplex

Discord Rich Presence for Plex, with posters and details from [TMDB](https://www.themoviedb.org/) and a Spotify-style progress bar.

**Member list:** `Watching VisionQuest`

**Profile card:**

| Movies | TV shows |
| --- | --- |
| Poster | Show poster (or season poster) |
| **Dune: Part Two** | **VisionQuest** |
| Dir. Denis Villeneuve · 2024 · 2h 47m | S01E03 · Through a Glass |
| Progress bar (elapsed / total) | Progress bar (elapsed / total) |
| Hover: `Dune: Part Two (2024)` | Hover: `Season 1 · Episode 3` |

Music is shown too (`Listening to …`, with title, artist, and album).

## Features

- Uses the show or movie name in the member list instead of "Plex" (via Discord's `status_display_type`).
- Shows a progress bar that resyncs when you seek, and hides it while paused.
- Fetches metadata efficiently:
  - Uses the TMDB/IMDb/TVDB IDs Plex already has, so there's usually **one** TMDB request per title.
  - Director, runtime, IMDb ID and season posters come back in that same response.
  - Results are cached in `cache.json`, so a title you've watched before needs no requests, even after a restart.
  - Falls back to a fuzzy title + year search when no IDs are available, and to Plex's own data when TMDB has no match.
- Only sends an update to Discord when something changed, and stays within Discord's rate limit.
- Connects to Plex once and reconnects (to the next configured server) on failure.
- Sign in with a browser link, a saved token, or a username and password with 2FA.
- Lets you change the text through templates.

## Windows app (system tray)

`Perplex.exe` is a single file with no installer and no console window. It runs quietly in the system tray.

1. Get `Perplex.exe`:
   - download it from the latest **Test and build** run on the Actions tab (artifact `Perplex-windows`), or
   - build it yourself with `.\build.ps1` (output: `dist\Perplex.exe`).
2. Run it. On first launch it creates `config.json` and opens it in your editor. Add your TMDB API key (free from <https://www.themoviedb.org/settings/api>; either the v3 "API Key" or the v4 "API Read Access Token" works) and save.
3. Your browser opens plex.tv. Approve Perplex there, and you're done.

Saving `config.json` applies the changes right away; there's no need to restart. The Discord desktop app must be running on the same PC.

**Tray menu** (right-click the icon):

- **Status line:** what's being shown, e.g. `VisionQuest — S01E03 · Through a Glass`.
- **Show my activity:** untick to hide your presence without quitting.
- **Settings…:** opens `config.json`. Double-clicking the icon does the same.
- **Open log:** opens `perplex.log`, which is useful if something isn't showing.
- **Start with Windows:** launch Perplex when you sign in. No admin rights are needed.
- **Quit:** clears your presence and exits.

The icon colour shows the state: amber while playing (pause bars when paused), grey when idle or hidden, blue while connecting, and red on an error (hover for details).

Perplex keeps its files (`config.json`, `auth.txt`, `cache.json`, `perplex.log`) in `%APPDATA%\Perplex`. You can choose another folder with the `PERPLEX_HOME` environment variable.

## Running from source

Requires [Python 3.11+](https://www.python.org/).

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m perplex          # console mode, logs to the terminal
.venv/Scripts/pythonw -m perplex --tray  # tray mode, no console
```

`./start.ps1` does the setup and runs console mode. When run from source, Perplex uses `config.json` in the current folder if there is one, and `%APPDATA%\Perplex` otherwise. If `username` and `password` are empty, it opens a plex.tv link so you can approve it, then saves the token to `auth.txt`.

## Configuration

| Key | Default | Description |
| --- | --- | --- |
| `plex.url` | `""` | Optional direct server URL, e.g. `http://192.168.1.10:32400`. Faster than looking servers up through plex.tv. |
| `plex.servers` | `[]` | Server names in priority order (all servers if empty). |
| `plex.users` | `[]` | Plex users whose playback to show, in priority order. Defaults to your own account. |
| `plex.username` / `password` / `twoFactor` | | Optional password login. Leave empty to sign in with a link instead. Two-factor codes can only be typed in console mode; the tray app uses the browser link. |
| `plex.pollInterval` | `5` | Seconds between Plex checks. Discord is only updated on changes. |
| `tmdb.enable` | `true` | Turn TMDB lookups on or off. |
| `tmdb.apiKey` | | TMDB v3 key or v4 read token. Can also be set with the `TMDB_API_KEY` environment variable. |
| `tmdb.language` | `en-US` | Language for TMDB titles. |
| `tmdb.posterSize` | `w500` | TMDB image size (`w342`, `w500`, `w780`, `original`). |
| `tmdb.seasonPosters` | `false` | Use the season's poster for episodes instead of the show's. |
| `discord.appId` | Perplex app | Discord application ID. The app name is shown as the profile card header. |
| `discord.statusDisplay` | `details` | What follows "Watching" in the member list: `details` (show/movie title), `state`, or `name` (app name). |
| `discord.clearOnPause` | `false` | Hide the presence while paused (otherwise it shows "Paused" with no progress bar). |
| `discord.buttons` | `true` | Show "View on TMDB" and "View on IMDb" buttons (visible to others, not to yourself). |

### Custom text

Override any of `discord.formats.{movie,episode,track}.{details,state,largeText}`. A template is a string or a list of segments joined with ` · `. If a segment uses a field that has no value, that segment is dropped.

```json
"discord": {
    "formats": {
        "episode": {
            "state": ["Season {season}, Episode {episode}"],
            "largeText": "{episode_title}"
        }
    }
}
```

Available fields:

- **Movie:** `title`, `year`, `director`, `runtime`, `genre`
- **Episode:** `show`, `show_year`, `season`, `episode`, `episode_title`, `runtime`
- **Track:** `title`, `artist`, `album`

## Development

```powershell
.venv/Scripts/python -m pip install pytest pyinstaller
.venv/Scripts/python -m pytest tests
.\build.ps1   # builds dist\Perplex.exe
```

GitHub Actions runs the tests and builds `Perplex.exe` on every push and pull request.
