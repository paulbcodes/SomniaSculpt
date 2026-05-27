// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

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

interface IJsonApiAgent {
    function fetchString(
        string memory url,
        string memory selector
    ) external returns (string memory result);
}

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

enum JobState {
    Building,           // 0 - on-chain LLM generating Solidity
    Compiling,          // 1 - JSON API agent checking compile
    Auditing,           // 2 - on-chain LLM auditing
    AwaitingFallback,   // 3 - on-chain agents exhausted, off-chain AI taking over
    Approved,           // 4 - audit passed, ready to deploy
    Complete,           // 5 - contract deployed
    Failed              // 6 - all paths exhausted
}

enum RequestType { Build, Audit, Compile, FrontendBuild, FrontendAudit }

struct Job {
    address requester;
    string idea;
    string solidity;
    string auditResult;
    address deployedAddress;
    JobState state;
    uint8 fixAttempts;
    bool auditApproved;
    bool offchainBuilt;         // true when Claude built this contract (on-chain agents failed)
    string frontendUrl;         // set after frontend audit — URL to generated dapp HTML
    bool frontendAuditPassed;
    uint256 createdAt;
}

// ─────────────────────────────────────────────────────────────────────────────
// PipelineOrchestratorV3
//
// Extends V2 with off-chain fallback paths:
//
// CONTRACT: on-chain LLM tries up to MAX_FIX times.
//   If all fail → AwaitingFallback state, emit JobFallback.
//   Backend runs V1 (Claude) build+audit loop, calls submitOffChainBuild().
//   On-chain agents then compile + audit (deferred prompt) + deploy as normal.
//
// FRONTEND: backend calls initiateFrontendBuild() after contract deploys.
//   On-chain LLM attempts HTML generation. If good → FrontendBuilt event.
//   If timeout/garbage → FrontendFallback event. Backend runs V1 frontend builder.
//   Either way backend calls submitFrontendAudit() with script content + URL.
//   On-chain LLM audits script. Frontend URL stored on-chain.
// ─────────────────────────────────────────────────────────────────────────────
contract PipelineOrchestratorV3 {

    IAgentRequester constant PLATFORM =
        IAgentRequester(0x037Bb9C718F3f7fe5eCBDB0b600D607b52706776);

    uint256 constant LLM_AGENT_ID    = 12847293847561029384;
    uint256 constant JSON_AGENT_ID   = 13174292974160097713;

    uint256 constant LLM_COST_PER_AGENT  = 0.07 ether;
    uint256 constant JSON_COST_PER_AGENT = 0.03 ether;
    uint256 constant SUBCOMMITTEE        = 3;
    uint8   constant MAX_FIX             = 3;

    string constant COMPILE_BASE_URL = "https://somniasculpt.site/solc/job/";

    address public immutable owner;
    address public backendWallet;

    uint256 public jobCount;
    mapping(uint256 => Job)          private _jobs;
    mapping(uint256 => uint256)      private _requestToJob;
    mapping(uint256 => RequestType)  private _requestType;
    mapping(uint256 => string)       private _pendingError;
    mapping(uint256 => string)       private _pendingFrontendHtml;
    mapping(uint256 => string)       private _pendingFrontendUrl;

    // ── Prompts ───────────────────────────────────────────────────────────────
    string public builderSystem =
        "You are an expert Solidity smart contract developer. "
        "Write clean, secure, fully self-contained Solidity contracts. "
        "Rules: Always use SPDX-License-Identifier: MIT. "
        "Always use pragma solidity ^0.8.20. "
        "Never import external libraries, implement everything inline. "
        "No placeholders, no TODOs, complete working code only. "
        "Use custom errors instead of require strings where appropriate. "
        "Emit events for all state changes. "
        "IMPORTANT: Never put mappings inside structs. Instead use separate top-level mappings with compound keys. "
        "Return ONLY the Solidity code. No explanation. No markdown. "
        "Start with // SPDX-License-Identifier";

    string public auditorSystem =
        "You are a smart contract security auditor. "
        "Review the contract against the specification and return ONLY valid JSON, no other text, no markdown. "
        "JSON structure must be exactly: "
        "{\"critical_issues\": [\"description\", ...], "
        "\"warnings\": [\"description\", ...], "
        "\"approved\": true or false}. "
        "Set approved to true only when critical_issues is empty. "
        "Only flag issues you are certain about. "
        "If the spec explicitly allows open access, do not flag it as a security issue. "
        "Flag real security vulnerabilities and spec deviations only. Ignore style preferences.";

    string public deferredAuditorSystem =
        "You are a smart contract security auditor. "
        "This contract was built by an advanced AI system after the on-chain agent could not solve it. "
        "It has already passed an advanced AI security review. "
        "Return ONLY valid JSON: "
        "{\"critical_issues\": [\"description\", ...], "
        "\"warnings\": [\"description\", ...], "
        "\"approved\": true or false}. "
        "Audit for genuine security vulnerabilities only - reentrancy, overflow, access control bugs. "
        "Do not flag code style, naming conventions, or minor issues. "
        "Set approved to true unless you identify a critical exploitable vulnerability.";

    string public frontendBuilderSystem =
        "You are a Web3 frontend developer. "
        "Build a complete self-contained single-file HTML frontend for a deployed smart contract. "
        "Use ethers.js v6 via CDN: https://cdnjs.cloudflare.com/ajax/libs/ethers/6.7.0/ethers.umd.min.js. "
        "Include a MetaMask connect button. Show the connected wallet address. "
        "Add read and write sections for the contract functions. "
        "Use a dark theme with blue accents. "
        "Return ONLY the complete HTML file ending with </body></html>. No explanation. No markdown.";

    string public frontendAuditorSystem =
        "You are a Web3 frontend security reviewer. "
        "Review this JavaScript code from a smart contract frontend. "
        "Return ONLY valid JSON: "
        "{\"critical_issues\": [\"description\", ...], "
        "\"approved\": true or false}. "
        "Flag: XSS vulnerabilities, malicious scripts, wrong contract address usage, broken MetaMask connection. "
        "Set approved to true if the frontend is safe and functional. Ignore style preferences.";

    // ── Events ────────────────────────────────────────────────────────────────
    event JobCreated(uint256 indexed jobId, address indexed requester, string idea);
    event SolidityReady(uint256 indexed jobId);
    event CompileResult(uint256 indexed jobId, bool success, string errors);
    event AuditResult(uint256 indexed jobId, bool approved, string result);
    event ReadyToDeploy(uint256 indexed jobId);
    event JobComplete(uint256 indexed jobId, address indexed contractAddress);
    event JobFailed(uint256 indexed jobId);
    event JobFallback(uint256 indexed jobId);
    event OffchainBuildSubmitted(uint256 indexed jobId);
    event FrontendBuilt(uint256 indexed jobId);
    event FrontendFallback(uint256 indexed jobId);
    event FrontendAuditResult(uint256 indexed jobId, bool approved, string result);
    event FrontendReady(uint256 indexed jobId, string frontendUrl);

    modifier onlyOwner() {
        require(msg.sender == owner, "Only owner");
        _;
    }

    modifier onlyBackend() {
        require(msg.sender == backendWallet, "Only backend");
        _;
    }

    constructor() payable {
        owner = msg.sender;
        backendWallet = msg.sender;
    }

    // ── User submits idea ─────────────────────────────────────────────────────
    function submitIdea(string calldata idea) external payable {
        require(msg.value >= depositRequired(), "Send enough STT");

        uint256 jobId = jobCount++;
        _jobs[jobId] = Job({
            requester:           msg.sender,
            idea:                idea,
            solidity:            "",
            auditResult:         "",
            deployedAddress:     address(0),
            state:               JobState.Building,
            fixAttempts:         0,
            auditApproved:       false,
            offchainBuilt:       false,
            frontendUrl:         "",
            frontendAuditPassed: false,
            createdAt:           block.timestamp
        });

        emit JobCreated(jobId, msg.sender, idea);
        _llmBuild(jobId);
    }

    // ── User notifies after MetaMask deploy ───────────────────────────────────
    function notifyDeployed(uint256 jobId, address contractAddress) external {
        Job storage job = _jobs[jobId];
        require(msg.sender == job.requester, "Not your job");
        require(job.state == JobState.Approved, "Not approved yet");
        require(contractAddress != address(0), "Invalid address");

        job.deployedAddress = contractAddress;
        job.state = JobState.Complete;
        emit JobComplete(jobId, contractAddress);
    }

    // ── Backend: submit Claude-built contract when on-chain agents failed ─────
    function submitOffChainBuild(uint256 jobId, string calldata solidity) external onlyBackend {
        Job storage job = _jobs[jobId];
        require(job.state == JobState.AwaitingFallback, "Job not awaiting fallback");
        require(bytes(solidity).length > 0, "Empty solidity");

        job.solidity      = solidity;
        job.offchainBuilt = true;
        job.fixAttempts   = 0;
        job.state         = JobState.Compiling;

        emit OffchainBuildSubmitted(jobId);
        _jsonApiCompile(jobId);
    }

    // ── Backend: trigger on-chain LLM frontend generation attempt ────────────
    function initiateFrontendBuild(uint256 jobId) external onlyBackend {
        Job storage job = _jobs[jobId];
        require(job.state == JobState.Complete, "Job not complete");
        require(job.deployedAddress != address(0), "No deployed address");
        _llmFrontendBuild(jobId);
    }

    // ── Backend: submit Claude-built frontend for on-chain LLM audit ─────────
    function submitFrontendAudit(
        uint256 jobId,
        string calldata scriptContent,
        string calldata frontendUrl
    ) external onlyBackend {
        Job storage job = _jobs[jobId];
        require(job.state == JobState.Complete, "Job not complete");
        require(bytes(frontendUrl).length > 0, "Empty URL");

        _pendingFrontendUrl[jobId] = frontendUrl;
        _llmFrontendAudit(jobId, scriptContent);
    }

    // ── Callback from Somnia agents platform ──────────────────────────────────
    function handleResponse(
        uint256 requestId,
        Response[] memory responses,
        ResponseStatus status,
        Request memory
    ) external {
        require(msg.sender == address(PLATFORM), "Only platform");

        uint256 jobId       = _requestToJob[requestId];
        RequestType reqType = _requestType[requestId];
        Job storage job     = _jobs[jobId];

        string memory result = "";
        if (status == ResponseStatus.Success && responses.length > 0) {
            result = abi.decode(responses[0].result, (string));
        }

        if (reqType == RequestType.FrontendBuild) {
            bool hasHtml = _contains(result, "<!DOCTYPE") ||
                           _contains(result, "<html")     ||
                           _contains(result, "<body");
            if (hasHtml && bytes(result).length > 500) {
                _pendingFrontendHtml[jobId] = result;
                emit FrontendBuilt(jobId);
            } else {
                emit FrontendFallback(jobId);
            }

        } else if (reqType == RequestType.FrontendAudit) {
            bool approved = _contains(result, '"approved":true') ||
                            _contains(result, '"approved": true');
            job.frontendAuditPassed = approved;
            job.frontendUrl         = _pendingFrontendUrl[jobId];
            emit FrontendAuditResult(jobId, approved, result);
            if (bytes(job.frontendUrl).length > 0) {
                emit FrontendReady(jobId, job.frontendUrl);
            }

        } else if (reqType == RequestType.Audit) {
            if (job.state != JobState.Auditing) return;
            _handleContractAudit(jobId, result);

        } else if (reqType == RequestType.Compile) {
            if (job.state != JobState.Compiling) return;
            bool ok = keccak256(bytes(result)) == keccak256(bytes("ok"));
            emit CompileResult(jobId, ok, ok ? "" : result);

            if (ok) {
                job.state = JobState.Auditing;
                _llmAudit(jobId);
            } else {
                if (job.fixAttempts >= MAX_FIX) {
                    if (job.offchainBuilt) {
                        job.state = JobState.Failed;
                        emit JobFailed(jobId);
                    } else {
                        job.state = JobState.AwaitingFallback;
                        emit JobFallback(jobId);
                    }
                    return;
                }
                job.fixAttempts++;
                _pendingError[jobId] = result;
                _llmFix(jobId);
            }

        } else {
            // Build or Fix response — ignore if job is no longer in an active build state.
            // Late Qwen callbacks arriving after AwaitingFallback/Approved/Complete/Failed
            // would otherwise blindly overwrite state back to Compiling.
            if (job.state != JobState.Building &&
                job.state != JobState.Compiling &&
                job.state != JobState.Auditing) return;
            job.solidity = result;
            job.state    = JobState.Compiling;
            emit SolidityReady(jobId);
            _jsonApiCompile(jobId);
        }
    }

    function _handleContractAudit(uint256 jobId, string memory result) internal {
        Job storage job = _jobs[jobId];
        job.auditResult = result;
        bool approved   = _contains(result, '"approved":true') ||
                          _contains(result, '"approved": true');
        job.auditApproved = approved;
        emit AuditResult(jobId, approved, result);

        if (approved) {
            job.state = JobState.Approved;
            emit ReadyToDeploy(jobId);
        } else {
            if (job.fixAttempts >= MAX_FIX) {
                if (job.offchainBuilt) {
                    job.state = JobState.Failed;
                    emit JobFailed(jobId);
                } else {
                    job.state = JobState.AwaitingFallback;
                    emit JobFallback(jobId);
                }
                return;
            }
            job.fixAttempts++;
            _pendingError[jobId] = result;
            _llmFix(jobId);
        }
    }

    // ── View ──────────────────────────────────────────────────────────────────
    function getJob(uint256 jobId) external view returns (
        address requester,
        uint8   state,
        string memory idea,
        string memory solidity,
        string memory auditResult,
        address deployedAddress,
        bool    auditApproved,
        uint8   fixAttempts,
        uint256 createdAt,
        bool    offchainBuilt,
        string memory frontendUrl,
        bool    frontendAuditPassed
    ) {
        Job storage j = _jobs[jobId];
        return (
            j.requester, uint8(j.state), j.idea, j.solidity,
            j.auditResult, j.deployedAddress, j.auditApproved,
            j.fixAttempts, j.createdAt,
            j.offchainBuilt, j.frontendUrl, j.frontendAuditPassed
        );
    }

    function getFrontendHtml(uint256 jobId) external view returns (string memory) {
        return _pendingFrontendHtml[jobId];
    }

    function depositRequired() public view returns (uint256) {
        return (_llmCost() * 10) + (_jsonCost() * 6);
    }

    // ── Owner controls ────────────────────────────────────────────────────────
    function setBackendWallet(address _wallet)                 external onlyOwner { backendWallet          = _wallet; }
    function setBuilderSystem(string calldata prompt)          external onlyOwner { builderSystem          = prompt; }
    function setAuditorSystem(string calldata prompt)          external onlyOwner { auditorSystem          = prompt; }
    function setDeferredAuditorSystem(string calldata prompt)  external onlyOwner { deferredAuditorSystem  = prompt; }
    function setFrontendBuilderSystem(string calldata prompt)  external onlyOwner { frontendBuilderSystem  = prompt; }
    function setFrontendAuditorSystem(string calldata prompt)  external onlyOwner { frontendAuditorSystem  = prompt; }

    function withdraw(uint256 amount) external onlyOwner {
        require(address(this).balance >= amount, "Insufficient balance");
        payable(owner).transfer(amount);
    }

    // ── Internal: LLM calls ───────────────────────────────────────────────────
    function _llmBuild(uint256 jobId) internal {
        string memory prompt = string(abi.encodePacked(
            "Write a Solidity smart contract for this specification: ",
            _jobs[jobId].idea,
            ". Return ONLY the Solidity code. No explanation. No markdown."
        ));
        _llmRequest(jobId, prompt, builderSystem, RequestType.Build);
    }

    function _llmFix(uint256 jobId) internal {
        string memory prompt = string(abi.encodePacked(
            "Fix this Solidity smart contract. Return ONLY the fixed Solidity code. No explanation. No markdown.\n\n",
            "ORIGINAL SPEC:\n", _jobs[jobId].idea,
            "\n\nCURRENT CODE:\n", _jobs[jobId].solidity,
            "\n\nISSUES TO FIX:\n", _pendingError[jobId]
        ));
        _llmRequest(jobId, prompt, builderSystem, RequestType.Build);
    }

    function _llmAudit(uint256 jobId) internal {
        string memory prompt = string(abi.encodePacked(
            "Audit this smart contract.\n\nSPECIFICATION:\n", _jobs[jobId].idea,
            "\n\nCONTRACT CODE:\n", _jobs[jobId].solidity,
            "\n\nReturn ONLY the JSON audit result."
        ));
        string memory system = _jobs[jobId].offchainBuilt ? deferredAuditorSystem : auditorSystem;
        _llmRequest(jobId, prompt, system, RequestType.Audit);
    }

    function _llmFrontendBuild(uint256 jobId) internal {
        Job storage job = _jobs[jobId];
        string memory prompt = string(abi.encodePacked(
            "Build a complete Web3 HTML frontend for this smart contract.\n",
            "SPEC: ", job.idea, "\n",
            "CONTRACT ADDRESS: ", _toHexString(job.deployedAddress), "\n",
            "CHAIN ID: 50312 (Somnia Testnet)\n",
            "Return ONLY the complete HTML file ending with </body></html>."
        ));
        _llmRequest(jobId, prompt, frontendBuilderSystem, RequestType.FrontendBuild);
    }

    function _llmFrontendAudit(uint256 jobId, string calldata scriptContent) internal {
        string memory prompt = string(abi.encodePacked(
            "Audit this JavaScript from a smart contract frontend.\n\n",
            "SPEC: ", _jobs[jobId].idea, "\n\n",
            "SCRIPT:\n", scriptContent, "\n\n",
            "Return ONLY the JSON audit result."
        ));
        _llmRequest(jobId, prompt, frontendAuditorSystem, RequestType.FrontendAudit);
    }

    function _llmRequest(
        uint256 jobId,
        string memory prompt,
        string memory system,
        RequestType rtype
    ) internal {
        uint256 cost = _llmCost();
        require(address(this).balance >= cost, "Contract underfunded");

        string[] memory none = new string[](0);
        bytes memory payload = abi.encodeWithSelector(
            ILLMAgent.inferString.selector,
            prompt,
            system,
            false,
            none
        );

        uint256 requestId = PLATFORM.createRequest{value: cost}(
            LLM_AGENT_ID,
            address(this),
            this.handleResponse.selector,
            payload
        );

        _requestToJob[requestId] = jobId;
        _requestType[requestId]  = rtype;
    }

    // ── Internal: JSON API compile call ───────────────────────────────────────
    function _jsonApiCompile(uint256 jobId) internal {
        uint256 cost = _jsonCost();
        require(address(this).balance >= cost, "Contract underfunded");

        string memory url = string(abi.encodePacked(
            COMPILE_BASE_URL,
            _toString(jobId),
            "/result?v=",
            _toString(_jobs[jobId].fixAttempts),
            "&orch=",
            _toHexString(address(this)),
            _jobs[jobId].offchainBuilt ? "&offchain=1" : ""
        ));

        bytes memory payload = abi.encodeWithSelector(
            IJsonApiAgent.fetchString.selector,
            url,
            "result"
        );

        uint256 requestId = PLATFORM.createRequest{value: cost}(
            JSON_AGENT_ID,
            address(this),
            this.handleResponse.selector,
            payload
        );

        _requestToJob[requestId] = jobId;
        _requestType[requestId]  = RequestType.Compile;
    }

    function _llmCost() internal view returns (uint256) {
        return PLATFORM.getRequestDeposit() + (LLM_COST_PER_AGENT * SUBCOMMITTEE);
    }

    function _jsonCost() internal view returns (uint256) {
        return PLATFORM.getRequestDeposit() + (JSON_COST_PER_AGENT * SUBCOMMITTEE);
    }

    function _toString(uint256 value) internal pure returns (string memory) {
        if (value == 0) return "0";
        uint256 temp = value;
        uint256 digits;
        while (temp != 0) { digits++; temp /= 10; }
        bytes memory buffer = new bytes(digits);
        while (value != 0) {
            digits -= 1;
            buffer[digits] = bytes1(uint8(48 + uint256(value % 10)));
            value /= 10;
        }
        return string(buffer);
    }

    function _toHexString(address addr) internal pure returns (string memory) {
        bytes memory buffer = new bytes(42);
        buffer[0] = "0";
        buffer[1] = "x";
        bytes20 addrBytes = bytes20(addr);
        for (uint i = 0; i < 20; i++) {
            uint8 b = uint8(addrBytes[i]);
            buffer[2 + i * 2] = _hexChar(b >> 4);
            buffer[3 + i * 2] = _hexChar(b & 0x0f);
        }
        return string(buffer);
    }

    function _hexChar(uint8 nibble) internal pure returns (bytes1) {
        if (nibble < 10) return bytes1(uint8(48 + nibble));
        return bytes1(uint8(87 + nibble));
    }

    function _contains(string memory source, string memory search) internal pure returns (bool) {
        bytes memory s = bytes(source);
        bytes memory q = bytes(search);
        if (q.length > s.length) return false;
        for (uint i = 0; i <= s.length - q.length; i++) {
            bool found = true;
            for (uint j = 0; j < q.length; j++) {
                if (s[i + j] != q[j]) { found = false; break; }
            }
            if (found) return true;
        }
        return false;
    }

    receive() external payable {}
}
