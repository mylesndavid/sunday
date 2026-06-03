"""Hardened memory-palace traversal benchmark.

Differences from the first spike:
- ~60 rooms, generated.
- NO global room list. The agent starts in the Foyer and can only `look()` at
  the room it's in and `go(hallway)` to a connected room. It must navigate from
  the entrance — no teleporting by name (that's what leaked answers before).
- Deep multi-hop chains (up to 5 hops), contradictions (stale fact archived,
  current fact in place), and look-alike distractors (two similar companies).
- Scored by difficulty bucket; logs the path walked vs. the shortest path so we
  can see where it wanders / gets lost.
"""
import asyncio, json, random, sys
from collections import deque
sys.path.insert(0, "/Users/myles/Development/Repos/sunday/src")
from dataclasses import replace
from sunday.config import load_config
from sunday.runtime import build_runtime

random.seed(42)

# ── generate a world ──
FIRST = ["Dana","Marco","Wen","Priya","Sam","Iris","Theo","Nadia","Owen","Lila",
         "Kato","Mira","Hugo","Zoe","Raj","Elsa","Bo","Yara","Finn","Cleo"]
CITIES = [("Austin","Texas"),("Pittsburgh","Pennsylvania"),("Boulder","Colorado"),
          ("Tucson","Arizona"),("Salem","Oregon"),("Mobile","Alabama"),("Reno","Nevada")]
ALLERGY = ["peanuts","shellfish","penicillin","bee stings","gluten","dairy","none"]
DIET = ["vegetarian","vegan","pescatarian","keto","no restrictions"]
SPECIES = ["British Shorthair cat","corgi","cockatiel","goldendoodle","tabby cat"]
PETFOOD = ["a prescription urinary diet","grain-free kibble","pellet seed mix","raw diet"]
COMPANIES = ["Lumen Robotics","Lumin Analytics","Harbor Freight Labs","Vega Foods","Tessel AI","Northwind Energy"]
INDUSTRY = ["warehouse robotics","data analytics","logistics software","frozen foods","ML tooling","wind power"]

people = {}
for i, n in enumerate(FIRST):
    people[n] = {
        "name": n, "role": random.choice(["engineer","designer","teacher","analyst","nurse","chef"]),
        "city": random.choice(CITIES), "allergy": random.choice(ALLERGY),
        "diet": random.choice(DIET), "birthday": f"{random.choice(['Jan','Mar','Jul','Sep','Nov'])} {random.randint(1,28)}",
    }
companies = {}
for i, c in enumerate(COMPANIES):
    companies[c] = {"name": c, "industry": INDUSTRY[i], "city": random.choice(CITIES),
                    "ceo": FIRST[i]}  # ceo is a known person
pets = {}
for i in range(5):
    owner = FIRST[i]
    pets[f"{owner}'s pet"] = {"owner": owner, "name": random.choice(["Mochi","Biscuit","Pixel","Waffle","Juno"]),
                              "species": random.choice(SPECIES), "food": random.choice(PETFOOD)}
# employment: each person -> a company
for i, n in enumerate(FIRST):
    people[n]["employer"] = COMPANIES[i % len(COMPANIES)]

# ── lay out rooms + hallways (graph) ──
rooms = {}   # name -> {"items":[...], "halls":{label:dest}}
def room(name, items=None):
    rooms.setdefault(name, {"items": [], "halls": {}})
    if items: rooms[name]["items"] += items
def hall(a, label, b): rooms[a]["halls"][label] = b

room("Foyer", ["This palace holds what Sunday knows about a circle of people, led by Dana."])
for wing in ["Work Wing","Home Wing","Social Wing","Pets Wing","Health Wing"]:
    room(wing, [f"The {wing.lower()}."]); hall("Foyer", wing.replace(" Wing","").lower(), wing)

for n, p in people.items():
    r = f"{n}'s Room"
    room(r, [f"{n} is a {p['role']}.", f"{n}'s birthday is {p['birthday']}.",
             f"{n} is {p['diet']}.", f"{n}'s allergy: {p['allergy']}."])
    # person reachable from Social Wing; first 8 also from Work Wing
    hall("Social Wing", n.lower(), r)
    hall(r, "where they work", f"{p['employer']} (company)")
    hall(r, "their city", f"{p['city'][0]} (city)")
