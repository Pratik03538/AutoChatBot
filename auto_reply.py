"""Auto-reply to newly received messages from explicitly configured contacts."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from draft_reply import HUMAN_REVIEW_REQUIRED, get_api_key, make_draft
from main import start_chrome, wait_for_whatsapp_login


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "contacts.json"
STATE_DIR = PROJECT_DIR / "chat_data"
MESSAGE_CONTAINER_SELECTOR = (
    '#main [data-testid="msg-container"], '
    "#main .message-in, #main .message-out"
)
COMPOSER_SELECTORS = (
    '[data-testid="conversation-compose-box-input"]',
    '#main footer [contenteditable="true"][role="textbox"]',
    '#main footer [contenteditable="true"]',
)
MAX_MESSAGE_CHARS = 700


def normalize_phone(raw_phone: str, country_code: str) -> str:
    """Return WhatsApp's digits-only international number."""
    raw_phone = raw_phone.strip()
    digits = re.sub(r"\D", "", raw_phone)
    if raw_phone.startswith("+"):
        normalized = digits
    elif len(digits) == 10:
        normalized = country_code + digits
    elif digits.startswith(country_code) and len(digits) > 10:
        normalized = digits
    elif 8 <= len(digits) <= 15:
        normalized = digits
    else:
        raise ValueError(
            f"Invalid phone number {raw_phone!r}. Use 10 local digits or full "
            "international format such as +919876543210."
        )

    if not 8 <= len(normalized) <= 15:
        raise ValueError(f"Phone number {raw_phone!r} must have 8 to 15 digits.")
    return normalized


def load_config() -> tuple[list[dict], int, int, int]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            "contacts.json nahi mila. Project folder mein contacts.example.json "
            "ko dekhkar contacts.json banao."
        )

    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    country_code = re.sub(r"\D", "", str(data.get("country_code", "91")))
    if not country_code:
        raise ValueError("contacts.json mein country_code set karein.")

    poll_seconds = max(5, min(120, int(data.get("poll_seconds", 12))))
    history_messages = max(5, min(50, int(data.get("history_messages", 25))))
    memory_messages = max(20, min(300, int(data.get("memory_messages", 120))))

    configured = data.get("contacts")
    if not isinstance(configured, list):
        raise ValueError("contacts.json ka 'contacts' field list hona chahiye.")

    contacts = []
    seen = set()
    for item in configured:
        if not isinstance(item, dict) or not item.get("enabled", False):
            continue
        phone = normalize_phone(str(item.get("phone", "")), country_code)
        if phone in seen:
            raise ValueError(f"Duplicate contact number: {phone}")
        seen.add(phone)
        contact = dict(item)
        contact["phone_e164"] = phone
        contact["name"] = str(item.get("name") or f"+{phone}")
        contact["relationship"] = str(item.get("relationship") or "partner")
        contact["memory_notes"] = str(item.get("memory_notes") or "").strip()
        contact["tone"] = str(item.get("tone") or "romantic and sweet")
        contact["auto_send"] = bool(item.get("auto_send", False))
        contacts.append(contact)

    if not contacts:
        raise ValueError("contacts.json mein kam se kam ek enabled contact add karein.")
    return contacts, poll_seconds, history_messages, memory_messages


def _find_composer(driver):
    for selector in COMPOSER_SELECTORS:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if element.is_displayed():
                return element
    return None


def open_contact_chat(driver, contact: dict) -> None:
    """Open only the configured phone number, using the existing WhatsApp profile."""
    number = contact["phone_e164"]
    driver.get(f"https://web.whatsapp.com/send?phone={number}")
    try:
        WebDriverWait(driver, 45).until(lambda browser: _find_composer(browser))
    except TimeoutException as error:
        raise RuntimeError(
            f"{contact['name']} (+{number}) ki chat nahi khuli. Number WhatsApp "
            "par registered hai ya nahi aur web login check karein."
        ) from error


def _message_direction(driver, element) -> str:
    try:
        return driver.execute_script(
            """
            const node = arguments[0];
            if (node.closest('.message-in')) return 'incoming';
            if (node.closest('.message-out')) return 'outgoing';
            return 'unknown';
            """,
            element,
        )
    except WebDriverException:
        return "unknown"


