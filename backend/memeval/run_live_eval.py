#!/usr/bin/env python3
"""
Live eval runner for Möbius memory-v2.
LLM path: echo "$PROMPT" | timeout 60s claude -p
Cache: sha256(prompt) -> response in memeval/llm_cache.json
"""
import hashlib, json, os, re, shlex, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CACHE_FILE = SCRIPT_DIR / "llm_cache.json"
RESULTS_DIR = SCRIPT_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# ── Cache ──────────────────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}

def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))

_cache = load_cache()

def llm(prompt: str, label="") -> str:
    key = hashlib.sha256(prompt.encode()).hexdigest()
    if key in _cache:
        return _cache[key]
    if label:
        print(f"  [LLM call: {label}]", flush=True)
    result = subprocess.run(
        ["bash", "-c", f"echo {shlex.quote(prompt)} | timeout 60s claude -p"],
        capture_output=True, text=True, timeout=90
    )
    response = result.stdout.strip()
    # Strip the session-init preamble Claude -p prepends
    # It usually ends with a blank line before the actual answer
    lines = response.split('\n')
    # Find the last non-empty block (the actual answer)
    # Claude -p often outputs preamble ending in blank line then answer
    # Heuristic: find the last paragraph
    paragraphs = [p.strip() for p in response.split('\n\n') if p.strip()]
    if len(paragraphs) > 1:
        # Take all paragraphs after the first (preamble)
        response = '\n\n'.join(paragraphs[1:])
    else:
        response = paragraphs[-1] if paragraphs else response
    _cache[key] = response
    save_cache(_cache)
    return response

# ── Normalization ──────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^\w\s]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def facts_from_response(text: str) -> list[str]:
    """Parse LLM list output into individual facts."""
    facts = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Strip list markers: "- ", "* ", "1. ", etc.
        line = re.sub(r'^[-*\d]+[.)]\s*', '', line).strip()
        if line:
            facts.append(line)
    return facts

def match_fact(gold: str, extracted_list: list[str]) -> bool:
    """Check if gold_fact is covered by any extracted fact (exact norm or LLM judge)."""
    gold_n = normalize(gold)
    for ext in extracted_list:
        if gold_n == normalize(ext):
            return True
        # Fuzzy: if 60% of gold words appear in ext
        gold_words = set(gold_n.split())
        ext_words = set(normalize(ext).split())
        if gold_words and len(gold_words & ext_words) / len(gold_words) >= 0.7:
            return True
    # LLM judge for remaining
    for ext in extracted_list:
        judge_prompt = (
            f"Do these two statements express the same fact? Answer only 'yes' or 'no'.\n"
            f"A: {gold}\n"
            f"B: {ext}"
        )
        ans = llm(judge_prompt, label=f"judge").lower()
        if 'yes' in ans[:20]:
            return True
    return False

def match_extracted_to_gold(ext: str, gold_list: list[str]) -> bool:
    """Check if an extracted fact matches any gold fact (for precision)."""
    ext_n = normalize(ext)
    for gold in gold_list:
        gold_n = normalize(gold)
        if ext_n == gold_n:
            return True
        gold_words = set(gold_n.split())
        ext_words = set(ext_n.split())
        if gold_words and len(gold_words & ext_words) / len(gold_words) >= 0.7:
            return True
    return False

# ── Capture Eval ──────────────────────────────────────────────────────────────

