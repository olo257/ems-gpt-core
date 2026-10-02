# EMS-GPT AI Observer worker

The worker runs outside EMS-GPT Core. It polls the read-only context and the
operator mailbox, asks an OpenAI-compatible model to review the data, and
saves scheduled analyses as AI Observer runs and detailed TODO items. It has
no database, Home Assistant, planner, PPD, executor, settings, or command access.
The Core read-only context also carries current TODO items, prioritizing
operator-accepted items for follow-up analysis. An accepted item remains a
request for analysis, not permission for the worker to change Core or controls.

## Required configuration

In Home Assistant, install **EMS-GPT AI Observer Worker** from the same add-on
repository. Configure and start it only after setting these values:

1. In **EMS-GPT Core → Configuration**, set `agent_api_token` to a long random
   secret. For example, generate one with `openssl rand -hex 32` in a trusted
   terminal.
2. In **EMS-GPT AI Observer Worker → Configuration**, paste that exact same
   value into its `agent_api_token` field. This is the Core API password.
3. In the worker configuration, set `llm_base_url` and `llm_model`; set
   `llm_api_key` only if that model endpoint requires a provider key. This is a
   separate credential from `agent_api_token`.
4. Confirm `core_api_url` is the Core add-on's private network address and
   enable the worker. The default is `http://9a3d0112-ems-gpt-core:8099`; the
   hostname is the repository and add-on slug with underscores replaced by
   hyphens, as required for Supervisor DNS.
5. Start the worker add-on. It is manual-start by default and does not run
   merely because a token was saved.

| Variable | Purpose |
| --- | --- |
| `EMS_AGENT_CORE_URL` | Internal HTTP base address of the Core add-on, reachable from the worker container. |
| `EMS_AGENT_API_TOKEN` | Same secret configured as the Core add-on option `agent_api_token`. Generate a separate random value; never use `SUPERVISOR_TOKEN`. |
| `EMS_AGENT_LLM_BASE_URL` | OpenAI-compatible API base, for example a local model server or a hosted provider's `/v1` base URL. |
| `EMS_AGENT_LLM_MODEL` | Model name accepted by that API. |
| `EMS_AGENT_LLM_API_KEY` | Model-provider credential, if the selected server requires one. This is separate from `EMS_AGENT_API_TOKEN`. |

## Optional configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMS_AGENT_ID` | `ems-analysis-agent` | Stable worker identity for claims and run records. |
| `EMS_AGENT_POLL_SECONDS` | `30` | Mailbox polling interval; minimum 5 seconds. |
| `EMS_AGENT_SUPERVISION_INTERVAL_SECONDS` | `900` | Minimum interval between checks for a new analytics run; `0` disables periodic review while keeping mailbox replies enabled. |
| `EMS_AGENT_LLM_TIMEOUT_SECONDS` | `120` | Model request timeout. |
| `EMS_AGENT_STATE_PATH` | `/data/agent-worker-state.json` | Persistent marker preventing duplicate model runs for the same analytics run. Mount `/data` to persistent storage. |

The Core produces analytics at most about once every 50 minutes. The worker
uses each completed analytics run once, reads up to 28 days of completed slots
and 96 future slots, compacts older history into daily evidence, and creates an
Observer run even when no finding needs a TODO. Critical findings create an
immediate `OPEN` TODO; recurring warnings follow the existing three-day
Observer lifecycle. Operator questions can still be handled by the mailbox,
but autonomous findings are written to **AI Observer** and **TODO**, not chat.

## Build and start

Build this directory as its own container and supply the variables above via a
secret store or container environment. Keep the API base address on a trusted
private network; do not publish the worker or Core API to the public internet.
Do not put either secret in source control or command-line history.

The token in Core only authenticates the worker to Core. A hosted LLM receives
the compacted EMS context, including energy measurements, prices, SOC, and
execution history. Select a provider and endpoint whose data handling matches
your requirements.
