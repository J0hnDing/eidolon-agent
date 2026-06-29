import json
import sys

DEFAULT_MESSAGE = "Hello from the sample echo skill."


def build_response(payload):
    if not isinstance(payload, dict):
        return {
            "title": "Sample Echo Skill",
            "summary": "I can echo JSON objects. Please send an object with a message field.",
            "echoed_message": "",
            "received_input": payload,
            "suggested_next_input": {"message": "Hello, assistant"},
            "warnings": ["Input was not a JSON object."],
        }

    message = payload.get("message") or payload.get("text") or DEFAULT_MESSAGE
    label = payload.get("label") or "Echo response"
    return {
        "title": "Sample Echo Skill",
        "summary": f"{label}: {message}",
        "echoed_message": message,
        "received_input": payload,
        "suggested_next_input": {"message": "Try editing this message and running again."},
        "warnings": [],
    }


def main():
    raw_input = sys.stdin.read()
    try:
        payload = json.loads(raw_input or "{}")
        response = build_response(payload)
    except json.JSONDecodeError as exc:
        response = {
            "title": "Sample Echo Skill",
            "summary": "The input was not valid JSON.",
            "echoed_message": "",
            "received_input": raw_input,
            "suggested_next_input": {"message": "Hello, assistant"},
            "warnings": [f"Invalid JSON input: {exc.msg}"],
        }
    print(json.dumps(response))

if __name__ == '__main__':
    main()
