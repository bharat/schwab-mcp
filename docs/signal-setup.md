# Signal approval setup

The Signal approval backend lets you approve or deny trades by replying to a
Signal message on your phone. It talks to a local
[`signal-cli`][signal-cli] daemon over HTTP, so the MCP server never needs a
public inbound endpoint and trade details stay end-to-end encrypted between
the daemon and your phone.

There are two ways to attach the daemon to Signal. **Linking** is simpler and
needs no extra phone number; **registering** gives the bot its own identity.

## Option A — link as a device on your account (recommended)

The daemon joins your existing Signal account as a secondary device, the same
way Signal Desktop does. Approval prompts land in your **Note to Self** thread.

```bash
scripts/signal-daemon up
scripts/signal-daemon link        # opens a QR — scan from your phone:
                                  # Signal → Settings → Linked Devices → +
```

After scanning, both `--signal-account` and `--signal-approver` are **your**
number:

```bash
schwab-mcp server \
  --signal-api-url http://127.0.0.1:8080 \
  --signal-account  +15555550199 \
  --signal-approver +15555550199
```

To unlink later, remove the device from your phone's Linked Devices list.

## Option B — register a separate bot number

If you'd rather the bot have its own identity (and you have a spare number):

```bash
scripts/signal-daemon up
scripts/signal-daemon register +15555550100         # Signal sends an SMS code
scripts/signal-daemon verify   +15555550100 123456  # enter the code
```

Then `--signal-account` is the bot's number and `--signal-approver` is yours:

```bash
schwab-mcp server \
  --signal-api-url http://127.0.0.1:8080 \
  --signal-account  +15555550100 \
  --signal-approver +15555550199
```

## Approving a trade

When a write tool is invoked you receive a Signal message describing the tool
and its arguments. **Reply to that message** (long-press → Reply) with `ok` to
approve or `no` to deny. Anything else, or no reply within `--signal-timeout`
seconds (default 600), is treated as a denial.

You may not configure Discord and Signal approvals at the same time; the
server wires exactly one approval backend.

[signal-cli]: https://github.com/AsamK/signal-cli
[rest]: https://github.com/bbernhard/signal-cli-rest-api
