# Abstention evaluation: Claude and ChatGPT subscription providers

Task: `arctic-abstention-subscription-providers-r1`, branch `fm/arctic-abstention-subscription-providers-r1`, 2026-09-16.

The captain asked for two more evaluation providers beside Google Gemini.
Both must bill the Claude subscription and the ChatGPT subscription that the installed harnesses use.
No API key is used.
Every call must be one isolated session, with reasoning on, with no tool access, and with the smallest possible token overhead.

## 1. Design

### 1.1 Facts that the probes established

Every claim in this section was checked against the installed binaries on 2026-09-16.
The methods were: the help output of each binary, one real call per vendor, and a local dummy endpoint that captured the exact request bytes without a model call.

Installed binaries and logins:

| Vendor | Binary | Version | Login state |
|---|---|---|---|
| Anthropic | `~/.npm-global/bin/claude` | 2.1.273 (Claude Code) | `claude auth status`: logged in, `authMethod: claude.ai`, `subscriptionType: max` |
| OpenAI | `~/.npm-global/bin/codex` | codex-cli 0.154.0 | `codex login status`: "Logged in using ChatGPT" |

### 1.2 Ways to call Claude on the subscription

| Method | Bills the subscription | Verdict |
|---|---|---|
| Claude Code print mode (`claude -p`) | Yes. The captured request carries `authorization: Bearer` from the claude.ai OAuth login and no `x-api-key`. | Selected. |
| Claude Code `--bare` mode | No. The help text states: "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper (OAuth and keychain are never read)". | Rejected. |
| Claude Agent SDK | Same engine as print mode. It adds a Python or Node dependency and the same harness prompt. | Rejected. Print mode gives the same request with one subprocess. |
| Anthropic Messages API with the OAuth token | Not a supported path. The token is a harness credential, not an API key. | Rejected. |

Print mode, property by property:

