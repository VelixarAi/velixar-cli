# Velixar Foundry CLI

`velixar foundry` is a thin, authenticated client for the Velixar model gateway. It discovers live
models, sends one stateless Responses request, streams its events, and reads settled receipts. It
does not implement its own router, identity system, agent runtime, session store, memory layer, or
function executor.

## Configuration and authentication

Set the gateway URL with `--gateway-url`, `VELIXAR_GATEWAY_URL`, or `gateway_url` in
`~/.velixar/config.json`, in that order. There is no default gateway URL while deployment remains
BG-10. A missing URL prints `Foundry gateway not configured` and exits 78.

Authentication uses the existing `VELIXAR_API_KEY` or config-file `api_key`. The key is sent only as
an Authorization bearer value to authenticated routes; health probes receive no credential. Gateway
catalog and response calls receive the bearer at the configured gateway host. The existing Velixar
API host (`VELIXAR_BASE_URL` or config `base_url`) separately receives that bearer for `/v1/usage`,
`/v1/usage/receipts`, and `/v1/agents/definitions`; this does not create a second client-side memory
or identity system.

All Foundry stdout and stderr passes through one redaction layer, including normal output, error
messages, verbose metadata, JSON, previews, and stream deltas. It removes configured key values,
Authorization values, `vlx_` keys, JWT-shaped strings, and recognized provider-key patterns whether
labelled or embedded in text. `--verbose` adds only safe server error metadata after the server's
message.

## Commands

```text
velixar foundry status [--json]
velixar foundry models [--capability ...] [--family ...] [--surface ...] [--recommended-for ...] [--json]
velixar foundry run [--prompt TEXT] [--task TYPE] [--model auto|ID] [--capability NAME --list] [--preview] [--quiet] [--json]
velixar foundry receipts [--before ISO_TIME] [--limit 1..100] [--json]
velixar foundry agents [--json]
```

Every command and subcommand supports `--help`.

`status` distinguishes an unreachable gateway, a disabled gateway (404), a refused key, and an OK
service. It reports gateway health/readiness, the model catalog's account block, and only the
`build{}` block from `/v1/usage`.

`models` displays the live callable `data[]`, `not_included[]`, and `unavailable[]`. Filters operate
only on fields the gateway returned: `tool_calling`, `structured_output`, `streaming`, `family`,
`surfaces`, and `recommended_for`. They do not rank or select models.

`receipts` reads `/v1/usage/receipts` from the existing Velixar API host for the key-derived
workspace. The CLI sends `--before` unchanged as a cursor and does not parse or invent one. `agents`
reads `/v1/agents/definitions` from that same host and labels the results
`definitions - not verified identities`. A displayed agent name is never accepted for execution.

## Model selection and execution

Most users omit `--model`. The CLI reads `velixar.account.default_model` from the live catalog,
requires that id to be in the current callable `data[]`, and labels the choice:

```text
Auto: Velixar default for your plan -> <id>
```

This is compatibility behavior while server-side auto routing is BG-1. It is not a client-side
router: task words, model family, capability flags, and price never influence the choice.
`--model auto` is an explicit alias for omitting `--model`; it sends the catalog's callable account
default and never sends the literal model id `auto`.

Experts may use `--model <id>`. The id must match the live callable `data[]` exactly. An unavailable,
not-included, or unknown model prints `Requested model unavailable.` plus the server-supplied reason
when present and is never replaced by another model. A listed-but-unavailable id exits 69, a
not-included id exits 77, and an unknown id exits 79. If the account default is absent from callable
`data[]`, Auto exits 69 without selecting a substitute.

On `run`, `--capability ... --list` filters models for information only. Passing `--capability`
without `--list` requests capability-profile execution, which exits 69 with BG-2. A task type is
local preview metadata only and never selects a model, reaches the gateway, or grants access.

Both flag and interactive input create one immutable local `ExecutionRequest`. The gateway payload
contains only `model`, `input`, `stream:true`, `store:false`, and optional `max_output_tokens`. It
never contains `user_id`, `principal_id`, `workspace_id`, `provider`, or `resolved_model`.

## Preview, streaming, and cancellation

`--preview` performs live catalog validation and shows the exact request structure and fields after
the mandatory redaction boundary, without executing it. The preview is deterministic for the same
catalog and inputs. It displays only server-supplied list rates and says
`Cost estimate: not available yet (BG-11)`; it never invents a cost estimate.

Human mode renders `response.output_text.delta`. `--quiet` suppresses generated text but retains
provenance. For `run`, `--json` writes one redacted server event per line (NDJSON), in order. Other
commands emit one JSON object. Provenance comes only from `velixar.charge` and
`X-Velixar-Response-Id`, never from model prose.

Ctrl-C cancels only the current response id using `POST /v1/responses/{id}/cancel`. If the header and
event ids conflict, the CLI refuses to guess. A broken nonterminal stream also requests cancellation
for its known current id. Cancellation exits 130.

## Identity and unavailable capabilities

The backend derives trusted identity and workspace from the API key. `--workspace` is only intended
as a consistency check, but the server cannot echo the workspace yet, so it prints
`cannot verify workspace (BG-4)` and exits 69 without sending the id.

These features are not emulated in the CLI:

- tasks/recommendation routing: BG-1/2
- capability-profile routing: BG-2
- inspect/resume: BG-3
- trusted agent execution identity: BG-5
- sessions: BG-6
- memory context modes: BG-7
- Velixar-authorized functions: BG-8
- multi-agent execution: BG-9

The local default context description is “specified prompt only”; passing `--context` requests a
backend context mode and therefore exits 69 with BG-7.

## Errors and exit codes

Server `error.message` is printed verbatim except mandatory secret redaction. JSON errors preserve
the redacted server envelope and add `exit_code`. The main mappings are: 0 success, 1
unknown/internal, 2 CLI usage, 5 allowance stop, 65 malformed request, 69 unavailable (including
transport failure, disabled gateway model catalog, and listed-but-unavailable model), 75 retryable
throttling, 76 daily stop, 77 permission/entitlement denial, 78 missing configuration, 79 not found,
and 130 interruption. `status` reports gateway 404 as the non-error state `not enabled`; direct
`models` or `run` catalog access maps that condition to exit 69.

The CLI never automatically retries a model call, falls back to another model, deletes a rejected
key, sends provider credentials, or treats a task/model/display name/workspace argument as authority.

## Examples

```bash
velixar foundry --gateway-url https://gateway.example status --json
velixar foundry models --capability streaming --family openai
velixar foundry run --prompt "Review this change"
velixar foundry run --model gpt-5.3-codex --prompt "Explain this failure" --preview
velixar foundry receipts --limit 25 --json
velixar foundry agents
```

Until the corresponding backend gaps land, `--agent-name`, `--workspace`, `--context`, `--function`,
`--multi-agent`, `inspect`, `resume`, `sessions`, `functions`, and `recommend` fail explicitly rather
than simulating authority or state in the client.
