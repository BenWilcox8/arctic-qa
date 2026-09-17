"""Project the dataset onto the two deadlines from the measured rates."""
from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 9, 17, 9, 41, tzinfo=UTC)
DEADLINES = {
    "13:00Z": datetime(2026, 9, 17, 13, 0, tzinfo=UTC),
    "14:00Z": datetime(2026, 9, 17, 14, 0, tzinfo=UTC),
}
GENERATION_STOPS = datetime(2026, 9, 17, 13, 45, tzinfo=UTC)
CLAUDE_RESUMES = datetime(2026, 9, 17, 10, 11, tzinfo=UTC)

ACCEPTED_NOW = 140
ACCEPT_PER_HOUR = 40.0          # 48/h over the last 30 min, 31 to 35/h over longer
COMPLETE_NOW = {"google_gemini": 69, "anthropic_claude_code": 52, "openai_codex": 68}
ALL_THREE_NOW = 44
# Questions an arm can finish in an hour. The ceiling is the evaluation
# policy: 12 calls a minute for each subscription vendor over 18 trials per
# question, and 30 a minute for Gemini over 12 trials per question.
CEILING = {"google_gemini": 150.0, "anthropic_claude_code": 40.0, "openai_codex": 40.0}
MEASURED = {"google_gemini": 60.0, "anthropic_claude_code": 40.0, "openai_codex": 30.0}


def accepted_at(moment):
    end = min(moment, GENERATION_STOPS)
    hours = max((end - NOW).total_seconds() / 3600.0, 0.0)
    return ACCEPTED_NOW + ACCEPT_PER_HOUR * hours


def hours_for(arm, moment):
    start = CLAUDE_RESUMES if arm == "anthropic_claude_code" else NOW
    return max((moment - max(start, NOW)).total_seconds() / 3600.0, 0.0)


def project(rates):
    out = {}
    for label, moment in DEADLINES.items():
        pool = accepted_at(moment)
        arms = {}
        for arm, rate in rates.items():
            arms[arm] = min(COMPLETE_NOW[arm] + rate * hours_for(arm, moment), pool)
        out[label] = (pool, arms, min(arms.values()))
    return out


for name, rates in (("measured", MEASURED), ("policy ceiling", CEILING)):
    print(f"== {name} rates ==")
    for arm, rate in rates.items():
        print(f"   {arm}: {rate:.0f} questions an hour")
    for label, (pool, arms, floor) in project(rates).items():
        print(f"  {label}: accepted {pool:.0f}")
        for arm, value in arms.items():
            print(f"    {arm:24} {value:6.0f} complete "
                  f"({100 * value / pool:4.1f} percent of the pool)")
        print(f"    {'all three arms':24} {floor:6.0f} "
              f"({100 * floor / pool:4.1f} percent)")
    print()
