"""Event-driven WhatsApp Web auto-replies for explicitly configured contacts.

A browser-side MutationObserver detects incoming message and chat-list DOM changes.
Selenium waits on that event queue instead of polling WhatsApp at a fixed interval.
WhatsApp Web and Groq network/inference latency mean zero-second delivery cannot be guaranteed.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from draft_reply import GroqAPIError, HUMAN_REVIEW_REQUIRED, get_api_key, make_draft
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
OBSERVER_WAIT_SECONDS = 15


OBSERVER_INSTALL_SCRIPT = r"""
const contacts = arguments[0] || [];
const activePhone = arguments[1] || null;
const emitInitialUnread = Boolean(arguments[2]);
if (window.__autochatObserver?.observer) window.__autochatObserver.observer.disconnect();

const eventKey = event => [
  event.type, event.phone || "", event.message_id || "", event.snapshot || ""
].join("|");
const takeEvents = () => {
  const items = state.events.splice(0, state.events.length);
  for (const item of items) state.eventKeys.delete(eventKey(item));
  return items;
};
const state = {
  contacts, activePhone, events: [], waiters: [], seenMessages: new Set(),
  rowSnapshots: new Map(), initialized: false, observer: null,
  scheduled: false, eventKeys: new Set(),
  stats: {scans: 0, mutations: 0, lastMutationAt: Date.now(), incomingNodes: 0,
          outgoingNodes: 0, matchedRows: 0, activePhone: activePhone}
};
window.__autochatObserver = state;

