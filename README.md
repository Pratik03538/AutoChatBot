# AutoChatBot

A small Python Selenium script that opens WhatsApp Web in Chrome, keeps its login session in a dedicated local Chrome profile, and prints up to the first 10 visible chat names in the terminal.

The WhatsApp browser script reads chat titles only. It does not open chats, read or save message contents, or send messages.

## Requirements

- Python 3.10 or newer
- Google Chrome installed
- Internet access for WhatsApp Web and the first Selenium install

## Run the WhatsApp chat-list script

From the AutoChatBot folder in Windows CMD:

```cmd
python -m pip install -r requirements.txt
python main.py
```

On the first run, scan the QR code in Chrome and wait for the chat list. Later runs reuse the session saved in `chrome_profile/` unless WhatsApp expires it. The first 10 visible chat titles are printed in the terminal. Press Enter to close Chrome.

If the profile is already open in another Chrome process, close that process before running the script again.

## Create romantic reply drafts

The separate `draft_reply.py` tool creates one short, natural reply draft at a time using Groq's `openai/gpt-oss-20b` model. Groq advertises about 1,000 generated tokens per second for this model. The tool does not access WhatsApp; paste only the message you want help replying to, optionally add context, choose a tone, then review/edit and send the draft yourself.

From the AutoChatBot folder in Windows CMD:

```cmd
python draft_reply.py
```

The first time it runs, the terminal asks for a Groq API key with hidden input. Create a key in the [Groq Console](https://console.groq.com/keys). The key is held only while the program is running; it is not written to a project file. Keep the key private and never commit it.

Groq offers a free tier, but the exact model limits depend on the account and can change. Check the [Groq rate limits page](https://console.groq.com/docs/rate-limits) and your Console's Limits page; this project does not promise unlimited or fixed daily messages. The model's published speed is a provider figure, so actual response time also depends on your internet connection and current service load.

Only the message and optional context pasted into the drafter are sent to Groq's API. Nothing is saved to a local chat history, and the tool cannot read or send WhatsApp messages. Avoid pasting passwords, phone numbers, or private details. Groq says inference inputs and outputs are not retained by default, although it may temporarily log them for reliability or abuse monitoring for up to 30 days; see [Groq's data controls](https://console.groq.com/docs/your-data).

## Privacy

`chrome_profile/` contains browser session data and authentication cookies. It stays on your computer and is excluded by `.gitignore`. The WhatsApp script does not export chat data. Never add the profile, cookies, credentials, message exports, or other private data to GitHub.

WhatsApp Web may change its page layout, which can require updating the chat selectors in `main.py`.
