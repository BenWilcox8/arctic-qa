# Chapter 2 corpus

The chapter 2 corpus is a new extraction of the same stored article objects.
It does not change, move, or remove a chapter 1 object.

## Why the corpus was extracted again

The chapter 1 extractor called `pdftotext -layout`.
That mode keeps the physical page layout.
On a two-column page it writes one output line for each visual line.
The left column and the right column then join on one line, with a run of spaces between them.

The r15 holistic acceptance audit measured the result.
A column gutter was present in 81 of 95 evidence quotes.
The same text carried soft hyphens, ligatures, and words that a line wrap cut in two.
An eligibility span was one physical line, so a phrase that crossed a line could never bind.

## What the chapter 2 extractor does

`src/arctic_qa/pdf_layout.py` reads the word geometry that poppler writes with `pdftotext -bbox-layout`.
It rebuilds the text in reading order with a recursive XY cut over the lines of each page.
The cut looks for a column gutter first.
A full-width heading blocks that cut for its own region, so the band cut separates the heading and the columns split under it.
Each leaf of the cut becomes one or more paragraphs.

`src/arctic_qa/text_structure.py` groups the paragraphs into sections.
It removes a repeated page header or footer.
It also holds the sentence splitter.

`src/arctic_qa/extraction.py` builds the stored section and chunk records.
A chunk starts and ends on a sentence boundary.
Each chunk keeps its page number, its heading, and its heading path.

The extractor needs no new package.
`poppler-utils` is already in the nix devshell, and `pdftotext -bbox-layout` is part of it.

## Where the chapter 2 corpus lives

The root is `/mnt/crdata/research-abstention/arctic-qa/chapter2/`.

| Path | Content |
| --- | --- |
| `corpus-root.json` | The marker that makes this root authoritative. |
| `extracted/<hash>/text.txt` | The flat text for one article. Eligibility spans read this file. |
| `parsed/<hash>/sections.jsonl` | The sections of one article. |
| `chunks/<hash>/chunks.jsonl` | The sentence-complete chunks of one article. |
| `index/<hash>.json` | The parse receipt for one stored object, with every hash. |
| `progress/reextraction.ndjson` | One checkpoint line for each article. |
| `corpus-freeze/<freeze-id>/` | The frozen manifest, its descriptor, and the freeze receipt. |
| `article-access/<freeze-id>/` | The chapter 2 access run directory. |

`extracted`, `parsed`, and `chunks` are content addressed by the sha256 of the object.
`index` is addressed by the sha256 of the stored original.

## How the run reads the chapter 2 corpus

`extract_source` resolves its corpus root with `chapter2_corpus.chapter2_root`.
The function returns the chapter 2 root when `corpus-root.json` is present, and the namespace when it is not.
When the root holds a verified parse of the same bytes, `extract_source` returns that frozen parse.
The run then reads the exact objects that the freeze receipt hashed, and it pays no extraction cost.

The parse is faithful because the chapter 2 build derives the same source identifier as the streaming bridge.
`chapter2_corpus.corpus_source_id` repeats the rule of `discovery.manual_record`.

## Operate the three stages

Run each stage under `nice`, with two workers.
The pass is resumable: a completed article writes a checkpoint line and an index record.

```bash
nix develop -c bash -c 'PYTHONPATH=src nice -n 19 python -m arctic_qa chapter2-corpus \
  --action extract \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/article-access-r1/run-20260912T060442Z \
  --legacy-freeze-dir /mnt/crdata/research-abstention/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1 \
  --code-commit "$(git rev-parse HEAD)" --jobs 2'
```

The `freeze` action needs a freeze identifier and a run identifier:

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa chapter2-corpus \
  --action freeze --freeze-id chapter2-r1 --run-id chapter2-r1 \
  --access-run-dir ... --legacy-freeze-dir ... --code-commit "$(git rev-parse HEAD)"'
```

The `quality` action compares the two extractions on the two-column papers:

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa chapter2-corpus \
  --action quality --sample-size 50 --report-file /path/to/report.json \
  --access-run-dir ... --legacy-freeze-dir ...'
```

CAUTION: Do not remove a file under a chapter 1 directory.
The chapter 1 corpus, its manifests, and its receipts are read-only history.

## The chapter 2 order and the re-freeze receipt

The chapter 2 order derives from the chapter 1 frozen order of the same articles.
`full-text-ready-manifest.jsonl` gives that order through its `manifest_position` field.
The freeze writes an order hash over the ordered candidate keys.

The freeze receipt records the extractor name, the extractor version, the poppler version, and the exact command.
It also records the sha256 of the chapter 1 manifest and the chapter 1 freeze receipt.

## The chapter 2 access run directory

The freeze writes an access run directory under `article-access/<freeze-id>/`.
The directory uses the `article-access-manifest-v1` and `article-access-item-v1` schemas.
The streaming command accepts it as `--access-run-dir` with no change to `streaming.py`.

Each item keeps the chapter 1 source path, media type, and content hash.
The `extraction_path` and the `extraction_sha256` fields point at the chapter 2 text.
The `provenance` field names the chapter 1 item receipt and its sha256.

## Extraction quality measures

`src/arctic_qa/extraction_quality.py` counts four defects on any text:

- `gutter_rate` is the share of text lines that join two columns.
- `mid_word_break_rate` is the count of mid-word line breaks for each thousand characters.
- `presentation_rate` is the count of ligatures and soft hyphens for each thousand characters.
- `sentence_complete_rate` is the share of chunks that end on a sentence.
- `word_retention` is the count of chapter 2 words for each chapter 1 word.

`word_retention` is the guard against text loss.
A value near 1.0 shows that the reading-order extractor kept the words of the paper.
A repeated running head is the only text the extractor removes on purpose.

A chunk that holds a heading or an identifier has no terminal punctuation.
The sentence measure counts such a chunk as incomplete, so the number is a lower bound.
