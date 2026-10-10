"""Reliability evaluation for the narrative layer (supports RO4's "reliability of generated narratives").

    python eval/run_eval.py                  # rules + guard + scripted narrators (no LLM needed)
    OLLAMA_MODEL=llama3.1:8b python eval/run_eval.py --ollama     # adds real-LLM faithfulness & latency

Outputs eval/out/report.md, eval/out/narratives_for_rating.csv

All telemetry here is SYNTHETIC (fatigue_xai.simulator).  The numbers characterise the pipeline's
logic and the LLM's faithfulness - not detection accuracy on real drivers.
"""
import argparse, asyncio, csv, os, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fatigue_xai.guard import validate
from fatigue_xai.llm import llm_from_env
from fatigue_xai.narrative import NarrativeConfig, NarrativeService, template_body
from fatigue_xai.prompt import facts_view
from fatigue_xai.rules import FacsRuleEngine
from fatigue_xai.schemas import LEVELS
from fatigue_xai.simulator import generate

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
SEEDS = range(1, 11)


def assessments(scenario, seed):
    frames, phases, micro = generate(scenario, seed=seed)
    e = FacsRuleEngine("ev")
    return [a for f in frames if (a := e.ingest(f))], phases, micro


def rule_metrics():
    rows = {}
    # 1. false alarms on alert drivers
    fa = tot = 0
    for s in SEEDS:
        out, *_ = assessments("alert_steady", s)
        post = [a for a in out if a.ts > 20]
        fa += sum(a.risk_level != "LOW" for a in post)
        tot += len(post)
    rows["false_alarm_rate_alert_steady"] = fa / tot
    # 2. lead time before first microsleep (closure >= 1 s) and before first yawn
    leads, detected, flips = [], 0, []
    for s in SEEDS:
        out, _, micro = assessments("drift_to_microsleep", s)
        first = next((a for a in out if a.risk_level != "LOW"), None)
        if first and first.ts < micro[0]:
            detected += 1
            leads.append(micro[0] - first.ts)
        crit = next((a for a in out if a.risk_level == "CRITICAL"), None)
        rows.setdefault("critical_delay_after_first_microsleep_s", []).append(None if not crit else crit.ts - micro[0])
        ch = sum(1 for x, y in zip(out, out[1:]) if x.risk_level != y.risk_level)
        flips.append(ch / (out[-1].ts / 60))
    rows["microsleep_warned_in_advance"] = f"{detected}/{len(list(SEEDS))}"
    rows["lead_time_s_median"] = st.median(leads) if leads else None
    rows["lead_time_s_min"] = min(leads) if leads else None
    cd = [x for x in rows["critical_delay_after_first_microsleep_s"] if x is not None]
    rows["critical_delay_after_first_microsleep_s"] = st.median(cd) if cd else None
    rows["level_changes_per_min_drift"] = st.mean(flips)
    # 3. occlusion & phase expectations
    ok = n = 0
    for s in SEEDS:
        out, phases, _ = assessments("drift_to_microsleep", s)
        for ph in phases:
            lv = [LEVELS.index(a.risk_level) for a in out if ph.t0 <= a.ts <= ph.t1]
            n += 1
            ok += max(lv) <= LEVELS.index(ph.expected_max_level) and max(lv) >= LEVELS.index(ph.expected_min_level)
    rows["phase_level_agreement"] = ok / n
    unk = sum(any(a.risk_level == "UNKNOWN" for a in assessments("occlusion", s)[0]) for s in SEEDS)
    rows["occlusion_flagged_unknown"] = f"{unk}/{len(list(SEEDS))}"
    return rows


def sample_assessments(every_s=10.0):
    """MODERATE+ assessments sampled across scenarios/seeds for narrator evaluation."""
    picked = []
    for sc in ("drift_to_microsleep", "yawn_only", "occlusion", "night_low_light"):
        for s in list(SEEDS)[:3]:
            out, *_ = assessments(sc, s)
            last = -1e9
            for a in out:
                if a.ts - last >= every_s and (a.risk_level != "LOW" or a.early_drift or a.quality_notes):
                    picked.append((sc, s, a))
                    last = a.ts
    return picked


