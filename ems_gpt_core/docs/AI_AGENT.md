# EMS-GPT Agent API

Core now provides a bounded mailbox for a separate EMS analysis agent. The
agent can review future planner slots, completed execution, analytics and
Observer results, then return a written analysis to the operator panel. The
Core does not call a language model by itself; a worker using this protocol
must be configured separately.

## Safety boundary

- The agent context endpoint is read-only and caps each slot list at 96 rows.
- The same endpoint includes up to 25 recent Core TODO items, including
  accepted/rejected status and operator notes. TODO remains in
  `ems_gpt_core_todo`, the single source of truth; chats and workers read it
  without copying it into a second list.
- The only agent write endpoint creates an answer to an operator message that
  the same agent previously claimed.
- Agent code has no adapter to the planner, PPD, executor, Home Assistant
  services, commands or settings.
- Agent endpoints require the add-on option `agent_api_token`; the token is
  separate from Home Assistant's Supervisor token. Leave it empty to disable
  all worker endpoints.
- Operator messages are available only through the authenticated Home
  Assistant ingress UI.

## EMS-GPT project chats

At the start of a TODO review, a project chat can read the current list through
the EMS-HASS connector using the Core app API path
`api/todo?status=ACCEPTED&limit=10` (GET) to find suggestions the operator has
directed to project analysis. Read the full `details`, evidence, and review
note before proposing a change. Acceptance requests analysis; it
does not authorize automatic planner, device, or Core changes. The chat should
report the analysis and prepare any code change for review separately.

## Worker flow

The worker can run both as a mailbox responder and as a scheduled Observer
analysis process. Scheduled findings are persisted to the `AI Observer` run
history and TODO list; they are not posted as unsolicited chat messages. See
[`../../ems_gpt_ai_observer_worker/README.md`](../../ems_gpt_ai_observer_worker/README.md) for add-on setup,
LLM configuration, and the separate model-provider key.

Use the Core API address reachable from the worker add-on's private Supervisor
network and send `Authorization: Bearer <agent_api_token>` plus
`X-EMS-Agent-ID: <stable-worker-name>` on worker requests. The API address is
not the browser ingress URL.

1. `GET /api/agent/inbox?limit=5` claims pending operator messages. Claims
   expire after ten minutes so a worker restart does not strand a question.
2. `GET /api/agent/context` reads up to 96 future slots and the last 28 days of
   completed slots (up to 2,688), plus recent analytics and Observer runs. The payload includes the explicit
   forbidden-operation list.
3. Analyze the question against this evidence. Cite slot timestamps and run
   identifiers in the reply; state when the available measurements do not
   support a conclusion.
4. `POST /api/agent/reply` with JSON `{"message_id":"...","message":"..."}`
   writes the answer. The claimed message ID and worker ID must match.
5. For scheduled analysis, the worker reviews a new completed analytics run
   only once and submits structured findings to the authenticated
   `POST /api/agent/observer-result` endpoint. Core validates the source run and
   findings, then stores an Observer run and detailed TODOs. This endpoint
   cannot change plans or controls.
6. `GET /api/agent/messages?thread_id=...` can be used by the operator UI to
   display the complete exchange; it does not require the worker token because
   the UI is served through Home Assistant ingress.

The operator sends questions with `POST /api/agent/message` and JSON
`{"message":"..."}`. Inputs are limited to 4,000 characters; replies to
12,000 characters. A message remains visibly `PENDING` or `CLAIMED` until a
worker answers. A configured token only enables the API; it does not mean a
worker is online.

## Regression gates

`tests/test_agent_service.py` verifies the read-only context, message bounds,
claim lifecycle, and absence of planner/PPD/executor/HA dependencies in the
agent service. Existing planner, PPD, executor and offline contract suites
remain release gates and must pass before publication.
