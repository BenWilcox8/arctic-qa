"""Independent verification of the chapter 2 freeze."""

import json
import pathlib
import random
import sys
from arctic_qa.util import canonical_json, sha256_bytes, sha256_file

ROOT = pathlib.Path("/mnt/crdata/research-abstention/arctic-qa/chapter2")
FREEZE = ROOT / "corpus-freeze" / "chapter2-full-text-4420-r1"
ACCESS = ROOT / "article-access" / "chapter2-full-text-4420-r1"
receipt = json.loads((FREEZE / "freeze-receipt.json").read_text())
descriptor = json.loads((FREEZE / "manifest-descriptor.json").read_text())

manifest_path = FREEZE / "chapter2-corpus-manifest.jsonl"
records = [json.loads(line) for line in manifest_path.read_text().splitlines() if line]
ok = True


def check(name, condition, detail=""):
    global ok
    print(("PASS " if condition else "FAIL ") + name + (" " + detail if detail else ""))
    ok = ok and condition


check("manifest records = 4420", len(records) == 4420)
check(
    "manifest sha256 matches the receipt",
    sha256_file(manifest_path) == receipt["hashes"]["manifest_sha256"],
    receipt["hashes"]["manifest_sha256"][:16],
)
check(
    "descriptor manifest sha256 agrees",
    descriptor["manifest_sha256"] == receipt["hashes"]["manifest_sha256"],
)

order = [r["candidate_key"] for r in records]
check(
    "positions are 1..4420 strict",
    [r["manifest_position"] for r in records] == list(range(1, 4421)),
)
check("candidate keys unique", len(set(order)) == 4420)
check(
    "order hash recomputes",
    sha256_bytes(canonical_json(order).encode()) == receipt["hashes"]["order_sha256"],
    receipt["hashes"]["order_sha256"][:16],
)
check(
    "descriptor order hash agrees",
    descriptor["order_sha256"] == receipt["hashes"]["order_sha256"],
)

legacy_order = []
legacy_manifest = pathlib.Path(receipt["derives_from"]["legacy_manifest"]["path"])
with legacy_manifest.open() as handle:
    for line in handle:
        if line.strip():
            row = json.loads(line)
            legacy_order.append(
                (int(row["manifest_position"]), str(row["candidate_key"]))
            )
legacy_order.sort()
check(
    "order equals the chapter 1 frozen order", order == [key for _, key in legacy_order]
)
check(
    "legacy manifest sha256 recomputes",
    sha256_file(legacy_manifest)
    == receipt["derives_from"]["legacy_manifest"]["sha256"],
)

random.seed(20260915)
sample = random.sample(records, 60)
bad = []
for row in sample:
    for path_key, hash_key in (
        ("extraction_path", "extraction_sha256"),
        ("parse_path", "parse_sha256"),
        ("chunk_path", "chunk_sha256"),
    ):
        path = pathlib.Path(row[path_key])
        if not path.is_file() or sha256_file(path) != row[hash_key]:
            bad.append((row["candidate_key"], path_key))
check("60 sampled documents verify all three hashes", not bad, str(bad[:3]))

amanifest = json.loads((ACCESS / "run-manifest.json").read_text())
aprogress = json.loads((ACCESS / "progress.json").read_text())
check(
    "access target_total = selection length",
    amanifest["target_total"] == len(amanifest["selection"]) == 4420,
)
check("access progress completed", aprogress["state"] == "completed")
check("access receipt present", (ACCESS / "run-receipt.json").is_file())
items = sorted((ACCESS / "items").glob("item-*.json"))
check("access items = 4420", len(items) == 4420)

mismatch = []
for position, selected in enumerate(amanifest["selection"], start=1):
    item = json.loads(items[position - 1].read_text())
    if (
        item["schema"] != "article-access-item-v1"
        or item["run_id"] != amanifest["run_id"]
        or item["position"] != position != selected["position"]
        or item["candidate_key"] != selected["candidate_key"]
        or item["subgroup"] != selected["subgroup"]
        or item["identity_verified"] is not True
        or item["access_state"] != "full_text_ready"
        or item["extraction_coverage"]["article_body_recognized"] is not True
    ):
        mismatch.append(position)
check(
    "every access item matches its manifest position", not mismatch, str(mismatch[:5])
)

paths = {
    pathlib.Path(json.loads(p.read_text())["extraction_path"]) for p in items[:200]
}
check(
    "access extraction paths point inside the chapter 2 root",
    all(ROOT in p.parents for p in paths),
)

print("\nVERIFIED" if ok else "\nVERIFICATION FAILED")
sys.exit(0 if ok else 1)
