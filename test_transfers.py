import fpl_tools

result = fpl_tools.suggest_weekly_transfers(manager_id=7261134, gameweek=2, free_transfers=1)

if "error" in result:
    print("ERROR:", result["error"])
else:
    print(f"Team: {result['team_name']}  |  Bank: £{result['bank']}m")
    print(f"\nHit advice: {result['hit_advice']}\n")

    bs = result["best_single"]
    if bs:
        print("=== BEST SINGLE TRANSFER ===")
        print(f"OUT: {bs['out']['name']:25} ({bs['out']['team']}) xP {bs['out']['xp']}")
        print(f"IN:  {bs['in']['name']:25} ({bs['in']['team']}) xP {bs['in']['xp']}")
        print(f"xP gain: +{bs['xp_gain']}  |  cost change: £{bs['cost_change']}m")
    else:
        print("No beneficial single transfer found.")

    bd = result["best_double"]
    if bd:
        print("\n=== BEST DOUBLE TRANSFER ===")
        for m in bd["moves"]:
            print(f"OUT {m['out']['name']:22} -> IN {m['in']['name']:22} (+{m['xp_gain']})")
        print(f"Total xP gain: +{bd['xp_gain']}")