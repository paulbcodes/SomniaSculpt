# Somnia Sculpt

**Describe a dapp idea in plain English. Somnia's on-chain AI agents build it, audit it, and deploy it — no coding knowledge required.**

Built for the Somnia Agentic L1 Hackathon.

---

## Live Demo

> Link available at final submission.

---

## What It Does

Somnia Sculpt turns a plain-English description into a live, deployed smart contract and working dapp frontend. You describe what you want — a token, a voting system, an NFT drop, anything. On-chain AI agents write the Solidity, verify it compiles, audit it for security issues, and generate a usable dapp interface. You deploy with one MetaMask click. The full build and audit trail is stored on-chain and verifiable by anyone.

---

## Why It's Agentic L1-Native

This project doesn't just run on Somnia — it uses Somnia's agentic primitives as the core execution layer for every meaningful decision.

### On-Chain LLM Agents
Contract generation, security auditing, and frontend generation are all handled by Somnia's on-chain LLM agents. Every response arrives via `handleResponse()` callback and is stored on-chain. The build and audit history is fully verifiable — not a black box.

### On-Chain JSON API Agent
After the LLM agent writes Solidity, a JSON API agent calls an external compile service to verify the code compiles cleanly. The result — success or error message — comes back to the contract on-chain. The contract then decides whether to retry, fix, or proceed to audit.

### Autonomous On-Chain State Machine
`PipelineOrchestratorV3` drives the entire pipeline without human intervention:

```
Building → Compiling → Auditing → Approved → [user deploys] → Complete
                                      ↓
                            AwaitingFallback (if agents exhaust retries)
```

Agent responses drive state transitions. If the audit finds issues, the contract calls for a fix and loops automatically up to `MAX_FIX` times. Every transition is an on-chain event.

### Reactivity Precompile (0x0100)
A `ReactiveHandler` contract is registered with Somnia's Reactivity precompile. Once a user contract is live, the precompile watches it autonomously — no polling, no cron job. When the deployed contract emits any event, the precompile calls `ReactiveHandler._onEvent()`, which immediately requests an AI analysis from Somnia's on-chain LLM agent. The insight is stored on-chain and streamed to the UI in real time.

This is the full loop: user contract fires an event → precompile triggers → on-chain AI analyses it → result stored on-chain → user sees it.

---

## Architecture

```
User (browser)
    │  describe idea
    ▼
PipelineOrchestratorV3.sol
    │
    ├── createRequest() ──▶  Somnia LLM Agent    (write Solidity)
    │   ◀── handleResponse()
    │
    ├── createRequest() ──▶  Somnia JSON API Agent  (compile check)
    │   ◀── handleResponse()
    │
    ├── createRequest() ──▶  Somnia LLM Agent    (security audit)
    │   ◀── handleResponse()
    │
    │   [state = Approved — user deploys via MetaMask]
    │
    ├── createRequest() ──▶  Somnia LLM Agent    (frontend generation)
    │   ◀── FrontendReady
    │
    └── ReactiveHandler.sol ◀──── Reactivity precompile (0x0100)
            │                     (fires on any event from deployed contract)
            └── createRequest() ──▶  Somnia LLM Agent  (event analysis)
                ◀── handleResponse()  (insight stored on-chain)
```

**Supporting off-chain components:**
- `pipeline_v3.py` — monitors for `JobFallback` events, runs fallback builder, submits results back on-chain via `submitOffChainBuild()`
- `app_v3.py` — Flask server and SSE event stream
- `compiler_api/` — compile service hosted on VPS, called by the JSON API agent

---

## Somnia Primitives Used

| Primitive | Where |
|---|---|
| On-Chain LLM Agents | Contract generation, security audit, frontend generation, event analysis |
| On-Chain JSON API Agent | Compile verification via external API |
| Reactivity Precompile (0x0100) | Autonomous event monitoring on deployed user contracts |
| somnia_watch WebSocket | Real-time event streaming to the browser |

---

## Deployed Contracts (Somnia Testnet — Chain 50312)

| Contract | Address |
|---|---|
| PipelineOrchestratorV3 | `0xD21f0262bE547Ac4d37979421e6Cb020FAD40B45` |
| ReactiveHandler | `0x3BdF983FAd09D2a2b72b7a8eA10A0ef45d9172C2` |

---

## The Fallback Layer

On-chain agents are the first choice for everything. If they fail to produce valid Solidity within `MAX_FIX` attempts, the contract emits `JobFallback`. An off-chain watcher detects this, runs an alternative build, and submits the result back via `submitOffChainBuild()`. The on-chain compile and audit still happen — the audit trail is always on-chain regardless of what built the code. The UI flags when the fallback path was taken.

This means the app never fails catastrophically in production, while the on-chain agent path remains primary and the result is always transparent about what ran where.

---

## Designed for Somnia's Roadmap

Frontend generation on-chain currently falls back to off-chain for one specific reason: generating a complete dapp HTML file is a large-output task, and LLM inference pricing on Somnia today is fixed rather than resource-based. Under fixed pricing, runners evaluate whether a request is worth their compute before accepting it — a large token output at a fixed rate isn't always picked up.

Somnia's documentation notes this is a stop-gap. The roadmap moves to per-request pricing based on actual resource consumption — tokens in, tokens out, container-seconds. When that ships, large-output tasks like frontend generation become properly priced and on-chain agents will handle them reliably.

Our fallback architecture was built with exactly this transition in mind. The fallback exists for today. The on-chain path exists for where Somnia is going.

---

## Repository Structure

```
contracts/
  PipelineOrchestratorV3.sol   # on-chain state machine
  ReactiveHandler.sol          # Reactivity precompile handler
agents/
  builder.py                   # fallback: contract builder
  auditor.py                   # fallback: security auditor
  frontend_builder.py          # fallback: frontend generator
tools/
  reactive_registrar.py        # registers contracts with Reactivity precompile
  somnia_monitor.py            # somnia_watch WebSocket listener
compiler_api/
  app.py                       # compile service (called by JSON API agent)
static/
  v3.html                      # browser UI
app_v3.py                      # Flask server
pipeline_v3.py                 # fallback watcher
```

---

## Roadmap

- **ReactiveHandler V3 integration** — wire autonomous event monitoring into the V3 pipeline so all deployed contracts are automatically subscribed to the Reactivity precompile
- **On-chain frontend generation** — becomes fully viable when Somnia's per-request pricing ships; fallback path will be retired
- **Prompt routing** — use the audit result to select a specialised fix prompt (reentrancy failure, access control failure, arithmetic issue each get a targeted prompt); agent output driving agent selection is a natural next step for the state machine
- **Prompt tuning** — reduce false positive audit failures through targeted prompt refinement once more test data is collected
- **UI improvements** — session persistence, richer status feedback, mobile layout
