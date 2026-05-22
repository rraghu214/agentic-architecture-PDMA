"""
run.py — PMDA Agent6 scenario runner.

Usage:
    python run.py A        # Claude Shannon Wikipedia (artifact attach)
    python run.py B        # Tokyo activities + weather (multi-goal)
    python run.py C1       # Mom's birthday - store (durable memory write)
    python run.py C2       # Mom's birthday - recall (cross-run, NO clean state)
    python run.py D        # Python asyncio best practices (multi-artifact)
    python run.py 0        # Step 0 - simulate agent5 failure modes
"""
import asyncio
import sys

SCENARIOS = {
    "A": {
        "num": 1,
        "label": "Query A - Claude Shannon Wikipedia (artifact attach)",
        "query": (
            "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me "
            "his birth date, death date, and three key contributions to information theory."
        ),
    },
    "B": {
        "num": 2,
        "label": "Query B - Tokyo activities + weather (multi-goal)",
        "query": (
            "Find 3 family-friendly things to do in Tokyo this weekend. "
            "Check Saturday's weather forecast there and tell me which one is most appropriate."
        ),
    },
    "C1": {
        "num": 3.1,
        "label": "Query C Run 1 - Mom's birthday store (durable memory write)",
        "query": (
            "My mom's birthday is 15 May 2026. Remember that and give me a calendar "
            "reminder for two weeks before and on the day."
        ),
    },
    "C2": {
        "num": 3.2,
        "label": "Query C Run 2 - Mom's birthday recall (cross-run, zero tool calls expected)",
        "query": "When is mom's birthday?",
    },
    "D": {
        "num": 4,
        "label": "Query D - Python asyncio best practices (multi-artifact synthesis)",
        "query": (
            "Search for 'Python asyncio best practices', read the top 3 results, "
            "and give me a short numbered list of the advice they agree on."
        ),
    },
}


def main():
    if len(sys.argv) < 2 or sys.argv[1].upper() not in (set(SCENARIOS) | {"0"}):
        print(__doc__)
        print("Available scenarios:", ", ".join(["0"] + list(SCENARIOS)))
        sys.exit(1)

    key = sys.argv[1].upper()

    if key == "0":
        import simulate_agent5_failure
        simulate_agent5_failure.main()
        return

    s = SCENARIOS[key]
    print(f"\n{'='*60}")
    print(f"  {s['label']}")
    print(f"{'='*60}\n")

    import agent6
    asyncio.run(agent6.run(s["query"], scenario_num=s["num"]))


if __name__ == "__main__":
    main()
