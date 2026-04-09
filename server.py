#!/usr/bin/env python3
"""
Steam Library MCP Server

Connects Claude to Trent's Steam game library hosted on Turso.
Provides tools for querying games, getting recommendations, tracking progress,
and managing the backlog across multiple devices.
"""

import json
import os
import pathlib
import asyncio
import base64
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime

import httpx
import libsql_client
from pydantic import BaseModel, Field, ConfigDict, field_validator
from fastmcp import FastMCP

try:
    from prefab_ui.app import PrefabApp
    from prefab_ui.themes import Theme
    from prefab_ui.components import (
        Column, Row, Grid, Card, CardContent, CardFooter, CardHeader, CardTitle, CardDescription,
        Dashboard, DashboardItem,
        Heading, Badge, Muted, Separator, Image, Progress,
        Metric, Ring, Tabs, Tab,
        DataTable, DataTableColumn,
        Alert, AlertTitle, AlertDescription,
        Text, Div, Small,
    )
    _HAS_PREFAB = True
except Exception:
    _HAS_PREFAB = False


# =============================================================================
# Configuration
# =============================================================================

TURSO_URL = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
STEAM_ID = os.environ.get("STEAM_ID", "76561198008411530")

if not TURSO_URL or not TURSO_TOKEN:
    import sys
    print("Error: TURSO_URL and TURSO_TOKEN environment variables are required.", file=sys.stderr)
    print("Set them in your Claude Desktop config or .env file.", file=sys.stderr)

# =============================================================================
# Initialize MCP Server
# =============================================================================

mcp = FastMCP("steam_library_mcp")

# =============================================================================
# Database Helper
# =============================================================================

async def _query_turso(sql: str, params: Optional[list] = None) -> list:
    """Execute a query against Turso and return rows as list of dicts."""
    async with libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN) as client:
        if params:
            result = await client.execute(sql, params)
        else:
            result = await client.execute(sql)

        if not result.columns or not result.rows:
            return []

        columns = result.columns
        return [dict(zip(columns, row)) for row in result.rows]


async def _execute_turso(sql: str, params: Optional[list] = None) -> int:
    """Execute a write query against Turso and return rows affected."""
    async with libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN) as client:
        if params:
            result = await client.execute(sql, params)
        else:
            result = await client.execute(sql)
        return result.rows_affected


def _format_hours(minutes: Optional[int]) -> str:
    """Convert minutes to human-readable hours."""
    if not minutes or minutes == 0:
        return "0h"
    hours = minutes / 60
    if hours < 1:
        return f"{minutes}m"
    return f"{hours:.1f}h"


def _format_relative_date(date_str: Optional[str]) -> Optional[str]:
    """Convert a YYYY-MM-DD string to a human-readable relative date."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        delta = (datetime.now().date() - d).days
        if delta < 7:
            return f"{delta}d ago" if delta > 0 else "today"
        if delta < 30:
            return f"{delta // 7}w ago"
        if delta < 365:
            return f"{delta // 30}mo ago"
        years = delta // 365
        return f"{years}y ago"
    except (ValueError, TypeError):
        return None


def _format_game_summary(game: dict) -> str:
    """Format a single game into a readable summary line."""
    name = game.get("name", "Unknown")
    playtime = _format_hours(game.get("playtime_minutes"))
    pct = game.get("completion_pct", 0)
    review = game.get("review_score")
    review_str = f"{review:.0f}%" if review else "N/A"
    deck = game.get("deck_status", "unknown")
    hltb = game.get("hltb_main_hours")
    hltb_str = f"{hltb:.0f}h" if hltb else "?"
    genre = game.get("primary_genre", "")
    status = game.get("status", "")

    return (f"**{name}**  -  {playtime} played | {pct:.0f}% achievements | "
            f"Reviews: {review_str} | HLTB: {hltb_str} | Deck: {deck} | {genre} | Status: {status}")


# =============================================================================
# Input Models
# =============================================================================

class DeviceEnum(str, Enum):
    STEAM_DECK = "steam_deck"
    LIVING_ROOM = "living_room"
    OFFICE = "office"
    ANY = "any"


class GameStatusEnum(str, Enum):
    UNPLAYED = "unplayed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    NOT_INTERESTED = "not_interested"


class SearchGamesInput(BaseModel):
    """Input for searching games in the library."""
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(
        ...,
        description="Search term to match against game names (e.g., 'Portal', 'Final Fantasy', 'Souls')",
        min_length=1, max_length=200
    )
    limit: Optional[int] = Field(default=20, description="Max results to return", ge=1, le=100)


class QueryLibraryInput(BaseModel):
    """Input for flexible library queries with filters."""
    model_config = ConfigDict(str_strip_whitespace=True)

    genre: Optional[str] = Field(default=None, description="Filter by genre (e.g., 'RPG', 'Action', 'Indie', 'Racing')")
    device: Optional[DeviceEnum] = Field(default=None, description="Filter by device compatibility: steam_deck, living_room, office, or any")
    status: Optional[GameStatusEnum] = Field(default=None, description="Filter by play status: unplayed, in_progress, completed, abandoned, not_interested")
    min_review_score: Optional[float] = Field(default=None, description="Minimum review score percentage (0-100)", ge=0, le=100)
    min_review_count: Optional[int] = Field(default=None, description="Minimum number of reviews", ge=0)
    max_hltb_hours: Optional[float] = Field(default=None, description="Maximum HowLongToBeat main story hours", ge=0)
    min_completion_pct: Optional[float] = Field(default=None, description="Minimum achievement completion percentage (0-100)", ge=0, le=100)
    max_completion_pct: Optional[float] = Field(default=None, description="Maximum achievement completion percentage (0-100)", ge=0, le=100)
    has_achievements: Optional[bool] = Field(default=None, description="Filter to only games with achievements")
    sort_by: Optional[str] = Field(
        default="review_score",
        description="Sort results by: review_score, playtime, completion_pct, hltb_main_hours, name, last_played, metacritic"
    )
    sort_order: Optional[str] = Field(default="DESC", description="Sort order: ASC or DESC")
    limit: Optional[int] = Field(default=25, description="Max results to return", ge=1, le=100)
    offset: Optional[int] = Field(default=0, description="Pagination offset", ge=0)

    @field_validator("sort_by")
    @classmethod
    def validate_sort(cls, v: Optional[str]) -> Optional[str]:
        allowed = {"review_score", "playtime_minutes", "playtime", "completion_pct",
                   "hltb_main_hours", "name", "last_played", "metacritic", "review_count"}
        if v and v not in allowed:
            raise ValueError(f"sort_by must be one of: {', '.join(allowed)}")
        if v == "playtime":
            return "playtime_minutes"
        return v


class GetRecommendationsInput(BaseModel):
    """Input for getting personalized game recommendations."""
    model_config = ConfigDict(str_strip_whitespace=True)

    device: Optional[DeviceEnum] = Field(
        default=DeviceEnum.ANY,
        description="Which device are you on? steam_deck, living_room, office, or any"
    )
    available_hours: Optional[float] = Field(
        default=None,
        description="How many hours do you have to play? Filters to games beatable in this time.",
        ge=0.5, le=200
    )
    mood: Optional[str] = Field(
        default=None,
        description="What kind of game are you in the mood for? (e.g., 'relaxing', 'intense', 'story-rich', 'quick session', 'classic', 'indie', 'new')"
    )
    genre: Optional[str] = Field(default=None, description="Preferred genre (e.g., 'RPG', 'Action', 'Platformer')")
    count: Optional[int] = Field(default=8, description="Number of recommendations", ge=1, le=20)
    include_in_progress: Optional[bool] = Field(default=True, description="Include games you've already started?")


class RenderGamesInput(BaseModel):
    """Input for rendering a custom list of games in the Prefab UI."""
    model_config = ConfigDict(str_strip_whitespace=True)

    app_ids: list[int] = Field(
        ...,
        description="List of Steam app_ids to render (from steam_run_query results)",
        min_length=1,
        max_length=20
    )
    label: Optional[str] = Field(
        default="Custom Selection",
        description="Header label shown in the UI (e.g. 'Hidden Gems', 'Short Horror Games')"
    )


class GetGameDetailInput(BaseModel):
    """Input for getting detailed info about a specific game."""
    game_name: str = Field(..., description="Name or partial name of the game to look up", min_length=1)


class UpdateGameStatusInput(BaseModel):
    """Input for updating a game's status."""
    game_name: str = Field(..., description="Name or partial name of the game", min_length=1)
    status: GameStatusEnum = Field(..., description="New status: completed, abandoned, not_interested, in_progress, unplayed")
    notes: Optional[str] = Field(default=None, description="Optional notes about why you're changing the status", max_length=500)


class GetStatsInput(BaseModel):
    """Input for library statistics."""
    category: Optional[str] = Field(
        default="overview",
        description="Stats category: overview, genres, completion, deck, backlog, playtime, recent"
    )


class RunSQLInput(BaseModel):
    """Input for running a custom read-only SQL query against the library."""
    model_config = ConfigDict(str_strip_whitespace=True)

    sql: str = Field(
        ...,
        description="SQL SELECT query to run against the steam library database. Tables: games, achievements, devices, user_overrides, sync_log. Only SELECT queries allowed.",
        min_length=5, max_length=2000
    )

    @field_validator("sql")
    @classmethod
    def validate_sql(cls, v: str) -> str:
        v_upper = v.strip().upper()
        if not v_upper.startswith("SELECT"):
            raise ValueError("Only SELECT queries are allowed for safety. Use steam_update_game_status for modifications.")
        dangerous = ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "TRUNCATE"]
        for keyword in dangerous:
            if keyword in v_upper.split("SELECT", 1)[0]:
                raise ValueError(f"Query contains forbidden keyword: {keyword}")
        return v


# =============================================================================
# Tools
# =============================================================================

