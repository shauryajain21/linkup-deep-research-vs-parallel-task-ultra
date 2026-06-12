#!/usr/bin/env python3
import argparse, asyncio, glob, json, os, subprocess, statistics, time

import httpx

DATA = os.environ.get("INPUT", "data/queries.jsonl")
ANSWERS_DIR = "results/answers"
GRADES = "results/grades.jsonl"
RESULTS = "results/results.json"

JUDGE_MODEL = "claude-fable-5"
DIMS = ["accuracy", "completeness", "gtm_value", "specificity",
        "source_quality", "signal_to_noise", "conciseness"]

JUDGE_SYSTEM = """You are a GTM research quality evaluator. A sales team asked a question about a company
and received an answer synthesized from search results. The answer will be consumed inside a sales
dashboard, where a busy rep needs the information fast — NOT read as a long-form report. Score the
answer on 7 dimensions (0-5).

- accuracy: factually grounded and about the right company/entity?
- completeness: covers everything the question asked? (Judge COVERAGE of the asked facts, not length.)
- gtm_value: actionable for a sales/GTM professional?
- specificity: concrete details (numbers, names, products, dates) vs vague generalities?
- source_quality: are the cited sources authoritative and on-target rather than random pages?
- signal_to_noise: dense with relevant info, or padded with boilerplate, hedging, and filler?
- conciseness: as short as it can be while still complete, usable at a glance? Penalize bloat —
  long preambles, executive summaries that restate the body, length beyond what the facts require.

Calibration: 4.5-5 excellent, 3.5-4 good, 2.5-3 basic/vague, 0-2 wrong/empty.
Return ONLY valid JSON: {"accuracy":<n>,"completeness":<n>,"gtm_value":<n>,"specificity":<n>,"source_quality":<n>,"signal_to_noise":<n>,"conciseness":<n>,"reason":"<one sentence>"}"""


def load_queries():
    return [json.loads(l) for l in open(DATA) if l.strip()]


def answer_path(provider, qid):
    return f"{ANSWERS_DIR}/{provider}_{qid}.json"


async def _retry_get(client, url, headers, ok_408=False):
    while True:
        try:
            r = await client.get(url, headers=headers, params={"timeout": 600} if ok_408 else None,
                                  timeout=660 if ok_408 else 60)
            if r.status_code in (429,) or (ok_408 and r.status_code == 408):
                if r.status_code == 429:
                    await asyncio.sleep(10)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            await asyncio.sleep(5)


async def run_linkup(client, q, sem, depth):
    key = os.environ["LINKUP_API_KEY"]
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    rec = {"id": q["id"], "provider": "linkup", "query": q["query"], "status": "failed",
           "answer": None, "sources": None, "latency_s": None}
    t0 = time.monotonic()
    async with sem:
        try:
            rid = None
            for a in range(7):
                r = await client.post("https://api.linkup.so/v1/research", headers=hdr,
                    json={"q": q["query"], "reasoningDepth": depth, "mode": "auto",
                          "outputType": "sourcedAnswer"}, timeout=60)
                if r.status_code == 429:
                    await asyncio.sleep(min(60, 5 * (a + 1))); continue
                r.raise_for_status(); rid = r.json()["id"]; break
            deadline = time.monotonic() + 2400
            while time.monotonic() < deadline:
                await asyncio.sleep(8)
                d = await _retry_get(client, f"https://api.linkup.so/v1/research/{rid}", hdr)
                if d.get("status") == "completed":
                    o = d.get("output") or {}
                    rec.update(status="completed", answer=o.get("answer"), sources=o.get("sources"))
                    break
                if d.get("status") == "failed":
                    break
        except Exception as e:
            rec["error"] = repr(e)
    rec["latency_s"] = round(time.monotonic() - t0, 1)
    json.dump(rec, open(answer_path("linkup", q["id"]), "w"), indent=2)
    print(f"[linkup {q['id']}] {rec['status']} {rec['latency_s']}s", flush=True)


async def run_parallel(client, q, sem, processor, key):
    hdr = {"x-api-key": key, "Content-Type": "application/json"}
    rec = {"id": q["id"], "provider": "parallel", "query": q["query"], "status": "failed",
           "answer": None, "basis": None, "latency_s": None}
    t0 = time.monotonic()
    async with sem:
        try:
            r = await client.post("https://api.parallel.ai/v1/tasks/runs", headers=hdr,
                json={"input": q["query"], "processor": processor}, timeout=60)
            r.raise_for_status(); rid = r.json()["run_id"]
            d = await _retry_get(client, f"https://api.parallel.ai/v1/tasks/runs/{rid}/result",
                                 hdr, ok_408=True)
            rec["status"] = d["run"]["status"]
            o = d.get("output") or {}
            c = o.get("content")
            rec["answer"] = c if isinstance(c, str) else json.dumps(c)
            rec["basis"] = o.get("basis")
        except Exception as e:
            rec["error"] = repr(e)
    rec["latency_s"] = round(time.monotonic() - t0, 1)
    json.dump(rec, open(answer_path("parallel", q["id"]), "w"), indent=2)
    print(f"[parallel {q['id']}] {rec['status']} {rec['latency_s']}s", flush=True)


