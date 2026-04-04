# Steam Library MCP Server

MCP server that connects Claude to your Steam game library hosted on Turso. Ask natural language questions about your 908-game backlog and get recommendations based on real data.

## Setup

### 1. Install dependencies

```bash
cd steam-library-mcp
pip install -e .
```

### 2. Configure Claude Desktop

Add this to your Claude Desktop config file:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "steam-library": {
      "command": "python",
      "args": ["/path/to/steam-library-mcp/server.py"],
      "env": {
        "TURSO_URL": "https://steamdb-trentbroderick.aws-us-east-2.turso.io",
        "TURSO_TOKEN": "your-turso-token-here",
        "STEAM_API_KEY": "your-steam-api-key"
      }
    }
  }
}
```

### 3. Restart Claude Desktop

The tools will appear automatically.

## Available Tools

| Tool | Description |
|------|-------------|
| `steam_search_games` | Search for games by name |
| `steam_query_library` | Filter games by genre, device, status, reviews, HLTB, etc. |
| `steam_get_recommendations` | Get personalized recommendations by device, mood, time, genre |
| `steam_get_game_detail` | Deep dive into a specific game with achievement breakdown |
| `steam_get_stats` | Library statistics (overview, genres, completion, deck, backlog, playtime, recent) |
| `steam_update_game_status` | Mark games as completed, abandoned, or not interested |
| `steam_run_query` | Run custom SQL queries against the database |

## Example Questions

- "What should I play on my Steam Deck tonight?"
- "I have 2 hours, what can I knock out?"
- "What RPGs am I closest to finishing?"
- "What cult classics am I sleeping on?"
- "Show me my backlog stats"
- "What are the best unplayed indie games I own?"
