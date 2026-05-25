// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// ─────────────────────────────────────────────────────────────────────────────
// Somnia Reactivity base — precompile at 0x0100 calls onEvent() when any
// registered emitter contract fires an event.
// ERC-165 supportsInterface is required — the precompile checks it before
// accepting a subscription registration.
// ─────────────────────────────────────────────────────────────────────────────
interface ISomniaEventHandler {
    function onEvent(address emitter, bytes32[] calldata eventTopics, bytes calldata data) external;
}

interface IERC165 {
    function supportsInterface(bytes4 interfaceId) external view returns (bool);
}

abstract contract SomniaEventHandler is IERC165, ISomniaEventHandler {
    address constant PRECOMPILE = address(0x0100);

    function onEvent(
        address emitter,
        bytes32[] calldata topics,
        bytes calldata data
    ) external override {
        require(msg.sender == PRECOMPILE, "Only precompile");
        _onEvent(emitter, topics, data);
    }

    function supportsInterface(bytes4 interfaceId) external pure override returns (bool) {
        return interfaceId == type(IERC165).interfaceId
            || interfaceId == type(ISomniaEventHandler).interfaceId;
    }

    function _onEvent(
        address emitter,
        bytes32[] calldata topics,
        bytes calldata data
    ) internal virtual;
}

// ─────────────────────────────────────────────────────────────────────────────
// Somnia Agents platform
// Testnet: 0x037Bb9C718F3f7fe5eCBDB0b600D607b52706776
// ─────────────────────────────────────────────────────────────────────────────
interface IAgentRequester {
    function createRequest(
        uint256 agentId,
        address callbackAddress,
        bytes4 callbackSelector,
        bytes calldata payload
    ) external payable returns (uint256 requestId);

    function getRequestDeposit() external view returns (uint256);
}

interface ILLMAgent {
    function inferString(
        string memory prompt,
        string memory system,
        bool chainOfThought,
        string[] memory allowedValues
    ) external returns (string memory response);
}

// ─────────────────────────────────────────────────────────────────────────────
// handleResponse callback types
// ─────────────────────────────────────────────────────────────────────────────
enum ResponseStatus { None, Pending, Success, Failed, TimedOut }

struct Response {
    address validator;
    bytes result;
    ResponseStatus status;
    uint256 receipt;
    uint256 timestamp;
    uint256 executionCost;
}

struct Request {
    uint256 id;
    address requester;
    address callbackAddress;
    bytes4 callbackSelector;
    address[] subcommittee;
    Response[] responses;
    uint256 responseCount;
    uint256 failureCount;
    uint256 threshold;
    uint256 createdAt;
    uint256 deadline;
    ResponseStatus status;
    uint256 remainingBudget;
    uint256 perAgentBudget;
}