def run_capture_eval(cases: list[dict]) -> dict:
    print("\n=== CAPTURE EVAL ===", flush=True)
    results = []
    for i, case in enumerate(cases):
        cid = case.get('id', f'case-{i}')
        blind_spot = case['blind_spot']
        gold_facts = case['gold_facts']
        transcript = case['transcript']  # list of {role, text}
        
        # Format transcript for LLM
        transcript_text = '\n'.join(
            f"{m['role'].upper()}: {m['text']}" for m in transcript
        )
        
        extract_prompt = (
            "List the durable first-person facts about the user stated in this transcript, "
            "one per line; omit one-off/transient details.\n\n"
            f"Transcript:\n{transcript_text}"
        )
        
        print(f"  Case {i+1}/{len(cases)}: {cid} [{blind_spot}]", flush=True)
        response = llm(extract_prompt, label=f"extract-{cid}")
        extracted = facts_from_response(response)
        
        # Precision: how many extracted match gold
        if extracted:
            prec_hits = sum(1 for e in extracted if match_extracted_to_gold(e, gold_facts))
            precision = prec_hits / len(extracted)
        else:
            precision = 0.0
        
        # Recall: how many gold facts were captured
        recall_hits = sum(1 for g in gold_facts if match_fact(g, extracted))
        recall = recall_hits / len(gold_facts) if gold_facts else 1.0
        
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        
        results.append({
            'id': cid,
            'blind_spot': blind_spot,
            'precision': round(precision, 3),
            'recall': round(recall, 3),
            'f1': round(f1, 3),
            'gold_facts': gold_facts,
            'extracted': extracted,
            'num_gold': len(gold_facts),
            'recall_hits': recall_hits,
        })
        print(f"    P={precision:.2f} R={recall:.2f} F1={f1:.2f} (gold={len(gold_facts)}, extracted={len(extracted)})", flush=True)
    
    # Aggregate
    overall_precision = sum(r['precision'] for r in results) / len(results)
    overall_recall = sum(r['recall'] for r in results) / len(results)
    overall_f1 = sum(r['f1'] for r in results) / len(results)
    
    # Per blind_spot recall
    bs_groups = {}
    for r in results:
        bs = r['blind_spot']
        if bs not in bs_groups:
            bs_groups[bs] = []
        bs_groups[bs].append(r['recall'])
    bs_recall = {bs: round(sum(v)/len(v), 3) for bs, v in bs_groups.items()}
    
    return {
        'cases': results,
        'overall_precision': round(overall_precision, 3),
        'overall_recall': round(overall_recall, 3),
        'overall_f1': round(overall_f1, 3),
        'per_blind_spot_recall': bs_recall,
    }

# ── E2E Eval ──────────────────────────────────────────────────────────────────

