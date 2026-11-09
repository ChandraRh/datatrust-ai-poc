import os

import requests


URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate(body: dict, timeout: int = 60) -> dict:
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    response = requests.post(
        URL.format(model=model),
        timeout=timeout,
        headers={
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
            "Content-Type": "application/json",
        },
        json=body,
    )
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
