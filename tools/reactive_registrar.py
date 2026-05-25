import os
from web3 import Web3

SOMNIA_RPC = os.getenv("DEPLOY_RPC_URL", "https://api.infra.testnet.somnia.network/")
REACTIVE_HANDLER_ADDRESS = "0x3BdF983FAd09D2a2b72b7a8eA10A0ef45d9172C2"
PRECOMPILE_ADDRESS = "0x0000000000000000000000000000000000000100"

# 20 gwei — default maxFeePerGas for subscription callbacks
_CALLBACK_MAX_FEE = 20_000_000_000
# 10M gas — default callback gas limit
_CALLBACK_GAS_LIMIT = 10_000_000

_STATS_ABI = [
    {
        "name": "registeredContractCount",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "requestCount",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

_HANDLER_ABI = [
    {
        "name": "registerContract",
        "type": "function",
        "inputs": [
            {"name": "contractAddress", "type": "address"},
            {"name": "spec", "type": "string"},
            {"name": "auditApproved", "type": "bool"},
            {"name": "criticalIssues", "type": "uint8"},
            {"name": "warningCount", "type": "uint8"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    }
]

_PRECOMPILE_ABI = [
    {
        "name": "subscribe",
        "type": "function",
        "inputs": [
            {
                "name": "subscriptionData",
                "type": "tuple",
                "components": [
                    {"name": "eventTopics", "type": "bytes32[4]"},
                    {"name": "origin", "type": "address"},
                    {"name": "caller", "type": "address"},
                    {"name": "emitter", "type": "address"},
                    {"name": "handlerContractAddress", "type": "address"},
                    {"name": "handlerFunctionSelector", "type": "bytes4"},
                    {"name": "priorityFeePerGas", "type": "uint64"},
                    {"name": "maxFeePerGas", "type": "uint64"},
                    {"name": "gasLimit", "type": "uint64"},
                    {"name": "isGuaranteed", "type": "bool"},
                    {"name": "isCoalesced", "type": "bool"},
                ],
            }
        ],
        "outputs": [{"name": "subscriptionId", "type": "uint256"}],
        "stateMutability": "nonpayable",
    }
]


def _w3():
    w3 = Web3(Web3.HTTPProvider(SOMNIA_RPC))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Somnia RPC at {SOMNIA_RPC}")
    return w3


def _send(w3, account, tx):
    tx["nonce"] = w3.eth.get_transaction_count(account.address)
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
    priority_fee = w3.to_wei(1, "gwei")
    tx["maxFeePerGas"] = base_fee * 2 + priority_fee
    tx["maxPriorityFeePerGas"] = priority_fee
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted: {receipt.transactionHash.hex()}")
    return receipt


def register_contract(
    user_contract_address: str,
    spec: str,
    audit_approved: bool,
    critical_issues: int,
    warning_count: int,
) -> str:
    """
    Register a newly deployed user contract with ReactiveHandler.
    Returns the transaction hash.
    """
    private_key = os.getenv("DEPLOY_PRIVATE_KEY")
    if not private_key:
        raise EnvironmentError("DEPLOY_PRIVATE_KEY not set in environment")

    w3 = _w3()
    account = w3.eth.account.from_key(private_key)
    handler = w3.eth.contract(
        address=Web3.to_checksum_address(REACTIVE_HANDLER_ADDRESS),
        abi=_HANDLER_ABI,
    )

    tx = handler.functions.registerContract(
        Web3.to_checksum_address(user_contract_address),
        spec[:60],   # just enough context for the LLM prompt — reduces storage gas cost
        audit_approved,
        min(critical_issues, 255),
        min(warning_count, 255),
    ).build_transaction({"from": account.address, "gas": 5_000_000})

    receipt = _send(w3, account, tx)
    return receipt.transactionHash.hex()


def get_stats() -> dict:
    """Read live platform stats directly from ReactiveHandler on-chain."""
    try:
        w3 = _w3()
        handler = w3.eth.contract(
            address=Web3.to_checksum_address(REACTIVE_HANDLER_ADDRESS),
            abi=_STATS_ABI,
        )
        contracts = handler.functions.registeredContractCount().call()
        insights = handler.functions.requestCount().call()
        return {"contracts": contracts, "insights": insights}
    except Exception:
        return {"contracts": 0, "insights": 0}


def subscribe_to_events(user_contract_address: str) -> str:
    """
    Register user contract with Somnia Reactivity precompile (0x0100).
    Tells the precompile: watch user_contract_address, call our ReactiveHandler.
    Returns the transaction hash.
    Caller's wallet must have >= 32 STT balance.
    """
    private_key = os.getenv("DEPLOY_PRIVATE_KEY")
    if not private_key:
        raise EnvironmentError("DEPLOY_PRIVATE_KEY not set in environment")

    w3 = _w3()
    account = w3.eth.account.from_key(private_key)
    precompile = w3.eth.contract(
        address=Web3.to_checksum_address(PRECOMPILE_ADDRESS),
        abi=_PRECOMPILE_ABI,
    )

    # onEvent(address,bytes32[],bytes) selector
    on_event_selector = w3.keccak(text="onEvent(address,bytes32[],bytes)")[:4]

    zero_address = "0x0000000000000000000000000000000000000000"
    zero_topic = b"\x00" * 32

    subscription_data = (
        [zero_topic, zero_topic, zero_topic, zero_topic],   # eventTopics — all events
        zero_address,                                         # origin wildcard
        zero_address,                                         # caller reserved
        Web3.to_checksum_address(user_contract_address),     # emitter to watch
        Web3.to_checksum_address(REACTIVE_HANDLER_ADDRESS),  # our handler
        on_event_selector,                                    # onEvent selector
        0,                                                    # priorityFeePerGas
        _CALLBACK_MAX_FEE,                                   # maxFeePerGas (20 gwei)
        _CALLBACK_GAS_LIMIT,                                 # gasLimit (10M)
        False,                                               # isGuaranteed
        False,                                               # isCoalesced
    )

    tx = precompile.functions.subscribe(subscription_data).build_transaction({
        "from": account.address,
        "gas": 500_000,
    })

    receipt = _send(w3, account, tx)
    return receipt.transactionHash.hex()