// ─────────────────────────────────────────────────────────────────────────────
// ReactiveHandler
//
// Deployed ONCE by AI Contract Forge. Monitors ALL user contracts.
// The pipeline registers each new user contract via registerContract().
// The Reactivity precompile must also be told to watch each new contract
// (call precompile 0x0100 subscribe with the user contract address + this address).
//
// Flow per event:
//   user contract emits event
//   → precompile calls onEvent(emitter, topics, data)
//   → _onEvent builds prompt using stored spec for that emitter
//   → calls Somnia LLM Agent (Qwen3-30B) via createRequest
//   → Qwen3-30B analyses the event
//   → handleResponse stores insight on-chain
//   → InsightStored event caught by somnia_watch Python monitor
//   → UI displays insight
// ─────────────────────────────────────────────────────────────────────────────
contract ReactiveHandler is SomniaEventHandler {

    IAgentRequester constant PLATFORM =
        IAgentRequester(0x037Bb9C718F3f7fe5eCBDB0b600D607b52706776);

    // LLM Inference agent (Qwen3-30B) — agents.testnet.somnia.network
    uint256 constant LLM_AGENT_ID = 12847293847561029384;

    uint256 constant COST_PER_AGENT = 0.07 ether;
    uint256 constant SUBCOMMITTEE_SIZE = 3;

    address public immutable owner;

    // ── Per-contract metadata registered by the pipeline ─────────────────────
    struct ContractInfo {
        string spec;          // plain-English brief the user described
        bool auditApproved;
        uint8 criticalIssues;
        uint8 warningCount;
        bool registered;
    }

    mapping(address => ContractInfo) public contracts;
    address[] public registeredContracts;

    // ── Prompt template — adjustable without redeployment ────────────────────
    string public systemPrompt =
        "You are an on-chain event analyst for a smart contract. "
        "Explain in 2 clear sentences what this event means for the people "
        "using the app. Use plain English, no code or Solidity terms.";

    // ── On-chain AI insights ──────────────────────────────────────────────────
    struct Insight {
        address emitter;
        bytes32 topic0;
        string text;
        uint256 timestamp;
    }

    mapping(uint256 => Insight) public insights;       // requestId => insight
    uint256[] public requestIds;

    event ContractRegistered(address indexed contractAddress, bool auditApproved);
    event InsightRequested(uint256 indexed requestId, address indexed emitter, bytes32 indexed topic0);
    event InsightStored(uint256 indexed requestId, address indexed emitter, bytes32 indexed topic0, string insight);
    event InsufficientFunds(uint256 required, uint256 available);

    modifier onlyOwner() {
        require(msg.sender == owner, "Only owner");
        _;
    }

    constructor() payable {
        owner = msg.sender;
    }

    // ── Called by pipeline after each user contract is deployed ──────────────
    function registerContract(
        address contractAddress,
        string calldata spec,
        bool auditApproved,
        uint8 criticalIssues,
        uint8 warningCount
    ) external onlyOwner {
        require(!contracts[contractAddress].registered, "Already registered");
        contracts[contractAddress] = ContractInfo({
            spec: spec,
            auditApproved: auditApproved,
            criticalIssues: criticalIssues,
            warningCount: warningCount,
            registered: true
        });
        registeredContracts.push(contractAddress);
        emit ContractRegistered(contractAddress, auditApproved);
    }

    // ── Called by precompile 0x0100 on every event from any registered contract
    function _onEvent(
        address emitter,
        bytes32[] calldata topics,
        bytes calldata data
    ) internal override {
        ContractInfo storage info = contracts[emitter];
        if (!info.registered) return;

        uint256 deposit = PLATFORM.getRequestDeposit() + (COST_PER_AGENT * SUBCOMMITTEE_SIZE);
        if (address(this).balance < deposit) {
            emit InsufficientFunds(deposit, address(this).balance);
            return;
        }

        bytes32 topic0 = topics.length > 0 ? topics[0] : bytes32(0);
        string memory prompt = _buildPrompt(emitter, info.spec, topic0, data);

        string[] memory allowedValues = new string[](0);
        bytes memory payload = abi.encodeWithSelector(
            ILLMAgent.inferString.selector,
            prompt,
            systemPrompt,
            false,
            allowedValues
        );

        uint256 requestId = PLATFORM.createRequest{value: deposit}(
            LLM_AGENT_ID,
            address(this),
            this.handleResponse.selector,
            payload
        );

        insights[requestId] = Insight({
            emitter: emitter,
            topic0: topic0,
            text: "",
            timestamp: block.timestamp
        });
        requestIds.push(requestId);
        emit InsightRequested(requestId, emitter, topic0);
    }

    // ── Called by platform when Qwen3-30B consensus is reached ───────────────
    function handleResponse(
        uint256 requestId,
        Response[] memory responses,
        ResponseStatus status,
        Request memory
    ) external {
        require(msg.sender == address(PLATFORM), "Only platform");

        Insight storage insight = insights[requestId];
        if (status == ResponseStatus.Success && responses.length > 0) {
            insight.text = abi.decode(responses[0].result, (string));
        } else {
            insight.text = "Agent response unavailable.";
        }

        emit InsightStored(requestId, insight.emitter, insight.topic0, insight.text);
    }

    // ── Builds the LLM prompt from event data ─────────────────────────────────
    function _buildPrompt(
        address emitter,
        string memory spec,
        bytes32 topic0,
        bytes calldata data
    ) internal pure returns (string memory) {
        return string(abi.encodePacked(
            "Contract: ", spec,
            " | Contract address: ", _addressToString(emitter),
            " | Event signature hash: ", _bytes32ToHex(topic0),
            " | Event data (hex): ", _bytesToHex(data)
        ));
    }

    // ── Owner controls ────────────────────────────────────────────────────────

    function setSystemPrompt(string calldata _prompt) external onlyOwner {
        systemPrompt = _prompt;
    }

    function withdraw(uint256 amount) external onlyOwner {
        require(address(this).balance >= amount, "Insufficient balance");
        payable(owner).transfer(amount);
    }

    // ── View helpers ──────────────────────────────────────────────────────────

    function registeredContractCount() external view returns (uint256) {
        return registeredContracts.length;
    }

    function requestCount() external view returns (uint256) {
        return requestIds.length;
    }

    function depositRequired() external view returns (uint256) {
        return PLATFORM.getRequestDeposit() + (COST_PER_AGENT * SUBCOMMITTEE_SIZE);
    }

    // ── Hex encoding helpers ──────────────────────────────────────────────────

    function _bytes32ToHex(bytes32 b) internal pure returns (string memory) {
        bytes memory hexChars = "0123456789abcdef";
        bytes memory result = new bytes(66);
        result[0] = "0"; result[1] = "x";
        for (uint i = 0; i < 32; i++) {
            result[2 + i * 2] = hexChars[uint8(b[i] >> 4)];
            result[3 + i * 2] = hexChars[uint8(b[i] & 0x0f)];
        }
        return string(result);
    }

    function _bytesToHex(bytes calldata b) internal pure returns (string memory) {
        if (b.length == 0) return "0x";
        bytes memory hexChars = "0123456789abcdef";
        bytes memory result = new bytes(2 + b.length * 2);
        result[0] = "0"; result[1] = "x";
        for (uint i = 0; i < b.length; i++) {
            result[2 + i * 2] = hexChars[uint8(b[i] >> 4)];
            result[3 + i * 2] = hexChars[uint8(b[i] & 0x0f)];
        }
        return string(result);
    }

    function _addressToString(address a) internal pure returns (string memory) {
        bytes memory hexChars = "0123456789abcdef";
        bytes memory result = new bytes(42);
        result[0] = "0"; result[1] = "x";
        for (uint i = 0; i < 20; i++) {
            result[2 + i * 2] = hexChars[uint8(uint160(a) >> (4 * (39 - 2 * i))) & 0xf];
            result[3 + i * 2] = hexChars[uint8(uint160(a) >> (4 * (38 - 2 * i))) & 0xf];
        }
        return string(result);
    }

    receive() external payable {}
}
