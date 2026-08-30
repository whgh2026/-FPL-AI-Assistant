import fpl_tools

print("=== TOP 10 PLAYERS BY xP ===")
result = fpl_tools.rank_players_by_xp(limit=10)
for p in result.get("players", []):
    print(f"{p['name']:25} {p['position']:4} £{p['price']:4} xP:{p['xp']:5} ({p['team']})")
