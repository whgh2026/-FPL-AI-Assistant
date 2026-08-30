from google.genai import types

function_declarations = [
    types.FunctionDeclaration(
        name="get_fpl_bootstrap_data",
        description="Fetches all FPL data: players, teams, gameweeks, fixtures, and their stats.",
        parameters=types.Schema(
            type="OBJECT",
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="get_user_squad",
        description="Retrieves a specific manager's current FPL squad, bank balance, and transfer info for a given gameweek.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "manager_id": types.Schema(type="INTEGER", description="The FPL manager ID (6-digit number from your team URL)"),
                "gameweek": types.Schema(type="INTEGER", description="Gameweek number (1-38)"),
            },
            required=["manager_id", "gameweek"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_upcoming_fixtures",
        description="Returns upcoming fixtures for the next N gameweeks with difficulty ratings and opponent info.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "gameweeks": types.Schema(type="INTEGER", description="Number of gameweeks to look ahead (default 3)"),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_player_stats",
        description="Fetches detailed performance stats for a single player: recent form, injuries, and upcoming fixtures.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "player_id": types.Schema(type="INTEGER", description="The FPL player ID"),
            },
            required=["player_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="analyze_team_form",
        description="Analyzes a Premier League team's recent form: goals for/against, defensive strength.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "team_id": types.Schema(type="INTEGER", description="FPL team ID (1-20)"),
                "last_n_matches": types.Schema(type="INTEGER", description="Number of recent matches to analyze (default 5)"),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="calculate_expected_points",
        description="Calculates expected Fantasy Points for a player based on form, minutes, and opponent difficulty.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "player_id": types.Schema(type="INTEGER", description="FPL player ID"),
                "player_price": types.Schema(type="NUMBER", description="Player cost in millions"),
                "minutes_expected": types.Schema(type="NUMBER", description="Expected minutes to play (0-90)"),
                "upcoming_opponent_id": types.Schema(type="INTEGER", description="Defending team's ID"),
            },
            required=["player_id", "player_price", "minutes_expected", "upcoming_opponent_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="suggest_transfers",
        description="Suggests top replacement players for a given position within budget.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "current_player_id": types.Schema(type="INTEGER", description="ID of player to replace"),
                "position": types.Schema(type="STRING", enum=["GK", "DEF", "MID", "FWD"], description="Player position"),
                "max_price": types.Schema(type="NUMBER", description="Maximum price in millions"),
            },
            required=["current_player_id", "position", "max_price"],
        ),
    ),
]
