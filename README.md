# Arctic QA

Arctic QA builds local, source-supported scientific QA and MCQ records.
Arctic science is the first domain configuration.
The project does not claim that Arctic science is underrepresented in model training.

The current implementation is infrastructure only.
It has not produced a research dataset or evaluated a model.
The strongest automated output label is `machine_accepted_unverified`.

## Safety boundary

The default data root is `/mnt/crdata/research-abstention`.
The CLI writes project data only under its `arctic-qa` namespace.
It refuses the default root when the expected mounted drive is absent or read-only.
It never uses a root-disk fallback.

The CLI permits a different root only with `--test-mode`.
Use this option only for tests and disposable fixtures.

The pipeline treats source content as untrusted data.
Provider prompts tell models not to obey source instructions or call tools.

## Development shell

Enter the pinned Nix shell:

```bash
nix develop
```

Run the CLI from the repository:

```bash
PYTHONPATH=src python -m arctic_qa --help
PYTHONPATH=src python -m arctic_qa --json doctor
```

The shell supplies Python, pytest, and `pdftotext`.
The project does not require a server, vector database, GPU, or parser model.

The optional read-only corpus-stage viewer uses only the Python standard library.
See `docs/CORPUS_VIEWER.md` for its artifact boundary and start command.

## Staged workflow

All commands use the mounted default root unless you supply a test root.

### 1. Check the environment

```bash
PYTHONPATH=src python -m arctic_qa --json doctor
```

The result reports only whether provider keys are set.
It never prints key values.

### 2. Discover sources

Look up the two collaborator DOI seeds:

```bash
PYTHONPATH=src python -m arctic_qa --json discover \
  --adapter crossref \
  --doi 10.1111/j.1365-2419.2005.00365.x \
  --doi 10.1016/j.rsase.2025.101797
```

The DOI records are discovery seeds.
They do not receive automatic geographic acceptance.

Run a bounded Crossref query:

```bash
PYTHONPATH=src python -m arctic_qa --json discover \
  --adapter crossref \
  --query "Arctic coastal ecology" \
  --pages 2 \
  --per-page 5
```

Expand references from one DOI:

```bash
PYTHONPATH=src python -m arctic_qa --json discover \
  --adapter crossref \
  --references-of 10.1111/j.1365-2419.2005.00365.x \
  --max-references 5
```

Use `--adapter replay --input FILE` when public APIs are unavailable.
Use `--adapter manual --input FILE` for supplied metadata.

An optional Zotero custody bridge reads `library-originals/catalog.tsv`:

```bash
PYTHONPATH=src python -m arctic_qa --json discover \
  --adapter catalog \
  --input /mnt/crdata/research-abstention/library-originals/catalog.tsv
```

The command reads public rows only.
It does not modify the archive or create a second Zotero library.

### 3. Fetch an original

Final geographic screening needs source-bound evidence.
Fetch and extract the source before you mark it eligible.

```bash
PYTHONPATH=src python -m arctic_qa --json fetch \
  --source-id SOURCE_ID \
  --url 'https://publisher.example/article.xml' \
  --media-type application/xml
```

Production fetch accepts HTTPS only.
It checks every redirect and the final URL.
Tests can read one explicit file URL only with global `--test-mode` and `--allow-test-file`.

The maximum original size is 50 MiB by default.
The CLI stores each original by SHA-256 and never replaces different content at the same immutable path.

### 4. Extract and chunk

```bash
PYTHONPATH=src python -m arctic_qa --json extract \
  --source-id SOURCE_ID \
  --char-cap 6000 \
  --overlap-chars 500
```

Use publisher JATS first.
Use publisher HTML when JATS is unavailable.
Use born-digital PDF text as the last authorized fallback.

The extractor retains section IDs, heading paths, pages, block numbers, offsets, object labels, hashes, and warnings.
Generation prefers prose chunks without table, figure, or equation labels.

### 5. Screen geography

Create an evidence file from a study setting or source coordinates:

```json
{
  "evidence_kind": "site_coordinates",
  "latitudes": [71.3],
  "named_regions": [],
  "source_content_hash": "SOURCE_SHA256",
  "evidence_quote": "Exact study-setting text from the extracted chunk.",
  "locator": {
    "chunk_id": "CHUNK_ID",
    "start_offset": 120,
    "end_offset": 177
  },
  "site_coverage": "complete"
}
```

Apply the rule:

```bash
PYTHONPATH=src python -m arctic_qa --json screen \
  --source-id SOURCE_ID \
  --evidence geography.json
```

The version 1 rule uses 66.56 degrees north and a configurable marine list.
The marine list is a proposed corpus control.
It is not a universal definition of Arctic science.

Titles, keywords, affiliations, and collaborator status cannot establish acceptance.
The hash, chunk, offsets, and quote must resolve against stored content.
Latitude values outside minus 90 through 90 are invalid.
Mixed core and noncore site sets are excluded even when the input omits a mixed label.
Partial site coverage stays pending.
The default generation queue excludes unresolved, mixed, and subarctic-related records.

### 6. Generate candidates

The versioned role defaults are in `config/roles.v1.json`.
The strongest profile uses `claude-opus-5` for authoring.
It uses `gemini-3.1-pro-preview` for independent reconstruction, answer checks, and exact-option checks.

The cost-aware profile uses `claude-sonnet-5` and `gemini-3.8-flash`.
These assignments are unvalidated starting configurations.

Set keys only in the environment or another private supported input:

```bash
export ANTHROPIC_API_KEY='...'
export GEMINI_API_KEY='...'
```

Do not put keys in repository files, command output, manifests, or exports.

Live mode requires credentials and an explicit positive budget:

```bash
PYTHONPATH=src python -m arctic_qa --json generate \
  --source-id SOURCE_ID \
  --run-id pilot-r1 \
  --arm answer_first \
  --author-provider claude \
  --author-model claude-opus-5 \
  --verifier-provider gemini \
  --verifier-model gemini-3.1-pro-preview \
  --budget-mode tokens \
  --budget-limit 50000 \
  --reservation 5000 \
  --max-output-tokens 2048 \
  --reasoning-token-cap 2048 \
  --billable-token-overhead 1024
```

The user reservation is only a floor.
It cannot reduce the computed bound.
Before dispatch, the CLI calculates a UTF-8 byte upper bound for the full input.
The bound also includes output, reasoning, billable overhead, and all retry attempts.
This bound is conservative because one input token cannot contain less than one encoded byte.
The reasoning floor equals the output cap.
The billable-overhead floor is 1,024 tokens.

USD mode also needs all three configured prices:

```text
--input-price-per-million PRICE
--output-price-per-million PRICE
--reasoning-price-per-million PRICE
```

The CLI refuses a USD request before dispatch when a price is absent.
An unexpected provider overage is recorded and stops later calls.

Use `--arm direct_joint` for the required baseline.
Each run freezes one proposed finding per paper family.
Both arms use the same finding record, reconstruction, and acceptance gates.
The pipeline retains a failed finding instead of sampling a replacement.
If another source version in that family requests generation, the command stops and identifies the source version that owns the frozen finding.

The reconstructor does not receive the proposed answer.
It must return source-located evidence, scope, alternatives, and an independently assigned question claim type.
The answer verifier receives the proposed answer and reconstruction.
All QA gates run before distractor generation.
After generation, the verifier receives each exact displayed option in a separate call.
Each answer-verification and option-verification record must resolve to its completed call receipt.
Each option verdict binds to the stored source, QA, displayed option, prompt, provider, model, request, and response payload.
The stored option response must be a complete schema-valid object that equals the recorded verdict fields.
Incomplete objects and JSON arrays fail closed.
Author verification flags have no acceptance authority.
A scope substitution can remain a valid distractor when it is false for this question, even if it is true in another location or period.

The pipeline records each call before dispatch.
A response must pass the complete role-specific nested schema before completion.
A timeout after dispatch creates an `ambiguous_charge` receipt.
The pipeline does not retry that receipt as a free request.

### 7. Validate candidates

```bash
PYTHONPATH=src python -m arctic_qa --json validate --item-id ITEM_ID
```

The default release policy requires executable distractor incompatibility.
Nonnumeric deterministic checks use a typed rule on the source-located answer.
They reject negated or disjunctive displayed assertions and ignore self-asserted allowed or excluded values from a distractor.
Numeric deterministic checks reject negated, multi-quantity, or disjunctive displayed assertions because one metadata value cannot bind them safely.
Model-only contradiction has the `model-verified` label and a residual-error notice.
It does not receive deterministic or certain status.

Candidate IDs include the run ID, so identical content from separate runs has a separate stored record.
`validate --candidate FILE` validates an external payload without changing stored candidate status or writing a release validation event.
Stored validation events include the exact candidate payload hash, and export requires that hash to match.

One item can use one component-only correction after hard gates pass.
The candidate must fail exactly one declared remediable component gate.
An absent or false independent source-entailment result stays unresolved.
Unsafe version 1 candidates are rejected because they lack exact-option verifier bindings.
If fewer than three distractors pass, the short-answer item remains accepted and the MCQ is withheld.

### 8. Export records

```bash
PYTHONPATH=src python -m arctic_qa --json export \
  --run-id pilot-r1 \
  --seed arctic-qa-v1
```

The export keeps short-answer records as a first-class file.
An answer-present MCQ requires three accepted distractors.
An answer-absent form requires four accepted distractors.

The absent form has the `invalid_option_set` label.
It does not establish model ignorance or absent real-world evidence.

### 9. Resume and inspect status

```bash
PYTHONPATH=src python -m arctic_qa --json resume --run-id pilot-r1
PYTHONPATH=src python -m arctic_qa --json status --run-id pilot-r1
```

Completed calls are reused by run, entity, role, and prompt hash.
Ambiguous calls require manual charge reconciliation before a retry.

## Smoke workflows

Run the complete offline fake-provider workflow:

```bash
PYTHONPATH=src python -m arctic_qa --json smoke \
  --fixture-dir fixtures \
  --run-id smoke-r1
```

Add two bounded public Crossref lookups:

```bash
PYTHONPATH=src python -m arctic_qa --json smoke \
  --fixture-dir fixtures \
  --run-id public-smoke-r1 \
  --public
```

The fixture is synthetic, CC0, and marked `test_only`.
Smoke output is infrastructure evidence, not a research result.

## Data layout

The CLI creates these paths under `/mnt/crdata/research-abstention/arctic-qa`:

- `state.sqlite3` contains resumable state and small records.
- `originals/` contains immutable original objects and retrieval manifests.
- `parsed/` contains immutable section JSONL.
- `chunks/` contains immutable chunk JSONL.
- `manifests/` contains content-addressed source manifests.
- `runs/` contains run receipts.
- `replay/` contains public discovery responses for offline replay.
- `exports/` contains deterministic JSONL exports.
- `backups/` contains database backups before future schema changes.

Large source text and logs stay on the mounted drive.
The repository contains only small synthetic fixtures.

## Automated labels

The validator stores separate states for schema, evidence, scope, reconstruction, contradiction, and alternative-answer search.
It also stores `rejected`, `unresolved`, and `machine_accepted_unverified` outcomes.

The system never emits `CERTAINLY_TRUE` or `CERTAINLY_FALSE`.
It never converts model votes into a confidence probability.

Read [Method traceability](docs/METHODS.md) for the evidence basis and limits.