for c, cd in companies.items():
    r = f"{c} (company)"
    room(r, [f"{c} does {cd['industry']}.", f"{c}'s CEO is {cd['ceo']}."])
    hall("Work Wing", c.lower(), r)
    hall(r, "the CEO", f"{cd['ceo']}'s Room")
    hall(r, "headquarters city", f"{cd['city'][0]} (city)")
for cname, state in CITIES:
    r = f"{cname} (city)"
    room(r, [f"{cname} is in {state}."])
for pk, pd in pets.items():
    r = f"{pk}"
    room(r, [f"{pd['owner']}'s pet {pd['name']} is a {pd['species']}.", f"{pd['name']} eats {pd['food']}."])
    hall("Pets Wing", pd['owner'].lower()+" pet", r)
    hall(f"{pd['owner']}'s Room", "their pet", r)

# contradiction: Dana moved. Archive holds the stale city; Dana's Room has current.
room("Archive (old facts)", ["OUTDATED: Dana used to live in Reno, Nevada (moved away)."])
hall("Foyer", "archive", "Archive (old facts)")
people["Dana"]["city"] = ("Austin","Texas")
rooms["Dana's Room"]["items"].append("Dana currently lives in Austin, Texas (as of this year).")
rooms["Dana's Room"]["halls"]["their city"] = "Austin (city)"

# distractor: Lumen Robotics vs Lumin Analytics (look-alike). Make CEOs distinct + memorable.
rooms["Lumen Robotics (company)"]["items"].append("Lumen Robotics' CEO Dana is known for kicking off standups with a joke.")
rooms["Lumin Analytics (company)"]["items"].append("Lumin Analytics' CEO Marco is known for never using slides.")

# ── BFS shortest hop distance from Foyer ──
def hops_to(target):
    seen, q = {"Foyer"}, deque([("Foyer", 0)])
    while q:
        r, d = q.popleft()
        if r == target: return d
        for nb in rooms.get(r, {}).get("halls", {}).values():
            if nb not in seen and nb in rooms: seen.add(nb); q.append((nb, d+1))
    return 99

# ── questions (answer tokens, type, the room the answer lives in) ──
D = people["Dana"]
QS = [
    ("What is Dana's role?", [D["role"]], "easy", "Dana's Room"),
    ("What is Dana's pet and what does it eat?", [pets["Dana's pet"]["species"].split()[-1], pets["Dana's pet"]["food"].split()[-1]], "mid", "Dana's pet"),
    ("What industry is the company Dana works at in?", [companies[D["employer"]]["industry"].split()[-1]], "mid", f"{D['employer']} (company)"),
    ("What is the diet of the CEO of the company Dana works at?", [people[companies[D["employer"]]["ceo"]]["diet"].split()[0]], "hard", f"{companies[D['employer']]['ceo']}'s Room"),
    ("What allergy does the CEO of the company Dana works at have?", [people[companies[D["employer"]]["ceo"]]["allergy"].split()[0]], "hard", f"{companies[D['employer']]['ceo']}'s Room"),
    ("What state is the headquarters city of the company Dana works at in?", [dict(CITIES)[companies[D["employer"]]["city"][0]]], "hard", f"{companies[D['employer']]['city'][0]} (city)"),
    ("What city does Dana live in NOW (current, not old)?", ["austin"], "contradiction", "Dana's Room"),
    ("Who is the CEO of Lumin Analytics (the analytics company, not the robotics one)?", ["marco"], "distractor", "Lumin Analytics (company)"),
    ("Who is the CEO of Lumen Robotics (the robotics company)?", ["dana"], "distractor", "Lumen Robotics (company)"),
    ("What is Wen's birthday?", [people["Wen"]["birthday"].split()[0].lower()], "mid", "Wen's Room"),
    ("What does Priya's pet eat?", [pets["Priya's pet"]["food"].split()[-1]] if "Priya's pet" in pets else ["__none__"], "mid", "Priya's pet" if "Priya's pet" in pets else "Foyer"),
    ("What is the diet of the CEO of the company Wen works at?", [people[companies[people['Wen']['employer']]['ceo']]['diet'].split()[0]], "hard", f"{companies[people['Wen']['employer']]['ceo']}'s Room"),
]

