"""
AgentMail tool — Kaveri's dedicated agent email inbox.

Provides send, list, get, and delete operations via the AgentMail REST API.
Inbox: kaveri@agentmail.to
"""

import json
import os
from typing import Optional

import httpx

from tools.registry import registry

BASE_URL = "https://api.agentmail.to/v0"
KAVERI_SIGNATURE = """

--
Kaveri 🌊 **{quote}**
AI Chief of Staff to Subri
kaveri@agentmail.to"""

def _generate_dynamic_quote() -> str:
    """Generate a fresh, witty signature line using an LLM — context-aware of Kaveri/Subri dynamic."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": (
                    "Generate ONE short, punchy email signature tagline for Kaveri — "
                    "AI Chief of Staff to Subri (VP of Tech, Mattel, South Indian roots, Coimbatore). "
                    "Kaveri is the river. She's not an assistant — she's a strategic counterweight. "
                    "Make it smart, witty, occasionally funny, personal to this duo. "
                    "No quotes. No explanation. Just the line. Max 10 words. "
                    "Examples of the vibe: 'The river doesn't ask. It flows.', "
                    "'More Kaveri, less chaos.', 'Behind every good VP, a smarter river.'"
                )
            }]
        )
        return response.content[0].text.strip().strip('"').strip("'")
    except Exception:
        return "Steady force. Strategic counterweight. Long-term focused."


def _get_client():
    api_key = os.getenv("AGENTMAIL_API_KEY", "")
    if not api_key:
        raise ValueError("AGENTMAIL_API_KEY not set")
    return httpx.Client(
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )


def _get_inbox():
    return os.getenv("AGENTMAIL_INBOX_ID", "kaveri@agentmail.to")


def _rotating_quote() -> str:
    return _generate_dynamic_quote()


def agentmail(
    action: str,
    to: Optional[str] = None,
    subject: Optional[str] = None,
    text: Optional[str] = None,
    html: Optional[str] = None,
    message_id: Optional[str] = None,
    limit: int = 10,
    after: Optional[str] = None,
    add_signature: bool = True,
    task_id: str = None,
) -> str:
    """
    Interact with Kaveri's AgentMail inbox (kaveri@agentmail.to).

    Actions:
    - list: List messages in inbox
    - get: Get a specific message by message_id
    - send: Send an email (to, subject, text required)
    - delete: Delete a message by message_id
    """
    try:
        inbox = _get_inbox()
        client = _get_client()

        if action == "list":
            params = {"limit": str(limit)}
            if after:
                params["after"] = after
            resp = client.get(f"{BASE_URL}/inboxes/{inbox}/messages", params=params)
            resp.raise_for_status()
            return json.dumps({"ok": True, "action": "list", "data": resp.json()})

        elif action == "get":
            if not message_id:
                return json.dumps({"ok": False, "error": "message_id required for get"})
            resp = client.get(f"{BASE_URL}/inboxes/{inbox}/messages/{message_id}")
            resp.raise_for_status()
            return json.dumps({"ok": True, "action": "get", "data": resp.json()})

        elif action == "send":
            if not to or not subject or not text:
                return json.dumps({"ok": False, "error": "to, subject, and text are required for send"})
            # Append signature
            if add_signature:
                text = text + KAVERI_SIGNATURE.format(quote=_rotating_quote())
            body = {"to": to, "subject": subject, "text": text}
            if html:
                body["html"] = html
            resp = client.post(f"{BASE_URL}/inboxes/{inbox}/messages/send", json=body)
            resp.raise_for_status()
            return json.dumps({"ok": True, "action": "send", "data": resp.json()})

        elif action == "delete":
            if not message_id:
                return json.dumps({"ok": False, "error": "message_id required for delete"})
            resp = client.delete(f"{BASE_URL}/inboxes/{inbox}/messages/{message_id}")
            if resp.status_code == 204:
                return json.dumps({"ok": True, "action": "delete", "message_id": message_id})
            resp.raise_for_status()
            return json.dumps({"ok": True, "action": "delete", "data": resp.json()})

        else:
            return json.dumps({"ok": False, "error": f"Unknown action: {action}. Valid: list, get, send, delete"})

    except httpx.HTTPStatusError as e:
        return json.dumps({"ok": False, "error": f"AgentMail API error {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def check_requirements() -> bool:
    return bool(os.getenv("AGENTMAIL_API_KEY"))


registry.register(
    name="agentmail",
    toolset="agentmail",
    schema={
        "name": "agentmail",
        "description": (
            "Kaveri's dedicated agent email inbox (kaveri@agentmail.to) via AgentMail. "
            "Use for sending emails to Subri or others, reading Kaveri's inbox, and managing email threads. "
            "Always uses Kaveri's rotating signature when sending. "
            "Actions: list (inbox), get (specific message), send (compose+send), delete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "send", "delete"],
                    "description": "Action to perform",
                },
                "to": {"type": "string", "description": "Recipient email address (send only)"},
                "subject": {"type": "string", "description": "Email subject (send only)"},
                "text": {"type": "string", "description": "Plain text email body (send only)"},
                "html": {"type": "string", "description": "Optional HTML body (send only)"},
                "message_id": {"type": "string", "description": "Message ID for get/delete"},
                "limit": {"type": "integer", "description": "Number of messages to list (default 10)", "default": 10},
                "after": {"type": "string", "description": "Pagination cursor for list"},
                "add_signature": {"type": "boolean", "description": "Append Kaveri signature (default true)", "default": True},
            },
            "required": ["action"],
        },
    },
    handler=lambda args, **kw: agentmail(
        action=args.get("action"),
        to=args.get("to"),
        subject=args.get("subject"),
        text=args.get("text"),
        html=args.get("html"),
        message_id=args.get("message_id"),
        limit=args.get("limit", 10),
        after=args.get("after"),
        add_signature=args.get("add_signature", True),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_requirements,
    requires_env=["AGENTMAIL_API_KEY"],
)
