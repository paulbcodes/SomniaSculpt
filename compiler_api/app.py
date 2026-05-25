import hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS
from solcx import compile_source, install_solc, get_installed_solc_versions
from web3 import Web3

app = Flask(__name__)
CORS(app)

SOLC_VERSION        = "0.8.20"
SOMNIA_RPC          = "https://api.infra.testnet.somnia.network/"
ORCHESTRATOR_V2     = "0xA9991F20dE5bBD635de95cdf49B7Cf24a62183d1"
ORCHESTRATOR_V3     = "0xD21f0262bE547Ac4d37979421e6Cb020FAD40B45"   # TODO: set after deploying PipelineOrchestratorV3
ORCHESTRATOR        = ORCHESTRATOR_V2  # default for backward compatibility

_GET_JOB_ABI = [{
    "name": "getJob",
    "type": "function",
    "inputs": [{"name": "jobId", "type": "uint256"}],
    "outputs": [
        {"name": "requester",       "type": "address"},
        {"name": "state",           "type": "uint8"},
        {"name": "idea",            "type": "string"},
        {"name": "solidity",        "type": "string"},
        {"name": "auditResult",     "type": "string"},
        {"name": "deployedAddress", "type": "address"},
        {"name": "auditApproved",   "type": "bool"},
        {"name": "fixAttempts",     "type": "uint8"},
        {"name": "createdAt",       "type": "uint256"},
    ],
    "stateMutability": "view",
}]

# cache: (jobId, v, orch_address) -> {"result": "ok"|"<error>", "bytecode": str, "abi": list}
_cache: dict = {}


def _ensure_solc():
    installed = [str(v) for v in get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        install_solc(SOLC_VERSION)


def _resolve_orchestrator(orch_param: str) -> str:
    """Resolve orchestrator address from ?orch= query param. Defaults to V2."""
    if not orch_param:
        return ORCHESTRATOR_V2
    addr = orch_param.strip().lower()
    if addr == ORCHESTRATOR_V3.lower() and ORCHESTRATOR_V3:
        return ORCHESTRATOR_V3
    # Accept any valid checksum address passed directly (e.g. from V3 contract URL)
    if len(orch_param) == 42 and orch_param.startswith("0x"):
        return orch_param
    return ORCHESTRATOR_V2


def _get_solidity(job_id: int, orch_address: str = None) -> str:
    addr = orch_address or ORCHESTRATOR_V2
    w3 = Web3(Web3.HTTPProvider(SOMNIA_RPC))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(addr),
        abi=_GET_JOB_ABI
    )
    job = contract.functions.getJob(job_id).call()
    return job[3]  # solidity field — same position in both V2 and V3


def _compile(source: str) -> dict:
    _ensure_solc()
    try:
        compiled = compile_source(source, output_values=["abi", "bin"], solc_version=SOLC_VERSION)
    except Exception as e:
        raw = str(e)
        errors = [line.strip() for line in raw.splitlines() if "Error" in line or "error" in line]
        error_msg = errors[0] if errors else raw[:400]
        return {"result": error_msg, "bytecode": None, "abi": None}

    # pick last contract with bytecode
    artifact = None
    for key, data in compiled.items():
        if data.get("bin"):
            artifact = {"contract_name": key.split(":")[-1], "abi": data["abi"], "bytecode": "0x" + data["bin"]}

    if not artifact:
        return {"result": "No deployable contract found", "bytecode": None, "abi": None}

    return {"result": "ok", "bytecode": artifact["bytecode"], "abi": artifact["abi"]}


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"ok": True})


# ── POST compile (direct source — for testing) ────────────────────────────────
@app.route("/compile", methods=["POST"])
def compile_direct():
    data = request.get_json(silent=True)
    if not data or "source" not in data:
        return jsonify({"success": False, "errors": ["Missing 'source' field"]}), 400
    result = _compile(data["source"])
    if result["result"] != "ok":
        return jsonify({"success": False, "errors": [result["result"]]})
    return jsonify({"success": True, "bytecode": result["bytecode"], "abi": result["abi"],
                    "contract_name": data.get("contract_name", "")})


# ── GET compile result — called by Somnia JSON API agent ─────────────────────
# Returns {"result": "ok"} on success or {"result": "<error>"} on failure
# ?v= param is the fixAttempts counter used as a cache-busting key
@app.route("/job/<int:job_id>/result")
def job_result(job_id):
    v    = request.args.get("v", "0")
    orch = _resolve_orchestrator(request.args.get("orch", ""))
    cache_key = (job_id, v, orch)

    if cache_key not in _cache:
        try:
            source = _get_solidity(job_id, orch)
        except Exception as e:
            return jsonify({"result": f"Failed to read contract: {str(e)[:200]}"}), 500

        if not source:
            return jsonify({"result": "No Solidity found for this job"}), 404

        _cache[cache_key] = _compile(source)

    return jsonify({"result": _cache[cache_key]["result"]})


# ── GET artifacts — called by frontend before MetaMask deploy ─────────────────
@app.route("/job/<int:job_id>/artifacts")
def job_artifacts(job_id):
    orch = _resolve_orchestrator(request.args.get("orch", ""))
    # find latest cached version for this jobId + orch
    latest = None
    for (jid, v, o), data in _cache.items():
        if jid == job_id and o == orch and data["result"] == "ok":
            latest = data

    if not latest:
        try:
            source = _get_solidity(job_id, orch)
        except Exception as e:
            return jsonify({"error": f"Failed to read contract: {str(e)[:200]}"}), 500
        if not source:
            return jsonify({"error": "No Solidity found"}), 404
        result = _compile(source)
        if result["result"] != "ok":
            return jsonify({"error": result["result"]}), 400
        latest = result

    return jsonify({"bytecode": latest["bytecode"], "abi": latest["abi"]})


if __name__ == "__main__":
    _ensure_solc()
    app.run(host="0.0.0.0", port=5050)