def _message_text(element) -> str:
    pieces = []
    seen = set()
    for selector in ('span.selectable-text', '[data-testid="selectable-text"]'):
        try:
            for part in element.find_elements(By.CSS_SELECTOR, selector):
                text = (part.text or "").strip()
                if part.is_displayed() and text and text not in seen:
                    seen.add(text)
                    pieces.append(text)
        except WebDriverException:
            continue
    return "\n".join(pieces).strip()


def _message_identity(driver, element, direction: str, text: str) -> str:
    try:
        stable_id = driver.execute_script(
            """
            const node = arguments[0];
            const holder = node.matches('[data-id]') ? node : node.querySelector('[data-id]');
            let meta = '';
            if (node.matches('[data-pre-plain-text]')) {
              meta = node.getAttribute('data-pre-plain-text') || '';
            } else {
              const item = node.querySelector('[data-pre-plain-text]');
              meta = item ? (item.getAttribute('data-pre-plain-text') || '') : '';
            }
            return (holder && holder.getAttribute('data-id')) || meta;
            """,
            element,
        )
    except WebDriverException:
        stable_id = ""
    fingerprint = f"{stable_id}|{direction}|{text}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def extract_visible_messages(driver) -> list[dict[str, str]]:
    elements = driver.find_elements(By.CSS_SELECTOR, MESSAGE_CONTAINER_SELECTOR)
    records = []
    seen = set()
    for element in elements:
        try:
            if not element.is_displayed():
                continue
            direction = _message_direction(driver, element)
            if direction not in {"incoming", "outgoing"}:
                continue
            text = _message_text(element)
            if not text:
                continue
            identity = _message_identity(driver, element, direction, text)
            if identity in seen:
                continue
            seen.add(identity)
            records.append(
                {
                    "id": identity,
                    "speaker": "Her" if direction == "incoming" else "You",
                    "direction": direction,
                    "text": text[:MAX_MESSAGE_CHARS],
                }
            )
        except WebDriverException:
            continue
    return records


def _find_message_scroller(driver):
    return driver.execute_script(
        """
        const root = document.querySelector('#main');
        if (!root) return null;
        const nodes = [root, ...root.querySelectorAll('*')];
        const scrollable = nodes.filter(
          node => node.clientHeight > 120 && node.scrollHeight > node.clientHeight + 80
        );
        scrollable.sort((a, b) => b.scrollHeight - a.scrollHeight);
        return scrollable[0] || null;
        """
    )


def read_recent_history(driver, history_limit: int) -> list[dict[str, str]]:
    """Load a bounded slice of older messages, then return to the newest messages."""
    messages = extract_visible_messages(driver)
    for _ in range(8):
        if len(messages) >= history_limit:
            break
        scroller = _find_message_scroller(driver)
        if scroller is None:
            break
        try:
            previous_top = driver.execute_script("return arguments[0].scrollTop", scroller)
            if previous_top <= 0:
                break
            previous_count = len(messages)
            driver.execute_script("arguments[0].scrollTop = 0", scroller)
            time.sleep(0.4)
            messages = extract_visible_messages(driver)
            if len(messages) <= previous_count and previous_top <= 1:
                break
        except WebDriverException:
            break

    scroller = _find_message_scroller(driver)
    if scroller is not None:
        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight", scroller
            )
            time.sleep(0.25)
        except WebDriverException:
            pass

    messages = extract_visible_messages(driver)
    return messages[-history_limit:]


def _merge_memory(
    saved: list[dict[str, str]],
    live: list[dict[str, str]],
    memory_limit: int,
) -> list[dict[str, str]]:
    if not saved:
        return live[-memory_limit:]

    saved_index = {message.get("id"): index for index, message in enumerate(saved)}
    overlap = next(
        (
            (live_index, saved_index[message.get("id")])
            for live_index, message in enumerate(live)
            if message.get("id") in saved_index
        ),
        None,
    )
    if overlap:
        _, first_saved_index = overlap
        combined = saved[:first_saved_index] + live
    else:
        combined = live

    unique = []
    seen = set()
    for message in combined:
        message_id = message.get("id")
        if message_id and message_id not in seen:
            seen.add(message_id)
            unique.append(message)
    return unique[-memory_limit:]


