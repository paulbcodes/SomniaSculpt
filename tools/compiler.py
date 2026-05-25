from solcx import compile_source, install_solc, get_installed_solc_versions

SOLC_VERSION = "0.8.20"


def _ensure_compiler():
    installed = [str(v) for v in get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        print(f"  [compiler] Installing solc {SOLC_VERSION}...")
        install_solc(SOLC_VERSION)


def compile_contract(code: str) -> dict:
    _ensure_compiler()
    try:
        compile_source(
            code,
            output_values=["abi", "bin"],
            solc_version=SOLC_VERSION
        )
        return {"success": True, "errors": [], "warnings": []}
    except Exception as e:
        raw = str(e)
        errors = [line.strip() for line in raw.splitlines() if "Error" in line or "error" in line]
        if not errors:
            errors = [raw[:600]]
        return {"success": False, "errors": errors, "warnings": []}


def get_artifacts(code: str, contract_name: str = None) -> dict:
    """Returns ABI and bytecode for deployment. Raises on compile failure.
    If contract_name is given, finds that specific contract in the compiled output.
    Otherwise falls back to the last contract with bytecode (typically the top-level one).
    """
    _ensure_compiler()
    compiled = compile_source(
        code,
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION
    )
    # compiled keys are like "<stdin>:ContractName"
    if contract_name:
        for key, data in compiled.items():
            if key.split(":")[-1] == contract_name and data.get("bin"):
                return {
                    "contract_name": contract_name,
                    "abi": data["abi"],
                    "bytecode": "0x" + data["bin"]
                }
        raise ValueError(f"Contract '{contract_name}' not found in compiled output")

    # No name given — take the last contract with bytecode (avoids picking helper contracts first)
    result = None
    for key, data in compiled.items():
        if data.get("bin"):
            result = {
                "contract_name": key.split(":")[-1],
                "abi": data["abi"],
                "bytecode": "0x" + data["bin"]
            }
    if result:
        return result
    raise ValueError("No deployable contract found in compiled output")
