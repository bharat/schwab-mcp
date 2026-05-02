# Signal approval setup

The Signal approval backend lets you approve or deny trades by replying to a
Signal message on your phone. It talks to a local
[`signal-cli`][signal-cli] daemon over HTTP, so the MCP server never needs a
public inbound endpoint and trade details stay end-to-end encrypted between
the daemon and your phone.

## What you need

| Thing | Notes |
|---|---|
| A second phone number | The daemon registers its **own** Signal account; it cannot share the number your phone uses. A cheap VoIP number works. |
| `signal-cli-rest-api` running locally | Easiest is the [bbernhard/signal-cli-rest-api][rest] Docker image. |
| Your own number | Passed as `--signal-approver`; only replies from this number are honoured. |

## One-time registration

```bash
# 1. Start the REST daemon (persists state in the named volume)
docker run -d --name signal-api \
  -p 127.0.0.1:8080:8080 \
  -v signal-cli-data:/home/.local/share/signal-cli \
  -e MODE=native \
  bbernhard/signal-cli-rest-api:latest

# 2. Register the bot number (replace with your VoIP number)
curl -X POST http://127.0.0.1:8080/v1/register/+15555550100

# 3. Verify with the SMS code Signal sends to that number
curl -X POST http://127.0.0.1:8080/v1/register/+15555550100/verify/123456
```

Send yourself a test message to confirm:

```bash
curl -X POST http://127.0.0.1:8080/v2/send \
  -H 'Content-Type: application/json' \
  -d '{"number":"+15555550100","recipients":["+15555550199"],"message":"hello"}'
```

## Running the server with Signal approvals

```bash
schwab-mcp server \
  --client-id "$SCHWAB_CLIENT_ID" \
  --client-secret "$SCHWAB_CLIENT_SECRET" \
  --signal-api-url http://127.0.0.1:8080 \
  --signal-account +15555550100 \
  --signal-approver +15555550199
```

When a write tool is invoked you'll receive a Signal message describing the
tool and its arguments. **Reply to that message** (long-press → Reply) with
`ok` to approve or `no` to deny. Anything else, or no reply within
`--signal-timeout` seconds (default 600), is treated as a denial.

You may not configure Discord and Signal approvals at the same time; the
server wires exactly one approval backend.

[signal-cli]: https://github.com/AsamK/signal-cli
[rest]: https://github.com/bbernhard/signal-cli-rest-api
