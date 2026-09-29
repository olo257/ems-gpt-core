# EMS-GPT Agent API

Core now provides a bounded mailbox for a separate EMS analysis agent. The
agent can review future planner slots, completed execution, analytics and
Observer results, then return a written analysis to the operator panel. The
Core does not call a language model by itself; a worker using this protocol
must be configured separately.

## Safety boundary

- The agent context endpoint is read-only and caps each slot list at 96 rows.
- The only agent write endpoint creates an answer to an operator message that
  the same agent previously claimed.
- Agent code has no adapter to the planner, PPD, executor, Home Assistant
  services, commands or settings.
- Agent endpoints require the add-on option `agent_api_token`; the token is
  separate from Home Assistant's Supervisor token. Leave it empty to disable
  all worker endpoints.
- Operator messages are available only through the authenticated Home
  Assistant ingress UI.

## Worker flow

Use the Core ingress base URL and send `Authorization: Bearer <agent_api_token>`
plus `X-EMS-Agent-ID: <stable-worker-name>` on worker requests.

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
5. `GET /api/agent/messages?thread_id=...` can be used by the operator UI to
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
