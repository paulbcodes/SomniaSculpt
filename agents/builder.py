import os
import re
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client

SYSTEM = """You are an expert Solidity smart contract developer.
You write clean, secure, fully self-contained Solidity contracts.
Rules:
- Always use SPDX-License-Identifier: MIT
- Always use pragma solidity ^0.8.20
- Never import external libraries — implement everything inline
- No placeholders, no TODOs, complete working code only
- Use custom errors instead of require strings where appropriate
- Emit events for all state changes
Return ONLY the Solidity code. No explanation. No markdown. Start with // SPDX-License-Identifier."""


def generate(spec: str, previous_code: str = None, issues: list = None) -> str:
    if previous_code and issues:
        user_msg = f"""Fix this smart contract based on the issues listed below.

ORIGINAL SPEC:
{spec}

CURRENT CODE:
{previous_code}

ISSUES TO FIX:
{chr(10).join(f'- {i}' for i in issues)}

Return the complete fixed Solidity contract. No explanation. No markdown."""
    else:
        user_msg = f"""Write a Solidity smart contract for this specification:

{spec}

Return ONLY the Solidity code. No explanation. No markdown."""

    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_msg}]
    )

    code = response.content[0].text.strip()
    code = re.sub(r"^```solidity\n?", "", code)
    code = re.sub(r"^```\n?", "", code)
    code = re.sub(r"\n?```$", "", code)
    return code.strip()
