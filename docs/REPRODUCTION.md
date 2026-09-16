# Reproduction

This document takes you from a clean checkout to a working pipeline.
Every command below was run from a fresh clone before this document was written.
Each section says whether a command is free or paid, and what a paid command costs.

Read the [documentation index](README.md) for the reference documents behind each stage.

## 1. Get the code

```bash
git clone https://github.com/BenWilcox8/arctic-qa.git
cd arctic-qa
```

A clone is about 15 MB: 6 MB of files and the git history.
It holds no source text, no model output and no credential.

## 2. Set up the environment

The pinned way is the Nix flake.
It gives Python 3.13, pytest, ruff and `pdftotext`.

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --version'
```

The command prints `0.1.0`.

If you have no Nix, use plain Python instead.
The package declares no third-party dependency.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/arctic-qa --version
```

You need Python 3.11 or later.
You also need `pdftotext` from `poppler-utils` for the PDF extraction stage.
Install `pytest` in the same environment to run the tests.

The rest of this document writes the Nix form.
With the plain form, replace `nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa ...'` with `.venv/bin/arctic-qa ...`.

## 3. Environment variables

The project reads these variables.
None of them holds a secret value except the two API keys.

| Variable | What it names | Default |
| --- | --- | --- |
| `ARCTIC_QA_DATA_ROOT` | The directory that holds every run: the database, the originals, the receipts and the exports. | `/mnt/crdata/research-abstention`, the mounted drive of the machine that produced the runs of the paper. |
| `ARCTIC_QA_CONFIG_DIR` | The directory that holds the local credential files. | `~/.config/arctic-qa` |
| `GEMINI_API_KEY` | The Gemini API key. The eligibility adapter reads it, and prefers it over the credential file. The shared broker reads the credential file only. | Not set. |
| `ANTHROPIC_API_KEY` | The Anthropic API key. Only the direct Claude transport reads it. | Not set. |
| `ARCTIC_CH2_EVIDENCE_DIR` | The chapter 2 yield-audit evidence bundle. The chapter 2 replay tests skip without it. | The path of the machine that produced the run. |
| `ARCTIC_REAL_CORPUS_DIR` | The corpus search run of the paper. The corpus-viewer tests over the real corpus skip without it. | The path of the machine that produced the run. |
| `ARCTIC_REAL_CORPUS_RUN` | The run identifier inside that corpus. | `20260911T232247Z` |
| `ARCTIC_ZOTERO_RECEIPTS_DIR` | The Zotero custody receipts of that corpus. | The path of the machine that produced the run. |

Set the data root to any writable directory.

```bash
export ARCTIC_QA_DATA_ROOT="$HOME/arctic-qa-data"
mkdir -p "$ARCTIC_QA_DATA_ROOT"
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json doctor'
```

The report gives `"status":"ok"`, the resolved namespace, the `pdftotext` path and whether each key is set.
It never prints a key value.

The built-in example root must be a mounted drive.
A root that you name through `ARCTIC_QA_DATA_ROOT` carries no mount rule.

## 4. Credentials

Free commands need no credential.
Skip this section until you want to run a paid stage.

The shared broker reads the Gemini key from a file, never from the environment.
The file must hold one line, and both the file and its directory must be private.

```bash
mkdir -p ~/.config/arctic-qa && chmod 700 ~/.config/arctic-qa
printf '%s' "$YOUR_GEMINI_KEY" > ~/.config/arctic-qa/gemini-api-key
chmod 600 ~/.config/arctic-qa/gemini-api-key
```

The broker refuses the file when its mode is not `600`, or when the directory is group-readable or world-readable.
The separate Gemini eligibility adapter also accepts `GEMINI_API_KEY` from the environment.

The two subscription evaluation providers use no API key.
Claude Code bills the claude.ai login of its binary, and Codex bills the ChatGPT login of its binary.
The "Subscription providers" section of [ABSTENTION_EVALUATION.md](ABSTENTION_EVALUATION.md) states the login checks.

## 5. Run the test suite

