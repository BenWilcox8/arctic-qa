# Method traceability

This file maps major method choices to existing research.
It distinguishes cited precedent from proposed Arctic adaptations.

The combined pipeline is proposed and unvalidated.
No cited paper establishes that the complete design is optimal.

| Choice | Precedent and locator | Limitation | Adaptation label |
| --- | --- | --- | --- |
| Answer-first generation | [Alberti et al. 2019](https://aclanthology.org/P19-1620.pdf), sections 3.1 through 3.4, pages 2 through 4 | The paper trained models and measured downstream QA gains. | Proposed API-only Arctic transfer |
| Independent reconstruction | [Alberti et al. 2019](https://aclanthology.org/P19-1620.pdf), sections 3.1 through 3.4 | Roundtrip agreement does not prove truth. | Proposed cross-provider filter |
| Direct-joint baseline | [SciQAG](https://arxiv.org/html/2405.09939v1), sections 3.1 through 3.3 | The main generator used training and eight A800 GPUs. | Required API baseline |
| Component-only correction | [MCQG-SRefine](https://aclanthology.org/2025.naacl-long.538.pdf), sections 2.2 through 5 and pages 19 through 20 | Human review found factual errors and information loss. | One correction after hard gates |
| Typed distractor proposals | [D-GEN](https://aclanthology.org/2025.findings-acl.174.pdf), sections 3.3 through 6.4 | Human review remained necessary in the source study. | Proposal only before strict checks |
| Distractor validity and plausibility separation | [Feng et al. 2024](https://aclanthology.org/2024.findings-naacl.193.pdf), sections 2 through 4 | The study covers mathematics, not Arctic science. | Separate automated states |
| Post-generation distractor checking | [Yu et al. 2024](https://aclanthology.org/2024.inlg-main.16.pdf), sections 3.2 through 3.4 and conclusion | The verifier and NLI checks retain reasoning and multiple-correct-option failures. | Verify each exact displayed option and bind the verdict to source and QA hashes |
| QA and entailment checks | [Fabbri et al. 2022](https://aclanthology.org/2022.naacl-main.187.pdf), sections 5.1 through 7 | The metrics do not detect every factual inconsistency. | Separate answer reconstruction, source entailment, scope, and alternative-answer gates |
| Source-bounded evidence labels | [QASPER](https://aclanthology.org/2021.naacl-main.365.pdf), sections 2.1 through 4.1 | Paper-relative absence is not world absence. | Exact immutable-source locators |
| Alternative-evidence search limits | [SciFact-Open](https://aclanthology.org/2022.findings-emnlp.347.pdf), sections 3.1, 5, and 8 | An incomplete pool cannot prove absence. | Reject unresolved alternatives |
| No target-adaptive retention | [AutoBencher](https://arxiv.org/html/2407.08351v1), section 4.1 and appendix A | Adaptive search can select model-specific weaknesses. | Fixed corpus eligibility before generation |
| Structured article preference | [NISO JATS 1.4](https://www.niso.org/standards-committees/jats) | JATS availability varies by publisher. | JATS, then HTML, then PDF |
| Column-aware PDF reading order | Recursive XY cut, the standard page segmentation method; no locator is recorded here because the implementation was written from the page geometry, not from a paper | The cut needs a visible gutter and fails on an irregular layout. | Reading order over poppler word geometry, with sentence-complete chunks |

The geography rule uses 66.56 degrees north and a reviewed marine allowlist.
This rule comes from the captain requirement and project configuration.
It is not a research result or a universal Arctic definition.
The implementation binds each geographic decision to a stored content hash and an exact extracted chunk locator.
Each latitude and named region must occur in the quoted study-setting text.
The quote must also state complete scope.
The implementation rejects invalid coordinates and computes mixed site scope.

The chapter 2 corpus applies this preference again over the same stored objects.
Every stored original of the frozen corpus is a PDF, so the JATS and HTML paths are unused there.
The [chapter 2 corpus document](CHAPTER2_CORPUS.md) records the extractor and its measured effect.

The source manifest freezes identities, versions, hashes, geography decisions, and rights fields.
This control adapts the frozen-manifest method in the project study.

The author and verifier use separate calls.
Provider separation can reduce one source of correlated error.
It cannot guarantee independence or factual truth.
The author stores necessary question context separately from the question.
This context can define an unfamiliar acronym or resolve an ambiguous referent.
It must not contain the answer, an answer-bearing result, or a gratuitous paper summary.
The blinded reconstructor receives both the question and this context.
External benchmark evaluators must receive the same pair under the [benchmark input contract](BENCHMARK_INPUT_CONTRACT.md).
The initial matched-arm policy freezes one proposed finding per paper family and run.
Both arms use that finding, and a failed finding remains in the rejection record.
Generation from a second source version in the same family stops instead of selecting another finding.

The role defaults reflect vendor capabilities recorded on 2026-09-11.
They are not winners of an Arctic QA evaluation.
`config/roles.v1.json` holds the role assignment under contract `generation-model-roles-v1`.
The stream loads and validates that file before the first paper.
The writer and every judge must use different models and different providers.
The strongest configured judge model must hold `standalone_verifier` and `option_verifier`.
A run that names a role profile must serve those models, and a production-phase run must name one.
The run manifest records the resolved roles and the effective model of each role.

Machine acceptance stops at `machine_accepted_unverified`.
A false or absent source-entailment result stops acceptance.
Nonnumeric deterministic distractor rules are derived from the source-located answer record.
A distractor cannot create its own allowed or excluded set.
Numeric values, units, and tolerance must resolve in the answer and source quote.
Each answer-verification and post-generation option-verification claim must match a completed call receipt and its stored response.
An option response must be an object with every required field and no extra field.
The validator compares the canonical response object with the recorded verdict fields.
An incomplete object or a JSON array fails closed.
Each option verdict also binds to the stored source hash, QA hash, displayed option hash, prompt, provider, model, and request record.
Author verification flags do not control acceptance.
Truth at a different location or time does not invalidate a distractor when the question fixes its scope.
Negated text assertions fail closed because a surface value does not represent their truth conditions.
Negated, multi-quantity, and disjunctive numeric assertions also fail closed because one metadata value cannot bind them safely.
A run-specific candidate ID prevents identical content in another run from losing its candidate record.
External candidate-file validation cannot change stored state.
Stored validation events and exports bind to the exact candidate payload hash.
A future authorized audit can add a stronger review label without blocking this production workflow.
