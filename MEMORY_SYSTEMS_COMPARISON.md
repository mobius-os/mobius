# Memory systems diverge on who writes, and when

*State of the field as of 18 November 2026. Möbius facts come from reading the Memory app v2.5.0 and platform source code.*

**Contents**

- [What a memory system is for, and how the field works](#what-a-memory-system-is-for-and-how-the-field-works)
- [Each stage of memory, system by system](#each-stage-of-memory-system-by-system)
  - [Store](#store)
  - [Write: who decides what to save, and when](#write-who-decides-what-to-save-and-when)
  - [Update: replacing, retiring and forgetting](#update-replacing-retiring-and-forgetting)
  - [Decide to recall: most systems remove the decision](#decide-to-recall-most-systems-remove-the-decision)
  - [Retrieve: finding the right memory](#retrieve-finding-the-right-memory)
  - [Use: applying what was retrieved](#use-applying-what-was-retrieved)
  - [Summary by system](#summary-by-system)
- [Coding harnesses load small files into every prompt rather than retrieve](#coding-harnesses-load-small-files-into-every-prompt-rather-than-retrieve)
- [Published scores move 16–22 points with whoever runs them](#published-scores-move-1622-points-with-whoever-runs-them)
  - [The main benchmarks in brief](#the-main-benchmarks-in-brief)
  - [Why the same system scores so differently](#why-the-same-system-scores-so-differently)
  - [At a glance: every published score, system by benchmark](#at-a-glance-every-published-score-system-by-benchmark)
  - [What the numbers mean](#what-the-numbers-mean)
  - [Which stages of memory each benchmark tests](#which-stages-of-memory-each-benchmark-tests)
  - [Tokens, latency and cost favour memory only past about ten turns](#tokens-latency-and-cost-favour-memory-only-past-about-ten-turns)
- [Personalization is memory applied to one user, and its benchmarks rarely test behaviour change](#personalization-is-memory-applied-to-one-user-and-its-benchmarks-rarely-test-behaviour-change)
- [Memory for self-improvement targets the optimizer, not the task](#memory-for-self-improvement-targets-the-optimizer-not-the-task)
- [Möbius is the only summary-fed, embedding-free, self-auditing graph](#möbius-is-the-only-summary-fed-embedding-free-self-auditing-graph)
  - [What Möbius could borrow, with trade-offs](#what-möbius-could-borrow-with-trade-offs)
- [Benchmarking Möbius needs the baselines memory vendors omit](#benchmarking-möbius-needs-the-baselines-memory-vendors-omit)
- [Conclusion](#conclusion)

## What a memory system is for, and how the field works

**Purpose.** A language model remembers nothing between sessions, and its context window holds only so much. A memory system keeps what should outlast a chat and brings it back when it matters: who the user is, what they prefer, decisions and their reasons, what happened last time. The aim is an agent that needs fewer corrections, repeats less work and spends fewer tokens than it would by re-reading the whole history.

**Components.** Every memory system has a store and five stages that act on it:
- **Store:** where memory lives. The options are markdown files, rows in a vector database (searched by embedding similarity), a knowledge graph of entities and facts, or structured profiles.
- **Write:** deciding what to save, and when.
- **Update:** replacing or retiring facts that changed, and forgetting.
- **Decide to recall:** noticing that memory is relevant. Either the agent decides, or the system injects memory automatically.
- **Retrieve:** finding the right items.
- **Use:** applying them in the answer or task.

A sixth stage, **outcome** (was the real task done better or cheaper?), is what memory is ultimately judged on. Benchmarks measure it; systems do not implement it.

**How Möbius works.**
- **Capture.** During a chat, the agent calls `remember` to append a short capture to an inbox file.
- **Nightly consolidation.** At 05:30 a consolidator files the captures into a git-versioned markdown graph. The consolidator is an LLM with no tools that sees each chat's title, Summary and captures but never the transcript. In the graph, `index.md` links to maps of content (MOCs, topic hub pages), which link to one-claim notes.
- **Admission rule.** Admits lasting facts: owner preferences, people, decisions with reasons, working style, and where things live. Excludes code knowledge, task progress and secrets.
- **Recall.** Nothing from the graph enters a chat automatically; only the 10 most recent chat Summaries do. To use the graph, the agent must call `search`, which has an LLM walk the graph without embeddings and takes minutes, and then `read`.
- **Self-tuning.** A nightly audit replays past lookups and rewrites a hint of at most 800 characters (`recall_guidance`) that steers that walk.

The Möbius section near the end gives details and caveats.

**How the others work: five trends.**
1. **Writing is moving to background consolidation.** The first memory layers (Mem0, Zep) have an LLM extract facts from every message. In 2026, Codex, ChatGPT, Copilot and Gemini CLI added passes over idle sessions, the design Möbius already uses. Letta and Claude Code let the agent edit its own files.
2. **Updating stale facts is the weakest stage and the least agreed on.** Zep timestamps old facts as invalid, Mem0 went append-only in 2026, and most coding harnesses do nothing. On independent tests of updating, most systems score low; the best, MemOS at 62% on HaluMem, comes from a benchmark whose authors may overlap with MemOS's maker.
3. **A small pinned index, with details read on demand.** Harnesses load a small markdown file into every prompt and let the agent open more. Few systems rely on the agent deciding unprompted to search, which is what Möbius does.
4. **Memory layers retrieve with hybrid search.** They combine embedding similarity, BM25 keyword ranking and graph search, merged by reciprocal rank fusion (RRF) and often reranked, in about 0.2–1.5 s.
5. **Vendor scores are unreplicated, and simple baselines are hard to beat.** One system's score on the LoCoMo benchmark spans 21.9 points depending on who ran it. On independent tests, long context, BM25 or plain files mostly tie with or beat the memory layers.

**Bottom line for Möbius.** It resembles Letta Code's git-backed markdown, but no other system combines a tool-less nightly writer, an embedding-free navigator that takes minutes, and a self-audit that tunes it. It has no accuracy numbers for v2.5.0, and its weak points (prompt-only supersession, minutes of latency, recall that depends on the agent choosing to search) are the field's weak points too.

Numbers in the text are tagged by who produced them: **[vendor]** the system's maker, **[rival]** a competing vendor, **[indep]** a party with no product in the comparison (author affiliations were not always checked), **[snippet]** seen only in a search-result summary.

## Each stage of memory, system by system

Storage does not visibly separate systems: on MemoryAgentBench (an independent benchmark, described in the benchmarks section), systems on very different stores (Mem0 21.1, Zep 24.0, Cognee 20.6, MemGPT 28.3) land within about 8 points of each other, and all fall below BM25 keyword search (41.5). They differ in more than storage, so this is suggestive, not proof. The larger design differences are in writing, updating and deciding to recall. A summary table follows the stage sections.

### Store

- **Vector rows.** Mem0 OSS v3 keeps a vector database plus entity links; graph memory is on the paid platform only. Hindsight uses Postgres with pgvector, A-Mem ChromaDB, and LangMem LangGraph's BaseStore.
- **Graphs.** Zep's Graphiti is a temporal knowledge graph. Cognee pairs an embedded graph store with vectors and can collapse both into one Postgres. MemOS's "MemCube" sits on Neo4j and Qdrant, and Supermemory calls itself a "temporal vector-graph engine".
- **Files.** Letta Code, the coding harnesses (Claude Code, Codex, Gemini CLI, Cline) and Möbius all store markdown. Letta Code and Möbius version theirs in git.
- **Typed or tiered stores.** Hindsight separates world facts, experiences, observations (consolidated beliefs with proof counts) and user-curated "mental models". MIRIX keeps six types: core, episodic, semantic, procedural, resource and a "knowledge vault" ([MIRIX](https://arxiv.org/html/2507.07957v1)). MemoryOS (BAI-LAB, EMNLP 2025, unrelated to MemTensor's MemOS) uses short-, mid- and long-term tiers ([MemoryOS](https://arxiv.org/html/2506.06326v1)). Memobase stores structured user profiles and event timelines instead of a graph ([i-programmer](https://i-programmer.info/news/105-artificial-intelligence/18295-memobase-add-memory-to-your-ai-agents.html)) [snippet]. MemoryOS and Memobase, like A-Mem, target conversation and personalization rather than agent work.

### Write: who decides what to save, and when

Three designs, plus hybrids.

**1. An LLM extracts facts as the conversation happens.**
- **Mem0.** Its 2025 paper used an extract-then-update pipeline in which an LLM chose ADD, UPDATE, DELETE or NOOP for each fact ([paper](https://arxiv.org/html/2504.19413)). In April 2026 Mem0 replaced it with a single **ADD-only** pass: "Memories accumulate; nothing is overwritten" ([README](https://raw.githubusercontent.com/mem0ai/mem0/main/README.md); [migration](https://raw.githubusercontent.com/mem0ai/mem0/main/docs/migration/oss-v2-to-v3.mdx)). Any comparison that still describes Mem0 as ADD/UPDATE/DELETE is describing the old system.
- **Zep / Graphiti.** An LLM turns each incoming episode into fact edges at ingest ([Zep paper](https://arxiv.org/html/2501.13956)).
- **Supermemory** writes automatically for each document.
- **MemOS** extracts automatically, and its plugins save every turn.
- **Hindsight's `retain`** has an LLM extract facts, times, entities and relationships ([paper](https://arxiv.org/html/2512.12818)).
- **Cognee 1.x** exposes remember, recall, improve and forget; `remember` runs an ingestion pipeline ([GitHub](https://github.com/topoteretes/cognee); [1.0 blog](https://www.cognee.ai/blog/deep-dives/inside-cognee-1-0)).
- **MIRIX** routes each write through one of six per-type manager agents.
- **A-Mem** has an LLM annotate each new note, link it to others and rewrite neighbouring notes ([paper](https://arxiv.org/html/2502.12110)).
- **Mastra's Observational Memory** compresses chat about 339× into notes, so only about 3% of a 10M-token history remains ([Mastra](https://mastra.ai/research/observational-memory-beam)) [vendor].

**2. The agent edits its own memory.**
- **Letta Code.** Its MemFS ("context repositories") is a git-backed tree of markdown files that the agent edits with bash and git. Reflection and defragmentation subagents work in git worktrees, and defragmentation targets "15–25 focused files" ([Letta blog](https://www.letta.com/blog/context-repositories); [GitHub](https://github.com/letta-ai/letta-code)). This is the closest public analogue to Möbius.
- **Claude Code's auto memory and the Anthropic API memory tool** work the same way (see the harness section).

**3. A background pass consolidates later.**
- **Layers.** This design is rare. Mem0's "Dream" (paid platform only; [pricing](https://mem0.ai/pricing)) and Supermemory's "Dreaming" exist, but neither is documented in detail. Cognee has a periodic `improve` step, and Hindsight forms "observations" in the background. LangMem describes both hot-path and background writing, but it has not been released since v0.0.30 (27 October 2025) ([guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)).
- **Harnesses.** In 2026, Codex, ChatGPT, Copilot and Gemini CLI all adopted this design (see the harness section).

**Möbius is a hybrid of designs 2 and 3.**
- The agent decides *what* is worth capturing (`remember`).
- A tool-less offline writer decides *how* it is filed, merged or dropped.
- Until the nightly run, a capture is not searchable.

**Code knowledge** splits the field:
- **Built without LLM calls:** Cognee builds a symbol and dependency graph of code.
- **Built from history:** Hindsight builds per-repository banks from git history and sessions.
- **Excluded by policy:** Möbius and Claude Code (see the harness section).

### Update: replacing, retiring and forgetting

**Layers that keep history and mark what is current:**
- **Zep's Graphiti** compares each new fact edge with related edges. When the two contradict, it sets the old edge's `t_invalid`. It keeps both event time and transaction time (**bi-temporal**: when the fact was true, and when the system learned it) ([Graphiti README](https://raw.githubusercontent.com/getzep/graphiti/main/README.md)).
- **Supermemory** does the same job without a schema, using `updates`/`extends`/`derives` relations and an `isLatest` flag; temporary facts expire ([graph docs](https://supermemory.ai/docs/concepts/graph-memory.md)). Its contradiction-resolution algorithm is undocumented.
- **Hindsight** preserves raw facts and deduplicates at 0.97 similarity. Within a day, the newest amendment wins. Since v1.2.1 it keeps only the three newest earlier-state notes per observation ([observations](https://hindsight.vectorize.io/developer/observations); [releases](https://github.com/vectorize-io/hindsight/releases)).
- **MemOS** has version-chain and TTL (time-to-live) fields on paper.

**Layers that overwrite or append:**
- **Mem0 (2026)** is append-only, so the retrieval ranking alone must surface the current fact.
- **A-Mem** overwrites neighbouring notes as it "evolves" them.
- **LangMem** lets an LLM insert, update or delete.

**Explicit erasure:**
- **Cognee** reviews conflicting facts, and its `forget(clean_history=True)` is the only documented history purge among the layers.

**Agent-edited files:**
- **Letta Code** relies on git merges and defragmentation, with no model of which fact supersedes which.

**Harnesses** mostly do nothing. The exceptions:
- **Copilot** expires memories after 90 days and re-checks each memory's code citation before use.
- **Codex** drops memories unused for 30 days (third-party report).
- **Gemini CLI** requires human approval for every memory.

**Möbius.**
- **Supersession.** The nightly writer is told to update a superseded claim in place and record `supersedes`, but only the prompt enforces this.
- **Forgetting** follows audited usefulness, not age.
- **History.** Git keeps every past version, including deleted chats' identifiers.

**Evidence:** this is the stage where systems fail most visibly.
- **HaluMem update accuracy** (whether stored facts are correctly changed; independence from MemTensor unconfirmed): Mem0 25.5%, Zep 47.3%, MemOS 62.1%. Mem0 skipped 74% of required updates.
- **STALE** (whether a system notices a memory has become invalid): every system is near the floor (best 17.8, against 8.7 for a plain model).

### Decide to recall: most systems remove the decision

A memory only helps if it reaches the prompt when it matters. Möbius leaves that to the agent: it must decide to call `search`, guided by cues in its system prompt. Few other systems rely on that decision alone. They use four patterns, often combined.

| Pattern | Examples | Fires when | Cost | Weakness |
|---|---|---|---|---|
| **1. Pinned in every prompt** | Claude Code (first 200 lines or 25 KB of `MEMORY.md`, plus CLAUDE.md files above the working directory); Letta Code (the full memory file tree with per-file descriptions, plus everything in `system/`); Supermemory (user profile at session start); Copilot (most recent repository memories at session start, count undocumented); Cline, Kiro and Windsurf "always on" rules | Always | Fixed tokens on every request; cheap with prompt caching if small | Must stay small; always-loaded context can raise cost without helping (the AGENTS.md study, [arXiv 2602.11988](https://arxiv.org/abs/2602.11988), detailed in the harness section) |
| **2. Retrieved on every prompt by a hook or wrapper** | Cognee's Claude Code plugin (one graph search per prompt, up to 9,000 characters injected, 20 s local / 45 s cloud time budget); MemOS plugins ("recall before each run"); Hindsight's wrappers around each model call; Mem0 and Zep as typically wired by applications | Every prompt | Retrieval latency on every prompt, plus injected tokens that accumulate in the history | Pays even when nothing relevant exists; needs fast retrieval |
| **3. Triggered by context** | Claude Code (a subdirectory CLAUDE.md loads when the agent touches a file there); Cognee (a hook before each file read injects that file's code-graph context); Windsurf and Kiro (rules that load on a file glob); Windsurf `model_decision` (the model sees only each rule's description and chooses); Devin Knowledge (loads "when its current work is related to the specified triggers") | When the agent touches a matching file or topic | Low and targeted | Needs a reliable trigger; file paths work for code, but personal questions touch no file |
| **4. Agent-initiated tool call** | **Möbius** (`search` and `read`); Letta (files outside `system/`); the Anthropic API memory tool (`view`); the Mem0, Zep and Cognee Model Context Protocol (MCP) servers | When the agent decides to look | Only when used | Silent misses: if the agent doesn't search, nothing records that it should have |

**The dominant design is a small pinned index plus details read on demand** (patterns 1 and 4 together). The index acts as the cue: the agent sees that a note about, say, the budget exists, and opens it. Letta's always-visible file tree and Claude Code's `MEMORY.md` of pointers both work this way. Even systems built around a memory tool hedge against the agent forgetting to call it. The Anthropic API memory tool adds a system-prompt instruction, "ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE", and the Mem0 and Cognee coding plugins add hooks on top of their MCP tools "so capture happens automatically".

**Cost of per-prompt retrieval versus a pinned block.** Take a 20-turn chat and cache pricing where writing to the cache costs 1.25× the normal input price and reading from it 0.1×.
- **Per-prompt injection at Cognee's cap** (9,000 characters ≈ 2,250 tokens every turn):
  - New tokens: 20 × 2,250 = 45,000, which at 1.25× is 56,250.
  - Re-reads: injection k is re-read on the remaining 20 − k turns, so 2,250 × (19 + 18 + … + 0) = 2,250 × 190 = 427,500 tokens, which at 0.1× is 42,750.
  - Total: **about 99,000 input-equivalent tokens per chat.**
- **A pinned 1,500-token block:** 1,500 × 1.25 + 19 × 1,500 × 0.1 ≈ **4,700**, about 20× less.

Real injections are often below the cap, so the first figure is a worst case.

**Evidence is thin.** I found no study that measures how often agents fail to call a memory tool. The nearest is Locomo-Plus, which removes the cue from LoCoMo questions so the system must notice by itself that a remembered fact applies: scores fall 41–46 points. The AGENTS.md study shows always-loaded context can add cost without improving success. VibeMemBench (an independent coding benchmark) shows that injecting verified history helps, but that the memory systems it tested mostly failed to deliver it in a usable form.

**Implications for Möbius.** Today Möbius pins only the 10 most recent chat Summaries, not even `index.md`, so a question that doesn't sound like a memory question gives the agent no reason to search. Three options follow from the patterns above:
1. **Pinned index or profile (platform change).** Inject a capped `index.md` or user profile, as Claude Code and Letta do. App prompt fragments are fixed per app version, so the Memory app cannot put a changing index into the system prompt by itself.
2. **Start-of-chat tool call (app-only).** Add a `profile` tool and an instruction to call it once at the start of each chat, copying the Anthropic memory tool's approach. Its risk is the same: the agent may skip the call, so the benchmark should measure the call rate.
3. **Per-prompt hook with a fast first stage (platform or container change).** Claude Code supports prompt hooks, so a quick keyword (BM25) search over notes and captures could inject only strong matches, leaving the slow model-guided search to the agent. Möbius's current search takes minutes, so it cannot run on every prompt as is. Whether Codex supports an equivalent hook is unverified.

The benchmark can compare these directly: how often memory reaches the prompt when it should, and the cost per resolved task, for agent-initiated search only (today), a pinned index, a start-of-chat `profile` call and a per-prompt keyword hook.

### Retrieve: finding the right memory

**Memory layers mostly fuse several searches.**
- **Zep** combines cosine similarity, BM25 and graph search with RRF, then applies a cross-encoder reranker. It reports 189 ms median latency [vendor].
- **Cognee** fuses graph, vector and BM25 results with RRF.
- **Hindsight** fuses four channels with RRF (k=60), then reranks with a cross-encoder; since v1.1.0 a "decision model" also ranks results.
- **Supermemory** combines vector, full-text and graph search plus a user profile. It claims about 720 tokens per query and a profile call of about 50 ms [vendor].
- **Mem0 OSS v3** is simpler. It retrieves semantic candidates (default `top_k` 20), and BM25 and entity scores only re-rank them. On LoCoMo its 2026 pipeline averages 6,956 tokens per retrieval call with a top-200 budget [vendor].
- **MemOS** uses graph plus vector search, **A-Mem** top-k cosine similarity, and **LangMem** key, semantic and metadata lookups.
- **Memobase** claims retrieval under 80 ms [snippet].

**Harnesses mostly skip retrieval.** They load a pinned file, and the agent greps or opens the rest. Letta Code pins `system/` and the file tree. Copilot is the exception: it retrieves repository memories.

**Möbius** has `traverse()` use an LLM to walk `index.md` → MOCs → notes and prune siblings. There are no embeddings, and a lexical fallback runs only if the walk fails. The documented latency is "a few minutes", which at 2 minutes is about 170× slower than Mem0 and 635× slower than Zep (see the Möbius section).

**Evidence:**
- **Simple keyword search is a strong baseline.** On MemoryAgentBench, BM25 scored 41.5 against Mem0's 21.1.
- **Agentic search trades minutes of latency for accuracy.** On LongMemEval-V2, retrieval-augmented generation (RAG) scored 42.8% in 0.1 s, while a Codex agent searching files scored 69.9% in 177 s. The tokens and latency section has the figures.

### Use: applying what was retrieved

Most systems paste the retrieved text into the prompt and leave the model to apply it. A few do more:
- **Hindsight's `reflect`** reasons over the memory bank before answering. It checks mental models first, then observations, then raw facts, and verifies stale observations against the raw facts.
- **Copilot** re-checks each memory's code citation against the current branch at the moment of use.
- **Mem0's append-only store** pushes work onto this stage: the reader model must pick the current fact from several versions.
- **Möbius** returns notes the agent then opens with `read`.

**Evidence:** the clearest finding is that retrieved memory can make output worse.
- **VibeMemBench (coding):** 69% of failing pairings (160 of 231) were "form degradation". The injected memory made the patch worse; it was not a missing fact.
- **AGENTS.md study:** always-loaded context raised cost per resolved task without raising success.

### Summary by system

| System | Store | Who writes, when | When memory is retrieved | How it is retrieved | Stale or conflicting facts | Code memory | Licence / self-host | Built for |
|---|---|---|---|---|---|---|---|---|
| Mem0 (OSS v3) | Vector DB plus entity links; graph only on the paid platform | LLM on `add()`, one ADD-only pass | When the app or agent calls search (API or MCP tool); apps often call it before every response | Semantic candidates; BM25 and entity scores only re-rank; `top_k` 20 | Append-only; ranking must surface the current fact | None (plugins, hosted MCP server) | Apache-2.0; graph, profiles and on-prem are paid | Per-user personalization in apps |
| Zep / Graphiti | Temporal knowledge graph on a graph DB | LLM per episode at ingest | When the app calls search, typically before every response; MCP tool for agents | Cosine + BM25 + graph search, RRF, cross-encoder | Bi-temporal `t_invalid`; history kept | None | Graphiti Apache-2.0; Zep server cloud-only (Community Edition discontinued) | Enterprise agents with changing facts |
| Cognee 1.x | Embedded graph store (Ladybug) + LanceDB/pgvector vectors; can collapse into one Postgres | Pipeline (`remember`), periodic `improve` | Every prompt (Claude Code plugin hook); before each file read (code context); MCP tool | Graph/vector/BM25 fused by RRF | Fact-conflict review; `forget(clean_history=True)` | Symbol/dependency code graph built without LLM calls; Claude Code plugin injects ≤9,000 chars | Apache-2.0; Cloud optional | Knowledge-graph memory for agents and documents |
| Hindsight | Postgres + pgvector (or Oracle) | LLM `retain`; background observations | Every model call via wrappers; MCP `recall`/`reflect` tools | 4 channels, RRF k=60, cross-encoder | Raw facts kept; newest same-day amendment wins | Per-repo banks from git history and sessions | MIT | Agents that learn from their own experience |
| Supermemory | "Temporal vector-graph engine" | Automatic per document; background "Dreaming" | User profile at session start; otherwise when the app or agent queries | Vector + full-text + graph; profile | `updates` + `isLatest`; temporary facts expire | Plugins; SMFS filesystem | Repo MIT; self-hosting the full product needs the $399 Scale tier or Enterprise | Hosted memory API |
| Letta Code | Git-backed markdown (MemFS) | Agent via bash/git; reflection and defragmentation subagents | `system/` and file tree in every prompt; other files when the agent reads them | `system/` pinned; agent greps or reads the rest | Git merge; defragmentation; no supersession model | Repo memory via `/init` | Apache-2.0; Cloud by default, local backend available | Stateful coding agent |
| MemOS | MemCube on Neo4j + Qdrant (SQLite locally) | Automatic extraction; plugins save every turn | Before each run (plugins) | Graph + vector | Version chain and TTL (time-to-live) fields on paper | Skill memory | Apache-2.0; Cloud | Memory "operating system" for LLM apps |
| A-Mem | ChromaDB notes | LLM on each insert rewrites neighbours | When the application queries the library | Top-k cosine | Overwrites (evolution) | None | MIT library | Research |
| LangMem | LangGraph BaseStore | Hot path or background | When the agent calls its search tool, or the app queries | Key, semantic, metadata | LLM insert/update/delete | None | MIT; stale since Oct 2025 | LangGraph apps |
| **Möbius v2.5.0** | Git-versioned markdown graph: `index.md` → MOCs (maps of content) → atomic notes | Agent `remember` appends to an inbox; tool-less LLM consolidates nightly | Only when the agent calls `search`; the 10 latest chat Summaries are injected at chat start | LLM navigator walks the graph; no embeddings; lexical fallback | `supersedes` is a prompt instruction only; evidence-based forgetting; git keeps everything | Deliberately none | MIT; single owner, self-hosted | One person's lasting facts across chats |

The **goals differ more than the marketing suggests**. Mem0, Zep, Supermemory and MemOS are infrastructure for multi-tenant apps, priced per add, per retrieval or per ingested byte. Zep charges about 1 credit per 350 bytes and nothing for retrieval ([Zep pricing](https://www.getzep.com/pricing)). Hindsight and Letta aim at agents that improve from their own runs. Cognee alone builds a deterministic code graph. In the sources read, Mem0, Zep and LangMem publish no coding-task benchmark of their own. The only coding results come from independent suites ([VibeMemBench](https://arxiv.org/html/2609.23570v1); [CL-Bench](https://arxiv.org/html/2606.05661)), covered in the benchmarks section.

## Coding harnesses load small files into every prompt rather than retrieve

Harness memory is almost always **plain markdown loaded into the prompt**, with two variants: files the user writes (CLAUDE.md, AGENTS.md, GEMINI.md, Cursor Rules, Kiro steering) and notes the agent writes (Claude Code auto memory, Codex memories, Gemini Auto Memory, Windsurf Cascade memories). Only Copilot, Devin and ChatGPT keep memory on a vendor server. The 2026 trend is toward **background consolidation of idle sessions**. Codex runs a background job over chats that have been idle "long enough" ([Codex docs](https://learn.chatgpt.com/docs/customization/memories?surface=app)), about 6 h according to third parties ([Vaughan](https://codex.danielvaughan.com/2026/04/18/codex-built-in-memory-system-deep-dive/)). Gemini CLI mines sessions idle at least 3 h that contain at least 10 messages ([Gemini Auto Memory](https://geminicli.com/docs/cli/auto-memory/)). ChatGPT has "dreaming" ([ChannelLife](https://channellife.news/story/openai-rolls-out-chatgpt-memory-system-to-us-users), third-party). Copilot Dreaming has been in preview since 28 October 2026 ([changelog](https://github.blog/changelog/2026-10-28-github-intelligence-puts-your-code-context-and-history-to-work)). The vendors converged on the batch design Möbius already uses. Copilot's own January 2026 design had rejected offline curation as too costly ([GitHub blog](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)).

| Harness | Always loaded | Who writes | Approval / verification | Staleness handling | Status |
|---|---|---|---|---|---|
| Claude Code | CLAUDE.md files + first 200 lines or 25 KB of `MEMORY.md`; topic files on demand | Agent (auto memory); keeps only what "Claude can't derive from the code or git history" | None | None documented | On by default ([docs](https://code.claude.com/docs/en/memory)) |
| Claude API memory tool | Nothing; the injected prompt says "ALWAYS VIEW YOUR MEMORY DIRECTORY" | Model via `memory_20250818` tool; developer supplies storage | Developer's choice | Developer's choice | [docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) |
| Codex | AGENTS.md; memory files | Background extraction + consolidation | Secret redaction | `max_unused_days` 30 (third-party, [Vaughan](https://codex.danielvaughan.com/2026/04/08/codex-cli-memory-internals/)) | Off by default |
| ChatGPT | Saved memories + memory summary | Model + "dreaming" | User can edit | Curated by dreaming | [FAQ](https://help.openai.com/en/articles/8590148-memory-faq) [snippet] |
| Cursor | Rules only | User | n/a | n/a | Memories removed ([forum](https://forum.cursor.com/t/are-my-memories-gone/144057)) |
| Windsurf / Devin Desktop | Rules; Cascade memories "when relevant" | Agent | None | None | Migrating to Skills ([MemoryLake](https://www.memorylake.ai/en/blogs/devin-desktop-removed-cascade), third-party) |
| GitHub Copilot Memory | Retrieved repo memories | Agent, from contributors with write access | **Citations checked against the current branch at use**; voting | **90-day expiry** (7–180 configurable) | Generally available 28 Oct 2026 ([docs](https://docs.github.com/en/enterprise-cloud@latest/copilot/concepts/agents/copilot-memory)) |
| Cline Memory Bank | 6 files read at every task | Agent, following pasted instructions (a methodology, not a feature) | None | User triggers "update memory bank" | [docs](https://docs.cline.bot/features/memory-bank.md) |
| Gemini CLI | GEMINI.md | Background agent writes `.patch` proposals to an inbox | **Every memory needs human approval**; cannot edit GEMINI.md | "No artifacts unless the evidence is strong" | [docs](https://geminicli.com/docs/cli/auto-memory/) |
| Kiro / Amp | Steering files / AGENTS.md | User (Amp drafts on request) | n/a | n/a | [Kiro](https://kiro.dev/docs/steering/) [snippet]; [Amp](https://ampcode.com/docs/markdown/customize/agents-md) [snippet] |
| Devin Knowledge | Items fetched by trigger | Agent suggests; user approves | Approval | None | Moving to Skills ([docs](https://docs.devin.ai/product-guides/knowledge.md)) |

On code facts, Claude Code and Copilot take **opposite stances**. Claude Code refuses to store anything derivable from the code. Copilot stores code facts but anchors each to a code location and re-checks the citation before use. This is the only harness I found that checks whether a memory is still true, not merely whether it is old. Copilot is also the only harness with a controlled result. Its A/B test (a randomized with-versus-without-memory comparison) raised pull-request merge rate from 83% to 90%, which is **+7 points, or 90/83 = 1.084, i.e. +8.4% relative**, and raised positive review feedback from 75% to 77%, with p < 0.00001 ([GitHub blog](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)) [vendor]. The pinned-file approach has a fixed cost. Claude Code's 25 KB ceiling is about 25,000/4 ≈ **6,250 tokens per request**, while 200 lines at about 15 tokens per line is about 3,000 tokens. Whichever limit binds first sets the real figure. The best independent cost evidence is the AGENTS.md study ([arXiv 2602.11988](https://arxiv.org/abs/2602.11988)). Adding a context file raised the cost per resolved task for Sonnet from $1.30/0.599 = **$2.17** to $1.51/0.587 = **$2.57 (+18.6%)**, and for GPT-5.2 from $0.32/0.566 = $0.565 to $0.43/0.544 = $0.790 (+40%), with no gain in resolution rate. Steps rose only 1.6% while cost rose 34%, so the extra cost came from tokens read on every step, not from extra work.

## Published scores move 16–22 points with whoever runs them

### The main benchmarks in brief

**Mostly vendor-reported (each vendor runs its own setup):**
- **LoCoMo:** about 1,540 questions over synthetic chat histories of about 26k tokens. It is the most cited, and it has known wrong answers.
- **LongMemEval (LME):** 500 questions over chat histories. In the standard "S" setting there are about 115k tokens of history per question. The questions cover updated facts, timing, reasoning across sessions, and knowing when to abstain.
- **BEAM:** conversations of up to 10M tokens, scored against rubrics on 10 abilities. The 10M tier has only 10 conversations.
- **PersonaMem:** whether answers are personalised to a user's history. Personalization benchmarks get their own section below.

**Independent (one party runs every system through the same harness):**
- **MemoryAgentBench:** long texts are fed in chunks, then four skills are tested: exact recall, learning from examples, whole-text understanding, and forgetting overwritten facts.
- **HaluMem:** scores extraction, updating and question answering separately. This document uses its update-accuracy score.
- **STALE:** 1,200 queries testing whether a system notices that a stored memory has become invalid.
- **VibeMemBench:** coding. 111 targets from 90 real repositories, scored as the change in tests passed against memory switched off.
- **RECON:** 1,414 questions requiring reasoning across case files of 50–100k tokens.
- **MemPABench:** whether the agent picks the user's preferred setting.
- **CL-Bench:** six task families in which an agent must learn across episodes.

**Variants that change the setup:**
- **Locomo-Plus:** LoCoMo with the explicit cue removed, so the system must notice by itself that a remembered fact applies.
- **LongMemEval-V2:** an agent setting in which the agent searches files itself. It reports latency as well as accuracy.

The "What the numbers mean" table below gives each benchmark's metric, reference points and independence caveats.

### Why the same system scores so differently

On the two most-cited benchmarks, LoCoMo and LongMemEval (LME; LME-S is its standard setting), scores depend heavily on who runs them. For Zep, the correctly computed LoCoMo runs span 75.14 − 58.44 = 16.7 points from vendor to rival, and 21.9 points once the 80.32 vendor run is included. Three settings drive most of the spread: **the reader model, the judge, and the question categories**. On top of that, LoCoMo itself is weak. An audit found 99 of 1,540 gold answers wrong (6.4%), which caps a perfect system at about 93.57%. The same audit found the standard judge accepted 62.81% of deliberately wrong answers ([Penfield](https://penfieldlabs.substack.com/p/we-audited-locomo-64-of-the-answer) [snippet]; [repo](https://github.com/dial481/locomo-audit)). Penfield sells a memory product, so the audit is only "independent-ish". Any vendor LoCoMo score above about 93.6 therefore implies either a changed category set or a lenient judge. Locomo-Plus, which tests whether a system applies a remembered fact without being cued, cuts systems by 41–46 points; GPT-4o falls from 62.99 to 21.05 ([Locomo-Plus](https://arxiv.org/html/2602.10715)). On BEAM, swapping the judge alone moved Mastra from 82.4 to 71.5, a 10.9-point change with no change to the system ([Mastra](https://mastra.ai/research/observational-memory-beam)).

### At a glance: every published score, system by benchmark

Scores are percentages unless a column says otherwise; Cognee's BEAM score of 0.67 is shown as 67. Who ran each number:
- **v:** the vendor, on its own system.
- **r:** a rival vendor. **r-MT** marks MemTensor's single gpt-4o-mini harness, which also produced the MemOS row.
- **i:** an independent party.
- **s:** seen only in a search snippet, not on a primary page.

**Table 1: independent benchmarks.** In each column, one party with no product in the comparison ran every system through the same harness, model and judge, so rows can be compared directly.

| System | MemoryAgentBench | HaluMem update accuracy | STALE | VibeMemBench (points vs memory off) | RECON accuracy | MemPABench | CL-Bench (6 tasks) |
|---|---|---|---|---|---|---|---|
| Mem0 | 21.1 | 25.5 | 8.3 | −1.8 to −5.5 | 9.7 (graph variant 10.8) | 50.0 | 15.1 |
| Zep / Graphiti | 24.0 | 47.3 | 6.0 | – | – | – | – |
| Letta / MemGPT | 28.3 (MemGPT) | – | – | – | – | – | – |
| Cognee | 20.6 | – | – | – | – | – | – |
| Hindsight (Vectorize) | – | – | – | – | 10.2 | – | – |
| Supermemory | – | – | – | – | 15.1 | – | – |
| MemOS (MemTensor) | – | 62.1 | – | – | – | – | – |
| MIRIX | 26.2 (37.7 with gpt-4.1-mini) | – | – | – | – | – | – |
| A-Mem | – | – | 5.1 | −2.5 to −4.0 | – | – | – |
| SimpleMem | – | – | – | −0.2 to −1.8 | – | – | – |
| MemoryOS (not MemOS) | – | – | – | −3.8 to +2.0 | – | – | – |
| LightMem | – | – | 17.8 | – | – | – | – |
| ACE (playbook memory) | – | – | – | – | – | – | 4.6 |
| Claude Code | – | – | – | – | – | – | 19.0 |
| **Möbius Memory** | – | – | – | – | – | – | – |
| *Full context / plain model* | 42.2 (GPT-4o-mini); 60.6 (GPT-5-mini) | – | 8.7 | – | 22.4 (best long-context run) | – | 22.3 (in-context, Sonnet 4.6) |
| *BM25 keyword search* | 41.5 | – | – | – | – | – | – |
| *Plain file-based memory* | – | – | – | – | – | 59.2 | – |
| *No memory* | – | – | – | 0 (by definition) | – | 49.6 | – |
| *Verified past history (oracle)* | – | – | – | +1.1 to +4.5 | – | – | – |

**Table 2: benchmarks reported mainly by vendors.** Each number may use a different reader model, judge and question set, so rows can't be compared directly. The few independent numbers (i) come from one paper, arXiv 2603.04814, which tested a custom pipeline built on Mem0's framework, not the hosted product.

| System | LoCoMo | LongMemEval (S) | BEAM 10M | PersonaMem |
|---|---|---|---|---|
| Mem0 | 92.5 v (2026); 66.88 v (2025); 64.57 r-MT; 57.68 i | 94.4 v (2026); 66.4 r-MT; 49.0 i | 48.6 v (1M: 64.1 v) | 43.1 r-MT; 62.5 i (v2) |
| Zep / Graphiti | 80.32 v; 75.14 v; 65.99 r; 58.44 r; 59.22 r-MT | 71.2 v; 63.8 r-MT | – | 57.8 r-MT |
| Letta (filesystem agent) | 74.0 v (forced first search) | – | – | – |
| Cognee | 90.7–91.3 v (own question set) | – | 67 v | – |
| Hindsight (Vectorize) | 83.2–89.6 v | 83.6–91.4 v; 94.6 v (s) | 64.1 v (s) | – |
| Supermemory | 55.34 r-MT | 81.6–85.2 v; 58.4 r-MT | – | 53.9 r-MT |
| MemOS (MemTensor) | 75.80 v | 77.8 v | – | 61.2 v |
| MIRIX | 64.33 r-MT | 43.5 r-MT | – | – |
| A-Mem | 48.38 r | – | – | – |
| LangMem | 58.10 r | – | – | – |
| OpenAI memory | 52.90 r | – | – | – |
| Mastra Observational Memory | – | – | 66.4 v | – |
| **Möbius Memory** | – | – | – | – |
| *Full context* | 72.90 r (gpt-4o-mini); 92.85 i (GPT-5-mini) | 60.2 (gpt-4o); 82.4 i (GPT-5-mini); 39.0 v (20B model) | too long to run | 69.8 i (v2) |
| *Simple retrieval* | 60.97 r (best RAG) | – | 24.9 (RAG, s) | – |

Not shown: further 10M-tier BEAM claims from Exabase (68.0, s) and Honcho (40.6, s), and Supermemory's "97" on LongMemEval, which is a retrieval recall metric, not answer accuracy.

### What the numbers mean

| Benchmark | What it tests | What the number is | Reference points | Independence caveat |
|---|---|---|---|---|
| **MemoryAgentBench** | Long texts fed to the memory in chunks, then questions on four skills: exact recall, learning from examples, whole-text understanding, forgetting overwritten facts | An average of sub-task scores. It is not "% correct", so read it only against the baselines. | BM25 41.5; long context 42.2–60.6 | Academic (UC San Diego). One author may also be a MIRIX author (unverified). |
| **HaluMem update accuracy** | Whether the system correctly updates stored memories when facts change | % of required updates done correctly | No baseline row. Mem0 skipped 74% of updates. | Unclear whether the authors overlap with MemTensor, which makes MemOS |
| **STALE** | Whether the system notices a stored memory has become invalid; 1,200 queries | A score where everything is near the floor (best 17.8) | Plain GPT-4o-mini with no memory layer: 8.7 | Academic (Wuhan, CUHK, HKUST) |
| **VibeMemBench** | Coding: 111 targets from 90 real repositories; memory holds past work on the same repo | Change in % of tasks whose tests pass, against the same model with memory off | 0 = no help. Perfect history gives +1.1 to +4.5. | Affiliations not checked. Targets were picked because history helped, which favours the oracle. |
| **RECON** | Reasoning across 50–100k-token case files (criminal, medical, financial); 1,414 questions | % of answers judged correct. About 45% of questions got "I don't know". | Best long-context model 22.4 | Single author, not peer-reviewed |
| **MemPABench** | Whether the agent picks the user's preferred setting | % of correct preference choices | No memory 49.6, so about 50 is the floor | Academic, Oct 2026 |
| **CL-Bench** | Six task families where the agent must learn across episodes | Normalized reward, higher is better | Plain in-context learning 22.3 | Each system ran on a different model (Mem0 and ACE on GPT-5.4, others on Sonnet 4.6), so the system is confounded with the model |
| **LoCoMo** | Q&A over about 26k-token synthetic chat histories; about 1,540 questions | % of answers an LLM judge marks correct | Full context 72.9–92.9 depending on the model. Ceiling about 93.6, because 6.4% of answers are wrong. | Mostly vendor-run. The judge accepted 63% of deliberately wrong answers. |
| **LongMemEval (S)** | 500 questions over about 115k-token histories: updates, timing, multi-session, abstaining | % of answers judged correct | Full context 39–82 depending on the model | Mostly vendor-run; reader models differ a lot |
| **BEAM 10M** | 10M-token conversations, 10 abilities | Judged against rubrics | Plain RAG about 25. Full context can't run. | Vendor-run. Changing the judge alone moved one score 11 points. Only 10 conversations. |
| **PersonaMem** | Personalised answers given a user's history | % correct | Full context 69.8 (v2, independent) | Mostly MemTensor-run |

### Which stages of memory each benchmark tests

Only one of the scores above measures retrieval alone: Supermemory's "97", which is recall@20. Most of the rest test a **fixed question-answering pipeline**. The harness feeds a history into the system, asks a question, and an LLM judges the answer. That covers more than retrieval, but not an agent doing real work. The columns are the six stages defined at the top of this document.

| Benchmark | Write | Update | Decide to recall | Retrieve | Use | Outcome |
|---|---|---|---|---|---|---|
| Supermemory's "97" (recall@20) | | | | ✓ | | |
| LoCoMo, LongMemEval, BEAM, PersonaMem, RECON, MemoryAgentBench | partly¹ | partly² | ✗ (the question cues it) | ✓ | ✓ (answer only) | |
| HaluMem | ✓ | ✓ | | ✓ | ✓ | (each stage scored separately) |
| STALE | | ✓ | | | ✓ | |
| Locomo-Plus | partly¹ | | **✓** (no explicit cue) | ✓ | ✓ | |
| MemPABench | ? | | ? | ✓ | ✓ (picks a setting) | partly |
| VibeMemBench | ✗ (built from past work on the same repo) | | ✗ (harness injects it) | ✓ | ✓ | **✓** (tests pass) |
| CL-Bench | ✓ (the agent's own experience) | ✓ | depends on system | ✓ | ✓ | **✓** (task reward) |
| LongMemEval-V2 (agent setting) | | | ✓ (the agent searches files itself) | ✓ | ✓ | answer plus latency |

¹ The harness pushes the whole history into memory. The system chooses what to extract, but no agent ever chooses to save something during real work.
² LongMemEval and BEAM include questions about facts that changed, but they score only the final answer.

How MemPABench and CL-Bench handle writing and recall is not covered in the notes behind this document; the "?" and "depends" cells reflect that.

What this means:
- **The question-answering benchmarks remove the hardest step.** The question tells the system to remember, and retrieval is often forced; Letta's 74.0 on LoCoMo forced a search before every answer. Locomo-Plus removes the cue and scores fall 41–46 points. That is a measure of what the cued benchmarks leave out.
- **Almost none measure outcome or cost.** VibeMemBench and CL-Bench come closest, and both find memory layers rarely beat simple baselines. None reports cost per completed task.
- **Möbius is exposed exactly where these benchmarks look away.** Its recall is entirely agent-initiated, so "decide to recall" is its likeliest failure point. A cued benchmark that forces search would overstate it. No published benchmark found tests an agent that decides by itself to consult owner-curated memory across real tasks, with cost measured. The Ada benchmark is designed to fill that gap.

How to read a score:
1. **Check who ran it.** Same-date outside re-runs of vendor LoCoMo numbers come in 2–21 points lower.
2. **Compare to a baseline in the same column and harness.** A memory system that doesn't beat full context, BM25 or no memory has not shown value, whatever its absolute score.
3. **Check the reader model.** The same system moves 5–10 points with a stronger reader, and full context moves 39 → 82 on LongMemEval.
4. **Check the metric.** Retrieval recall (Supermemory's 97) and answer accuracy can't be ranked against each other.
5. **Allow for sample noise.** The standard error of a score p on n questions is √(p(1−p)/n). For example:
   - 200 questions at 70%: √(0.7 × 0.3 / 200) = √0.00105 ≈ 0.032, so about ±6.3 points at 95%.
   - 1,540 questions (LoCoMo): √(0.21 / 1,540) ≈ 0.012, so about ±2.3 points.
   - 20 questions (Cognee's BEAM tier) at 79%: about ±18 points.

The overall pattern from Table 1: on independent tests, simple baselines match or beat memory layers in almost every column. BM25 and long context lead on MemoryAgentBench, and long context leads on RECON. Plain files lead on MemPABench, where Mem0 is no better than no memory. In-context learning leads on CL-Bench. Memory off beats 11 of 12 pairings on VibeMemBench. The exceptions are MemOS on HaluMem updates, which has no baseline row, and LightMem on STALE.

Möbius has no published scores. Its only evaluation tested an older pipeline on 8 internal cases: 5/8 correct, against 7/8 for a flat inbox and 3/8 with no memory.

Every number above, with its source:

| System | Benchmark | Score | Who ran it | Settings / caveat | Source |
|---|---|---|---|---|---|
| Mem0 | LoCoMo | 66.88 ± 0.15 | [vendor] 2025 | Old ADD/UPDATE pipeline | [paper](https://arxiv.org/html/2504.19413) |
| Mem0 | LoCoMo | 92.5 (91.6 elsewhere) | [vendor] 2026 | Judge unnamed; top-200 retrieval; within about 1 point of the ~93.6 ceiling set by wrong gold answers | [docs](https://docs.mem0.ai/core-concepts/memory-evaluation); [page](https://mem0.ai/library/coding-agents/claude-code-memory) |
| Mem0 | LoCoMo / LME | 64.57 / 66.4 | [rival] MemTensor | MemOS harness | [MemOS v4](https://arxiv.org/html/2507.03724v4) |
| Mem0 | LoCoMo / LME | 57.68 / 49.00 | [indep] | Long-context GPT-5-mini: 92.85 / 82.40 | [2603.04814](https://arxiv.org/html/2603.04814v1) |
| Mem0 | MemoryAgentBench | 21.1 | [indep] | BM25 41.5; GPT-4o-mini long context 42.2 | [MemoryAgentBench](https://arxiv.org/html/2507.05257v4) |
| Mem0 / Zep / MemOS | HaluMem update accuracy | 25.50% / 47.28% / 62.11% | [indep] per notes | Mem0 omitted 74.02% of updates | [HaluMem](https://arxiv.org/html/2511.03506v3) |
| Mem0 / Zep / A-Mem | STALE | 8.3 / 6.0 / 5.1 | [indep] | Plain GPT-4o-mini 8.7; LightMem 17.8 | [STALE](https://arxiv.org/html/2605.06527) |
| Zep | LoCoMo | 75.14 ± 0.17; 80.32 ± 0.43 | [vendor] | 80.32 at 1,997 tokens and 189 ms median latency, gpt-4o-mini reader and judge; an earlier 84% was withdrawn | [blog](https://www.getzep.com/blog/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/); [post](https://www.getzep.com/blog/the-retrieval-tradeoff-what-50-experiments-taught-us-about-context-engineering/); [issue](https://github.com/getzep/zep-papers/issues/5) |
| Zep | LoCoMo | 65.99 / 58.44 / 59.22 | [rival] Mem0 paper / Mem0 CTO / MemTensor | Rival configuration | [Mem0 paper](https://arxiv.org/html/2504.19413); [MemOS v4](https://arxiv.org/html/2507.03724v4) |
| Zep | LME-S | 71.2 | [vendor] | GPT-4o reader | [paper](https://arxiv.org/html/2501.13956v1) |
| Letta | LoCoMo | 74.0 | [vendor] | Agent forced to call `search_files`; judge unstated | [blog](https://www.letta.com/blog/benchmarking-ai-agent-memory) |
| Cognee | LoCoMo; BEAM | 90.7 / 91.3; 0.79 @100K, 0.67 @10M | [vendor] | LoCoMo on a 1,762-question "audited" set, judge and exclusions not retrieved, two numbers unexplained; BEAM 100K tier is 20 questions | [benchmarks](https://www.cognee.ai/ai-memory-benchmarks) |
| Cognee / MemGPT / MIRIX | MemoryAgentBench | 20.6 / 28.3 / 26.2–37.7 | [indep] | Same harness as Mem0 above; MIRIX may share an author (Yu Wang) with the benchmark, unverified | [MemoryAgentBench](https://arxiv.org/html/2507.05257v4) |
| Hindsight | LME-S | 83.6 / 89.0 / 91.4 | [vendor] | GPT-OSS-120B judge; full context with the 20B model scored only 39.0; competitor rows copied, not re-run | [paper](https://arxiv.org/html/2512.12818) |
| Hindsight | LME | 94.6 | [vendor] [snippet] | "Independently reproduced" by co-authors | [benchmarks](https://vectorize.io/benchmarks) |
| Supermemory | LME-S | 81.6 / 84.6 / 85.2 | [vendor], copied by a rival | Answer accuracy, GPT-4o judge | [Hindsight paper](https://arxiv.org/html/2512.12818) |
| Supermemory | LME-S | 97 | [vendor] | **Recall@20 with aggregation**, a retrieval metric, not accuracy | [research page](https://supermemory.ai/research/longmembench.md) |
| Supermemory / Hindsight | RECON | 15.1% / 10.2% | [indep] | Best long-context model 22.4%; ~45% abstention | [RECON](https://arxiv.org/html/2607.16716v1) |
| MemOS | LoCoMo / LME | 75.80 / 77.8 | [vendor] | No full-context row | [paper](https://arxiv.org/html/2507.03724) |
| A-Mem / LangMem / OpenAI memory | LoCoMo | 48.38 / 58.10 / 52.90 | [rival] Mem0 | Full context 72.90 in the same table | [Mem0 paper](https://arxiv.org/html/2504.19413) |
| Mem0, A-Mem, MemoryOS, SimpleMem | VibeMemBench (coding) | Mem0 −1.8 to −5.5 vs memory-off | [indep] | 11 of 12 system-solver pairings did not beat memory-off; oracle history +1.1 to +4.5 | [VibeMemBench](https://arxiv.org/html/2609.23570v1) |

The **Zep spread** shows the problem most clearly. Its lowest rival run is 58.44 and its best vendor run is 80.32, so 80.32 − 58.44 = **21.9 points** for one system on one benchmark. A Mem0 page lists a "claimed" 94.7 for Zep ([Mem0 blog](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)), while another Mem0 page says 80.32 ([Mem0 blog](https://mem0.ai/blog/state-of-ai-agent-memory-2026)). I found no primary Zep source for 94.7. Mem0's own score rose from 66.88 to 92.5 between 2025 and 2026. No outside party has run the April 2026 ADD-only pipeline; the independent 57.68 (March 2026) tested the earlier one. Small samples add noise as well. Cognee's 0.79 BEAM score rests on n = 20 questions. Its standard error is √(0.79 × 0.21 / 20) = √0.0083 ≈ **0.091**, i.e. about ±0.18 at 95%, so the claimed lead over the earlier 0.73 is noise.

When the history fits in the window, **full context or simple search generally matches or beats memory layers**. Examples: 92.85 versus 57.68 on LoCoMo, BM25 41.5 versus Mem0 21.1, and plain GPT-4o-mini at or above three layers on STALE. There are three exceptions. First, MemOS led HaluMem on updates. My notes label HaluMem independent, but they do not settle whether its authors overlap with MemOS's maker, MemTensor, so treat that lead as unconfirmed. Second, on CL-Bench's coding sub-task, ACE (11.58) and Mem0 (11.11 at $2.6) beat in-context learning (9.76 at $6.9), although each system ran on a different model, so the result is confounded ([CL-Bench](https://arxiv.org/html/2606.05661)). Third, Ground Truth First found that rankings reversed between 3 and 9 weeks of history (p = 0.031), with full history losing its advantage at the longer horizon ([Ground Truth First](https://arxiv.org/abs/2607.21962)). VibeMemBench's failure analysis is the most relevant to coding: 160 of 231 failing pairings (69%) were **form degradation**, where injected memory made the patch worse rather than missing a fact, and 42% of pairings were already at ceiling.

### Tokens, latency and cost favour memory only past about ten turns

Mem0's 2025 paper reported tokens per query and latency at the 50th and 95th percentiles (p50/p95) ([paper](https://arxiv.org/html/2504.19413)) [rival for every system except Mem0]. Full context used 26,031 tokens at 9.87/17.12 s. Mem0 used 1,764 tokens at 0.71/1.44 s, and Zep 3,911 tokens at 1.29/2.93 s. A-Mem used 2,520 tokens at 1.41/4.37 s. LangMem used only 127 tokens but took 18.5/60.4 s. Mem0's 2026 pipeline averages 6,956 tokens per retrieval call on LoCoMo with a top-200 budget ([docs](https://docs.mem0.ai/core-concepts/memory-evaluation)) [vendor]. That is 6,956/26,031 = **26.7% of full context (a 73.3% saving)**, but 6,956/1,764 = **3.9× its own 2025 figure**, so part of the higher score was bought with more context. Supermemory claims about 720 tokens per query and a profile call of about 50 ms ([README](https://github.com/supermemoryai/supermemory)) [vendor]. RECON measured 6.4K tokens per question for Supermemory and 8.4K for Hindsight on its own workload ([RECON](https://arxiv.org/html/2607.16716v1)) [indep]. Building memory is slow. MemoryAgentBench timed Mem0's construction at 2,946 s ≈ 49 min with 4,096-token chunks, and 18,483 s ≈ 5.1 h with 512-token chunks, where Cognee took 4,728 s and BM25 0.09 s ([MemoryAgentBench](https://arxiv.org/html/2507.05257v4)) [indep]. That table's header was garbled, so the column mapping is uncertain.

The **cost break-even** comes from an independent cost model on LongMemEval ([arXiv 2603.04814](https://arxiv.org/html/2603.04814v1)). Memory costs $0.0435 per conversation to write (with GPT-5-nano) and $0.0013 per read. Uncached long-context GPT-5-mini costs $0.0293 per query, which is 0.0293/0.0013 ≈ 22.5× more. Without caching, each read saves $0.0293 − $0.0013 = $0.0280, so the write cost is repaid after $0.0435 / $0.0280 ≈ **1.6 queries**. Prompt caching changes the answer. The paper's long-context totals at 100k are $0.0588 at 10 turns and $0.0947 at 20 turns. That is a marginal $0.0036 per turn, only 2.8× a memory read, so most of the cost sits in the first, cache-writing turn. I read these as the 10- and 20-turn totals because the arithmetic below reproduces the paper's conclusions. Memory then totals $0.0435 + 10 × $0.0013 = $0.0565 at 10 turns, just under $0.0588, hence the paper's **break-even of about 10 turns at 100k** (13 at 30k, 9 at 200k–500k). At 20 turns memory totals $0.0435 + 20 × $0.0013 = $0.0695, and 0.0695/0.0947 = 0.73, so memory is **about 26% cheaper**. For a harness that keeps a warm cache, the token argument for memory is therefore weak in short sessions. The main gains are across sessions and once histories outgrow the window. LongMemEval-V2 shows the latency side of the same trade: retrieval-augmented generation (RAG) scored 42.8% at 0.1 s, a Codex agent 69.9% at 177 s, and AgentRunbook-C, a file-based coding agent, 74.9% at 108 s ([LongMemEval-V2](https://xiaowu0162.github.io/longmemeval-v2/)) [indep]. Agentic search buys accuracy with minutes of latency, which is exactly Möbius's trade.

## Personalization is memory applied to one user, and its benchmarks rarely test behaviour change

**How personalization relates to memory.** Personalization means the agent behaves the way *this* user wants: the right tone and defaults, the right people and places, and no repeated corrections. Memory is the main way to do this without retraining the model. There are three routes:
- **Always in the prompt:** a profile or preference file attached to every request. Examples are ChatGPT's saved memories and summary, Supermemory's user profile, CLAUDE.md and Letta's `system/` files.
- **Retrieved when relevant:** preferences stored as memories and fetched for a query, as in Mem0, Zep and Möbius.
- **In the weights:** per-user fine-tuning or adapters. This document does not cover them. They are costlier to keep current, and harder to change or erase when a preference changes.

Personalization stresses the memory stages differently from fact recall:
- **Write.** Preferences are mostly implicit. Users rarely say "remember that I prefer X"; they react, correct, or quietly accept a choice. PersonaMem-v2 is built mostly from implicit preferences, and in MemPABench the simulated user expresses preferences only by reacting.
- **Update.** Preferences change, so the old one must stop applying. PersonaMem, PERMA and MemPABench (with scheduled change events) test this.
- **Decide to recall.** This is the hardest stage. "Book me a flight" does not sound like a memory question, yet a seat preference matters. PrefEval tests whether a stated preference is applied later without a reminder.
- **Use.** A preference must be applied in the right context and not elsewhere. CUPID tests context-specific preferences. MemPABench separately scores whether the setting is right in context.
- **Outcome.** The test is whether the user stops having to repeat themselves. TellOnce scores how often a corrected preference is violated again.

**Which systems focus on it.**

| Focus | Systems |
|---|---|
| **Mainly personalization** | **Mem0:** per-user memory for apps, keyed by user. **Supermemory:** hosted memory API with automatic user profiles. **Memobase:** structured profiles plus event timelines. **MemoryOS and A-Mem:** conversation and personalization research. **ChatGPT memory:** consumer product with saved memories and a profile summary. |
| **Partly** | **Zep:** user and business facts that change over time, for enterprise agents. **MemOS:** a general memory "operating system" that promotes its PersonaMem results. |
| **Not mainly** | **Hindsight and Letta:** agents learning from their own experience. **Cognee:** knowledge graphs over documents and code. **Coding harnesses** (Claude Code, Codex, Copilot): project and repository facts. |
| **Möbius** | **Personalization for one owner.** Its admission rule keeps preferences, people, working style, corrections and "quieter confirmations of a non-obvious choice", and excludes code knowledge. That puts it in Mem0's and Supermemory's category, but single-owner and self-hosted rather than multi-tenant. |

**The benchmarks fall into three groups.** Most numbers in this table were extracted by an automated page summarizer and not checked against the PDFs. Rows marked [A] were read from the arXiv abstract only.

| Benchmark | What it tests | Stage it stresses | Key result |
|---|---|---|---|
| ***1. Recall preferences from a prepared history (cued questions)*** | | | |
| [LaMP](https://arxiv.org/html/2304.11406) / [LongLaMP](https://arxiv.org/html/2407.11016) (2023–24) | Personalized writing from a user profile; real Amazon, MovieLens and email data | Use | Accuracy, MAE (mean absolute error) and ROUGE text-overlap metrics; no memory across sessions |
| [PersonaMem](https://arxiv.org/html/2504.14225) (2025) | Seven query types, including the latest preference and how preferences evolved; 32k–1M-token histories | Update, retrieve | Frontier models about 50% on four-option multiple choice |
| [PersonaMem-v2](https://arxiv.org/abs/2512.06688) (Dec 2025) [A] | Mostly implicit preferences; 20,000+ preferences over 128k tokens | Write, retrieve | Frontier models 37–48%; a 2k-token memory used 16× fewer input tokens than 32k of context |
| [PrefEval](https://arxiv.org/html/2502.09597) (ICLR 2025) | Applying a stated preference later without a reminder, with up to ~100k tokens of real filler | **Decide to recall**, use | The nearest public test of unprompted use |
| [CUPID](https://arxiv.org/abs/2508.01674) (2025) [A] | Context-specific preferences from earlier sessions; 756 human-curated histories | Retrieve, use | Precision under 50% and recall under 65% at finding the relevant earlier context |
| [PERMA](https://arxiv.org/html/2603.23231v2) (Mar 2026) | Preferences that build up over ordered events; up to 1.2M tokens | Update | One of the few to report context tokens per query |
| ***2. Personalization within a single task*** | | | |
| [PersonaLens](https://arxiv.org/html/2506.09902v1) (2025); [UserBench](https://arxiv.org/abs/2507.22034) (2025) | Whether task dialogues respect a user profile, using simulated users | Use | The LLM judge agrees with humans only moderately (Cohen's κ 0.52); no memory across sessions |
| ***3. Does learning in one session change behaviour in a fresh one? (2026, preprints)*** | | | |
| [MemPABench](https://arxiv.org/html/2610.30485v1) (Oct 2026) | Picking the user's preferred setting (when to confirm or act, what to disclose) across 99 sessions over 4 months with 5 preference changes | Write, update, use | Files 59.2%, Mem0 50.0%, no memory 49.6%, random 45.2% |
| [TellOnce](https://arxiv.org/abs/2606.13174) (Jun 2026) [A] | Whether a correction persists, from anonymized real agent interactions | Write, outcome | Scored as the rate of repeat violations with learned state frozen |
| [PAST-Bench](https://arxiv.org/html/2608.04003v1) (Aug 2026) | Whether retained experience helps in fresh sessions, with stale and distractor controls | Write, update, outcome | Small gains (+0.13 to +0.24), some within run-to-run noise; also checks that the intended files were written and read |
| [PraxisMemBench](https://arxiv.org/abs/2610.20022) (Oct 2026) [A] | Personal plus procedural memory over daily-life and work trajectories | Outcome | Claude Code's built-in memory 20.6% session success, against 40.8% for the authors' own system |

**What the results show.**
- **Simple beats elaborate here too.** On MemPABench, plain file memory gained 59.2 − 49.6 = **+9.6 points** over no memory, while Mem0 gained 50.0 − 49.6 = **+0.4**, within noise. Retrieval-style stores often *held* a preference without surfacing it when the decision was made.
- **Pinned files over-apply.** MemPABench also scores getting both the task and the user's setting right in context. Under file memory that score fell from 21.8% to 14.4%, because a preference seen in every prompt got applied where it didn't belong. This is the cost side of the "always in the prompt" route.
- **Full context still leads on cued recall.** On PersonaMem, full context scored 69.8 in an independent v2 run, against 43.1–61.2 for Mem0, Zep, Supermemory and MemOS.
- **Memory layers rarely beat plain retrieval.** On MemoryBench (continual learning from user feedback), no memory system consistently beat plain RAG ([MemoryBench](https://arxiv.org/html/2510.17281)).
- **Simulated users inflate results.** They are more cooperative than people, and changing which LLM plays the user moves success by up to 9 points ([Lost in Simulation](https://arxiv.org/abs/2601.17087)) [A].
- **No group-3 benchmark has been run on more than a few memory products,** and all are preprints.

**Relevance to Möbius.** Personalization is what Möbius is for, so these benchmarks fit it better than LoCoMo. Its design meets the three hard parts unevenly:
- **Implicit preferences.** Capture depends on the agent calling `remember` when the owner reacts or corrects. That call is already the main recall bottleneck in Möbius's own evaluation.
- **Unprompted recall (its worst fit).** Preferences matter most on requests that don't sound like memory questions, which is exactly where agent-initiated `search` gets skipped. The standard fix is a small pinned profile. Möbius already builds one for the Reflection app (up to 48 notes, sorted alphabetically), but it never reaches chats.
- **Over-application (an untested advantage).** One-claim notes that carry their context might avoid the over-application seen with pinned files.

The closest public templates are TellOnce (does a correction persist?) and MemPABench (choosing settings across preference changes). The Ada benchmark's answer-style correction tasks already include transfer and over-generalization controls, which test the same two failure modes: forgetting a correction, and applying it where it doesn't belong.

## Memory for self-improvement targets the optimizer, not the task

Every system above stores memory for the agent that solves the task. A different line of work stores memory for an **optimizer that rewrites the agent's harness** (its prompts, tools and context setup) while the model weights stay frozen. The clearest recent example is Self-Improvement Memory (SIM) ([arXiv 2611.18856](https://arxiv.org/abs/2611.18856); Google Cloud AI and Google DeepMind, 13 Nov 2026, preprint, not peer-reviewed). Its goal is *continual* self-improvement: when the benchmark or the policy model changes, the optimizer continues from what earlier runs learned instead of restarting from a seed harness.

**Setup.** Each SIM entry is keyed by a **root-cause hypothesis** and lists the attempts made under it: the edit and its rationale, a **predicted outcome logged before evaluation**, the observed outcome, and provenance (domain, benchmark, policy model, round). The optimizer's proposer either cites an existing root cause or names a new one. After each round, only the selected candidate is recorded. There is no retrieval: the whole record goes into the proposer's and selector's prompts "without retrieval or truncation". There is no pruning either; entries persist, and only identical root-cause text is merged.

**Results** [vendor]. Every run used 10 rounds of 5 candidates with Gemini 3.8 Flash as policy, proposer and selector. Across six transfer settings (coding on Terminal-Bench, text classification, workspace tasks, engineering design), full SIM finished highest, **2.43–4.35 points above carrying over the improved harness without SIM**. Only coding was repeated: in 10 paired runs SIM won 9, averaging 21.70 against 19.33, a gain of **+2.36 points (95% interval 1.05 to 3.68, p = 0.003)**. On a base near 20%, that is about 2.36 / 19.33 ≈ 12% relative.

**Caveats.**
- Apart from coding, each setting is a single optimization run. With held-out sets of roughly 33–50 tasks, one task is worth 2–3 points, so the 2–4-point margins are fragile. The held-out sizes come from a summary of the appendix and are unverified.
- The baselines are ablations of SIM itself (cold start, harness transfer, root causes only). It is not compared with Mem0, Letta, ACE, Reflexion or any other memory system.
- No token or dollar costs are reported. A coding round reportedly takes about 4 hours, so a 10-round run takes about 40 hours. That figure also comes from the appendix summary and is unverified.
- The workspace judge is from the same model family as the policy and proposer, a possible self-preference bias that the paper does not discuss.

**Relevance to Möbius.** Möbius already runs a small self-improving loop: the nightly recall audit rewrites `recall_guidance`, the hint of at most 800 characters that steers the search navigator. That loop keeps only the current hint, with no record of what was tried or whether it worked. SIM's format suggests a cheap addition: for each hint change, log the hypothesis ("search misses X because Y"), the predicted effect on recall and noise, and the next audit's observed effect. That would let the consolidator avoid oscillating between hints or retrying failed ones, at the cost of a few log lines per night and some prompt tokens. The Reflection app's friction log has the same "root cause → fix → outcome" shape, but Reflection only proposes fixes, so the outcome step depends on the owner applying them. SIM does not bear on per-task coding efficiency: it improves a harness over many costly evaluation rounds, not an agent's memory between tasks.

## Möbius is the only summary-fed, embedding-free, self-auditing graph

**Write path.** The `remember` tool appends a capture of up to 4,000 characters to `captures.jsonl`. Captures are **not searchable until the nightly consolidation**, which cron runs at 05:30. The consolidator is a Claude or Codex command-line interface (CLI) with no tools. It sees each chat's title, Summary and captures, and **never rereads transcripts**. It must give every proposal a provenance handle. Publishing creates a hermetic git commit, and readers pin to a `.ready` commit. No layer above combines a tool-less offline writer with commit-pinned reads. Letta Code's git-backed tree is the nearest analogue, but its agent writes directly with bash.

**Read path.** `traverse()` has an LLM walk `index.md` → MOCs → notes and prune siblings, with no embeddings and a lexical fallback only on failure. The documented latency is "a few minutes", with a 90 s timeout per call. Taking 2 minutes as an example, that is 120/0.189 ≈ 635× slower than Zep's 189 ms median, and 120/0.71 ≈ 170× slower than Mem0's 0.71 s. A nightly deep-replay audit labels recalled notes as `deep_recall` or `deep_noise`. From these verdicts it writes `recall_guidance` of at most 800 characters, which tunes the navigator. No other system I found tunes its retriever from its own audits. The catch is that the audit's "deep reader" is itself an LLM reference, so recall is measured against another model's judgement, not against ground truth.

**Four corrections to how Möbius is often described.** First, the platform injects **only the 10 most recent chat Summaries** into a new chat, with no size cap. It does not inject Digests or graph notes, and Claude Code's auto memory is disabled. The graph reaches a chat only when the agent searches it. Second, **`supersedes` is enforced only by the LLM prompt**. No code checks or indexes it, so nothing equivalent to Zep's `t_invalid` exists. The only measurement comes from the older pipeline, where memeval's capture recall on supersede cases was 0.0. Third, deleting a chat removes its id, title and backlink from the current tree only. The rule "Never retain or reconstruct a deleted chat's id or title" covers "a current note or source record". **Earlier commits still hold the chat's id in `source: [chat:<id>]` lines and its title in `sources/*.json`**, because commits are immutable and nothing in the store code rewrites history. The newer source records use a keyed hash (HMAC-SHA256) of the chat id. I found no purge path, and uninstalling keeps the repository. Fourth, **memeval tested an older pipeline and has no v2.5.0 numbers**. No results for the current Memory app are committed in either repository. That older pipeline extracted generic facts from transcripts and pasted all of them into the prompt, with no graph and no search. Its capture precision was 0.494 and recall 0.889. End to end it scored 5/8, against 3/8 with no memory and 7/8 for a flat inbox of full transcripts. With n = 8, the standard error at 5/8 is √(0.625 × 0.375 / 8) = √0.0293 ≈ **0.17**, so the 95% interval is about ±0.34. The two-question gap to the flat inbox therefore sits inside the noise. The runs were also not independent, because responses were cached by a hash of the prompt.

Two smaller facts matter for privacy and scope. The app requests chat-log access `summary_with_deleted`, which also covers chats deleted within the last seven days. Yet v2.5.0 reads only titles and timestamps, so the grant is wider than the code needs. Code knowledge is excluded by policy, the same stance as Claude Code and the opposite of Copilot and Cognee.

### What Möbius could borrow, with trade-offs

| Borrowing | From | Gain | Cost / risk |
|---|---|---|---|
| Make same-day captures searchable (search `captures.jsonl` lexically until consolidation) | Mem0, Zep (write-on-ingest) | Fixes the up-to-24 h blind spot | Unconsolidated captures are noisier and may contradict the graph; mark them as provisional |
| BM25 or hybrid prefilter before the LLM walk | Zep, Cognee, Hindsight (RRF) | Could cut minutes to seconds; independent harnesses show BM25 is a strong baseline (41.5 vs Mem0 21.1) | Adds an index; may lose the navigator's cross-note reasoning; must be measured, not assumed |
| Structural `supersedes` field plus a check that the superseded note is retired | Zep bi-temporal edges; Supermemory `isLatest` | Targets the field's weakest operation (the old pipeline's 0.0 supersede recall; Mem0's HaluMem update omissions) | Schema change; the LLM can still pick the wrong predecessor |
| Verify notes against their sources at read time, plus an expiry for unused notes | Copilot (citation check, 90-day expiry) | Catches stale facts the nightly pass missed | Möbius's forgetting policy deliberately keeps rarely used lasting facts; expiry should apply only to transient notes |
| Human approval inbox for new or changed notes | Gemini CLI, Devin | Owner sees every change; fits a single-owner system | Approval fatigue; Gemini's own default is to create nothing unless evidence is strong |
| History purge on chat deletion | Cognee `forget(clean_history=True)` | Real erasure | Rewriting git history breaks commit pins and rollback; needs a rebuild step |
| Narrow `summary_with_deleted` to the level actually used | Least privilege | Smaller privacy surface | None found, unless a future version reads transcripts |
| Cap or budget the 10 injected Summaries | Claude Code's 25 KB / 200-line cap | Bounded per-request cost | May drop context that some chats need |
| Small pinned profile block | Letta `system/`; Supermemory profile | Lasting facts reach every chat without a search taking minutes | A 1,500-token block costs every request; see the calculation below |
| Keep raw captures next to consolidated notes; typed memories | Hindsight (raw facts, world vs experience) | Guards against consolidation losing details | More storage and more tokens at read time |
| Log each `recall_guidance` change with its hypothesis, predicted effect and observed effect | Self-Improvement Memory (arXiv 2611.18856) | Stops the nightly audit from oscillating between hints or retrying failed ones | A few log lines and prompt tokens per night; evidence is one vendor preprint with small gains (+2.4 points on coding) |

The **pinned-profile cost** works out as follows. This is my own estimate, assuming a 1,500-token block, a 20-turn chat, and Anthropic-style cache pricing (a cache write costs 1.25× the input price, a cache read 0.1×). Turn 1 costs 1,500 × 1.25 = 1,875 input-equivalent tokens. Turns 2–20 cost 19 × 1,500 × 0.1 = 2,850. The total is **about 4,725 input-equivalent tokens per chat**, against 30,000 uncached. A draft Möbius benchmark design put it at about 4,900 under slightly different assumptions, so roughly 4,700–4,900, or about 245 per turn. That is cheap if the profile is accurate. But the AGENTS.md study suggests always-loaded context can raise cost per resolved task by 19–40% without helping. Möbius already builds a profile, but it never reaches chats. The profile builder takes up to 48 user notes, sorted alphabetically rather than by importance, and publishes them for the Reflection app. Ranking that list by importance would be a cheap first step towards a pinned block.

## Benchmarking Möbius needs the baselines memory vendors omit

The literature points to five design rules. First, **include the baselines that independent harnesses show are hard to beat**: no memory, full or long context where the history fits, BM25 or plain files over captures, a flat inbox of raw captures or transcripts (the memeval leader at 7/8), and an append-only log in the style of Mem0 2026. Add an oracle arm that injects the correct fact, because VibeMemBench's oracle gained only +1.1 to +4.5 points. If an oracle cannot help, the task cannot show a memory effect. Second, **validate the judge with negative controls**. Score deliberately wrong answers and report how many the judge accepts, given LoCoMo's 62.81% false acceptance and BEAM's 10.9-point judge swing. Use the same reader model in every arm. Third, **measure tokens and dollars per resolved task, including memory overhead**: the nightly consolidator's receipts, the minutes of navigator calls, and the 10 injected Summaries. The AGENTS.md study shows accuracy can stay flat while cost per resolved task rises 19–40%. Fourth, **test supersession and omission directly**. The old pipeline's 0.0 supersede recall and Mem0's 74.02% update omission on HaluMem show these are the weakest operations across the field, and a time-shifted design like Ground Truth First's (3 versus 9 weeks) matters because rankings flipped. Fifth, **treat the 10 Summaries as a separate factor**, since they reach every chat without the graph. A memory-on versus memory-off comparison that leaves Summaries on measures only the graph's marginal value.

Sample size has to match the noise. To detect a 15% relative difference in tokens per task at 80% power and two-sided α = 0.05, the number of runs per arm is n = 2 × (1.96 + 0.84)² × CV² / 0.15², where CV is the coefficient of variation (standard deviation divided by mean). With CV = 0.5: 2 × 7.84 × 0.25 / 0.0225 = **174 runs per arm**. With CV = 1.0: 2 × 7.84 × 1.0 / 0.0225 ≈ **697**. Repeated runs of the same SWE-bench task differ by up to 30× in total tokens ([Bai et al.](https://arxiv.org/abs/2604.22750)), so a CV near 1 is plausible. Those figures are for token cost. Accuracy is a different metric, but memeval's 8 questions give a ±0.34 interval, which is far too wide to separate arms that differ by a few points. The built-in replay benchmark is useful for regression checks, but because its reference is an LLM's deep read, it cannot stand in for ground truth.

## Conclusion

The field is converging on Möbius's architecture from both sides. The harnesses (Codex, ChatGPT, Copilot, Gemini CLI) adopted background consolidation of idle sessions in 2026, and Letta moved to git-backed markdown. Meanwhile Mem0 retreated from LLM-decided updates to append-only. What is genuinely novel in Möbius is not the graph or git. It is **commit-pinned reads from a tool-less writer, and a retriever that tunes itself from nightly audits**. Its weakest points match the field's weakest points: supersession that no code enforces, erasure that git history defeats, and minutes of read latency that independent results suggest a BM25 prefilter or a flat inbox might match.

The most useful benchmark result for Möbius would therefore not be a LoCoMo number, which would be uninterpretable given that benchmark's 6.4% wrong gold answers and lenient judge. It would be a time-shifted, cost-per-resolved-task comparison against the flat inbox, BM25 over captures and long context, with Summaries on and off as separate arms. Every independent study so far predicts that the simple baselines will be hard to beat. That is uncomfortable, but it is the comparison that would show whether the graph earns its minutes.