```bash
nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'
```

The suite is free and takes about 20 minutes.
`tests/test_model_broker.py` and `tests/test_streaming.py` hold real rate-limit sleeps, so the run pauses.
The tests that need an external evidence bundle or the mounted corpus skip when it is absent.

The suite is the executable specification of the project.
When you want to know what a contract is, read the test before the code.

Run the linter with the same shell.

```bash
nix develop -c bash -c 'ruff check . && ruff format --check src tests'
```

## 6. A free end-to-end run

The `smoke` command runs the whole pipeline on a synthetic fixture with a fake provider.
It makes no network call and costs nothing.

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json smoke \
  --fixture-dir fixtures --run-id smoke-r1'
```

The result gives `"status":"passed"`.
It walks store, extract, chunk, screen, generate, validate and export.
The export holds two MCQ rows and one short-answer row, each labeled `machine_accepted_unverified`.
The files land under `$ARCTIC_QA_DATA_ROOT/arctic-qa/exports/`.

The fixture is synthetic and marked `test_only`.
This output is infrastructure evidence, not a research result.

## 7. Discovery against the public APIs

Discovery is free.
It calls Crossref and OpenAlex over HTTPS and writes only metadata.

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json discover \
  --adapter crossref --query "Arctic coastal ecology" --pages 1 --per-page 3'
```

The result gives the number of added records and the SHA-256 of the new source manifest.
Use `--adapter replay --input FILE` when the public APIs are unavailable.

## 8. The corpus

The repository does not hold the corpus.
The full texts are copyrighted articles, so they stay on the data root of the run and are not redistributed.
The frozen manifest records the DOI, the retrieval URL and the SHA-256 of each paper, so a reader can get the same articles from their publishers.

The corpus stages are in [CLI_WALKTHROUGH.md](CLI_WALKTHROUGH.md), sections 2 to 5:
discovery, the metadata prefilter, the bounded source pass, the article-access readiness pass, and the freeze.
[CHAPTER2_CORPUS.md](CHAPTER2_CORPUS.md) holds the re-extraction and the freeze receipt of the chapter 2 corpus.

Without the frozen corpus you can still run every stage on your own papers.
The pipeline reads a stored original by its SHA-256 and never depends on where the paper came from.

## 9. The calibration harness in cassette mode

The source-blind judge is calibrated against a labeled set.
Replay is free and makes no call.

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json \
  calibrate-standalone --mode replay \
  --cassette research/arctic-ch3-production-run-r1/standalone-calibration-v2.cassette.v6.jsonl'
```

The report gives `"passed":true`, a must-pass rate of `0.95` over 20 gating rows, and no must-fail violation.
The release rule needs a must-pass rate of `0.80` or more, and zero must-fail violations.

Replay refuses a cassette whose header hash differs from the current `STANDALONE_SYSTEM` text.
A prompt change therefore forces a fresh recording.
Recording is a paid call on the judge model.
[STANDALONE_CALIBRATION.md](STANDALONE_CALIBRATION.md) holds the record command and the review rule.

## 10. A generation run

This stage is paid.
The producer is `stream`, and [STREAMING_DATASET.md](STREAMING_DATASET.md) holds its full command.

Before the first paid call the run needs four reviewed files:

1. A frozen eligibility run directory, from the article-access pass.
2. A streaming budget policy with the allocation ceiling, the session ceiling, the submission cap and the per-paper cost cap.
3. An execution gate that an independent reviewer signed, bound to the code commit and to the campaign.
4. The shared paid-call ledger and its receipts directory.

The broker refuses every request until all four agree.
[SHARED_MODEL_BROKER.md](SHARED_MODEL_BROKER.md) states each refusal and how to settle it.

Measured cost: the chapter 3 expansion measured USD 0.36 for each accepted item over its first 35 papers, against a target of USD 1.00.
The first chapter 3 run was authorized at USD 20, and the expansion at USD 200.
`research/arctic-ch3-expansion-200-r1/report.md` holds the measurement table.

You can read the whole call plan without paying.
[STREAMING_DATASET.md](STREAMING_DATASET.md), section "Chapter 3 call plan", lists every call the producer makes for one paper.

## 11. The abstention benchmark

The evaluator asks each model one item twice: once with the correct option present, and once with it absent.
The abstention option is always listed.

Two actions are free and need no evaluation set.

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json abstention-eval \
  --action list-models --provider anthropic_claude_code'
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json abstention-eval \
  --action pause-status --pause-file config/benchmark-evaluation-model-pause-v1.json'
```