const norm = value => String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
const contactForLabel = label => {
  const key = norm(label);
  if (!key) return null;
  return contacts.find(c => norm(c.whatsapp_name) === key) || null;
};
const pushEvent = event => {
  const key = eventKey(event);
  if (state.eventKeys.has(key)) return;
  state.eventKeys.add(key);
  state.events.push(event);
  while (state.events.length > 500) {
    const dropped = state.events.shift();
    state.eventKeys.delete(eventKey(dropped));
  }
  while (state.waiters.length && state.events.length) {
    const waiter = state.waiters.shift();
    clearTimeout(waiter.timer);
    waiter.done(takeEvents());
  }
};
const readText = element => {
  const values = [];
  const seen = new Set();
  for (const node of element.querySelectorAll('span.selectable-text, [data-testid="selectable-text"]')) {
    const value = (node.innerText || node.textContent || "").trim();
    if (value && !seen.has(value)) {
      seen.add(value);
      values.push(value);
    }
  }
  return values.join("\n").slice(0, 700);
};
const messageIdentity = element => {
  const holder = element.matches("[data-id]") ? element : element.querySelector("[data-id]");
  const id = holder?.getAttribute("data-id");
  if (id) return id;
  return element.matches("[data-pre-plain-text]")
    ? (element.getAttribute("data-pre-plain-text") || "")
    : (element.querySelector("[data-pre-plain-text]")?.getAttribute("data-pre-plain-text") || "");
};
const activeContact = () => {
  const header = document.querySelector('#main header');
  if (header) {
    const titleCandidates = [
      ...Array.from(header.querySelectorAll('[title]'), n => n.getAttribute('title') || ''),
      ...Array.from(header.querySelectorAll('[aria-label]'), n => n.getAttribute('aria-label') || '')
    ];
    for (const title of titleCandidates) {
      const match = contactForLabel(title);
      if (match) return match;
    }
  }
  // Selenium navigates to an explicitly configured phone number before setting
  // the observer. WhatsApp may render an avatar/status title we cannot map.
  return state.activePhone
    ? (contacts.find(c => c.phone_e164 === state.activePhone) || null)
    : null;
};
const unreadPresent = row => {
  const indicators = row.querySelectorAll(
    '[data-testid*="unread" i], [aria-label*="unread" i], [title*="unread" i], ' +
    '[aria-label*="new message" i], [data-icon*="unread" i]'
  );
  if (indicators.length) return true;
  const accessible = [
    row.getAttribute("aria-label") || "",
    ...Array.from(row.querySelectorAll("[aria-label]"), n => n.getAttribute("aria-label") || "")
  ].join(" ");
  return /\bunread\b|\bnew messages?\b/i.test(accessible);
};
const rowLabel = row => {
  const candidates = [
    ...Array.from(row.querySelectorAll('[data-testid="cell-frame-title"][title]'), n => n.getAttribute('title') || ''),
    ...Array.from(row.querySelectorAll('span[title], [title]'), n => n.getAttribute('title') || ''),
    ...Array.from(row.querySelectorAll('[aria-label]'), n => n.getAttribute('aria-label') || '')
  ];
  for (const candidate of candidates) {
    const match = contactForLabel(candidate);
    if (match) return match.whatsapp_name;
  }
  return candidates.find(Boolean) || '';
};
const scan = () => {
  state.stats.scans += 1;
  const first = !state.initialized;
  const active = activeContact();
  const phone = active?.phone_e164 || state.activePhone;
  state.stats.activePhone = phone;
  state.stats.incomingNodes = document.querySelectorAll('#main .message-in, #main [data-testid="msg-container"].message-in').length;
  state.stats.outgoingNodes = document.querySelectorAll('#main .message-out').length;

  if (phone) {
    const nodes = document.querySelectorAll(
      '#main [data-testid="msg-container"], #main .message-in, #main .message-out'
    );
    for (const node of nodes) {
      const incoming = Boolean(node.closest(".message-in") || node.classList.contains("message-in"));
      const outgoing = Boolean(node.closest(".message-out") || node.classList.contains("message-out"));
      if (!incoming && !outgoing) continue;
      const text = readText(node);
      if (!text) continue;
      const rawId = messageIdentity(node);
      const id = rawId || ((incoming ? "in:" : "out:") + text);
      const unique = phone + "|" + id + "|" + (incoming ? "in" : "out");
      if (first) {
        state.seenMessages.add(unique);
      } else if (incoming && !state.seenMessages.has(unique)) {
        state.seenMessages.add(unique);
        pushEvent({type: "incoming", phone, message_id: rawId, text, observed_at: Date.now()});
      } else {
        state.seenMessages.add(unique);
      }
    }
  }

  const pane = document.querySelector("#pane-side");
  if (pane) {
    const rows = pane.querySelectorAll(
      '[data-testid="cell-frame-container"], [role="listitem"], [role="row"]'
    );
    let matchedRows = 0;
    for (const row of rows) {
      if (!row.isConnected) continue;
      const contact = contactForLabel(rowLabel(row));
      if (!contact) continue;
      matchedRows += 1;
      const unread = unreadPresent(row);
      const snapshot = norm(row.innerText || "") + "|" + norm(row.getAttribute("aria-label") || "") + "|" + unread;
      const previous = state.rowSnapshots.get(contact.phone_e164);
      state.rowSnapshots.set(contact.phone_e164, snapshot);
      if (unread && first && emitInitialUnread) {
        pushEvent({
          type: "unread_contact",
          phone: contact.phone_e164,
          chat_name: contact.whatsapp_name,
          snapshot,
          observed_at: Date.now()
        });
      } else if (!first && previous !== undefined && previous !== snapshot) {
        // A preview change also signals an incoming message on builds where
        // WhatsApp no longer exposes a stable unread badge selector.
        pushEvent({
          type: unread ? "unread_contact" : "chat_changed",
          phone: contact.phone_e164,
          chat_name: contact.whatsapp_name,
          snapshot,
          observed_at: Date.now()
        });
      }
    }
    state.stats.matchedRows = matchedRows;
  }
  state.initialized = true;
};
const scheduleScan = records => {
  state.stats.mutations += 1;
  state.stats.lastMutationAt = Date.now();
  const relevant = (records || []).some(record => {
    const target = record.target?.nodeType === 1 ? record.target : record.target?.parentElement;
    if (target?.closest?.("#main, #pane-side")) return true;
    for (const node of Array.from(record.addedNodes || [])) {
      if (node.nodeType !== 1) continue;
      if (node.matches?.("#main, #pane-side") || node.closest?.("#main, #pane-side")) return true;
    }
    return false;
  });
  const now = Date.now();
  if (relevant && state.activePhone && now - (state.stats.lastWakeAt || 0) >= 350) {
    state.stats.lastWakeAt = now;
    pushEvent({
      type: "dom_changed",
      phone: state.activePhone,
      snapshot: String(Math.floor(now / 350)),
      observed_at: now
    });
  }
  if (state.scheduled) return;
  state.scheduled = true;
  queueMicrotask(() => {
    state.scheduled = false;
    try { scan(); } catch (error) { /* transient WhatsApp DOM changes are harmless */ }
  });
};

