import re
import json
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM = """You are an expert Web3 frontend developer.
You generate clean, self-contained single-file HTML frontends that interact with deployed Ethereum smart contracts.

Rules:
- Output ONE complete HTML file. No external files. No placeholders.
- Use ethers.js v6 via CDN: https://cdnjs.cloudflare.com/ajax/libs/ethers/6.7.0/ethers.umd.min.js
- MetaMask connect button at the top — show connected wallet address when connected
- For every non-constructor ABI function:
  - view/pure functions: a button that calls it and displays the result clearly
  - write functions: form inputs for each parameter + a submit button that sends the transaction
- Embed the contract address, ABI, and expected chain ID as JS constants at the top of the script
- On wallet connect AND on every write transaction, check the connected chain ID matches the expected chain ID — if not, show a clear error message telling the user to switch networks. Do not proceed with the transaction if on the wrong network.
- CRITICAL: The ABI is the absolute source of truth. Use the exact function names, parameter names, and parameter types from the ABI — never from the spec description. If the spec says 'createToken' but the ABI says 'deployToken', use 'deployToken'. If the spec says separate buyTax/sellTax but the ABI has a single taxPercent, use taxPercent. The spec describes intent; the ABI describes reality.
- Never call any function not present in the ABI. Do not assume name(), symbol(), decimals() or any standard interface functions exist unless they are in the ABI.
- Handle errors gracefully — show user-friendly messages, not raw exceptions
- Styling: when DESIGN CONTEXT is provided, use those exact hex colours for backgrounds, buttons, accents, and borders; match the stated style; use the page title from Product Name; make the Primary Action the most prominent element on the page; use the Function Labels as button/section text instead of raw function names. When no DESIGN CONTEXT is provided, use basic clean styling.
- No external CSS frameworks
- No console.log spam. No TypeScript. No build steps required.
- Be concise — avoid verbose comments and redundant code. Target under 400 lines.
- CRITICAL: You MUST end the file with </body></html>. Never truncate output mid-function or mid-string.
Return ONLY the HTML. No explanation. No markdown fences."""


def generate(spec: str, contract_name: str, address: str, abi: list,
             chain_id: int = 50312, frontend_spec: dict = None,
             previous_code: str = None, issues: list = None) -> str:
    abi_json = json.dumps(abi, indent=2)

    design_block = ""
    if frontend_spec:
        hints = json.dumps(frontend_spec.get("function_hints", {}), indent=2)
        design_block = f"""
DESIGN CONTEXT:
Product Name: {frontend_spec.get('product_name', contract_name)}
Primary Colour: {frontend_spec.get('primary_color', '#0000ff')}
Secondary Colour: {frontend_spec.get('secondary_color', '#003399')}
Style: {frontend_spec.get('style', 'clean')}
Primary Action: {frontend_spec.get('primary_action', '')}
User Type: {frontend_spec.get('user_type', '')}
Function Labels (use these as button/section text instead of raw function names):
{hints}
"""

    if previous_code and issues:
        user_msg = f"""Fix this frontend based on the issues listed.

CONTRACT SPEC:
{spec}

CONTRACT NAME: {contract_name}
CONTRACT ADDRESS: {address}
CHAIN ID: {chain_id}
CONTRACT ABI:
{abi_json}
{design_block}
CURRENT CODE:
{previous_code}

ISSUES TO FIX:
{chr(10).join(f'- {i}' for i in issues)}

Return the complete fixed HTML file. No explanation."""
    else:
        user_msg = f"""Generate a frontend for this deployed smart contract.

CONTRACT SPEC:
{spec}

CONTRACT NAME: {contract_name}
CONTRACT ADDRESS: {address}
CHAIN ID: {chain_id}
CONTRACT ABI:
{abi_json}
{design_block}
Return a single complete HTML file. No explanation."""

    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_msg}]
    )

    code = response.content[0].text.strip()
    code = re.sub(r"^```html\n?", "", code)
    code = re.sub(r"^```\n?", "", code)
    code = re.sub(r"\n?```$", "", code)
    return code.strip()


def is_complete(html: str) -> bool:
    return html.strip().endswith("</html>")
