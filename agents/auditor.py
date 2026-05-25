import os
import re
import json
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client

SYSTEM = """You are a smart contract security auditor.
Review the contract against the specification and return ONLY valid JSON — no other text, no markdown.
JSON structure must be exactly:
{
  "critical_issues": ["description of issue", ...],
  "warnings": ["technical description of warning for a developer", ...],
  "user_warnings": ["plain English explanation of the same warning for a non-technical product owner", ...],
  "approved": true or false
}
Set approved to true only when critical_issues is empty.
Flag real security issues and spec deviations. Ignore style preferences.
user_warnings must have the same number of items as warnings, in the same order.
Each user_warning should explain what the warning means for the product owner in plain language — no code terms, no function names, no Solidity jargon. Focus on what it means for people using the app, not how to fix the code. If a warning is a minor internal detail with no real user impact, say so plainly, e.g. "Minor internal optimisation — no action needed on your part." """


def review(spec: str, code: str, compiler_result: dict, slither_result: dict) -> dict:
    compiler_summary = "PASSED" if compiler_result["success"] else f"FAILED: {compiler_result.get('errors', [])}"
    slither_summary = slither_result.get("summary", "Not run")
    slither_issues = "\n".join(slither_result.get("issues", [])) or "None"

    user_msg = f"""Audit this smart contract.

SPECIFICATION:
{spec}

CONTRACT CODE:
{code}

COMPILER: {compiler_summary}

SLITHER SUMMARY: {slither_summary}
SLITHER FINDINGS:
{slither_issues}

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
            "critical_issues": ["Auditor returned malformed JSON — treating as failed"],
            "warnings": [],
            "approved": False
        }
