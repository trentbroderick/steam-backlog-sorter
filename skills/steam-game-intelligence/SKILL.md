---
name: steam-game-intelligence
description: >
  Skill for intelligently interpreting Steam library data — especially achievement context — when answering ANY question about the user's games. Use this skill whenever: making game recommendations, assessing whether a game has been "beaten" or "finished", summarizing play history or backlog, comparing games by progress, or any query that touches the user's Steam library MCP tools. ALWAYS use this skill before relying on completion_pct as a proxy for progress. completion_pct is an achievement completionism metric, NOT a story progress metric. This skill teaches you how to read achievement data properly so you don't tell someone to "finish" a game they already beat.
---

# Steam Game Intelligence

## The Core Problem

The `completion_pct` field in the games table measures **achievement completionism**, not **story progress**. A player who completed the main story of Cyberpunk 2077 might sit at 77% completion — the remaining 23% is side content, collectibles, challenge achievements, and DLC. Telling that player to "finish Cyberpunk" is wrong and makes you look like you don't know what you're talking about.

**Rule #1: NEVER use `completion_pct` alone to determine whether a game has been finished.**

## Achievement Analysis Framework

When answering ANY question about the user's game status or making recommendations, follow this priority stack:

### Priority 1: Achievement Context (REQUIRED)

Before making any claim about whether a game is "done," "in progress," or "unfinished," you MUST query the achievements table for that game and look for **story completion indicators**:

**Strong completion signals** (check descriptions and display names):
- Descriptions containing: "complete the main story", "finish the game", "see the credits", "complete the campaign", "beat the final boss", "complete all main quests/missions"
- Display names that are clearly narrative endpoints: "The End", "Fin", "Credits", "Epilogue", final chapter names
- Achievements with empty descriptions BUT high-ish global_pct (20-40%) that follow a sequence of similarly-structured story achievements (these are often hidden story beats — e.g., Cyberpunk's tarot card achievements)

**How to query:**
```sql
-- Get all achievements for a game, sorted by global unlock rate descending
SELECT display_name, description, unlocked, global_pct
FROM achievements
WHERE app_id = (SELECT app_id FROM games WHERE name LIKE '%GameName%')
ORDER BY global_pct DESC
```

Story progression achievements tend to have **descending global_pct** — earlier story beats are unlocked by more players. If you see a chain like 88% → 68% → 54% → 51% → 35% with the last one being "Complete the main storyline" and it's unlocked, the game is **beaten**.

### Priority 2: Secondary Signals (Weighted Context)

Use these to supplement, NEVER to override, achievement context:

- **`status` field**: If the user has manually set a game to "completed" or "abandoned," respect that. This is explicit user intent.
- **Playtime vs HLTB**: If `playtime_minutes` significantly exceeds `hltb_main_hours * 60`, the user has likely finished the main content. This is a soft signal, not proof.
- **`global_pct` distribution**: Achievements with <5% global unlock rate are almost always optional/hardcore/collectible content. Missing these does NOT mean the game is unfinished.
- **`completion_pct`**: Useful for gauging completionism interest, NOT story progress. A player at 40% who has the "beat the game" achievement is done. A player at 95% is a completionist. Neither number tells you if they've seen the credits without checking achievements.

### Priority 3: Genre-Aware Interpretation

Not all games have "main stories" to complete:

- **Roguelikes/Roguelites** (Balatro, Hades, Risk of Rain): "Completion" is fuzzy. Look for milestone achievements (first win, boss kills, unlock milestones). A first-win achievement being unlocked means they've "beaten" it at least once, but these games are designed for replay.
- **Multiplayer/Live Service** (Apex Legends, CS2, Destiny 2): completion_pct is nearly meaningless. Focus on playtime and engagement patterns.
- **Sandbox/Sim** (Cities Skylines, Factorio): No story endpoint. Use playtime relative to HLTB and achievement breadth to gauge engagement.
- **Linear/Narrative** (Cyberpunk, Witcher, BioShock): Story completion achievements are the definitive signal. Always check these.

## Recommendation Behavior

When recommending games to play:

### What to Recommend
- **Unplayed games** (`status = 'unplayed'` or `playtime_minutes = 0`) with good review scores
- **Games in progress** where story completion achievements exist but are NOT unlocked
- **Games matching requested genre/mood/time constraints**

### What NOT to Recommend
- Games where the main story completion achievement IS unlocked — **deprioritize these**. The user beat the game. Don't tell them to play it again unless they specifically ask about replaying or completing remaining content.
- Games marked `abandoned` or `not_interested` — respect user intent
- Games where the user has very high completion_pct (>90%) — they're either done or deliberately stopped

### When to Override Deprioritization
If a game the user beat is genuinely relevant to mention (e.g., they ask "what story games have I played?" or you're drawing comparisons), you can reference it — but frame it correctly:
- ✅ "You finished Cyberpunk 2077's main story — if you enjoyed that, you might like..."
- ❌ "You're 77% through Cyberpunk 2077, maybe finish that first"

## Response Framing

When discussing a user's game progress:

- **Completed story + <100% achievements**: "You beat [game] — you have X% of achievements if you want to go back for completionist content"
- **No story achievement unlocked + significant playtime**: "You've put X hours into [game] but haven't finished the main story yet"
- **No story achievement exists (sandbox/multiplayer)**: Frame around playtime and engagement, not completion percentage
- **Low playtime + few achievements**: "You've barely touched [game]" — safe to recommend

## Quick Reference: Common Completion Achievement Patterns

| Pattern | Example | Signal |
|---------|---------|--------|
| Explicit story completion | "Complete the main storyline" | Definitive |
| Final chapter/act name | "The World" (Cyberpunk), "The End" | Strong |
| Credits reference | "Watch the credits roll" | Definitive |
| Empty desc + narrative sequence | Tarot cards in Cyberpunk | Strong (contextual) |
| "Beat on [difficulty]" | "Complete on Hard" | Definitive |
| First win (roguelike) | "First Clear", "Escaped!" | Strong for genre |
| Kill final boss | "Slay [boss name]" | Strong |

## Critical Reminders

1. **Always query achievements before judging game status.** No exceptions for story-driven games.
2. **completion_pct is a completionism metric, not a progress metric.** Internalize this.
3. **Deprioritize beaten games in recommendations** unless contextually relevant.
4. **When in doubt, check achievements.** It takes one query. Do it.
5. **Frame progress accurately.** The difference between "you're 77% done" and "you beat the main story with 77% of achievements" is the difference between being helpful and being wrong.
