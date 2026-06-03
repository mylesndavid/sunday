"""Memory-palace recall spike.

A small navigable palace (rooms with items + hallways to other rooms). A real
model gets ONLY navigation tools — list_rooms + enter_room — so it can't see
everything at once; it must reason about where a fact lives and walk there.
We then score recall on questions with known answers, some multi-hop.
"""
import asyncio, json, sys
sys.path.insert(0, "/Users/myles/Development/Repos/sunday/src")
from dataclasses import replace
from sunday.config import load_config
from sunday.runtime import build_runtime

# ── the palace: a fictional person's memory, organized as places ──
PALACE = {
    "Foyer": {"blurb": "entry / who this is",
        "items": ["This palace holds what Sunday knows about Dana Okafor."],
        "hallways": {"work life": "Work", "personal life": "Home", "health": "Health"}},
    "Work": {"blurb": "job, company, colleagues",
        "items": ["Dana is a staff ML engineer.",
                  "Dana works at a company called Lumen Robotics.",
                  "Dana joined Lumen in 2023."],
        "hallways": {"the company": "Lumen Robotics", "coworkers": "Work People"}},
    "Lumen Robotics": {"blurb": "the company Dana works at",
        "items": ["Lumen Robotics builds warehouse picking arms.",
                  "Lumen is based in Pittsburgh.",
                  "Lumen's CEO is Priya Vance."],
        "hallways": {"the CEO": "Work People", "the city": "Pittsburgh"}},
    "Work People": {"blurb": "colleagues",
        "items": ["Priya Vance is the CEO of Lumen; she is allergic to peanuts.",
                  "Marco is Dana's manager; Marco bikes to work.",
                  "Dana's desk neighbor is Wen, who speaks Cantonese."],
        "hallways": {"back to company": "Lumen Robotics"}},
    "Home": {"blurb": "family, pets, where Dana lives",
        "items": ["Dana lives in Austin, Texas.",
                  "Dana has a partner named Sam.",
                  "Dana has a cat named Mochi."],
        "hallways": {"the pet": "Pets", "the city": "Austin", "partner's stuff": "Sam"}},
    "Pets": {"blurb": "animals",
        "items": ["Mochi is a gray British Shorthair cat.",
                  "Mochi is on a prescription urinary diet from the vet.",
                  "Mochi's vet is Dr. Halpern at Barton Springs Animal Clinic."],
        "hallways": {}},
    "Sam": {"blurb": "Dana's partner",
        "items": ["Sam is a high-school chemistry teacher.",
                  "Sam's birthday is March 9.",
                  "Sam is vegetarian."],
        "hallways": {}},
    "Austin": {"blurb": "the city Dana lives in",
        "items": ["Austin is in Texas.",
                  "Dana's favorite Austin restaurant is a ramen place called Kemuri."],
        "hallways": {}},
    "Pittsburgh": {"blurb": "where Lumen Robotics is",
        "items": ["Pittsburgh is in Pennsylvania.",
                  "Dana visits the Pittsburgh office once a quarter."],
        "hallways": {}},
    "Health": {"blurb": "Dana's health",
        "items": ["Dana is lactose intolerant.",
                  "Dana runs to stay in shape and is training for a half marathon in October."],
        "hallways": {}},
}

# question, accepted answer token(s) (any present = correct), hops needed
QUESTIONS = [
    ("What company does Dana work at?", ["lumen"], 1),
    ("What city does Dana live in?", ["austin"], 1),
    ("What kind of animal is Mochi and what does Mochi eat?", ["british shorthair", "urinary"], 2),
    ("Dana works at a company — what is that company's CEO allergic to?", ["peanut"], 3),
    ("What is the partner of the person this palace is about allergic to or restricted from eating?", ["vegetarian"], 2),
    ("In what state is the company Dana works at located?", ["pennsylvania"], 3),
    ("What's Dana's favorite restaurant?", ["kemuri"], 2),
    ("Is Dana ok with dairy?", ["lactose", "no", "intolerant"], 1),
]

TOOLS = [
    {"type": "function", "function": {"name": "list_rooms",
        "description": "List every room in the palace with a one-line blurb. The map.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "enter_room",
        "description": "Enter a room to read its items and see the hallways (labeled connections) leading out to other rooms.",
        "parameters": {"type": "object", "properties": {"room": {"type": "string"}}, "required": ["room"]}}},
]

def list_rooms():
    return [{"room": r, "blurb": v["blurb"]} for r, v in PALACE.items()]

def enter_room(room):
    v = PALACE.get(room)
    if not v:
        near = [r for r in PALACE if room.lower() in r.lower() or r.lower() in room.lower()]
        return {"error": f"no room '{room}'", "did_you_mean": near}
    return {"room": room, "items": v["items"],
            "hallways": [{"label": k, "to": d} for k, d in v["hallways"].items()]}

SYS = ("You answer a question by NAVIGATING a memory palace. You cannot see everything — "
       "use list_rooms to see the map, then enter_room to read a room and see its hallways to "
       "connected rooms. Walk to where the answer lives, following hallways across rooms as needed. "
       "Do not guess from prior knowledge — the answer is in the palace. When you have it, reply with "
       "a short final answer (no tool call).")

async def run(rt, question, max_rounds=12):
    messages = [{"role": "user", "content": question}]
    path = []
    for _ in range(max_rounds):
        res = await rt.complete(system_prompt=SYS, messages=messages, tools_schema=TOOLS, purpose="spike")
        if not res.tool_calls:
            return (res.content or "").strip(), path
        messages.append({"role": "assistant", "content": res.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                             "function": {"name": tc.name, "arguments": tc.arguments}} for tc in res.tool_calls]})
        for tc in res.tool_calls:
            try: args = json.loads(tc.arguments or "{}")
            except Exception: args = {}
            if tc.name == "list_rooms": out = list_rooms()
            elif tc.name == "enter_room":
                out = enter_room(args.get("room", "")); path.append(args.get("room", "?"))
            else: out = {"error": f"unknown tool {tc.name}"}
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out)})
    return "(gave up — too many hops)", path

async def main():
    cfg = load_config()
    cfg.model = replace(cfg.model, provider="codex", name="gpt-5.2", reasoning=True)
    rt = build_runtime(cfg)
    hits = 0
    for q, accept, hops in QUESTIONS:
        try:
            ans, path = await run(rt, q)
        except Exception as e:
            print(f"\nQ: {q}\n  ERROR: {e}"); continue
        ok = any(tok in ans.lower() for tok in accept)
        hits += ok
        print(f"\nQ ({hops}-hop): {q}")
        print(f"  walked: {' -> '.join(path) or '(none)'}")
        print(f"  answer: {ans[:160]}")
        print(f"  {'HIT' if ok else 'MISS'} (need one of {accept})")
    print(f"\n=== recall: {hits}/{len(QUESTIONS)} ===")

asyncio.run(main())
