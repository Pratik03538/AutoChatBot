# AutoChatBot

A Windows CMD Python project with two WhatsApp tools:

- `main.py` opens WhatsApp Web in Chrome using a dedicated local profile and prints up to 10 visible chat names.
- `auto_reply.py` watches only phone numbers listed in local `contacts.json`, uses recent chat context to create replies, and automatically sends them when that contact has `auto_send: true`.
- `draft_reply.py` remains available for manual, paste-a-message reply drafts.

## Requirements

- Python 3.10 or newer
- Google Chrome installed
- Internet access
- A Groq API key for AI replies

Install the dependency from Windows CMD:

```cmd
cd /d C:\Users\Pratik\PycharmProjects\AutoChatBot
venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Automatic replies

The project folder contains a local `contacts.json` with the testing number supplied by the owner. That file is ignored by Git and is not pushed to GitHub. Add future numbers as objects in its `contacts` list. For each number, set `enabled` and `auto_send` to `true` only when you want it monitored and allowed to send.

A 10-digit local number uses the configured `country_code` (currently `91`). A full international number can be entered as `+919876543210`. You can edit `name`, `relationship`, `tone`, and `memory_notes` for each contact. Keep private details in `contacts.json`; the tracked `contacts.example.json` is only a format example.

Run from Windows CMD:

```cmd
cd /d C:\Users\Pratik\PycharmProjects\AutoChatBot
venv\Scripts\activate
run_auto_reply.cmd
```

The first time, the program asks for a Groq API key using hidden terminal input. Create one at the [Groq Console](https://console.groq.com/keys). The key is not written to a project file. The CMD launcher removes the known stale `C:\chromedriver` folder from its temporary PATH so Selenium can use the matching Chrome driver.

When it starts, the bot opens WhatsApp Web with the existing local Chrome profile and waits for the chat list/login. On first launch for each contact, the latest incoming message is saved as a baseline; it is used for context but does not trigger an automatic reply. Later, only newly received text messages from configured contacts are considered. The bot polls every 12 seconds by default, can respond to multiple newly received messages in order, and will not reply to its own outgoing messages. Stop it with Ctrl+C.

For context, it loads up to `memory_messages` recent WhatsApp text messages and keeps that bounded history per contact in local `chat_data/` across restarts. Each AI request receives only the newest `history_messages` messages, the new incoming message, and that contact's `memory_notes`. Media-only messages are ignored. You can change these limits and add persistent style details such as preferred names or inside jokes in `contacts.json`.

The recent messages and notes sent for each reply go to Groq. Message history is also saved locally in `chat_data/` so the bot can remember context across restarts; both `chat_data/` and `contacts.json` are excluded from Git. Groq says inference data is not retained by default, but it may temporarily log data for reliability or abuse checks for up to 30 days; see [Groq data controls](https://console.groq.com/docs/your-data). Avoid including sensitive information in style notes or chat messages.

AI cannot guarantee a perfect reply. If it detects a safety issue, medical/legal/financial decision, serious conflict, or too much ambiguity, it skips automatic sending and prints a human-review notice. Replies are designed to be short, warm, and informed by the recent chat, but adjust each contact's notes if the tone needs tuning.

Groq advertises about 1,000 generated tokens per second for `openai/gpt-oss-20b`; the actual response time depends on network and service load. Free-tier limits vary by account and model. Check the [Groq rate limits page](https://console.groq.com/docs/rate-limits) and the Limits page in your Console; there is no fixed message-count guarantee. WhatsApp Web's layout may change and require selector updates.

## Manual draft mode

To generate one reply from text you paste manually, run:

```cmd
python draft_reply.py
```

This command does not access WhatsApp or send messages.

## Privacy

`chrome_profile/` contains WhatsApp session data and cookies, `contacts.json` contains phone numbers and preferences, and `chat_data/` contains recent message memory and reply-tracking state. These remain local and are ignored by `.gitignore`. Do not commit or share them.
