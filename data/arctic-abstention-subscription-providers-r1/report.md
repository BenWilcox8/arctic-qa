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
