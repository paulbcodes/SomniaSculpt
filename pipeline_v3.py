"""
V3 fallback pipeline watcher.

Watches PipelineOrchestratorV3 for jobs that need off-chain assistance:

  CONTRACT FALLBACK:
    State goes to AwaitingFallback (3) → on-chain agents exhausted MAX_FIX attempts.
    We run V1 Claude build+audit loop, then call submitOffChainBuild().
    On-chain agents then compile + audit (deferred prompt) + user deploys as normal.

  FRONTEND FALLBACK:
    After contract deploys, we call initiateFrontendBuild() → on-chain LLM attempts HTML.
    If FrontendBuilt event fires (on-chain HTML looks valid) → store it, submit for audit.
    If FRONTEND_TIMEOUT seconds pass with no valid result → run V1 frontend builder, submit for audit.
    Either way we call submitFrontendAudit() with script content + VPS URL.
    On-chain LLM audits the script. Frontend URL stored on-chain.
"""

import os
import re
import time
import json
import logging
import threading
import requests
from pathlib import Path
from queue import Queue

from web3 import Web3
from dotenv import load_dotenv

from agents.builder import generate as build_contract
from agents.auditor import review as audit_contract
from agents.frontend_builder import generate as build_frontend, is_complete as fe_is_complete
from agents.frontend_auditor import review as audit_frontend
from tools.compiler import compile_contract, get_artifacts
from tools.reactive_registrar import register_contract, subscribe_to_events

load_dotenv()

log = logging.getLogger(__name__)

RPC_URL        = os.getenv("DEPLOY_RPC_URL", "https://api.infra.testnet.somnia.network/")
PRIVATE_KEY    = os.getenv("DEPLOY_PRIVATE_KEY", "")
COMPILER_API   = os.getenv("COMPILER_API_URL", "https://dexsnip.site/solc")
PUBLIC_URL     = os.getenv("V3_PUBLIC_URL", "http://localhost:5001")

CONTRACT_TIMEOUT  = int(os.getenv("V3_CONTRACT_TIMEOUT",  "300"))   # 5 min
FRONTEND_TIMEOUT  = int(os.getenv("V3_FRONTEND_TIMEOUT",  "30"))    # 30s — on-chain frontend never succeeds at fixed cost
POLL_INTERVAL     = int(os.getenv("V3_POLL_INTERVAL",     "10"))
MAX_V1_ITER       = 5

# Job states (mirror JobState enum in V3 contract)
STATE_BUILDING          = 0
STATE_COMPILING         = 1
STATE_AUDITING          = 2
STATE_AWAITING_FALLBACK = 3
STATE_APPROVED          = 4
STATE_COMPLETE          = 5
STATE_FAILED            = 6

STATE_LABELS = {
    0: "Building",
    1: "Compiling",
    2: "Auditing",
    3: "Awaiting Fallback",
    4: "Approved",
    5: "Complete",
    6: "Failed",
}

V3_ABI = [
    {
        "name": "getJob", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "jobId", "type": "uint256"}],
        "outputs": [
            {"name": "requester",           "type": "address"},
            {"name": "state",               "type": "uint8"},
            {"name": "idea",                "type": "string"},
            {"name": "solidity",            "type": "string"},
            {"name": "auditResult",         "type": "string"},
            {"name": "deployedAddress",     "type": "address"},
            {"name": "auditApproved",       "type": "bool"},
            {"name": "fixAttempts",         "type": "uint8"},
            {"name": "createdAt",           "type": "uint256"},
            {"name": "offchainBuilt",       "type": "bool"},
            {"name": "frontendUrl",         "type": "string"},
            {"name": "frontendAuditPassed", "type": "bool"},
        ],
    },
    {
        "name": "getFrontendHtml", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "jobId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "submitOffChainBuild", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "jobId",    "type": "uint256"},
            {"name": "solidity", "type": "string"},
        ],
        "outputs": [],
    },
    {
        "name": "initiateFrontendBuild", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "jobId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "submitFrontendAudit", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "jobId",         "type": "uint256"},
            {"name": "scriptContent", "type": "string"},
            {"name": "frontendUrl",   "type": "string"},
        ],
        "outputs": [],
    },
    {
        "name": "FrontendBuilt",   "type": "event",
        "inputs": [{"name": "jobId", "type": "uint256", "indexed": True}],
    },
    {
        "name": "FrontendFallback", "type": "event",
        "inputs": [{"name": "jobId", "type": "uint256", "indexed": True}],
    },
    {
        "name": "FrontendReady", "type": "event",
        "inputs": [
            {"name": "jobId",       "type": "uint256", "indexed": True},
            {"name": "frontendUrl", "type": "string",  "indexed": False},
        ],
    },
]