@mcp.tool(
    name="steam_search_games",
    annotations={
        "title": "Search Steam Library",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_search_games(params: SearchGamesInput) -> str:
    """Search for games in the Steam library by name.

    Use this to find specific games, look up a title, or check if a game is owned.
    Searches across all 908 games in the library.

    Returns matching games with playtime, achievement progress, review scores,
    HLTB estimates, Deck compatibility, and genre.
    """
    try:
        rows = await _query_turso(
            """SELECT name, app_id, playtime_minutes, completion_pct, review_score,
                      review_desc, hltb_main_hours, deck_status, primary_genre, status,
                      achievements_unlocked, achievements_total, last_played_date
               FROM games
               WHERE LOWER(name) LIKE LOWER(?)
               ORDER BY playtime_minutes DESC
               LIMIT ?""",
            [f"%{params.query}%", params.limit]
        )

        if not rows:
            return f"No games found matching '{params.query}' in the library."

        lines = [f"## Search Results: '{params.query}' ({len(rows)} found)\n"]
        for g in rows:
            lines.append(_format_game_summary(g))
            if g.get("last_played_date"):
                lines.append(f"  Last played: {g['last_played_date']}")
            if g.get("achievements_total") and g["achievements_total"] > 0:
                lines.append(f"  Achievements: {g.get('achievements_unlocked', 0)}/{g['achievements_total']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"Error searching games: {e}"


@mcp.tool(
    name="steam_query_library",
    annotations={
        "title": "Query Steam Library with Filters",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_query_library(params: QueryLibraryInput) -> str:
    """Query the full Steam library with flexible filters.

    Filter by genre, device compatibility, play status, review scores, HLTB times,
    achievement completion, and more. Supports sorting and pagination.
    Scans all 908 games in the database  -  never a random subset.

    Use this for questions like:
    - "What unplayed RPGs do I have?"
    - "Show me Deck-verified games under 10 hours"
    - "What games am I closest to finishing?"
    - "Show me highly-rated games I haven't touched"
    """
    try:
        conditions = []
        query_params = []

        if params.genre:
            conditions.append("(LOWER(primary_genre) LIKE LOWER(?) OR LOWER(all_genres) LIKE LOWER(?))")
            query_params.extend([f"%{params.genre}%", f"%{params.genre}%"])

        if params.device and params.device != DeviceEnum.ANY:
            if params.device == DeviceEnum.STEAM_DECK:
                conditions.append("deck_status IN ('verified', 'playable')")
            elif params.device == DeviceEnum.LIVING_ROOM:
                # Controller-friendly: Deck verified/playable is a good proxy
                conditions.append("deck_status IN ('verified', 'playable')")
            # Office PC can play anything  -  no filter needed

        if params.status:
            conditions.append("status = ?")
            query_params.append(params.status.value)

        if params.min_review_score is not None:
            conditions.append("review_score >= ?")
            query_params.append(params.min_review_score)

        if params.min_review_count is not None:
            conditions.append("review_count >= ?")
            query_params.append(params.min_review_count)

        if params.max_hltb_hours is not None:
            conditions.append("hltb_main_hours IS NOT NULL AND hltb_main_hours <= ?")
            query_params.append(params.max_hltb_hours)

        if params.min_completion_pct is not None:
            conditions.append("completion_pct >= ?")
            query_params.append(params.min_completion_pct)

        if params.max_completion_pct is not None:
            conditions.append("completion_pct <= ?")
            query_params.append(params.max_completion_pct)

        if params.has_achievements:
            conditions.append("achievements_total > 0")

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        sort_col = params.sort_by or "review_score"
        sort_dir = params.sort_order or "DESC"

        sql = f"""SELECT name, app_id, playtime_minutes, completion_pct, review_score,
                         review_desc, review_count, hltb_main_hours, hltb_extra_hours,
                         deck_status, primary_genre, all_genres, status, metacritic,
                         achievements_unlocked, achievements_total, last_played_date,
                         developer
                  FROM games
                  {where_clause}
                  ORDER BY {sort_col} {sort_dir} NULLS LAST
                  LIMIT ? OFFSET ?"""

        query_params.extend([params.limit, params.offset])
        rows = await _query_turso(sql, query_params)

        # Get total count
        count_sql = f"SELECT COUNT(*) as total FROM games {where_clause}"
        count_rows = await _query_turso(count_sql, query_params[:-2] if query_params else None)
        total = count_rows[0]["total"] if count_rows else 0

        if not rows:
            return "No games matched your filters."

        lines = [f"## Library Query Results ({len(rows)} of {total} matches)\n"]
        for g in rows:
            lines.append(_format_game_summary(g))
            extra = []
            if g.get("developer"):
                extra.append(f"by {g['developer']}")
            if g.get("metacritic"):
                extra.append(f"Metacritic: {g['metacritic']}")
            if g.get("last_played_date"):
                extra.append(f"Last played: {g['last_played_date']}")
            if extra:
                lines.append(f"  {' | '.join(extra)}")
            lines.append("")

        if total > params.offset + len(rows):
            lines.append(f"\n*Showing {params.offset + 1}-{params.offset + len(rows)} of {total}. Use offset={params.offset + params.limit} to see more.*")

        return "\n".join(lines)

    except Exception as e:
        return f"Error querying library: {e}"



def _review_badge_variant(score: Optional[float]) -> str:
    if not score:       return "secondary"
    if score >= 90:     return "success"
    if score >= 75:     return "default"
    if score >= 60:     return "warning"
    return "destructive"


# -- Genre accent colors -------------------------------------------------------
# Pre-built Tailwind class strings (full strings required for JIT discovery).
_GENRE_CARD_CLASSES: Dict[str, str] = {
    "action":               "border-t-2 border-red-500/40",
    "indie":                "border-t-2 border-pink-500/40",
    "adventure":            "border-t-2 border-emerald-500/40",
    "rpg":                  "border-t-2 border-purple-500/40",
    "strategy":             "border-t-2 border-amber-500/40",
    "simulation":           "border-t-2 border-cyan-500/40",
    "casual":               "border-t-2 border-sky-400/40",
    "racing":               "border-t-2 border-orange-500/40",
    "sports":               "border-t-2 border-lime-500/40",
    "free to play":         "border-t-2 border-teal-400/40",
    "massively multiplayer":"border-t-2 border-violet-500/40",
    "early access":         "border-t-2 border-yellow-500/40",
    "education":            "border-t-2 border-blue-500/40",
    "sexual content":       "border-t-2 border-rose-400/40",
    "nudity":               "border-t-2 border-rose-400/40",
}

_GENRE_HERO_CLASSES: Dict[str, str] = {
    "action":               "border-t-4 border-red-500/60",
    "indie":                "border-t-4 border-pink-500/60",
    "adventure":            "border-t-4 border-emerald-500/60",
    "rpg":                  "border-t-4 border-purple-500/60",
    "strategy":             "border-t-4 border-amber-500/60",
    "simulation":           "border-t-4 border-cyan-500/60",
    "casual":               "border-t-4 border-sky-400/60",
    "racing":               "border-t-4 border-orange-500/60",
    "sports":               "border-t-4 border-lime-500/60",
    "free to play":         "border-t-4 border-teal-400/60",
    "massively multiplayer":"border-t-4 border-violet-500/60",
    "early access":         "border-t-4 border-yellow-500/60",
    "education":            "border-t-4 border-blue-500/60",
    "sexual content":       "border-t-4 border-rose-400/60",
    "nudity":               "border-t-4 border-rose-400/60",
}


def _genre_card_class(genre: str, hero: bool = False) -> str:
    """Return Tailwind border classes for a genre, for card or hero treatment."""
    key = (genre or "").lower().strip()
    mapping = _GENRE_HERO_CLASSES if hero else _GENRE_CARD_CLASSES
    return mapping.get(key, "border-t-2 border-slate-500/40" if not hero else "border-t-4 border-slate-500/60")


# -- Recommendations UI theme -------------------------------------------------
_RECO_THEME = Theme(
    mode="dark",
    font="Inter",
    dark_css="""
        --background: 220 16% 8%;
        --card: 220 16% 11%;
        --card-foreground: 210 20% 90%;
    """,
) if _HAS_PREFAB else None

_RECO_STYLESHEET = "body { background: linear-gradient(180deg, hsl(220 16% 6%) 0%, hsl(220 16% 12%) 100%); min-height: 100vh; }"


async def _fetch_image_data_url(app_id: int, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch a Steam header image and return it as a base64 data URL."""
    url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = base64.b64encode(resp.content).decode()
            return f"data:image/jpeg;base64,{data}"
    except Exception:
        pass
    return None


def _game_status_badge(g: dict) -> Optional[tuple]:
    """Return (label, variant) for a game's play status, or None."""
    status = g.get("status", "")
    pt = g.get("playtime_minutes") or 0
    pct = g.get("completion_pct") or 0
    if pt == 0:
        return ("🟢 Unplayed", "success")
    if pct >= 80:
        return ("🔥 Almost Done", "warning")
    if status == "in_progress" or pt > 0:
        return ("🔵 In Progress", "info")
    return None


def _build_game_grid(items, image_data_urls):
    """Render a 3-column grid of game recommendation cards."""
    with Grid(columns=3, gap=4):
        for i, (score, g, reasons) in enumerate(items, 1):
            pt     = g.get("playtime_minutes") or 0
            hltb   = g.get("hltb_main_hours")
            pct    = g.get("completion_pct") or 0
            rs     = g.get("review_score") or 0
            app_id = g.get("app_id")
            ach_unlocked = g.get("achievements_unlocked") or 0
            ach_total    = g.get("achievements_total") or 0
            genre        = g.get("primary_genre") or ""
            genre_border = _genre_card_class(genre)
            last_played  = _format_relative_date(g.get("last_played_date"))
            name         = g["name"]

            with Card(css_class=f"overflow-hidden {genre_border} transition-all duration-200 hover:scale-[1.02] hover:shadow-xl"):
                if app_id:
                    img_src = (image_data_urls or {}).get(app_id)
                    if img_src:
                        Image(src=img_src, alt=name, css_class="w-full object-cover", height="140px")

                with CardHeader():
                    CardTitle(content=f"{i}. {name}", css_class="text-lg font-bold")
                    CardDescription(content=g.get("developer") or genre or "")

                with CardContent():
                    # Review score progress bar with label
                    if rs:
                        Small(f"Review: {rs:.0f}%", css_class="text-muted-foreground mb-1")
                        Progress(value=int(rs), min=0, max=100, variant=_review_badge_variant(rs))

                    # Achievement Ring (if the game has achievements and has been played)
                    if pct > 0 and ach_total > 0:
                        with Row(gap=3, css_class="mt-3 items-center"):
                            Ring(
                                value=int(pct), min=0, max=100,
                                label=f"{pct:.0f}%",
                                size="sm",
                                variant=_review_badge_variant(pct),
                            )
                            Muted(f"{ach_unlocked}/{ach_total} achievements")

                    # Metadata badges — color-coded by type
                    with Row(gap=2, css_class="mt-2 flex-wrap"):
                        status_badge = _game_status_badge(g)
                        if status_badge:
                            Badge(label=status_badge[0], variant=status_badge[1])
                        deck = g.get("deck_status", "")
                        if deck == "verified":
                            Badge(label="🎮 Deck Verified", variant="success")
                        elif deck == "playable":
                            Badge(label="🎮 Deck Playable", variant="info")
                        if pt > 0:
                            Badge(label=f"🕹 {_format_hours(pt)} played", variant="secondary")
                        if hltb:
                            left = max(0, hltb - pt / 60) if pt > 0 else hltb
                            Badge(label=f"⏱ {left:.0f}h {'left' if pt > 0 else 'HLTB'}", variant="secondary")
                        if rs:
                            Badge(label=f"{rs:.0f}% positive", variant=_review_badge_variant(rs))
                        if genre:
                            Badge(label=genre, variant="outline")
                        if last_played:
                            Badge(label=f"📅 {last_played}", variant="outline")

                with CardFooter():
                    with Div(css_class="w-full space-y-2"):
                        with Div(css_class="border-l-2 border-blue-500/50 pl-3"):
                            Text(f"✨ {'; '.join(reasons[:2])}", css_class="text-sm italic text-muted-foreground")
                        Muted(f'💬 Try: "Tell me more about {name}" or "Plan a session for {name}"')


def _build_recommendations_app(
    top,
    device_label: str,
    mood: Optional[str],
    hours: Optional[float],
    image_data_urls: Optional[Dict[int, str]] = None,
    total_candidates: int = 0,
) -> "PrefabApp":
    # Summary stats
    avg_review = (sum(g.get("review_score") or 0 for _, g, _ in top) / len(top)) if top else 0
    total_est_hours = sum(
        max(0, (g.get("hltb_main_hours") or 0) - ((g.get("playtime_minutes") or 0) / 60))
        for _, g, _ in top
    )

    # Tab subsets
    almost_done = [(s, g, r) for s, g, r in top
                   if (g.get("completion_pct") or 0) >= 50 and (g.get("playtime_minutes") or 0) > 0]
    quick_plays = [(s, g, r) for s, g, r in top
                   if 0 < (g.get("hltb_main_hours") or 999) <= 5]

    with Column(gap=4) as view:
        # Header row
        with Row(gap=2, css_class="items-center justify-between flex-wrap"):
            Heading(f"🎮 Recommended for {device_label}")
            with Row(gap=2):
                if mood:
                    Badge(label=mood.title(), variant="secondary")
                if hours:
                    Badge(label=f"{hours}h session", variant="outline")

        # Interactivity hint
        with Alert():
            AlertTitle("These picks are interactive")
            AlertDescription(
                "Click any game and ask me about it — e.g. "
                "\"Tell me more about [game]\" for a deep dive, "
                "\"Plan a session for [game]\" for tonight, "
                "or \"What achievements am I close to finishing in [game]?\""
            )

        # Summary metrics
        with Grid(columns=4, gap=3):
            Metric(
                label="Picks",
                value=str(len(top)),
                description=f"of {total_candidates} analyzed" if total_candidates else None,
            )
            Metric(
                label="Avg Review",
                value=f"{avg_review:.0f}%",
                description="of top picks",
            )
            Metric(
                label="Est. Hours",
                value=f"{total_est_hours:.0f}h",
                description="to finish them all",
            )
            if top:
                _, best_game, best_reasons = top[0]
                best_name = best_game["name"]
                Metric(
                    label="Top Pick",
                    value=best_name[:20] + ("…" if len(best_name) > 20 else ""),
                    description=(best_reasons[0][:40] if best_reasons else None),
                )

        Separator()

        # Hero card for #1 pick
        if top:
            _, hero_g, hero_reasons = top[0]
            hero_pt    = hero_g.get("playtime_minutes") or 0
            hero_hltb  = hero_g.get("hltb_main_hours")
            hero_pct   = hero_g.get("completion_pct") or 0
            hero_rs    = hero_g.get("review_score") or 0
            hero_app   = hero_g.get("app_id")
            hero_ach_u = hero_g.get("achievements_unlocked") or 0
            hero_ach_t = hero_g.get("achievements_total") or 0
            hero_genre = hero_g.get("primary_genre") or ""
            hero_border = _genre_card_class(hero_genre, hero=True)
            hero_last_played = _format_relative_date(hero_g.get("last_played_date"))
            hero_name  = hero_g["name"]

            with Card(css_class=f"overflow-hidden {hero_border} shadow-lg"):
                if hero_app:
                    img_src = (image_data_urls or {}).get(hero_app)
                    if img_src:
                        Image(src=img_src, alt=hero_name, css_class="w-full object-cover", height="260px")
                with CardHeader():
                    CardTitle(content=f"🏆 {hero_name}", css_class="text-2xl font-extrabold")
                    CardDescription(
                        content=hero_g.get("developer") or hero_genre or "",
                        css_class="text-base",
                    )
                with CardContent():
                    if hero_rs:
                        Small(f"Review: {hero_rs:.0f}%", css_class="text-muted-foreground mb-1")
                        Progress(value=int(hero_rs), min=0, max=100, variant=_review_badge_variant(hero_rs))
                    if hero_pct > 0 and hero_ach_t > 0:
                        with Row(gap=3, css_class="mt-3 items-center"):
                            Ring(
                                value=int(hero_pct), min=0, max=100,
                                label=f"{hero_pct:.0f}%",
                                size="sm",
                                variant=_review_badge_variant(hero_pct),
                            )
                            Muted(f"{hero_ach_u}/{hero_ach_t} achievements")
                    with Row(gap=2, css_class="mt-3 flex-wrap"):
                        hero_status = _game_status_badge(hero_g)
                        if hero_status:
                            Badge(label=hero_status[0], variant=hero_status[1])
                        hero_deck = hero_g.get("deck_status", "")
                        if hero_deck == "verified":
                            Badge(label="🎮 Deck Verified", variant="success")
                        elif hero_deck == "playable":
                            Badge(label="🎮 Deck Playable", variant="info")
                        if hero_pt > 0:
                            Badge(label=f"🕹 {_format_hours(hero_pt)} played", variant="secondary")
                        if hero_hltb:
                            left = max(0, hero_hltb - hero_pt / 60) if hero_pt > 0 else hero_hltb
                            Badge(label=f"⏱ {left:.0f}h {'left' if hero_pt > 0 else 'HLTB'}", variant="secondary")
                        if hero_rs:
                            Badge(label=f"{hero_rs:.0f}% positive", variant=_review_badge_variant(hero_rs))
                        if hero_genre:
                            Badge(label=hero_genre, variant="outline")
                        if hero_last_played:
                            Badge(label=f"📅 {hero_last_played}", variant="outline")
                with CardFooter():
                    with Div(css_class="w-full space-y-2"):
                        with Div(css_class="border-l-2 border-blue-500/50 pl-3"):
                            Text(f"✨ {'; '.join(hero_reasons)}", css_class="text-sm italic text-muted-foreground")
                        Muted(f'💬 Try: "Tell me more about {hero_name}" or "Plan a session for {hero_name}"')

        # Tabbed game grid (remaining picks)
        with Tabs():
            with Tab(f"All Picks"):
                _build_game_grid(top, image_data_urls)
            if almost_done:
                with Tab(f"Almost Done ({len(almost_done)})"):
                    _build_game_grid(almost_done, image_data_urls)
            if quick_plays:
                with Tab(f"Quick Plays ({len(quick_plays)})"):
                    _build_game_grid(quick_plays, image_data_urls)

    return PrefabApp(
        view=view,
        theme=_RECO_THEME,
        stylesheets=[_RECO_STYLESHEET],
    )


@mcp.tool(
    name="steam_get_recommendations",
    app=True,
    annotations={
        "title": "Get Game Recommendations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False
    }
)
async def steam_get_recommendations(params: GetRecommendationsInput):
    """Get personalized game recommendations from the library.

    Analyzes the entire library considering: review scores, HLTB completion times,
    achievement progress, Deck compatibility, genre, and play history.
    Games with status='completed' are deprioritized (score penalty) rather than excluded,
    so they only surface if nothing better matches.

    Use this for questions like:
    - "What should I play on my Steam Deck tonight?"
    - "I have 2 hours, what can I knock out?"
    - "What RPGs should I focus on?"
    - "What cult classics am I sleeping on?"
    - "What games am I closest to finishing?"
    - "Recommend something based on my mood / genre / time"
    Ask clarifying questions (mood, device, time available, genre) if the user hasn't specified.

    For bespoke queries (hidden gems, specific filters, custom SQL), use steam_run_query
    to get app_ids, then pass them to steam_render_games for the Prefab UI.
    """
    try:
        # Build different recommendation pools
        # completed games are deprioritized in scoring rather than excluded
        conditions = ["status NOT IN ('abandoned', 'not_interested')"]
        query_params = []

        if params.device == DeviceEnum.STEAM_DECK:
            conditions.append("deck_status IN ('verified', 'playable')")
        elif params.device == DeviceEnum.LIVING_ROOM:
            conditions.append("deck_status IN ('verified', 'playable')")

        if not params.include_in_progress:
            conditions.append("playtime_minutes = 0")

        if params.genre:
            conditions.append("(LOWER(primary_genre) LIKE LOWER(?) OR LOWER(all_genres) LIKE LOWER(?))")
            query_params.extend([f"%{params.genre}%", f"%{params.genre}%"])

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        # Pull all candidates - no cap, score the entire library
        sql = f"""SELECT name, app_id, playtime_minutes, completion_pct, review_score,
                         review_count, review_desc, hltb_main_hours, hltb_extra_hours,
                         deck_status, primary_genre, all_genres, status, metacritic,
                         achievements_unlocked, achievements_total, last_played_date,
                         developer, hltb_completionist_hours
                  FROM games
                  {where}
                  AND review_score IS NOT NULL"""

        rows = await _query_turso(sql, query_params if query_params else None)

        if not rows:
            return "No games match your criteria. Try broadening your filters."

        # Score each game
        scored = []
        for g in rows:
            score = 0.0
            reasons = []

            # Review quality (0-30 points)
            rs = g.get("review_score") or 0
            rc = g.get("review_count") or 0
            review_pts = (rs / 100) * 20
            if rc > 10000:
                review_pts += 5
            elif rc > 1000:
                review_pts += 3
            if rs >= 95:
                review_pts += 5
                reasons.append(f"{g.get('review_desc', 'Highly rated')} ({rs:.0f}%)")
            elif rs >= 90:
                reasons.append(f"Very well reviewed ({rs:.0f}%)")
            score += review_pts

            # Metacritic bonus (0-10)
            mc = g.get("metacritic")
            if mc and mc >= 90:
                score += 10
                reasons.append(f"Metacritic {mc}")
            elif mc and mc >= 80:
                score += 5

            # Completion proximity bonus (0-25 points)  -  heavily reward close-to-done
            pct = g.get("completion_pct") or 0
            pt = g.get("playtime_minutes") or 0
            if pct >= 80 and pt > 0:
                score += 25
                remaining = (g.get("achievements_total") or 0) - (g.get("achievements_unlocked") or 0)
                reasons.append(f"Almost done! {pct:.0f}% complete, {remaining} achievements left")
            elif pct >= 60 and pt > 0:
                score += 15
                reasons.append(f"Well into it at {pct:.0f}%")
            elif pct >= 30 and pt > 0:
                score += 5

            # Time fit bonus (0-15 points)
            hltb = g.get("hltb_main_hours")
            if params.available_hours and hltb:
                remaining_est = hltb - (pt / 60) if pt > 0 else hltb
                if remaining_est <= 0:
                    remaining_est = 1
                if remaining_est <= params.available_hours:
                    score += 15
                    reasons.append(f"Fits your time (~{remaining_est:.0f}h remaining, HLTB: {hltb:.0f}h)")
                elif remaining_est <= params.available_hours * 1.5:
                    score += 8
                    reasons.append(f"Close to fitting ({remaining_est:.0f}h remaining)")
            elif hltb and hltb <= 15:
                score += 5
                reasons.append(f"Approachable length ({hltb:.0f}h)")

            # Mood matching (0-10 points)
            if params.mood:
                mood_lower = params.mood.lower()
                genres = (g.get("all_genres") or "").lower()
                genre_primary = (g.get("primary_genre") or "").lower()

                mood_genre_map = {
                    "relaxing": ["casual", "simulation", "puzzle", "indie"],
                    "intense": ["action", "shooter", "fighting", "horror"],
                    "story": ["adventure", "rpg", "visual novel"],
                    "story-rich": ["adventure", "rpg", "visual novel"],
                    "quick": [],  # handled by time
                    "classic": [],  # handled by review age
                    "cult": [],
                    "indie": ["indie"],
                    "new": [],
                }

                matching_genres = mood_genre_map.get(mood_lower, [])
                if matching_genres:
                    for mg in matching_genres:
                        if mg in genres:
                            score += 10
                            break

                if "quick" in mood_lower and hltb and hltb <= 5:
                    score += 15
                    reasons.append("Quick play!")

                if ("classic" in mood_lower or "cult" in mood_lower) and rs and rs >= 95 and rc and rc > 5000:
                    score += 15
                    reasons.append("Certified classic")

            # Unplayed bonus  -  prioritize the untouched
            if pt == 0:
                score += 3
                reasons.append("Unplayed  -  fresh experience")

            # Deprioritize games the user has already completed (story done)
            # These sink to the bottom but can still surface if explicitly relevant
            game_status = g.get("status") or ""
            if game_status == "completed":
                score -= 100
                reasons.append("Story completed  -  deprioritized")

            if not reasons:
                reasons.append(f"{g.get('primary_genre', 'Game')}  -  {g.get('review_desc', '')}")

            scored.append((score, g, reasons))

        # Sort by score, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        total_candidates = len(scored)
        top = scored[:params.count]

        device_label = {
            DeviceEnum.STEAM_DECK: "Steam Deck",
            DeviceEnum.LIVING_ROOM: "Living Room PC",
            DeviceEnum.OFFICE: "Office PC",
            DeviceEnum.ANY: "any device",
        }.get(params.device, "any device")

        # Build text fallback for LLM context
        lines = [f"## Recommended Games for {device_label}\n"]
        if params.mood:
            lines.append(f"*Mood: {params.mood}*\n")
        if params.available_hours:
            lines.append(f"*Available time: {params.available_hours}h*\n")

        for i, (score, g, reasons) in enumerate(top, 1):
            pt = g.get("playtime_minutes") or 0
            hltb = g.get("hltb_main_hours")
            remaining = f"~{max(0, hltb - pt/60):.0f}h remaining" if hltb and pt > 0 else (f"~{hltb:.0f}h to beat" if hltb else "")

            lines.append(f"### {i}. {g['name']}")
            lines.append(f"**{_format_hours(pt)} played** | "
                         f"**{g.get('completion_pct', 0):.0f}%** achievements | "
                         f"**{g.get('review_desc', '')}** | "
                         f"Deck: {g.get('deck_status')} | "
                         f"{g.get('primary_genre', '')}")
            if remaining:
                lines.append(f"*{remaining}*")
            if g.get("developer"):
                lines.append(f"*by {g['developer']}*")
            lines.append(f"**Why:** {'; '.join(reasons)}")
            lines.append("")

        fallback_text = "\n".join(lines)

        if _HAS_PREFAB:
            try:
                app_ids = [g.get("app_id") for _, g, _ in top if g.get("app_id")]
                async with httpx.AsyncClient() as client:
                    results = await asyncio.gather(
                        *[_fetch_image_data_url(aid, client) for aid in app_ids],
                        return_exceptions=True,
                    )
                image_data_urls = {
                    aid: url
                    for aid, url in zip(app_ids, results)
                    if isinstance(url, str)
                }
                return _build_recommendations_app(top, device_label, params.mood, params.available_hours, image_data_urls, total_candidates=total_candidates)
            except Exception:
                pass

        return fallback_text

    except Exception as e:
        import traceback as _tb
        err_detail = _tb.format_exc()
        return f"Error generating recommendations: {type(e).__name__}: {e}\n\n{err_detail}"


@mcp.tool(
    name="steam_render_games",
    app=True,
    annotations={
        "title": "Render Games in Prefab UI",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_render_games(params: RenderGamesInput):
    """Render a custom list of games in the Prefab card UI.

    Use this after steam_run_query when the user wants a visual presentation
    of a bespoke game selection (e.g. hidden gems, short games, a specific genre).

    Workflow:
    1. Run steam_run_query with any custom SQL to get app_ids
    2. Pass those app_ids to this tool
    3. This tool renders them in the full Prefab card UI

    Do NOT call steam_get_recommendations first — that renders its own query.
    Just call this tool directly with your app_id list.
    """
    try:
        placeholders = ",".join("?" * len(params.app_ids))
        sql = f"""SELECT name, app_id, playtime_minutes, completion_pct, review_score,
                         review_count, review_desc, hltb_main_hours, hltb_extra_hours,
                         deck_status, primary_genre, all_genres, status, metacritic,
                         achievements_unlocked, achievements_total, last_played_date,
                         developer, hltb_completionist_hours
                  FROM games
                  WHERE app_id IN ({placeholders})"""
        rows = await _query_turso(sql, params.app_ids)

        if not rows:
            return "No games found for the provided app_ids."

        # Preserve the caller's ordering
        order = {aid: i for i, aid in enumerate(params.app_ids)}
        rows.sort(key=lambda g: order.get(g.get("app_id"), 999))

        # Wrap as (score, game, reasons) tuples matching _build_recommendations_app signature
        top = [(0.0, g, [g.get("primary_genre", ""), g.get("review_desc", "")]) for g in rows]

        if _HAS_PREFAB:
            try:
                app_ids = [g.get("app_id") for g in rows if g.get("app_id")]
                async with httpx.AsyncClient() as client:
                    results = await asyncio.gather(
                        *[_fetch_image_data_url(aid, client) for aid in app_ids],
                        return_exceptions=True,
                    )
                image_data_urls = {
                    aid: url
                    for aid, url in zip(app_ids, results)
                    if isinstance(url, str)
                }
                return _build_recommendations_app(top, params.label, None, None, image_data_urls, total_candidates=len(rows))
            except Exception:
                pass

        # Fallback text
        lines = [f"## {params.label}\n"]
        for g in rows:
            pt = g.get("playtime_minutes") or 0
            lines.append(f"- **{g['name']}** — {g.get('review_desc', '')} | {g.get('primary_genre', '')} | {_format_hours(pt)} played")
        return "\n".join(lines)

    except Exception as e:
        import traceback as _tb
        return f"Error rendering games: {type(e).__name__}: {e}\n\n{_tb.format_exc()}"


@mcp.tool(
    name="steam_debug",
    annotations={"title": "Debug Steam MCP Server", "readOnlyHint": True}
)
async def steam_debug() -> str:
    """Diagnostic tool  -  reports package versions, _HAS_PREFAB, and tests the Prefab pipeline.

    Call this if steam_get_recommendations returns a 500 error.
    Returns a plain-text report of what's working and what isn't.
    """
    import sys
    lines = [f"Python: {sys.version}"]
    lines.append(f"_HAS_PREFAB: {_HAS_PREFAB}")

    # Package versions
    try:
        import importlib.metadata as _imeta
        for pkg in ["fastmcp", "prefab-ui", "libsql-client", "pydantic", "howlongtobeatpy"]:
            try:
                lines.append(f"  {pkg}: {_imeta.version(pkg)}")
            except Exception:
                lines.append(f"  {pkg}: NOT FOUND")
    except Exception as e:
        lines.append(f"  version check failed: {e}")

    # Prefab pipeline smoke-test
    if _HAS_PREFAB:
        try:
            dummy_top = [(80.0, {
                "name": "Test Game", "app_id": 12345, "playtime_minutes": 60,
                "completion_pct": 50.0, "review_score": 90.0, "hltb_main_hours": 10.0,
                "deck_status": "verified", "primary_genre": "Action",
                "developer": "Test Dev", "review_desc": "Very Positive",
            }, ["High score", "Deck verified"])]
            app = _build_recommendations_app(dummy_top, "Steam Deck", None, None)
            j = app.to_json()
            import json as _json
            lines.append(f"Prefab pipeline: OK ({len(_json.dumps(j))} bytes)")
        except Exception as e:
            import traceback as _tb
            lines.append(f"Prefab pipeline FAILED: {type(e).__name__}: {e}")
            lines.append(_tb.format_exc())
    else:
        lines.append("Prefab pipeline: SKIPPED (_HAS_PREFAB=False)")
        try:
            from prefab_ui.app import PrefabApp as _PA
            lines.append("  (but prefab_ui.app imports fine now  -  stale flag?)")
        except Exception as e2:
            lines.append(f"  prefab_ui import error: {e2}")

    # DB connectivity
    try:
        rows = await _query_turso("SELECT COUNT(*) as cnt FROM games")
        lines.append(f"DB connection: OK ({rows[0]['cnt'] if rows else '?'} games)")
    except Exception as e:
        lines.append(f"DB connection FAILED: {type(e).__name__}: {e}")

    return "\n".join(lines)


def _build_stats_overview_app(s: dict, ach: Optional[dict]) -> "PrefabApp":
    """Build a Dashboard UI for the library overview stats."""
    total       = s["total"] or 1
    played      = s["played"] or 0
    unplayed    = s["unplayed"] or 0
    completed   = s["completed"] or 0
    abandoned   = s["abandoned"] or 0
    perfect     = s["perfect_games"] or 0
    total_hrs   = (s["total_playtime"] or 0) / 60
    avg_review  = s["avg_review"] or 0
    played_pct  = played / total * 100

    ach_unlocked = (ach.get("unlocked") or 0) if ach else 0
    ach_total    = (ach.get("total") or 1) if ach else 1
    ach_pct      = ach_unlocked / ach_total * 100

    with Dashboard(columns=4, row_height=110, gap=4) as dash:
        # Row 1: heading
        with DashboardItem(col=1, row=1, col_span=4):
            Heading("📊 Steam Library Overview")

        # Row 2: four key metrics
        with DashboardItem(col=1, row=2):
            Metric(label="Total Games", value=f"{total:,}")
        with DashboardItem(col=2, row=2):
            Metric(label="Played", value=f"{played_pct:.0f}%", description=f"{played:,} of {total:,} games")
        with DashboardItem(col=3, row=2):
            Metric(label="Total Playtime", value=f"{total_hrs:,.0f}h", description=f"{total_hrs/24:.0f} days")
        with DashboardItem(col=4, row=2):
            Metric(label="Avg Review", value=f"{avg_review:.1f}%", description="across library")

        # Row 3–4: achievement ring (tall) + smaller metric tiles
        with DashboardItem(col=1, row=3, col_span=2, row_span=2):
            with Card():
                with CardHeader():
                    CardTitle("Achievement Progress")
                with CardContent():
                    with Row(gap=4, css_class="items-center"):
                        Ring(
                            value=int(ach_pct), min=0, max=100,
                            label=f"{ach_pct:.1f}%",
                            size="lg",
                            variant="success" if ach_pct >= 80 else ("warning" if ach_pct >= 50 else "default"),
                        )
                        with Column(gap=1):
                            Metric(label="Unlocked", value=f"{ach_unlocked:,}", description=f"of {ach_total:,} total")

        with DashboardItem(col=3, row=3):
            Metric(label="100% Perfect", value=str(perfect), description="fully completed")
        with DashboardItem(col=4, row=3):
            Metric(label="Marked Done", value=str(completed))
        with DashboardItem(col=3, row=4):
            Metric(label="Unplayed", value=f"{unplayed:,}", description=f"{unplayed/total*100:.0f}% of library")
        with DashboardItem(col=4, row=4):
            Metric(label="Abandoned", value=str(abandoned))

    return PrefabApp(view=dash)


def _build_game_detail_app(g: dict, achs: list, img_src: Optional[str] = None) -> "PrefabApp":
    """Build a card-based UI for a single game's detail view."""
    pt_hours = (g.get("playtime_minutes") or 0) / 60
    pct      = g.get("completion_pct") or 0
    rs       = g.get("review_score") or 0
    hltb     = g.get("hltb_main_hours")
    ach_unlocked = g.get("achievements_unlocked") or 0
    ach_total    = g.get("achievements_total") or 0
    deck     = (g.get("deck_status") or "unknown").title()

    with Column(gap=4) as view:
        # Hero card
        with Card(css_class="overflow-hidden"):
            if img_src:
                Image(src=img_src, alt=g["name"], css_class="w-full object-cover", height="200px")
            with CardHeader():
                CardTitle(content=g["name"])
                with Row(gap=2, css_class="flex-wrap mt-1"):
                    if g.get("developer"):
                        Badge(label=g["developer"], variant="secondary")
                    if g.get("primary_genre"):
                        Badge(label=g["primary_genre"], variant="outline")
                    Badge(label=f"🎮 Deck: {deck}", variant="outline")
                    if g.get("status"):
                        Badge(label=g["status"].replace("_", " ").title(), variant="outline")

        # Key metrics row
        with Grid(columns=4, gap=3):
            Metric(label="Playtime", value=f"{pt_hours:.1f}h",
                   description=g.get("last_played_date") and f"Last: {g['last_played_date']}")
            Metric(label="Steam Review", value=f"{rs:.0f}%", description=g.get("review_desc"))
            if hltb:
                remaining = max(0.0, hltb - pt_hours)
                Metric(label="Remaining", value=f"~{remaining:.0f}h",
                       description=f"HLTB: {hltb:.0f}h main")
            if g.get("metacritic"):
                Metric(label="Metacritic", value=str(g["metacritic"]))

        # Achievement card (only when the game has them)
        if ach_total > 0:
            with Card():
                with CardHeader():
                    CardTitle("Achievements")
                with CardContent():
                    with Row(gap=4, css_class="items-center"):
                        Ring(
                            value=int(pct), min=0, max=100,
                            label=f"{pct:.0f}%",
                            size="lg",
                            variant=_review_badge_variant(pct),
                        )
                        with Column(gap=2):
                            Metric(label="Progress",
                                   value=f"{ach_unlocked}/{ach_total}",
                                   description=f"{pct:.1f}% complete")
                            # Easiest remaining achievements
                            locked = [a for a in achs if not a.get("unlocked")]
                            if locked:
                                easiest = sorted(locked, key=lambda a: a.get("global_pct") or 0, reverse=True)[:3]
                                for a in easiest:
                                    Muted(f"• {a.get('display_name','?')} — {a.get('global_pct',0):.1f}% of players")

    return PrefabApp(view=view)


@mcp.tool(
    name="steam_get_game_detail",
    app=True,
    annotations={
        "title": "Get Game Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_get_game_detail(params: GetGameDetailInput):
    """Get detailed information about a specific game including full achievement list.

    Look up any game by name (partial match supported). Returns playtime, achievements
    with unlock status and rarity, review scores, HLTB times, Deck status, and more.
    """
    try:
        games = await _query_turso(
            """SELECT * FROM games WHERE LOWER(name) LIKE LOWER(?) ORDER BY
               CASE WHEN LOWER(name) = LOWER(?) THEN 0 ELSE 1 END,
               playtime_minutes DESC LIMIT 1""",
            [f"%{params.game_name}%", params.game_name]
        )

        if not games:
            return f"No game found matching '{params.game_name}'. Try a shorter search term."

        g = games[0]

        # Fetch achievements if applicable
        achs = []
        if g.get("achievements_total") and g["achievements_total"] > 0:
            achs = await _query_turso(
                """SELECT display_name, description, unlocked, unlock_time, global_pct
                   FROM achievements WHERE app_id = ?
                   ORDER BY unlocked DESC, global_pct DESC""",
                [g["app_id"]]
            )

        # Build text fallback for LLM context
        lines = [f"# {g['name']}\n"]
        lines.append(f"**App ID:** {g['app_id']}")
        lines.append(f"**Developer:** {g.get('developer', 'Unknown')} | **Publisher:** {g.get('publisher', 'Unknown')}")
        lines.append(f"**Genre:** {g.get('all_genres', g.get('primary_genre', 'Unknown'))}")
        lines.append(f"**Release Date:** {g.get('release_date', 'Unknown')}")
        lines.append(f"**Status:** {g.get('status', 'Unknown')}")
        lines.append("")
        lines.append("## Play Stats")
        lines.append(f"- **Playtime:** {_format_hours(g.get('playtime_minutes'))}")
        if g.get("last_played_date"):
            lines.append(f"- **Last Played:** {g['last_played_date']}")
        lines.append(f"- **Achievements:** {g.get('achievements_unlocked', 0)}/{g.get('achievements_total', 0)} ({g.get('completion_pct', 0):.1f}%)")
        lines.append("")
        lines.append("## Reviews & Ratings")
        lines.append(f"- **Steam Reviews:** {g.get('review_desc', 'N/A')} ({g.get('review_score', 0):.0f}% from {g.get('review_count', 0):,} reviews)")
        if g.get("metacritic"):
            lines.append(f"- **Metacritic:** {g['metacritic']}")
        lines.append("")
        lines.append("## How Long to Beat")
        lines.append(f"- **Main Story:** {g.get('hltb_main_hours', '?')}h")
        lines.append(f"- **Main + Extras:** {g.get('hltb_extra_hours', '?')}h")
        lines.append(f"- **Completionist:** {g.get('hltb_completionist_hours', '?')}h")
        if g.get("playtime_minutes") and g.get("hltb_main_hours"):
            remaining = g["hltb_main_hours"] - (g["playtime_minutes"] / 60)
            if remaining > 0:
                lines.append(f"- **Estimated Remaining:** ~{remaining:.0f}h (main story)")
        lines.append("")
        lines.append(f"## Steam Deck: **{g.get('deck_status', 'unknown').upper()}**\n")
        if achs:
            unlocked_list = [a for a in achs if a.get("unlocked")]
            locked_list   = [a for a in achs if not a.get("unlocked")]
            lines.append(f"## Achievements ({len(unlocked_list)}/{len(achs)} unlocked)\n")
            if locked_list:
                lines.append("### Easiest Remaining:")
                for a in sorted(locked_list, key=lambda x: x.get("global_pct", 0), reverse=True)[:10]:
                    desc = f"  -  {a['description']}" if a.get("description") else ""
                    lines.append(f"- {a.get('display_name', a.get('api_name', '?'))}{desc} ({a.get('global_pct', 0):.1f}% of players)")
                lines.append("")
        if g.get("user_notes"):
            lines.append(f"## Notes\n{g['user_notes']}\n")
        fallback_text = "\n".join(lines)

        # Prefab UI
        if _HAS_PREFAB:
            try:
                img_src = None
                if g.get("app_id"):
                    async with httpx.AsyncClient() as client:
                        img_src = await _fetch_image_data_url(g["app_id"], client)
                return _build_game_detail_app(g, achs, img_src)
            except Exception:
                pass

        return fallback_text

    except Exception as e:
        return f"Error getting game details: {e}"


@mcp.tool(
    name="steam_get_stats",
    app=True,
    annotations={
        "title": "Get Library Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_get_stats(params: GetStatsInput) -> str:
    """Get statistics about the Steam library.

    Categories: overview, genres, completion, deck, backlog, playtime, recent.
    Analyzes all 908 games for accurate aggregate stats.
    """
    try:
        cat = (params.category or "overview").lower()

        if cat == "overview":
            rows = await _query_turso("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN playtime_minutes > 0 THEN 1 ELSE 0 END) as played,
                    SUM(CASE WHEN playtime_minutes = 0 THEN 1 ELSE 0 END) as unplayed,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned,
                    SUM(playtime_minutes) as total_playtime,
                    AVG(CASE WHEN review_score IS NOT NULL THEN review_score END) as avg_review,
                    SUM(CASE WHEN completion_pct >= 100 AND achievements_total > 0 THEN 1 ELSE 0 END) as perfect_games
                FROM games
            """)
            s = rows[0]
            total_hrs = (s["total_playtime"] or 0) / 60

            # Achievement stats
            ach_rows = await _query_turso("SELECT SUM(achievements_unlocked) as unlocked, SUM(achievements_total) as total FROM games WHERE achievements_total > 0")
            ach = ach_rows[0] if ach_rows else None

            # Text fallback
            lines = [
                "# Steam Library Overview\n",
                f"- **Total Games:** {s['total']}",
                f"- **Played:** {s['played']} ({s['played']/s['total']*100:.0f}%)",
                f"- **Unplayed:** {s['unplayed']} ({s['unplayed']/s['total']*100:.0f}%)",
                f"- **Completed (100% achievements):** {s['perfect_games']}",
                f"- **Marked Completed:** {s['completed']}",
                f"- **Abandoned:** {s['abandoned']}",
                f"- **Total Playtime:** {total_hrs:,.0f} hours ({total_hrs/24:.0f} days)",
                f"- **Average Review Score:** {s['avg_review']:.1f}%",
            ]
            if ach:
                lines.append(f"- **Achievements:** {ach['unlocked']:,}/{ach['total']:,} ({ach['unlocked']/ach['total']*100:.1f}%)")

            if _HAS_PREFAB:
                try:
                    return _build_stats_overview_app(s, ach)
                except Exception:
                    pass

            return "\n".join(lines)

        elif cat == "genres":
            rows = await _query_turso("""
                SELECT primary_genre, COUNT(*) as count,
                       SUM(CASE WHEN playtime_minutes > 0 THEN 1 ELSE 0 END) as played,
                       AVG(review_score) as avg_review,
                       SUM(playtime_minutes)/60.0 as total_hours
                FROM games
                WHERE primary_genre IS NOT NULL AND primary_genre != 'Unknown'
                GROUP BY primary_genre
                ORDER BY count DESC
            """)
            lines = ["# Genre Breakdown\n"]
            for r in rows:
                lines.append(f"- **{r['primary_genre']}:** {r['count']} games | "
                             f"{r['played']} played | {r['total_hours']:.0f}h total | "
                             f"Avg review: {r['avg_review']:.0f}%")
            return "\n".join(lines)

        elif cat == "completion":
            rows = await _query_turso("""
                SELECT name, achievements_unlocked, achievements_total, completion_pct,
                       playtime_minutes/60.0 as hours
                FROM games
                WHERE achievements_total > 0 AND playtime_minutes > 0
                ORDER BY completion_pct DESC
                LIMIT 25
            """)
            lines = ["# Achievement Completion Leaderboard\n"]
            for r in rows:
                remaining = r["achievements_total"] - r["achievements_unlocked"]
                lines.append(f"- **{r['name']}:** {r['achievements_unlocked']}/{r['achievements_total']} "
                             f"({r['completion_pct']:.0f}%)  -  {remaining} left  -  {r['hours']:.1f}h played")
            return "\n".join(lines)

        elif cat == "deck":
            rows = await _query_turso("""
                SELECT deck_status, COUNT(*) as count,
                       SUM(CASE WHEN playtime_minutes = 0 THEN 1 ELSE 0 END) as unplayed
                FROM games
                GROUP BY deck_status
                ORDER BY count DESC
            """)
            lines = ["# Steam Deck Compatibility\n"]
            for r in rows:
                lines.append(f"- **{r['deck_status'].title()}:** {r['count']} games ({r['unplayed']} unplayed)")

            # Top unplayed Deck verified
            top_deck = await _query_turso("""
                SELECT name, review_score, hltb_main_hours, primary_genre
                FROM games WHERE deck_status = 'verified' AND playtime_minutes = 0
                AND review_score > 85 ORDER BY review_score DESC LIMIT 10
            """)
            if top_deck:
                lines.append("\n### Top Unplayed Deck-Verified Games:")
                for g in top_deck:
                    hltb = f"{g['hltb_main_hours']:.0f}h" if g.get("hltb_main_hours") else "?"
                    lines.append(f"- **{g['name']}**  -  {g['review_score']:.0f}% | HLTB: {hltb} | {g['primary_genre']}")

            return "\n".join(lines)

        elif cat == "backlog":
            # Backlog analysis
            rows = await _query_turso("""
                SELECT
                    SUM(CASE WHEN playtime_minutes = 0 AND hltb_main_hours IS NOT NULL THEN hltb_main_hours ELSE 0 END) as total_backlog_hours,
                    COUNT(CASE WHEN playtime_minutes = 0 THEN 1 END) as unplayed_count,
                    AVG(CASE WHEN playtime_minutes = 0 AND hltb_main_hours IS NOT NULL THEN hltb_main_hours END) as avg_hltb
                FROM games
            """)
            s = rows[0]

            lines = [
                "# Backlog Analysis\n",
                f"- **Unplayed Games:** {s['unplayed_count']}",
                f"- **Total Backlog (main story):** ~{s['total_backlog_hours']:,.0f} hours ({s['total_backlog_hours']/24:.0f} days)",
                f"- **Average Game Length:** ~{s['avg_hltb']:.0f} hours",
                f"- **At 2h/day:** ~{s['total_backlog_hours']/2/365:.1f} years to clear",
                f"- **At 4h/day:** ~{s['total_backlog_hours']/4/365:.1f} years to clear",
            ]

            # Quick wins
            quick = await _query_turso("""
                SELECT name, hltb_main_hours, review_score, deck_status
                FROM games WHERE playtime_minutes = 0 AND hltb_main_hours <= 5
                AND review_score > 85 ORDER BY review_score DESC LIMIT 10
            """)
            if quick:
                lines.append("\n### Quick Wins (under 5h, 85%+ reviews):")
                for g in quick:
                    lines.append(f"- **{g['name']}**  -  {g['hltb_main_hours']:.1f}h | {g['review_score']:.0f}% | Deck: {g['deck_status']}")

            return "\n".join(lines)

        elif cat == "playtime":
            rows = await _query_turso("""
                SELECT name, playtime_minutes/60.0 as hours, completion_pct, review_desc
                FROM games WHERE playtime_minutes > 0
                ORDER BY playtime_minutes DESC LIMIT 20
            """)
            lines = ["# Most Played Games\n"]
            for r in rows:
                lines.append(f"- **{r['name']}:** {r['hours']:.1f}h | {r['completion_pct']:.0f}% | {r['review_desc']}")
            return "\n".join(lines)

        elif cat == "recent":
            rows = await _query_turso("""
                SELECT name, last_played_date, playtime_minutes/60.0 as hours,
                       completion_pct, primary_genre
                FROM games WHERE last_played_date IS NOT NULL
                ORDER BY last_played DESC LIMIT 15
            """)
            lines = ["# Recently Played\n"]
            for r in rows:
                lines.append(f"- **{r['name']}**  -  {r['last_played_date']} | "
                             f"{r['hours']:.1f}h | {r['completion_pct']:.0f}% | {r['primary_genre']}")
            return "\n".join(lines)

        else:
            return f"Unknown stats category: '{cat}'. Available: overview, genres, completion, deck, backlog, playtime, recent"

    except Exception as e:
        return f"Error getting stats: {e}"


@mcp.tool(
    name="steam_update_game_status",
    annotations={
        "title": "Update Game Status",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_update_game_status(params: UpdateGameStatusInput) -> str:
    """Update a game's status (completed, abandoned, not_interested, in_progress, unplayed).

    Use this to mark games as completed when achievements don't reflect it,
    remove games from the backlog, or track progress manually.
    """
    try:
        # Find the game
        games = await _query_turso(
            "SELECT app_id, name, status FROM games WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
            [f"%{params.game_name}%"]
        )

        if not games:
            return f"No game found matching '{params.game_name}'."

        game = games[0]
        old_status = game["status"]

        # Update status
        await _execute_turso(
            "UPDATE games SET status = ?, user_notes = COALESCE(?, user_notes), updated_at = ? WHERE app_id = ?",
            [params.status.value, params.notes, datetime.now().isoformat(), game["app_id"]]
        )

        # Log the override
        await _execute_turso(
            "INSERT INTO user_overrides (app_id, field, value, reason) VALUES (?, 'status', ?, ?)",
            [game["app_id"], params.status.value, params.notes or f"Changed from {old_status}"]
        )

        return f"Updated **{game['name']}** status: {old_status} → **{params.status.value}**" + \
               (f"\nNotes: {params.notes}" if params.notes else "")

    except Exception as e:
        return f"Error updating game status: {e}"


@mcp.tool(
    name="steam_run_query",
    annotations={
        "title": "Run Custom SQL Query",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_run_query(params: RunSQLInput) -> str:
    """Run a custom read-only SQL query against the Steam library database.

    Tables available:
    - games: app_id, name, playtime_minutes, last_played, last_played_date,
             achievements_total, achievements_unlocked, completion_pct,
             hltb_main_hours, hltb_extra_hours, hltb_completionist_hours,
             deck_status, review_score, review_count, review_desc, metacritic,
             primary_genre, all_genres, developer, publisher, release_date,
             status, user_notes
    - achievements: app_id, api_name, display_name, description, unlocked,
                    unlock_time, global_pct
    - devices: device_id, device_name, input_type, context
    - user_overrides: app_id, field, value, reason, created_at
    - sync_log: sync_time, sync_type, games_added, games_updated, status

    Only SELECT queries are allowed.
    """
    try:
        rows = await _query_turso(params.sql)

        if not rows:
            return "Query returned no results."

        # Format as markdown table if reasonable size
        if len(rows) <= 50 and len(rows[0]) <= 8:
            columns = list(rows[0].keys())
            lines = ["| " + " | ".join(columns) + " |"]
            lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
            for row in rows:
                vals = [str(row.get(c, "")) for c in columns]
                lines.append("| " + " | ".join(vals) + " |")
            return "\n".join(lines)
        else:
            # JSON for larger results
            return json.dumps(rows[:100], indent=2, default=str)

    except Exception as e:
        return f"Error running query: {e}"


# =============================================================================
# Steam API Helpers (for sync operations)
# =============================================================================

STEAM_API_BASE = "http://api.steampowered.com"
STEAM_STORE_BASE = "https://store.steampowered.com"


async def _steam_api_get(url: str, params: dict) -> Optional[dict]:
    """Hit a Steam API endpoint with error handling."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return None


async def _fetch_player_achievements(app_id: int) -> tuple:
    """Fetch achievements for a game. Returns (ach_list, unlocked, total)."""
    # Player achievements
    data = await _steam_api_get(
        f"{STEAM_API_BASE}/ISteamUserStats/GetPlayerAchievements/v0001/",
        {"appid": app_id, "key": STEAM_API_KEY, "steamid": STEAM_ID}
    )
    if not data or "playerstats" not in data:
        return [], 0, 0
    ps = data["playerstats"]
    if not ps.get("success") or "achievements" not in ps:
        return [], 0, 0

    raw_achs = ps["achievements"]

    # Global percentages
    gdata = await _steam_api_get(
        f"{STEAM_API_BASE}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/",
        {"gameid": app_id}
    )
    gpcts = {}
    if gdata and "achievementpercentages" in gdata:
        for a in gdata["achievementpercentages"].get("achievements", []):
            gpcts[a["name"]] = a["percent"]

    # Schema for display names
    sdata = await _steam_api_get(
        f"{STEAM_API_BASE}/ISteamUserStats/GetSchemaForGame/v2/",
        {"key": STEAM_API_KEY, "appid": app_id}
    )
    smap = {}
    if sdata and "game" in sdata:
        for a in sdata["game"].get("availableGameStats", {}).get("achievements", []):
            smap[a["name"]] = {"display_name": a.get("displayName", a["name"]),
                               "description": a.get("description", "")}

    result = []
    unlocked = 0
    for a in raw_achs:
        api_name = a["apiname"]
        is_unlocked = a.get("achieved", 0) == 1
        if is_unlocked:
            unlocked += 1
        s = smap.get(api_name, {})
        result.append({
            "api_name": api_name,
            "display_name": s.get("display_name", api_name),
            "description": s.get("description", ""),
            "unlocked": is_unlocked,
            "unlock_time": a.get("unlocktime", 0) if is_unlocked else None,
            "global_pct": gpcts.get(api_name, 0)
        })

    return result, unlocked, len(raw_achs)


async def _fetch_store_data(app_id: int) -> Optional[dict]:
    """Fetch Steam Store metadata for a game."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{STEAM_STORE_BASE}/api/appdetails",
                                    params={"appids": app_id})
            if resp.status_code == 200:
                data = resp.json()
                key = str(app_id)
                if key in data and data[key].get("success"):
                    return data[key]["data"]
        except Exception:
            pass
    return None


async def _fetch_reviews(app_id: int) -> tuple:
    """Fetch review score, count, description for a game."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{STEAM_STORE_BASE}/appreviews/{app_id}",
                params={"json": 1, "language": "all", "purchase_type": "all"}
            )
            if resp.status_code == 200:
                data = resp.json()
                qs = data.get("query_summary", {})
                total = qs.get("total_reviews", 0)
                pos = qs.get("total_positive", 0)
                desc = qs.get("review_score_desc", "")
                score = (pos / total * 100) if total > 0 else None
                return score, total, desc
        except Exception:
            pass
    return None, None, None


async def _fetch_deck_status(app_id: int) -> str:
    """Fetch Steam Deck compatibility status."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{STEAM_STORE_BASE}/saleaction/ajaxgetdeckappcompatibilityreport",
                params={"nAppID": app_id}
            )
            if resp.status_code == 200:
                data = resp.json()
                cat = data.get("results", {}).get("resolved_category", 0)
                return {1: "unsupported", 2: "playable", 3: "verified"}.get(cat, "unknown")
        except Exception:
            pass
    return "unknown"


async def _fetch_hltb(game_name: str) -> tuple:
    """Fetch HowLongToBeat times. Returns (main, extra, completionist)."""
    try:
        from howlongtobeatpy import HowLongToBeat
        import re
        clean = re.sub(r"[™®©:]", "", game_name)
        clean = re.sub(r"\s*(edition|goty|deluxe|remastered|definitive|complete|collection).*$",
                       "", clean, flags=re.IGNORECASE).strip()
        results = await HowLongToBeat().async_search(clean)
        if results:
            best = max(results, key=lambda r: r.similarity)
            if best.similarity > 0.3:
                return (best.main_story or None,
                        best.main_extra or None,
                        best.completionist or None)
    except Exception:
        pass
    return None, None, None


async def _batch_execute_turso(statements: list) -> None:
    """Execute multiple write statements in a single Turso batch."""
    async with libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN) as client:
        await client.batch(statements)


# =============================================================================
# Sync Tools
# =============================================================================

class SyncRefreshInput(BaseModel):
    """Input for bi-weekly metadata refresh."""
    offset: int = Field(default=0, description="Start from this game index (for batched processing)", ge=0)
    batch_size: int = Field(default=75, description="Number of games to process per call", ge=10, le=150)


@mcp.tool(name="steam_sync_recent")
async def steam_sync_recent() -> str:
    """Daily sync: check recently played games and update playtime + achievements.

    Hits the Steam API for the last 25 recently played games, compares with
    the database, and updates playtime, last_played, and achievement progress
    for any games with changes.
    """
    try:
        if not STEAM_API_KEY:
            return "Error: STEAM_API_KEY not configured."

        # Get recently played from Steam
        data = await _steam_api_get(
            f"{STEAM_API_BASE}/IPlayerService/GetRecentlyPlayedGames/v0001/",
            {"key": STEAM_API_KEY, "steamid": STEAM_ID, "count": 25, "format": "json"}
        )
        if not data or "response" not in data:
            return "Error: Could not fetch recently played games from Steam."

        games = data["response"].get("games", [])
        if not games:
            return "No recently played games found."

        updated = []
        ach_updated = []
        errors = []

        for g in games:
            app_id = g["appid"]
            new_playtime = g.get("playtime_forever", 0)
            name = g.get("name", f"AppID {app_id}")

            try:
                # Get current DB state
                db_rows = await _query_turso(
                    "SELECT playtime_minutes, achievements_unlocked, achievements_total FROM games WHERE app_id = ?",
                    [app_id]
                )

                if not db_rows:
                    # Game not in DB yet  -  skip (weekly sync handles new games)
                    continue

                old = db_rows[0]
                old_playtime = old.get("playtime_minutes", 0) or 0
                changes = []

                # Update playtime if changed
                if new_playtime != old_playtime:
                    now_iso = datetime.now().isoformat()
                    await _execute_turso(
                        """UPDATE games SET playtime_minutes = ?, last_played = ?,
                           last_played_date = DATE('now'), updated_at = ?
                           WHERE app_id = ?""",
                        [new_playtime, int(datetime.now().timestamp()), now_iso, app_id]
                    )
                    diff = new_playtime - old_playtime
                    changes.append(f"+{diff}min playtime ({_format_hours(old_playtime)} → {_format_hours(new_playtime)})")

                    # Auto-set status to in_progress if was unplayed
                    if old_playtime == 0 and new_playtime > 0:
                        await _execute_turso(
                            "UPDATE games SET status = 'in_progress' WHERE app_id = ? AND status = 'unplayed'",
                            [app_id]
                        )
                        changes.append("status → in_progress")

                # Re-check achievements
                if old.get("achievements_total", 0) and old.get("achievements_total", 0) > 0:
                    achs, new_unlocked, total = await _fetch_player_achievements(app_id)
                    old_unlocked = old.get("achievements_unlocked", 0) or 0

                    if new_unlocked != old_unlocked:
                        pct = (new_unlocked / total * 100) if total > 0 else 0
                        await _execute_turso(
                            "UPDATE games SET achievements_unlocked = ?, completion_pct = ?, updated_at = ? WHERE app_id = ?",
                            [new_unlocked, pct, datetime.now().isoformat(), app_id]
                        )
                        changes.append(f"achievements {old_unlocked} → {new_unlocked}/{total} ({pct:.0f}%)")

                        # Update individual achievements
                        for a in achs:
                            if a["unlocked"]:
                                await _execute_turso(
                                    """UPDATE achievements SET unlocked = 1, unlock_time = ?,
                                       global_pct = ? WHERE app_id = ? AND api_name = ?""",
                                    [a["unlock_time"], a["global_pct"], app_id, a["api_name"]]
                                )
                        ach_updated.append(name)

                        await asyncio.sleep(1)  # Rate limit

                if changes:
                    updated.append(f"**{name}**: {', '.join(changes)}")

            except Exception as e:
                errors.append(f"{name}: {e}")

        # Log the sync
        await _execute_turso(
            "INSERT INTO sync_log (sync_time, sync_type, games_updated, status) VALUES (?, ?, ?, ?)",
            [datetime.now().isoformat(), "daily_recent", len(updated),
             "success" if not errors else "partial"]
        )

        lines = [f"## Daily Sync Complete\n"]
        lines.append(f"Checked **{len(games)}** recently played games.\n")
        if updated:
            lines.append(f"### Updated ({len(updated)} games):")
            for u in updated:
                lines.append(f"- {u}")
        else:
            lines.append("No changes detected  -  database is up to date.")

        if ach_updated:
            lines.append(f"\n**Achievement progress updated for:** {', '.join(ach_updated)}")

        if errors:
            lines.append(f"\n### Errors ({len(errors)}):")
            for e in errors[:5]:
                lines.append(f"- {e}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error in daily sync: {e}"


@mcp.tool(name="steam_sync_new_games")
async def steam_sync_new_games() -> str:
    """Weekly sync: detect newly purchased games, add to database with full enrichment.

    Fetches full owned games list from Steam, compares with database,
    and adds any new games with complete enrichment (store data, reviews,
    HLTB times, Deck compatibility, achievements).
    """
    try:
        if not STEAM_API_KEY:
            return "Error: STEAM_API_KEY not configured."

        # Get full owned games list
        data = await _steam_api_get(
            f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v0001/",
            {"key": STEAM_API_KEY, "steamid": STEAM_ID,
             "include_appinfo": 1, "include_played_free_games": 1, "format": "json"}
        )
        if not data or "response" not in data:
            return "Error: Could not fetch owned games from Steam."

        steam_games = data["response"].get("games", [])

        # Get existing app_ids from DB
        db_rows = await _query_turso("SELECT app_id FROM games")
        existing_ids = {r["app_id"] for r in db_rows}

        new_games = [g for g in steam_games if g["appid"] not in existing_ids]

        if not new_games:
            await _execute_turso(
                "INSERT INTO sync_log (sync_time, sync_type, games_added, status) VALUES (?, ?, 0, 'success')",
                [datetime.now().isoformat(), "weekly_new_games"]
            )
            return f"## Weekly Sync Complete\n\nNo new games detected. Library still at **{len(existing_ids)}** games."

        added = []
        errors = []

        for g in new_games:
            app_id = g["appid"]
            name = g.get("name", f"AppID {app_id}")
            playtime = g.get("playtime_forever", 0)

            try:
                # Fetch store data for enrichment
                store = await _fetch_store_data(app_id)
                await asyncio.sleep(1.5)  # Rate limit

                # Extract store metadata
                genres = ""
                primary_genre = "Unknown"
                developer = ""
                publisher = ""
                release_date = ""
                metacritic = None

                if store:
                    genre_list = store.get("genres", [])
                    if genre_list:
                        genres = ", ".join([gn.get("description", "") for gn in genre_list])
                        primary_genre = genre_list[0].get("description", "Unknown")
                    devs = store.get("developers", [])
                    developer = devs[0] if devs else ""
                    pubs = store.get("publishers", [])
                    publisher = pubs[0] if pubs else ""
                    rd = store.get("release_date", {})
                    release_date = rd.get("date", "")
                    mc = store.get("metacritic", {})
                    metacritic = mc.get("score") if mc else None

                # Fetch reviews
                review_score, review_count, review_desc = await _fetch_reviews(app_id)
                await asyncio.sleep(0.5)

                # Fetch Deck status
                deck_status = await _fetch_deck_status(app_id)

                # Fetch HLTB
                hltb_main, hltb_extra, hltb_comp = await _fetch_hltb(name)

                # Fetch achievements
                achs, ach_unlocked, ach_total = await _fetch_player_achievements(app_id)
                pct = (ach_unlocked / ach_total * 100) if ach_total > 0 else 0
                await asyncio.sleep(1)

                # Determine status
                status = "in_progress" if playtime > 0 else "unplayed"

                # Insert game
                await _execute_turso(
                    """INSERT INTO games (app_id, name, playtime_minutes, last_played,
                       achievements_total, achievements_unlocked, completion_pct,
                       hltb_main_hours, hltb_extra_hours, hltb_completionist_hours,
                       deck_status, review_score, review_count, review_desc, metacritic,
                       primary_genre, all_genres, developer, publisher, release_date,
                       status, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [app_id, name, playtime, g.get("rtime_last_played", 0),
                     ach_total, ach_unlocked, pct,
                     hltb_main, hltb_extra, hltb_comp,
                     deck_status, review_score, review_count, review_desc, metacritic,
                     primary_genre, genres, developer, publisher, release_date,
                     status, datetime.now().isoformat()]
                )

                # Insert achievements
                for a in achs:
                    await _execute_turso(
                        """INSERT OR IGNORE INTO achievements (app_id, api_name, display_name,
                           description, unlocked, unlock_time, global_pct)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        [app_id, a["api_name"], a["display_name"], a["description"],
                         a["unlocked"], a["unlock_time"], a["global_pct"]]
                    )

                hltb_str = f"{hltb_main:.0f}h" if hltb_main else "?"
                added.append(f"**{name}**  -  {primary_genre} | Reviews: {review_desc or 'N/A'} | "
                             f"HLTB: {hltb_str} | Deck: {deck_status} | {ach_total} achievements")

            except Exception as e:
                errors.append(f"{name}: {e}")

        # Log
        await _execute_turso(
            "INSERT INTO sync_log (sync_time, sync_type, games_added, status) VALUES (?, ?, ?, ?)",
            [datetime.now().isoformat(), "weekly_new_games", len(added),
             "success" if not errors else "partial"]
        )

        lines = [f"## Weekly Sync Complete\n"]
        lines.append(f"Library: **{len(existing_ids)}** → **{len(existing_ids) + len(added)}** games\n")
        if added:
            lines.append(f"### New Games Added ({len(added)}):")
            for a in added:
                lines.append(f"- {a}")
        if errors:
            lines.append(f"\n### Errors ({len(errors)}):")
            for e in errors[:10]:
                lines.append(f"- {e}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error in weekly sync: {e}"


@mcp.tool(name="steam_sync_refresh_metadata")
async def steam_sync_refresh_metadata(params: SyncRefreshInput) -> str:
    """Bi-weekly sync: refresh review scores, Deck status, and genres for a batch of games.

    Processes games in batches to respect API rate limits. Call repeatedly with
    increasing offset until all games are processed. Each call handles ~75 games.
    """
    try:
        # Get batch of games
        games = await _query_turso(
            "SELECT app_id, name, review_score, deck_status, all_genres FROM games ORDER BY app_id LIMIT ? OFFSET ?",
            [params.batch_size, params.offset]
        )

        if not games:
            return f"## Metadata Refresh Complete\n\nNo more games to process (offset {params.offset})."

        total_count = await _query_turso("SELECT COUNT(*) as total FROM games")
        total = total_count[0]["total"] if total_count else 0

        updated = []
        errors = []

        for g in games:
            app_id = g["app_id"]
            name = g["name"]
            changes = []

            try:
                # Refresh reviews
                new_score, new_count, new_desc = await _fetch_reviews(app_id)
                await asyncio.sleep(1.5)  # Rate limit for store API

                if new_score is not None:
                    old_score = g.get("review_score")
                    if old_score is None or abs((new_score or 0) - (old_score or 0)) >= 0.5:
                        changes.append(f"reviews: {old_score or 0:.0f}% → {new_score:.0f}%")

                    await _execute_turso(
                        "UPDATE games SET review_score = ?, review_count = ?, review_desc = ?, updated_at = ? WHERE app_id = ?",
                        [new_score, new_count, new_desc, datetime.now().isoformat(), app_id]
                    )

                # Refresh Deck status
                new_deck = await _fetch_deck_status(app_id)
                if new_deck != "unknown":
                    old_deck = g.get("deck_status", "unknown")
                    if new_deck != old_deck:
                        changes.append(f"Deck: {old_deck} → {new_deck}")
                    await _execute_turso(
                        "UPDATE games SET deck_status = ?, updated_at = ? WHERE app_id = ?",
                        [new_deck, datetime.now().isoformat(), app_id]
                    )

                # Refresh genres from store
                store = await _fetch_store_data(app_id)
                await asyncio.sleep(1)
                if store:
                    genre_list = store.get("genres", [])
                    if genre_list:
                        new_genres = ", ".join([gn.get("description", "") for gn in genre_list])
                        new_primary = genre_list[0].get("description", "Unknown")
                        old_genres = g.get("all_genres", "")
                        if new_genres != old_genres:
                            changes.append(f"genres updated")
                        await _execute_turso(
                            "UPDATE games SET primary_genre = ?, all_genres = ?, updated_at = ? WHERE app_id = ?",
                            [new_primary, new_genres, datetime.now().isoformat(), app_id]
                        )

                    # Also refresh metacritic if available
                    mc = store.get("metacritic", {})
                    if mc and mc.get("score"):
                        await _execute_turso(
                            "UPDATE games SET metacritic = ? WHERE app_id = ?",
                            [mc["score"], app_id]
                        )

                if changes:
                    updated.append(f"**{name}**: {', '.join(changes)}")

            except Exception as e:
                errors.append(f"{name}: {e}")

        # Log
        await _execute_turso(
            "INSERT INTO sync_log (sync_time, sync_type, games_updated, status) VALUES (?, ?, ?, ?)",
            [datetime.now().isoformat(), f"biweekly_metadata_offset_{params.offset}",
             len(updated), "success" if not errors else "partial"]
        )

        processed_through = params.offset + len(games)
        more_remaining = processed_through < total

        lines = [f"## Metadata Refresh  -  Batch {params.offset // params.batch_size + 1}\n"]
        lines.append(f"Processed games **{params.offset + 1}–{processed_through}** of **{total}**\n")

        if updated:
            lines.append(f"### Changes Detected ({len(updated)}):")
            for u in updated[:20]:
                lines.append(f"- {u}")
            if len(updated) > 20:
                lines.append(f"- ... and {len(updated) - 20} more")
        else:
            lines.append("No significant changes in this batch.")

        if errors:
            lines.append(f"\n### Errors ({len(errors)}):")
            for e in errors[:5]:
                lines.append(f"- {e}")

        if more_remaining:
            lines.append(f"\n**More games remaining.** Call again with `offset={processed_through}` to continue.")
        else:
            lines.append(f"\n**All {total} games processed.** Full refresh complete.")

        return "\n".join(lines)

    except Exception as e:
        return f"Error in metadata refresh: {e}"


# =============================================================================
# Prompts (skills exposed to any MCP client  -  phone, web, etc.)
# =============================================================================

_SKILLS_DIR = pathlib.Path(__file__).parent / "skills"


def _load_skill(name: str) -> str:
    """Load a skill's SKILL.md content from the skills directory."""
    path = _SKILLS_DIR / name / "SKILL.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return (f"Skill '{name}' not found on the server. "
            f"The skill file should exist at skills/{name}/SKILL.md in the repo.")


@mcp.prompt(
    name="steam_session_planner",
    description="Pick ONE Steam game for a specific play session. Use when you want a decisive recommendation for tonight, an hour, or a specific device (Deck / Living Room / Office PC). Returns opinionated single-game pick, not a top 5 list."
)
def steam_session_planner_prompt() -> str:
    """Load the steam-session-planner skill content."""
    return _load_skill("steam-session-planner")


@mcp.prompt(
    name="steam_backlog_triage",
    description="Walk through structured triage of the unplayed Steam backlog. Use when you want to clean up your library by making batch keep/abandon/not-interested decisions on 10-20 games at a time."
)
def steam_backlog_triage_prompt() -> str:
    """Load the steam-backlog-triage skill content."""
    return _load_skill("steam-backlog-triage")


@mcp.prompt(
    name="steam_game_intelligence",
    description="Foundational context for correctly interpreting Steam library data. Critical rules about completion_pct (achievement completionism, NOT story progress), how to judge whether a game is actually beaten, and genre-aware interpretation. Load this before any other Steam library question."
)
def steam_game_intelligence_prompt() -> str:
    """Load the steam-game-intelligence skill content."""
    return _load_skill("steam-game-intelligence")


# =============================================================================
# Entry point (local development only  -  Horizon ignores __main__)
# =============================================================================

if __name__ == "__main__":
    mcp.run()
