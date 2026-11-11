"""A demo memory with neutral text: eight past sessions, then one session in which the agent recalls,
finds, opens and compacts over four turns. A fifth turn runs under a turn budget, which cuts its second
open. No model and no network.

    python3 demo/seed.py DIR
    python3 -m breadcrumbs --home DIR/breadcrumbs serve --open     # tick "context" to see the Gantt and Venn
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # no summaries: the demo needs no model
from breadcrumbs.hermes import BreadcrumbsProvider  # noqa: E402

PAST = {
    "tea": [("The kettle boils too fast and my green tea turns bitter.",
             "Green tea likes water at about 80 C. Let the kettle rest for three minutes, then steep two minutes."),
            ("Can I steep the same leaves twice?", "Yes. The second steep needs about thirty seconds more."),
            ("Is oolong the same?", "Oolong takes hotter water, about 90 C. Steep oolong for three minutes.")],
    "tea-2": [("Which tea needs boiling water from the kettle?",
               "Black tea and herbal tea need a full boil. Oolong needs 90 C. Green tea turns bitter at a full boil."),
              ("And oolong leaves?", "Rinse oolong leaves once, then steep them.")],
    "bike": [("My bike chain skips on the small cog when I climb.",
              "A worn chain skips under load. Measure the stretch with a chain gauge; replace it at 0.75 percent."),
             ("The new chain still skips on the smallest cog.", "Then the cog is worn too. Replace the cassette."),
             ("Should I adjust the derailleur?", "Only if the chain skips on every cog: then the derailleur needs a tune. "
              "Turn the derailleur barrel a quarter turn.")],
    "bike-2": [("How often should I oil the bike chain?",
                "Oil the chain every 300 km, or after rain. Wipe off the extra oil, or the chain collects grit."),
               ("And the derailleur?", "Oil the derailleur pivots once a month; keep oil off the derailleur pulleys.")],
    "garden": [("The tomato leaves curl and turn yellow at the edges.",
                "Water deeply twice a week instead of a little every day, and add mulch to keep the soil moist.")],
    "parser": [("The parser crashes on an empty line at the end of the file.",
                "The crash comes from split_lines in reader.py.\n"
                "It drops the last line when the file ends with an empty line.\n"
                "The parser then reads one line past the end of the list and raises IndexError.\n"
                "The fix keeps a trailing newline as its own empty line, so the line count matches the file.\n"
                "I added two tests: a file that ends with one newline, and a file that ends with two newlines.\n"
                "Both pass on CPython and GraalPy; the change is four lines in reader.py."),
               ("The tests pass now on CPython and GraalPy.", "Good. I added a test for a file that ends with two newlines.")],
    "trip": [("Plan a two-day walk on the coast path in May.",
              "Day one: 18 km from the harbour to the lighthouse. Day two: 14 km to the bay, then the bus back.")],
    "bread": [("My sourdough is dense and flat.",
               "Let the starter double before you mix, and give the dough four folds in the first two hours.")],
}
NOW = "today"
CALL = {"id": "call_1", "type": "function", "function": {"name": "mcp__crumb_find", "arguments": '{"q": "chain skips cog"}'}}


def main(home):
    p = BreadcrumbsProvider()
    p.initialize("tea", hermes_home=home, platform="cli", agent_context="primary")
    tick = iter(range(1_794_000_000, 1_794_000_000 + 10 ** 7, 37))  # fixed times: the same store every run
    p.clock = lambda: next(tick)
    for sid, turns in PAST.items():
        p.on_session_switch(sid)
        for user, answer in turns:
            p.sync_turn(user, answer, session_id=sid)
    p.on_session_switch(NOW, reset=True)
    q = "My green tea is bitter again. What did we say about the kettle?"
    p.prefetch(q, session_id=NOW)
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("tea"), "level": 0.6})
    p.sync_turn(q, "Let the kettle rest three minutes, so the water is near 80 C, and steep two minutes.", session_id=NOW)
    q = "And the bike: the chain skips again on the small cog."
    p.prefetch(q, session_id=NOW)
    p.handle_tool_call("mcp__crumb_find", {"q": "chain skips cog"})
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("bike"), "level": 1.0})
    a = "Last time a new chain did not help: replace the cassette."
    p.sync_turn(q, a, session_id=NOW, messages=[
        {"role": "user", "content": q},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "tool_call_id": "call_1", "name": "mcp__crumb_find", "content": "bike chain hits"},
        {"role": "assistant", "content": a}])
    p.on_pre_compress([])  # the context was compacted
    q = "Which day of the coast walk was longer?"
    p.prefetch(q, session_id=NOW)
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("trip"), "level": 1.0})
    p.sync_turn(q, "Day one: 18 km. Day two is 14 km.", session_id=NOW)
    q = "Remind me how to fix the dense sourdough."
    p.prefetch(q, session_id=NOW)
    p.sync_turn(q, "Let the starter double before you mix, and fold the dough four times.", session_id=NOW)
    p.turn_factor = 1.0  # the last turn has a turn budget of one recall budget
    q = "What was the fix in reader.py?"
    p.prefetch(q, session_id=NOW)
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("parser"), "level": 0.3})
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("parser"), "level": 1.0})  # the turn is full: cut
    p.turn_factor = 0.0
    p.sync_turn(q, "It keeps the last line of the file, and two tests check it.", session_id=NOW)
    p.shutdown()
    root = os.path.join(home, "breadcrumbs")
    print("seeded %s\nnext:  python3 -m breadcrumbs --home %s serve --open" % (root, root))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
