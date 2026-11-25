# AutoChatBot

Windows Python project for event-driven WhatsApp Web monitoring and Groq-assisted replies.

## What it does

- Opens WhatsApp Web in Chrome using a dedicated persistent local profile at `chrome_profile/`.
- Uses Selenium to install a browser-side JavaScript `MutationObserver`. The observer watches the active conversation for incoming text messages and the chat list for unread changes to explicitly configured contacts.
- Wakes Python through an asynchronous browser event queue. It does not wait for a fixed 12-second polling interval.
- Matches inactive chats by their exact WhatsApp display title (`whatsapp_name`), then opens only the configured phone number and reads recent text context.
- Sends the latest incoming message and bounded recent history to Groq to produce a concise reply in the conversation's language.
- Persists message IDs and chat context locally to avoid routine duplicate replies and to recover messages after transient API errors.
- Uses a safe dry-run default. Real sending requires both global `dry_run: false` and that contact's `auto_send: true`.

## Important latency and reliability limits

This is event-driven browser automation, not a zero-latency messaging transport. The observer reacts as soon as WhatsApp Web renders a DOM change, but actual response time still includes WhatsApp delivery/rendering, Selenium/browser scheduling, Groq inference, internet latency and WhatsApp sending. No script can guarantee zero seconds or error-free delivery.

WhatsApp Web is not a documented automation interface. Its HTML selectors and unread indicators may change. If the chat title or unread marker cannot be identified, the bot fails closed rather than sending to an unverified contact. For dependable production-grade real-time messaging, use the official WhatsApp Business Platform and its webhooks where eligible.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- Google Chrome installed
- Internet access
- Groq API key

Install from PyCharm terminal:

```cmd
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Selenium 4 uses Selenium Manager to locate/download a compatible ChromeDriver. Close other Chrome processes using this project's `chrome_profile` before starting the bot.

## First-time configuration

1. Copy `contacts.example.json` to `contacts.json` in the project root.
2. Add your own WhatsApp number and change `whatsapp_name` to the exact title shown in the WhatsApp Web chat list.
3. Set `enabled: true` for a contact you want monitored.
4. Keep `auto_send: false` and global `dry_run: true` for the first test.
5. Copy `.env.example` to `.env`, then set `GROQ_API_KEY`.

Example contact configuration:

```json
{
  "country_code": "91",
  "dry_run": true,
  "history_messages": 25,
  "memory_messages": 120,
  "contacts": [
    {
      "phone": "9876543210",
      "name": "Local label for this contact",
      "whatsapp_name": "Exact saved name shown in WhatsApp",
      "relationship": "friend",
      "tone": "warm and natural",
      "memory_notes": "",
      "enabled": true,
      "auto_send": false
    }
  ]
}
```

The example number is a placeholder. Replace it before enabling a contact. A 10-digit local number uses `country_code`; a full international number may start with `+`. Configuration flags must be JSON booleans (`true`/`false`), not quoted strings such as `"false"`.

## Run from PyCharm

- Select the project's `.venv` interpreter.
- Run `auto_reply.py`.
- On first launch, Chrome opens WhatsApp Web. Scan the QR code if requested.
- The script visits each configured chat once to create a safe baseline. Messages already present at baseline are not auto-replied to.
- Once the `[READY]` message appears, the MutationObserver monitors new incoming messages and unread chat-list changes.
- Stop the run with Ctrl+C or the PyCharm stop control.

### Terminal diagnostics

A healthy run prints an observer-install line and then an `[WAIT] Alive` status approximately every 15 seconds when idle. When WhatsApp mutates the active chat or a configured chat-list row, it prints an `[EVENT]` line. Newly extracted incoming text appears as `[MESSAGE]`; draft-only mode prints the Groq reply as `[DRY RUN]`.

Check these diagnostic fields:
- `incoming_nodes`: visible WhatsApp incoming message containers in the active chat.
- `message_containers`: visible message wrappers recognised by the current selector.
- `mutations`: how many WhatsApp DOM mutation callbacks the observer has seen.
- `matched_rows`: configured contact rows successfully matched in the chat list.

If `mutations=0`, check that WhatsApp Web is fully loaded and the observer is still installed. If mutations increase but `incoming_nodes=0`, the WhatsApp Web message selectors likely need updating. If incoming nodes are present but no `[MESSAGE]` appears, check the saved message marker in local `chat_data/`; don't delete that state because it protects against duplicates.

You may also run:

```cmd
.venv\Scripts\python.exe auto_reply.py
```

Or double-click `run_auto_reply.cmd`; it prefers `.venv`, supports the older `venv` folder, and falls back to the system `python` command.

## Enabling real automatic sending

Only after testing drafts and confirming the exact contact mapping:

- Set global `dry_run` to `false`.
- Set `auto_send` to `true` only for the individual contacts that explicitly agreed to receive automated replies.
- Leave all other contacts disabled or in draft-only mode.

Both controls are required. A contact with `auto_send: true` still will not receive messages while global `dry_run: true`.

The program only monitors contacts in the local `contacts.json`. Do not configure people who have not agreed to automated replies. Keep replies personal, relevant and low volume; do not use this tool for bulk messaging or unsolicited outreach.

## Groq and privacy

The local `.env` file contains the private API key. Recent WhatsApp text messages and style notes sent for each reply are transmitted to Groq for inference. Chat history, phone numbers, session cookies and message-processing state remain in local files excluded from Git. Avoid putting sensitive personal information in chat notes or sending sensitive conversations to an external model.

The reply prompt asks Groq to match the incoming message's language, use history only as context, avoid inventing facts, and defer sensitive/ambiguous medical, legal, financial, safety or serious-conflict messages to a human. These checks reduce risk but cannot guarantee a correct reply. Review real-send behavior carefully.

## Failure handling

- Transient Groq network, timeout, rate-limit and server errors receive bounded retries. Unprocessed messages remain recoverable and can be retried.
- Authentication/configuration errors are reported without marking the incoming message as processed.
- If WhatsApp send confirmation is uncertain, the message is not automatically resent; manually inspect the chat to avoid duplicates.
- If the saved history marker is no longer present in the visible history, the bot skips that uncertain backlog rather than risk replying twice.
- If WhatsApp changes its DOM selectors or unread indicators, the bot may need a selector update. It does not bypass QR login, CAPTCHA or MFA.

## Tests

Run the standard-library test suite:

```cmd
python -m unittest discover -s tests -v
```

The tests validate phone/config parsing, safe defaults, message-state behavior and bounded Groq retry handling. They do not replace a live WhatsApp Web test.

## Other modes

`main.py` opens WhatsApp Web and lists visible chat names. `draft_reply.py` generates a manual reply from text pasted into the terminal; it does not open WhatsApp or send messages.

## Local files that must stay private

- `chrome_profile/`: WhatsApp session cookies and authentication data
- `.env`: Groq API key
- `contacts.json`: phone numbers and contact preferences
- `chat_data/`: recent chat history and message-processing markers

These paths are excluded by `.gitignore`. Never commit or share them.
