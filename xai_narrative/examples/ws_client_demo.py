"""Stand-in for Alok's Spring Boot gateway: streams simulated frames to the service and prints what comes back.

Terminal 1:  python -m fatigue_xai.api
Terminal 2:  python examples/ws_client_demo.py [--scenario drift_to_microsleep] [--speed 20]

--speed 1 = real time (24 fps); 20 = 20x faster.  Use NARRATIVE_LLM=none in terminal 1 for template-only.
"""
import argparse, asyncio, json, os, sys
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fatigue_xai.simulator import generate  # noqa: E402


async def main(url, scenario, speed):
    frames, *_ = generate(scenario)
    async with websockets.connect(f"{url}/ws/demo", max_size=None) as ws:
        async def reader():
            streaming = False
            async for raw in ws:
                e = json.loads(raw)
                t = e["type"]
                if t == "assessment":
                    a = e["assessment"]
                    print(f"\r[{a['ts_str']}] level={a['risk_level']:<9} p={a['p_fatigue_smoothed']:.2f} "
                          f"trend={a['trend']:<8} findings={[f['id'] for f in a['findings']][:3]}      ", end="", flush=True)
                elif t == "narrative_final":
                    print(f"\n>>> ({e['source']}, {e['latency_ms']:.0f} ms) {e['text']}\n")
                elif t == "narrative_retry":
                    print(f"\n    guard rejected a draft: {e['violations']}")
                elif t == "error":
                    print("\nERROR:", e)
        task = asyncio.create_task(reader())
        for i, f in enumerate(frames):
            await ws.send(f.model_dump_json())
            await asyncio.sleep(1 / 24 / speed)
        await asyncio.sleep(15 if speed < 5 else 8)   # let the last narrative finish
        task.cancel()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8100")
    ap.add_argument("--scenario", default="drift_to_microsleep",
                    choices=["drift_to_microsleep", "alert_steady", "yawn_only", "occlusion", "night_low_light"])
    ap.add_argument("--speed", type=float, default=20.0)
    a = ap.parse_args()
    asyncio.run(main(a.url, a.scenario, a.speed))
