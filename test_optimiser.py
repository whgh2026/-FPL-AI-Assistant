import fpl_tools

result = fpl_tools.optimise_full_squad(budget=100.0)

if "error" in result:
    print("ERROR:", result["error"])
else:
    print(f"=== OPTIMAL SQUAD (£{result['total_price']}m / £{result['budget']}m, total xP: {result['total_xp']}) ===\n")
    for p in result["squad"]:
        print(f"{p['position']:4} {p['name']:28} £{p['price']:5} xP:{p['xp']:6} ({p['team']})")