def run_e2e_eval(cases: list[dict]) -> dict:
    print("\n=== E2E EVAL ===", flush=True)
    results = []
    
    for case in cases:
        cid = case['id']
        persona = case['persona']
        transcripts = case['transcripts']  # list of {id, messages}
        questions = case['questions']
        
        print(f"\n  Case: {cid} ({persona})", flush=True)
        
        # Normalize the transcript shape. The fixture stores `transcripts` as a
        # FLAT list of {role, text} messages (one conversation). An older shape
        # used a list of {messages: [...]} objects. Handle both so the extractor
        # actually sees the dialogue (the flat shape silently yielded 0 notes).
        if transcripts and isinstance(transcripts[0], dict) and 'role' in transcripts[0]:
            convos = [transcripts]
        else:
            convos = [(t.get('messages') or t.get('transcript') or []) for t in transcripts]

        # Extract durable facts from each conversation (the V2 memory approach)
        all_notes = []
        for msgs in convos:
            transcript_text = '\n'.join(
                f"{m['role'].upper()}: {m['text']}" for m in msgs
            )
            if not transcript_text.strip():
                continue
            extract_prompt = (
                "List the durable first-person facts about the user stated in this transcript, "
                "one per line; omit one-off/transient details. Be specific.\n\n"
                f"Transcript:\n{transcript_text}"
            )
            response = llm(extract_prompt, label=f"e2e-extract-{cid}")
            notes = facts_from_response(response)
            all_notes.extend(notes)

        # Deduplicate notes
        seen = set()
        unique_notes = []
        for n in all_notes:
            nn = normalize(n)
            if nn not in seen:
                seen.add(nn)
                unique_notes.append(n)

        print(f"    Extracted {len(unique_notes)} unique facts from {len(convos)} conversation(s)", flush=True)

        # Flat context for baselines
        flat_context = '\n'.join(
            f"TRANSCRIPT {i+1}:\n" + '\n'.join(
                f"{m['role'].upper()}: {m['text']}"
                for m in msgs
            )
            for i, msgs in enumerate(convos)
        )
        
        # Answer each question with 3 systems
        q_results = []
        for q in questions:
            qtext = q['text']
            gold = q['gold_answer']
            should_abstain = q['should_abstain']
            
            print(f"    Q: {qtext[:60]}", flush=True)
            
            # V2: use extracted notes
            if unique_notes:
                notes_context = '\n'.join(f"- {n}" for n in unique_notes)
                v2_prompt = (
                    f"Using these user facts:\n{notes_context}\n\n"
                    f"Answer this question about the user: {qtext}\n\n"
                    "If the facts don't contain relevant information, say 'I don't know / not stated'."
                )
            else:
                v2_prompt = (
                    f"Answer this question about the user: {qtext}\n\n"
                    "If you have no information, say 'I don't know / not stated'."
                )
            v2_answer = llm(v2_prompt, label=f"e2e-v2-{cid}")
            
            # NoMemory baseline
            no_mem_prompt = (
                f"Answer this question about the user: {qtext}\n\n"
                "You have no prior information about this user. "
                "If you don't know, say 'I don't know / not stated'."
            )
            no_mem_answer = llm(no_mem_prompt, label=f"e2e-nomem-{cid}")
            
            # FlatInbox baseline
            flat_prompt = (
                f"Based on this conversation history:\n{flat_context[:4000]}\n\n"
                f"Answer: {qtext}\n\n"
                "If the answer isn't in the history, say 'I don't know / not stated'."
            )
            flat_answer = llm(flat_prompt, label=f"e2e-flat-{cid}")
            
            # Score: did V2 get it right?
            def score_answer(answer: str, gold: str, should_abstain: bool) -> bool:
                ans_n = normalize(answer)
                if should_abstain:
                    return any(p in ans_n for p in ["don't know", "not stated", "no information", "abstain", "haven't", "not mentioned"])
                if not gold or gold == '(none stated)':
                    return True
                gold_n = normalize(gold)
                gold_words = set(gold_n.split())
                ans_words = set(ans_n.split())
                # Core words match
                stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'i', 'you', 'your', 'my', 'to', 'and', 'or', 'not', 'in', 'on'}
                key_words = gold_words - stopwords
                if key_words and len(key_words & ans_words) / len(key_words) >= 0.6:
                    return True
                return False
            
            v2_correct = score_answer(v2_answer, gold, should_abstain)
            nom_correct = score_answer(no_mem_answer, gold, should_abstain)
            flat_correct = score_answer(flat_answer, gold, should_abstain)
            
            q_results.append({
                'question': qtext,
                'gold_answer': gold,
                'should_abstain': should_abstain,
                'type': q.get('tests', ''),
                'v2_answer': v2_answer[:200],
                'v2_correct': v2_correct,
                'nomemory_answer': no_mem_answer[:200],
                'nomemory_correct': nom_correct,
                'flatinbox_answer': flat_answer[:200],
                'flatinbox_correct': flat_correct,
            })
            
            verdict = "✓" if v2_correct else "✗"
            print(f"      gold={gold[:30]!r} | V2={verdict} | ans={v2_answer[:50]!r}", flush=True)
        
        results.append({
            'id': cid,
            'persona': persona,
            'notes_extracted': len(unique_notes),
            'questions': q_results,
            'v2_accuracy': sum(1 for q in q_results if q['v2_correct']) / len(q_results),
            'nomemory_accuracy': sum(1 for q in q_results if q['nomemory_correct']) / len(q_results),
            'flatinbox_accuracy': sum(1 for q in q_results if q['flatinbox_correct']) / len(q_results),
        })
    
    return {'cases': results}

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    base = Path(__file__).parent
    
    # Load fixtures
    corpus_raw = json.loads((base / "fixtures/eval-corpus-real-sessions.json").read_text())
    e2e_raw = json.loads((base / "fixtures/eval-e2e-real-sessions.json").read_text())
    
    capture_cases = corpus_raw['capture_cases']
    e2e_cases = e2e_raw['e2e_cases']
    
    print(f"Loaded {len(capture_cases)} capture cases, {len(e2e_cases)} E2E cases")
    
    # Run evals
    capture_results = run_capture_eval(capture_cases)
    e2e_results = run_e2e_eval(e2e_cases)
    
    # Build full results
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    results = {
        'timestamp': timestamp,
        'llm_path': 'echo $PROMPT | claude -p',
        'capture': capture_results,
        'e2e': e2e_results,
    }
    
    out_path = base / f"results/run-{timestamp}.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved to: {out_path}")
    
    # ── PRINT REPORT ──────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("MEMORY-V2 EVAL RESULTS")
    print("="*60)
    
    print("\n## Capture Eval (18 CaptureCases)")
    print(f"LLM path: `echo $PROMPT | claude -p` (cache: {len(_cache)} entries)\n")
    
    cr = capture_results
    print("| Metric | Score |")
    print("|--------|-------|")
    print(f"| Precision (avg) | {cr['overall_precision']:.3f} |")
    print(f"| Recall (avg) | {cr['overall_recall']:.3f} |")
    print(f"| F1 (avg) | {cr['overall_f1']:.3f} |")
    
    print("\n### Per Blind-Spot Recall (KEY RESULT)")
    print("| Blind-Spot Type | N Cases | Recall |")
    print("|-----------------|---------|--------|")
    bs_counts = {}
    for c in cr['cases']:
        bs = c['blind_spot']
        bs_counts[bs] = bs_counts.get(bs, 0) + 1
    for bs, recall in sorted(cr['per_blind_spot_recall'].items()):
        n = bs_counts.get(bs, 0)
        print(f"| {bs} | {n} | {recall:.3f} |")
    
    print("\n## E2E Eval (2 Cases, 8 Questions)")
    print("| Case | System | Accuracy |")
    print("|------|--------|----------|")
    for case in e2e_results['cases']:
        cid = case['id']
        print(f"| {cid} | V2 (FactExtractor+Retrieval) | {case['v2_accuracy']:.2f} |")
        print(f"| {cid} | NoMemory baseline | {case['nomemory_accuracy']:.2f} |")
        print(f"| {cid} | FlatInbox baseline | {case['flatinbox_accuracy']:.2f} |")
    
    # Overall E2E accuracy
    all_q = [q for case in e2e_results['cases'] for q in case['questions']]
    v2_acc = sum(1 for q in all_q if q['v2_correct']) / len(all_q)
    nom_acc = sum(1 for q in all_q if q['nomemory_correct']) / len(all_q)
    flat_acc = sum(1 for q in all_q if q['flatinbox_correct']) / len(all_q)
    print(f"| **OVERALL** | **V2** | **{v2_acc:.2f}** |")
    print(f"| **OVERALL** | **NoMemory** | **{nom_acc:.2f}** |")
    print(f"| **OVERALL** | **FlatInbox** | **{flat_acc:.2f}** |")
    
    # SUPERSEDE verdict
    print("\n## Explicit Verdicts\n")
    for case in e2e_results['cases']:
        for q in case['questions']:
            if 'python' in q['gold_answer'].lower() or 'supersede' in q.get('type', '').lower():
                print(f"**SUPERSEDE TEST** (Case: {case['id']})")
                print(f"  Question: {q['question']}")
                print(f"  Gold answer: {q['gold_answer']}")
                print(f"  V2 answer: {q['v2_answer'][:200]}")
                print(f"  V2 correct: {'YES ✓' if q['v2_correct'] else 'NO ✗'}")
                print()
            if q['should_abstain']:
                print(f"**ABSTENTION TEST** (Case: {case['id']})")
                print(f"  Question: {q['question']}")
                print(f"  V2 answer: {q['v2_answer'][:200]}")
                print(f"  Abstained correctly: {'YES ✓' if q['v2_correct'] else 'NO ✗ (hallucinated)'}")
                print()
    
    print(f"\nCache entries: {len(_cache)}")
    print(f"Results file: {out_path}")

if __name__ == '__main__':
    main()