def _state_file(phone: str) -> Path:
    digest = hashlib.sha256(phone.encode("utf-8")).hexdigest()[:20]
    return STATE_DIR / f"{digest}.json"


def _read_state(phone: str) -> dict:
    path = _state_file(phone)
    if not path.exists():
        return {"last_processed_id": None, "history": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"last_processed_id": None, "history": []}
    except (OSError, json.JSONDecodeError):
        return {"last_processed_id": None, "history": []}


def _write_state(phone: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_file(phone)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _contact_context(contact: dict) -> str:
    lines = [
        f"Relationship: {contact['relationship']}",
        f"Name or label: {contact['name']}",
    ]
    if contact["memory_notes"]:
        lines.append(f"Long-term notes from account owner: {contact['memory_notes']}")
    return "\n".join(lines)


def _send_message(driver, draft: str) -> bool:
    text = " ".join(draft.split()).strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise RuntimeError("AI reply khaali ya bahut lamba tha; send nahi kiya.")

    composer = _find_composer(driver)
    if composer is None:
        raise RuntimeError("WhatsApp message box nahi mila; reply send nahi kiya.")
    current_text = (composer.get_attribute("textContent") or composer.text or "").strip()
    if current_text:
        raise RuntimeError(
            "WhatsApp message box mein pehle se unsent text hai; usse chhede bina ruk gaya."
        )

    before_ids = {
        message["id"]
        for message in extract_visible_messages(driver)
        if message["direction"] == "outgoing"
    }
    composer.click()
    composer.send_keys(text)
    composer.send_keys(Keys.ENTER)

    def sent(browser) -> bool:
        return any(
            message["direction"] == "outgoing"
            and message["text"].strip() == text
            and message["id"] not in before_ids
            for message in extract_visible_messages(browser)[-8:]
        )

    try:
        WebDriverWait(driver, 12).until(sent)
        return True
    except TimeoutException:
        return False


def _handle_new_messages(
    driver,
    contact: dict,
    live: list[dict[str, str]],
    state: dict,
    api_key: str,
    memory_limit: int,
    history_limit: int,
) -> None:
    history = _merge_memory(state.get("history", []), live, memory_limit)
    state["history"] = history

    incoming = [message for message in history if message["direction"] == "incoming"]
    if not state.get("initialized", False):
        state["initialized"] = True
        if incoming:
            state["last_processed_id"] = incoming[-1]["id"]
            print(
                f"[READY] {contact['name']} (+{contact['phone_e164']}): "
                "purane messages context ke liye load hue; naye message ka wait hai."
            )
        else:
            print(
                f"[READY] {contact['name']}: chat baseline set; "
                "ab naye incoming messages ka wait hai."
            )
        _write_state(contact["phone_e164"], state)
        return

    if not incoming:
        _write_state(contact["phone_e164"], state)
        return

    last_processed_id = state.get("last_processed_id")
    ordered_ids = [message["id"] for message in history]
    if last_processed_id is None:
        marker_index = -1
    elif last_processed_id in ordered_ids:
        marker_index = ordered_ids.index(last_processed_id)
    else:
        if incoming[-1]["id"] != last_processed_id:
            state["last_processed_id"] = incoming[-1]["id"]
            _write_state(contact["phone_e164"], state)
            print(
                f"[SKIP] {contact['name']}: purana marker history mein nahi mila; "
                "duplicate ya purana reply bhejne se bachne ke liye latest message skip kiya."
            )
        return

    pending_indexes = [
        index
        for index in range(marker_index + 1, len(history))
        if history[index]["direction"] == "incoming"
    ]
    if not pending_indexes:
        _write_state(contact["phone_e164"], state)
        return

    timeline = history[: marker_index + 1]
    cursor = marker_index + 1
    for source_index in pending_indexes:
        timeline.extend(history[cursor : source_index + 1])
        cursor = source_index + 1
        current = history[source_index]

        # Save first so an interrupted send cannot be accidentally duplicated.
        state["last_processed_id"] = current["id"]
        state["history"] = history[-memory_limit:]
        _write_state(contact["phone_e164"], state)

        prompt_history = [
            {"speaker": message["speaker"], "text": message["text"]}
            for message in timeline[-history_limit - 1 : -1]
        ]
        try:
            draft = make_draft(
                api_key=api_key,
                message=current["text"],
                context=_contact_context(contact),
                tone=contact["tone"],
                history=prompt_history,
            )
        except RuntimeError as error:
            print(
                f"[NO REPLY] {contact['name']}: AI request failed ({error}). "
                "Is message ko manually check karein."
            )
            break

        if draft == HUMAN_REVIEW_REQUIRED:
            print(
                f"[REVIEW] {contact['name']}: situation ko insaan khud handle kare; "
                "automatic reply nahi bheja."
            )
            continue

        if not contact["auto_send"]:
            print(f"[DRAFT] {contact['name']}: {draft}")
            continue

        try:
            sent_ok = _send_message(driver, draft)
        except (RuntimeError, WebDriverException) as error:
            print(
                f"[SEND ERROR] {contact['name']}: {error}. "
                "WhatsApp chat manually check karein."
            )
            break

        if not sent_ok:
            print(
                f"[CHECK] {contact['name']}: send confirm nahi hua. "
                "WhatsApp chat manually check karein; duplicate se bachne ke liye "
                "same incoming message dobara auto-send nahi hoga."
            )
            break

        assistant_message = {
            "id": hashlib.sha256(
                f"auto-reply|{current['id']}|{draft}".encode("utf-8")
            ).hexdigest(),
            "speaker": "You",
            "direction": "outgoing",
            "text": " ".join(draft.split())[:MAX_MESSAGE_CHARS],
        }
        timeline.append(assistant_message)
        print(f"[SENT] {contact['name']} (+{contact['phone_e164']}) ke liye reply bhej diya.")


def main() -> None:
    driver = None
    try:
        contacts, poll_seconds, history_limit, memory_limit = load_config()
        if not contacts:
            raise ValueError("Koi enabled contact nahi hai.")

        print("Enabled contacts:")
        for contact in contacts:
            mode = "AUTO SEND" if contact["auto_send"] else "DRAFT ONLY"
            print(f"  - {contact['name']}: +{contact['phone_e164']} ({mode})")
        print(
            "\nBot sirf listed contacts ke naye incoming text messages dekhega. "
            "Purani chat context ke liye padhi jayegi; startup par purane message "
            "ka reply nahi bhejega. Ctrl+C se band karein."
        )
        api_key = get_api_key()
        driver = start_chrome()
        driver.get("https://web.whatsapp.com/")
        wait_for_whatsapp_login(driver)

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        active_phone = None
        print(
            f"\nMonitoring shuru. Har {poll_seconds} second mein configured chats check hongi."
        )
        while True:
            for contact in contacts:
                if active_phone != contact["phone_e164"]:
                    try:
                        open_contact_chat(driver, contact)
                        active_phone = contact["phone_e164"]
                    except RuntimeError as error:
                        print(f"[WAIT] {error}")
                        active_phone = None
                        continue

                live = read_recent_history(driver, memory_limit)
                state = _read_state(contact["phone_e164"])
                if not live:
                    if not state.get("initialized", False):
                        state["initialized"] = True
                        state["last_processed_id"] = None
                        state["history"] = []
                        _write_state(contact["phone_e164"], state)
                        print(
                            f"[READY] {contact['name']}: empty chat baseline set; "
                            "ab naye incoming messages ka wait hai."
                        )
                    else:
                        print(
                            f"[WAIT] {contact['name']}: visible text messages nahi mile; "
                            "WhatsApp layout ya number check karein."
                        )
                    continue

                _handle_new_messages(
                    driver,
                    contact,
                    live,
                    state,
                    api_key,
                    memory_limit,
                    history_limit,
                )
            time.sleep(poll_seconds)
    except (EOFError, KeyboardInterrupt):
        print("\nAutoChatBot band ho gaya.")
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"\n[ERROR] {error}")
    except WebDriverException as error:
        print(f"\n[ERROR] WhatsApp/Chrome control nahi ho paya: {error}")
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
