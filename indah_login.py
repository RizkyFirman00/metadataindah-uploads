import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from indah_automation.shared import DEFAULT_STATE_PATH, WEB_URL


async def main() -> None:
    parser = argparse.ArgumentParser(description="Login manual ke INDAH dan simpan storage_state untuk phase automation.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Path storage_state output.")
    parser.add_argument("--timeout-minutes", type=int, default=10, help="Batas tunggu login manual.")
    args = parser.parse_args()

    state_path = Path(args.state).expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        await page.goto(WEB_URL, wait_until="domcontentloaded")

        print("Silakan login manual di browser yang terbuka, termasuk captcha.")
        print("Script akan lanjut otomatis setelah token login INDAH terdeteksi.")

        deadline = asyncio.get_event_loop().time() + args.timeout_minutes * 60
        while asyncio.get_event_loop().time() < deadline:
            token_present = await page.evaluate(
                """() => {
                    try {
                        const vuex = JSON.parse(localStorage.getItem('vuex') || '{}');
                        return Boolean(vuex.auth && vuex.auth.token);
                    } catch (err) {
                        return false;
                    }
                }"""
            )
            if token_present:
                await context.storage_state(path=str(state_path))
                print(f"Login tersimpan: {state_path}")
                await browser.close()
                return
            await asyncio.sleep(2)

        await browser.close()
        raise SystemExit("Timeout menunggu login. Jalankan ulang `python indah_login.py`.")


if __name__ == "__main__":
    asyncio.run(main())

