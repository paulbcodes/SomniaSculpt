import re
import json
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM = """You are a Web3 frontend code reviewer.
You review HTML/JS frontends that interact with deployed smart contracts.
Respond with ONLY valid JSON — no other text, no markdown.

JSON structure:
{
  "critical_issues": ["description", ...],
  "warnings": ["technical description of warning for a developer", ...],
  "user_warnings": ["plain English explanation of the same warning for a non-technical app owner", ...],
  "approved": true or false
}

Set approved to true only when critical_issues is empty.
user_warnings must have the same number of items as warnings, in the same order.
Each user_warning should describe what the issue means for people using the app — no HTML/JS terms, no code references. If a warning is a minor internal code quality issue with no user-facing impact, say so plainly, e.g. "Minor internal code note — no impact on your users."

IMPORTANT: The ABI is the source of truth — not the spec. The spec is background context only.
The contract is already deployed. Do NOT flag differences between the spec and the ABI — that ship has sailed.
Your only job is to verify the frontend correctly uses the provided ABI.

Critical issues (must fix):
- Frontend calls a function name that does not exist in the ABI
- Frontend passes wrong number or wrong types of arguments to an ABI function
- Contract address is missing or clearly wrong
- A non-view ABI function has no UI at all
- MetaMask connection is broken or missing
- User input rendered directly as innerHTML (XSS)
- write functions called without sending a transaction

Warnings (optional, not blocking):
- Minor UX improvements
- Missing result formatting
- Edge cases not handled"""


def review(spec: str, contract_name: str, address: str, abi: list, code: str) -> dict:
    abi_json = json.dumps(abi, indent=2)

    user_msg = f"""Review this frontend.

CONTRACT SPEC:
{spec}

CONTRACT NAME: {contract_name}
CONTRACT ADDRESS: {address}
ABI:
{abi_json}

FRONTEND CODE:
{code}

Return ONLY the JSON audit result."""

    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_msg}]
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```json\n?", "", text)
    text = re.sub(r"\n?```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "critical_issues": ["Frontend auditor returned malformed JSON — treating as failed"],
            "warnings": [],
            "approved": False
        }