| Property | Finding |
|---|---|
| Fresh isolated session | `--no-session-persistence` writes nothing to disk. A new session id per call. Verified: no session file. |
| Tools fully disabled | `--tools ""`. The init event reports `tools: []`. The captured request has no `tools` array. `--strict-mcp-config` with no MCP config gives `mcp_servers: []`. `--disable-slash-commands` gives `slash_commands: []`. |
| Settings and memory files | `--setting-sources ""` loads no user, project or local settings. `--system-prompt` replaces the default system prompt, so no CLAUDE.md content enters. Verified: the captured system array holds only a billing header, one SDK sentence, and the evaluation system text verbatim. |
| Reasoning on, preset selectable | `--effort <low, medium, high, xhigh, max>`. The captured request holds `thinking: {type: adaptive}` and `output_config.effort: medium`. The result reports `thinking_tokens`. |
| Output constraint | `--json-schema` adds a `StructuredOutput` tool to the request (captured). A tool is not allowed, so the provider uses no schema. The strict single-letter parser files everything else as N0. |
| Exact model id | `--model claude-opus-5` passes through verbatim (captured: `model: claude-opus-5`). The binary does not validate the id locally. A bogus id also passes through, so the API decides. |
| Token usage | The `json` result reports `usage.input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `output_tokens_details.thinking_tokens`, and `modelUsage` per model id. |
| Temperature | Not exposed. The captured request has `temperature: null`. |
| Harness overhead per call | The captured request adds: `x-anthropic-billing-header: ...` (one line), `You are a Claude agent, built on Anthropic's Claude Agent SDK.`, a `<system-reminder>` with the user email (about 90 words), and one `# Environment` system message with cwd, platform, model name and date (about 60 words). The evaluation prompt itself is verbatim. Measured: 846 input tokens for a trial that Gemini counts as 270 (tokenizers differ). Nothing further can be cut: `--exclude-dynamic-system-prompt-sections` applies only to the default system prompt. |
| Side calls | The first probe made one extra Haiku call (1070 input tokens) that names the session. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` removed that call in the capture. |
| Nested process | With `CLAUDECODE=1` set the print mode still ran. The provider unsets every `CLAUDE*` variable anyway, so no parent session leaks in. |
| Cost report | `total_cost_usd` and `modelUsage.costUSD` are list prices for information only (`costBasis: list`). The ledger records USD 0. |

### 1.3 Ways to call ChatGPT models on the subscription

| Method | Bills the subscription | Verdict |
|---|---|---|
| Codex CLI `codex exec` | Yes. `auth.json` holds `auth_mode: chatgpt` and the request carries the ChatGPT bearer token and `chatgpt-account-id`. | Selected. |
| Codex app-server or SDK | Same engine, one more long-lived process and a protocol layer. | Rejected. `exec` gives one subprocess per call. |
| Custom `model_provider` with the Responses API | Needs an API key (`env_key`). | Rejected. Used only with a dummy endpoint to capture the request bytes. |

`codex exec`, property by property:

| Property | Finding |
|---|---|
| Fresh isolated session | `--ephemeral` persists no session. Verified: no `sessions` directory after the call. |
| Tools fully disabled | Codex has no single switch. The provider sets `web_search = "disabled"`, `features.shell_tool = false`, `features.unified_exec = false`, `agents.enabled = false`, `features.view_image = false`, and the other feature switches listed in the config file. The captured request then has an empty `tools` array and one `additional_tools` item with the code-mode `exec` shell (a JavaScript isolate with no file system, no network, and no nested tools that can read anything), `wait`, and `request_user_input` (Plan mode only). `sandbox_mode = "read-only"` and `approval_policy = "never"` block any command. `features.code_mode_host = false` makes Codex fail closed, so that one stays on. |
| Web search | CAUTION: the default is `cached` and the first probe searched the web nine times and found the source paper. `tools.web_search = false` does not remove it. `web_search = "disabled"` (top level) removes it. Verified by one real call: no `web_search` item. |
| Settings and memory files | The provider runs with its own `CODEX_HOME` that holds one strict `config.toml` and a symlink to the real `auth.json`. `project_doc_max_bytes = 0` reads no AGENTS.md. `--ignore-rules` loads no `.rules`. `HOME` points to an empty directory, so no host skills from `~/.agents/skills` are listed. `skills.max_context_tokens = 1` shrinks the bundled skills catalog to a 500-byte stub. `--strict-config` rejects any unknown key. |
| Reasoning on, preset selectable | `model_reasoning_effort` with the presets of the model catalog (`gpt-5.6-terra`: low, medium, high, xhigh, max, ultra). The captured request holds `reasoning.effort: medium`. The turn reports `reasoning_output_tokens`. |
| Output constraint | `--output-schema <file>` sets the Responses API `text.format` JSON schema. The root must be an object. The provider sends `{"letter": <enum of the trial letters>}` and reads `letter` as the raw answer. Verified by one real call: the final message was `{"letter":"A"}`. |
| Exact model id | `-m gpt-5.6-terra`. The catalog (`codex debug models`) lists `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5` as visible. |
| Token usage | The `turn.completed` event reports `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`. |
| Temperature | Not exposed. |
| Harness overhead per call | The captured request adds: the `exec` and `wait` tool descriptions (about 4500 bytes), a `<skills_instructions>` stub (500 bytes), a `<permissions instructions>` block (340 bytes), and one `<environment_context>` user message (650 bytes). The system text goes in verbatim as the developer message through `model_instructions_file`, so the built-in Codex persona prompt is gone. Measured: 3800 input tokens with the subagent tools still on. With `agents.enabled = false` the request body is 11.3 KB. |
| Side calls | With `chatgpt_base_url` captured, Codex fetched plugin lists, user settings and analytics endpoints. These are not model calls. |

### 1.4 Chosen methods

- Claude: `claude -p` with `--model`, `--effort`, `--tools ""`, `--system-prompt`, `--setting-sources ""`, `--no-session-persistence`, `--disable-slash-commands`, `--strict-mcp-config`, `--permission-mode dontAsk`, `--permission-prompts none`, `--output-format json`, from an empty scratch directory, with every `CLAUDE*` variable unset and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
- ChatGPT: `codex exec` with `-m`, `--ephemeral`, `--strict-config`, `--skip-git-repo-check`, `--ignore-rules`, `--json`, `--output-schema`, a private `CODEX_HOME` with one strict `config.toml`, an empty `HOME`, from an empty scratch directory.

### 1.5 Fairness caveats against the Gemini path

1. Temperature: Gemini runs at the API maximum (2.0). Neither harness exposes temperature. Both run at the provider default.
2. Residual harness prompt: Gemini sees the evaluation prompt only. Claude sees one SDK sentence, a billing header, a user-email reminder and an environment block. ChatGPT sees tool descriptions, a permissions block and an environment block. The evaluation prompt bytes are identical for all three.
3. Reasoning presets: Gemini `thinkingLevel` (low, medium, high), Claude `effort` (low, medium, high, xhigh, max), Codex `model_reasoning_effort` (low to ultra by model). The names match at `medium`, but the vendors define them differently.
4. Output constraint: Gemini uses an enum at the decoder. Codex uses a JSON-schema enum at the decoder. Claude has no decoder constraint, so its N0 rate includes prompt-compliance errors.
5. Metering: subscription calls have no price. The ledger records USD 0 with the token counts.

### 1.6 Structure

- `src/arctic_qa/abstention_subscription.py` holds both providers, one transport per vendor, the scripted transport, the subscription ledger, and the decoding record.
- `config/benchmark-evaluation-subscription-models-v1.json` registers each vendor: binary path, models, presets, and the isolation flags.
- The subscription ledger is a sibling of the shared paid-call ledger, not part of it.
  The shared broker validates a Gemini price config, a Gemini API base and positive prices, and runs `countTokens` before every call.
  A change there touches the production money ledger.
  The subscription ledger applies the same evaluation policy file (per-item-condition-model-arm cap, per-minute pace, one concurrent request, no retries, stop on the first error), binds the same gate schema, and writes one immutable receipt per request key with USD 0.
- The response record gains one `harness` field with the vendor, the exact model id, the preset, the invocation and the raw final text.

## 2. Implementation

Commit `8bd21e9` on branch `fm/arctic-abstention-subscription-providers-r1` adds:

- `src/arctic_qa/abstention_subscription.py`: the two providers, the subprocess transport, the scripted transport, the subscription ledger, the decoding record, the gate record, the dry run and the model enumeration.
- `config/benchmark-evaluation-subscription-models-v1.json`: the registry of both vendors (binary path, checked version, models, presets, timeout, output constraint).
- `src/arctic_qa/abstention_cli.py`: the `--provider` switch of `run`, `dry-run`, `gate-template` and `list-models`, plus `--subscription-models-file`, `--subscription-ledger-dir`, `--binary-path`, `--scratch-dir` and `--catalog-bundled`.
- `src/arctic_qa/abstention_providers.py`: one new optional field `harness` on the response record. The Gemini provider leaves it `None`.
- `tests/test_abstention_subscription.py`: 13 unit tests with the scripted transport.

The Gemini provider, the prompt contract and the shared paid-call ledger are unchanged.

What one subscription call records:

- The response row carries `harness` with the vendor, the exact model id, the preset, the argv, the environment names, the raw final text, the side-call models (Claude), the schema flag (Codex) and the harness error lines.
- The receipt (`<ledger>/receipts/<request_key>.json`) adds the stdout, the stderr tail, the stdin hash, the timing, the usage, `billing: subscription` and `cost_usd: "0"`.
- The request key binds the vendor, the model, the run id, the trial id, the item identity, the prompt bytes and the output constraint.
- The ledger (`<ledger>/subscription-ledger.json`) counts submissions per item, condition, model and arm, and paces them per minute under the evaluation policy.

## 3. Tests

Unit tests (`nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_abstention_subscription.py tests/test_abstention_run.py tests/test_abstention_render.py tests/test_abstention_broker.py -o addopts="" -q'`): 36 passed.
The new tests cover: the isolation flags of both invocations, the prompt bytes against the Gemini payload, the private Codex home and its strict config, the N0 filing of a sentence, a wrong letter, a `max_tokens` stop and a non-JSON Codex message, USD 0 accounting with the token counts, the harness failure and timeout states, the resume from receipts, the per-item cap and the per-minute pace, the gate binding refusals, the request key, the parsers on the real probe outputs, and the dry run through the CLI.
`ruff check src tests` and `ruff format --check` on every touched file pass.
The whole suite result is in section 5.