try {
  scan();
  state.observer = new MutationObserver(scheduleScan);
  state.observer.observe(document.body, {
    subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ["aria-label", "data-testid", "title", "class"]
  });
  return {
    installed: true,
    mappedContacts: contacts.length,
    activePhone: state.stats.activePhone,
    incomingNodes: state.stats.incomingNodes,
    outgoingNodes: state.stats.outgoingNodes,
    matchedRows: state.stats.matchedRows
  };
} catch (error) {
  return {installed: false, error: String(error)};
}
"""

OBSERVER_WAIT_SCRIPT = r"""
const timeoutMs = Math.max(100, Number(arguments[0] || 15000));
const done = arguments[arguments.length - 1];
const state = window.__autochatObserver;
if (!state) return done([]);
const takeEvents = () => {
  const items = state.events.splice(0, state.events.length);
  for (const item of items) {
    const key = [item.type, item.phone || "", item.message_id || "", item.snapshot || ""].join("|");
    state.eventKeys.delete(key);
  }
  return items;
};
if (state.events.length) return done(takeEvents());
let settled = false;
const waiter = {
  done: events => {
    if (settled) return;
    settled = true;
    done(events);
  },
  timer: null
};
waiter.timer = setTimeout(() => {
  if (settled) return;
  settled = true;
  const index = state.waiters.indexOf(waiter);
  if (index >= 0) state.waiters.splice(index, 1);
  done([]);
}, timeoutMs);
state.waiters.push(waiter);
"""


def normalize_phone(raw_phone: str, country_code: str) -> str:
    """Return a validated digits-only international phone number."""
    raw_phone = str(raw_phone or "").strip()
    country_code = re.sub(r"\D", "", str(country_code or ""))
    digits = re.sub(r"\D", "", raw_phone)
    if not digits:
        raise ValueError("Contact phone number is empty.")
    if raw_phone.startswith("+"):
        normalized = digits
    elif len(digits) == 10:
        if not country_code:
            raise ValueError("Set a valid country_code for 10-digit local numbers.")
        normalized = country_code + digits
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


def _strict_bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be true or false (JSON boolean), not a string/number.")
    return value


def load_config() -> tuple[list[dict], int, int, bool]:
    if not CONFIG_FILE.is_file():
        raise FileNotFoundError(
            "contacts.json nahi mila. contacts.example.json ko contacts.json naam se copy karein."
        )
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"contacts.json JSON format invalid hai: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("contacts.json ka top-level value JSON object hona chahiye.")

    country_code = re.sub(r"\D", "", str(data.get("country_code", "91")))
    if not country_code:
        raise ValueError("contacts.json mein country_code set karein.")
    dry_run = _strict_bool(data.get("dry_run"), "dry_run", True)
    history_messages = max(5, min(50, int(data.get("history_messages", 25))))
    memory_messages = max(20, min(300, int(data.get("memory_messages", 120))))

    configured = data.get("contacts")
    if not isinstance(configured, list):
        raise ValueError("contacts.json ka 'contacts' field list hona chahiye.")

    contacts: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(configured, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"contacts[{index - 1}] JSON object hona chahiye.")
        enabled = _strict_bool(item.get("enabled"), f"contacts[{index - 1}].enabled", False)
        auto_send = _strict_bool(item.get("auto_send"), f"contacts[{index - 1}].auto_send", False)
        if not enabled:
            continue
        phone = normalize_phone(str(item.get("phone", "")), country_code)
        if phone in seen:
            raise ValueError(f"Duplicate contact number: {phone}")
        seen.add(phone)
        name = str(item.get("name") or f"+{phone}").strip()
        whatsapp_name = str(item.get("whatsapp_name") or name).strip()
        if not whatsapp_name:
            raise ValueError(f"contacts[{index - 1}].whatsapp_name cannot be empty.")
        contact = dict(item)
        contact.update({
            "phone_e164": phone,
            "name": name,
            "whatsapp_name": whatsapp_name,
            "relationship": str(item.get("relationship") or "friend"),
            "memory_notes": str(item.get("memory_notes") or "").strip(),
            "tone": str(item.get("tone") or "warm and natural"),
            "auto_send": auto_send,
        })
        contacts.append(contact)

    if not contacts:
        raise ValueError("contacts.json mein kam se kam ek enabled contact add karein.")
    return contacts, history_messages, memory_messages, dry_run


def _find_composer(driver):
    for selector in COMPOSER_SELECTORS:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if element.is_displayed():
                return element
    return None


def open_contact_chat(driver, contact: dict) -> None:
    """Open only the configured phone number in the existing WhatsApp session."""
    number = contact["phone_e164"]
    driver.get(f"https://web.whatsapp.com/send?phone={number}")
    try:
        WebDriverWait(driver, 45).until(lambda browser: _find_composer(browser))
    except TimeoutException as error:
        raise RuntimeError(
            f"{contact['name']} (+{number}) ki chat nahi khuli. Number WhatsApp par "
            "registered hai ya web login/session check karein."
        ) from error


def install_dom_observer(driver, contacts: list[dict], active_phone: str | None, emit_initial_unread: bool) -> None:
    payload = [
        {"phone_e164": c["phone_e164"], "whatsapp_name": c["whatsapp_name"]}
        for c in contacts
    ]
    result = driver.execute_script(
        OBSERVER_INSTALL_SCRIPT, payload, active_phone, emit_initial_unread
    )
    if not isinstance(result, dict) or not result.get("installed"):
        raise RuntimeError(f"WhatsApp DOM observer install nahi hua: {result}")
    active = result.get("activePhone") or active_phone or "unknown"
    print(
        f"[OBSERVER] Installed | active={active} | "
        f"incoming_nodes={result.get('incomingNodes', 0)} | "
        f"outgoing_nodes={result.get('outgoingNodes', 0)} | "
        f"matched_contact_rows={result.get('matchedRows', 0)}",
        flush=True,
    )


def _observer_status(driver) -> dict:
    return driver.execute_script(
        """
        const state = window.__autochatObserver;
        const main = document.querySelector('#main');
        const header = main?.querySelector('header');
        return {
          installed: Boolean(state?.observer),
          activePhone: state?.stats?.activePhone || state?.activePhone || null,
          scans: state?.stats?.scans || 0,
          mutations: state?.stats?.mutations || 0,
          incomingNodes: main?.querySelectorAll('.message-in').length || 0,
          outgoingNodes: main?.querySelectorAll('.message-out').length || 0,
          messageContainers: main?.querySelectorAll('[data-testid="msg-container"]').length || 0,
          paneRows: document.querySelectorAll('#pane-side [data-testid="cell-frame-container"]').length || 0,
          matchedRows: state?.stats?.matchedRows || 0,
          lastMutationAt: state?.stats?.lastMutationAt || 0,
          headerText: (header?.innerText || '').trim().slice(0, 100),
          url: location.href
        };
        """
    )


def wait_for_dom_events(driver, timeout_seconds: float = OBSERVER_WAIT_SECONDS) -> list[dict]:
    """Wait on the MutationObserver queue, without fixed-interval DOM polling."""
    try:
        observer_exists = driver.execute_script(
            "return Boolean(window.__autochatObserver && window.__autochatObserver.observer)"
        )
        if not observer_exists:
            return [{"type": "observer_missing"}]
        result = driver.execute_async_script(
            OBSERVER_WAIT_SCRIPT, int(max(0.1, timeout_seconds) * 1000)
        )
        return result if isinstance(result, list) else []
    except WebDriverException as error:
        raise RuntimeError(
            f"WhatsApp DOM event wait fail hua: {error}. Page reload karke retry karein."
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
    records: list[dict[str, str]] = []
    seen: set[str] = set()
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
            records.append({
                "id": identity,
                "speaker": "Contact" if direction == "incoming" else "You",
                "direction": direction,
                "text": text[:MAX_MESSAGE_CHARS],
            })
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
    """Accumulate older virtualized messages instead of replacing each scroll slice."""
    collected: list[dict[str, str]] = []
    seen: set[str] = set()

    for _ in range(12):
        visible = extract_visible_messages(driver)
        fresh = [message for message in visible if message["id"] not in seen]
        if fresh:
            for message in fresh:
                seen.add(message["id"])
            collected = fresh + collected

        scroller = _find_message_scroller(driver)
        if scroller is None:
            break
        try:
            position = driver.execute_script("return arguments[0].scrollTop", scroller)
            if position <= 1:
                break
            driver.execute_script(
                "arguments[0].scrollTop = Math.max(0, arguments[0].scrollTop - "
                "Math.max(300, arguments[0].clientHeight * 0.75))",
                scroller,
            )
            time.sleep(0.2)
        except WebDriverException:
            break

    scroller = _find_message_scroller(driver)
    if scroller is not None:
        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight", scroller
            )
            time.sleep(0.2)
        except WebDriverException:
            pass

    latest = extract_visible_messages(driver)
    for message in latest:
        if message["id"] not in seen:
            seen.add(message["id"])
            collected.append(message)
    return collected[-history_limit:]


def _merge_memory(
    saved: list[dict[str, str]],
    live: list[dict[str, str]],
    memory_limit: int,
) -> list[dict[str, str]]:
    if not saved:
        return live[-memory_limit:]
    saved_index = {message.get("id"): index for index, message in enumerate(saved)}
    overlap = next(
        (saved_index[message.get("id")] for message in live if message.get("id") in saved_index),
        None,
    )
    combined = saved[:overlap] + live if overlap is not None else live
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
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
        return {"initialized": False, "last_processed_id": None, "history": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("state root is not an object")
        value.setdefault("initialized", False)
        value.setdefault("last_processed_id", None)
        value.setdefault("history", [])
        return value
    except (OSError, json.JSONDecodeError, ValueError):
        raise RuntimeError(f"Local chat state corrupt hai: {path}. Backup karke inspect karein.") from None


def _write_state(phone: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_file(phone)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _contact_context(contact: dict) -> str:
    lines = [
        f"Relationship: {contact['relationship']}",
        f"Name or label: {contact['name']}",
    ]
    if contact["memory_notes"]:
        lines.append(f"Style notes supplied by the account owner: {contact['memory_notes']}")
    return "\n".join(lines)


def _send_message(driver, draft: str) -> bool:
    text = " ".join(draft.split()).strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise RuntimeError("AI reply khaali ya 700 characters se lamba tha; send nahi kiya.")

    composer = _find_composer(driver)
    if composer is None:
        raise RuntimeError("WhatsApp message box nahi mila; reply send nahi kiya.")
    current_text = (composer.get_attribute("textContent") or composer.text or "").strip()
    if current_text:
        raise RuntimeError(
            "WhatsApp message box mein pehle se unsent text hai; use chhede bina ruk gaya."
        )

    before_ids = {
        message["id"]
        for message in extract_visible_messages(driver)
        if message["direction"] == "outgoing"
    }
    attempted = False
    try:
        composer.click()
        composer.send_keys(text)
        attempted = True
        composer.send_keys(Keys.ENTER)
    except WebDriverException as error:
        if attempted:
            raise SendUncertain(
                "Send ke baad browser error aaya; duplicate se bachne ke liye auto-resend nahi kiya. "
                "WhatsApp chat manually verify karein."
            ) from error
        raise

    def sent(browser) -> bool:
        return any(
            message["direction"] == "outgoing"
            and " ".join(message["text"].split()).strip() == text
            and message["id"] not in before_ids
            for message in extract_visible_messages(browser)[-12:]
        )

    try:
        WebDriverWait(driver, 15).until(sent)
        return True
    except TimeoutException as error:
        raise SendUncertain(
            "WhatsApp ne send confirm nahi kiya. Chat manually check karein; "
            "duplicate se bachne ke liye same incoming message auto-retry nahi hoga."
        ) from error


class SendUncertain(RuntimeError):
    """The send may have reached WhatsApp, so automatic resend is unsafe."""


def _pending_messages(history: list[dict[str, str]], state: dict) -> list[dict[str, str]]:
    incoming = [message for message in history if message["direction"] == "incoming"]
    marker = state.get("last_processed_id")
    if marker is None:
        return incoming
    ids = [message["id"] for message in history]
    if marker not in ids:
        if incoming and incoming[-1]["id"] != marker:
            print(
                "[SKIP] Last processed marker history se bahar hai; uncertain backlog skip "
                "kiya aur future messages ke liye baseline latest incoming par advance ki."
            )
            # The current bounded history cannot establish which messages are new.
            # Skip this ambiguous backlog once, but recover the watcher for later messages.
            state["last_processed_id"] = incoming[-1]["id"]
        return []
    marker_index = ids.index(marker)
    return [
        message for index, message in enumerate(history)
        if index > marker_index and message["direction"] == "incoming"
    ]


def _handle_new_messages(
    driver,
    contact: dict,
    live: list[dict[str, str]],
    state: dict,
    api_key: str,
    memory_limit: int,
    history_limit: int,
    dry_run: bool,
) -> None:
    history = _merge_memory(state.get("history", []), live, memory_limit)
    if not state.get("initialized", False):
        incoming = [message for message in history if message["direction"] == "incoming"]
        state.update({
            "initialized": True,
            "last_processed_id": incoming[-1]["id"] if incoming else None,
            "history": history,
        })
        _write_state(contact["phone_e164"], state)
        print(f"[BASELINE] {contact['name']}: existing messages ko reply nahi bheja.")
        return

    pending = _pending_messages(history, state)
    if not pending:
        state["history"] = history
        _write_state(contact["phone_e164"], state)
        return

    timeline = list(history)
    sent_context: list[dict[str, str]] = []
    for current in pending:
        current_index = next(
            (index for index, message in enumerate(history) if message["id"] == current["id"]),
            len(history),
        )
        prior_messages = history[:current_index] + sent_context
        prompt_history = [
            {"speaker": message["speaker"], "text": message["text"]}
            for message in prior_messages
        ][-history_limit:]

        # The processed marker advances only after a draft/review/send outcome.
        # A Groq failure leaves this incoming message recoverable.
        draft = make_draft(
            api_key=api_key,
            message=current["text"],
            context=_contact_context(contact),
            tone=contact["tone"],
            history=prompt_history,
        )

        if draft == HUMAN_REVIEW_REQUIRED:
            state["last_processed_id"] = current["id"]
            state["history"] = history[-memory_limit:]
            _write_state(contact["phone_e164"], state)
            print(f"[REVIEW] {contact['name']}: sensitive/ambiguous message; human review needed.")
            continue

        if not contact["auto_send"] or dry_run:
            mode = "DRY RUN" if dry_run else "DRAFT ONLY"
            print(f"[{mode}] {contact['name']}: {draft}")
            state["last_processed_id"] = current["id"]
            state["history"] = history[-memory_limit:]
            _write_state(contact["phone_e164"], state)
            continue

        # Journal the send intent before touching WhatsApp. If the process dies
        # between Enter and the state commit, startup recovery will not blindly resend.
        state["pending_send"] = {
            "incoming_id": current["id"],
            "draft": draft,
            "started_at": time.time(),
        }
        _write_state(contact["phone_e164"], state)
        try:
            _send_message(driver, draft)
        except SendUncertain as error:
            state["last_processed_id"] = current["id"]
            state.pop("pending_send", None)
            state["history"] = history[-memory_limit:]
            _write_state(contact["phone_e164"], state)
            print(f"[SEND CHECK] {contact['name']}: {error}")
            continue
        except RuntimeError:
            # These errors occur before a send attempt (e.g. composer missing or
            # an existing unsent draft); clear the journal so a safe retry can run.
            state.pop("pending_send", None)
            _write_state(contact["phone_e164"], state)
            raise

        assistant_message = {
            "id": hashlib.sha256(
                f"auto-reply|{current['id']}|{draft}".encode("utf-8")
            ).hexdigest(),
            "speaker": "You",
            "direction": "outgoing",
            "text": " ".join(draft.split())[:MAX_MESSAGE_CHARS],
        }
        timeline_index = next(
            (index for index, message in enumerate(timeline) if message["id"] == current["id"]),
            len(timeline) - 1,
        )
        timeline.insert(timeline_index + 1, assistant_message)
        sent_context.append(assistant_message)
        state["last_processed_id"] = current["id"]
        state.pop("pending_send", None)
        state["history"] = timeline[-memory_limit:]
        _write_state(contact["phone_e164"], state)
        print(f"[SENT] {contact['name']}: reply sent and verified.")


def _baseline_contact(driver, contact: dict, history_limit: int, memory_limit: int) -> bool:
    """Create a safe startup baseline, or report whether previously pending messages exist."""
    open_contact_chat(driver, contact)
    live = read_recent_history(driver, memory_limit)
    state = _read_state(contact["phone_e164"])
    history = _merge_memory(state.get("history", []), live, memory_limit)
    if not state.get("initialized", False):
        incoming = [message for message in history if message["direction"] == "incoming"]
        state.update({
            "initialized": True,
            "last_processed_id": incoming[-1]["id"] if incoming else None,
            "history": history,
        })
        _write_state(contact["phone_e164"], state)
        print(f"[BASELINE] {contact['name']}: existing chat captured; old messages won't trigger replies.")
        return False

    pending_send = state.get("pending_send")
    if isinstance(pending_send, dict):
        incoming_id = pending_send.get("incoming_id")
        draft = " ".join(str(pending_send.get("draft", "")).split()).strip()
        outgoing_texts = {
            " ".join(message["text"].split()).strip()
            for message in live if message["direction"] == "outgoing"
        }
        state["last_processed_id"] = incoming_id or state.get("last_processed_id")
        state.pop("pending_send", None)
        if draft and draft in outgoing_texts:
            print(f"[RECOVERY] {contact['name']}: send history mein mil gaya; duplicate resend roka.")
        else:
            print(
                f"[RECOVERY CHECK] {contact['name']}: previous send outcome uncertain. "
                "Chat manually verify karein; duplicate se bachne ke liye auto-resend nahi hoga."
            )

    pending = _pending_messages(history, state)
    state["history"] = history
    _write_state(contact["phone_e164"], state)
    return bool(pending)


def _process_contact(
    driver,
    contact: dict,
    contacts: list[dict],
    active_phone: str | None,
    api_key: str,
    history_limit: int,
    memory_limit: int,
    dry_run: bool,
) -> str:
    if active_phone != contact["phone_e164"]:
        open_contact_chat(driver, contact)
        active_phone = contact["phone_e164"]
        install_dom_observer(driver, contacts, active_phone, emit_initial_unread=True)

    live = read_recent_history(driver, memory_limit)
    state = _read_state(contact["phone_e164"])
    _handle_new_messages(
        driver, contact, live, state, api_key, memory_limit, history_limit, dry_run
    )
    return active_phone


def main() -> None:
    driver = None
    try:
        contacts, history_limit, memory_limit, dry_run = load_config()
        print("Enabled contacts:")
        for contact in contacts:
            mode = "DRAFT" if not contact["auto_send"] else ("DRY RUN" if dry_run else "AUTO SEND")
            print(f"  - {contact['name']} | WhatsApp chat: {contact['whatsapp_name']} | {mode}")
        if dry_run:
            print("\n[SAFE MODE] dry_run=true hai. Koi WhatsApp message send nahi hoga.")
        else:
            print("\n[WARNING] dry_run=false. auto_send=true contacts ko real messages bheje ja sakte hain.")

        api_key = get_api_key()
        driver = start_chrome()
        driver.set_script_timeout(OBSERVER_WAIT_SECONDS + 5)
        driver.get("https://web.whatsapp.com/")
        wait_for_whatsapp_login(driver)

        pending_contacts: set[str] = set()
        active_phone: str | None = None
        for contact in contacts:
            try:
                has_pending = _baseline_contact(driver, contact, history_limit, memory_limit)
                active_phone = contact["phone_e164"]
                if has_pending:
                    pending_contacts.add(contact["phone_e164"])
            except (RuntimeError, WebDriverException) as error:
                active_phone = None
                print(f"[WARMUP ERROR] {contact['name']}: {error}")

        install_dom_observer(driver, contacts, active_phone, emit_initial_unread=True)
        contacts_by_phone = {contact["phone_e164"]: contact for contact in contacts}
        retry_schedule: dict[str, tuple[float, int]] = {}

        print(
            "\n[READY] Event-driven monitoring active. MutationObserver watches WhatsApp DOM "
            "changes; fixed 12-second polling nahi hai. Ctrl+C se stop karein."
        )

        while True:
            now = time.monotonic()
            requested_phones = set(pending_contacts)
            pending_contacts.clear()
            due_phones = {
                phone for phone in requested_phones
                if phone not in retry_schedule or retry_schedule[phone][0] <= now
            }
            pending_contacts.update(requested_phones - due_phones)
            for phone, (due, _attempt) in list(retry_schedule.items()):
                if due <= now:
                    due_phones.add(phone)

            for phone in due_phones:
                contact = contacts_by_phone.get(phone)
                if not contact:
                    continue
                try:
                    active_phone = _process_contact(
                        driver, contact, contacts, active_phone, api_key,
                        history_limit, memory_limit, dry_run
                    )
                    retry_schedule.pop(phone, None)
                except GroqAPIError as error:
                    if error.retryable:
                        previous_attempt = retry_schedule.get(phone, (0.0, 0))[1]
                        attempt = previous_attempt + 1
                        delay = min(60, 5 * (2 ** min(attempt - 1, 4)))
                        retry_schedule[phone] = (time.monotonic() + delay, attempt)
                        print(f"[RETRY] {contact['name']}: Groq temporary failure; retry in {delay}s. {error}")
                    else:
                        retry_schedule.pop(phone, None)
                        print(f"[GROQ ERROR] {contact['name']}: {error}. Message marker remains unadvanced.")
                except (RuntimeError, WebDriverException) as error:
                    retry_schedule[phone] = (time.monotonic() + 15, 1)
                    print(f"[CONTACT ERROR] {contact['name']}: {error}. Retry in 15s.")

            next_due = min((value[0] for value in retry_schedule.values()), default=None)
            timeout = OBSERVER_WAIT_SECONDS
            if next_due is not None:
                timeout = min(timeout, max(0.2, next_due - time.monotonic()))

            events = wait_for_dom_events(driver, timeout)
            if any(event.get("type") == "observer_missing" for event in events):
                print("[OBSERVER] WhatsApp page reloaded; reinstalling MutationObserver.")
                install_dom_observer(driver, contacts, active_phone, emit_initial_unread=True)
                continue

            event_phones = {
                str(event.get("phone"))
                for event in events
                if event.get("type") in {"incoming", "unread_contact"}
                and str(event.get("phone")) in contacts_by_phone
            }
            pending_contacts.update(event_phones)

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
