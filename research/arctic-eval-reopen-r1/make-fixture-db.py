import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("CREATE TABLE candidates (item_id TEXT, status TEXT, run_id TEXT)")
db.executemany(
    "INSERT INTO candidates VALUES (?, 'machine_accepted_unverified', "
    "'arctic-qa-production-campaign-003')",
    [(item,) for item in sys.argv[2:]],
)
db.commit()
