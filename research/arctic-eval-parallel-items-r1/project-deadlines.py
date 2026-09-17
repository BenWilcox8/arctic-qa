"""Two plans for the deadline, from the rates measured on fresh questions."""
from datetime import UTC, datetime

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
DEADLINES = (
    ("13:00Z", datetime(2026, 9, 17, 13, 0, tzinfo=UTC)),
    ("14:00Z", datetime(2026, 9, 17, 14, 0, tzinfo=UTC)),
)
CLAUDE_RESUMES = datetime(2026, 9, 17, 10, 11, tzinfo=UTC)
ACCEPTED_NOW = 151
ACCEPT_PER_HOUR = 40.0
COMPLETE_NOW = {"google_gemini": 71, "anthropic_claude_code": 52, "openai_codex": 73}
# Questions an hour. The Gemini arm is bound by the exclusive section of the
# shared ledger while the producer writes to it, and by its own policy pace
# after the producer stops. The subscription arms are bound by their policy
# pace, which no other process touches.
GEMINI_WITH_PRODUCER = 15.0
GEMINI_ALONE = 40.0
CLAUDE = 31.0
CODEX = 31.0


def hours(start, end):
    return max((end - start).total_seconds() / 3600.0, 0.0)


def plan(label, generation_stops):
    print(f"== {label}: generation stops {generation_stops:%H:%M}Z ==")
    for name, deadline in DEADLINES:
        pool = ACCEPTED_NOW + ACCEPT_PER_HOUR * hours(NOW, min(generation_stops, deadline))
        gemini = (
            COMPLETE_NOW["google_gemini"]
            + GEMINI_WITH_PRODUCER * hours(NOW, min(generation_stops, deadline))
            + GEMINI_ALONE * hours(max(generation_stops, NOW), deadline)
        )
        claude = COMPLETE_NOW["anthropic_claude_code"] + CLAUDE * hours(
            max(CLAUDE_RESUMES, NOW), deadline
        )
        codex = COMPLETE_NOW["openai_codex"] + CODEX * hours(NOW, deadline)
        arms = {
            "google_gemini": min(gemini, pool),
            "anthropic_claude_code": min(claude, pool),
            "openai_codex": min(codex, pool),
        }
        floor = min(arms.values())
        print(f"  {name}: accepted {pool:.0f}")
        for arm, value in arms.items():
            print(f"    {arm:24} {value:6.0f}")
        print(f"    {'all three arms':24} {floor:6.0f} "
              f"({100 * floor / pool:4.0f} percent of the pool)")
    print()


plan("the captain's plan", datetime(2026, 9, 17, 13, 45, tzinfo=UTC))
plan("stop generation at 11:00Z", datetime(2026, 9, 17, 11, 0, tzinfo=UTC))
plan("stop generation now", NOW)
