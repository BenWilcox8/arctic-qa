"""Measure the freeze-time signatures on the 81 frozen chapter 2 quotes.

Chapter 2 yield audit, section 4.5 (c): measure the real signatures before
promoting any check. Reads the family evidence bundles of the audit and runs
the re-derived admission checks of generation.py on every frozen finding.

Usage: python3 freeze-time-signatures.py <audit-evidence-dir> <out.json>
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from arctic_qa import generation  # noqa: E402

EVIDENCE, OUT = sys.argv[1:3]
GOOD = ("machine_accepted_unverified", "incomplete_non_mcq")
counts = Counter()
hits = []
phrase_hits = []
for path in sorted(glob.glob(os.path.join(EVIDENCE, "families", "*.json"))):
    bundle = json.load(open(path))
    good_findings = {
        row["candidate"]["finding_id"] for row in bundle["candidates"] if row.get("status") in GOOD
    }
    for finding in bundle["findings"]:
        answer = finding.get("answer") or json.loads(finding.get("answer_json", "{}"))
        quote = str(answer.get("evidence_quote") or "")
        labelled = bool(answer.get("interpretation_span_ids"))
        good = finding.get("finding_id") in good_findings
        counts["findings"] += 1
        counts["good_findings"] += good
        no_verb = not generation._FINITE_VERB_PATTERN.search(quote)
        words = len(quote.split())
        signatures = {
            "no_finite_verb": no_verb,
            "no_finite_verb_unlabelled": no_verb and not labelled,
            "short_fragment_rule": no_verb and not labelled and words <= generation.MAX_TABLE_FRAGMENT_WORDS,
            "column_interleave_2": len(generation._COLUMN_INTERLEAVE_PATTERN.findall(quote)) >= 2,
            "line_wrap_hyphen": bool(generation._LINE_WRAP_HYPHEN_PATTERN.search(quote)),
            "cyrillic": bool(re.search("[Ѐ-ӿ]", quote)),
        }
        for name, value in signatures.items():
            if value:
                counts[name] += 1
                if good:
                    counts[f"{name}_on_good_item"] += 1
        reason = generation._finding_admission_reason(answer, list(answer.get("interpretation_span_ids") or []))
        if reason:
            counts[f"admission:{reason}"] += 1
            if good:
                counts[f"admission:{reason}_on_good_item"] += 1
            hits.append({"family": bundle["family_id"], "good": good, "reason": reason, "words": words, "quote": quote[:120]})
        for phrase in answer.get("required_question_phrases") or []:
            counts["phrases"] += 1
            if generation._CASE_BOUNDARY_ARTIFACT_PATTERN.search(str(phrase)):
                counts["phrase_case_boundary_artifact"] += 1
                phrase_hits.append({"family": bundle["family_id"], "good": good, "phrase": phrase})
            if generation._LINE_WRAP_HYPHEN_PATTERN.search(str(phrase)):
                counts["phrase_line_wrap_hyphen"] += 1
result = {"counts": dict(counts), "admission_hits": hits, "phrase_hits": phrase_hits}
json.dump(result, open(OUT, "w"), indent=1, ensure_ascii=False)
print(json.dumps(result, indent=1, ensure_ascii=False))