The first prints the registered models and the version of the installed harness binary.
The second prints which models are held now.

An evaluation set is built from the state database of a production run.

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json abstention-eval \
  --action build-set --state-db "$ARCTIC_QA_DATA_ROOT/arctic-qa/state.sqlite3" \
  --output-dir "$ARCTIC_QA_DATA_ROOT/arctic-qa/eval-sets" --population current \
  --contract-file config/abstention-eval-chapter2-contract-v1.json'
```

The builder selects only items of the current contract, so the smoke database gives `"item_count":0`.
A real set needs a production database.

The free dry run drives the whole evaluator with a scripted policy and a private ledger.
It is covered end to end by one test, which is the fastest way to see the path work:

```bash
nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_abstention_run.py -q'
```

The paid run needs a private evaluation gate that an independent reviewer signed.
The Gemini models bill the API key.
The Claude Code and Codex models bill a subscription and record USD 0 for each call.
The live benchmark was authorized at USD 200 for the Gemini share.
[ABSTENTION_EVALUATION.md](ABSTENTION_EVALUATION.md) holds the procedure, from `build-set` to `score`.

## 12. The cost guard

The guard watches the benchmark spend and the subscription quotas.
It pauses one model of the evaluation plan by writing a pause file, and it never stops a process.

One cycle against a recorded quota report is free.

```bash
mkdir -p /tmp/guard-demo/journal /tmp/guard-demo/guard
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa.benchmark_guard --once \
  --journal-dir /tmp/guard-demo/journal --guard-dir /tmp/guard-demo/guard \
  --pause-file /tmp/guard-demo/pause.json \
  --plan-file config/benchmark-evaluation-plan-high-v1.json \
  --recorded-quota-file fixtures/quota-axi-2026-09-16T10-41Z.json'
```

The cycle prints the Gemini spend, the three subscription windows, the paused models and any error.
With no shared ledger selected it reports `no shared paid-call ledger selected` and pauses nothing.

The live guard runs as a systemd user unit against a read-only snapshot of a landed commit.
[BENCHMARK_GUARD.md](BENCHMARK_GUARD.md) holds the unit command and every pause rule.

## 13. What each stage costs

| Stage | Paid | Note |
| --- | --- | --- |
| Test suite, linter | No | About 20 minutes. |
| `smoke` | No | Fake provider, synthetic fixture. |
| `discover` | No | Public Crossref and OpenAlex requests. |
| Metadata prefilter, source pass | No | Metadata only. |
| Extraction and chunking | No | Local `pdftotext`. |
| `calibrate-standalone --mode replay` | No | Reads a recorded cassette. |
| `abstention-eval --action dry-run` | No | Scripted policy, private ledger. |
| `benchmark_guard --once --recorded-quota-file` | No | Reads a saved quota report. |
| Eligibility screening | Yes | One Gemini call for each paper, plus up to two bounded re-asks. |
| `stream` generation | Yes | USD 0.36 for each accepted item, measured over 35 papers. |
| `calibrate-standalone --mode record` | Yes | One judge call for each gating row. |
| Gemini benchmark evaluation | Yes | Authorized at USD 200 for the live run. |
| Claude Code and Codex evaluation | Subscription | USD 0 for each call. It consumes the quota of the login. |

## 14. Where the numbers of the paper come from

Every paid call leaves a receipt in the model-receipts directory, and one row in the shared ledger.
`research/` holds one report for each task that built or corrected the pipeline, with the measurements it made.
[research/README.md](../research/README.md) says what each report backs.
