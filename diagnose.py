import fpl_tools

bootstrap = fpl_tools._get_bootstrap()

print("=== TEAM STRENGTH VALUES ===")
for t in bootstrap.get("teams", []):
    print(f"{t['name']:20} strength={t.get('strength')}")

print("\n=== SAMPLE PLAYER DATA (first 5 with minutes > 0) ===")
count = 0
for p in bootstrap.get("elements", []):
    mins = int(p.get("minutes", 0) or 0)
    if mins > 0:
        print(f"{p['first_name']} {p['second_name']:20} "
              f"team={p.get('team')} mins={mins} starts={p.get('starts')} "
              f"xg90={p.get('expected_goals_per_90')} xa90={p.get('expected_assists_per_90')}")
        count += 1
    if count >= 5:
        break

print(f"\nTotal players with minutes > 0: "
      f"{sum(1 for p in bootstrap.get('elements', []) if int(p.get('minutes', 0) or 0) > 0)}")
print(f"Total players with starts >= 1: "
      f"{sum(1 for p in bootstrap.get('elements', []) if int(p.get('starts', 0) or 0) >= 1)}")