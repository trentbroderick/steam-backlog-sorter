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
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime

import httpx
import libsql_client
from pydantic import BaseModel, Field, ConfigDict, field_validator
from fastmcp import FastMCP

# Prefab UI (rich UI responses for MCP clients that support it)
try:
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        Column, Row, Grid, Card, CardContent, Heading,
        Badge, Muted, Separator,
    )
    PREFAB_AVAILABLE = True
except Exception:
    PREFAB_AVAILABLE = False

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

    return (f"**{name}** — {playtime} played | {pct:.0f}% achievements | "
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
    count: Optional[int] = Field(default=5, description="Number of recommendations", ge=1, le=20)
    include_in_progress: Optional[bool] = Field(default=True, description="Include games you've already started?")


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
    Scans all 908 games in the database — never a random subset.

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
            # Office PC can play anything — no filter needed

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
    if not score:
        return "secondary"
    if score >= 90:
        return "success"
    if score >= 75:
        return "default"
    if score >= 60:
        return "warning"
    return "destructive"


def _build_recommendations_app(top, device_label: str, mood: Optional[str], hours: Optional[float]):
    """Build a Prefab card-grid app for recommendations using the context-manager DSL."""
    subtitle_bits = []
    if mood:
        subtitle_bits.append(f"mood: {mood}")
    if hours:
        subtitle_bits.append(f"{hours}h available")
    subtitle = " • ".join(subtitle_bits) if subtitle_bits else "Personalized picks from your library"

    with Column(gap=4) as view:
        Heading(f"Recommended for {device_label}")
        Muted(subtitle)
        Separator()
        with Grid(cols=3, gap=4):
            for i, (score, g, reasons) in enumerate(top, 1):
                pt = g.get("playtime_minutes") or 0
                hltb = g.get("hltb_main_hours")
                pct = g.get("completion_pct") or 0
                rs = g.get("review_score")
                with Card():
                    with CardContent():
                        with Column(gap=2):
                            Heading(f"{i}. {g['name']}")
                            Muted(g.get("developer") or g.get("primary_genre") or "")
                            with Row(gap=2):
                                if g.get("deck_status") and g["deck_status"] != "unknown":
                                    Badge(f"Deck: {g['deck_status']}", variant="outline")
                                if hltb:
                                    if pt > 0:
                                        Badge(f"~{max(0, hltb - pt / 60):.0f}h left", variant="outline")
                                    else:
                                        Badge(f"{hltb:.0f}h HLTB", variant="outline")
                                if pct > 0:
                                    Badge(f"{pct:.0f}% done", variant="outline")
                                if rs:
                                    Badge(f"{rs:.0f}%", variant=_review_badge_variant(rs))
                            Muted(f"Why: {'; '.join(reasons[:3])}")

    return PrefabApp(view=view)


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

    Analyzes all 908 games considering: review scores, HLTB completion times,
    achievement progress, Deck compatibility, genre, and play history.

    Use this for questions like:
    - "What should I play on my Steam Deck tonight?"
    - "I have 2 hours, what can I knock out?"
    - "What RPGs should I focus on?"
    - "What cult classics am I sleeping on?"
    - "What games am I closest to finishing?"
    """
    try:
        # Build different recommendation pools
        conditions = ["status NOT IN ('completed', 'abandoned', 'not_interested')"]
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

        # Pull candidates — we get a generous pool to score from
        sql = f"""SELECT name, app_id, playtime_minutes, completion_pct, review_score,
                         review_count, review_desc, hltb_main_hours, hltb_extra_hours,
                         deck_status, primary_genre, all_genres, status, metacritic,
                         achievements_unlocked, achievements_total, last_played_date,
                         developer, hltb_completionist_hours
                  FROM games
                  {where}
                  AND review_score IS NOT NULL
                  ORDER BY review_score DESC
                  LIMIT 200"""

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

            # Completion proximity bonus (0-25 points) — heavily reward close-to-done
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

            # Unplayed bonus — prioritize the untouched
            if pt == 0:
                score += 3
                reasons.append("Unplayed — fresh experience")

            if not reasons:
                reasons.append(f"{g.get('primary_genre', 'Game')} — {g.get('review_desc', '')}")

            scored.append((score, g, reasons))

        # Sort by score, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:params.count]

        device_label = {
            DeviceEnum.STEAM_DECK: "Steam Deck",
            DeviceEnum.LIVING_ROOM: "Living Room PC",
            DeviceEnum.OFFICE: "Office PC",
            DeviceEnum.ANY: "any device",
        }.get(params.device, "any device")

        # Rich UI path (Prefab)
        if PREFAB_AVAILABLE:
            return _build_recommendations_app(top, device_label, params.mood, params.available_hours)

        # Text fallback
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

        return "\n".join(lines)

    except Exception as e:
        return f"Error generating recommendations: {e}"


@mcp.tool(
    name="steam_get_game_detail",
    annotations={
        "title": "Get Game Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def steam_get_game_detail(params: GetGameDetailInput) -> str:
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
        lines = [f"# {g['name']}\n"]
        lines.append(f"**App ID:** {g['app_id']}")
        lines.append(f"**Developer:** {g.get('developer', 'Unknown')} | **Publisher:** {g.get('publisher', 'Unknown')}")
        lines.append(f"**Genre:** {g.get('all_genres', g.get('primary_genre', 'Unknown'))}")
        lines.append(f"**Release Date:** {g.get('release_date', 'Unknown')}")
        lines.append(f"**Status:** {g.get('status', 'Unknown')}")
        lines.append("")

        # Play stats
        lines.append("## Play Stats")
        lines.append(f"- **Playtime:** {_format_hours(g.get('playtime_minutes'))}")
        if g.get("last_played_date"):
            lines.append(f"- **Last Played:** {g['last_played_date']}")
        lines.append(f"- **Achievements:** {g.get('achievements_unlocked', 0)}/{g.get('achievements_total', 0)} ({g.get('completion_pct', 0):.1f}%)")
        lines.append("")

        # Reviews & ratings
        lines.append("## Reviews & Ratings")
        lines.append(f"- **Steam Reviews:** {g.get('review_desc', 'N/A')} ({g.get('review_score', 0):.0f}% from {g.get('review_count', 0):,} reviews)")
        if g.get("metacritic"):
            lines.append(f"- **Metacritic:** {g['metacritic']}")
        lines.append("")

        # HLTB
        lines.append("## How Long to Beat")
        lines.append(f"- **Main Story:** {g.get('hltb_main_hours', '?')}h")
        lines.append(f"- **Main + Extras:** {g.get('hltb_extra_hours', '?')}h")
        lines.append(f"- **Completionist:** {g.get('hltb_completionist_hours', '?')}h")
        if g.get("playtime_minutes") and g.get("hltb_main_hours"):
            remaining = g["hltb_main_hours"] - (g["playtime_minutes"] / 60)
            if remaining > 0:
                lines.append(f"- **Estimated Remaining:** ~{remaining:.0f}h (main story)")
        lines.append("")

        # Deck
        lines.append(f"## Steam Deck: **{g.get('deck_status', 'unknown').upper()}**\n")

        # Achievements detail
        if g.get("achievements_total") and g["achievements_total"] > 0:
            achs = await _query_turso(
                """SELECT display_name, description, unlocked, unlock_time, global_pct
                   FROM achievements WHERE app_id = ?
                   ORDER BY unlocked DESC, global_pct DESC""",
                [g["app_id"]]
            )

            if achs:
                unlocked = [a for a in achs if a.get("unlocked")]
                locked = [a for a in achs if not a.get("unlocked")]

                lines.append(f"## Achievements ({len(unlocked)}/{len(achs)} unlocked)\n")

                if locked:
                    # Show easiest locked achievements (highest global %)
                    lines.append("### Easiest Remaining:")
                    for a in sorted(locked, key=lambda x: x.get("global_pct", 0), reverse=True)[:10]:
                        desc = f" — {a['description']}" if a.get("description") else ""
                        lines.append(f"- {a.get('display_name', a.get('api_name', '?'))}{desc} ({a.get('global_pct', 0):.1f}% of players)")
                    lines.append("")

        if g.get("user_notes"):
            lines.append(f"## Notes\n{g['user_notes']}\n")

        return "\n".join(lines)

    except Exception as e:
        return f"Error getting game details: {e}"


@mcp.tool(
    name="steam_get_stats",
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

            # Achievement stats
            ach_rows = await _query_turso("SELECT SUM(achievements_unlocked) as unlocked, SUM(achievements_total) as total FROM games WHERE achievements_total > 0")
            if ach_rows:
                a = ach_rows[0]
                lines.append(f"- **Achievements:** {a['unlocked']:,}/{a['total']:,} ({a['unlocked']/a['total']*100:.1f}%)")

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
                             f"({r['completion_pct']:.0f}%) — {remaining} left — {r['hours']:.1f}h played")
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
                    lines.append(f"- **{g['name']}** — {g['review_score']:.0f}% | HLTB: {hltb} | {g['primary_genre']}")

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
                    lines.append(f"- **{g['name']}** — {g['hltb_main_hours']:.1f}h | {g['review_score']:.0f}% | Deck: {g['deck_status']}")

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
                lines.append(f"- **{r['name']}** — {r['last_played_date']} | "
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
                    # Game not in DB yet — skip (weekly sync handles new games)
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
            lines.append("No changes detected — database is up to date.")

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
                added.append(f"**{name}** — {primary_genre} | Reviews: {review_desc or 'N/A'} | "
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

        lines = [f"## Metadata Refresh — Batch {params.offset // params.batch_size + 1}\n"]
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
# Prompts (skills exposed to any MCP client — phone, web, etc.)
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
# Entry point (local development only — Horizon ignores __main__)
# =============================================================================

if __name__ == "__main__":
    mcp.run()