async def cmd_run(args):
    os.makedirs(ANSWERS_DIR, exist_ok=True)
    queries = [q for q in load_queries() if not os.path.exists(answer_path(args.provider, q["id"]))]
    print(f"{args.provider}: {len(queries)} to run ({args.provider} resumable)", flush=True)
    if not queries:
        return
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        if args.provider == "linkup":
            await asyncio.gather(*[run_linkup(client, q, sem, args.depth) for q in queries])
        else:
            keys = [k.strip() for k in os.environ["PARALLEL_API_KEYS"].split(",") if k.strip()]
            await asyncio.gather(*[run_parallel(client, q, sem, args.processor, keys[i % len(keys)])
                                   for i, q in enumerate(queries)])


def judge_one(query, answer):
    if not answer or len(answer) < 20:
        return {d: 0 for d in DIMS} | {"reason": "empty"}
    prompt = f"Question: {query}\n\nAnswer: {answer}"
    err = "no attempts"
    for attempt in range(8):
        try:
            r = subprocess.run(["claude", "-p", "--model", JUDGE_MODEL, "--system-prompt", JUDGE_SYSTEM,
                                "--exclude-dynamic-system-prompt-sections"],
                               input=prompt, capture_output=True, text=True, timeout=300)
            raw = r.stdout.strip()
            if not raw:
                err = "empty stdout"; time.sleep(min(60, 8 * (attempt + 1))); continue
            if "```" in raw:
                for p in raw.split("```"):
                    p = p.strip().lstrip("json").strip()
                    if p.startswith("{"):
                        raw = p; break
            d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            return {k: float(d.get(k, 0)) for k in DIMS} | {"reason": str(d.get("reason", ""))}
        except Exception as e:
            err = repr(e); time.sleep(min(60, 5 * (attempt + 1)))
    return {d: 0 for d in DIMS} | {"reason": f"judge failed: {err}"}


def cmd_judge(args):
    done = {}
    if os.path.exists(GRADES):
        for line in open(GRADES):
            if line.strip():
                r = json.loads(line); done[(r["id"], r["provider"])] = r
    cf = open(GRADES, "a")
    for path in sorted(glob.glob(f"{ANSWERS_DIR}/*.json")):
        rec = json.load(open(path))
        if rec.get("status") != "completed" or not rec.get("answer"):
            continue
        if (rec["id"], rec["provider"]) in done:
            continue
        g = judge_one(rec["query"], rec["answer"])
        total = round(sum(g[d] for d in DIMS) / len(DIMS), 2)
        row = {"id": rec["id"], "provider": rec["provider"], "grade": g, "total": total}
        done[(rec["id"], rec["provider"])] = row
        cf.write(json.dumps(row) + "\n"); cf.flush()
        print(f"{rec['id']:6} {rec['provider']:9} total={total}  {g['reason'][:70]}", flush=True)
    cf.close()
    json.dump(list(done.values()), open(RESULTS, "w"), indent=2)
    print(f"saved {len(done)} grades -> {RESULTS}")


def cmd_report(args):
    rows = json.load(open(RESULTS))
    by = {}
    for r in rows:
        by.setdefault(r["id"], {})[r["provider"]] = r
    paired = [q for q in by if "linkup" in by[q] and "parallel" in by[q]]
    n = len(paired)
    lk = [by[q]["linkup"]["total"] for q in paired]
    pl = [by[q]["parallel"]["total"] for q in paired]
    print(f"\nScored queries: {n}")
    print(f"Overall   Linkup {statistics.mean(lk):.2f} (med {statistics.median(lk):.1f})   "
          f"Parallel {statistics.mean(pl):.2f} (med {statistics.median(pl):.1f})\n")
    print(f"{'dimension':16}{'Linkup':>9}{'Parallel':>10}")
    for d in DIMS:
        l = statistics.mean(by[q]["linkup"]["grade"][d] for q in paired)
        p = statistics.mean(by[q]["parallel"]["grade"][d] for q in paired)
        print(f"{d:16}{l:>9.2f}{p:>10.2f}")


def main():
    ap = argparse.ArgumentParser(description="Linkup vs Parallel deep-research GTM benchmark")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a provider over the query set")
    r.add_argument("provider", choices=["linkup", "parallel"])
    r.add_argument("--depth", default="S", help="Linkup reasoning depth (S/M/L/XL)")
    r.add_argument("--processor", default="ultra", help="Parallel processor (base/core/pro/ultra/...)")
    r.add_argument("--concurrency", type=int, default=5)
    sub.add_parser("judge", help="score all collected answers with Claude Fable 5")
    sub.add_parser("report", help="print the Linkup-vs-Parallel comparison")
    args = ap.parse_args()
    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    elif args.cmd == "judge":
        cmd_judge(args)
    elif args.cmd == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
