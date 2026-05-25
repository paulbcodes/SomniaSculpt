"""
Real-time event monitor using Somnia's native somnia_watch WebSocket subscription.
Listens to all events emitted by a deployed contract and forwards them to a queue.
"""

import asyncio
import json
import threading
from queue import Queue

import websockets
from eth_abi import decode as abi_decode
from eth_utils import keccak, to_hex

SOMNIA_WS = "wss://api.infra.testnet.somnia.network/ws"


def _build_event_index(abi: list) -> dict:
    """Build topic0 → event ABI entry map from a contract ABI."""
    index = {}
    for item in abi:
        if item.get("type") != "event":
            continue
        inputs = item.get("inputs", [])
        sig = f"{item['name']}({','.join(i['type'] for i in inputs)})"
        topic0 = to_hex(keccak(text=sig))
        index[topic0.lower()] = item
    return index


def _decode_event(event_abi: dict, topics: list, data: str) -> dict:
    """Decode a raw log into named params using the event ABI."""
    inputs = event_abi.get("inputs", [])
    indexed = [i for i in inputs if i.get("indexed")]
    non_indexed = [i for i in inputs if not i.get("indexed")]

    decoded = {}

    # Decode indexed params from topics[1:]
    for i, inp in enumerate(indexed):
        raw = bytes.fromhex(topics[i + 1][2:]) if i + 1 < len(topics) else b"\x00" * 32
        try:
            (val,) = abi_decode([inp["type"]], raw)
            decoded[inp["name"]] = str(val)
        except Exception:
            decoded[inp["name"]] = topics[i + 1] if i + 1 < len(topics) else "?"

    # Decode non-indexed params from data
    if non_indexed and data and data != "0x":
        try:
            types = [i["type"] for i in non_indexed]
            values = abi_decode(types, bytes.fromhex(data[2:]))
            for inp, val in zip(non_indexed, values):
                decoded[inp["name"]] = str(val)
        except Exception:
            pass

    return decoded


async def _run(address: str, abi: list, out_queue: Queue, stop: threading.Event):
    event_index = _build_event_index(abi)

    subscribe = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_subscribe",
        "params": ["somnia_watch", {"address": address, "eth_calls": []}],
    }

    try:
        async with websockets.connect(SOMNIA_WS, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(json.dumps(subscribe))
            ack = json.loads(await ws.recv())
            sub_id = ack.get("result")

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue

                msg = json.loads(raw)
                if msg.get("method") != "eth_subscription":
                    continue

                result = msg["params"]["result"]
                topics = result.get("topics", [])
                data = result.get("data", "0x")
                simulation = result.get("simulationResults", [])

                topic0 = topics[0].lower() if topics else ""
                event_abi = event_index.get(topic0)

                if event_abi:
                    params = _decode_event(event_abi, topics, data)
                    out_queue.put({
                        "type": "CHAIN_EVENT",
                        "event_name": event_abi["name"],
                        "params": params,
                        "simulation": simulation,
                        "contract": address,
                    })
                else:
                    # Unknown event — forward raw so the AI can still reason about it
                    out_queue.put({
                        "type": "CHAIN_EVENT",
                        "event_name": "UnknownEvent",
                        "params": {"topics": topics, "data": data},
                        "simulation": simulation,
                        "contract": address,
                    })

            if sub_id:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_unsubscribe",
                    "params": [sub_id],
                }))

    except Exception as e:
        out_queue.put({"type": "MONITOR_ERROR", "msg": str(e)})


def start(address: str, abi: list, out_queue: Queue, stop: threading.Event):
    """
    Start monitoring in a daemon thread.
    Events are pushed to out_queue as {"type": "CHAIN_EVENT", ...} dicts.
    Set stop to signal shutdown.
    """
    def _thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run(address, abi, out_queue, stop))
        loop.close()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    return t
