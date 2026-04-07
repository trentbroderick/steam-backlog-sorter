---
name: steam-backlog-triage
description: Walk Trent through structured triage decisions on his unplayed Steam backlog. Use when he says "my backlog is too big", "help me clean up my library", "let's triage some games", "I have too many unplayed games", or any variant of decluttering his Steam library. This skill presents games in small batches (10-20 at a time), gives him concrete data on each, and records keep/someday/not_interested decisions via steam_update_game_status. The goal is shrinking his unplayed count from hundreds down to a real shortlist of games he actually intends to play. Do NOT use for general library queries or recommendations — this is specifically for batch decision-making sessions.
---

# Steam Backlog Triage

## Purpose
Trent has ~600 unplayed games. Most of them he will never play. The longer the backlog stays bloated, the harder it is to find anything worth playing. This skill helps him make explicit, recorded decisions on batches of games so the database reflects reality: what he'll actually play vs. what's just clutter.

## When to Use
**Trigger phrases:**
- "Help me triage my backlog"
- "My backlog is too big, let's clean it up"
- "I have too many unplayed games"
- "Let's go through some unplayed games"
- "Help me decide what to abandon"

**Do NOT use when:**
- He's asking for a recommendation (use `steam-session-planner`)
- He's asking what's in his library (use `steam_query_library`)
- He wants stats on his backlog (use `steam_get_stats` with category=backlog)

## Workflow

### Step 1: Read steam-game-intelligence FIRST
Always read the `steam-game-intelligence` skill before triaging. You'll be making judgment calls about whether games are "actually unplayed" vs. "barely touched" vs. "tried and abandoned" — and the achievement data is essential for this.

### Step 2: Ask how he wants to scope the session
Use `AskUserQuestion` to clarify:

1. **Batch size** — How many games per round? (10, 20, or 30. Default: 15.)
2. **Triage angle** — Which slice of the backlog should we work on?
   - "Obvious abandons" — installed long ago, briefly played, never returned (lowest emotional cost)
   - "Pure unplayed" — never touched at all, decide based on metadata alone
   - "Stale in-progress" — marked in_progress but `last_played` is months/years old
   - "By genre" — focus on a specific genre he wants to thin out
   - "Random batch" — just give me whatever
3. **Session length** — Quick (1 batch) or extended (multiple batches)?

### Step 3: Pull the batch
Use `steam_run_query` with SQL tailored to the chosen angle. Examples:

**Obvious abandons:**
```sql
SELECT name, app_id, playtime_minutes, completion_pct, review_score,
       hltb_main_hours, primary_genre, last_played_date, achievements_total
FROM games
WHERE playtime_minutes BETWEEN 1 AND 60
  AND status NOT IN ('completed', 'abandoned', 'not_interested')
  AND (last_played_date IS NULL OR last_played_date < DATE('now', '-2 years'))
ORDER BY playtime_minutes ASC
LIMIT [batch_size]
```

**Pure unplayed:**
```sql
SELECT name, app_id, review_score, review_desc, hltb_main_hours,
       primary_genre, deck_status, metacritic, developer
FROM games
WHERE playtime_minutes = 0
  AND status NOT IN ('not_interested', 'abandoned')
ORDER BY RANDOM()
LIMIT [batch_size]
```

**Stale in-progress:**
```sql
SELECT name, app_id, playtime_minutes, completion_pct, last_played_date,
       achievements_unlocked, achievements_total, review_desc
FROM games
WHERE status = 'in_progress'
  AND last_played_date < DATE('now', '-6 months')
ORDER BY last_played_date ASC
LIMIT [batch_size]
```

### Step 4: Present the batch as a decision matrix
For each game, show one compact line with the data that matters for triage. Use this format:

```
## Triage Batch — [angle name] (15 games)

For each: **K**eep / **S**omeday / **N**ot interested / **A**bandon (already tried)
Or just say a number for individual feedback.

1. **Hollow Knight** — 32m played | 4% achievements | 90% reviews ("Overwhelmingly Positive") | HLTB 26h | Metroidvania | Tried 3 years ago
2. **Disco Elysium** — Unplayed | 96% reviews | HLTB 21h | RPG | Deck: verified
3. **Slay the Spire** — 18m played | 0% achievements | 96% reviews | HLTB 23h | Roguelike | Tried 2 years ago
...
```

