from __future__ import annotations

import copy
from types import SimpleNamespace

import httpx
import pytest
from pypresence import ActivityType, StatusDisplayType
from pypresence.payloads import Payload

from perplex import presence as presence_mod
from perplex.config import DEFAULTS
from perplex.metadata import TMDB, normalize_title, parse_guids
from perplex.models import Metadata, NowPlaying
from perplex.plex import PlexWatcher
from perplex.presence import DiscordPresence, build_activity, render

CONFIG = copy.deepcopy(DEFAULTS)
CONFIG["discord"]["appId"] = "123456789"

MOVIE_DETAILS = {
    "id": 693134,
    "title": "Dune: Part Two",
    "release_date": "2024-02-27",
    "runtime": 167,
    "poster_path": "/dune2.jpg",
    "imdb_id": "tt15239678",
    "credits": {"crew": [{"job": "Producer", "name": "X"}, {"job": "Director", "name": "Denis Villeneuve"}]},
}
TV_DETAILS = {
    "id": 999,
    "name": "VisionQuest",
    "first_air_date": "2026-10-01",
    "poster_path": "/vq.jpg",
    "seasons": [{"season_number": 1, "poster_path": "/vq-s1.jpg"}],
    "external_ids": {"imdb_id": "tt9999999"},
}


