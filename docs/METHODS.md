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
| Source-bounded evidence labels | [QASPER](https://aclanthology.org/2021.naacl-main.365.pdf), sections 2.1 through 4.1 | Paper-relative absence is not world absence. | Exact immutable-source locators |
| Alternative-evidence search limits | [SciFact-Open](https://aclanthology.org/2022.findings-emnlp.347.pdf), sections 3.1, 5, and 8 | An incomplete pool cannot prove absence. | Reject unresolved alternatives |
| No target-adaptive retention | [AutoBencher](https://arxiv.org/html/2407.08351v1), section 4.1 and appendix A | Adaptive search can select model-specific weaknesses. | Fixed corpus eligibility before generation |
| Structured article preference | [NISO JATS 1.4](https://www.niso.org/standards-committees/jats) | JATS availability varies by publisher. | JATS, then HTML, then PDF |

The geography rule uses 66.56 degrees north and a reviewed marine allowlist.
This rule comes from the captain requirement and project configuration.
It is not a research result or a universal Arctic definition.

The source manifest freezes identities, versions, hashes, geography decisions, and rights fields.
This control adapts the frozen-manifest method in the project study.

The author and verifier use separate calls.
Provider separation can reduce one source of correlated error.
It cannot guarantee independence or factual truth.

The role defaults reflect vendor capabilities recorded on 2026-09-11.
They are not winners of an Arctic QA evaluation.

Machine acceptance stops at `machine_accepted_unverified`.
A future authorized audit can add a stronger review label without blocking this production workflow.