### 3.1 Live test: runs/subscription-test-r1

The same 5 items as the Gemini canary (`abstention-eval-set-4d3202cf27b30859`), both conditions, one repeat, medium preset, through `--action run` with a reviewed gate per provider.
Gates, review record and launcher: `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/subscription-test-r1-*`.
Runs: `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/runs/subscription-test-r1/{claude,codex}`.
Ledgers: `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/subscription/{anthropic_claude_code,openai_codex}`.

| | Gemini canary (gemini-3.1-pro-preview) | Claude (claude-opus-5) | ChatGPT (gpt-5.6-terra) |
|---|---|---|---|
| Calls made | 10 | 10 | 10 |
| Invalid (N0) | 0 | 0 | 0 |
| Mean latency (s) | 14.5 | 3.3 | 8.5 |
| Median latency (s) | 14.2 | 3.5 | 7.2 |
| Prompt tokens per call (harness overhead included) | 216 | 762 | 2369 |
| Thinking tokens per call | 469 | 113 | 168 |
| Answer tokens per call | 1 | 3 | 20 (the JSON object) |
| Cost | USD 0.061 | USD 0 (subscription) | USD 0 (subscription) |
| N1 gold present, gold chosen | 3 | 3 | 4 |
| N2 gold present, distractor chosen | 1 | 1 | 1 |
| N3 gold present, abstained | 1 | 1 | 0 |
| N4 gold absent, distractor chosen | 0 | 3 | 5 |
| N5 gold absent, abstained | 5 | 2 | 0 |