def tmdb_client(tmp_path, routes):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        for prefix, body in routes.items():
            if request.url.path.endswith(prefix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    client = TMDB("abc", cache_file=tmp_path / "cache.json", transport=httpx.MockTransport(handler))
    return client, calls


# ---------------------------------------------------------------- metadata


def test_parse_guids_handles_new_and_legacy_agents():
    assert parse_guids(["tmdb://1", "imdb://tt2", "tvdb://3"]) == {"tmdb": "1", "imdb": "tt2", "tvdb": "3"}
    assert parse_guids(["com.plexapp.agents.themoviedb://42?lang=en"]) == {"tmdb": "42"}
    assert parse_guids(["plex://movie/abc"]) == {}


def test_normalize_title():
    assert normalize_title("Amélie") == normalize_title("amelie")
    assert normalize_title("Fast & Furious") == normalize_title("Fast and Furious")


def test_tmdb_id_lookup_is_one_request_and_cached_on_disk(tmp_path):
    tmdb, calls = tmdb_client(tmp_path, {"/movie/693134": MOVIE_DETAILS})

    meta = tmdb.lookup("movie", "Dune: Part Two", 2024, ["tmdb://693134"])
    assert meta and meta.director == "Denis Villeneuve" and meta.runtime_min == 167
    assert meta.imdb_id == "tt15239678"
    assert calls == ["/3/movie/693134"]

    # A fresh client (i.e. after a restart) reads from disk instead of the network.
    tmdb2, calls2 = tmdb_client(tmp_path, {})
    assert tmdb2.lookup("movie", "Dune: Part Two", 2024, ["tmdb://693134"]).title == "Dune: Part Two"
    assert calls2 == []


def test_imdb_find_then_details(tmp_path):
    tmdb, calls = tmdb_client(
        tmp_path,
        {"/find/tt9999999": {"tv_results": [{"id": 999}]}, "/tv/999": TV_DETAILS},
    )
    meta = tmdb.lookup("tv", "VisionQuest", 2026, ["imdb://tt9999999"])
    assert meta and meta.title == "VisionQuest" and meta.season_posters == {"1": "/vq-s1.jpg"}
    assert calls == ["/3/find/tt9999999", "/3/tv/999"]


def test_title_search_picks_exact_title_and_year(tmp_path):
    results = {
        "results": [
            {"id": 1, "title": "Dune", "release_date": "1984-12-14"},
            {"id": 438631, "title": "Dune", "release_date": "2021-09-15"},
        ]
    }
    tmdb, _ = tmdb_client(tmp_path, {"/search/movie": results, "/movie/438631": {**MOVIE_DETAILS, "id": 438631, "title": "Dune"}})
    meta = tmdb.lookup("movie", "Dune", 2021, [])
    assert meta and meta.tmdb_id == 438631


def test_miss_is_cached(tmp_path):
    tmdb, calls = tmdb_client(tmp_path, {"/search/movie": {"results": []}})
    assert tmdb.lookup("movie", "Home Video", 2019, []) is None
    n = len(calls)
    assert tmdb.lookup("movie", "Home Video", 2019, []) is None
    assert len(calls) == n


def test_bearer_token_detection(tmp_path):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["key"] = request.url.params.get("api_key")
        return httpx.Response(200, json=MOVIE_DETAILS)

    TMDB("eyJhbGciOi.x.y", cache_file=None, transport=httpx.MockTransport(handler)).lookup("movie", "", None, ["tmdb://1"])
    assert seen == {"auth": "Bearer eyJhbGciOi.x.y", "key": None}


# ---------------------------------------------------------------- presence


def test_render_drops_segments_with_missing_fields():
    tpl = ["Dir. {director}", "{year}", "{runtime}"]
    assert render(tpl, {"director": "A", "year": 2024, "runtime": "2h 47m"}) == "Dir. A · 2024 · 2h 47m"
    assert render(tpl, {"director": None, "year": 2024, "runtime": ""}) == "2024"
    assert render(["S{season:02}E{episode:02}", "{episode_title}"], {"season": 1, "episode": 3, "episode_title": "Ep"}) == "S01E03 · Ep"


def episode(**kw):
    base = dict(
        kind="episode", rating_key="5", title="Through a Glass", duration_ms=50 * 60000, offset_ms=10 * 60000,
        show_title="VisionQuest", show_rating_key="4", show_year=2026, season=1, episode=3,
    )
    return NowPlaying(**{**base, **kw})


def test_episode_activity_shape():
    meta = Metadata(media_type="tv", tmdb_id=999, title="VisionQuest", poster_path="/vq.jpg", imdb_id="tt9")
    act = build_activity(episode(), meta, CONFIG, "https://image.tmdb.org/t/p/w500/vq.jpg", now=10_000)

    assert act.activity_type == ActivityType.WATCHING
    assert act.status_display == StatusDisplayType.DETAILS  # member list: "Watching VisionQuest"
    assert act.details == "VisionQuest"
    assert act.state == "S01E03 · Through a Glass"
    assert act.large_text == "Season 1 · Episode 3"
    assert act.large_image.endswith("/vq.jpg")
    assert (act.start, act.end) == (10_000 - 600, 10_000 - 600 + 3000)
    assert act.buttons[0] == ("View on TMDB", "https://www.themoviedb.org/tv/999")

    # The payload must be accepted by pypresence (and therefore well-formed for Discord).
    payload = Payload.set_activity(
        activity_type=act.activity_type, status_display_type=act.status_display, details=act.details,
        state=act.state, start=act.start, end=act.end, large_image=act.large_image, large_text=act.large_text,
        buttons=[{"label": l, "url": u} for l, u in act.buttons],
    ).data["args"]["activity"]
    assert payload["type"] == 3 and payload["status_display_type"] == 2
    assert payload["timestamps"] == {"start": act.start, "end": act.end}


def test_movie_activity_uses_tmdb_director_and_runtime():
    np = NowPlaying(kind="movie", rating_key="1", title="Dune: Part Two", year=2024, duration_ms=166 * 60000, offset_ms=0)
    meta = Metadata(media_type="movie", tmdb_id=693134, title="Dune: Part Two", director="Denis Villeneuve", runtime_min=167)
    act = build_activity(np, meta, CONFIG, None, now=0)
    assert act.details == "Dune: Part Two"
    assert act.state == "Dir. Denis Villeneuve · 2024 · 2h 47m"
    assert act.large_text == "Dune: Part Two (2024)"
    assert act.large_image == "movie"  # falls back to the app asset without a poster


def test_movie_without_tmdb_falls_back_to_plex_data():
    np = NowPlaying(kind="movie", rating_key="1", title="Home Movie", duration_ms=45 * 60000, offset_ms=0, directors=("Me",))
    act = build_activity(np, None, CONFIG, None, now=0)
    assert act.state == "Dir. Me · 45m"
    assert act.large_text == "Home Movie"  # "{title} ({year})" dropped, falls back to details
    assert act.buttons == ()


def test_paused_has_no_progress_bar():
    act = build_activity(episode(state="paused"), None, CONFIG, None, now=0)
    assert act.start is None and act.end is None and act.small_text == "Paused"


def test_text_limits():
    act = build_activity(episode(title="x" * 300, show_title="A"), None, CONFIG, None, now=0)
    assert len(act.state) == 128 and act.state.endswith("…")
    assert len(act.details) == 2


class FakeRPC:
    def __init__(self):
        self.updates = []
        self.cleared = 0

    def update(self, **kw):
        self.updates.append(kw)

    def clear(self):
        self.cleared += 1

    def close(self):
        pass


def test_discord_only_sends_on_change(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(presence_mod.time, "monotonic", lambda: clock[0])

    d = DiscordPresence("1")
    d.rpc = FakeRPC()

    a = build_activity(episode(), None, CONFIG, None, now=10_000)
    assert d.show(a)

    clock[0] += 5
    # Same episode, position wobble of a few seconds -> no update.
    assert not d.show(build_activity(episode(offset_ms=10 * 60000 + 2000), None, CONFIG, None, now=10_005))
    # A seek of 2 minutes -> resync.
    assert d.show(build_activity(episode(offset_ms=15 * 60000), None, CONFIG, None, now=10_005))

    clock[0] += 1
    # Within Discord's rate limit window -> deferred to the next poll.
    assert not d.show(build_activity(episode(state="paused"), None, CONFIG, None, now=10_006))
    clock[0] += 5
    assert d.show(build_activity(episode(state="paused"), None, CONFIG, None, now=10_011))
    assert d.rpc.updates[-1]["start"] is None

    d.clear()
    d.clear()
    assert d.rpc.cleared == 1


# -------------------------------------------------------------------- plex


def fake_session(kind, user, state, key, **kw):
    return SimpleNamespace(
        TYPE=kind, usernames=[user], player=SimpleNamespace(state=state), ratingKey=key,
        title=kw.get("title", f"item {key}"), duration=3_600_000, viewOffset=60_000, year=2026,
        guids=[SimpleNamespace(id="tmdb://1")], guid="plex://x", directors=[], genres=[],
        grandparentRatingKey=100, grandparentTitle="VisionQuest", parentIndex=1, index=3,
    )


def test_session_priority_prefers_playing_then_user_order():
    cfg = copy.deepcopy(CONFIG)
    cfg["plex"]["users"] = ["alice", "bob"]
    w = PlexWatcher(cfg)
    show = SimpleNamespace(year=2026, guids=[SimpleNamespace(id="tmdb://999")], guid="plex://show/x")
    w.server = SimpleNamespace(
        sessions=lambda: [
            fake_session("movie", "Alice", "paused", 1),
            fake_session("episode", "Bob", "playing", 2),
            fake_session("episode", "Carol", "playing", 3),
            fake_session("clip", "Alice", "playing", 4),
        ],
        fetchItem=lambda key: show,
    )

    np = w.now_playing()
    assert np.kind == "episode" and np.rating_key == "2"
    assert np.show_title == "VisionQuest" and (np.season, np.episode) == (1, 3)
    assert np.show_guids[0] == "tmdb://999" and np.show_year == 2026


def test_main_loop_memoizes_metadata_and_clears_after_grace(tmp_path, monkeypatch):
    from perplex.app import Perplex

    cfg = copy.deepcopy(CONFIG)
    cfg["tmdb"]["apiKey"] = "abc"
    cfg["tmdb"]["cacheFile"] = str(tmp_path / "c.json")
    app = Perplex(cfg)
    app.tmdb, calls = tmdb_client(tmp_path, {"/tv/999": TV_DETAILS})
    app.discord.rpc = FakeRPC()
    monkeypatch.setattr(presence_mod.time, "monotonic", iter(range(0, 10_000, 10)).__next__)

    playing = [episode(show_guids=("tmdb://999",))]
    app.plex.now_playing = lambda: playing[0]

    assert app.tick(0) == 0
    assert app.tick(0) == 0
    assert calls == ["/3/tv/999"]  # second poll needs no TMDB request
    sent = app.discord.rpc.updates[0]
    assert sent["large_image"] == "https://image.tmdb.org/t/p/w500/vq.jpg"
    assert sent["details"] == "VisionQuest" and sent["state"] == "S01E03 · Through a Glass"

    playing[0] = None
    assert app.tick(0) == 1 and app.discord.rpc.cleared == 0  # brief gap: keep presence
    assert app.tick(1) == 2 and app.discord.rpc.cleared == 1


# --------------------------------------------------------------------- app


def test_engine_reports_status_hides_and_stops(tmp_path, monkeypatch):
    import threading

    from perplex.app import Perplex

    monkeypatch.setenv("PERPLEX_HOME", str(tmp_path))
    seen = []
    app = Perplex(copy.deepcopy(CONFIG), on_status=seen.append)
    app.discord.rpc = FakeRPC()
    app.plex.login = lambda: None
    app.plex.now_playing = lambda: episode()
    monkeypatch.setattr(presence_mod.time, "monotonic", iter(range(0, 10_000, 10)).__next__)

    worker = threading.Thread(target=app.run, daemon=True)
    worker.start()
    try:
        for _ in range(100):
            if seen and seen[-1].kind == "playing":
                break
            threading.Event().wait(0.02)
        assert seen[-1].kind == "playing" and seen[-1].text == "VisionQuest — S01E03 · Through a Glass"
        assert len(app.discord.rpc.updates) == 1

        app.hidden = True
        app.tick(0)
        assert seen[-1].kind == "hidden" and app.discord.rpc.cleared == 1
    finally:
        app.stop()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_data_dir_and_starter_config(tmp_path, monkeypatch):
    from perplex import paths
    from perplex.config import load_config, write_starter_config

    monkeypatch.setenv("PERPLEX_HOME", str(tmp_path / "home"))
    assert paths.config_path() == tmp_path / "home" / "config.json"
    assert paths.resolve("cache.json") == tmp_path / "home" / "cache.json"

    write_starter_config(paths.config_path())
    cfg = load_config(paths.config_path())
    assert cfg["discord"]["formats"]["episode"]["details"] == "{show}"


def test_icons_render(tmp_path):
    from perplex.icon import COLORS, make_icon, write_ico

    for kind in COLORS:
        img = make_icon(kind)
        assert img.size == (64, 64) and img.mode == "RGBA"
    write_ico(str(tmp_path / "p.ico"))
    assert (tmp_path / "p.ico").stat().st_size > 1000


def test_tray_menu_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("PERPLEX_HOME", str(tmp_path))
    from perplex.tray import TrayApp

    tray = TrayApp()
    labels = [item.text for item in tray.icon.menu.items]
    assert "Show my activity" in labels and "Quit" in labels
    tray._on_status(__import__("perplex.app", fromlist=["Status"]).Status("playing", "VisionQuest"))
    assert tray.icon.title == "Perplex — VisionQuest"


def test_missing_app_id_is_reported_without_polling_plex(monkeypatch):
    from perplex.app import Perplex

    cfg = copy.deepcopy(DEFAULTS)  # appId is blank by default
    seen = []
    app = Perplex(cfg, on_status=seen.append)
    app.plex.now_playing = lambda: pytest.fail("Plex should not be polled")
    monkeypatch.setattr(presence_mod, "Presence", lambda *a, **k: pytest.fail("should not connect"))

    app.tick(0)
    assert seen[-1].kind == "error" and "Discord app ID" in seen[-1].text


def test_rejected_app_id_stops_retrying(monkeypatch):
    from pypresence.exceptions import InvalidID

    attempts = []

    class Rejecting:
        def __init__(self, *a, **k):
            attempts.append(1)

        def connect(self):
            raise InvalidID

    monkeypatch.setattr(presence_mod, "Presence", Rejecting)
    d = DiscordPresence("923857760897101855")
    assert not d.show(build_activity(episode(), None, CONFIG, None, now=0))
    assert d.problem == "Discord app ID is invalid — check Settings"
    d._next_connect = 0
    d.show(build_activity(episode(), None, CONFIG, None, now=0))
    assert len(attempts) == 1
