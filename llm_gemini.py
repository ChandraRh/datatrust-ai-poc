import os

import requests


URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


RETRY_STATUS = (429, 500, 502, 503, 504)          # rate limit / model busy: worth another try


def generate(body: dict, timeout: int = 60, attempts: int = 3) -> dict:
    import time
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    for attempt in range(1, attempts + 1):
        response = requests.post(
            URL.format(model=model),
            timeout=timeout,
            headers={
                "x-goog-api-key": os.environ["GEMINI_API_KEY"],
                "Content-Type": "application/json",
            },
            json=body,
        )
        daily_limit = response.status_code == 429 and "per day" in response.text.lower()   # waiting seconds will not help
        if response.status_code in RETRY_STATUS and attempt < attempts and not daily_limit:
            wait = response.headers.get("Retry-After")
            time.sleep(min(float(wait), 20) if wait and wait.replace(".", "", 1).isdigit() else 2 * attempt ** 2)
            continue
        response.raise_for_status()
        return response.json()


def complete_json(system: str, text: str) -> str:
    """Make a one-shot Gemini call that returns JSON text."""
    data = generate({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    })
    return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