Observations:

- Both harness paths worked end to end through the gate, the ledger, the receipts and the scorer.
- No Claude call made a side call (`side_call_models: []` on all 10), so `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` removed the session-title call.
- Every Codex call honoured the schema (`schema_honoured: true`) and ran no tool item. Every Codex call also logged the harness line "Exceeded skills context budget", which is the intended effect of `skills.max_context_tokens = 1`.
- The first Codex call cost 4492 prompt tokens, the other nine about 2100 to 2190. The first call of a fresh `CODEX_HOME` carries more harness context.
- Five of the ten Claude calls reported 0 thinking tokens: adaptive thinking at the medium preset skipped the thinking block for those items.
- gpt-5.6-terra at medium never abstained (N5 = 0). This is a data point about the model, not about the harness: the abstention option was in the prompt and the schema allowed its letter.

## 4. Documentation

`docs/ABSTENTION_EVALUATION.md` gained the section "Subscription providers" with the chosen methods, the setup, the fairness caveats and the exact commands.

## 5. Whole test suite

`nix develop -c bash -c 'PYTHONPATH=src pytest tests -o addopts="" -q'`: 1233 passed in 12 minutes 31 seconds, at commit `8bd21e9` plus the documentation changes.

## 6. Open points for the captain

- Model choice for the control arms. The live test used `claude-opus-5` and `gpt-5.6-terra`. The registry also holds `claude-fable-5-1` and `gpt-6-astra` as the flagship tier of each vendor. Every registered model is one `--models` value away.
- The evaluation policy paces at 10 requests per minute and caps 8 calls per item, condition, model and arm, the same as the Gemini canary policy. A large subscription run can use a policy file with a faster pace, bound into a new gate.
- The Claude and Codex quotas are shared with every agent session on this machine. A large run should go in a quiet window.