async def narrator_eval(use_ollama):
    items = sample_assessments()
    res = {"n": len(items)}
    rows = []
    # (a) deterministic template: must satisfy factual guard checks
    bad = 0
    for sc, s, a in items:
        r = validate(template_body(a), facts_view(a), max_words=200)
        bad += any(("not in facts" in v) or ("AU" in v and "not in" in v) or "forbidden" in v for v in r.violations)
    res["template_factual_violations"] = bad
    # (b) real LLM
    if use_ollama:
        llm = llm_from_env()      # honours NARRATIVE_LLM / OLLAMA_* / OPENAI_COMPAT_* env vars
        if llm is None:
            res["llm"] = "NARRATIVE_LLM=none - nothing to evaluate"
            return res, rows
        if hasattr(llm, "is_up") and not await llm.is_up():
            res["llm"] = f"NOT REACHABLE at {llm.base_url}"
            return res, rows
        svc = NarrativeService(llm, NarrativeConfig(first_token_timeout_s=30, total_timeout_s=60, max_retries=1))
        src, first_pass, lat, ttft, viol = {}, 0, [], [], {}
        for sc, s, a in items:
            if a.risk_level == "CRITICAL":
                continue
            ev = await svc.narrate(a)
            src[ev["source"]] = src.get(ev["source"], 0) + 1
            first_pass += ev["guard"]["passed"] and ev["guard"]["attempts"] == 1
            lat.append(ev["latency_ms"])
            if ev["first_token_ms"]:
                ttft.append(ev["first_token_ms"])
            for v in ev["guard"]["violations"]:
                k = v.split(" ")[0] + " " + v.split(" ")[1] if " " in v else v
                viol[k] = viol.get(k, 0) + 1
            rows.append([sc, s, a.ts_str, a.risk_level, ev["source"], ev["guard"]["attempts"], ev["text"], "", "", ""])
        n = max(1, sum(src.values()))
        pct = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else None
        res.update({"llm_backend": llm.name, "llm_evaluated": n, "sources": src,
                    "llm_first_attempt_pass_rate": first_pass / n,
                    "llm_final_accept_rate": src.get("llm", 0) / n,
                    "latency_ms_p50": pct(lat, .5), "latency_ms_p95": pct(lat, .95),
                    "first_token_ms_p50": pct(ttft, .5), "violation_types": viol})
    else:
        for sc, s, a in items[:40]:
            rows.append([sc, s, a.ts_str, a.risk_level, "template", 0, template_body(a), "", "", ""])
    return res, rows


def guard_mutation():
    import re
    base = [a for _, _, a in sample_assessments() if a.findings][:30]
    caught = total = 0
    for a in base:
        f = facts_view(a)
        good = template_body(a)
        for mut in (
            lambda t: re.sub(r"\d+%", "99%", t, count=1),
            lambda t: t + " The operator should stop immediately.",
            lambda t: t + " This pattern suggests a sleep disorder.",
            lambda t: t.replace(f"Risk is {a.risk_level}", "Risk is CRITICAL" if a.risk_level != "CRITICAL" else "Risk is LOW"),
            lambda t: t + " The pulse was elevated.",
            lambda t: t + " Brow raising (AU2) was also seen.",
        ):
            m = mut(good)
            if m == good:
                continue
            total += 1
            caught += not validate(m, f, max_words=500).passed
    return caught, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", "--ollama", dest="ollama", action="store_true", help="evaluate the configured real LLM")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    rm = rule_metrics()
    nr, rows = asyncio.run(narrator_eval(args.ollama))
    caught, total = guard_mutation()
    with open(os.path.join(OUT, "narratives_for_rating.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["scenario", "seed", "time", "risk_level", "source", "attempts", "narrative",
                    "clarity_1to5", "usefulness_1to5", "trust_1to5"])
        w.writerows(rows)
    L = ["# Narrative layer evaluation (synthetic telemetry)\n", "## Rule engine\n"]
    for k, v in rm.items():
        L.append(f"- **{k}**: {v if not isinstance(v, float) else round(v, 3)}")
    L += ["\n## Guard (hallucination detection)\n",
          f"- injected hallucinations caught: **{caught}/{total}** ({caught / max(1, total):.1%})",
          "  (wrong number, advice, medical claim, wrong risk level, ungrounded pulse, ungrounded AU)\n",
          "## Narrator\n"]
    for k, v in nr.items():
        L.append(f"- **{k}**: {v}")
    if not args.ollama:
        L.append("- real-LLM metrics not run (use --ollama with a local Ollama server)")
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
