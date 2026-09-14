# Recovery and restart commands

These commands are release instructions only. Do not run them until internal
review changes the draft gate to a released gate. The recovery uses the old gate
because the interrupted reservation is bound to that old gate. The restart uses
the reviewed successor gate.

## Reviewed zero-cost recovery

Run from the released primary checkout after verifying the listed old ledger hash
and the absence of the four sidecars:

```sh
cd /home/ben/.treehouse/firstmate-c40011/6/firstmate/projects/arctic-qa
nix develop -c env PYTHONPATH=src python -m arctic_qa --json settle-pretransport-reservation \
  --request-key 445c8935c5dc9d1c5d03fe7d4d15308fd57d0e68f8d2d21bf310875b85b5512b \
  --expected-ledger-sha256 1ef465bf0f539709f8293f193e956b7b275d83e259d3ab5be760a4448b6d7a13 \
  --review-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-resume-review-r1/report.md \
  --traceback-evidence-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-qa-build-r1/production-pause-223.md \
  --streaming-budget-policy-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-qa-build-r1/proposed-streaming-dataset-budget-policy-v7.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file /home/ben/.config/arctic-qa/gate-6fbdf41-production-50usd-r1.json \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --ledger-config-transition-file /home/ben/.config/arctic-qa/config-transition-v2-6fbdf41-production-50usd-r1.json \
  --credential-file /home/ben/.config/arctic-qa/gemini-api-key \
  --prior-construction-spend-usd 0
```

## Corrected campaign restart

After the recovery receipt is present and the released successor gate and v3
canonical policy are installed, run:

```sh
cd /home/ben/.treehouse/firstmate-c40011/6/firstmate/projects/arctic-qa
nix develop -c env PYTHONPATH=src python -m arctic_qa --json --data-root /mnt/crdata/research-abstention stream \
  --phase away_production \
  --run-id first-production-6fbdf41-r1 \
  --campaign-id arctic-qa-production-campaign-001 \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/production-campaign-r1/quality-order-r1/materialized-top-800 \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/gemini-eligibility-r1/first-production-6fbdf41-r1 \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v5.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v3.schema.json \
  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/corpus-search-r1/protocol/protocol-v3.json \
  --credential-file /home/ben/.config/arctic-qa/gemini-api-key \
  --prior-construction-spend-usd 0 \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --streaming-budget-policy-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-qa-build-r1/proposed-streaming-dataset-budget-policy-v7.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file /home/ben/.config/arctic-qa/gate-6fbdf41-production-50usd-r1-v3.json \
  --max-papers 800 \
  --ledger-config-transition-file /home/ben/.config/arctic-qa/config-transition-v2-6fbdf41-production-50usd-r1.json
```

The released gate must bind the existing maximum request cost of USD0.25, the
existing maximum paper cost of USD1.00, the USD50 new campaign ceiling, and the
USD61.614496 cumulative ceiling. It must keep retries and fallback disabled.
