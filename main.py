"""Open WhatsApp Web and print the first ten visible chat names."""

from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "chrome_profile"
WHATSAPP_URL = "https://web.whatsapp.com/"
LOGIN_TIMEOUT_SECONDS = 180
MAX_CHATS = 10

CHAT_ROW_SELECTORS = (
    '[data-testid="cell-frame-container"]',
    '[role="listitem"]',
    '[role="row"]',
)
CHAT_NAME_SELECTORS = (
    '[data-testid="cell-frame-title"]',
    "span[title]",
)


def start_chrome() -> webdriver.Chrome:
    """Start Chrome with an app-specific profile stored beside this script."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")

    return webdriver.Chrome(options=options)


def wait_for_whatsapp_login(driver: webdriver.Chrome) -> None:
    """Wait until the WhatsApp chat pane appears after the user signs in."""
    print("Opening WhatsApp Web. Scan the QR code if WhatsApp asks you to sign in.")

    try:
        WebDriverWait(driver, LOGIN_TIMEOUT_SECONDS).until(
            lambda browser: any(
                pane.is_displayed()
                for pane in browser.find_elements(By.ID, "pane-side")
            )
        )
    except TimeoutException as error:
        raise TimeoutError(
            "WhatsApp Web did not finish loading within 3 minutes. "
            "Check the connection, scan the QR code, and try again."
        ) from error


def _visible_chat_rows(driver: webdriver.Chrome) -> list:
    panes = driver.find_elements(By.ID, "pane-side")
    if not panes:
        return []

    pane = panes[0]
    for selector in CHAT_ROW_SELECTORS:
        rows = [
            row
            for row in pane.find_elements(By.CSS_SELECTOR, selector)
            if row.is_displayed()
        ]
        if rows:
            return rows
    return []


def _chat_name(row) -> str | None:
    """Read the chat title only; do not inspect message-preview text."""
    for selector in CHAT_NAME_SELECTORS:
        for element in row.find_elements(By.CSS_SELECTOR, selector):
            if not element.is_displayed():
                continue

            name = (element.get_attribute("title") or element.text or "").strip()
            if name:
                return name

    return None


def get_first_visible_chat_names(driver: webdriver.Chrome) -> list[str]:
    """Return up to ten unique chat titles in the order WhatsApp displays them."""
    try:
        WebDriverWait(driver, 15).until(lambda browser: _visible_chat_rows(browser))
    except TimeoutException:
        return []

    names = []
    seen = set()
    for row in _visible_chat_rows(driver):
        try:
            name = _chat_name(row)
        except WebDriverException:
            continue

        if name and name not in seen:
            seen.add(name)
            names.append(name)
            if len(names) == MAX_CHATS:
                break

    return names


def main() -> None:
    driver = None
    try:
        driver = start_chrome()
        driver.get(WHATSAPP_URL)
        wait_for_whatsapp_login(driver)

        names = get_first_visible_chat_names(driver)
        print("\nFirst visible WhatsApp chats:")
        if not names:
            print("No visible chat names were found. WhatsApp may have changed its page layout.")
        else:
            for number, name in enumerate(names, start=1):
                print(f"{number:02d}. {name}")

        input("\nPress Enter to close Chrome (your local session will be kept)...")
    except TimeoutError as error:
        print(f"\n[ERROR] {error}")
    except WebDriverException as error:
        print(f"\n[ERROR] Could not start or control Chrome: {error}")
        print("Close any Chrome window using this profile, then run the script again.")
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()