TOOLS = [
    {"type":"function","function":{"name":"look","description":"Look at the room you're currently standing in: its written items, and the labeled hallways (doors) leading to other rooms.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"go","description":"Walk through a hallway/door by its label to the connected room. You can only go through hallways shown in the current room's look().","parameters":{"type":"object","properties":{"hallway":{"type":"string"}},"required":["hallway"]}}},
]
SYS = ("You are standing in a memory palace and must answer a question by WALKING to where the answer is. "
       "You start in the Foyer. There is no map — use look() to see the current room's items and its labeled "
       "hallways, and go(hallway) to walk through a door to a connected room. Reason about which door leads "
       "toward the answer; follow hallways across rooms for multi-step questions. Prefer CURRENT facts over "
       "anything marked OUTDATED/old. When you've found the answer, reply with a short final answer and no tool call.")

async def run(rt, q, max_steps=12):
    here = "Foyer"; path = ["Foyer"]
    messages = [{"role":"user","content": f"{q}\n\n(You are in the Foyer.)"}]
    for _ in range(max_steps):
        res = await rt.complete(system_prompt=SYS, messages=messages, tools_schema=TOOLS, purpose="harden")
        if not res.tool_calls:
            return (res.content or "").strip(), path
        messages.append({"role":"assistant","content":res.content or "",
            "tool_calls":[{"id":tc.id,"type":"function","function":{"name":tc.name,"arguments":tc.arguments}} for tc in res.tool_calls]})
        for tc in res.tool_calls:
            try: a = json.loads(tc.arguments or "{}")
            except Exception: a = {}
            if tc.name == "look":
                rd = rooms.get(here, {})
                out = {"room": here, "items": rd.get("items", []),
                       "hallways": [{"door": k, "leads_to": v} for k, v in rd.get("halls", {}).items()]}
            elif tc.name == "go":
                label = a.get("hallway","")
                dest = rooms.get(here, {}).get("halls", {}).get(label)
                if not dest:  # fuzzy match a door label
                    for k, v in rooms.get(here, {}).get("halls", {}).items():
                        if label.lower() in k.lower() or k.lower() in label.lower(): dest = v; break
                if dest and dest in rooms:
                    here = dest; path.append(here); out = {"moved_to": here}
                else:
                    out = {"error": f"no door '{label}' here", "doors": list(rooms.get(here,{}).get("halls",{}).keys())}
            else: out = {"error":"unknown"}
            messages.append({"role":"tool","tool_call_id":tc.id,"content":json.dumps(out)})
    return "(lost — too many steps)", path

async def main():
    cfg = load_config()
    cfg.model = replace(cfg.model, provider="openrouter", name="deepseek/deepseek-v4-flash", reasoning=False)
    rt = build_runtime(cfg)
    print(f"palace: {len(rooms)} rooms\n")
    from collections import defaultdict
    bucket = defaultdict(lambda: [0,0])
    for q, accept, typ, ansroom in QS:
        if accept == ["__none__"]: continue
        try: ans, path = await run(rt, q)
        except Exception as e: print(f"Q: {q}\n  ERROR {e}\n"); continue
        ok = any(t.lower() in ans.lower() for t in accept)
        sp = hops_to(ansroom)
        bucket[typ][0] += ok; bucket[typ][1] += 1
        print(f"[{typ}] {q}")
        print(f"  shortest={sp} hops | walked {len(path)-1}: {' -> '.join(path)}")
        print(f"  answer: {ans[:140]}")
        print(f"  {'HIT' if ok else 'MISS'} (need {accept})\n")
    print("=== by bucket ===")
    tot=[0,0]
    for k,(h,n) in bucket.items(): print(f"  {k}: {h}/{n}"); tot[0]+=h; tot[1]+=n
    print(f"  OVERALL: {tot[0]}/{tot[1]}")

asyncio.run(main())
