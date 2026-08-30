import os
import google.generativeai as genai
from dotenv import load_dotenv
import fpl_tools

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-3.5-flash-lite"

TOOL_MAP = {
    "get_fpl_bootstrap_data": fpl_tools.get_fpl_bootstrap_data,
    "get_user_squad": fpl_tools.get_user_squad,
    "get_upcoming_fixtures": fpl_tools.get_upcoming_fixtures,
    "get_player_stats": fpl_tools.get_player_stats,
    "analyze_team_form": fpl_tools.analyze_team_form,
    "calculate_expected_points": fpl_tools.calculate_expected_points,
    "suggest_transfers": fpl_tools.suggest_transfers,
}


def execute_tool(tool_name: str, tool_args: dict) -> dict:
    if tool_name not in TOOL_MAP:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return TOOL_MAP[tool_name](**tool_args)
    except Exception as e:
        return {"error": f"Tool execution failed: {str(e)}"}


def run_fpl_agent(manager_id: int, gameweek: int, user_prompt: str = None) -> str:
    system_prompt = f"""
You are an elite Fantasy Premier League (FPL) data scientist and strategist.
GOAL: Maximise points for the upcoming gameweek, respecting the £100m budget,
squad rules (15 players, max 3 per club), and the manager's available free transfers.

Context: Gameweek {gameweek}, Manager ID {manager_id}

WORKFLOW (follow strictly):
1. Call get_user_squad to load the current 15 players, bank, and team value.
2. Call get_upcoming_fixtures to assess the next 3 gameweeks' difficulty.
3. For EACH weak player in the squad, call suggest_transfers to find the best
   fixture-aware, in-form, budget-appropriate replacement.
4. Assume the manager has 1 free transfer unless told otherwise. Recommend the
   single highest-impact transfer for a "no-hit" plan, PLUS one optional -4 hit
   plan if the upside is clearly worth it.
5. Do NOT suggest players the manager already owns.

OUTPUT FORMAT (use this exact clean structure with markdown):

## Gameweek {gameweek} Action Plan

### Current Squad Snapshot
- Team Value / Bank / Free Transfers (state assumption)

### Recommended Transfer (No Hit)
- OUT: [Player, team, price, why weak]
- IN: [Player, team, price, form, next-3 fixture difficulty, why better]
- Net cost: £Xm

### Optional Aggressive Move (-4 Hit)
- OUT / IN / expected extra points, or "Not worth a hit this week"

### Starting XI & Captain
- Captain: [Player] (reason)
- Vice: [Player]
- Any bench changes

### One-Line Summary
- The single most important action to take this week.

Keep it concise, scannable, and data-backed. Avoid long paragraphs.
"""

    messages = [
        {"role": "user", "parts": [system_prompt + f"\n\nUser instruction: {user_prompt or 'Standard optimization.'}"]}
    ]

    tools = [{
        "function_declarations": [
            {"name": "get_fpl_bootstrap_data", "description": "Fetches all FPL data", "parameters": {"type": "OBJECT", "properties": {}}},
            {"name": "get_user_squad", "description": "Retrieves squad", "parameters": {"type": "OBJECT", "properties": {"manager_id": {"type": "INTEGER"}, "gameweek": {"type": "INTEGER"}}, "required": ["manager_id", "gameweek"]}},
            {"name": "get_upcoming_fixtures", "description": "Returns fixtures", "parameters": {"type": "OBJECT", "properties": {"gameweeks": {"type": "INTEGER"}}}},
            {"name": "get_player_stats", "description": "Player stats", "parameters": {"type": "OBJECT", "properties": {"player_id": {"type": "INTEGER"}}, "required": ["player_id"]}},
            {"name": "analyze_team_form", "description": "Team form", "parameters": {"type": "OBJECT", "properties": {"team_id": {"type": "INTEGER"}, "last_n_matches": {"type": "INTEGER"}}}},
            {"name": "calculate_expected_points", "description": "xP calculation", "parameters": {"type": "OBJECT", "properties": {"player_id": {"type": "INTEGER"}, "player_price": {"type": "NUMBER"}, "minutes_expected": {"type": "NUMBER"}, "upcoming_opponent_id": {"type": "INTEGER"}}, "required": ["player_id", "player_price", "minutes_expected", "upcoming_opponent_id"]}},
            {"name": "suggest_transfers", "description": "Fixture-aware transfer suggestions", "parameters": {"type": "OBJECT", "properties": {"current_player_id": {"type": "INTEGER"}, "position": {"type": "STRING"}, "max_price": {"type": "NUMBER"}}, "required": ["current_player_id", "position", "max_price"]}}
        ]
    }]

    model = genai.GenerativeModel(MODEL, tools=tools)

    while True:
        response = model.generate_content(messages)
        if response.candidates[0].content.parts:
            part = response.candidates[0].content.parts[0]
            if part.function_call:
                tool_name = part.function_call.name
                tool_args = {k: v for k, v in part.function_call.args.items()}
                print(f"🔧 Calling tool: {tool_name}")
                result = execute_tool(tool_name, tool_args)
                messages.append({"role": "model", "parts": [part]})
                messages.append({"role": "user", "parts": [{"function_response": {"name": tool_name, "response": result}}]})
            else:
                return part.text
        else:
            break
    return "No recommendation generated"


if __name__ == "__main__":
    # === UPDATE THESE TWO VALUES EACH WEEK ===
    MANAGER_ID = 7261134   # Your FPL manager ID
    GAMEWEEK = 2           # Current gameweek

    print("🚀 Starting FPL AI Data Scientist...\n")
    recommendation = run_fpl_agent(manager_id=MANAGER_ID, gameweek=GAMEWEEK)
    print("\n" + "=" * 80 + "\nGEMINI'S RECOMMENDATION\n" + "=" * 80)
    print(recommendation)