Include data that makes the decision easy:
- **Playtime + last played**: tells him if he tried it
- **Review score + description**: justifies whether it's even worth keeping
- **HLTB**: time investment required
- **Genre**: helps spot patterns ("oh I have 12 unplayed roguelikes")
- **Achievement progress**: distinguishes "tried it" from "got into it"

### Step 5: Collect decisions and execute
Wait for his decisions. He can respond in any of these ways:
- "1K 2K 3N 4S 5A 6N 7K..." (compact format)
- "Keep 1, 2, 4. Not interested in 3, 5, 6. Someday 7, 8."
- "1: keep, 2: I forgot I owned this, abandon"
- Conversational about specific games before deciding

**Mapping to status values:**
- **Keep** → no change (it stays in active backlog)
- **Someday** → `status = 'unplayed'` with note "someday pile" (or leave alone if already unplayed)
- **Not interested** → `status = 'not_interested'`
- **Abandon** → `status = 'abandoned'`

For each non-keep decision, call `steam_update_game_status` with the game name and new status. Add a brief note if context warrants it.

If he's chatty about a specific game, engage briefly — sometimes that conversation changes his mind, and that's fine. The goal is explicit decisions, not speed.

### Step 6: Summarize the round
After processing all decisions:

```
## Round Complete

**Decisions made:**
- Kept: 6 games
- Marked "not interested": 5 games
- Marked "abandoned": 3 games
- No change (someday): 1 game

**Backlog impact:**
- Unplayed/in-progress before: 612
- Unplayed/in-progress after: 604
- Removed from active backlog: 8 games

Want to do another batch?
```

Use `steam_get_stats` with category=backlog to get fresh numbers.

### Step 7: Continue or wrap
Ask if he wants another batch. If yes, vary the angle so he doesn't burn out on one slice. If no, give him the final stats and call it done.

## Critical Rules

1. **Small batches.** 10-20 games max per round. Decision fatigue is real and hits fast on this kind of task.

2. **One line per game.** Don't write paragraphs about each — that's what the detail tool is for if he wants more info on a specific game.

3. **Make the data do the work.** A game with 32 minutes played, 4% achievements, last played 3 years ago, and a 23-hour main story is *visibly* an abandon candidate. Let him see that without you editorializing.

4. **Respect his "keep" decisions.** Don't argue. If he wants to keep a game with 0% chance he'll play it, that's his call. The whole point is letting him make the decision, not making it for him.

5. **Record everything via steam_update_game_status.** The whole purpose is creating durable database state. Don't just discuss decisions — execute them.

6. **No guilt-tripping.** Avoid "but the reviews are so good!" or "are you sure?" energy. He owns his time.

7. **Use steam-game-intelligence to interpret the data correctly.** "0% achievements" might mean "the game has no achievements" — check before you draw conclusions. "8 hours played, 12% achievements" on a JRPG could mean he's deep in chapter 1, not "barely touched it."

## Example flow

**User:** "Help me clean up my backlog"

**Claude:** [asks the 3 scoping questions via AskUserQuestion]

**User:** [picks 15 games, "Obvious abandons", extended session]

**Claude:** [pulls SQL, presents 15-game batch in compact format]

**User:** "1K 2N 3N 4A 5K 6N 7N 8K 9A 10N 11K 12N 13S 14N 15A"

**Claude:** [executes 14 status updates, summarizes the round, asks about next batch]

**User:** "Yeah let's do unplayed RPGs next"

**Claude:** [pulls another batch with the new angle, repeats]

## Notes on the "Someday" tier
"Someday" is intentionally a non-action. If a game is already `unplayed`, marking it "someday" doesn't change the database — it just confirms the user thought about it and decided not to abandon it. Don't waste an `update_game_status` call on a no-op. Just acknowledge it in the summary.

If he wants a real "someday" tier separate from "unplayed", suggest adding a `user_notes` value of "someday pile" via the update tool — that's queryable later.
