---
name: steam-session-planner
description: Pick ONE specific Steam game for a specific play session. Use when Trent asks "what should I play tonight?", "I have an hour, what should I play?", "what should I play on my Deck right now?", or any variant of "give me one decisive game pick". This skill is opinionated — it makes a single confident recommendation, not a top 5 list. It checks Trent's calendar for the actual time window, considers his device context, and prioritizes games he's close to finishing over fresh starts. Do NOT use for browsing or "show me games like X" requests — use steam_get_recommendations for that.
---

# Steam Session Planner

## Purpose
Trent has 908 games. He doesn't need another list — he needs someone to just pick one. This skill makes a single, confident recommendation for a specific play session, with clear reasoning for *why this one, right now*.

## When to Use
**Trigger phrases:**
- "What should I play tonight?"
- "I have [N] hours / minutes — what should I play?"
- "Pick something for my Deck right now"
- "I'm bored, what's worth booting up?"
- "What should I play on the [Deck / Office PC / Living Room]?"

**Do NOT use when:**
- Trent wants to browse multiple options (use `steam_get_recommendations` directly)
- He's asking about a specific genre exploration (use `steam_query_library`)
- He's deciding what to *buy* (different problem entirely)

## Workflow

### Step 1: Read steam-game-intelligence FIRST
Always read the `steam-game-intelligence` skill before making any recommendation. It contains critical context about how to interpret completion_pct and playtime data — getting this wrong leads to embarrassing recommendations like telling him to "finish" a game he already beat.

### Step 2: Establish session context (ask only what's missing)
You need three things:
1. **Device** — Steam Deck, Living Room PC (controller-friendly required), or Office PC (anything goes)
2. **Available time** — how long is the session?
3. **Mood/energy** — winding down vs. wanting something engaging? (Optional, infer if not given)

If the user said "tonight" without a time, check Google Calendar (`gcal_list_events`) for tonight's events to estimate the available window. If they're free until midnight and it's currently 8 PM, you have a ~3-4 hour window.

If they said "I have an hour" — trust them, don't second-guess.

### Step 3: Query the library intelligently
Use `steam_query_library` and `steam_run_query` to find candidates. Your priority order:

1. **Close-to-finishing games** (highest priority): Games where they have meaningful playtime AND are 60%+ through achievements. These represent the highest-value session — finishing something feels great. Query: `playtime_minutes > 0 AND completion_pct >= 60 AND completion_pct < 100 AND status NOT IN ('completed', 'abandoned', 'not_interested')`.

2. **Active in-progress games**: Games with `status = 'in_progress'` and recent `last_played_date`. These are games he's actively engaged with.

3. **Quick wins for short sessions**: If session is <2 hours, prioritize games with `hltb_main_hours <= 6` and `review_score >= 85`.

4. **Untouched gems for longer sessions**: Only if categories 1-3 are exhausted, recommend a fresh start from unplayed games with high review scores.

Always filter by device:
- **Steam Deck** or **Living Room**: `deck_status IN ('verified', 'playable')`
- **Office PC**: no filter

### Step 4: Make ONE pick
Pick the single best candidate. Do NOT present a list.

### Step 5: Present the recommendation
Format:

```
## Tonight: **[Game Name]**

**Why this, right now:**
[2-3 sentences explaining the specific reasoning. Reference concrete data:
playtime, achievement progress, time fit, mood match, device fit.]

**The session:**
- Device: [device]
- Time fit: [estimated remaining playtime vs. their window]
- Where you left off: [if in-progress, what they were last doing — infer
  from achievement timeline if possible]

**Backup picks (if you're not feeling it):**
1. [Game] — [one-line reason]
2. [Game] — [one-line reason]
```

Two backup picks max. The point is decisiveness, not options.

### Step 6: Optional follow-through
After the recommendation, you can offer to:
- Use `steam_get_game_detail` to show the easiest remaining achievements (helps them dive in with a goal)
- Mark the game `in_progress` if it wasn't already (`steam_update_game_status`)

Do these only if they ask. Don't volunteer.

## Critical Rules

1. **One pick. Not five.** If you find yourself listing options as the main answer, you're doing it wrong.

2. **Specificity over hedging.** "Hades, because you're at 78% achievements and your last session was 6 days ago" beats "you might enjoy Hades, it's well-reviewed and on your Deck."

3. **Respect the time window.** Don't recommend a 40-hour JRPG for a 1-hour session. Don't recommend a 90-minute roguelike when he has a free Saturday.

4. **Read steam-game-intelligence first, every time.** completion_pct ≠ story progress. A 12% completion on a 60-hour RPG might mean he's halfway through the story. Never tell him to "finish" something based on completion_pct alone.

5. **No fake enthusiasm.** Don't say "this is going to be amazing!" — just give him the data and the reasoning. He can decide how excited to be.

## Example output

```
## Tonight: **Tunic**

**Why this, right now:**
You're at 67% achievements with 14 hours played, and your last session was 11 days ago — you're on the edge of forgetting what puzzle you were working on. The remaining content is concentrated in the late-game secret hunting, which fits a focused 2-hour block better than starting something new. It's Deck-verified and the controller layout is dialed in.

**The session:**
- Device: Steam Deck
- Time fit: ~3-4h to wrap the main remaining content, your 2h window covers a meaningful chunk
- Where you left off: Just unlocked the "Holy Cross" achievement — you're in the secrets phase

**Backup picks:**
1. Inscryption — 41% achievements, you're partway through Act 2
2. Slay the Spire — bite-sized runs, no commitment if you bounce
```
