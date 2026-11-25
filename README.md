# AutoChatBot

A small Python Selenium script that opens WhatsApp Web in Chrome, keeps its login session in a dedicated local Chrome profile, and prints up to the first 10 visible chat names in the terminal.

The script reads chat titles only. It does not open chats, read or save message contents, or send messages.

## Requirements

- Python 3.10 or newer
- Google Chrome installed
- Internet access for WhatsApp Web and the first Selenium install

## Run in PyCharm

1. Open the `AutoChatBot` project in PyCharm.
2. Open the PyCharm Terminal and run:

   ```bash
   python -m pip install -r requirements.txt
   python main.py
   ```

3. On the first run, scan the QR code in Chrome and wait for the chat list. Later runs reuse the session saved in `chrome_profile/` unless WhatsApp expires it.
4. The first 10 visible chat titles are printed in the terminal. Press Enter to close Chrome.

If the profile is already open in another Chrome process, close that process before running the script again.

## Privacy

`chrome_profile/` contains browser session data and authentication cookies. It stays on your computer and is excluded by `.gitignore`. The script does not export chat data. Never add the profile, cookies, credentials, message exports, or other private data to GitHub.

WhatsApp Web may change its page layout, which can require updating the chat selectors in `main.py`.