PRAGMA_NOISE = [
    "verbatimInvalidDeduplication",
    "FullInlinerNonExpressionSplitArgumentEvaluationOrder",
    "known compiler bug",
    "^0.8.20",
    "pragma version",
    "Pragma version",
]


def _emit(q: Queue, type_: str, **kwargs):
    if q:
        q.put({"type": type_, **kwargs})


def _send_tx(w3: Web3, account, fn_call):
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    priority_fee = w3.to_wei(1, "gwei")

    try:
        gas_estimate = fn_call.estimate_gas({"from": account.address})
        gas = int(gas_estimate * 1.3)
    except Exception:
        gas = 10_000_000

    tx = fn_call.build_transaction({
        "from":                 account.address,
        "nonce":                w3.eth.get_transaction_count(account.address),
        "gas":                  gas,
        "maxFeePerGas":         base_fee * 2 + priority_fee,
        "maxPriorityFeePerGas": priority_fee,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise Exception(f"Transaction reverted: {tx_hash.hex()}")
    return receipt


def _extract_script(html: str) -> str:
    """Pull <script> content for on-chain audit."""
    parts = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    script = "\n".join(parts).strip()
    return script[:6_000] if len(script) > 6_000 else script


def _get_artifacts_from_api(job_id: int, orch_address: str = "") -> dict | None:
    try:
        r = requests.get(f"{COMPILER_API}/job/{job_id}/artifacts",
                         params={"orch": orch_address}, timeout=15)
        if r.ok:
            data = r.json()
            if not data.get("error") and data.get("abi"):
                return data
    except Exception:
        pass
    return None


def _store_frontend(job_id: int, html: str, output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"frontend_{job_id}.html"
    path.write_text(html)
    return f"{PUBLIC_URL}/v3/frontend/{job_id}"


def _run_contract_fallback(job_id: int, idea: str, w3: Web3, account,
                           contract, queue: Queue) -> bool:
    """Run V1 Claude build+audit loop, submit result to V3 contract. Returns True on success."""
    _emit(queue, "FALLBACK", msg="[V3] On-chain agents exhausted — fallback layer taking over contract build...")

    previous_code = None
    issues = []

    for i in range(1, MAX_V1_ITER + 1):
        _emit(queue, "LOG", msg=f"[FALLBACK] Building contract ({i}/{MAX_V1_ITER})...")
        try:
            code = build_contract(idea, previous_code, issues if issues else None)
        except Exception as e:
            _emit(queue, "LOG", msg=f"[FALLBACK] Builder error: {e}", error=True)
            continue

        compile_result = compile_contract(code)
        if not compile_result["success"]:
            errs = compile_result["errors"]
            _emit(queue, "LOG", msg=f"[FALLBACK] Compile failed — {errs[0][:80] if errs else 'unknown'}", error=True)
            issues = [f"Compilation error: {e}" for e in errs]
            previous_code = code
            continue

        _emit(queue, "LOG", msg="[FALLBACK] Compiled — running audit...")
        try:
            audit = audit_contract(idea, code, compile_result, {"summary": "N/A", "issues": []})
        except Exception as e:
            _emit(queue, "LOG", msg=f"[FALLBACK] Auditor error: {e}", error=True)
            issues = [str(e)]
            previous_code = code
            continue

        if not audit.get("approved"):
            crits = audit.get("critical_issues", [])
            _emit(queue, "LOG", msg=f"[FALLBACK] Audit not approved — {len(crits)} issue(s)", error=True)
            issues = crits + audit.get("warnings", [])
            previous_code = code
            continue

        _emit(queue, "LOG", msg="[FALLBACK] Contract approved — submitting to chain...", ok=True)

        # A late Qwen response can arrive after AwaitingFallback and reset state to
        # Compiling/Auditing. Wait up to 3 min for the state to return to AwaitingFallback
        # before calling submitOffChainBuild (which requires that state).
        for _ in range(18):
            try:
                state_now = contract.functions.getJob(job_id).call()[1]
            except Exception:
                state_now = STATE_AWAITING_FALLBACK
            if state_now == STATE_AWAITING_FALLBACK:
                break
            if state_now in (STATE_APPROVED, STATE_COMPLETE, STATE_FAILED):
                _emit(queue, "LOG", msg=f"[V3] Job moved to state {state_now} while fallback layer was building — skipping submit", error=True)
                return False
            _emit(queue, "LOG", msg=f"[V3] Late Qwen response in flight (state={state_now}), waiting...")
            time.sleep(10)

        for attempt in range(3):
            # wait for AwaitingFallback before each attempt — late Qwen responses can temporarily change state
            for _ in range(18):
                try:
                    state_now = contract.functions.getJob(job_id).call()[1]
                except Exception:
                    state_now = STATE_AWAITING_FALLBACK
                if state_now == STATE_AWAITING_FALLBACK:
                    # stability delay — let any in-flight Qwen responses arrive before sending
                    time.sleep(20)
                    state_now = contract.functions.getJob(job_id).call()[1]
                    if state_now != STATE_AWAITING_FALLBACK:
                        _emit(queue, "LOG", msg=f"[V3] State changed during stability delay (state={state_now}), waiting...")
                        continue
                    break
                if state_now in (STATE_APPROVED, STATE_COMPLETE, STATE_FAILED):
                    _emit(queue, "LOG", msg=f"[V3] Job moved to state {state_now} while waiting to submit — skipping", error=True)
                    return False
                _emit(queue, "LOG", msg=f"[V3] Waiting for AwaitingFallback before attempt {attempt+1} (state={state_now})...")
                time.sleep(10)
            try:
                _send_tx(w3, account, contract.functions.submitOffChainBuild(job_id, code))
                _emit(queue, "LOG", msg="[V3] submitOffChainBuild sent — on-chain agents resuming compile+audit", ok=True)
                return True
            except Exception as e:
                _emit(queue, "LOG", msg=f"[V3] submitOffChainBuild failed (attempt {attempt+1}/3): {e}", error=True)
                if attempt < 2:
                    time.sleep(10)
        return False

    _emit(queue, "LOG", msg="[FALLBACK] Could not build contract after max iterations", error=True)
    return False


def _run_frontend_fallback(job_id: int, idea: str, abi: list, address: str,
                           chain_id: int, w3: Web3, account, contract,
                           queue: Queue, output_dir: Path) -> bool:
    """Run V1 Claude frontend build+audit loop, submit result for on-chain audit."""
    _emit(queue, "LOG", msg="[V3] Frontend fallback — fallback layer building frontend...")

    prev_fe = None
    fe_issues = []

    for i in range(1, MAX_V1_ITER + 1):
        _emit(queue, "LOG", msg=f"[FALLBACK] Building frontend ({i}/{MAX_V1_ITER})...")
        try:
            fe_code = build_frontend(idea, "Contract", address, abi, chain_id,
                                     None, prev_fe, fe_issues if fe_issues else None)
        except Exception as e:
            _emit(queue, "LOG", msg=f"[FALLBACK] Frontend builder error: {e}", error=True)
            continue

        if not fe_is_complete(fe_code):
            fe_issues = ["Output truncated. Produce shorter complete HTML ending with </body></html>."]
            prev_fe = None
            continue

        try:
            fe_audit = audit_frontend(idea, "Contract", address, abi, fe_code)
        except Exception as e:
            _emit(queue, "LOG", msg=f"[FALLBACK] Frontend auditor error: {e}", error=True)
            fe_issues = [str(e)]
            prev_fe = fe_code
            continue

        if not fe_audit.get("approved"):
            crits = fe_audit.get("critical_issues", [])
            _emit(queue, "LOG", msg=f"[FALLBACK] Frontend not approved — {len(crits)} issue(s)", error=True)
            fe_issues = crits + fe_audit.get("warnings", [])
            prev_fe = fe_code
            continue

        _emit(queue, "LOG", msg="[FALLBACK] Frontend approved — storing and submitting for on-chain audit...", ok=True)
        return _submit_frontend(job_id, fe_code, idea, w3, account, contract, queue, output_dir)

    # Max iterations — submit best attempt anyway
    if prev_fe:
        _emit(queue, "LOG", msg="[FALLBACK] Max frontend iterations — submitting best attempt...")
        return _submit_frontend(job_id, prev_fe, idea, w3, account, contract, queue, output_dir)

    _emit(queue, "LOG", msg="[FALLBACK] Frontend build failed completely", error=True)
    return False


def _submit_frontend(job_id: int, html: str, idea: str, w3: Web3, account,
                     contract, queue: Queue, output_dir: Path) -> bool:
    script = _extract_script(html)
    frontend_url = _store_frontend(job_id, html, output_dir)
    _emit(queue, "LOG", msg=f"[V3] Frontend stored — submitting for on-chain audit at {frontend_url}")

    try:
        _send_tx(w3, account,
                 contract.functions.submitFrontendAudit(job_id, script, frontend_url))
        _emit(queue, "LOG", msg="[V3] submitFrontendAudit sent — on-chain LLM auditing frontend", ok=True)
        _emit(queue, "FRONTEND_URL", url=frontend_url)
        return True
    except Exception as e:
        _emit(queue, "LOG", msg=f"[V3] submitFrontendAudit failed: {e}", error=True)
        # Still emit the URL so user can access the HTML even without on-chain audit
        _emit(queue, "FRONTEND_URL", url=frontend_url)
        return False


_REACTIVE_HANDLER = "0x3BdF983FAd09D2a2b72b7a8eA10A0ef45d9172C2"
_RH_ABI = [{"type": "event", "name": "InsightStored", "inputs": [
    {"name": "requestId", "type": "uint256", "indexed": True},
    {"name": "emitter",   "type": "address", "indexed": True},
    {"name": "topic0",    "type": "bytes32", "indexed": True},
    {"name": "insight",   "type": "string",  "indexed": False},
]}]


def _register_reactive(deployed_addr: str, idea: str, audit_approved: bool, queue: Queue):
    already_registered = False
    try:
        register_contract(deployed_addr, idea, audit_approved, 0, 0)
        _emit(queue, "LOG", msg="[Reactive] Contract registered on ReactiveHandler")
    except Exception as e:
        if "Already registered" in str(e):
            already_registered = True
            _emit(queue, "LOG", msg="[Reactive] Contract already registered — resuming monitor")
        else:
            _emit(queue, "LOG", msg=f"[Reactive] Registration failed: {e}", error=True)
            return

    if not already_registered:
        try:
            subscribe_to_events(deployed_addr)
            _emit(queue, "LOG", msg="[Reactive] Subscribed to Reactivity precompile — on-chain AI monitoring active", ok=True)
        except Exception as e:
            _emit(queue, "LOG", msg=f"[Reactive] Subscribe failed: {e}", error=True)

    _start_insight_monitor(deployed_addr, queue)


def _start_insight_monitor(deployed_addr: str, queue: Queue):
    from tools.somnia_monitor import start as monitor_start
    insight_q: Queue = Queue()
    stop = threading.Event()
    monitor_start(_REACTIVE_HANDLER, _RH_ABI, insight_q, stop)
    emitter_lower = deployed_addr.lower()

    def _forward():
        while True:
            evt = insight_q.get()
            if evt.get("type") == "CHAIN_EVENT" and evt.get("event_name") == "InsightStored":
                params = evt.get("params", {})
                if params.get("emitter", "").lower() == emitter_lower:
                    _emit(queue, "INSIGHT", text=params.get("insight", ""), timestamp=int(time.time()))

    threading.Thread(target=_forward, daemon=True).start()


def watch_job(job_id: int, orchestrator_address: str, queue: Queue, output_dir: str = "output/v3"):
    """
    Main watcher — runs in a background thread.
    Polls V3 contract state and handles fallbacks when triggered.
    """
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        _emit(queue, "ERROR", msg="Cannot connect to Somnia RPC")
        return

    if not PRIVATE_KEY:
        _emit(queue, "ERROR", msg="DEPLOY_PRIVATE_KEY not set — backend cannot submit fallback txns")
        return

    account  = w3.eth.account.from_key(PRIVATE_KEY)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(orchestrator_address),
        abi=V3_ABI
    )
    out_dir = Path(output_dir)

    last_state                = -1
    contract_fallback_started = False
    frontend_initiated        = False
    frontend_start_time       = 0
    frontend_done             = False
    reactivity_started        = False

    _emit(queue, "LOG", msg=f"[V3] Watching job #{job_id}...")

    while True:
        try:
            job = contract.functions.getJob(job_id).call()
        except Exception as e:
            _emit(queue, "LOG", msg=f"[V3] getJob error: {e}", error=True)
            time.sleep(POLL_INTERVAL)
            continue

        state           = job[1]
        idea            = job[2]
        deployed_addr   = job[5]
        audit_approved  = job[6]
        offchain_built  = job[9]
        frontend_url    = job[10]
        fe_audit_passed = job[11]

        if state != last_state:
            label = STATE_LABELS.get(state, str(state))
            _emit(queue, "STATE_CHANGE", state=state, label=label,
                  offchainBuilt=offchain_built)
            last_state = state

        # ── Contract fallback ─────────────────────────────────────────────────
        if state == STATE_AWAITING_FALLBACK and not contract_fallback_started:
            contract_fallback_started = True
            threading.Thread(
                target=_run_contract_fallback,
                args=(job_id, idea, w3, account, contract, queue),
                daemon=True,
            ).start()

        # ── Frontend pipeline (starts after deploy) ───────────────────────────
        if state == STATE_COMPLETE and not frontend_initiated and not frontend_done:
            frontend_initiated  = True
            frontend_start_time = time.time()
            artifacts = _get_artifacts_from_api(job_id, orchestrator_address)

            try:
                _send_tx(w3, account, contract.functions.initiateFrontendBuild(job_id))
                _emit(queue, "LOG", msg="[V3] initiateFrontendBuild sent — on-chain LLM attempting frontend...")
            except Exception as e:
                _emit(queue, "LOG", msg=f"[V3] initiateFrontendBuild failed: {e} — going direct to V1 frontend", error=True)
                # Skip on-chain attempt, go straight to V1
                frontend_start_time = 0  # force immediate fallback check

            # Store artifacts for potential V1 fallback
            _frontend_artifacts = artifacts  # captured in closure below

            def _check_onchain_frontend():
                """Check if on-chain LLM produced valid HTML; fall back to V1 if not."""
                nonlocal frontend_done
                deadline = time.time() + FRONTEND_TIMEOUT

                while time.time() < deadline and not frontend_done:
                    time.sleep(POLL_INTERVAL)
                    try:
                        html = contract.functions.getFrontendHtml(job_id).call()
                    except Exception:
                        continue

                    if html and len(html) > 500 and ("<html" in html.lower() or "<!doctype" in html.lower()):
                        _emit(queue, "LOG", msg="[V3] On-chain frontend generated — submitting for audit...", ok=True)
                        frontend_done = True
                        abi_list = _frontend_artifacts["abi"] if _frontend_artifacts else []
                        _submit_frontend(job_id, html, idea, w3, account, contract, queue, out_dir)
                        return

                if not frontend_done:
                    _emit(queue, "LOG", msg="[V3] Frontend timeout — handing to fallback layer...", error=True)
                    frontend_done = True
                    abi_list = _frontend_artifacts["abi"] if _frontend_artifacts else []
                    chain_id = 50312
                    _run_frontend_fallback(
                        job_id, idea, abi_list, deployed_addr, chain_id,
                        w3, account, contract, queue, out_dir
                    )

            threading.Thread(target=_check_onchain_frontend, daemon=True).start()

        # ── Reactivity registration (once, after deploy) ──────────────────────
        zero = "0x0000000000000000000000000000000000000000"
        if state == STATE_COMPLETE and not reactivity_started \
                and deployed_addr and deployed_addr != zero:
            reactivity_started = True
            threading.Thread(
                target=_register_reactive,
                args=(deployed_addr, idea, audit_approved, queue),
                daemon=True,
            ).start()

        # ── Terminal states ───────────────────────────────────────────────────
        if state == STATE_FAILED:
            _emit(queue, "LOG", msg="[V3] Job failed permanently", error=True)
            break

        time.sleep(POLL_INTERVAL)
