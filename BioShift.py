# -*- coding: utf-8 -*-
"""
BioShiftUpdated.py -- Prompt 1 -> Table 1, Prompt 2 -> Table 2/Table 3, and
Prompt 3 -> biological interpretation, for isolated review.
@author: pdawadi
"""
import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import NamedTemporaryFile

# ─────────────────── Third-party imports (with friendly error) ─────────────
try:
    import pandas as pd
except Exception:
    sys.exit("The 'pandas' package is required. Install with: pip install pandas")

try:
    import openai  # We support both OpenAI 1.x and legacy 0.x
except Exception:
    sys.exit("The 'openai' package is required. Install with: pip install openai")


# ─────────────────── Config (standalone: this file's own local config.txt,
# right next to this .py file in BioShift_0729/fix/) ────────────────────────
HERE = Path(__file__).resolve().parent
CONFIG_TXT = HERE / "config.txt"

def _parse_simple_kv(path: Path) -> dict:
    """
    Parse key=value lines, ignoring blank lines and comments (#).
    Keys are upper-cased. Values are raw (stripped).
    """
    cfg = {}
    if not path.exists():
        return cfg
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip().upper()] = v.strip()
    return cfg

def load_api_key() -> str:
    kv = _parse_simple_kv(CONFIG_TXT)
    key = kv.get("KEY", "").strip()
    if not key:
        key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("No API key found. Put KEY=... in config.txt (same folder) "
                 "or set the OPENAI_API_KEY environment variable.")
    return key

def load_gpt_options() -> dict:
    kv = _parse_simple_kv(CONFIG_TXT)
    default_model = (kv.get("DEFAULT_MODEL") or "").strip()
    if not default_model:
        sys.exit("DEFAULT_MODEL not set in config.txt.")
    try:
        temperature = float(kv.get("TEMPERATURE", "0.2"))
    except ValueError:
        temperature = 0.2
    try:
        max_tokens = int(kv.get("MAX_TOKENS", "2000"))
    except ValueError:
        max_tokens = 2000
    # Optional TOP_P=... (nucleus sampling; OpenAI's own default is 1.0 if
    # never set) and SEED=... (an integer -- when set, OpenAI attempts
    # best-effort deterministic sampling and returns a 'system_fingerprint'
    # identifying the exact backend model snapshot that served the
    # request, which call_openai records for the run log). Both are real,
    # inspectable decoding parameters, kept optional since most users never
    # need to touch them.
    try:
        top_p = float(kv.get("TOP_P", "1.0"))
    except ValueError:
        top_p = 1.0
    seed_raw = (kv.get("SEED") or "").strip()
    seed = None
    if seed_raw:
        try:
            seed = int(seed_raw)
        except ValueError:
            seed = None
    return {
        "default_model": default_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "seed": seed,
    }

API_KEY = load_api_key()
GPT_CFG = load_gpt_options()
DEFAULT_MODEL = GPT_CFG["default_model"]
TEMPERATURE = GPT_CFG["temperature"]
MAX_TOKENS = GPT_CFG["max_tokens"]
TOP_P = GPT_CFG["top_p"]
SEED = GPT_CFG["seed"]

def load_coshift_model() -> str:
    """Optional Prompt-2-only model override (COSHIFT_MODEL in config.txt),
    falling back to DEFAULT_MODEL if left blank. Added because Prompt 2's
    real KB blocks (e.g. an uncapped ImmuneXpresso block can run to 250+
    lines) compete for the model's attention against the abstracts shown in
    the same call -- gpt-4o-mini was observed to read real KB content but
    still choose to cite zero of it in its actual response, even when a
    real, citable direct pair (both ends in the master element list) was
    present. A stronger model just for this one step is the lowest-risk
    fix: no prompt-text changes, and Table 1/Prompt 3 keep using the
    cheaper DEFAULT_MODEL unaffected."""
    kv = _parse_simple_kv(CONFIG_TXT)
    return (kv.get("COSHIFT_MODEL") or "").strip() or DEFAULT_MODEL

def load_coshift_max_tokens() -> int:
    """Optional Prompt-2-only max_tokens override (COSHIFT_MAX_TOKENS in
    config.txt), falling back to MAX_TOKENS if blank/invalid -- same
    rationale as load_coshift_model: Part A + Part B for a whole batch can
    be a long response, and a low cap risks truncating it before KB-sourced
    rows (which may come later in the model's own generated table) are
    written out at all."""
    kv = _parse_simple_kv(CONFIG_TXT)
    raw = (kv.get("COSHIFT_MAX_TOKENS") or "").strip()
    if not raw:
        return MAX_TOKENS
    try:
        return int(raw)
    except ValueError:
        return MAX_TOKENS

COSHIFT_MODEL = load_coshift_model()
COSHIFT_MAX_TOKENS = load_coshift_max_tokens()

# Prompt 2 carries no KB content in its own text (PubMed abstracts only,
# same as Prompt 1); KB-sourced Table 2/3 rows are built directly in Python
# (see build_kb_sourced_table2_rows), so there is no LLM prompt-size budget
# to manage here.

def load_kb_flag() -> bool:
    """KNOWLEDGE_BASE=On/Off toggles the structured-knowledge-base lookup
    (ImmuneXpresso cell-cytokine interactions + UniProt co-mentions) that
    feeds Table 2/3's KB-sourced rows (see build_kb_sourced_table2_rows).
    Defaults to On. Set to Off in config.txt to skip it (e.g. if the
    Database/ files aren't present). MASI and MiMeDB are not part of this
    lookup; neither data file is loaded anywhere. The Table 3 knowledge
    graph's microbe node shape is detected via organism_taxonomy_ids.csv
    instead (see build_table3_knowledge_graph)."""
    kv = _parse_simple_kv(CONFIG_TXT)
    return (kv.get("KNOWLEDGE_BASE", "On") or "On").strip().lower() in ("on", "true", "1", "yes")

KNOWLEDGE_BASE = load_kb_flag()

def load_ncbi_api_key() -> str:
    """Optional NCBI_API_KEY=... in config.txt. PubMed's E-utilities work
    without one (rate-limited to 3 requests/second); a free key (register
    at https://www.ncbi.nlm.nih.gov/account/) raises that to 10/second,
    worth it once you're calling this per element."""
    kv = _parse_simple_kv(CONFIG_TXT)
    return (kv.get("NCBI_API_KEY", "") or "").strip()

NCBI_API_KEY = load_ncbi_api_key()

def load_pubmed_max_abstracts() -> int:
    """Optional PUBMED_MAX_ABSTRACTS=... in config.txt -- how many TOP-
    ranked abstracts (across ALL elements combined, ranked by real
    element-mention count -- see fetch_ranked_combined_pool) actually go
    to the LLM extraction step. Defaults to 1000 (i.e. process the whole
    real fetched pool, not just a top-N ranked slice -- a low-mention
    element like APCs/Th17 can lose the co-occurrence ranking and never
    reach extraction if this is capped well below the pool size). Lower
    this in config.txt if speed/cost matters more than exhausting the
    pool."""
    kv = _parse_simple_kv(CONFIG_TXT)
    try:
        return max(1, int(kv.get("PUBMED_MAX_ABSTRACTS", "1000")))
    except ValueError:
        return 1000

PUBMED_MAX_ABSTRACTS = load_pubmed_max_abstracts()

def load_pubmed_search_pool_size() -> int:
    """Optional PUBMED_SEARCH_POOL_SIZE=... in config.txt -- how many real
    PMIDs the initial COMBINED search+fetch (across all elements at once)
    pulls before ranking. This step is cheap (real PubMed API calls only,
    no LLM), so it can safely be much larger than PUBMED_MAX_ABSTRACTS --
    a bigger pool means the top-N-by-relevance selection has more real
    candidates to choose from. Defaults to 1000 (or PUBMED_MAX_ABSTRACTS
    itself if that's set higher than 1000)."""
    kv = _parse_simple_kv(CONFIG_TXT)
    try:
        return max(PUBMED_MAX_ABSTRACTS, int(kv.get("PUBMED_SEARCH_POOL_SIZE", str(max(1000, PUBMED_MAX_ABSTRACTS)))))
    except ValueError:
        return max(1000, PUBMED_MAX_ABSTRACTS)

PUBMED_SEARCH_POOL_SIZE = load_pubmed_search_pool_size()

def load_pubmed_extraction_runs() -> int:
    """Optional PUBMED_EXTRACTION_RUNS=... in config.txt -- how many
    independent times the full retrieval+extraction pass runs (see
    run_extraction_ensemble: each run re-fetches the combined pool, then
    independently extracts from it). A deterministic Python aggregation
    step then takes the majority-vote direction per (PMID, element) pair
    across the runs (aggregate_extraction_runs), which catches an
    occasional LLM misreading of a given abstract on a given call.
    Defaults to 5. This multiplies LLM call count/cost by this many
    times (e.g. 5 runs x 50 batches at the 1000-abstract default = ~250
    calls), so lower it in config.txt if that's too slow/expensive."""
    kv = _parse_simple_kv(CONFIG_TXT)
    try:
        return max(1, int(kv.get("PUBMED_EXTRACTION_RUNS", "5")))
    except ValueError:
        return 5

PUBMED_EXTRACTION_RUNS = load_pubmed_extraction_runs()

def load_max_concurrent_llm_calls() -> int:
    """Optional MAX_CONCURRENT_LLM_CALLS=... in config.txt -- how many
    batch-extraction LLM calls run_extraction_ensemble/run_coshift_
    ensemble fire off IN PARALLEL (via a thread pool) instead of one
    sequential call at a time. Each batch call is independent (a
    stateless read of one batch of abstracts, no shared state, no run
    ever depends on another run's or another batch's result), so
    parallelizing them doesn't change what's computed -- only how long
    real wall-clock time it takes, since most of that time is spent
    waiting on the OpenAI API, not on local CPU. Defaults to 5; raise it
    if your API tier allows more concurrent requests, lower it (to 1) to
    go back to fully sequential."""
    kv = _parse_simple_kv(CONFIG_TXT)
    try:
        return max(1, int(kv.get("MAX_CONCURRENT_LLM_CALLS", "5")))
    except ValueError:
        return 5

MAX_CONCURRENT_LLM_CALLS = load_max_concurrent_llm_calls()

def load_coshift_max_concurrent_llm_calls() -> int:
    """Optional COSHIFT_MAX_CONCURRENT_LLM_CALLS=... in config.txt --
    separate concurrency cap for run_coshift_batches, instead of reusing
    MAX_CONCURRENT_LLM_CALLS. Table 2/3's co-shift batches use COSHIFT_
    MODEL (gpt-4o) plus a full KB block and run ~11,000-20,000 tokens
    each, so just 2-3 landing in the same minute can exceed a 30,000 TPM
    account ceiling; each resulting 429 costs a 2-6s backoff retry (see
    call_openai), so over-parallelizing here makes runs slower, not
    faster. Defaults to 2; raise it only if your co-shift prompts are
    smaller than ~10,000 tokens or your account's TPM tier is higher."""
    kv = _parse_simple_kv(CONFIG_TXT)
    try:
        return max(1, int(kv.get("COSHIFT_MAX_CONCURRENT_LLM_CALLS", "2")))
    except ValueError:
        return 2

COSHIFT_MAX_CONCURRENT_LLM_CALLS = load_coshift_max_concurrent_llm_calls()


def load_pubmed_use_cache() -> bool:
    """Defaults to false -- every PUBMED_EXTRACTION_RUNS run makes a fresh
    real PubMed esearch/efetch call rather than reading pubmed_cache/.
    Not exposed in config.txt; can be enabled by adding
    PUBMED_USE_CACHE=true there if needed."""
    kv = _parse_simple_kv(CONFIG_TXT)
    return (kv.get("PUBMED_USE_CACHE", "false") or "false").strip().lower() not in ("false", "0", "no")

PUBMED_USE_CACHE = load_pubmed_use_cache()

def load_sample_model() -> str:
    """SAMPLE_MODEL=Human/Mouse in config.txt -- which species' gene symbol
    and UniProt ID columns to use when matching elements against the
    ImmPort Cytokine Registry (it has parallel Human and Mouse columns for
    every cytokine). Defaults to Human. If a specific element has no data
    in the Mouse columns (registry coverage is uneven), matching falls back
    to that element's Human columns rather than silently dropping it."""
    kv = _parse_simple_kv(CONFIG_TXT)
    val = (kv.get("SAMPLE_MODEL", "Human") or "Human").strip().lower()
    return "Mouse" if val.startswith("mouse") else "Human"

SAMPLE_MODEL = load_sample_model()

def _load_study_context_field(key: str, default: str) -> str:
    kv = _parse_simple_kv(CONFIG_TXT)
    return (kv.get(key, default) or default).strip()

def load_study_context() -> dict:
    """Study-design metadata from config.txt -- NOT used to filter or alter
    any evidence, only carried alongside Table 1/Table 2/Table 3 as context
    for the reader and for Prompt 1/2/3's own instructions, since literature/
    KB evidence found for an element may come from a different disease,
    disease stage, tissue, species, or technique than THIS dataset actually
    used. Every field is a free-text config value the user fills in for
    their own study; the values below are placeholder examples only (fill
    in config.txt with your actual study details). Matches Studycontext.txt
    and the "study context" paragraph shared verbatim across Prompts 1/2/3
    in BioShift_Prompts_0729_PD: disease, disease stage, tissue site, host
    species, experimental modality, taxonomic resolution, and the dataset's
    Baseline Group and Target Group.
      DISEASE_NAME: e.g. Periodontitis.
      DISEASE_STAGE: e.g. periodontitis severity/stage (or health status
        for the healthy/recovery context).
      TISSUE_SITE: e.g. subgingival plaque, gingival crevicular fluid,
        saliva, gingival tissue biopsy -- where samples came from.
      HOST_SPECIES: e.g. Human, Mouse, Rat -- falls back to SAMPLE_MODEL
        (the same Human/Mouse toggle already used for ImmPort Cytokine
        Registry matching) if left blank, since that's usually the same
        real answer.
      EXPERIMENTAL_MODALITY: e.g. 16S rRNA sequencing, RNA-seq, ELISA/
        Luminex cytokine panel, flow cytometry, in silico -- how the
        Observed Shift values were actually measured/derived.
      TAXONOMIC_RESOLUTION: e.g. species-level, genus-level, strain-level
        -- how finely microbes were identified in this dataset.
      BASELINE_GROUP / TARGET_GROUP: what the Observed Shift in the
        element list actually compares (Target vs Baseline), e.g. "Later
        time points" vs "Earlier time points" -- replaces the older
        "linear regression across longitudinal timepoints" framing."""
    host_species = _load_study_context_field("HOST_SPECIES", "")
    if not host_species:
        host_species = SAMPLE_MODEL
    return {
        "Disease": _load_study_context_field(
            "DISEASE_NAME", "Example: Periodontitis (fill in config.txt)"),
        "Disease Stage": _load_study_context_field(
            "DISEASE_STAGE", "Example: Stage II, Grade B chronic periodontitis (fill in config.txt)"),
        "Tissue/Site Specificity": _load_study_context_field(
            "TISSUE_SITE", "Example: Subgingival plaque (fill in config.txt)"),
        "Host Species": host_species,
        "Experimental Modality": _load_study_context_field(
            "EXPERIMENTAL_MODALITY", "Example: 16S rRNA sequencing + Luminex cytokine panel (fill in config.txt)"),
        "Taxonomic Resolution": _load_study_context_field(
            "TAXONOMIC_RESOLUTION", "Example: Species-level (fill in config.txt)"),
        "Baseline Group": _load_study_context_field(
            "BASELINE_GROUP", "Example: Earlier time points (fill in config.txt)"),
        "Target Group": _load_study_context_field(
            "TARGET_GROUP", "Example: Later time points (fill in config.txt)"),
    }

def _study_context_block_for_prompt() -> str:
    """Full Study Context block for {study_context} in Prompts 1/2/3 --
    ALL fields shown as real "- Field: Value" lines, including any left
    'Unknown' or a placeholder. The new prompts' own Study Context
    paragraph already tells the LLM this is metadata, not evidence, and
    Prompt 3's Analysis Instructions already handle an unfilled/
    placeholder field responsibly (treat as not specified, don't guess) --
    so showing every field as-is, exactly matching Studycontext.txt, is
    more transparent than silently omitting the unfilled ones."""
    ctx = load_study_context()
    return "\n".join(f"- {k}: {v}" for k, v in ctx.items())

def _study_context_pubmed_terms() -> list:
    """Real (filled-in, non-placeholder) values for the study-context
    fields the retrieval query combines with the Element list -- Disease,
    Disease Stage, Tissue/Site Specificity. Host Species and Experimental
    Modality are deliberately excluded from this literal PubMed query:
    generic single words like "Human"/"in silico" are common enough that
    OR-ing them in (see _build_disease_clause) would let a paper through
    just for containing that word anywhere in the title/abstract, with no
    requirement to actually be on-topic. Disease Name and Tissue Site are
    specific enough phrases to be a reliable topical anchor even OR'd.
    Species/Modality are still shown to the LLM via the Study Context
    block in every prompt, just not used as a PubMed keyword filter.
    Taxonomic Resolution/Baseline Group/Target Group are also not
    included. A field left blank, 'Unknown', or the unfilled
    '...(fill in config.txt)' example text is skipped (never searched for
    literally), so an unfilled field can't silently narrow/pollute every
    query."""
    ctx = load_study_context()
    wanted = ["Disease", "Disease Stage", "Tissue/Site Specificity"]
    terms = []
    for key in wanted:
        val = str(ctx.get(key, "")).strip()
        if not val or val.lower() == "unknown" or "(fill in config.txt)" in val.lower():
            continue
        terms.append(f'"{val}"[tiab]')
    return terms

def _build_disease_clause() -> str:
    """The AND-side of every PubMed query: real study-context terms
    (disease name/disease stage/tissue site -- see
    _study_context_pubmed_terms) OR'd together with the static
    periodontitis-name synonym terms (PUBMED_DISEASE_TERMS --
    "periodontitis"/"periodontal disease"/"gum disease"), rather than
    either/or. This is a pure recall increase: "periodontal disease"/
    "gum disease" name the same condition config.txt's Disease field
    specifies, just with different wording some papers use instead of
    the literal word "periodontitis", so adding them as more OR paths to
    the same topical anchor only lets in more on-topic papers, never a
    different topic. Falls back to just the static terms alone when
    nothing is filled in config.txt.

    OR rather than AND: "Periodontitis" alone is already a specific,
    reliable topical anchor -- virtually every genuinely relevant paper
    contains it. AND-requiring "Periodontal tissue" (or Disease Stage) as
    a literal phrase too risks dropping real periodontitis papers that
    describe the tissue differently (gingiva, subgingival, periodontium,
    gum) without ever using that exact wording. Host Species/Experimental
    Modality are excluded from this clause entirely -- see
    _study_context_pubmed_terms -- since generic single words like
    "Human"/"in silico" are common enough that OR-ing them in would
    effectively disable the filter rather than just broaden it."""
    # Always includes PUBMED_DISEASE_TERMS, so `terms` is never empty --
    # when _study_context_pubmed_terms() is empty (nothing filled in
    # config.txt), this reduces to exactly the static synonym terms alone.
    terms = list(dict.fromkeys(_study_context_pubmed_terms() + PUBMED_DISEASE_TERMS))
    return " OR ".join(terms)

# Set key for both new and legacy clients
os.environ["OPENAI_API_KEY"] = API_KEY
try:
    openai.api_key = API_KEY  # legacy client compat
except Exception:
    pass

# Try new client import if available

_OPENAI_NEW_CLIENT = None
try:
    from openai import OpenAI  # openai>=1.x
    _OPENAI_NEW_CLIENT = OpenAI(api_key=API_KEY)
except Exception:
    _OPENAI_NEW_CLIENT = None


# ─────────────────── IO Layout ─────────────────────────────────────────────
# Fully local to fix/ (HERE, defined above, is this file's own directory --
# no parent BioShift_0729/ folder involved). Observed-shift input CSVs live
# in fix/ObservedShift/<sample>.csv; this script's own Table 1/2/3 output is
# kept in fix/outputs/, separate from BioShift.py's own outputs/. "graphviz"
# and "table3" input subfolders from the old shared inputs/ layout were
# never actually read by this script (dead folders), so they're not
# recreated here.
FOLDERS = {
    "observed": HERE / "ObservedShift",
    "output":   HERE / "outputs",
}
for p in FOLDERS.values():
    p.mkdir(parents=True, exist_ok=True)

# Clean-output convention: every user-facing deliverable for a sample
# (Table 1, Table 2, Prompt 3 text, the network graph) is saved directly
# in that sample's own out_dir, and ONLY those files -- no tables/prompts/
# knowledge_network/kb_evidence subfolders, no raw per-run evidence dumps,
# no cache folders. Intermediate/traceability data (per-run PubMed rows,
# abstract text, etc.) is computed and used in-memory but never written
# to disk.

# Structured knowledge-base source files (read-only reference data curated
# by the user in fix/Database/ -- NOT created/managed by this pipeline).
# ImmuneXpresso and UniProt are this pipeline's only two KB sources; a
# pathway-co-membership relationship layer (from the ImmPort immune-GO
# gene sets) and MASI/MiMeDB were evaluated and dropped (found no genuine
# signal). MASI and MiMeDB are not loaded anywhere; the knowledge graph's
# microbe node shape is detected via organism_taxonomy_ids.csv instead
# (see build_table3_knowledge_graph).
KB_DIR = HERE / "Database"
KB_IMMUNEXPRESSO_FILE = KB_DIR / "ImmuneXpressoResults_Interactions.csv"
KB_CYTOKINE_REGISTRY_FILE = KB_DIR / "ImmPort_CytokineRegistry.November_2015.xls"

PROMPT_PUBMED_EXTRACT_D_MULTI = """AI Role
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Data
Below is the "study context." Study Context has the information that describes the dataset being analyzed. It is metadata, not scientific evidence. It specifies the conditions under which the data were collected, including, disease, disease stage, tissue site, host species, experimental modality, taxonomic resolution, and the dataset's Baseline Group and Target Group.

Study context:
{study_context}

Below is the "element list". For each element, the Observed Shift represents a comparison between the dataset's Baseline Group and Target Group, both defined in the Study Context above. An Observed Shift of 1 means the element's value is higher in the Target Group than in the Baseline Group (an increase); -1 means it is lower (a decrease). For this analysis, you will not use the observed shift values.
Element list:
{element_list}

Analysis Instructions
The abstracts below were retrieved from a PubMed search intended to capture literature relevant to one or more elements in the Element list above. Only use these abstracts as evidence. Use Review abstracts also as evidence, i.e., review statements count and inferred summaries count.

Abstracts:
---
{abstracts}

For each element in the Element list above, collect abstracts that reported increased, decreased, or mixed changes of the element. "Mixed" means the same abstract explicitly reports both increased and decreased changes for the same element in different comparisons, tissues, populations, or time points. Ignore nonsignificant trends and speculative statements ("may", "might", "suggests"). Exclude abstracts whose disease, disease stage, tissue site, or host species clearly differ from those specified in the Study Context. Differences in molecular measurement (experimental modality) (e.g., mRNA versus protein) should not by themselves exclude an abstract. Consider different taxonomic resolutions compatible only when the abstract explicitly refers to the queried taxon or one of its parent taxa in a biologically meaningful way. Treat official symbols, full names, and common aliases as the same element, and also match elements case-insensitively. Unchanged evidence should be ignored. Never infer changes not explicitly stated in the abstract.

Reporting Instructions
Summarize the results in a table. When an element had more than one abstract, produce one row for each (PMID, Element) pair. If no compatible abstract supports an element, omit that element from the table. Columns should be arranged with this order: "PMID", "Element", "Direction", "Quoted Evidence".
- "PMID": bare numeric ID, no prefix.
- "Element": copied EXACTLY as in the "element list."
- "Direction": exactly place one of Up, Down, and Mixed, based on the abstract's own text, where "Up," "Down," and "Mixed" corresponds to increased, decreased, and both directions of the changes.
- "Quoted Evidence": Quote the shortest contiguous phrase that explicitly supports the reported Direction. Never invent or paraphrase. Required for every row; checked word-for-word afterward, so a non-matching quote gets the row discarded.

The table should be pipe-separated ("|") with header row without divider nor extra spaces. Output ONLY the table.
"""

PUBMED_MULTI_PROMPTS_BY_CONTEXT = {
    "disease": PROMPT_PUBMED_EXTRACT_D_MULTI,
}

def get_pubmed_extract_multi_prompt(context: str) -> str:
    if context not in PUBMED_MULTI_PROMPTS_BY_CONTEXT:
        raise NotImplementedError(
            f"No combined-pool Prompt 1 template for context='{context}' yet -- "
            f"only 'disease' is built."
        )
    return PUBMED_MULTI_PROMPTS_BY_CONTEXT[context]

# ─────────────────── Utilities ─────────────────────────────────────────────
def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# Real, official per-1M-token prices (standard, non-batch API rate),
# verified directly against OpenAI's own pricing pages (developers.openai.
# com/api/docs/pricing and .../api/docs/models/gpt-4o-mini) on 2026-08-01
# -- (input $/1M tokens, output $/1M tokens). Only the 2 models this file
# actually calls (DEFAULT_MODEL/COSHIFT_MODEL in config.txt) need to be
# here; an unpriced/unrecognized model is reported with its real token
# count but flagged "cost unknown" in get_cost_summary rather than
# silently treated as free or guessed at a price.
_PRICING_PER_1M_TOKENS = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
_cost_lock = threading.Lock()
_cost_totals = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0, "snapshots": set()})

def _record_call_cost(model: str, input_tokens: int, output_tokens: int, snapshot: str = None) -> None:
    """Thread-safe (real co-shift/extraction calls run concurrently via
    ThreadPoolExecutor -- see MAX_CONCURRENT_LLM_CALLS/COSHIFT_MAX_
    CONCURRENT_LLM_CALLS) running total of REAL token usage per model, as
    reported by the OpenAI API's own 'usage' field on each response --
    never this file's own _estimate_tokens heuristic, which is a
    pre-call sizing approximation only, not billing-accurate.

    `model` stays keyed by the requested alias (e.g. 'gpt-4o') since
    that's what _PRICING_PER_1M_TOKENS is keyed by -- OpenAI prices a
    model family the same regardless of which dated snapshot actually
    served the request. `snapshot`, when given, is the REAL dated model
    string the API response itself reported (e.g. 'gpt-4o-2024-08-06')
    -- tracked separately (as a set, since a long run could in principle
    span more than one) purely for accurate reporting of which exact
    backend build was used, without touching the pricing lookup key."""
    with _cost_lock:
        d = _cost_totals[model]
        d["input_tokens"] += input_tokens
        d["output_tokens"] += output_tokens
        d["calls"] += 1
        if snapshot:
            d["snapshots"].add(snapshot)

def get_cost_summary() -> tuple:
    """Returns (total_dollars, multi-line breakdown string) for every
    real OpenAI call made so far this process, one line per model alias
    actually used, each annotated with the real dated snapshot(s) OpenAI
    itself reported serving those calls (see _record_call_cost) -- e.g.
    'gpt-4o (snapshot: gpt-4o-2024-08-06)' rather than just the bare
    alias, so the exact backend build is auditable, not just the family
    name. Real, not estimated -- built from the token counts the API
    itself reported for each real call."""
    total = 0.0
    lines = []
    for model, d in sorted(_cost_totals.items()):
        snap_note = f" (snapshot: {', '.join(sorted(d['snapshots']))})" if d["snapshots"] else " (snapshot: unknown)"
        pricing = _PRICING_PER_1M_TOKENS.get(model)
        if pricing is None:
            lines.append(f"  {model}{snap_note}: {d['calls']} call(s), {d['input_tokens']:,} in / "
                         f"{d['output_tokens']:,} out real token(s) -- cost unknown "
                         f"(no real price on file for this model)")
            continue
        in_price, out_price = pricing
        cost = d["input_tokens"] / 1_000_000 * in_price + d["output_tokens"] / 1_000_000 * out_price
        total += cost
        lines.append(f"  {model}{snap_note}: {d['calls']} call(s), {d['input_tokens']:,} in / "
                     f"{d['output_tokens']:,} out real token(s) -- ${cost:.4f}")
    return total, "\n".join(lines)

def _snapshot_cost_totals() -> dict:
    """Point-in-time copy of _cost_totals, for get_cost_summary_since()
    below. Exists because --sample all runs every sample in one process,
    and _cost_totals itself is a running total for the whole process --
    without this, every sample after the first would report the batch's
    cumulative cost so far instead of its own."""
    with _cost_lock:
        return {
            model: {
                "input_tokens": d["input_tokens"],
                "output_tokens": d["output_tokens"],
                "calls": d["calls"],
                "snapshots": set(d["snapshots"]),
            }
            for model, d in _cost_totals.items()
        }

def get_cost_summary_since(before: dict) -> tuple:
    """Same real-usage, real-pricing accounting as get_cost_summary(),
    but only for calls/tokens recorded after `before` (a
    _snapshot_cost_totals() taken at the start of one sample's run) --
    so each sample in a --sample all batch gets its own accurate cost
    line, not the cumulative total across every sample run so far this
    process. For a single-sample run (`before` taken at process start,
    when _cost_totals is empty) this returns the exact same numbers as
    get_cost_summary() would."""
    total = 0.0
    lines = []
    for model, d in sorted(_cost_totals.items()):
        b = before.get(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0, "snapshots": set()})
        calls = d["calls"] - b["calls"]
        if calls <= 0:
            continue
        input_tokens = d["input_tokens"] - b["input_tokens"]
        output_tokens = d["output_tokens"] - b["output_tokens"]
        new_snapshots = sorted(d["snapshots"] - b["snapshots"]) or sorted(d["snapshots"])
        snap_note = f" (snapshot: {', '.join(new_snapshots)})" if new_snapshots else " (snapshot: unknown)"
        pricing = _PRICING_PER_1M_TOKENS.get(model)
        if pricing is None:
            lines.append(f"  {model}{snap_note}: {calls} call(s), {input_tokens:,} in / "
                         f"{output_tokens:,} out real token(s) -- cost unknown "
                         f"(no real price on file for this model)")
            continue
        in_price, out_price = pricing
        cost = input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price
        total += cost
        lines.append(f"  {model}{snap_note}: {calls} call(s), {input_tokens:,} in / "
                     f"{output_tokens:,} out real token(s) -- ${cost:.4f}")
    return total, "\n".join(lines)

def _warn_if_truncated(finish_reason, model: str, max_tokens: int, usage) -> None:
    """Logs a visible warning when OpenAI's own response says it stopped
    because it hit max_tokens (finish_reason == 'length') rather than
    finishing its answer naturally. Without this, a response truncated
    mid-table and a complete-but-selective one look identical in the log
    -- an element can silently end up with zero evidence because its row
    was never generated, not because it was rejected. Prints completion-
    token usage alongside the configured cap so it's obvious how close to
    the ceiling the response landed."""
    if finish_reason != "length":
        return
    completion_tokens = None
    if usage is not None:
        completion_tokens = (getattr(usage, "completion_tokens", None) if hasattr(usage, "completion_tokens")
                              else (usage.get("completion_tokens") if hasattr(usage, "get") else None))
    print(f"WARNING: OpenAI response for model={model} was TRUNCATED (finish_reason='length') -- "
          f"hit the max_tokens={max_tokens} cap before finishing"
          + (f" (used {completion_tokens} real completion token(s))." if completion_tokens is not None else ".")
          + " Any table rows after the cutoff point were never generated, not just rejected -- "
            "raise the relevant *_MAX_TOKENS config value if this recurs often.")

def call_openai(prompt: str, model: str = None, max_tokens: int = None) -> str:
    """
    Fireproof OpenAI call:
    - Try new client (openai>=1.x) first
    - Fallback to legacy openai.ChatCompletion (<=0.x)
    - Retry 3 times with small backoff

    `model`/`max_tokens` default to DEFAULT_MODEL/MAX_TOKENS (the global
    config.txt settings) when not given -- callers that need a per-step
    override (e.g. extract_and_group_coshift_from_batch using
    COSHIFT_MODEL/COSHIFT_MAX_TOKENS) pass them explicitly instead.

    Every successful real call's REAL token usage (as OpenAI's own
    response reports it, not an estimate) is recorded via
    _record_call_cost so the real run cost can be printed at the end
    (see main()'s "Total cost" line) -- failed/retried attempts that
    never got a response contribute nothing.
    """
    use_model = model or DEFAULT_MODEL
    use_max_tokens = max_tokens if max_tokens is not None else MAX_TOKENS
    last_err = None
    for attempt in range(1, 4):
        try:
            if _OPENAI_NEW_CLIENT is not None:
                kwargs = dict(
                    model=use_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=TEMPERATURE,
                    max_tokens=use_max_tokens,
                    top_p=TOP_P,
                )
                if SEED is not None:
                    kwargs["seed"] = SEED
                resp = _OPENAI_NEW_CLIENT.chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                # resp.model is the REAL dated snapshot OpenAI's own
                # response reports serving this call (e.g.
                # 'gpt-4o-2024-08-06') -- distinct from use_model, which
                # is just the alias we requested ('gpt-4o'). Tracked
                # alongside cost so the exact backend build is auditable.
                real_snapshot = getattr(resp, "model", None)
                if usage is not None:
                    _record_call_cost(use_model, getattr(usage, "prompt_tokens", 0) or 0,
                                       getattr(usage, "completion_tokens", 0) or 0, snapshot=real_snapshot)
                finish_reason = getattr(resp.choices[0], "finish_reason", None)
                _warn_if_truncated(finish_reason, use_model, use_max_tokens, usage)
                return text.strip()
            else:
                resp = openai.ChatCompletion.create(
                    model=use_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=TEMPERATURE,
                    max_tokens=use_max_tokens,
                    top_p=TOP_P,
                )
                text = resp["choices"][0]["message"]["content"] or ""
                usage = resp.get("usage") if hasattr(resp, "get") else None
                real_snapshot = resp.get("model") if hasattr(resp, "get") else None
                if usage:
                    _record_call_cost(use_model, usage.get("prompt_tokens", 0) or 0,
                                       usage.get("completion_tokens", 0) or 0, snapshot=real_snapshot)
                finish_reason = resp["choices"][0].get("finish_reason") if hasattr(resp["choices"][0], "get") else None
                _warn_if_truncated(finish_reason, use_model, use_max_tokens, usage)
                return text.strip()
        except Exception as e:
            print(f"OpenAI error ({attempt}/3): {e}")
            last_err = e
            # A 429 rate-limit error's message states exactly how long
            # until the per-minute budget frees up (e.g. "Please try
            # again in 30.284s"); a fixed 2/4/6s backoff is often shorter
            # than that wait, so a retry can land before the budget frees
            # and hit the same 429 again, burning all 3 attempts. Waiting
            # the indicated time (plus a small safety margin, capped at
            # 65s so one retry can't stall the whole run) instead.
            wait_s = 2 * attempt
            m = re.search(r"try again in (\d+(?:\.\d+)?)s", str(e))
            if m:
                wait_s = max(wait_s, float(m.group(1)) + 1)
            time.sleep(min(wait_s, 65))
    print("Returning empty string after repeated OpenAI failures.")
    return ""


def _extract_clean_table(raw: str, min_cols: int = 2) -> str:
    """
    Extract only the pipe-separated lines from an LLM response.
    Keeps header/body rows that contain '|' and at least 'min_cols' parts.
    """
    lines = []
    for line in (raw or "").splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= min_cols and any(parts):
            # Rebuild with single '|' as separators, trimmed cells
            lines.append("|".join(parts))
    return "\n".join(lines)

def _read_csv_robust_encoding(path: Path) -> pd.DataFrame:
    """Reads a user-supplied CSV that may not actually be UTF-8 -- e.g. a
    file re-saved by Excel is very commonly Windows-1252/Latin-1, which
    pandas' default utf-8 read chokes on for any byte outside plain ASCII
    (accented characters, curly quotes, the degree sign, etc.). Tries a
    short, real list of common encodings in order and uses the first one
    that actually parses; never invents or drops data, just picks the
    right way to decode the same real bytes already on disk."""
    last_err = None
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError as e:
            last_err = e
            continue
    # Last resort: utf-8 with invalid bytes replaced, so a run isn't
    # blocked entirely by one stray byte the encodings above still choke on.
    print(f"Could not decode {path} with utf-8/utf-8-sig/cp1252/latin-1 ({last_err}); "
          f"falling back to utf-8 with invalid bytes replaced.")
    return pd.read_csv(path, encoding="utf-8", encoding_errors="replace")


def extract_elements(observed_path: Path):
    df = _read_csv_robust_encoding(observed_path)
    cols = [c for c in df.columns if c.lower().startswith("element")]
    if not cols:
        raise ValueError(f"No 'Element' column found in {observed_path}")
    elements = (
        df[cols[0]]
        .astype(str)
        .map(lambda x: x.strip())
        .replace({"nan": ""})
        .dropna()
        .unique()
    )
    return elements, df


_KB_CACHE = {"immunexpresso": None, "cytokine_registry": None,
             "organism_taxonomy": None, "organism_taxonomy_synonyms": None}

# Cytokine/protein names commonly use Greek-letter subscripts (IL-1alpha,
# TNF-alpha, IFN-gamma, ...). Transliterate these to Latin BEFORE stripping
# non-alphanumerics, otherwise e.g. "IL-1alpha" and "IL-1beta" both collapse
# to the same normalized string ("il1") and silently collide/overwrite each
# other in norm_map -- a real bug hit on CaseStudy's "IL-1alpha"/"IL-1beta".
_GREEK_TO_LATIN = str.maketrans({
    "α": "a", "β": "b", "γ": "g", "δ": "d", "ε": "e",
    "ζ": "z", "η": "h", "θ": "th", "ι": "i", "κ": "k",
    "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o",
    "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t",
    "υ": "u", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "w",
})

@lru_cache(maxsize=None)
def _norm_name(x: str) -> str:
    """Lowercase, transliterate Greek letters to Latin (so IL-1α vs
    IL-1β don't collide), then strip everything but letters/digits, for
    name matching.

    Cached (lru_cache) -- this is a pure function of its input string,
    called per-row against real KB dataframes inside
    find_kb_neighborhood_edges for every element in a call, and the same
    small set of real KB values repeats across calls; the cache turns
    that from a fresh regex+translate on every call into a dict lookup
    after the first. Purely a speed optimization -- doesn't change what
    any function returns, and nothing about Prompt 2's own text."""
    s = str(x or "").lower().translate(_GREEK_TO_LATIN)
    return re.sub(r"[^a-z0-9]", "", s)


# ─────────────────── Cell Ontology live lookup (cell-type name synonyms) ───
# Live query against the EBI Ontology Lookup Service's Cell Ontology (CL)
# -- the same standard ontology ImmuneXpresso's own "Cell Ontology ID"/
# "Cell Ontology Label" columns already reference elsewhere in this
# pipeline -- rather than a hand-typed abbreviation dict, so cell-type
# synonym coverage generalizes to any cell type without a code change.
# `exact=true` + `queryFields=label,synonym` (documented OLS4 search
# parameters) restrict matches to an exact string match on CL's own
# official label or synonym -- never a loose full-text relevance guess --
# so a query for a non-cell-type element (a cytokine, a microbe) correctly
# returns no match rather than a spurious one. Same caching convention as
# _fetch_uniprot_function below: permanent local disk cache (cell-type
# nomenclature doesn't change run to run) plus an in-memory cache for the
# life of one process run. Returns {} on any failure or no match -- never
# invents a synonym; callers must treat that as "no expansion available."
CL_CACHE_DIR = HERE / "cl_cache"
OLS_SEARCH_BASE = "https://www.ebi.ac.uk/ols4/api/search"
_CL_LOOKUP_MEMORY_CACHE = {}

def _fetch_cell_ontology_synonyms(term: str) -> dict:
    """Live EBI OLS query for `term`, restricted to the Cell Ontology (CL),
    exact-match only. Returns {'label': <official CL term>, 'synonyms':
    [...], 'obo_id': 'CL:0000235'} for the top exact match, or {} if
    nothing matched or the request failed."""
    key = _norm_name(term)
    if not key:
        return {}
    if key in _CL_LOOKUP_MEMORY_CACHE:
        return _CL_LOOKUP_MEMORY_CACHE[key]

    cache_file = CL_CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            result = json.loads(cache_file.read_text(encoding="utf-8"))
            _CL_LOOKUP_MEMORY_CACHE[key] = result
            return result
        except Exception:
            pass  # corrupt cache file -- fall through and refetch

    params = {"q": term, "ontology": "cl", "exact": "true",
              "queryFields": "label,synonym", "rows": "3"}
    url = f"{OLS_SEARCH_BASE}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        docs = data.get("response", {}).get("docs", []) or []
        top = docs[0] if docs else {}
        result = {
            "label": top.get("label", ""),
            "synonyms": top.get("synonym", []) or [],
            "obo_id": top.get("obo_id", ""),
        } if top else {}
    except Exception as e:
        print(f"Cell Ontology lookup failed for '{term}' ({e}); skipping cell-type expansion.")
        result = {}

    _CL_LOOKUP_MEMORY_CACHE[key] = result
    try:
        ensure_dir(CL_CACHE_DIR)
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass  # caching is an optimization only -- a write failure shouldn't break the run
    return result


# Guards the substring-containment fallback below against a SHORT KB label
# (e.g. the 3-letter cytokine gene symbol "LTA") matching as a coincidental
# substring buried inside a much LONGER, biologically unrelated element
# name -- e.g. "Bacteroidetes bacterium oral taxon 272" normalizes to
# "...bacteriumORALTAxon272", which contains "lta" purely by accident of
# spelling ("oral" + "taxon"), so an unguarded "norm_val in key" match
# would relabel a "B cell -- LTA" ImmuneXpresso record as
# "B-cell -- Bacteroidetes bacterium oral taxon 272" in Table 3. A scan of
# Database/ImmuneXpressoResults_Interactions.csv against Table 1 elements
# found this class of false match at length ratios (longer string /
# shorter string) of 8.3-11.3, while every needed substring match in this
# pipeline (e.g. 'T cell' vs 'T cells', 'IFNgamma' vs 'IFNG') has a ratio
# no higher than 6.4. _SUBSTRING_MATCH_MAX_RATIO sits between those two
# measured ranges, rejecting the bad case without breaking real matches.
_SUBSTRING_MATCH_MAX_RATIO = 7.5

def _substring_match_ok(a: str, b: str) -> bool:
    """True if `a` and `b` (already-normalized strings) may be treated as
    the same real-world entity via substring containment -- both sides
    must be a real name fragment (>=3 chars) AND not wildly different in
    length (see _SUBSTRING_MATCH_MAX_RATIO's docstring above for why: a
    short real symbol like 'LTA' can appear as a pure spelling coincidence
    inside an unrelated long name, e.g. bacteria species names containing
    'oral taxon')."""
    if len(a) < 3 or len(b) < 3:
        return False
    if not (a in b or b in a):
        return False
    longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
    return (len(longer) / len(shorter)) <= _SUBSTRING_MATCH_MAX_RATIO

def _match_norm(norm_val: str, norm_map: dict):
    """Match a normalized KB name against the normalized Table3 element
    names. Exact match first; falls back to substring containment (either
    direction) for naming variants (e.g. 'T cell' vs 'T cells', 'IFNgamma'
    vs 'IFNG'), then to a live Cell Ontology synonym lookup for that
    element (see _fetch_cell_ontology_synonyms) -- e.g. the KB might say
    'T-helper 17 cell' while the element list says 'Th17'; a real CL query
    for 'Th17' resolves its official synonyms rather than relying on a
    hand-typed abbreviation table. The substring fallback is guarded by
    _substring_match_ok (see its docstring) so a short real KB label can't
    be falsely matched just because it happens to appear inside an
    unrelated, much longer element name. Returns the matching key into
    norm_map, or None."""
    if not norm_val:
        return None
    if norm_val in norm_map:
        return norm_val
    for key in norm_map:
        if _substring_match_ok(key, norm_val):
            return key
    for key, original_name in norm_map.items():
        hit = _fetch_cell_ontology_synonyms(original_name)
        if not hit:
            continue
        for candidate in [hit.get("label", "")] + list(hit.get("synonyms", [])):
            nc = _norm_name(candidate)
            if nc and _substring_match_ok(nc, norm_val):
                return key
    return None

def _load_cytokine_registry() -> pd.DataFrame:
    if _KB_CACHE["cytokine_registry"] is not None:
        return _KB_CACHE["cytokine_registry"]
    if not KB_CYTOKINE_REGISTRY_FILE.exists():
        print(f"KB file not found (skipping): {KB_CYTOKINE_REGISTRY_FILE}")
        df = pd.DataFrame()
    else:
        try:
            df = pd.read_excel(KB_CYTOKINE_REGISTRY_FILE)
            df.columns = [str(c).strip() for c in df.columns]
            df["_SymNorm"] = df.get("EntrezGene Symbol (Human)", pd.Series(dtype=str)).map(_norm_name)
        except Exception as e:
            print(f"Could not read {KB_CYTOKINE_REGISTRY_FILE}: {e}")
            df = pd.DataFrame()
    _KB_CACHE["cytokine_registry"] = df
    return df

def _registry_species_columns(sample_model: str = None) -> dict:
    """Resolve the ImmPort Cytokine Registry's column names for the
    requested species (SAMPLE_MODEL=Human/Mouse). The registry's Mouse
    columns exist but aren't uniformly populated, and the Mouse UniProt
    column is even named inconsistently ('UniProtID (Mouse)' vs the
    Human side's 'UniProtDB ID (Human)') -- callers should still fall back
    to Human per-row if a Mouse cell is blank for a given gene, since
    'Mouse selected' shouldn't silently drop coverage that only exists on
    the Human side of the same row."""
    model = (sample_model or SAMPLE_MODEL or "Human").strip().lower()
    if model == "mouse":
        return {
            "symbol": "EntrezGene Symbol (Mouse)", "official_name": "EntrezGeneofficial name (Mouse)",
            "aliases": "EntrezGene Aliases (Mouse)", "uniprot_id": "UniProtID (Mouse)",
            "uniprot_name": "UniProt protein name (Mouse)",
        }
    return {
        "symbol": "EntrezGene Symbol (Human)", "official_name": "EntrezGene official name (Human)",
        "aliases": "EntrezGene Aliases (Human)", "uniprot_id": "UniProtDB ID (Human)",
        "uniprot_name": "UniProt protein name (Human)",
    }

def _build_alias_norm_index(reg: pd.DataFrame, aliases_col: str) -> dict:
    """Explodes a semicolon-separated 'EntrezGene Aliases (...)' column into
    {normalized_alias: row_index} (first row wins if the same real alias
    somehow appears on more than one row -- rare, and no worse than the
    existing first-row-wins convention used elsewhere in this file, e.g.
    _build_element_alias_map). Used as a second-pass, still-exact match in
    find_gene_identity_info (below) for elements written under a trivial/
    common name rather than the official gene symbol -- e.g. 'MIP-1δ'
    doesn't equal gene symbol 'CCL15', but CCL15's own real, curated
    Aliases column (confirmed directly against the registry file) lists
    'MIP-1D' and 'MIP-1 delta' verbatim. Still exact-string matching
    against a real, curated alias list -- not substring/fuzzy guessing --
    so it doesn't reintroduce the wrong-gene risk this function's
    docstring already warns about for symbol matching."""
    index = {}
    if aliases_col not in reg.columns:
        return index
    for idx, raw in reg[aliases_col].items():
        for alias in str(raw or "").split(";"):
            alias = alias.strip()
            if not alias:
                continue
            key = _norm_name(alias)
            if key and key not in index:
                index[key] = idx
    return index

def find_gene_identity_info(elements, sample_model: str = None) -> dict:
    """For each Table3 element that matches a gene symbol (or, failing
    that, a real curated alias -- see _build_alias_norm_index) in the
    ImmPort Cytokine Registry (species per SAMPLE_MODEL, Human by
    default), return {element_name: {gene_symbol, official_name,
    uniprot_name, uniprot_id, protein_ontology_name, synonyms (list)}}.
    Matching is exact-normalized (no substring/alias fuzzing -- gene
    symbols/aliases are short, e.g. 'IL6' vs 'IL6R', so anything looser
    risks matching the wrong gene). Falls back to the Human symbol/alias
    columns per-element if Mouse is selected but that row has no Mouse
    value populated.

    The alias fallback matters because a symbol-only match misses trivial
    names: element 'MIP-1δ' has gene symbol 'CCL15' (a completely
    different string), so without it 'MIP-1δ' would get zero registry
    synonyms and its PubMed query would only ever search that literal
    string. CCL15's row lists 'MIP-1D'/'MIP-1 delta' in its Aliases
    column, so matching against aliases too resolves 'MIP-1δ' to the
    same row a search for 'CCL15' would find."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    reg = _load_cytokine_registry()
    out = {}
    if reg.empty or not elements:
        return out
    cols = _registry_species_columns(sample_model)
    human_cols = _registry_species_columns("Human")
    alias_index = _build_alias_norm_index(reg, cols.get("aliases", ""))
    human_alias_index = (
        alias_index if cols is human_cols else _build_alias_norm_index(reg, human_cols.get("aliases", ""))
    )
    for e in elements:
        key = _norm_name(e)
        use_cols = cols
        hit = reg[reg[cols["symbol"]].map(_norm_name) == key] if cols["symbol"] in reg.columns else reg.iloc[0:0]
        if hit.empty and cols is not human_cols:
            hit = reg[reg["_SymNorm"] == key]  # fall back to Human (precomputed column)
            use_cols = human_cols
        if hit.empty and key in alias_index:
            hit = reg.loc[[alias_index[key]]]
            use_cols = cols
        if hit.empty and cols is not human_cols and key in human_alias_index:
            hit = reg.loc[[human_alias_index[key]]]
            use_cols = human_cols
        if hit.empty:
            continue
        row = hit.iloc[0]
        syn_raw = str(row.get("Protein Ontology synonyms", "") or "")
        synonyms = [s.strip() for s in syn_raw.split(";") if s.strip()]
        out[e] = {
            "gene_symbol": str(row.get(use_cols["symbol"], "")).strip(),
            "official_name": str(row.get(use_cols["official_name"], "")).strip(),
            "uniprot_name": str(row.get(use_cols["uniprot_name"], "")).strip(),
            "uniprot_id": str(row.get(use_cols["uniprot_id"], "")).strip(),
            "protein_ontology_name": str(row.get("Protein Ontology name", "")).strip(),
            "synonyms": synonyms,
        }
    return out


# ─────────────────── PubMed evidence (Prompt 1 / Table 1) ──────────────────
# Real per-element evidence, replacing the old GPT-memory-only Prompt A/B.
# Flow: one real PubMed search per element (esearch) -> real abstract text
# for the hits (efetch) -> abstracts split into small batches -> each batch
# read ONCE, statelessly, by PROMPT_PUBMED_EXTRACT (no batch sees any other
# batch's results) -> batch outputs (PMID, Direction) tallied together in
# plain Python (build_table1_evidence), never by asking the LLM to track a
# running total. PMID is the permanent citation; raw abstract text itself
# is cached locally only as a speed optimization (like uniprot_cache_v2/), not
# because it needs to be kept -- anyone can always re-fetch by PMID.
PUBMED_CACHE_DIR = HERE / "pubmed_cache"
PUBMED_BATCH_SIZE = 20      # abstracts per LLM extraction call (kept small for reliability)
PUBMED_DISEASE_TERMS = ['periodontitis[tiab]', '"periodontal disease"[tiab]', '"gum disease"[tiab]']
# Coverage guarantee threshold (see fetch_ranked_combined_pool): an element
# needs at least this many mentions in the combined top-N pool to count as
# genuinely "covered" -- below that, it also gets its own small dedicated
# fallback search (fetch_pubmed_abstracts_for_element), same as a
# zero-mention element. A single tangential mention in the combined pool
# is not always enough for the LLM to report a direction for that element
# across all extraction runs, so requiring at least 3 mentions before
# skipping the fallback catches thinly-covered elements the pool alone
# would leave with empty evidence. This is a "try harder" threshold, not
# a guarantee: an element whose real PubMed literature is genuinely
# thinner than 3 abstracts under this pipeline's search terms will still
# show fewer than 3 Total Abstracts Screened -- this pipeline never
# fabricates an abstract to hit a quota. Raise further to be more
# thorough at the cost of more PubMed calls for elements that already
# have several fine hits.
COMBINED_POOL_MIN_MENTIONS_PER_ELEMENT = 3
NCBI_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

def _ncbi_common_params() -> dict:
    p = {"tool": "BioShift", "email": "bioshift@example.com"}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p

def _pubmed_esearch(query: str, retmax: int = 100) -> list:
    """Real PubMed search (esearch) -- returns a list of real PMIDs matching
    `query`, ranked by relevance. Returns [] on any failure (network down,
    bad query, timeout) -- callers must treat that as 'no evidence found',
    never fabricate PMIDs.

    Sent as a POST (form body), not a GET with the query in the URL: the
    COMBINED query (all elements' name variants OR'd together -- see
    _build_combined_pubmed_query) can run several KB of text once a
    sample has 20+ elements with multiple gene/protein synonyms each,
    which triggers HTTP 414 'Request-URI Too Long' as a GET. NCBI's own
    E-utilities docs recommend POST for exactly this case; there is no
    equivalent length limit on the request body."""
    params = {"db": "pubmed", "term": query, "retmode": "json",
              "retmax": str(retmax), "sort": "relevance", **_ncbi_common_params()}
    url = f"{NCBI_EUTILS_BASE}/esearch.fcgi"
    try:
        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result.get("esearchresult", {}).get("idlist", []) or []
    except Exception as e:
        print(f"PubMed esearch failed for query '{query}' ({e}); returning no PMIDs.")
        return []

def _pubmed_efetch(pmids: list) -> dict:
    """Real PubMed abstract fetch (efetch) for a list of PMIDs -- returns
    {pmid: {'title': ..., 'abstract': ..., 'pub_types': [...]}}. 'pub_types'
    is PubMed's own real <PublicationType> list for that article (e.g.
    "Randomized Controlled Trial", "Journal Article") -- used later to rank
    evidence strength, never invented. Batches the request in groups of 200
    (NCBI's recommended max per call). Returns {} on total failure; partial
    per-batch failures just skip that batch's PMIDs (never invents text for
    a PMID it couldn't fetch)."""
    out = {}
    pmids = [str(p).strip() for p in pmids if str(p).strip()]
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i + 200]
        params = {"db": "pubmed", "id": ",".join(chunk), "rettype": "abstract",
                   "retmode": "xml", **_ncbi_common_params()}
        url = f"{NCBI_EUTILS_BASE}/efetch.fcgi?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            for art in root.findall(".//PubmedArticle"):
                pmid_el = art.find(".//PMID")
                pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""
                if not pmid:
                    continue
                title_el = art.find(".//ArticleTitle")
                title = "".join(title_el.itertext()).strip() if title_el is not None else ""
                abs_parts = []
                for ab in art.findall(".//Abstract/AbstractText"):
                    label = ab.get("Label")
                    text = "".join(ab.itertext()).strip()
                    abs_parts.append(f"{label}: {text}" if label else text)
                abstract = " ".join(abs_parts).strip()
                pub_types = [
                    "".join(pt.itertext()).strip()
                    for pt in art.findall(".//PublicationTypeList/PublicationType")
                ]
                pub_types = [pt for pt in pub_types if pt]
                if abstract:
                    out[pmid] = {"title": title, "abstract": abstract, "pub_types": pub_types}
            time.sleep(0.34 if not NCBI_API_KEY else 0.11)  # respect 3/sec (or 10/sec with key)
        except Exception as e:
            print(f"PubMed efetch failed for a batch of {len(chunk)} PMIDs ({e}); skipping.")
            continue
    return out

# The ImmPort Cytokine Registry only covers cytokines/genes, so without
# this, a cell-type acronym like APC has NO synonym expansion at all and
# searches/matches miss every abstract that spells the term out instead of
# using the acronym (common -- many papers use the acronym only after
# first spelling it out, or use the spelled-out form throughout). Uses the
# same live Cell Ontology lookup as _match_norm (see
# _fetch_cell_ontology_synonyms above) instead of a hand-typed acronym
# table, so it generalizes to any real cell type, not just the ones
# someone remembered to add to a dict.
def _acronym_expansions(name: str) -> list:
    """If `name` resolves to a real Cell Ontology term (see
    _fetch_cell_ontology_synonyms), return its official label plus any
    real CL synonyms, each in singular/plural and hyphenated/unhyphenated
    form -- otherwise [] (correct for non-cell-type elements: cytokines,
    microbes, etc. simply get no expansion here)."""
    hit = _fetch_cell_ontology_synonyms(name)
    if not hit:
        return []
    names = [n for n in [hit.get("label", "")] + list(hit.get("synonyms", [])) if n]
    out = []
    for n in names:
        unhyphenated = n.replace("-", " ")
        out.extend([n, n + "s", unhyphenated, unhyphenated + "s"])
    return [o for o in dict.fromkeys(out)]

def _pluralize_variant(name: str) -> list:
    """Simple, standard English singular<->plural counterpart for a name
    (e.g. 'APC'<->'APCs', 'macrophage'<->'macrophages') -- NOT full
    stemming, just the common 's'/'es' pattern, so word-boundary text
    matching and PubMed phrase search don't miss the other form."""
    n = name.strip()
    if not n:
        return []
    low = n.lower()
    if low.endswith("s"):
        singular = n[:-2] if low.endswith("es") and len(n) > 2 else n[:-1]
        return [singular] if singular else []
    else:
        suffix = "es" if low.endswith(("s", "x", "z", "ch", "sh")) else "s"
        return [n + suffix]

def _build_element_alias_map(elements: list) -> dict:
    """Maps a normalized alias -> canonical master-list element string.
    Used to validate the LLM's reported "Element" cell in combined-pool
    extraction without being broken by harmless differences from the
    exact master-list spelling -- case, spacing/hyphenation, or singular
    vs. plural (e.g. the master list says 'APCs' but the LLM writes
    'APC') -- even though the prompt asks for an exact copy, LLMs don't
    always comply on minor formatting, and rejecting those near-misses
    was silently discarding real, correct extractions. Includes each
    element's own normalized form plus its singular/plural counterpart."""
    alias_map = {}
    for e in elements:
        for alias in [e] + _pluralize_variant(e):
            key = _normalize_for_match(alias)
            if key and key not in alias_map:
                alias_map[key] = e
    return alias_map

# Element-specific supplementary search terms -- not algorithmically
# inferred, each entry is a hand-verified addition (see the entry's own
# comment), since widening beyond an element's own exact name/registry
# aliases always trades some precision for recall and shouldn't happen
# silently. Applied as extra OR terms in _element_name_variants, keyed by
# _norm_name(element).
_ELEMENT_SUPPLEMENTARY_SEARCH_TERMS = {
    # "PDGF" as a bare symbol has no EntrezGene Symbol/Aliases entry in
    # the ImmPort registry (only its specific genes PDGFA/PDGFB/PDGFRA/
    # PDGFRB are listed). The registry does have a separate family-level
    # row (UNIQUE ID CID_150, "Platelet-Derived Growth Factor",
    # MeSH ID D010982) whose "Typographical variations"/"IX Synonyms"
    # columns list "PDGF" -- but find_gene_identity_info/
    # _build_alias_norm_index only read EntrezGene Symbol/Aliases, so
    # that entry is otherwise invisible to this pipeline. Adding the
    # spelled-out synonym directly here gets it into both the PubMed
    # search query and the co-occurrence/quote matching without also
    # teaching the alias-index reader a third registry column.
    _norm_name("PDGF"): ["platelet-derived growth factor"],
}

def _element_name_variants(element: str, gene_info: dict = None) -> list:
    """Name variants for one element: itself, plus known ImmPort Cytokine
    Registry synonyms/official name if it's a cytokine/gene with any (so
    a search for 'IL-6' also catches papers that only say 'B-cell
    stimulatory factor 2'), plus standard cell-type acronym expansions
    (e.g. 'APC' -> 'antigen-presenting cell'/'antigen presenting cell'),
    plus any explicit, hand-curated supplementary terms from
    _ELEMENT_SUPPLEMENTARY_SEARCH_TERMS, plus (for microbe elements) an
    NCBI-verified reclassified/synonym name from organism_taxonomy_ids.csv
    (see _load_organism_taxonomy_synonyms -- e.g. 'Lactobacillus panis'
    also searches 'Limosilactobacillus panis', the genus NCBI now uses),
    plus a singular/plural counterpart for every name collected so far
    (standard English 's'/'es' pluralization only). Used both to build
    PubMed queries and to detect which elements a given abstract actually
    mentions."""
    names = [element]
    if gene_info and element in gene_info:
        names.extend(gene_info[element].get("synonyms", []) or [])
        official = gene_info[element].get("official_name", "")
        if official:
            names.append(official)
    names.extend(_acronym_expansions(element))
    names.extend(_ELEMENT_SUPPLEMENTARY_SEARCH_TERMS.get(_norm_name(element), []))
    organism_synonym = _load_organism_taxonomy_synonyms().get(element)
    if organism_synonym:
        names.append(organism_synonym)
    plural_extra = []
    for n in names:
        plural_extra.extend(_pluralize_variant(n))
    names.extend(plural_extra)
    return [n for n in dict.fromkeys(n.strip() for n in names) if n]  # dedupe, keep order

def _build_pubmed_query(element: str, gene_info: dict = None) -> str:
    """Real search query for one element: its name variants OR'd together,
    AND'd with the real study-context terms (disease/disease stage/tissue
    site/host species/experimental modality -- see _build_disease_clause),
    falling back to the static periodontitis/gum-disease terms if none of
    those study-context fields are actually filled in."""
    names = _element_name_variants(element, gene_info)
    name_clause = " OR ".join(f'"{n}"[tiab]' for n in names)
    disease_clause = _build_disease_clause()
    return f"({name_clause}) AND ({disease_clause})"

def _build_combined_pubmed_query(elements: list, gene_info: dict = None) -> str:
    """Real search query covering ALL elements at once: every element's
    name variants OR'd together (across all elements), AND'd with the real
    study-context terms (disease/disease stage/tissue site/host species/
    experimental modality -- see _build_disease_clause) -- used for the
    shared, relevance-ranked retrieval pool (see fetch_ranked_combined_pool)
    rather than fetching each element separately."""
    all_names = []
    for elem in elements:
        all_names.extend(_element_name_variants(elem, gene_info))
    all_names = [n for n in dict.fromkeys(all_names) if n]
    name_clause = " OR ".join(f'"{n}"[tiab]' for n in all_names)
    disease_clause = _build_disease_clause()
    return f"({name_clause}) AND ({disease_clause})"

def _find_mentioned_elements(text: str, elements: list, gene_info: dict = None) -> list:
    """Which of `elements` are REALLY named (by exact name or a known
    synonym) in `text` -- real, case-insensitive, word-boundary text
    matching, never a guess. Used to rank abstracts by real co-occurrence
    count and to tell the extraction prompt which elements are actually
    worth asking about for a given abstract."""
    low = text.lower()
    found = []
    for elem in elements:
        variants = _element_name_variants(elem, gene_info)
        for v in variants:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(v.lower()) + r"(?![A-Za-z0-9])", low):
                found.append(elem)
                break
    return found

def _normalize_for_match(text: str) -> str:
    """Lowercase, transliterate Greek letters to Latin (see
    _GREEK_TO_LATIN -- same table _norm_name already uses for real KB
    row matching, reused here for consistency), strip quote marks/
    punctuation, collapse whitespace. Used to (1) compare a quoted-
    evidence string against the real abstract text it's supposed to come
    from, tolerant of the LLM swapping straight/smart quotes or minor
    spacing when it copies, and (2) resolve an LLM-reported element name
    back to the master list (_build_element_alias_map).

    The Greek-letter transliteration matters because a bare regex
    collapse deletes a Greek letter as "not [a-z0-9]": without it, both
    'IL-1α' and 'IL-1β' would strip down to the same key ('il 1'), so
    _build_element_alias_map's normalized-key dict could only hold one
    of them and the other's rows would silently resolve to the wrong
    element. Transliterating first ('α'->'a') keeps them distinguishable
    ('il 1a' vs 'il 1b')."""
    text = text.lower()
    text = re.sub(r"[‘’“”'\"`]", "", text)
    text = text.translate(_GREEK_TO_LATIN)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()

def _tight_norm(text: str) -> str:
    """_normalize_for_match, with internal spaces also removed -- so
    'IL-6' ('il 6') and 'IL6' ('il6') compare equal. Without this, a name
    with a hyphen (as an LLM might write it) and the same name without
    one (as UniProt data literally spells it) would normalize to
    different strings under plain _normalize_for_match and never compare
    equal, so a genuinely correct match would look ungrounded. Only used
    for comparing two short, DISCRETE names against each other (e.g. an
    extra element name against a KB record's own known participant
    names) -- deliberately NOT used against a long block of quote/
    abstract text, where stripping every space first would risk
    spurious mid-sentence substring collisions (e.g. 'April 6' -> 'april6'
    contains 'il6')."""
    return _normalize_for_match(text).replace(" ", "")

def _quote_is_grounded(quote: str, source_text: str) -> bool:
    """Real grounding check: is `quote` actually present (after light
    normalization) in `source_text`? This is the anti-fabrication
    safeguard for the combined-pool extraction -- instead of restricting
    the LLM to a pre-computed candidate-element list (which can have
    false negatives from imperfect synonym matching and blocks the model
    from using its own reading of the text), we let the LLM report on
    any element it genuinely finds discussed, and verify honesty
    afterward by confirming its quoted evidence is real, exact text from
    that specific abstract -- a fabricated or misattributed quote will
    not survive this check."""
    quote = (quote or "").strip()
    if not quote:
        return False
    q = _normalize_for_match(quote)
    t = _normalize_for_match(source_text)
    if not q:
        return False
    if q in t:
        return True
    # Tolerate the LLM trimming/ellipsis-ing a long quote: require a
    # substantial leading chunk of it to be real, verbatim text rather
    # than the whole thing.
    chunk = q[:60] if len(q) > 60 else q
    return len(chunk) >= 15 and chunk in t

def _disease_context_bare_terms() -> list:
    """Same real, filled-in study-context values as
    _study_context_pubmed_terms (Disease, Disease Stage, Tissue/Site
    Specificity), PLUS the static periodontitis-name synonym terms
    (PUBMED_DISEASE_TERMS -- "periodontitis"/"periodontal disease"/"gum
    disease"), WITHOUT the '"..."[tiab]' PubMed query wrapper -- plain
    phrases, for real text-proximity checking against an abstract's own
    text (see _quote_has_nearby_disease_context) rather than for building
    a search query. Kept in sync with _build_disease_clause's own
    same union-not-either/or change (see that function's docstring) --
    this is only a soft, printed-NOTE flag (never a hard reject), but
    should still recognize the same broadened set of real disease-name
    phrasings _build_disease_clause's retrieval now searches for."""
    ctx = load_study_context()
    wanted = ["Disease", "Disease Stage", "Tissue/Site Specificity"]
    terms = []
    for key in wanted:
        val = str(ctx.get(key, "")).strip()
        if not val or val.lower() == "unknown" or "(fill in config.txt)" in val.lower():
            continue
        terms.append(val)
    static_terms = [re.sub(r"\[tiab\]$", "", t).strip('"') for t in PUBMED_DISEASE_TERMS]
    return list(dict.fromkeys(terms + static_terms))

def _quote_has_nearby_disease_context(quote: str, source_text: str) -> bool:
    """Soft, code-only structural check for a risk quote-grounding alone
    can't catch: a quote can be real, verbatim text from an abstract that
    still isn't about this study's own disease context -- e.g. the
    abstract's introduction/discussion citing a different, unrelated
    study's finding (that sentence is still real text, so
    _quote_is_grounded correctly accepts it as genuine even though it was
    never this paper's own on-topic result).

    Checks whether any of this study's own disease-context terms
    (_disease_context_bare_terms -- the same values used to build the
    PubMed query's own AND clause) appear in the SAME naively-split
    sentence as the quote. Sentence-level, not a flat character window --
    a flat window was tested and found too generous, still matching the
    disease term in the next sentence over even for a genuinely off-topic
    quote.

    Deliberately a soft signal, not a hard filter: returns a bool for the
    caller to log a warning with, never to silently drop a row on. Two
    sources of false positives are accepted on purpose rather than
    guarded against: (1) a genuinely correct quote whose own sentence
    just doesn't happen to repeat the disease name (common in
    results-heavy writing), and (2) the naive '.'/'!'/'?' sentence
    splitter mis-splitting on an abbreviation (e.g. 'P. gingivalis'),
    treated as inconclusive (returns False, so it also just gets
    flagged) rather than guessed at. A silent hard-reject on either would
    quietly delete real evidence, so this stays a flag, not a filter."""
    sentences = re.split(r"(?<=[.!?])\s+", source_text.strip())
    q_norm = _normalize_for_match(quote)
    if not q_norm:
        return False
    terms_norm = [t for t in (_normalize_for_match(term) for term in _disease_context_bare_terms()) if t]
    hit_idx = [i for i, s in enumerate(sentences) if q_norm in _normalize_for_match(s)]
    if not hit_idx:
        return False  # quote didn't land cleanly in one naive-split sentence -- inconclusive, flag it
    return any(term in _normalize_for_match(sentences[i]) for term in terms_norm for i in hit_idx)

def fetch_pubmed_abstracts_for_element(element: str, gene_info: dict = None) -> list:
    """Real, once-per-element PubMed search + fetch. Returns a list of
    {pmid, title, abstract} dicts, capped at PUBMED_MAX_ABSTRACTS total per
    element (fewer if fewer real papers exist for that element -- never
    padded; each element searches independently, so this cap does not
    trade coverage off between elements). Cached to disk per element+query
    so reruns don't re-hit the API (abstract text doesn't change run to
    run)."""
    query = _build_pubmed_query(element, gene_info)
    cache_key = re.sub(r"[^a-zA-Z0-9]+", "_", element).strip("_").lower()[:80]
    cache_file = PUBMED_CACHE_DIR / f"{cache_key}.json"
    if PUBMED_USE_CACHE and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("query") == query:
                print(f"[{element}] Using cached PubMed abstracts: {len(cached.get('abstracts', []))} "
                      f"found (cache: {cache_file})")
                return cached.get("abstracts", [])
        except Exception:
            pass  # corrupt cache -- fall through and refetch
    elif not PUBMED_USE_CACHE:
        print(f"[{element}] PUBMED_USE_CACHE=false -- skipping cache, making a fresh real PubMed call.")

    print(f"[{element}] Searching PubMed: {query}")
    cap = PUBMED_MAX_ABSTRACTS
    pmids = _pubmed_esearch(query, retmax=cap)
    print(f"[{element}] esearch found {len(pmids)} PMID(s) (cap {cap}).")
    if not pmids:
        result = []
    else:
        print(f"[{element}] Fetching abstract text for {len(pmids)} PMID(s)...")
        fetched = _pubmed_efetch(pmids)
        # Preserve esearch's relevance order; drop any PMID efetch couldn't
        # return real text for (never fabricate an abstract).
        result = [{"pmid": pmid, **fetched[pmid]} for pmid in pmids if pmid in fetched]
        missing = len(pmids) - len(result)
        print(f"[{element}] Retrieved {len(result)} real abstract(s)"
              + (f" ({missing} PMID(s) had no fetchable abstract text -- skipped, not invented)."
                 if missing else "."))

    if PUBMED_USE_CACHE:
        try:
            ensure_dir(PUBMED_CACHE_DIR)
            cache_file.write_text(json.dumps({"query": query, "abstracts": result}, ensure_ascii=False),
                                   encoding="utf-8")
            print(f"[{element}] Saved {len(result)} abstract(s) to cache: {cache_file}")
        except Exception as e:
            print(f"[{element}] Could not write cache file {cache_file}: {e}")
    return result

def _format_abstracts_block(batch: list) -> str:
    parts = []
    for a in batch:
        block = f"PMID: {a['pmid']}\nTitle: {a.get('title', '')}\nAbstract: {a.get('abstract', '')}"
        if a.get("matched_elements"):
            block += f"\nPossible elements mentioned in this abstract: {', '.join(a['matched_elements'])}"
        parts.append(block)
    return "\n\n".join(parts)

def fetch_ranked_combined_pool(elements: list, gene_info: dict = None, out_dir: Path = None) -> list:
    """Combined-search retrieval strategy: ONE real PubMed search covering
    ALL elements at once (element1 OR element2 OR ... AND periodontitis),
    pulling a larger real pool (PUBMED_SEARCH_POOL_SIZE -- cheap, real API
    calls only, no LLM cost). Every fetched abstract is then scored by how
    many of the real input elements it actually mentions (_find_mentioned_
    elements -- real text matching, not PubMed's own relevance score), and
    the pool is ranked by that count so abstracts covering MULTIPLE
    elements (the strongest real co-shift evidence) float to the top.

    Only the top PUBMED_MAX_ABSTRACTS (by this ranking) go on to the
    expensive LLM extraction step. Coverage guarantee: any element with
    ZERO mentions anywhere in that top selection gets its own small
    fallback search (fetch_pubmed_abstracts_for_element) appended, so an
    element never silently drops out just for losing the ranking -- it's
    reported with real (if sparse) evidence instead.

    Returns a list of {pmid, title, abstract, matched_elements} dicts,
    each matched_elements being the real elements found in that abstract's
    own text."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    query = _build_combined_pubmed_query(elements, gene_info)
    cache_key = "combined_" + re.sub(r"[^a-zA-Z0-9]+", "_", "_".join(sorted(elements))).strip("_").lower()[:120]
    cache_file = PUBMED_CACHE_DIR / f"{cache_key}.json"
    pool = None
    if PUBMED_USE_CACHE and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("query") == query and cached.get("pool_size") == PUBMED_SEARCH_POOL_SIZE:
                pool = cached.get("pool", [])
                print(f"Using cached combined PubMed pool: {len(pool)} abstract(s) "
                      f"(cache: {cache_file})")
        except Exception:
            pass  # corrupt cache -- fall through and refetch
    elif not PUBMED_USE_CACHE:
        print("PUBMED_USE_CACHE=false -- skipping cache, making a fresh real PubMed call.")

    if pool is None:
        print(f"Searching PubMed (combined, {len(elements)} element(s)): {query}")
        pmids = _pubmed_esearch(query, retmax=PUBMED_SEARCH_POOL_SIZE)
        print(f"Combined esearch found {len(pmids)} PMID(s) (pool cap {PUBMED_SEARCH_POOL_SIZE}).")
        fetched = {}
        if pmids:
            print(f"Fetching abstract text for {len(pmids)} PMID(s)...")
            fetched = _pubmed_efetch(pmids)
        pool = []
        for pmid in pmids:
            if pmid not in fetched:
                continue  # no real abstract text -- never invent one
            a = fetched[pmid]
            matched = _find_mentioned_elements(f"{a.get('title', '')} {a.get('abstract', '')}",
                                                elements, gene_info)
            if not matched:
                continue  # real text, but none of our elements actually named in it
            pool.append({"pmid": pmid, "title": a.get("title", ""), "abstract": a.get("abstract", ""),
                         "matched_elements": matched})
        print(f"Combined pool: {len(pool)} real abstract(s) mention at least one input element.")
        if PUBMED_USE_CACHE:
            try:
                ensure_dir(PUBMED_CACHE_DIR)
                cache_file.write_text(
                    json.dumps({"query": query, "pool_size": PUBMED_SEARCH_POOL_SIZE, "pool": pool},
                               ensure_ascii=False),
                    encoding="utf-8")
                print(f"Saved combined pool to cache: {cache_file}")
            except Exception as e:
                print(f"Could not write combined cache file {cache_file}: {e}")

    # Rank by real co-occurrence count, descending; stable sort keeps
    # PubMed's own relevance order as the tiebreaker (pool is already in
    # that order from esearch).
    ranked = sorted(pool, key=lambda a: len(a["matched_elements"]), reverse=True)
    top = ranked[:PUBMED_MAX_ABSTRACTS]
    print(f"Selected top {len(top)} of {len(ranked)} pooled abstract(s) by real element-mention count.")

    # Coverage guarantee: any element with FEWER than
    # COMBINED_POOL_MIN_MENTIONS_PER_ELEMENT real mentions in the selected
    # top set -- zero, same as before, or just thinly covered (e.g. one
    # tangential real mention that gave the LLM nothing to report) -- gets
    # its own small dedicated fallback search, so it's never silently left
    # with only whatever scraps the combined ranking happened to surface.
    mention_counts = Counter(e for a in top for e in a["matched_elements"])
    thin = [e for e in elements if mention_counts.get(e, 0) < COMBINED_POOL_MIN_MENTIONS_PER_ELEMENT]
    seen_pmids = {a["pmid"] for a in top}
    for elem in thin:
        fallback_n = min(5, PUBMED_MAX_ABSTRACTS)
        n_have = mention_counts.get(elem, 0)
        reason = ("not covered at all" if n_have == 0 else
                   f"only {n_have} real mention(s)")
        print(f"[{elem}] {reason} in the combined top-{PUBMED_MAX_ABSTRACTS} pool -- "
              f"running a small fallback search (up to {fallback_n} abstracts) for this element alone.")
        fb_abstracts = fetch_pubmed_abstracts_for_element(elem, gene_info)[:fallback_n]
        for a in fb_abstracts:
            if a["pmid"] in seen_pmids:
                continue
            top.append({"pmid": a["pmid"], "title": a.get("title", ""), "abstract": a.get("abstract", ""),
                        "matched_elements": [elem]})
            seen_pmids.add(a["pmid"])

    return top


def extract_multi_element_directions_from_batch(batch: list, elements: list, context: str = "disease") -> list:
    """Combined-pool counterpart to extract_directions_from_batch: ONE
    stateless LLM call over a batch where different abstracts can be about
    different elements. The LLM decides which elements each abstract
    discusses and what direction (Up/Down/Mixed/Unclear) by actually
    reading the abstract text -- the per-abstract "matched_elements" hint
    is shown only as a starting point, never used as a hard filter here.

    Returns a list of {pmid, element, direction, quote} for every
    (abstract, element) pair the LLM reported on that passes validation:
      1. PMID must be one of this batch's real PMIDs.
      2. Element must be one of the real master-list `elements` (guards
         against the LLM inventing or misspelling a name).
      3. Direction must be Up, Down, or Mixed -- per the new Prompt 1
         (BioShift_Prompts_0729_PD), "Unclear"/"Not Applicable" are no
         longer valid outputs; an element an abstract doesn't clearly
         support should simply be omitted by the LLM, and any row that
         still comes back with something other than up/down/mixed is
         dropped here rather than trusted.
      4. Quoted Evidence must be real, grounded text -- verified with
         _quote_is_grounded against that PMID's own real title+abstract,
         not just trusted at face value. This replaces the old
         "element must be in the pre-computed candidate list" gate, which
         was too brittle: our own regex/synonym matcher can miss a real
         mention, silently discarding a row the LLM actually got right
         from reading the text. Checking the quote against the real
         source text is a stronger, more direct anti-fabrication check."""
    if not batch:
        return []
    real_pmids = {a["pmid"] for a in batch}
    text_by_pmid = {a["pmid"]: f"{a.get('title', '')} {a.get('abstract', '')}" for a in batch}
    valid_elements = [str(e).strip() for e in elements if str(e).strip()]
    element_alias_map = _build_element_alias_map(valid_elements)
    element_list_str = "\n".join(f"- {e}" for e in sorted(valid_elements))
    prompt_template = get_pubmed_extract_multi_prompt(context)
    prompt = prompt_template.format(element_list=element_list_str, abstracts=_format_abstracts_block(batch),
                                     study_context=_study_context_block_for_prompt())
    raw = call_openai(prompt)
    table_text = _extract_clean_table(raw, min_cols=2)
    rows = []
    dropped = []
    for i, line in enumerate(table_text.splitlines()):
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) < 4:
            continue
        if i == 0 and parts[0].lower() == "pmid":
            continue  # header row
        pmid_raw, element_raw, direction, quote = parts[0], parts[1], parts[2].strip().lower(), parts[3]
        pmid = re.sub(r"\D", "", pmid_raw)
        if pmid not in real_pmids:
            dropped.append((pmid_raw, element_raw, "unknown PMID"))
            continue
        element = element_alias_map.get(_normalize_for_match(element_raw))
        if element is None:
            dropped.append((pmid_raw, element_raw, "element not in master list"))
            continue  # LLM named something outside the real input elements -- discard
        if direction not in ("up", "down", "mixed"):
            # New Prompt 1 only permits Up/Down/Mixed -- "unclear"/"not
            # applicable"/anything else is rejected rather than kept.
            dropped.append((pmid_raw, element_raw, f"invalid direction '{direction}' (must be up/down/mixed)"))
            continue
        if not _quote_is_grounded(quote, text_by_pmid.get(pmid, "")):
            # Includes the actual claimed quote (truncated) -- not just the
            # element/PMID -- so a real rejection can be diagnosed from the
            # log alone (e.g. genuine LLM paraphrase vs. a real matching
            # bug) instead of guessing why verification failed.
            dropped.append((pmid_raw, element_raw, "quote not grounded", quote[:150]))
            continue  # can't verify this claim against the real source -- don't trust it
        if not _quote_has_nearby_disease_context(quote, text_by_pmid.get(pmid, "")):
            # Soft flag only (see _quote_has_nearby_disease_context's
            # docstring) -- the quote IS real, verbatim text, but doesn't
            # itself sit in a sentence mentioning this study's own disease
            # context, which is what a background-citation-to-unrelated-
            # work case (real text, wrong topic) looks like. Kept, not
            # dropped -- printed so it's easy to spot-check.
            print(f"  NOTE: PMID:{pmid} quote for '{element}' has no disease-context term in its "
                  f"own sentence -- possible off-topic/background-citation risk, worth a manual "
                  f"check: {quote[:150]!r}")
        rows.append({"pmid": pmid, "element": element, "direction": direction, "quote": quote})
    if dropped:
        print(f"Combined-batch extraction: dropped {len(dropped)} unverified row(s) -- "
              f"{dropped[:5]}{'...' if len(dropped) > 5 else ''}")
    if not rows and table_text.strip():
        print(f"WARNING: combined-batch extraction returned a table but 0 rows validated. "
              f"First 300 chars of raw response: {raw[:300]!r}")
    elif not table_text.strip() and raw.strip():
        print(f"WARNING: combined-batch LLM response had no parseable '|' table at all. "
              f"First 300 chars: {raw[:300]!r}")
    return rows

def run_extraction_ensemble(elements: list, gene_info: dict = None, out_dir: Path = None,
                             context: str = "disease", n_runs: int = None):
    """Runs the extraction step n_runs independent times over ONE shared
    real PubMed pool -- the pool is fetched ONCE (real esearch/efetch;
    PubMed's own results for a fixed query don't change run to run, so
    re-fetching it n_runs times was pure wasted time, not a source of any
    real signal) and then reused for every run. Only the LLM extraction
    itself is repeated n_runs times, since that's the actual point of the
    ensemble: measuring whether the LLM reads the same real abstract the
    same way twice, not whether repeated searches find different papers.

    Returns a tuple (all_runs, pool):
      - all_runs: a list of n_runs row-lists, each a full set of {pmid,
        element, direction, quote} rows from one independent LLM pass --
        aggregation into a single consensus table happens separately in
        aggregate_extraction_runs (deterministic Python, never the LLM).
      - pool: the real shared abstract pool itself (list of {pmid, title,
        abstract, matched_elements} dicts, same object fetch_ranked_
        combined_pool returned), returned alongside the runs so callers
        (build_table1_evidence) can compute a real per-element "how many
        actual PubMed abstracts were screened for this element" count
        from matched_elements -- a true retrieval-coverage number,
        independent of whether the LLM ever extracted a direction from
        any of them. This is a real denominator on its own, not conflated
        with "how many of those abstracts yielded a validated directional
        claim" (see
        build_table1_evidence's docstring for the full rename this
        enabled).

    Every (run, batch) extraction call is independent -- no run or batch
    depends on any other's result -- so all of them are fired at once
    through a thread pool (MAX_CONCURRENT_LLM_CALLS at a time) instead of
    one at a time. Most of each call's wall-clock time is spent waiting
    on the OpenAI API, not on local CPU, so this cuts real run time
    roughly by that concurrency factor without changing what gets
    computed or how many calls are made."""
    if n_runs is None:
        n_runs = PUBMED_EXTRACTION_RUNS
    print("=== Fetching abstract pool once (shared across all extraction runs) ===")
    pool = fetch_ranked_combined_pool(elements, gene_info, out_dir=out_dir)
    n_batches = (len(pool) + PUBMED_BATCH_SIZE - 1) // PUBMED_BATCH_SIZE
    jobs = []  # (run_idx, batch_no, batch)
    for run_idx in range(1, n_runs + 1):
        for i in range(0, len(pool), PUBMED_BATCH_SIZE):
            batch = pool[i:i + PUBMED_BATCH_SIZE]
            batch_no = i // PUBMED_BATCH_SIZE + 1
            jobs.append((run_idx, batch_no, batch))
    print(f"=== Extracting {len(jobs)} (run, batch) job(s) -- {n_runs} run(s) x {n_batches} "
          f"batch(es) of up to {PUBMED_BATCH_SIZE} abstract(s), up to {MAX_CONCURRENT_LLM_CALLS} "
          f"concurrent LLM call(s) ===")
    rows_by_run = defaultdict(list)
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_LLM_CALLS) as pool_exec:
        futures = {
            pool_exec.submit(extract_multi_element_directions_from_batch, batch, elements, context):
                (run_idx, batch_no) for run_idx, batch_no, batch in jobs
        }
        for fut in as_completed(futures):
            run_idx, batch_no = futures[fut]
            try:
                batch_rows = fut.result()
            except Exception as e:
                print(f"  Run {run_idx} batch {batch_no}/{n_batches} failed ({e}); treated as 0 rows.")
                batch_rows = []
            print(f"  Run {run_idx}/{n_runs} -- batch {batch_no}/{n_batches} done -- "
                  f"{len(batch_rows)} row(s).")
            rows_by_run[run_idx].extend(batch_rows)
    all_runs = [rows_by_run[run_idx] for run_idx in range(1, n_runs + 1)]
    for run_idx, run_rows in enumerate(all_runs, start=1):
        print(f"Run {run_idx}/{n_runs} done -- {len(run_rows)} (abstract, element) row(s) reported.")
    return all_runs, pool

def _majority_direction(votes: list, n_runs: int):
    """Given the directions reported for one (pmid, element) pair across
    however many of the n_runs independent extraction runs actually
    reported it (a run that never identifies this element in this
    abstract simply contributes no vote), return the consensus
    direction, or None if there's no real consensus.

    A single grounded report (1 of n_runs) is enough to count: this
    pipeline's extraction runs share one fixed, already quote-grounded/
    disease-context-checked abstract pool, so a hit in even 1 run is a
    verified finding, not fabricated noise, and requiring a strict
    majority would unfairly penalize thin-coverage elements (e.g. a rare
    organism with only 1-2 abstracts total has no room to reach a strict
    majority even when the one mention is genuine). Still requires: among
    whatever votes a pair did get, one direction must be a strict
    plurality winner -- a straight tie (e.g. 1 run says Up, another says
    Down, nothing else) has no real consensus and is dropped."""
    if not votes:
        return None
    counts = Counter(votes)
    ranked = counts.most_common()
    top_direction, top_count = ranked[0]
    runner_up_count = ranked[1][1] if len(ranked) > 1 else 0
    if top_count <= runner_up_count:
        return None
    return top_direction

def aggregate_extraction_runs(all_runs: list, n_runs: int) -> list:
    """Deterministic Python aggregation (never the LLM) of n_runs
    independent extraction passes over the same real abstract pool into
    ONE consensus set of rows. For every (pmid, element) pair reported by
    at least one run, takes the plurality-vote direction across however
    many runs reported it (_majority_direction -- see its docstring: a
    single 1-of-n_runs report is now enough, only a real tie between two
    directions is dropped) and drops pairs with a real tie. Returns
    {pmid, element, direction, quote, votes} rows, where 'votes' is a
    human-readable "k runs" string -- k = how many runs actually reported
    the winning direction for this (pmid, element) pair. This is
    deliberately just k, not "k/m" or "k/m (of n_runs total)": n_runs
    (Table 1's own "Total Runs" column -- see build_table1_evidence)
    already gives the reader the denominator context once for the whole
    table, so a bare "(3 runs)" next to a PMID reads as "3 of Total Runs"
    without repeating it."""
    by_key = defaultdict(list)  # (pmid, element) -> [(direction, quote), ...]
    for run_rows in all_runs:
        for row in run_rows:
            by_key[(row["pmid"], row["element"])].append((row["direction"], row["quote"]))

    consensus_rows = []
    dropped = 0
    for (pmid, element), entries in by_key.items():
        votes = [d for d, _ in entries]
        direction = _majority_direction(votes, n_runs)
        if direction is None:
            dropped += 1
            continue
        quote = next(q for d, q in entries if d == direction)  # a real, grounded quote from a reporting run
        consensus_rows.append({
            "pmid": pmid, "element": element, "direction": direction, "quote": quote,
            "votes": f"{votes.count(direction)} runs",
        })
    print(f"Consensus aggregation: {len(consensus_rows)} (abstract, element) pair(s) accepted "
          f"(1+ real report, no unresolved tie) across {n_runs} runs; {dropped} dropped for a real tie.")
    return consensus_rows


# Deterministic (never LLM) evidence-strength hierarchy, keyed by PubMed's
# own <PublicationType> values -- higher number = stronger evidence.
# Level 7 (strongest) = systematic reviews/meta-analyses, down to Level 1
# ("Others") for any publication type not otherwise classified.

def build_table1_evidence(sample: str, elements: list, obs_df: pd.DataFrame, out_dir: Path,
                           context: str = "disease") -> pd.DataFrame:
    """Prompt 1's final output: Table 1 with columns Element, Evidence for
    Up, Evidence for Down, Evidence for Mixed, Observed Shift, Total
    Abstracts Screened, Detected Directional Evidence (Citations), Total
    Abstract Support, % Support with Observed Shift, Total Runs. The old
    "Evidence for Unknown"/"unclear" bucket is gone since Prompt 1 no
    longer permits an Unclear direction -- an unsupported element is
    simply omitted upstream instead. Evidence for Up/Down/Mixed = PubMed
    PMIDs (batched, stateless LLM extraction) only; ImmuneXpresso/UniProt
    KB evidence is reported in Table 2/3 instead (see
    build_kb_sourced_table2_rows), never in Table 1.

    "% Support with Observed Shift" is a plain percentage, with the
    numerator/denominator broken out into their own columns, so every
    number in the table is independently sortable/filterable rather than
    needing to be parsed out of a compound string. "Total Runs"
    (PUBMED_EXTRACTION_RUNS -- the same for every row) lets each Evidence
    cell's "(k runs)" citation state just k rather than repeating a "/m"
    denominator on every PMID (see aggregate_extraction_runs).

    "Total Abstracts Screened" is the retrieval-coverage number: how many
    distinct PubMed abstracts (from the shared combined pool plus this
    element's own fallback search, see fetch_ranked_combined_pool)
    actually mention this element by text match, independent of whether
    the LLM ever extracted a direction from any of them (sourced from
    run_extraction_ensemble's returned pool via each abstract's
    matched_elements list). "Detected Directional Evidence (Citations)"
    is len(up_items)+len(down_items)+len(mixed_items) -- the count of
    those screened abstracts whose claims actually survived extraction +
    consensus + quote-grounding -- and it is the denominator for "Total
    Abstract Support" / "% Support with Observed Shift" (that percentage
    is "% of DETECTED directional claims that agree with Observed
    Shift", not diluted by abstracts that were screened but never
    yielded a direction at all). Keeping these two counts separate
    matters: an element can have PubMed abstracts retrieved for it that
    the LLM never turned into a usable directional claim, which should
    read as "thin extraction yield", not as "zero literature".

    MASI and MiMeDB are not used by this pipeline; neither source's data
    file is loaded anywhere in this script. 'Observed Shift' is the
    user's own uploaded data, untouched by any of this. Saves
    {sample}_table1.csv (CSV-only, no .txt rendering) and returns the
    DataFrame."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    obs_df = obs_df.copy()
    obs_df.columns = [str(c).strip() for c in obs_df.columns]
    obs_cols = [c for c in obs_df.columns if c.lower().startswith("element")]
    if obs_cols:
        obs_df.rename(columns={obs_cols[0]: "Element"}, inplace=True)
        obs_df["Element"] = obs_df["Element"].astype(str).map(lambda x: x.strip())
    obs_shift_col = next((c for c in obs_df.columns if "observed" in c.lower() and "shift" in c.lower()), None)
    observed_map = {}
    if "Element" in obs_df.columns and obs_shift_col:
        observed_map = dict(zip(obs_df["Element"], obs_df[obs_shift_col]))

    gene_info = find_gene_identity_info(elements, SAMPLE_MODEL)

    # 5x (configurable via PUBMED_EXTRACTION_RUNS) independent full
    # passes: each run re-fetches the combined pool (fetch_ranked_
    # combined_pool -- real esearch/efetch on run 1, served from
    # pubmed_cache/ on runs 2-5 since PubMed's own results for a fixed
    # query don't change) and then independently extracts from it.
    # aggregate_extraction_runs then takes a deterministic majority vote
    # per (pmid, element) pair across the runs, which is what actually
    # decides the direction that ends up in Table 1.
    all_runs, pool = run_extraction_ensemble(elements, gene_info, out_dir, context)
    consensus_rows = aggregate_extraction_runs(all_runs, PUBMED_EXTRACTION_RUNS)

    # Real per-element retrieval-coverage count -- how many distinct real
    # PubMed abstracts in the shared pool (combined search + any per-
    # element fallback search, see fetch_ranked_combined_pool) actually
    # mention this element by real text match. Sourced straight from each
    # abstract's own matched_elements list -- the same real signal
    # fetch_ranked_combined_pool already uses internally to decide which
    # elements need a fallback search -- never re-derived or guessed here.
    screened_counts = Counter(e for a in pool for e in a.get("matched_elements", []))

    by_element = {e: {"up": [], "down": [], "mixed": []} for e in elements}
    for row in consensus_rows:
        elem = row["element"]
        if elem not in by_element:
            continue  # extra safety; should already be guaranteed by master-list validation
        item = {"pmid": row["pmid"], "quote": row["quote"], "votes": row["votes"]}
        if row["direction"] == "up":
            by_element[elem]["up"].append(item)
        elif row["direction"] == "down":
            by_element[elem]["down"].append(item)
        else:  # "mixed" -- the only other value extract_multi_element_directions_from_batch permits
            by_element[elem]["mixed"].append(item)

    rows = []
    for elem in elements:
        pm = by_element[elem]

        # Each PubMed-derived citation shows its ensemble consensus count
        # (e.g. "(5 runs)") right next to the PMID -- the same confidence
        # signal Table 2 already surfaces on its edges, so a reader sees
        # not just THAT a claim is cited but how much of the independent-
        # run ensemble agreed on it. A bare count, not "(5/5 runs (of 5
        # total))" -- the table's own "Total Runs" column below already
        # gives the denominator once for the whole table.
        up_items = [f"PMID:{d['pmid']} ({d['votes']})" for d in pm["up"]]
        down_items = [f"PMID:{d['pmid']} ({d['votes']})" for d in pm["down"]]
        mixed_items = [f"PMID:{d['pmid']} ({d['votes']})" for d in pm["mixed"]]

        observed_shift_raw = observed_map.get(elem, "")
        try:
            observed_val = float(observed_shift_raw)
        except (TypeError, ValueError):
            observed_val = None

        # Total Abstracts Screened (retrieval coverage -- see
        # screened_counts above), Detected Directional Evidence
        # (Citations) (the denominator for support -- how many of those
        # screened abstracts survived extraction+consensus+quote-
        # grounding), Total Abstract Support (the numerator -- how many
        # of those detected citations' direction matches this element's
        # Observed Shift sign), and % Support with Observed Shift are
        # kept as separate, atomic columns rather than one compound text
        # cell, so every number is independently sortable/filterable.
        # "N/A" (never a fabricated 0) marks the cases where a percentage
        # can't be computed: no detected directional evidence for this
        # element, or no Observed Shift value was uploaded for it.
        total_abstracts_screened = screened_counts.get(elem, 0)
        detected_directional_evidence = len(up_items) + len(down_items) + len(mixed_items)
        if detected_directional_evidence == 0:
            total_abstract_support = "N/A"
            pct_support = "N/A"
        elif observed_val is None:
            total_abstract_support = "N/A"
            pct_support = "N/A"
        else:
            supporting = len(up_items) if observed_val > 0 else (len(down_items) if observed_val < 0 else 0)
            total_abstract_support = supporting
            pct_support = f"{round(100 * supporting / detected_directional_evidence)}%"

        rows.append({
            "Element": elem,
            "Evidence for Up": "; ".join(up_items),
            "Evidence for Down": "; ".join(down_items),
            "Evidence for Mixed": "; ".join(mixed_items),
            "Observed Shift": observed_shift_raw,
            "Total Abstracts Screened": total_abstracts_screened,
            "Detected Directional Evidence (Citations)": detected_directional_evidence,
            "Total Abstract Support": total_abstract_support,
            "% Support with Observed Shift": pct_support,
            "Total Runs": PUBMED_EXTRACTION_RUNS,
        })

    table1 = pd.DataFrame(rows)

    # Table 1 is CSV-only -- no .txt rendering and no appended "Study
    # Context" note. Table 3 keeps its .txt + study-context note; this
    # only applies to Table 1/2.
    csv_file = Path(out_dir) / f"{sample}_table1.csv"
    table1.to_csv(csv_file, index=False, encoding="utf-8")
    print(f"Table1 CSV saved: {csv_file}")

    return table1



# ─────────────────── Prompt 2: Table 2 + Table 3 (co-shift detection) ──────
# PROMPT_COSHIFT_COMBINED: verbatim Prompt 2 from BioShift_Prompts_0729_PD
# (the "_fixed" docx). Notable structure:
# 1. Part A relationships can involve more than two elements (not
#    pairs-only), and Evidence Source is either "PubMed" (an abstract) or
#    a KB database name -- KB evidence contributes its own rows directly,
#    it's not background-only context kept out of Table 2/3.
# 2. Part B is a pairwise breakdown of Part A -- "Relation" is the stated
#    connection worded as a sentence (Element A - Relation - Element B).
# 3. Per Prompt 3's own Table 2/Table 3 description, evidence from
#    different sources is never merged into one row -- Table 2/Table 3
#    are a straight concatenation of every batch's validated Part A/Part B
#    rows (KB-sourced duplicates across batches are still de-duplicated,
#    since the same KB content is shown to every batch).
PROMPT_COSHIFT_COMBINED = """AI Role
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Data
Below is the "study context." Study Context has the information that describes the dataset being analyzed. It is metadata, not scientific evidence. It specifies the conditions under which the data were collected, including, disease, disease stage, tissue site, host species, experimental modality, taxonomic resolution, and the dataset's Baseline Group and Target Group.

Study context:
{study_context}

Below is the "element list". For each element, the Observed Shift represents a comparison between the dataset's Baseline Group and Target Group, both defined in the Study Context above. An Observed Shift of 1 means the element's value is higher in the Target Group than in the Baseline Group (an increase); -1 means it is lower (a decrease). For this analysis, you will not use the observed shift values.

Element list:
{element_list}

Analysis Instructions
The abstracts below were retrieved from a PubMed search intended to capture literature relevant to one or more elements in the Element list above. The retrieved abstracts are ranked by the number of listed elements explicitly mentioned in the title and/or abstract, so abstracts containing the greatest number of co-occurring elements appear first, providing the strongest evidence for coordinated biological changes. Use these abstracts as evidence. Use Review abstracts also as evidence, i.e., review statements count and inferred summaries count.

Abstracts:
---
{abstracts}

For elements in the Element list above, collect abstracts that reported biological relationships between elements. Example relationships include "activates," "inhibits," "recruits," "binds," "induces," "secretes," "correlates," "associated with," "marker of," "expressed by," and "located in." Mere co-occurrence of two elements is not evidence of biological relationships. Include speculative statements ("may", "might", "suggests"). Exclude information whose disease, disease stage, tissue site, or host species clearly differ from those specified in the Study Context. Differences in molecular measurement (experimental modality) (e.g., mRNA versus protein) should not by themselves exclude information. Consider different taxonomic resolutions compatible only when the information explicitly refers to the queried taxon or one of its parent taxa in a biologically meaningful way. Treat official symbols, full names, and common aliases as the same element, and also match elements case-insensitively.

Reporting Instructions
Output exactly two labeled sections. Each section should contain only the requested table.

### PART A
Summarize the results in a table. For each biological relationship or pair of the elements identified from a single abstract, make a row. Do not merge relationships or pairs. If no compatible abstract supports a relationship or pair, omit that from the table. Columns should be arranged with this order: "Biological Relationship Name," "PMID," "Evidence Source," "List of elements," and "Quoted Evidence."

- "Biological Relationship Name": A short (3-8 words), specific biological name (never generic labels such as "Group 1").
- "PMID": bare numeric ID, no prefix.
- "Evidence Source": always write "PubMed."
- "List of elements": All in the biological relationship. When it is found in the "element list," copy EXACTLY as in the "element list."
- "Quoted Evidence": Quote the shortest contiguous phrase that explicitly supports the reported biological relationship. Required for every row; so a non-matching quote gets the row discarded.

The table should be pipe-separated ("|") with header row without divider nor extra spaces.

### PART B
Use the table generated in PART A. When a biological relationship contains more than two elements, break it into pairs accordingly. Generate pairwise relationships only when the original evidence explicitly supports each pair. When a pair of elements had more than one abstract, produce one row for each (PMID, Element A, Relation, Element B) pair. If no compatible abstract supports a pair of elements, omit that pair from the table. Columns should be arranged with this order: "PMID," "Evidence Source," "Element A," "Relation," "Element B," "Quoted Evidence".
- "PMID": bare numeric ID, no prefix.
- "Evidence Source": always write "PubMed."
- "Element A" and "Element B": When it is found in the "element list," copy EXACTLY as in the "element list."
- "Relation": Preserve the direction stated in the source whenever applicable. For symmetric relations (e.g., binds, correlates with), report it as stated in the source. It should complete a sentence by ordering as "Element A" "Relation" "Element B."
- "Quoted Evidence": Quote the shortest contiguous phrase that explicitly supports the reported relationship. Required for every row; so a non-matching quote gets the row discarded.

The table should be pipe-separated ("|") with header row without divider nor extra spaces.
"""

COSHIFT_COMBINED_PROMPTS_BY_CONTEXT = {
    "disease": PROMPT_COSHIFT_COMBINED,
}

def get_coshift_combined_prompt(context: str) -> str:
    if context not in COSHIFT_COMBINED_PROMPTS_BY_CONTEXT:
        raise NotImplementedError(
            f"No combined-pool Prompt 2 template for context='{context}' yet -- "
            f"only 'disease' is built."
        )
    return COSHIFT_COMBINED_PROMPTS_BY_CONTEXT[context]

def _load_immunexpresso() -> pd.DataFrame:
    if _KB_CACHE["immunexpresso"] is not None:
        return _KB_CACHE["immunexpresso"]
    if not KB_IMMUNEXPRESSO_FILE.exists():
        print(f"KB file not found (skipping): {KB_IMMUNEXPRESSO_FILE}")
        df = pd.DataFrame()
    else:
        try:
            df = pd.read_csv(KB_IMMUNEXPRESSO_FILE, dtype=str)
            df.columns = [str(c).strip() for c in df.columns]
        except Exception as e:
            print(f"Could not read {KB_IMMUNEXPRESSO_FILE}: {e}")
            df = pd.DataFrame()
    _KB_CACHE["immunexpresso"] = df
    return df


def _immunexpresso_source_target(cell: str, cytokine: str, actor: str) -> tuple:
    """ImmuneXpresso's own 'Actor' column names which side (cell or
    cytokine) is doing the acting in that specific record -- the same
    (cell, cytokine) pair can appear twice in the file, once per actor
    direction. The arrow should follow that field, not a fixed
    cell-first convention. Falls back to cell -> cytokine if Actor is
    missing/unrecognized."""
    if str(actor).strip().lower() == "cytokine":
        return cytokine, cell
    return cell, cytokine

def find_kb_edges_for_elements(elements) -> list:
    """Given the Table3 element names, return every DIRECT edge between two
    of them found in ImmuneXpresso -- an actual documented, source-cited
    interaction record (not just shared-category co-membership; see the
    note above KB_DIR). One dict per matched edge: Source, Target,
    Relationship, Direction, Source_DB, Evidence.

    An edge always needs two DIFFERENT elements (src_key != tgt_key,
    below), so fewer than 2 elements can never produce one -- skip the
    real, full-file scan entirely in that case rather than paying its
    real cost (a full pass over every ImmuneXpresso row) just to always
    get []. This matters in practice: this function is called once per
    prompt/count build, and a real element list mostly has isolated
    elements with no direct KB partner, so a single-element call is the
    common case, not the exception."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    if len(elements) < 2 or not KNOWLEDGE_BASE:
        return []
    norm_map = {}
    for e in elements:
        n = _norm_name(e)
        if n:
            norm_map[n] = e
    edges = []

    ix = _load_immunexpresso()
    if not ix.empty and {"Cell Ontology Label", "Cytokine Ontology Label"} <= set(ix.columns):
        for _, row in ix.iterrows():
            cell = str(row.get("Cell Ontology Label", "")).strip()
            cyto = str(row.get("Cytokine Ontology Label", "")).strip()
            src_key = _match_norm(_norm_name(cell), norm_map)
            tgt_key = _match_norm(_norm_name(cyto), norm_map)
            if src_key and tgt_key and src_key != tgt_key:
                sentiment = str(row.get("Action Sentiment", "")).strip()
                actor = str(row.get("Actor", "")).strip()
                n_papers = str(row.get("NumPapers", "")).strip()
                enrich = str(row.get("Enrichment Score", "")).strip()
                cell_id = str(row.get("Cell Ontology ID", "")).strip()
                src_name, tgt_name = _immunexpresso_source_target(norm_map[src_key], norm_map[tgt_key], actor)
                edges.append({
                    "Source": src_name,
                    "Target": tgt_name,
                    "Relationship": "cell-cytokine interaction",
                    "Direction": sentiment,
                    "Source_DB": "ImmuneXpresso (documented literature co-mention, correlational -- not proof of causation)",
                    # ImmuneXpresso is an aggregated relation table with no PMID column --
                    # (Cell Ontology ID, Cytokine label) is the closest thing to a unique,
                    # look-up-able record ID for this specific row.
                    "Evidence": f"record [{cell_id}, {norm_map[tgt_key]}]: {n_papers} paper(s), enrichment score {enrich}",
                    "Cell_Ontology_ID": cell_id,
                    "Cell": norm_map[src_key], "Cytokine": norm_map[tgt_key],
                })

    return edges

# How many of an element's own top KB partners to surface even when that
# partner isn't itself in Table3 (see find_kb_neighborhood_edges below).

KB_NEIGHBORHOOD_TOP_N = 3

@lru_cache(maxsize=None)
def _find_kb_neighborhood_edges_one(element: str, top_n_per_element: int) -> tuple:
    """Cached, single-element implementation behind find_kb_neighborhood_edges
    (below) -- one element's real KB neighborhood doesn't depend on which
    OTHER elements are being queried alongside it, so this decomposes
    cleanly per element. Caching here means any caller that repeatedly
    queries overlapping element subsets reuses an already-computed result
    instead of re-scanning the whole ImmuneXpresso file from scratch every
    time. Real KB files are static within one run, so this is safe for the
    lifetime of the process. Body is the original find_kb_neighborhood_edges
    implementation, restricted to one element."""
    elements = [element]
    if not KNOWLEDGE_BASE:
        return ()
    norm_map = {}
    for e in elements:
        n = _norm_name(e)
        if n:
            norm_map[n] = e
    edges = []

    ix = _load_immunexpresso()
    if not ix.empty and {"Cell Ontology Label", "Cytokine Ontology Label"} <= set(ix.columns):
        ix2 = ix.copy()
        ix2["_NumPapersNum"] = pd.to_numeric(ix2["NumPapers"], errors="coerce").fillna(0)
        ix2["_CellNorm"] = ix2["Cell Ontology Label"].map(_norm_name)
        ix2["_CytoNorm"] = ix2["Cytokine Ontology Label"].map(_norm_name)
        for key, elem_name in norm_map.items():
            # Element as a CYTOKINE -> its top real cell-type partners
            sub = ix2[ix2["_CytoNorm"] == key].sort_values("_NumPapersNum", ascending=False).head(top_n_per_element)
            for _, row in sub.iterrows():
                cell = str(row.get("Cell Ontology Label", "")).strip()
                if _norm_name(cell) == key:
                    continue
                cell_id = str(row.get("Cell Ontology ID", "")).strip()
                actor = str(row.get("Actor", "")).strip()
                src_name, tgt_name = _immunexpresso_source_target(cell, elem_name, actor)
                edges.append({
                    "Source": src_name, "Target": tgt_name,
                    "Relationship": "cell-cytokine interaction",
                    "Direction": str(row.get("Action Sentiment", "")).strip(),
                    "Source_DB": "ImmuneXpresso (documented literature co-mention, correlational -- not proof of causation)",
                    "Evidence": f"record [{cell_id}, {elem_name}]: {int(row['_NumPapersNum'])} paper(s), enrichment score {row.get('Enrichment Score', '')}",
                    "Cell_Ontology_ID": cell_id,
                    "Cell": cell, "Cytokine": elem_name,
                })
            # Element as a CELL TYPE -> its top real cytokine partners
            sub2 = ix2[ix2["_CellNorm"] == key].sort_values("_NumPapersNum", ascending=False).head(top_n_per_element)
            for _, row in sub2.iterrows():
                cyto = str(row.get("Cytokine Ontology Label", "")).strip()
                if _norm_name(cyto) == key:
                    continue
                cell_id = str(row.get("Cell Ontology ID", "")).strip()
                actor = str(row.get("Actor", "")).strip()
                src_name, tgt_name = _immunexpresso_source_target(elem_name, cyto, actor)
                edges.append({
                    "Source": src_name, "Target": tgt_name,
                    "Relationship": "cell-cytokine interaction",
                    "Direction": str(row.get("Action Sentiment", "")).strip(),
                    "Source_DB": "ImmuneXpresso (documented literature co-mention, correlational -- not proof of causation)",
                    "Evidence": f"record [{cell_id}, {cyto}]: {int(row['_NumPapersNum'])} paper(s), enrichment score {row.get('Enrichment Score', '')}",
                    "Cell_Ontology_ID": cell_id,
                    "Cell": elem_name, "Cytokine": cyto,
                })

    return tuple(edges)

def find_kb_neighborhood_edges(elements, top_n_per_element: int = KB_NEIGHBORHOOD_TOP_N) -> list:
    """Like find_kb_edges_for_elements, but NOT limited to edges where both
    ends are already in Table3. For each Table3 element, pulls its real
    top-N documented ImmuneXpresso partners (ranked by paper count) even
    when that partner isn't one of your elements. This is what makes the
    Structured Knowledge Base Evidence
    non-empty for datasets where your own elements never co-occur as a
    KB-recognized pair (e.g. two cytokines, or two microbes, as has been
    the case on both Testdata and CaseStudy) -- every edge returned is
    still a real, specific KB record, just not required to close back into
    Table3. One dict per matched edge, same shape as find_kb_edges_for_elements.

    Thin wrapper around the cached _find_kb_neighborhood_edges_one -- see
    its docstring for why per-element caching is safe and worthwhile
    here."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    if not elements or not KNOWLEDGE_BASE:
        return []
    out = []
    for e in elements:
        out.extend(_find_kb_neighborhood_edges_one(e, top_n_per_element))
    return out


# ─────────────────── UniProt live lookup (function text + pathways) ────────
# The only network call in this pipeline besides the OpenAI API -- everything
# else (ImmuneXpresso, ImmPort registry) is a local file. No local caching
# beyond the disk cache below; a missing/unreachable network degrades this
# layer to a no-op rather than crashing the run. Reactome pathway names are
# fetched here too, but several (e.g. "Interleukin-4 and Interleukin-13
# signaling") are broad categories similar to what was rejected from the
# pathway-co-membership layer (see the note above KB_DIR), so they are not
# turned into edges; only the FUNCTION text's named mentions are (see
# find_uniprot_function_mentions below), since those are anchored to a
# specific, inline-cited sentence rather than a shared broad-category label.
# _v2 suffix: bumped when the go_terms field was added to the cached
# records below (same pattern as UNIPROT_VIRULENCE_CACHE_DIR's own _v2), so
# a pre-existing 3-field cache entry from before go_terms existed is never
# silently read back and treated as if it already had that key.
UNIPROT_CACHE_DIR = HERE / "uniprot_cache_v2"

def _fetch_uniprot_function(uniprot_id: str) -> dict:
    """Fetch (with permanent local caching) a UniProt entry's FUNCTION text,
    Reactome pathway cross-references, and GO (molecular function) terms.
    uniprot_id may be an accession (P01584) or an entry name (IL1B_HUMAN) --
    both work as UniProt's REST path segment. Returns {} on any failure
    (network down, bad ID, timeout, etc.) -- callers must treat that as 'no
    data', not an error. Cached to disk permanently (protein function
    annotations don't change run to run), so repeated pipeline runs over
    the same elements skip the live network call entirely after the first
    time.

    go_terms is built from this entry's own real
    data["uniProtKBCrossReferences"] entries with "database": "GO" -- each
    such entry's properties list carries a {"key": "GoTerm", "value":
    "F:cytokine activity"} (or "P:"/"C:" for biological process/cellular
    component; confirmed via live testing, e.g. real entry
    {"database":"GO","id":"GO:0005125","properties":[{"key":"GoTerm",
    "value":"F:cytokine activity"}],...}). Only "F:" (molecular function)
    terms are kept -- same GO namespace convention already used by this
    file's other UniProt layer (see _fetch_uniprot_virulence_proteins'
    `go_f` field, "Gene Ontology (molecular function)") -- with the "F:"
    prefix stripped and multiple real terms joined with '; ', matching that
    same function's join style. Returns '' (not a fabricated term) when
    this entry has no real GO molecular-function annotation.

    UNIPROT_CACHE_DIR was renamed (_v2 suffix) when go_terms was added --
    see the constant's own comment above -- so any pre-existing 3-field
    cache file under the old dir name is simply never looked at again, not
    migrated; the read below also defensively checks the cached dict
    actually has a 'go_terms' key before trusting it, in case a foreign/
    hand-edited file slipped into this same v2 dir name."""
    uid = str(uniprot_id or "").strip()
    if not uid or uid.lower() == "nan":
        return {}
    cache_file = UNIPROT_CACHE_DIR / f"{uid}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # Defensive: only trust a cached record if it actually has the
            # go_terms key (guards against a stale pre-v2-shaped or
            # foreign/hand-edited cache file slipping through under this
            # same dir name -- same guard _fetch_uniprot_virulence_proteins
            # applies to its own cache read above).
            if isinstance(cached, dict) and "go_terms" in cached:
                return cached
        except Exception:
            pass  # corrupt cache file -- fall through and refetch
    url = f"https://rest.uniprot.org/uniprotkb/{urllib.parse.quote(uid)}.json"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"UniProt fetch failed for '{uid}' ({e}); skipping this protein's UniProt layer.")
        return {}

    function_text = ""
    for c in data.get("comments", []):
        if c.get("commentType") == "FUNCTION":
            function_text = " ".join(t.get("value", "") for t in c.get("texts", []))
            break
    pathways = []
    go_terms = []
    for x in data.get("uniProtKBCrossReferences", []):
        if x.get("database") == "Reactome":
            props = x.get("properties", [])
            name = props[0].get("value", "") if props else ""
            pathways.append({"id": x.get("id", ""), "name": name})
        elif x.get("database") == "GO":
            for prop in x.get("properties", []):
                if prop.get("key") != "GoTerm":
                    continue
                val = str(prop.get("value", "") or "")
                if val.startswith("F:"):  # molecular function only -- see docstring
                    term = val[len("F:"):].strip()
                    if term:
                        go_terms.append(term)
    go_terms_text = "; ".join(go_terms)

    real_accession = data.get("primaryAccession", "")
    if not real_accession:
        # A well-formed real UniProt entry always has a primaryAccession --
        # its absence means this JSON wasn't the real entry (a transient
        # malformed/incomplete response that still parsed as valid JSON,
        # so it never raised and reached the except branch above). Real,
        # observed failure this guards against: 3 real entries (IL1A_
        # HUMAN/IL1B_HUMAN/IL6_HUMAN -- some of the most common cytokines
        # this pipeline sees) got PERMANENTLY cached this way with
        # function_text="" and accession=the bare ID instead of a real
        # one, silently starving UniProt content for those genes on
        # every run since, because the cache is checked before ever
        # attempting a fresh fetch again. NOT caching here lets the next
        # run retry for real instead of repeating the same empty result
        # forever. (A real protein that genuinely has no FUNCTION comment
        # is still cached normally below -- that's real_accession present
        # with function_text empty, a different, valid case.)
        print(f"UniProt fetch for '{uid}' returned no real primaryAccession "
              "(malformed/incomplete response) -- not caching, will retry next run.")
        return {}

    result = {
        "accession": real_accession,
        "function_text": function_text,
        "reactome_pathways": pathways,
        "go_terms": go_terms_text,
    }
    try:
        ensure_dir(UNIPROT_CACHE_DIR)
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass
    return result


def _nearest_pmid(text: str, pos: int, window: int = 250) -> str:
    """Find the PubMed ID nearest to a text position -- checks forward
    first (a claim's citation usually follows it within the same sentence
    or clause in UniProt's prose), then backward, within `window` chars
    either side. Returns '' if none found in range -- callers must NOT
    fabricate a citation when this comes back empty."""
    fwd = text[pos: pos + window]
    m = re.search(r"PubMed:(\d+)", fwd)
    if m:
        return m.group(1)
    back = text[max(0, pos - window): pos]
    ids = re.findall(r"PubMed:(\d+)", back)
    return ids[-1] if ids else ""

def _extract_sentence(text: str, start: int, end: int) -> str:
    """Return the full sentence (rough '. ' boundary split) containing the
    match span [start, end) in `text` -- the real quoted context, so a
    reader can see exactly what UniProt's curators actually wrote, rather
    than a generic 'co-mentioned' label."""
    left = text.rfind(". ", 0, start)
    left = left + 2 if left != -1 else 0
    right = text.find(". ", end)
    right = right + 1 if right != -1 else len(text)
    return text[left:right].strip()

# Real relationship-indicating verbs/phrases -- used ONLY to detect whether
# one is literally present in the sentence around a mention (never invented
# or inferred). If none of these appear, the edge stays labeled as a plain
# co-mention rather than guessing at a relationship.
_RELATIONSHIP_VERB_PATTERNS = [
    "synergizes with", "synergize with", "synergizing with", "synergistically with",
    "induces", "inducing", "induced by", "induction of",
    "promotes", "promoting", "promoted by",
    "activates", "activating", "activated by", "activation of",
    "inhibits", "inhibiting", "inhibited by", "inhibition of",
    "suppresses", "suppressing", "suppressed by",
    "stimulates", "stimulating", "stimulated by",
    "regulates", "regulating", "regulated by",
    "upregulates", "upregulating", "downregulates", "downregulating",
    "mediates", "mediating", "mediated by",
    "triggers", "triggering", "triggered by",
    "blocks", "blocking", "blocked by",
    "enhances", "enhancing", "enhanced by",
    "drives", "driving", "driven by",
    "binds to", "binding to", "binds", "bind to", "bound to",
    "required for", "essential for",
    "produced by", "produces", "producing", "production of",
    "signals through", "signaling through",
    "acts through", "acting through",
    "differentiation of",
]

def _find_relationship_verb(sentence: str, patterns: list = None) -> str:
    """Scan a real sentence for the first occurrence of a known
    relationship verb/phrase. Returns the phrase exactly as matched
    (case-insensitive search, original casing preserved), or '' if none
    of the known phrases appear -- callers must fall back to a generic,
    non-specific label in that case, never fabricate one."""
    patterns = patterns if patterns is not None else _RELATIONSHIP_VERB_PATTERNS
    low = sentence.lower()
    best_pos, best_phrase = None, ""
    for phrase in patterns:
        idx = low.find(phrase)
        if idx != -1 and (best_pos is None or idx < best_pos):
            best_pos, best_phrase = idx, sentence[idx: idx + len(phrase)]
    return best_phrase

def find_uniprot_function_mentions(elements, sample_model: str = None) -> list:
    """For each Table3 element with a UniProt match, scan its REAL curated
    FUNCTION text (fetched/cached above) for named mentions of: (tier 1)
    other Table3 elements, and (tier 2) any other gene symbol in the FULL
    ImmPort Cytokine Registry (external context, same neighbor-expansion
    principle as find_kb_neighborhood_edges). Matching is whole-word,
    case-sensitive against each candidate's actual registry gene symbol
    (e.g. 'IL6', 'TNF') -- not a blind token scan -- to avoid matching
    ordinary capitalized English words. Every edge's Evidence is either a
    real inline 'PubMed:NNNNNNN' found near that specific mention, or the
    literal string 'no inline PMID found nearby' -- never a fabricated
    citation. One dict per mention, with an extra '_in_table3' flag.

    Each edge also carries Function_Text and GO_Terms -- the SOURCE
    protein's (elem's) own real, complete UniProt FUNCTION text and real
    GO molecular-function terms (both from the same already-fetched
    `fetched` dict the co-mention Sentence itself was extracted from; see
    _fetch_uniprot_function). This is the source protein's OWN full
    function/GO data, not anything specific to the particular co-mention --
    it lets _label_uniprot_relations_via_llm (see below) additionally write
    a short, grounded Functions description of Element A (the source
    protein) alongside the existing Relation phrase, without a second LLM
    call or a second network fetch."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    reg = _load_cytokine_registry()
    if reg.empty or not elements:
        return []
    cols = _registry_species_columns(sample_model)
    human_cols = _registry_species_columns("Human")
    sym_col = cols["symbol"] if cols["symbol"] in reg.columns else human_cols["symbol"]

    reg2 = reg.copy()
    reg2["_sym"] = reg2[sym_col].astype(str).str.strip().replace("nan", "")
    # Elements whose selected-species symbol is blank fall back to the Human symbol
    blank = reg2["_sym"] == ""
    if blank.any() and sym_col != human_cols["symbol"]:
        reg2.loc[blank, "_sym"] = reg2.loc[blank, human_cols["symbol"]].astype(str).str.strip().replace("nan", "")
    reg2["_symnorm"] = reg2["_sym"].map(_norm_name)

    table3_norm = {_norm_name(e): e for e in elements if _norm_name(e)}
    all_symbols = {}  # normalized -> display symbol, for every gene in the registry
    for sym in reg2["_sym"]:
        if sym:
            all_symbols.setdefault(_norm_name(sym), sym)

    gene_info = find_gene_identity_info(elements, sample_model)
    # Normalized registry symbols that belong to one of OUR OWN Table3
    # elements (e.g. Table3's "IL-6" registers under registry symbol "IL6")
    # -- excluded from Tier 2 so the same gene never appears twice in the
    # output as both a colored Table3 node (via Tier 1) and a separate
    # white "external" node under its bare registry symbol (via Tier 2).
    table3_symbol_norms = {_norm_name(info["gene_symbol"]) for info in gene_info.values() if info.get("gene_symbol")}
    edges = []
    for elem in elements:
        info = gene_info.get(elem)
        uid = info["uniprot_id"] if info else ""
        if not uid:
            continue
        fetched = _fetch_uniprot_function(uid)
        text = fetched.get("function_text", "")
        if not text:
            continue
        go_terms = fetched.get("go_terms", "")
        # The ImmPort registry's own "UniProtDB ID" column stores UniProt
        # ENTRY NAMES (e.g. "IL1B_HUMAN"), not accession numbers -- both are
        # valid REST path segments (see _fetch_uniprot_function's
        # docstring), but an accession (e.g. "P01584") is the conventional,
        # more citable identifier. UniProt's own API response carries the
        # real accession back as 'primaryAccession'; _fetch_uniprot_function
        # already captures that as 'accession', falling back to the entry
        # name only if the live lookup failed -- use that real accession as
        # this element's citable ID everywhere below, instead of the
        # registry's entry-name value.
        accession = fetched.get("accession") or uid
        key = _norm_name(elem)
        found = set()

        # Tier 1: other Table3 elements, matched via THEIR OWN registry symbol
        for other_key, other_name in table3_norm.items():
            if other_key == key or other_name in found:
                continue
            other_hit = reg2[reg2["_symnorm"] == other_key]
            if other_hit.empty:
                continue
            symbol = other_hit.iloc[0]["_sym"]
            if not symbol:
                continue
            m = re.search(rf'\b{re.escape(symbol)}\b', text)
            if not m:
                continue
            found.add(other_name)
            pmid = _nearest_pmid(text, m.end())
            sentence = _extract_sentence(text, m.start(), m.end())
            verb_cue = _find_relationship_verb(sentence)
            edges.append({
                "Source": elem, "Target": other_name,
                "Relationship": "co-mentioned in UniProt function text",
                "Direction": "", "Accession": accession,
                "Source_DB": "UniProt (curated function description, PubMed-cited)" if pmid else
                             "UniProt (curated function description -- no inline PMID found near this mention)",
                "Evidence": f"PubMed:{pmid}" if pmid else "no inline PMID found nearby",
                "Sentence": sentence, "Verb_Cue": verb_cue,
                "Function_Text": text, "GO_Terms": go_terms,
                "_in_table3": True,
            })

        # Tier 2: any other registry gene symbol mentioned (external context)
        for other_norm, symbol in all_symbols.items():
            if other_norm == key or symbol in found or not symbol:
                continue
            if other_norm in table3_symbol_norms:
                # Belongs to one of our own Table3 elements -- already
                # covered (or eligible to be covered) by Tier 1 under its
                # Table3 display name; skip to avoid a duplicate node.
                continue
            m = re.search(rf'\b{re.escape(symbol)}\b', text)
            if not m:
                continue
            found.add(symbol)
            pmid = _nearest_pmid(text, m.end())
            sentence = _extract_sentence(text, m.start(), m.end())
            verb_cue = _find_relationship_verb(sentence)
            edges.append({
                "Source": elem, "Target": symbol,
                "Relationship": "co-mentioned in UniProt function text",
                "Direction": "", "Accession": accession,
                "Source_DB": "UniProt (curated function description, PubMed-cited)" if pmid else
                             "UniProt (curated function description -- no inline PMID found near this mention)",
                "Evidence": f"PubMed:{pmid}" if pmid else "no inline PMID found nearby",
                "Sentence": sentence, "Verb_Cue": verb_cue,
                "Function_Text": text, "GO_Terms": go_terms,
                "_in_table3": False,
            })
    return edges


# ─────── UniProt live lookup (virulence proteins by organism taxonomy) ─────
# A SECOND, structurally distinct UniProt integration alongside
# find_uniprot_function_mentions above. That layer fetches ONE protein's own
# curated entry (per-accession /uniprotkb/{id}.json) and scans its FUNCTION
# text for named mentions of OTHER real elements -- an edge exists there
# because two names are co-mentioned in one real sentence. This layer
# instead hits UniProt's own REST *search* endpoint
# (rest.uniprot.org/uniprotkb/search) for every real, curated UniProt entry
# tagged with UniProt's own "Virulence" keyword (KW-0843) AND matched to one
# specific organism's real NCBI Taxonomy ID -- an edge exists here because
# UniProt's own taxonomy + keyword annotation directly ties that protein to
# that organism, not because of any shared sentence. Source is always a
# Table3 MICROBE element (the organism itself, never a protein); Target is
# the real virulence protein's own name. Taxonomy IDs come from
# organism_taxonomy_ids.csv (see _load_organism_taxonomy_ids below) -- a
# real, user-verified NCBI Taxonomy ID per organism, built and checked
# separately from this pipeline's own code.
ORGANISM_TAXONOMY_FILE = HERE / "organism_taxonomy_ids.csv"
# _v2: renamed when the go_terms (go_f) field was added to the cached
# records below, so a pre-existing 3-field cache (protein_name, accession,
# function_text only) from before that field existed can never be silently
# read back and treated as if it already had a (missing) go_terms key.
UNIPROT_VIRULENCE_CACHE_DIR = HERE / "uniprot_virulence_cache_v2"
UNIPROT_VIRULENCE_KEYWORD = "KW-0843"  # UniProt's own official "Virulence" keyword ID
# Fixed, deterministic Relation text find_uniprot_virulence_mentions gives
# every real virulence edge (see that function's own docstring for why this
# is fixed rather than LLM-authored) -- pulled into one shared constant
# (previously 2 separate string literals) so build_table3_knowledge_graph
# can reliably detect a virulence row from table3_df alone (it only
# receives the already-built DataFrame, not the raw edge dicts'
# _is_virulence_edge flag) without a 3rd independent copy of this string
# that could silently drift out of sync.
VIRULENCE_RELATION_LABEL = "UniProt-annotated virulence factor (KW-0843)"

def _load_organism_taxonomy_ids() -> dict:
    """Real, user-verified {element_name: ncbi_taxonomy_id} map for this
    pipeline's microbe elements, from organism_taxonomy_ids.csv (columns:
    Organism, NCBI Taxonomy ID, NCBI Matched Name, Notes). 'Organism' values
    match Table3 element names verbatim (confirmed against CaseStudy's own
    element list -- e.g. 'Streptococcus sanguinis', 'Dialister invisus').
    Only rows with a non-empty NCBI Taxonomy ID are kept -- an organism this
    file couldn't verify a real taxid for is skipped here entirely, never
    given a fabricated one. Cached for the life of one process run, same
    _KB_CACHE convention as _load_cytokine_registry above. This is also
    the pipeline's only real microbe-identity source now that MiMeDB is
    fully removed -- see build_table3_knowledge_graph's node-shape
    detection, which uses this instead."""
    if _KB_CACHE["organism_taxonomy"] is not None:
        return _KB_CACHE["organism_taxonomy"]
    out = {}
    if not ORGANISM_TAXONOMY_FILE.exists():
        print(f"KB file not found (skipping): {ORGANISM_TAXONOMY_FILE}")
    else:
        try:
            df = pd.read_csv(ORGANISM_TAXONOMY_FILE, dtype=str, keep_default_na=False)
            for _, row in df.iterrows():
                organism = str(row.get("Organism", "")).strip()
                taxid = str(row.get("NCBI Taxonomy ID", "")).strip()
                if organism and taxid:
                    out[organism] = taxid
        except Exception as e:
            print(f"Could not read {ORGANISM_TAXONOMY_FILE}: {e}")
    _KB_CACHE["organism_taxonomy"] = out
    return out

def _load_organism_taxonomy_synonyms() -> dict:
    """Real, already-verified {element_name: ncbi_matched_name} map, from
    organism_taxonomy_ids.csv's own 'NCBI Matched Name' column -- only kept
    when that column is non-empty AND actually differs from 'Organism'
    (e.g. 'Lactobacillus panis' -> 'Limosilactobacillus panis', a real 2020
    genus reclassification NCBI's own Taxonomy database records; blank for
    rows where NCBI's canonical name already matches the CaseStudy element
    name exactly, e.g. 'Dialister micraerophilus'). This brings microbe
    elements' PubMed search up to the same real-synonym standard
    cytokine/protein elements already have via the ImmPort Cytokine
    Registry (find_gene_identity_info) and cell-type elements already
    have via _acronym_expansions. No new lookup happens here -- this
    reads the exact same, already-verified CSV _load_organism_taxonomy_ids
    reads, just a different column, so nothing is guessed or fabricated.
    Cached with its own _KB_CACHE key so a fresh read of the CSV isn't
    forced just because _load_organism_taxonomy_ids happened to run first
    (or not at all) this process."""
    if _KB_CACHE.get("organism_taxonomy_synonyms") is not None:
        return _KB_CACHE["organism_taxonomy_synonyms"]
    out = {}
    if ORGANISM_TAXONOMY_FILE.exists():
        try:
            df = pd.read_csv(ORGANISM_TAXONOMY_FILE, dtype=str, keep_default_na=False)
            for _, row in df.iterrows():
                organism = str(row.get("Organism", "")).strip()
                matched = str(row.get("NCBI Matched Name", "")).strip()
                if organism and matched and matched.lower() != organism.lower():
                    out[organism] = matched
        except Exception as e:
            print(f"Could not read {ORGANISM_TAXONOMY_FILE} for synonyms: {e}")
    _KB_CACHE["organism_taxonomy_synonyms"] = out
    return out

def _clean_uniprot_cc_function_text(raw: str) -> str:
    """Clean a raw 'Function [CC]' TSV cell from UniProt's search endpoint
    (format confirmed via live testing, e.g. 'FUNCTION: Toxin, which has
    some hemolytic activity ... {ECO:0000269|PubMed:14532000}.') into the
    same plain-prose form _fetch_uniprot_function's JSON-based FUNCTION text
    already uses elsewhere in this file (no 'FUNCTION:' label, no inline
    '{ECO:...}' evidence-code tags). A real inline citation written directly
    in the running prose (e.g. '(PubMed:21829394)', OUTSIDE the {ECO:...}
    braces) is deliberately left untouched -- that's real evidence text, not
    an internal provenance tag. An entry with more than one FUNCTION comment
    is joined by UniProt's own TSV export with '; FUNCTION: ' between them;
    that joiner is normalized to a plain sentence boundary once the labels
    are stripped, rather than left as a dangling '; '. Returns '' unchanged
    if the cell was already empty -- a real, valid case (several real
    KW-0843-tagged entries, confirmed via live testing on Streptococcus
    sanguinis, have no curated FUNCTION comment at all)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace(".; FUNCTION:", ".").replace("; FUNCTION:", ".")
    text = re.sub(r"^FUNCTION:\s*", "", text)
    text = re.sub(r"\s*\{ECO:[^}]*\}", "", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text

# A KW-0843-matched entry's real UniProt FUNCTION text and GO (molecular
# function) terms (see _fetch_uniprot_virulence_proteins' `go_f` field
# below) are fed to a small, separate, batched LLM call --
# PROMPT_UNIPROT_VIRULENCE_DESCRIPTION /
# _label_uniprot_virulence_descriptions_via_llm, defined further down next
# to their sibling PROMPT_UNIPROT_RELATION / _label_uniprot_relations_via_llm
# -- which writes a short, grounded description of that protein's role as a
# virulence factor using only that real text, never inventing a mechanism.
# When an entry has neither FUNCTION text nor GO terms, the bare word
# "Virulence" is still an accurate, non-fabricated fallback label, because
# every record reaching that prompt already matched UniProt's own
# "Virulence" keyword (KW-0843) on that exact entry. See
# find_uniprot_virulence_mentions and build_kb_sourced_table2_rows below
# for how the LLM-authored text (or that fallback) reaches Table 3's
# Evidence/quote column.

def _fetch_uniprot_virulence_proteins(taxid: str, organism: str = "") -> list:
    """Live UniProt REST *search* query (rest.uniprot.org/uniprotkb/search --
    NOT the per-accession endpoint _fetch_uniprot_function uses above) for
    every real UniProt entry tagged with keyword KW-0843 ("Virulence") AND
    matched to this organism's real NCBI Taxonomy ID. Query shape confirmed
    working via live testing:
    '(keyword:KW-0843) AND (taxonomy_id:<ID>)', fields=protein_name,
    accession,cc_function,go_f, format=tsv -- header row 'Protein names /
    Entry / Function [CC] / Gene Ontology (molecular function)'. `go_f` is
    UniProt's own real field ID for "Gene Ontology (molecular function)"
    (confirmed via live testing, e.g. a real returned cell:
    'identical protein binding [GO:0042802]; toxin activity [GO:0090729]').

    Returns a list of {protein_name, accession, function_text, go_terms}
    dicts (one per real matched UniProt entry, function_text already
    cleaned by _clean_uniprot_cc_function_text; go_terms is the raw,
    semicolon-separated 'term [GO:NNNNNNN]' cell text, left unsplit --
    downstream consumers only need it as prose for the virulence-
    description LLM call, not as structured GO IDs). An EMPTY LIST is a
    real, valid result -- confirmed via live testing that several real
    organisms genuinely have zero KW-0843-tagged entries -- and is cached
    exactly like a non-empty one. Returns None (never an empty list) only
    on a genuine network/parse failure, so callers can tell 'confirmed zero
    real results' apart from 'could not check this run' and never crash the
    run either way -- same graceful-degradation contract as
    _fetch_uniprot_function above. Cached permanently to disk
    (UNIPROT_VIRULENCE_CACHE_DIR, same convention as UNIPROT_CACHE_DIR/
    CL_CACHE_DIR elsewhere in this file) -- KW-0843 tagging for a given
    taxon doesn't change run to run, so repeated pipeline runs over the
    same organisms skip the live network call entirely after the first
    time. UNIPROT_VIRULENCE_CACHE_DIR was renamed (v2 suffix) when the
    go_terms field was added, specifically so any pre-existing 3-field
    cache file from before this field existed can never be silently read
    back as if it already had a (missing) go_terms key -- old cache files
    under the old dir name are simply never looked at again, not migrated."""
    tid = str(taxid or "").strip()
    if not tid or tid.lower() == "nan":
        return None
    cache_file = UNIPROT_VIRULENCE_CACHE_DIR / f"{tid}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # Defensive: only trust a cached record if it actually has the
            # go_terms key (guards against a hand-edited/foreign cache file
            # under this same dir name slipping through with the old shape).
            if all(isinstance(r, dict) and "go_terms" in r for r in cached):
                return cached
        except Exception:
            pass  # corrupt cache file -- fall through and refetch

    query = f"(keyword:{UNIPROT_VIRULENCE_KEYWORD}) AND (taxonomy_id:{tid})"
    params = {"query": query, "fields": "protein_name,accession,cc_function,go_f", "format": "tsv"}
    url = f"https://rest.uniprot.org/uniprotkb/search?{urllib.parse.urlencode(params)}"
    print(f"UniProt virulence search: querying KW-0843 x taxonomy_id:{tid} "
          f"({organism or 'unknown organism'})...")
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"UniProt virulence search failed for taxonomy_id:{tid} "
              f"({organism or 'unknown organism'}) ({e}); skipping this organism's virulence layer.")
        return None

    lines = raw.splitlines()
    expected_header = ["Protein names", "Entry", "Function [CC]", "Gene Ontology (molecular function)"]
    if not lines or lines[0].split("\t")[:4] != expected_header:
        # A well-formed real response always starts with this exact header
        # -- its absence means this wasn't the real TSV response (same
        # "don't cache a malformed response" guard _fetch_uniprot_function
        # uses for a missing primaryAccession above).
        print(f"UniProt virulence search for taxonomy_id:{tid} returned an unexpected/malformed "
              f"response (not the expected TSV header) -- not caching, will retry next run.")
        return None

    records = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        cols += [""] * (4 - len(cols))  # a blank trailing cell (Function [CC] and/or GO) can shorten the row
        protein_name, accession, function_raw, go_raw = cols[0].strip(), cols[1].strip(), cols[2], cols[3]
        if not accession:
            continue
        records.append({
            "protein_name": protein_name,
            "accession": accession,
            "function_text": _clean_uniprot_cc_function_text(function_raw),
            "go_terms": str(go_raw or "").strip(),
        })

    try:
        ensure_dir(UNIPROT_VIRULENCE_CACHE_DIR)
        cache_file.write_text(json.dumps(records), encoding="utf-8")
    except Exception:
        pass
    print(f"UniProt virulence search: taxonomy_id:{tid} ({organism or 'unknown organism'}) -> "
          f"{len(records)} real KW-0843-tagged entr{'y' if len(records) == 1 else 'ies'}.")
    return records

def find_uniprot_virulence_mentions(elements) -> list:
    """For each Table3 element that is a real microbe with a known NCBI
    Taxonomy ID (organism_taxonomy_ids.csv, see _load_organism_taxonomy_ids),
    live-queries UniProt's own KW-0843 ("Virulence" keyword) search for
    every real curated protein entry UniProt itself has tagged as a
    virulence factor for that exact taxon (see
    _fetch_uniprot_virulence_proteins). One dict per real matched UniProt
    entry, with keys: Source (organism element name), Target (protein
    name), Accession (real UniProt accession -- this row's own citable ID,
    same _KB_EDGE_ID_FIELD['UniProt'] convention find_uniprot_function_
    mentions' edges already use), Function_Text (that protein's real,
    cleaned UniProt FUNCTION text, may be empty), GO_Terms (that protein's
    real GO molecular-function terms, may be empty), Relationship (a fixed,
    deterministic label -- see below for why), Source_DB, and
    _is_virulence_edge=True (lets build_kb_sourced_table2_rows tell these
    apart from the co-mention layer above without needing a different
    Evidence Source label -- both are still real 'UniProt' rows). No final
    Evidence/description string is computed here anymore -- Function_Text
    and GO_Terms are carried on the edge as-is, and
    _label_uniprot_virulence_descriptions_via_llm (a separate, small,
    batched LLM call defined near PROMPT_UNIPROT_VIRULENCE_DESCRIPTION)
    turns them into a grounded description afterward, inside
    build_kb_sourced_table2_rows.

    These edges are NOT run through _label_uniprot_relations_via_llm (the
    batched LLM Relation-labeling call used for find_uniprot_function_
    mentions' edges): that prompt asks the
    LLM to label a relationship strictly from a real quoted SENTENCE that
    mentions both Element A and Element B by name. These edges have no such
    sentence -- the organism's own name essentially never appears inside
    its protein's own FUNCTION text -- so feeding them through that prompt
    would either force a false 'co-mentioned' fallback on every single row,
    or risk the LLM inferring a relationship the text doesn't actually
    support. The real, fully-grounded fact here is instead structural, not
    textual: 'this organism's own UniProt-curated proteome includes a
    keyword-tagged (KW-0843) virulence factor' -- so a fixed, deterministic
    Relationship label is used instead, the same convention
    find_kb_edges_for_elements/find_kb_neighborhood_edges already use for
    ImmuneXpresso's own hardcoded 'cell-cytokine interaction' Relationship
    text. That deterministic label is only the Relationship (the edge
    *type*); the separate description/evidence *text* shown alongside it is
    what the new LLM call above produces from Function_Text/GO_Terms."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    tax_map = _load_organism_taxonomy_ids()
    if not tax_map or not elements:
        return []
    edges = []
    for elem in elements:
        taxid = tax_map.get(elem)
        if not taxid:
            continue
        records = _fetch_uniprot_virulence_proteins(taxid, elem)
        if not records:
            continue
        for r in records:
            accession = r.get("accession", "")
            protein_name = r.get("protein_name", "")
            if not accession or not protein_name:
                continue
            edges.append({
                "Source": elem, "Target": protein_name,
                "Accession": accession,
                "Relationship": VIRULENCE_RELATION_LABEL,
                "Function_Text": r.get("function_text", ""),
                "GO_Terms": r.get("go_terms", ""),
                "Source_DB": f"UniProt (keyword search, KW-0843 x taxonomy_id:{taxid})",
                "_is_virulence_edge": True,
            })
    return edges


# ─────────────────── Microbe taxonomy (MiMeDB) ──────────────────────────────
# MiMeDB is not used by this pipeline. Microbe detection for the Table 3
# knowledge graph uses organism_taxonomy_ids.csv instead (see
# build_table3_knowledge_graph), the same taxonomy source
# find_uniprot_virulence_mentions already uses.

# ─────────────────── KB citation ID lookups ─────────────────────────────────
# Prompt 2 shows the LLM no KB content at all -- it's PubMed-abstracts-only,
# same as Prompt 1/Table 1. KB-sourced Table 2/3 rows are instead built
# directly in Python from the ImmuneXpresso/UniProt data (see
# build_kb_sourced_table2_rows, below find_uniprot_function_mentions), with
# no LLM summarization step in between -- structurally eliminating any
# citation-fabrication risk an LLM summarization step over KB content would
# carry.
#
# _KB_BLOCK_NO_CAP is used by _build_kb_id_index below: neither
# find_kb_edges_for_elements (direct pairs, both ends already in `elements`)
# nor find_kb_neighborhood_edges (real per-element partners, only one end
# required to be in `elements`) caps ImmuneXpresso results when called with
# this value -- top_n_per_element is passed a very large number so nothing
# gets silently trimmed the way the network FIGURE intentionally trims to
# its top few partners for readability.
_KB_BLOCK_NO_CAP = 1_000_000

# ─────────────────── Prompt 2: Table 2 (co-shift functional groups) ────────
def _split_part_a_part_b(raw: str) -> tuple:
    """Splits one PROMPT_COSHIFT_COMBINED response into its two literal
    "### PART A" / "### PART B" sections (case-insensitive marker match).
    Returns (part_a_text, part_b_text); either half is "" if its marker
    wasn't found, so callers can detect and log a malformed response
    instead of silently mixing the two tables together."""
    marker_a = re.search(r"#{1,4}\s*part\s*a\b", raw, re.IGNORECASE)
    marker_b = re.search(r"#{1,4}\s*part\s*b\b", raw, re.IGNORECASE)
    if not marker_a or not marker_b or marker_b.start() <= marker_a.start():
        return raw, ""  # malformed -- treat whole response as Part A only, Part B empty
    return raw[marker_a.end():marker_b.start()], raw[marker_b.end():]

# ─── KB-sourced-row validation for Prompt 2 (Evidence Source can be "PMID"
# or one of the pipeline's 2 KB database names) ───────────────────────────
_KB_SOURCE_KEYS = ("immunexpresso", "uniprot")
_KB_SOURCE_DISPLAY_NAMES = {
    "immunexpresso": "ImmuneXpresso", "uniprot": "UniProt",
}
# Known fixed ID prefix for the 1 source that has one (see
# _build_kb_id_index for where it's populated from) -- used only by
# _resolve_ambiguous_kb_citation to reconstruct a real ID the LLM cited
# without its prefix. UniProt accessions have no single fixed prefix, so
# they're deliberately not in this table.
_KB_ID_PREFIXES = {"immunexpresso": "CL_"}

def _match_kb_source_key(evidence_source_raw: str):
    """Normalize a free-text 'Evidence Source' cell to one of the 2 KB
    index keys (immunexpresso/uniprot), or None if it names something else
    (e.g. "PMID"/"Abstract"/the generic "Knowledge Base"). Substring match,
    case-insensitive -- tolerant of the LLM writing "ImmuneXpresso
    database" or similar instead of the bare name."""
    low = (evidence_source_raw or "").strip().lower()
    for key in _KB_SOURCE_KEYS:
        if key in low:
            return key
    return None

def _resolve_ambiguous_kb_citation(id_raw: str, kb_index: dict, listed_elements_norm: set):
    """When the LLM names a KB citation's Evidence Source too vaguely to
    match one of the 2 real names directly (e.g. writing the generic
    "Knowledge Base" instead of "ImmuneXpresso"/"UniProt") and/or drops a
    real record ID's known
    prefix (citing "235" or "0000235" instead of the real "CL_0000235"),
    tries to recover which real record was actually meant -- WITHOUT ever
    accepting a guess.

    For each source with a known fixed prefix (_KB_ID_PREFIXES), builds a
    small set of plausible real-format reconstructions of id_raw (as
    given, prefixed, prefixed + zero-padded to the two real digit widths
    seen in this KB's own IDs), and keeps a candidate only if it is BOTH
    a real key in that source's kb_index AND actually involves at least
    one of this row's real listed elements -- the exact same
    anti-fabrication overlap check the normal (unambiguous-source-name)
    KB validation branch already requires; a format match alone is never
    enough. Returns (source_key, real_id) only if EXACTLY ONE (source,
    id) combination survives that filter across both sources combined
    (an unambiguous, evidence-backed resolution) -- returns None if zero
    or more than one qualify, so a genuinely ambiguous or unmatched
    citation still gets dropped rather than guessed at."""
    id_raw = (id_raw or "").strip()
    if not id_raw:
        return None
    digits = re.sub(r"\D", "", id_raw)
    hits = []
    for source, records in kb_index.items():
        candidates = {id_raw}
        prefix = _KB_ID_PREFIXES.get(source)
        if prefix and digits:
            candidates.add(f"{prefix}{digits}")
            candidates.add(f"{prefix}{digits.zfill(7)}")
            candidates.add(f"{prefix}{digits.zfill(5)}")
        for cand in candidates:
            real_names = records.get(cand)
            if real_names and (real_names & listed_elements_norm):
                hits.append((source, cand))
    unique_hits = list(dict.fromkeys(hits))
    return unique_hits[0] if len(unique_hits) == 1 else None

def _build_kb_id_index(elements: list, sample_model: str = None) -> dict:
    """Real KB records, indexed as {source_key: {id: {normalized real
    element-name strings that record actually involves}}}, built from the
    same real ImmuneXpresso/UniProt data build_kb_sourced_table2_rows
    itself uses (so the exact IDs are exactly what's checked against
    here). This is the KB-side counterpart to _quote_is_grounded: Prompt 2
    has the LLM SUMMARIZE (not quote verbatim) a KB record, so word-for-
    word grounding doesn't apply -- what CAN be verified is that the
    cited ID is a real record for that source, and that at least one
    element the LLM named actually belongs to that specific record
    (catches a fabricated ID or a real ID misattributed to the wrong
    elements). MASI and MiMeDB are not used by this pipeline -- ImmuneXpresso
    and UniProt are its only 2 KB sources, so a stray LLM citation naming
    either source correctly fails validation as "not a real record" (no
    index key for it at all)."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    idx = {k: {} for k in _KB_SOURCE_KEYS}
    if not elements:
        return idx

    all_edges = (find_kb_edges_for_elements(elements) +
                 find_kb_neighborhood_edges(elements, top_n_per_element=_KB_BLOCK_NO_CAP))
    for e in all_edges:
        if "ImmuneXpresso" in e.get("Source_DB", ""):
            rid = e.get("Cell_Ontology_ID", "")
            if rid:
                idx["immunexpresso"].setdefault(rid, set()).update(
                    {_normalize_for_match(e.get("Cell", "")), _normalize_for_match(e.get("Cytokine", ""))})

    try:
        # Keyed by the SAME real UniProt accession find_uniprot_function_
        # mentions' "Accession" field returns -- so a citation is checked
        # against the exact ID the LLM was actually shown.
        for e in find_uniprot_function_mentions(elements, sample_model):
            uid = e.get("Accession", "")
            if uid:
                idx["uniprot"].setdefault(uid, set()).update(
                    {_normalize_for_match(e.get("Source", "")), _normalize_for_match(e.get("Target", ""))})
    except Exception as ex:
        print(f"_build_kb_id_index: UniProt lookup failed ({ex}); continuing without it.")

    return idx

def _split_element_list_cell(text: str) -> list:
    """Split a Part A 'List of elements' cell (2+ element names in one
    pipe-delimited cell, separator not pinned down by the prompt itself)
    on comma/semicolon/slash, tolerating an "and"-joined last item."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"[;,/]|\band\b", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]

def _build_coshift_prompt(batch: list, elements: list, context: str = "disease") -> str:
    """Builds the exact Prompt 2 (PROMPT_COSHIFT_COMBINED, via
    get_coshift_combined_prompt) text that extract_and_group_coshift_from_batch
    sends to the LLM, without calling the API.

    This prompt is PubMed-abstracts-only -- no {knowledge_base_*}
    placeholders in the template. KB-sourced Table 2/3 rows are built
    separately, straight from KB data, by build_kb_sourced_table2_rows --
    no LLM call involved in that path at all, so there is no KB-content
    token budget to manage here."""
    valid_elements = [str(e).strip() for e in elements if str(e).strip()]
    element_list_str = "\n".join(f"- {e}" for e in sorted(valid_elements))
    prompt_template = get_coshift_combined_prompt(context)
    abstracts_text = _format_abstracts_block(batch) if batch else (
        "(No PubMed abstracts co-mention 2 or more elements from the Element list above.)")
    return prompt_template.format(
        element_list=element_list_str, abstracts=abstracts_text,
        study_context=_study_context_block_for_prompt(),
    )

def extract_and_group_coshift_from_batch(batch: list, elements: list, context: str = "disease") -> dict:
    """ONE stateless LLM call over a batch of real abstracts (each
    mentioning 2+ input elements) using the merged Prompt 2
    (BioShift_Prompts_0729_PD). Part A reports biological relationships
    -- each involving 2 OR MORE elements -- supported by a single abstract
    (Evidence Source "PubMed", since "PMID" names an ID format, not a
    source name, unlike "ImmuneXpresso"/"UniProt"). Part B breaks each
    validated Part A relationship down into its pairwise (Element A,
    Relation, Element B) statements.

    Prompt 2 shows the LLM no Knowledge Base content at all -- it's
    PubMed-abstracts-only, same as Prompt 1. KB-sourced Table 2/3 rows
    (ImmuneXpresso/UniProt) are instead built separately, directly in
    Python with no LLM call, by build_kb_sourced_table2_rows (called once
    from build_table2_coshift, not per-batch here) -- deterministic, so
    it only needs to run once, not once per PUBMED_EXTRACTION_RUNS run.
    The KB-sourced-Evidence-Source validation branch below is kept as a
    defensive fallback only (in case a model still names a KB source on
    its own) and should not normally trigger now that the prompt never
    mentions KB sources.

    "Grouping" by shared biological theme is gone -- "Biological
    Relationship Name" is a per-row label for one specific evidence item,
    not an aggregated cluster name.

    Validation:
      Part A -- PMID-sourced: PMID must be real (in this batch), quote
        must be grounded in that abstract's own text (_quote_is_grounded).
      Part A -- KB-sourced (defensive fallback, not expected in normal
        operation -- see note above): Evidence Source must name one of
        the 2 real KB sources, the cited ID must be a real record for
        that source (_build_kb_id_index), and at least one element the
        LLM listed must actually belong to that specific record (catches
        a fabricated or misattributed KB citation; word-for-word
        grounding doesn't apply since the LLM is asked to summarize, not
        quote, KB content).
      Part A -- either source: at least one of the "List of elements"
        entries must resolve to a real master-list element (this
        relationship must actually be "for elements in the Element list",
        per the prompt's own Input Data framing) -- entries that don't
        resolve are kept as their own literal names rather than dropped,
        consistent with this pipeline's existing KB-neighborhood-
        expansion convention (a real, named partner outside the master
        list is still real evidence, just not itself one of our
        elements).
      Part B -- every row must match an already-validated Part A row on
        (Evidence Source, ID), and both Element A and Element B must be
        members of that SAME Part A row's element list -- Part B can
        never smuggle in a pair Part A didn't validate.

    Returns {"part_a": [...], "part_b": [...]}:
      part_a row: {relationship_name, id, evidence_source, elements
        (list, canonical name where resolved else literal), quote}
      part_b row: {id, evidence_source, element_a, relation, element_b,
        quote}"""
    valid_elements = [str(e).strip() for e in elements if str(e).strip()]
    if not batch and not valid_elements:
        return {"part_a": [], "part_b": []}
    real_pmids = {a["pmid"] for a in batch}
    text_by_pmid = {a["pmid"]: f"{a.get('title', '')} {a.get('abstract', '')}" for a in batch}
    element_alias_map = _build_element_alias_map(valid_elements)
    kb_index = _build_kb_id_index(valid_elements, SAMPLE_MODEL)
    prompt = _build_coshift_prompt(batch, valid_elements, context)
    # Prompt-2-specific model/max_tokens override (COSHIFT_MODEL/
    # COSHIFT_MAX_TOKENS in config.txt) -- see load_coshift_model's
    # docstring: this step's real KB blocks compete with the abstracts for
    # the model's attention within one response, so it gets its own
    # (optionally stronger/longer) call instead of always reusing
    # DEFAULT_MODEL/MAX_TOKENS.
    raw = call_openai(prompt, model=COSHIFT_MODEL, max_tokens=COSHIFT_MAX_TOKENS)
    part_a_raw, part_b_raw = _split_part_a_part_b(raw)
    if not part_b_raw.strip():
        print("WARNING: co-shift combined response had no parseable '### PART B' section -- "
              "no pairwise breakdown will be available for this batch. "
              f"First 300 chars: {raw[:300]!r}")

    # -- Part A --
    # NOTE: a single PMID/KB ID CAN legitimately yield more than one
    # distinct Part A relationship (e.g. one abstract stating two separate
    # biological connections) -- part_a_rows is a plain list so none of
    # those get silently overwritten. elements_by_key accumulates the
    # UNION of every validated row's elements for a given (source, id),
    # since Part B's own rows only cite the ID (not which specific Part A
    # relationship they came from) and so must be checked against
    # everything Part A validated for that ID.
    table_text = _extract_clean_table(part_a_raw, min_cols=4)
    part_a_rows = []
    elements_by_key = defaultdict(set)  # (source_key_or_'pmid', id) -> {normalized element names}
    dropped_a = []
    for i, line in enumerate(table_text.splitlines()):
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) < 5:
            continue
        if i == 0 and parts[0].lower().startswith("biological relationship"):
            continue  # header row
        rel_name, id_raw, source_raw, elements_raw, quote = parts[0], parts[1], parts[2], parts[3], parts[4]
        kb_key = _match_kb_source_key(source_raw)
        # Matches both "pubmed" (the real Evidence Source value Prompt 2
        # now asks for) and "pmid" (kept for robustness -- an occasional
        # model deviation, or older cached content, might still write the
        # previous value; either way this is still real PubMed evidence).
        is_pmid = kb_key is None and (
            "pubmed" in source_raw.strip().lower() or "pmid" in source_raw.strip().lower())

        raw_tokens = _split_element_list_cell(elements_raw)
        resolved = [element_alias_map.get(_normalize_for_match(t)) for t in raw_tokens]
        row_elements = [r if r is not None else t for r, t in zip(resolved, raw_tokens)]
        if not any(r is not None for r in resolved):
            dropped_a.append((id_raw, source_raw, elements_raw, "no master-list element in row"))
            continue

        record_names = None  # real known names for the cited KB record, if this is a KB-sourced row
        if is_pmid:
            pmid = re.sub(r"\D", "", id_raw)
            if pmid not in real_pmids:
                dropped_a.append((id_raw, source_raw, elements_raw, "unknown PMID"))
                continue
            if not _quote_is_grounded(quote, text_by_pmid.get(pmid, "")):
                # Includes the actual claimed quote (truncated) so a real
                # rejection can be diagnosed from the log alone -- see the
                # matching change in extract_multi_element_directions_from_batch.
                dropped_a.append((id_raw, source_raw, elements_raw, "quote not grounded", quote[:150]))
                continue
            if not _quote_has_nearby_disease_context(quote, text_by_pmid.get(pmid, "")):
                # Soft flag only -- see _quote_has_nearby_disease_context's
                # docstring. Real, verbatim quote, just not confirmed to
                # sit in a sentence about this study's own disease context
                # (possible background-citation-to-unrelated-work case).
                # Kept, not dropped.
                print(f"  NOTE: PMID:{pmid} quote for row (id={id_raw!r}) has no disease-context "
                      f"term in its own sentence -- possible off-topic/background-citation risk, "
                      f"worth a manual check: {quote[:150]!r}")
            key = ("pmid", pmid)
            evidence_source_display = "PubMed"
        elif kb_key is not None:
            record_id = id_raw.strip()
            records = kb_index.get(kb_key, {})
            if record_id not in records:
                dropped_a.append((id_raw, source_raw, elements_raw, f"ID not a real {kb_key} record"))
                continue
            record_names = records[record_id]
            row_names_norm = {_normalize_for_match(t) for t in raw_tokens}
            if not (row_names_norm & record_names):
                dropped_a.append((id_raw, source_raw, elements_raw,
                                   f"none of the listed elements belong to this {kb_key} record"))
                continue
            key = (kb_key, record_id)
            evidence_source_display = source_raw.strip()
        else:
            # Evidence Source didn't name one of the 2 real KB sources
            # directly and isn't "PMID" either -- before giving up, try
            # to recover a real, unambiguous, element-overlap-verified KB
            # record it might still refer to (e.g. the LLM wrote the
            # generic "Knowledge Base" or dropped a real ID's prefix; see
            # _resolve_ambiguous_kb_citation's docstring for why this
            # stays anti-fabrication-safe).
            row_names_norm = {_normalize_for_match(t) for t in raw_tokens}
            resolved_kb = _resolve_ambiguous_kb_citation(id_raw, kb_index, row_names_norm)
            if resolved_kb is None:
                dropped_a.append((id_raw, source_raw, elements_raw, f"unrecognized Evidence Source '{source_raw}'"))
                continue
            kb_key, record_id = resolved_kb
            record_names = kb_index.get(kb_key, {}).get(record_id, set())
            key = (kb_key, record_id)
            evidence_source_display = _KB_SOURCE_DISPLAY_NAMES[kb_key]

        # "At least one resolved master-list element" (checked above) only
        # guarantees the row as a whole is anchored to something real --
        # it doesn't verify that an EXTRA name beyond the master list
        # (e.g. an unrelated element riding along with a real one) is
        # actually supported by this row's own cited evidence. Every
        # resolved master-list element is kept unconditionally; only
        # unresolved extras are checked, each against the text that
        # specific row's own evidence actually is -- the quote itself for
        # a PMID row, or the cited KB record's own known participant
        # names for a KB row (KB quotes are LLM summaries, not verbatim
        # text, so word-for-word grounding wouldn't be a meaningful check
        # there).
        if record_names is not None:
            # Tight (space-stripped) comparison on both sides -- see
            # _tight_norm's docstring: the LLM's own "List of elements"
            # cell and the real KB data underneath it can spell the same
            # real name with/without a hyphen (e.g. 'IL-6' vs the real
            # UniProt data's 'IL6'), which plain _normalize_for_match
            # would treat as different strings ('il 6' vs 'il6') even
            # though they're the same real name. Safe here specifically
            # because record_names is a set of short, discrete whole
            # names, not a block of running text.
            record_names_tight = {n.replace(" ", "") for n in record_names}
            def _extra_is_grounded(t):
                return _tight_norm(t) in record_names_tight
        else:
            quote_norm = _normalize_for_match(quote)
            def _extra_is_grounded(t):
                return bool(_normalize_for_match(t)) and _normalize_for_match(t) in quote_norm
        kept_elements, dropped_extras = [], []
        for elem, r, t in zip(row_elements, resolved, raw_tokens):
            if r is not None or _extra_is_grounded(t):
                kept_elements.append(elem)
            else:
                dropped_extras.append(t)
        if dropped_extras:
            print(f"  Co-shift Part A: dropped {len(dropped_extras)} ungrounded extra element(s) "
                  f"{dropped_extras} from row (id={id_raw!r}, source={source_raw!r}) -- not in the "
                  "master list and not supported by this row's own cited evidence; kept the row's "
                  "real master-list element(s).")
        row_elements = kept_elements

        # The LLM sometimes writes a correctly-cited multi-element finding
        # but only names one of those elements in "List of elements",
        # even though the quote itself literally names more. Recovers any
        # master-list element the quote itself really names -- the same
        # word-boundary text matching already used to rank/select
        # abstracts (fetch_ranked_combined_pool's _find_mentioned_
        # elements) -- so this only recovers elements the quote genuinely
        # mentions, never invents a relationship the quote doesn't
        # support.
        recovered = [e for e in _find_mentioned_elements(quote, valid_elements) if e not in row_elements]
        if recovered:
            print(f"  Co-shift Part A: recovered {len(recovered)} real master-list element(s) "
                  f"{recovered} for row (id={id_raw!r}, source={source_raw!r}) -- named in the "
                  "quote but missing from 'List of elements'.")
            row_elements = row_elements + recovered

        # Table 2 rows describe a relationship between 2+ elements (a
        # single-element row isn't a co-shift relationship). The
        # extras-grounding filter above can legitimately strip a row down
        # to just one element, so drop the whole row here rather than
        # publish a naked single-element line.
        distinct_elements = list(dict.fromkeys(_normalize_for_match(str(e)) for e in row_elements))
        if len(distinct_elements) < 2:
            dropped_a.append((id_raw, source_raw, elements_raw,
                               f"only {len(distinct_elements)} real element(s) left after grounding "
                               "(Table 2 rows need 2+) -- no partner element for "
                               f"{row_elements[0] if row_elements else '?'}"))
            continue

        part_a_rows.append({
            "relationship_name": rel_name.strip(), "id": key[1],
            "evidence_source": evidence_source_display, "elements": row_elements, "quote": quote,
        })
        elements_by_key[key].update(_normalize_for_match(str(e)) for e in row_elements)
    if part_a_rows:
        accepted_src_counts = Counter(r["evidence_source"] for r in part_a_rows)
        print(f"Co-shift Part A: {len(part_a_rows)} row(s) validated "
              f"(by evidence source: {dict(accepted_src_counts)})")
    if dropped_a:
        # Evidence-source breakdown of the FULL dropped list (not just the
        # 5-row preview) -- makes it visible at a glance whether the LLM
        # ever proposed any KB-sourced (ImmuneXpresso/UniProt) rows at all
        # this batch, vs. only PMID ones, without having to guess from a
        # truncated preview.
        src_counts = Counter(
            (_match_kb_source_key(d[1]) or
             ("pubmed" if ("pubmed" in str(d[1]).lower() or "pmid" in str(d[1]).lower()) else "other"))
            for d in dropped_a)
        print(f"Co-shift Part A: dropped {len(dropped_a)} unverified row(s) "
              f"(by evidence source: {dict(src_counts)}) -- "
              f"{dropped_a[:5]}{'...' if len(dropped_a) > 5 else ''}")
    if not part_a_rows and table_text.strip():
        print(f"WARNING: combined co-shift Part A returned a table but 0 rows validated. "
              f"First 300 chars of raw response: {raw[:300]!r}")
    elif not table_text.strip() and raw.strip():
        print(f"WARNING: combined co-shift response had no parseable '|' table at all. "
              f"First 300 chars: {raw[:300]!r}")

    # -- Part B: pairwise breakdown, only of already-validated Part A rows --
    pair_table_text = _extract_clean_table(part_b_raw, min_cols=5)
    part_b_rows = []
    dropped_b = []
    for i, line in enumerate(pair_table_text.splitlines()):
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) < 6:
            continue
        if i == 0 and parts[0].lower().startswith("pmid"):
            continue  # header row
        id_raw, source_raw, a_raw, relation_raw, b_raw, quote = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        kb_key = _match_kb_source_key(source_raw)
        # Matches both "pubmed" (the real Evidence Source value Prompt 2
        # now asks for) and "pmid" (kept for robustness -- an occasional
        # model deviation, or older cached content, might still write the
        # previous value; either way this is still real PubMed evidence).
        is_pmid = kb_key is None and (
            "pubmed" in source_raw.strip().lower() or "pmid" in source_raw.strip().lower())
        evidence_source_override = None
        if not is_pmid and kb_key is None:
            # Same real-record recovery as Part A (a vague Evidence
            # Source like the generic "Knowledge Base" and/or a
            # prefix-stripped real ID) -- see
            # _resolve_ambiguous_kb_citation's docstring. Uses Part B's
            # own element_a/element_b cells (not Part A's, which may not
            # even be visible here) as the real-overlap check.
            resolved = _resolve_ambiguous_kb_citation(
                id_raw, kb_index, {_normalize_for_match(a_raw), _normalize_for_match(b_raw)})
            if resolved is None:
                dropped_b.append((id_raw, source_raw, a_raw, b_raw, f"unrecognized Evidence Source '{source_raw}'"))
                continue
            kb_key, id_raw = resolved
            evidence_source_override = _KB_SOURCE_DISPLAY_NAMES[kb_key]
        key = ("pmid", re.sub(r"\D", "", id_raw)) if is_pmid else (kb_key, id_raw.strip())
        if key not in elements_by_key:
            dropped_b.append((id_raw, source_raw, a_raw, b_raw, "no matching validated Part A row"))
            continue
        parent_elements_norm = elements_by_key[key]
        a_norm, b_norm = _normalize_for_match(a_raw), _normalize_for_match(b_raw)
        if a_norm not in parent_elements_norm or b_norm not in parent_elements_norm:
            dropped_b.append((id_raw, source_raw, a_raw, b_raw, "element(s) not in that Part A row"))
            continue
        element_a = element_alias_map.get(a_norm, a_raw.strip())
        element_b = element_alias_map.get(b_norm, b_raw.strip())
        evidence_source_display = "PubMed" if is_pmid else (evidence_source_override or source_raw.strip())
        part_b_rows.append({
            "id": key[1], "evidence_source": evidence_source_display,
            "element_a": element_a, "relation": relation_raw.strip(), "element_b": element_b,
            "quote": quote,
        })
    if dropped_b:
        src_counts_b = Counter(
            (_match_kb_source_key(d[1]) or
             ("pubmed" if ("pubmed" in str(d[1]).lower() or "pmid" in str(d[1]).lower()) else "other"))
            for d in dropped_b)
        print(f"Co-shift Part B: dropped {len(dropped_b)} unmatched/unverified pairwise row(s) "
              f"(by evidence source: {dict(src_counts_b)}) -- "
              f"{dropped_b[:5]}{'...' if len(dropped_b) > 5 else ''}")

    # -- Part B pairwise-coverage check (Python, not the LLM) --
    # Python is the authority on which Element-A/B pairs should exist --
    # every unordered pair within one validated Part A row's own element
    # list, via itertools.combinations -- while the LLM's own Part B
    # response stays the sole source of the actual Relation text for a
    # pair (never fabricated here). This block only detects gaps: if a
    # Table 2 relationship supports elements {A, B, C}, that's 3 expected
    # pairs (A-B, A-C, B-C); if the model's own Part B section only wrote
    # 2 of those 3, the 3rd is silently missing from Table 3 without this
    # check. The missing pair is not added to Table 3 and not given an
    # invented Relation -- it's flagged only, like every other
    # soft/diagnostic check in this file.
    actual_pairs_by_key = defaultdict(set)
    for r in part_b_rows:
        key_bp = (r["evidence_source"].strip().lower(), str(r["id"]).strip())
        pair_norm = frozenset({_normalize_for_match(r["element_a"]), _normalize_for_match(r["element_b"])})
        actual_pairs_by_key[key_bp].add(pair_norm)

    missing_pairs = []
    for row in part_a_rows:
        key_ap = (row["evidence_source"].strip().lower(), str(row["id"]).strip())
        # Same de-dup-by-normalized-key approach as the earlier 2+-element
        # check -- keeps one real representative name per distinct
        # element so combinations() never pairs a name with itself under
        # a different spelling.
        seen_norm = {}
        for e in row["elements"]:
            n = _normalize_for_match(str(e))
            if n and n not in seen_norm:
                seen_norm[n] = e
        distinct_row_elements = list(seen_norm.values())
        if len(distinct_row_elements) < 2:
            continue  # already dropped above if this were the only element(s)
        have = actual_pairs_by_key.get(key_ap, set())
        for e1, e2 in itertools.combinations(distinct_row_elements, 2):
            pair_norm = frozenset({_normalize_for_match(e1), _normalize_for_match(e2)})
            if pair_norm not in have:
                missing_pairs.append((row["id"], row["evidence_source"], e1, e2))

    if missing_pairs:
        print(f"  Co-shift Part B: {len(missing_pairs)} real Table 2 pair(s) this batch have NO "
              "matching Part B row -- Table 2 validated these elements together, but the "
              "model's own Part B never gave a Relation for the pair, so it's NOT in Table "
              "3 (not invented here either): "
              f"{missing_pairs[:5]}{'...' if len(missing_pairs) > 5 else ''}")

    return {"part_a": part_a_rows, "part_b": part_b_rows}

def run_coshift_batches(elements: list, gene_info: dict = None, out_dir: Path = None,
                         context: str = "disease", n_runs: int = 1) -> list:
    """Prompt 2 counterpart to run_extraction_ensemble -- successor to the
    old run_coshift_ensemble now that extraction+grouping are ONE call
    (extract_and_group_coshift_from_batch). Fetches the combined abstract
    pool ONCE (real esearch/efetch) and reuses it for all n_runs
    independent passes, since PubMed's own results for a fixed query
    don't change run to run. From that one pool, keeps only abstracts
    that mention 2+ input elements before batching those into
    extract_and_group_coshift_from_batch.

    n_runs defaults to 1 -- the production pipeline (build_table2_coshift)
    calls this with the default, i.e. ONE real pass per batch. n_runs > 1
    exists only so measure_reproducibility.py can fire this same combined
    call multiple times purely to MEASURE stability; this function itself
    never aggregates across runs beyond concatenating each run's own
    batches together.

    Returns a list of n_runs dicts, each {"part_a": [...], "part_b":
    [...]} -- the concatenation, across every batch in that run, of
    extract_and_group_coshift_from_batch's own per-batch return dict."""
    print("=== Fetching abstract pool once (shared across all co-shift runs) ===")
    pool = fetch_ranked_combined_pool(elements, gene_info, out_dir=out_dir)
    multi = [a for a in pool if len(a.get("matched_elements", []) or []) >= 2]
    n_batches = (len(multi) + PUBMED_BATCH_SIZE - 1) // PUBMED_BATCH_SIZE
    # Prompt 2 is PubMed-abstracts-only, so when zero abstracts co-mention
    # 2+ elements there is nothing for the LLM to extract from this step
    # -- n_batches/jobs stay at 0 for this run, same as Table 1's own
    # pattern. KB-sourced Table 2/3 rows are covered separately by
    # build_kb_sourced_table2_rows (Python-only, no LLM call, run once
    # from build_table2_coshift regardless of what PubMed found here).
    abstract_batches = [multi[i:i + PUBMED_BATCH_SIZE] for i in range(0, len(multi), PUBMED_BATCH_SIZE)]
    jobs = []  # (run_idx, batch_no, batch)
    for run_idx in range(1, n_runs + 1):
        for batch_no, batch in enumerate(abstract_batches, start=1):
            jobs.append((run_idx, batch_no, batch))
    print(f"=== Co-shift extracting {len(jobs)} (run, batch) job(s) -- {n_runs} run(s) x "
          f"{n_batches} batch(es) of up to {PUBMED_BATCH_SIZE} abstract(s) ({len(multi)} of "
          f"{len(pool)} mention 2+ elements), "
          f"up to {COSHIFT_MAX_CONCURRENT_LLM_CALLS} concurrent LLM call(s) ===")
    results_by_run = defaultdict(lambda: {"part_a": [], "part_b": []})
    with ThreadPoolExecutor(max_workers=COSHIFT_MAX_CONCURRENT_LLM_CALLS) as pool_exec:
        futures = {
            pool_exec.submit(extract_and_group_coshift_from_batch, batch, elements, context):
                (run_idx, batch_no) for run_idx, batch_no, batch in jobs
        }
        for fut in as_completed(futures):
            run_idx, batch_no = futures[fut]
            try:
                batch_result = fut.result()
            except Exception as e:
                print(f"  Co-shift run {run_idx} batch {batch_no}/{n_batches} failed ({e}); treated as 0 rows.")
                batch_result = {"part_a": [], "part_b": []}
            print(f"  Co-shift run {run_idx}/{n_runs} -- batch {batch_no}/{n_batches} done -- "
                  f"{len(batch_result['part_a'])} relationship(s), {len(batch_result['part_b'])} pairwise row(s).")
            results_by_run[run_idx]["part_a"].extend(batch_result["part_a"])
            results_by_run[run_idx]["part_b"].extend(batch_result["part_b"])
    all_runs = [results_by_run[run_idx] for run_idx in range(1, n_runs + 1)]
    for run_idx, run_result in enumerate(all_runs, start=1):
        print(f"Co-shift run {run_idx}/{n_runs} done -- {len(run_result['part_a'])} relationship(s), "
              f"{len(run_result['part_b'])} pairwise row(s) reported.")
    return all_runs

def _dedupe_evidence_rows(rows: list, id_key: str, source_key: str, extra_keys: tuple = ()) -> list:
    """Collapses duplicate evidence rows -- same Evidence Source + same
    real ID (PMID or KB record) + same *extra_keys values -- keeping the
    first occurrence. A list-valued extra_key (e.g. Part A's "elements")
    is matched as an unordered set (sorted before comparing), since the
    LLM doesn't always list the same real elements in the same order on
    a repeat; scalar extra_keys (e.g. Part B's "element_a"/"element_b")
    stay order-sensitive, since A vs. B carries real directional meaning
    there.

    build_kb_sourced_table2_rows already dedupes its own ImmuneXpresso/
    UniProt rows before this function ever sees them, so calling this on
    KB rows too is a safe no-op. What this function actually matters for
    is PubMed-sourced rows: Table 2/3 runs multiple independent passes
    over the same shared abstract pool (n_runs=PUBMED_EXTRACTION_RUNS --
    see run_coshift_batches/build_table2_coshift; more passes give the
    model more chances to notice/cite evidence it sometimes skips on a
    given pass), so the same PMID can legitimately get re-asked and
    re-report the same relationship more than once. A PMID that
    genuinely supports two different relationships (different element
    sets) still keeps both rows distinct -- only an exact repeat of the
    same (source, id, elements) combination collapses."""
    seen = set()
    out = []
    for r in rows:
        source = str(r.get(source_key, "")).strip().lower()
        key_parts = [source, str(r.get(id_key, "")).strip()]
        for k in extra_keys:
            val = r.get(k, "")
            if isinstance(val, (list, tuple, set)):
                key_parts.append(" ".join(sorted(_normalize_for_match(str(v)) for v in val)))
            else:
                key_parts.append(_normalize_for_match(str(val)))
        key = tuple(key_parts)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

# Evidence Source display name -> the field on each KB edge dict (from
# find_kb_edges_for_elements/find_kb_neighborhood_edges/
# find_uniprot_function_mentions) that holds that source's own real,
# look-up-able record ID -- used by build_kb_sourced_table2_rows below to
# label each row with a real citable ID, never a fabricated one.
_KB_EDGE_ID_FIELD = {
    "ImmuneXpresso": "Cell_Ontology_ID",
    "UniProt": "Accession",
}

# Not one of the 3 protected prompts (PROMPT_COSHIFT_COMBINED/
# PROMPT_PUBMED_EXTRACT_D_MULTI/PROMPT_TABLE_INTERPRETATION) -- a small,
# single-purpose prompt: UniProt-sourced Table 3 rows should have an
# LLM-authored Relation grounded in that row's own Supporting Evidence
# sentence, the same way PubMed-sourced rows already do, instead of a
# generic hardcoded "co-mentioned in UniProt function text" label. Free
# to edit without the same standing-rule protection as the other 3.
#
# Each item also carries Element A's (the source protein's) own Function
# text and GO (molecular function) terms (see find_uniprot_function_
# mentions' Function_Text/GO_Terms, sourced from _fetch_uniprot_function),
# and the LLM is asked for a second, short Functions description of
# Element A itself -- in the same batched call as the Relation phrase,
# not a separate LLM call.
PROMPT_UNIPROT_RELATION = """AI Role
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Data
Below is the "study context." Study Context has the information that describes the dataset being analyzed. It is metadata, not scientific evidence. It specifies the conditions under which the data were collected, including, disease, disease stage, tissue site, host species, experimental modality, taxonomic resolution, and the dataset's Baseline Group and Target Group.

Study context:
{study_context}

Below is the "element list". For each element, the Observed Shift represents a comparison between the dataset's Baseline Group and Target Group, both defined in the Study Context above. An Observed Shift of 1 means the element's value is higher in the Target Group than in the Baseline Group (an increase); -1 means it is lower (a decrease). For this analysis, you will not use the observed shift values.

Element list:
{element_list}

Below is a set of UniProt co-mention items. Each numbered item gives Element A (a protein), Element B, a real Quoted Sentence (verbatim, from Element A's UniProt FUNCTION description) that mentions both, and Element A's own real UniProt Function text and GO (molecular function) terms. Either or both of the latter two may be empty for a given item.

Items:
{items_block}

Analysis Instructions
For each item above, use ONLY the real text given for that item -- never invent a relationship, mechanism, target, or activity the given text does not itself state.

Determine how Element A relates to Element B, using ONLY what that item's Quoted Sentence actually states. If the sentence only shows the two names appearing together without describing a specific interaction between them, the relationship is "co-mentioned," not an invented interaction.

Separately, describe Element A's own biological function, using ONLY that item's real Function text and/or GO terms. If both the Function text and GO terms are empty for an item, its function is "No described function available," not an invented one.

Reporting Instructions
Output exactly two labeled sections. Each section should contain only the requested table.

### PART A
Summarize each item's Relation in a table. Produce exactly one row per item, in the same order as the Items list above; never merge, skip, or reorder items. Columns should be arranged with this order: "Item," "Element A," "Element B," and "Relation."

- "Item": the item number exactly as given above.
- "Element A" and "Element B": copied EXACTLY as given for that item.
- "Relation": a short (2-6 word) phrase describing how Element A relates to Element B, grounded only in that item's Quoted Sentence -- e.g. "binds to," "induces production of," "synergizes with," "required for differentiation of." Write exactly "co-mentioned" when the sentence shows no specific interaction.

The table should be pipe-separated ("|") with header row without divider nor extra spaces.

### PART B
Summarize each item's Functions in a table. Produce exactly one row per item, in the same order as the Items list above; never merge, skip, or reorder items. Columns should be arranged with this order: "Item," "Element A," and "Functions."

- "Item": the item number exactly as given above.
- "Element A": copied EXACTLY as given for that item.
- "Functions": a short (1-2 sentence) description of Element A's own biological function, grounded only in that item's Function text and/or GO terms. Write exactly "No described function available" when both are empty.

The table should be pipe-separated ("|") with header row without divider nor extra spaces.
"""

def _label_uniprot_relations_via_llm(uniprot_edges: list, elements: list) -> dict:
    """Batches every UniProt co-mention edge's (Element A, Element B,
    Supporting Evidence sentence, Element A's own Function text/GO terms)
    into ONE LLM call (PROMPT_UNIPROT_RELATION, structured with the same
    AI Role / Input Data (study_context, element_list) / Analysis
    Instructions / Reporting Instructions shape as Prompt 1/2a, kept as
    its own separate prompt rather than merged into Prompt 2a) asking it
    for two grounded outputs per item, each its own labeled "### PART A" /
    "### PART B" pipe-separated table (parsed the same way as Prompt 2a's
    own Part A/B via _split_part_a_part_b/_extract_clean_table): (a) PART
    A -- a short Relation phrase grounded in that specific sentence, so
    Table 3's UniProt-sourced rows get an LLM-authored Relation (like
    PubMed-sourced rows already do via extract_and_group_coshift_from_batch)
    instead of a generic hardcoded "co-mentioned in UniProt function text"
    label; and (b) PART B -- a short Functions description of Element A
    (the source protein) itself, grounded in that protein's own
    Function_Text/GO_Terms, extending the same "Python only extracts, LLM
    decides" treatment used for the separate virulence-protein layer
    (PROMPT_UNIPROT_VIRULENCE_DESCRIPTION/
    _label_uniprot_virulence_descriptions_via_llm) to this older
    co-mention layer too, in the same batched call rather than a second
    one.

    ONE call for every UniProt edge in a run (not per PUBMED_EXTRACTION_RUNS
    run) -- this is a one-shot labeling task over real, already-fixed
    sentences/function text, not a resampling-for-coverage task the way
    Table 1/2's own PubMed extraction is.

    Never fabricates: the prompt restricts the LLM to only the quoted
    sentence's own wording for Relation, and only the real Function_Text/
    GO_Terms for Functions (with an explicit "No described function
    available" fallback instruction for items where both are genuinely
    empty). The real, verbatim quote (not the generated Relation) is still
    what's shown in Table 3's own Supporting Evidence column regardless of
    what Relation text is chosen here; the Functions description is
    appended alongside it (see build_kb_sourced_table2_rows), never
    replacing it. Any item either table doesn't include, or that fails to
    parse, is left OUT of the returned dict for that half -- callers must
    fall back to that edge's own deterministic Verb_Cue (see
    _find_relationship_verb, already computed by
    find_uniprot_function_mentions) or a plain "co-mentioned" label for
    Relation, and that edge's own raw Function_Text (else "No described
    function available") for Functions -- never a blank value for either.
    call_openai itself never raises (it retries 3x and returns "" on total
    failure), so an empty response here just means every item falls back,
    not an exception.

    Returns {id(edge): {"relation": relation_text, "functions":
    functions_text}} keyed by Python object identity of each dict in
    `uniprot_edges` -- robust to any item being skipped later (e.g. no real
    record ID), unlike a positional index which a `continue` could silently
    desync. A given edge can have only one of "relation"/"functions"
    populated if only one of the two PART tables covered that item number
    (each half is parsed independently); callers already read
    `.get("relation")`/`.get("functions")` with their own fallback, so a
    missing key is equivalent to an empty string."""
    if not uniprot_edges:
        return {}
    items_block = "\n".join(
        f"{i}. Element A: {e.get('Source', '')} | Element B: {e.get('Target', '')} | "
        f"Quoted Sentence: {e.get('Sentence', '')} | "
        f"Function text: {e.get('Function_Text', '') or '(none)'} | "
        f"GO terms: {e.get('GO_Terms', '') or '(none)'}"
        for i, e in enumerate(uniprot_edges, start=1)
    )
    valid_elements = sorted({str(e).strip() for e in elements if str(e).strip()})
    element_list_str = "\n".join(f"- {e}" for e in valid_elements)
    study_context = _study_context_block_for_prompt()
    prompt = PROMPT_UNIPROT_RELATION.format(
        items_block=items_block, element_list=element_list_str, study_context=study_context)
    raw = call_openai(prompt, model=COSHIFT_MODEL, max_tokens=COSHIFT_MAX_TOKENS)
    part_a_raw, part_b_raw = _split_part_a_part_b(raw)
    if not part_b_raw.strip():
        print("WARNING: UniProt Relation response had no parseable '### PART B' section -- "
              f"Functions descriptions will all fall back. First 300 chars: {raw[:300]!r}")

    # PART A: Item|Element A|Element B|Relation -- keyed by Item number, a
    # bare integer per the prompt's own instructions, so any row whose
    # first cell isn't purely numeric (the header row, or stray text) is
    # naturally skipped without relying on its line position.
    relations = {}
    for line in _extract_clean_table(part_a_raw, min_cols=4).splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        idx = int(cells[0])
        if 1 <= idx <= len(uniprot_edges) and cells[3].strip('"'):
            relations[idx] = cells[3].strip('"')

    # PART B: Item|Element A|Functions.
    functions = {}
    for line in _extract_clean_table(part_b_raw, min_cols=3).splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        idx = int(cells[0])
        if 1 <= idx <= len(uniprot_edges) and cells[2].strip('"'):
            functions[idx] = cells[2].strip('"')

    result = {}
    for i, e in enumerate(uniprot_edges, start=1):
        if i in relations or i in functions:
            result[id(e)] = {"relation": relations.get(i, ""), "functions": functions.get(i, "")}
    n_missing = len(uniprot_edges) - len(result)
    if n_missing:
        print(f"  UniProt Relation LLM call: {len(result)}/{len(uniprot_edges)} row(s) labeled; "
              f"{n_missing} falling back to deterministic verb cue / 'co-mentioned' and raw Function_Text.")
    else:
        print(f"  UniProt Relation LLM call: {len(result)}/{len(uniprot_edges)} row(s) labeled.")
    return result

# Not one of the 3 protected prompts (PROMPT_COSHIFT_COMBINED/
# PROMPT_PUBMED_EXTRACT_D_MULTI/PROMPT_TABLE_INTERPRETATION) -- another
# small, single-purpose prompt, same pattern as PROMPT_UNIPROT_RELATION
# just above: UniProt virulence-protein rows (find_uniprot_virulence_
# mentions) get an LLM-authored description grounded in that protein's own
# FUNCTION text and GO molecular-function terms, instead of a mechanically
# altered sentence. Free to edit without the same standing-rule
# protection as the other 3.
PROMPT_UNIPROT_VIRULENCE_DESCRIPTION = """AI Role
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Data
Below is the "study context." Study Context has the information that describes the dataset being analyzed. It is metadata, not scientific evidence. It specifies the conditions under which the data were collected, including, disease, disease stage, tissue site, host species, experimental modality, taxonomic resolution, and the dataset's Baseline Group and Target Group.

Study context:
{study_context}

Below is the "element list". For each element, the Observed Shift represents a comparison between the dataset's Baseline Group and Target Group, both defined in the Study Context above. An Observed Shift of 1 means the element's value is higher in the Target Group than in the Baseline Group (an increase); -1 means it is lower (a decrease). For this analysis, you will not use the observed shift values.

Element list:
{element_list}

Below is a set of UniProt virulence-protein items. Each numbered item gives a Protein name, its real UniProt Function text, and its real UniProt GO (molecular function) terms -- either or both of the latter two may be empty. Every protein listed was already independently tagged by UniProt's own curators with the official "Virulence" keyword (KW-0843) for the correct organism.

Items:
{items_block}

Analysis Instructions
For each item above, use ONLY the real text given for that item -- never invent a mechanism, target, activity, host tissue, or host species the given text does not itself state.

Name the specific host-directed virulence action or mechanism this protein carries out, grounded ONLY in that item's real Function text and/or GO terms. This must be a real, specific noun phrase naming the actual action described in the text -- not a generic label, not a truncated sentence fragment, and not a full sentence. If both the Function text and GO terms are empty for an item, its action is "Virulence" -- still an accurate, non-fabricated label, because every protein here was already independently tagged by UniProt's own curators with the official "Virulence" keyword (KW-0843), so "Virulence" alone remains a true, grounded label even with no further descriptive text to draw on.

Separately, judge whether this item belongs in this specific study, given the Study Context above. Default to "yes." Only judge "no" if the real Function text and/or GO terms EXPLICITLY state something that directly contradicts the Study Context (e.g. the text explicitly names a host species, tissue, or process that is clearly incompatible with the study context given). Never judge "no" just because the text doesn't explicitly confirm relevance, is silent on host tissue/species, or you are merely uncertain -- absence of stated relevance is NOT a conflict, and every protein reaching this prompt was already independently confirmed to be a real virulence factor of the correct organism via UniProt's own taxonomy + keyword annotation, so exclusion must be reserved for a genuine, textually-stated contradiction only.

Reporting Instructions
Output exactly two labeled sections. Each section should contain only the requested table.

### PART A
Summarize each item's Description in a table. Produce exactly one row per item, in the same order as the Items list above; never merge, skip, or reorder items. Columns should be arranged with this order: "Item," "Protein Name," and "Description."

- "Item": the item number exactly as given above.
- "Protein Name": copied EXACTLY as given for that item.
- "Description": a short, meaningful phrase (2-5 words) naming the specific host-directed virulence action or mechanism, grounded only in that item's Function text and/or GO terms. Write exactly "Virulence" when both are empty.

The table should be pipe-separated ("|") with header row without divider nor extra spaces.

### PART B
Summarize each item's study-context judgment in a table. Produce exactly one row per item, in the same order as the Items list above; never merge, skip, or reorder items. Columns should be arranged with this order: "Item," "Protein Name," "Include," and "Reason."

- "Item": the item number exactly as given above.
- "Protein Name": copied EXACTLY as given for that item.
- "Include": exactly "yes" or "no," per the Analysis Instructions above.
- "Reason": a brief (one sentence) reason quoting or closely paraphrasing the contradicting text when Include is "no"; leave blank when Include is "yes."

The table should be pipe-separated ("|") with header row without divider nor extra spaces.
"""

def _label_uniprot_virulence_descriptions_via_llm(virulence_edges: list, elements: list) -> dict:
    """Batches every UniProt virulence-protein edge's (protein name,
    Function_Text, GO_Terms) plus the dataset's Study Context and Element
    list into ONE LLM call (PROMPT_UNIPROT_VIRULENCE_DESCRIPTION,
    structured with the same AI Role / Input Data (study_context,
    element_list) / Analysis Instructions / Reporting Instructions shape
    as Prompt 1/2a, kept as its own separate prompt rather than merged
    into Prompt 2a or 2b) asking it, per item, for two independently
    labeled "### PART A" / "### PART B" pipe-separated tables (parsed the
    same way as Prompt 2a's own Part A/B via _split_part_a_part_b/
    _extract_clean_table): (a) PART A -- a short, grounded, meaningful
    phrase (2-5 words, e.g. "collagen degradation", "leukocyte
    disruption" -- not a sentence) naming the specific host-directed
    virulence action/mechanism that protein carries out -- kept short
    because it also doubles as the knowledge-graph edge label (see
    _virulence_graph_label) and Table 3's Supporting Evidence column,
    both of which read better as a short phrase than a truncated
    sentence; and (b) PART B -- whether that entry should be included
    given the study context -- default "yes"; "no" only on an explicit,
    textually-stated contradiction (never on mere silence/uncertainty --
    see the prompt's own Analysis Instructions). Same batching-into-one-
    call / {id(edge): ...}-by-object-identity / graceful-fallback-contract
    pattern as its sibling _label_uniprot_relations_via_llm above -- see
    that function's own docstring for the full reasoning on why id(edge)
    keying and a left-out-of-the-dict-on-failure contract are used instead
    of a positional index or raising.

    ONE call for every virulence edge in a run, same one-shot-labeling-task
    (not resampled per PUBMED_EXTRACTION_RUNS run) reasoning as
    _label_uniprot_relations_via_llm.

    Never fabricates: the prompt restricts the LLM to only the real
    Function_Text/GO_Terms/Study Context given per item, with an explicit
    "Virulence"-only description fallback for items where both Function
    text and GO terms are genuinely empty (still non-fabricated, since
    every edge reaching this function already matched UniProt's own
    KW-0843 keyword on that exact protein), and an explicit default-to-
    include instruction so the LLM cannot silently drop a real KW-0843
    match just because the study context isn't explicitly confirmed by the
    protein's own text. Any item either table doesn't include, or that
    fails to parse, is left OUT of the returned dict -- callers must
    default that item to include=True (never accidentally exclude real
    data on a parse failure) and fall back to that edge's own Function_Text
    / GO_Terms / a plain "Virulence" label for the description, never a
    blank description. call_openai itself never raises (retries 3x,
    returns "" on total failure), so an empty response here just means
    every item falls back to include=True, not an exception and not a
    silent drop.

    Returns {id(edge): {"include": bool, "description": str, "reason": str}}
    keyed by Python object identity of each dict in `virulence_edges` --
    same robustness-to-skipped-items reasoning as
    _label_uniprot_relations_via_llm's return value. An item covered only
    by PART A (no matching PART B row) still gets a dict entry with
    include defaulted to True -- the same "absence is never an exclusion"
    contract as a fully-missing item -- so a partial parse can never
    accidentally exclude real data.

    Returns {} immediately, no LLM call, if `virulence_edges` is empty --
    same guard as the sibling function."""
    if not virulence_edges:
        return {}
    items_block = "\n".join(
        f"{i}. Protein name: {e.get('Target', '')} | "
        f"Function text: {e.get('Function_Text', '') or '(none)'} | "
        f"GO terms: {e.get('GO_Terms', '') or '(none)'}"
        for i, e in enumerate(virulence_edges, start=1)
    )
    valid_elements = sorted({str(e).strip() for e in elements if str(e).strip()})
    element_list_str = "\n".join(f"- {e}" for e in valid_elements)
    study_context = _study_context_block_for_prompt()
    prompt = PROMPT_UNIPROT_VIRULENCE_DESCRIPTION.format(
        items_block=items_block, element_list=element_list_str, study_context=study_context)
    raw = call_openai(prompt, model=COSHIFT_MODEL, max_tokens=COSHIFT_MAX_TOKENS)
    part_a_raw, part_b_raw = _split_part_a_part_b(raw)
    if not part_b_raw.strip():
        print("WARNING: UniProt Virulence Description response had no parseable '### PART B' "
              f"section -- Include/Exclude will all default to True. First 300 chars: {raw[:300]!r}")

    # PART A: Item|Protein Name|Description.
    descriptions = {}
    for line in _extract_clean_table(part_a_raw, min_cols=3).splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        idx = int(cells[0])
        if 1 <= idx <= len(virulence_edges) and cells[2].strip('"'):
            descriptions[idx] = cells[2].strip('"')

    # PART B: Item|Protein Name|Include|Reason.
    includes = {}
    for line in _extract_clean_table(part_b_raw, min_cols=4).splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        idx = int(cells[0])
        include_raw = cells[2].strip().lower()
        if 1 <= idx <= len(virulence_edges) and include_raw in ("yes", "no"):
            includes[idx] = (include_raw == "yes", cells[3].strip('"'))

    parsed = {}
    for idx in set(descriptions) | set(includes):
        include, reason = includes.get(idx, (True, ""))
        parsed[idx] = {
            "include": include,
            "description": descriptions.get(idx, ""),
            "reason": reason,
        }
    result = {id(e): parsed[i] for i, e in enumerate(virulence_edges, start=1) if i in parsed}
    n_missing = len(virulence_edges) - len(result)
    n_excluded = sum(1 for v in result.values() if not v["include"])
    if n_excluded:
        reasons = "; ".join(
            f"{e.get('Target', '?')} ({v['reason'] or 'no reason given'})"
            for e, v in ((e, result.get(id(e))) for e in virulence_edges)
            if v and not v["include"]
        )
        print(f"  UniProt Virulence Description LLM call: {len(result)}/{len(virulence_edges)} row(s) "
              f"labeled; {n_missing} falling back to raw Function/GO text / 'Virulence' (defaulting to "
              f"include=True). {n_excluded} row(s) judged to conflict with the real study context and "
              f"excluded: {reasons}.")
    elif n_missing:
        print(f"  UniProt Virulence Description LLM call: {len(result)}/{len(virulence_edges)} row(s) "
              f"labeled; {n_missing} falling back to raw Function/GO text / 'Virulence' "
              f"(defaulting to include=True).")
    else:
        print(f"  UniProt Virulence Description LLM call: {len(result)}/{len(virulence_edges)} row(s) "
              f"labeled; 0 excluded.")
    return result

def build_kb_sourced_table2_rows(elements: list, gene_info: dict = None, sample_model: str = None) -> dict:
    """Mostly-Python counterpart to Prompt 2's PubMed-sourced rows -- built
    directly from real ImmuneXpresso/UniProt data, with NO LLM call for
    ImmuneXpresso rows and exactly ONE batched LLM call total for labeling
    UniProt rows' Relation text (see _label_uniprot_relations_via_llm).
    Prompt 2's own text shows the LLM no Knowledge Base content (see
    extract_and_group_coshift_from_batch's docstring); this function is
    what provides KB-sourced coverage instead, called once from
    build_table2_coshift rather than once per PUBMED_EXTRACTION_RUNS run,
    since the underlying KB edges are fully deterministic with no sampling
    variance to resample (the one UniProt-labeling LLM call is a single
    one-shot batch, also not repeated per run).

    Combines this pipeline's 2 KB sources (ImmuneXpresso and UniProt --
    MASI and MiMeDB are not used):
      - find_kb_edges_for_elements(elements): direct ImmuneXpresso edges
        where BOTH ends are already master-list elements.
      - find_kb_neighborhood_edges(elements): each element's real top-N
        ImmuneXpresso partners even when the partner isn't itself a
        master-list element (a real, named, external node -- not
        fabricated, just outside the input list).
      - find_uniprot_function_mentions(elements, sample_model): named
        mentions of other real genes/elements inside an element's own
        curated UniProt FUNCTION text (both Tier 1 -- other master-list
        elements -- and Tier 2 -- external ImmPort-registry gene symbols
        -- are included; both are real).
      - find_uniprot_virulence_mentions(elements): each real microbe
        element's own UniProt-curated virulence proteins (UniProt keyword
        KW-0843, matched by that organism's real NCBI Taxonomy ID -- see
        that function's own docstring). Still tagged "UniProt" (same
        Evidence Source / _KB_EDGE_ID_FIELD as the co-mention edges above),
        but structurally distinct and NOT run through
        _label_uniprot_relations_via_llm -- see below. Its Relationship
        label is fixed/deterministic (unchanged), but its description/
        evidence text is LLM-authored by a separate small batched call,
        _label_uniprot_virulence_descriptions_via_llm -- see below.

    Sources not used:
      - MiMeDB: every MiMeDB microbe-vs-disease edge had "Periodontitis"
        (the literal disease name) as its Target, never a genuine second
        element, so it could never support a 2-element Table 2 row.
      - MASI: its Microbe_Change field caused a separate problem in
        Table 1 (no per-element cap, producing 9,900+ character cells,
        plus a category mismatch between drug-exposure records and
        periodontitis-specific evidence).

    Since every KB edge already inherently connects exactly 2 real named
    entities (Source, Target), each edge directly yields BOTH one Part A
    row (a single relationship, exactly 2 elements) AND its own
    corresponding Part B row (Element A -> Element B) -- unlike a PubMed
    abstract, which can co-mention 3+ elements and so needs the separate
    combinations-based pairing step (see extract_and_group_coshift_from_batch's
    Part B, and the Table 3 pairwise-coverage gap check in
    extract_and_group_coshift_from_batch) -- no such step is needed here.

    Evidence Source is "ImmuneXpresso"/"UniProt" (never "PMID"), so these
    rows are visibly distinguishable from PubMed-sourced ones.
    The ID column holds that source's own real record ID (Cell Ontology
    ID / UniProt Accession -- see _KB_EDGE_ID_FIELD), never a fabricated
    one; a row is skipped (not fabricated with a blank ID) if that field
    is empty on the underlying edge. "Quoted Evidence" is the real
    'Evidence' text find_kb_edges_for_elements/find_kb_neighborhood_edges
    already build (paper counts/enrichment score, from real data) for
    ImmuneXpresso; for UniProt co-mention rows it's the real quoted UniProt
    sentence (already extracted by find_uniprot_function_mentions) PLUS an
    LLM-authored Functions description of the source protein (Element A),
    grounded in that protein's own real Function_Text/GO_Terms and produced
    by the SAME _label_uniprot_relations_via_llm call that labels Relation
    -- appended after the real sentence as a clearly labeled addition
    ("<sentence> | Functions: <description>"), never replacing or blending
    into the real sentence itself; for UniProt virulence-protein rows it's
    the LLM-authored description built from that protein's own real
    Function_Text/GO_Terms (see
    _label_uniprot_virulence_descriptions_via_llm), falling back to the raw
    Function_Text, then GO_Terms, then the literal string "Virulence" if
    the LLM call didn't cover that row -- never blank.

    Deduped by (evidence_source, id, sorted element-name pair), since the
    same real edge can otherwise appear from both
    find_kb_edges_for_elements and find_kb_neighborhood_edges (the latter
    is a superset per-element expansion of the former).

    Returns {"part_a": [...], "part_b": [...]}, same shape as
    extract_and_group_coshift_from_batch's return value -- part_a rows
    have keys relationship_name/id/evidence_source/elements/quote; part_b
    rows have keys id/evidence_source/element_a/relation/element_b/quote."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    if not elements or not KNOWLEDGE_BASE:
        return {"part_a": [], "part_b": []}

    # ImmuneXpresso and UniProt are this pipeline's only 2 KB sources, so
    # find_kb_edges_for_elements/find_kb_neighborhood_edges only ever
    # return ImmuneXpresso edges here.
    raw_edges = [("ImmuneXpresso", e)
                 for e in find_kb_edges_for_elements(elements) + find_kb_neighborhood_edges(elements)]
    uniprot_edges = find_uniprot_function_mentions(elements, sample_model)
    for e in uniprot_edges:
        raw_edges.append(("UniProt", e))

    # Virulence-protein edges (UniProt keyword KW-0843 x each microbe
    # element's real NCBI Taxonomy ID -- see find_uniprot_virulence_
    # mentions) are a second, structurally distinct UniProt-sourced layer.
    # Tagged with the same "UniProt" source_name as the co-mention edges
    # above -- so Evidence Source/_KB_EDGE_ID_FIELD/dedupe below all work
    # identically for both -- but deliberately kept OUT of `uniprot_edges`
    # (and so out of the Relation-labeling LLM call right below): see
    # find_uniprot_virulence_mentions' own docstring for why these get a
    # fixed deterministic Relation LABEL instead of an LLM-authored one.
    # Their description/evidence TEXT is still LLM-authored, just via a
    # separate call (_label_uniprot_virulence_descriptions_via_llm, right
    # below) grounded in Function_Text/GO_Terms rather than a shared
    # sentence.
    virulence_edges = find_uniprot_virulence_mentions(elements)
    for e in virulence_edges:
        raw_edges.append(("UniProt", e))

    # LLM-authored Relation text for UniProt CO-MENTION rows only
    # (`uniprot_edges`, never the virulence-protein rows above), grounded
    # in each edge's own Supporting Evidence sentence (see
    # _label_uniprot_relations_via_llm's docstring). ImmuneXpresso rows
    # are unaffected -- they already have a real,
    # structured Relationship field (e.g. "cell-cytokine interaction"),
    # no LLM needed there. Keyed by id(edge) so a later `continue` (e.g. a
    # blank record ID) can never desync a positional index.
    uniprot_relations = _label_uniprot_relations_via_llm(uniprot_edges, elements)

    # LLM-authored virulence-protein DESCRIPTION text (separate small
    # batched call from the Relation-labeling one above), grounded in each
    # virulence edge's own Function_Text/GO_Terms. Keyed by id(edge), same
    # reasoning as uniprot_relations above.
    virulence_descriptions = _label_uniprot_virulence_descriptions_via_llm(virulence_edges, elements)

    seen = set()
    part_a_rows, part_b_rows = [], []
    n_virulence_excluded = 0
    for source_name, edge in raw_edges:
        elem_a = str(edge.get("Source", "")).strip()
        elem_b = str(edge.get("Target", "")).strip()
        if not elem_a or not elem_b or elem_a.lower() == elem_b.lower():
            continue
        record_id = str(edge.get(_KB_EDGE_ID_FIELD[source_name], "")).strip()
        if not record_id:
            # No real, citable ID for this specific edge -- skip rather
            # than fabricate one (should be rare: every edge builder
            # above always sets its ID field for a well-formed record).
            continue
        dedupe_key = (source_name, record_id, tuple(sorted((elem_a.lower(), elem_b.lower()))))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        if source_name == "UniProt" and edge.get("_is_virulence_edge"):
            # A virulence edge the LLM explicitly judged (via
            # PROMPT_UNIPROT_VIRULENCE_DESCRIPTION's Include/Exclude
            # decision, grounded in the study context) to conflict with
            # this dataset's study context is dropped here entirely --
            # excluded from Table 2/3, not merely annotated. Safe by
            # construction: an item the LLM's response
            # didn't cover/parse is simply absent from virulence_
            # descriptions, so .get(...) returns None here and `include`
            # defaults to True -- a parse failure can never accidentally
            # exclude real data, only an explicit, parsed "no" can.
            vd = virulence_descriptions.get(id(edge))
            if vd is not None and vd.get("include") is False:
                n_virulence_excluded += 1
                continue
            # Virulence-protein edges (see find_uniprot_virulence_mentions)
            # skip the LLM/Verb_Cue fallback chain entirely -- there is no
            # shared sentence to label a relationship from here, only the
            # fixed, deterministic, fully-grounded Relationship text this
            # edge builder itself already set.
            relation = str(edge.get("Relationship", "")).strip() or VIRULENCE_RELATION_LABEL
        elif source_name == "UniProt":
            # uniprot_relations now maps id(edge) -> {"relation": ...,
            # "functions": ...} (see _label_uniprot_relations_via_llm's
            # updated return shape). Fallback chain, never a blank
            # Relation: LLM label (grounded in this row's own real
            # sentence) -> deterministic Verb_Cue (a real word found in
            # that same real sentence by _find_relationship_verb) -> plain
            # "co-mentioned".
            uniprot_llm = uniprot_relations.get(id(edge)) or {}
            relation = (str(uniprot_llm.get("relation", "")).strip()
                        or str(edge.get("Verb_Cue", "")).strip() or "co-mentioned")
        else:
            relation = str(edge.get("Relationship", "") or "KB-documented interaction").strip()
        direction = str(edge.get("Direction", "")).strip()
        if source_name == "UniProt" and edge.get("_is_virulence_edge"):
            # Description/evidence TEXT fallback chain, never blank:
            # LLM-authored description (grounded in this row's own real
            # Function_Text/GO_Terms) -> raw Function_Text -> raw GO_Terms
            # -> the literal string "Virulence" (still an accurate,
            # non-fabricated label -- see find_uniprot_virulence_mentions'
            # docstring -- since every row here already matched UniProt's
            # own KW-0843 keyword on that exact entry).
            quote = (str((vd or {}).get("description", "")).strip()
                     or str(edge.get("Function_Text", "")).strip()
                     or str(edge.get("GO_Terms", "")).strip()
                     or "Virulence")
        elif source_name == "UniProt":
            # Co-mention edges (find_uniprot_function_mentions): the real
            # verbatim quoted Sentence stays byte-for-byte recognizable as
            # UniProt's own words -- never replaced or blended -- with the
            # LLM-authored Functions description of Element A (the source
            # protein, grounded in its own real Function_Text/GO_Terms)
            # appended afterward as a clearly separate, labeled addition.
            # Same "Python only extracts, LLM decides" treatment already
            # applied to the virulence layer above, extended to this older
            # co-mention layer too -- same batched LLM call as Relation
            # (uniprot_llm above), no new LLM call. Functions fallback
            # chain, never blank: LLM-authored
            # description -> this edge's own raw Function_Text -> the
            # literal string "No described function available".
            uniprot_llm = uniprot_relations.get(id(edge)) or {}
            functions_desc = (str(uniprot_llm.get("functions", "")).strip()
                               or str(edge.get("Function_Text", "")).strip()
                               or "No described function available")
            sentence = str(edge.get("Sentence") or edge.get("Evidence") or "").strip()
            quote = f"{sentence} | Functions: {functions_desc}" if sentence else functions_desc
        else:
            quote = edge.get("Sentence") or edge.get("Evidence") or ""
        quote = str(quote).strip()

        part_a_rows.append({
            "relationship_name": relation, "id": record_id,
            "evidence_source": source_name, "elements": [elem_a, elem_b], "quote": quote,
        })
        part_b_rows.append({
            "id": record_id, "evidence_source": source_name,
            "element_a": elem_a, "relation": f"{relation} ({direction})" if direction else relation,
            "element_b": elem_b, "quote": quote,
        })

    if part_a_rows:
        src_counts = Counter(r["evidence_source"] for r in part_a_rows)
        # Precise about which UniProt rows went through which LLM call:
        # co-mention rows (uniprot_edges) get an LLM-authored Relation
        # LABEL via _label_uniprot_relations_via_llm; virulence-protein
        # rows (find_uniprot_virulence_mentions) keep a fixed deterministic
        # Relation label but get an LLM-authored description/evidence TEXT
        # via _label_uniprot_virulence_descriptions_via_llm instead -- see
        # this function's own docstring for why. Both still print under the
        # single "UniProt" source count above; this second clause just
        # clarifies which call covered which subset this run.
        llm_note = (f"one batched LLM call for {len(uniprot_edges)} UniProt co-mention row(s)' "
                    f"Relation labeling" if uniprot_edges else
                    "no UniProt co-mention rows this run, so no Relation-labeling LLM call")
        n_virulence = sum(1 for e in virulence_edges if e.get("Accession"))
        virulence_llm_note = (f"one batched LLM call for {len(virulence_edges)} UniProt virulence-protein "
                               f"row(s)' description labeling" if virulence_edges else
                               "no UniProt virulence-protein rows this run, so no description-labeling LLM call")
        print(f"KB-sourced Table 2/3: {len(part_a_rows)} row(s) built directly from real KB data "
              f"(by source: {dict(src_counts)}) -- no LLM call for ImmuneXpresso, {llm_note}; "
              f"{n_virulence} UniProt virulence-protein row(s) (KW-0843) use a fixed deterministic "
              f"Relation label, {virulence_llm_note}. "
              f"{n_virulence_excluded} real KW-0843 match(es) excluded as conflicting with the study "
              f"context per the LLM's Include/Exclude judgment (see log above for reasons).")
    elif n_virulence_excluded:
        # part_a_rows can legitimately be empty (e.g. every real KB edge
        # got excluded/deduped away) while exclusions still happened --
        # print this count either way so it's never silently invisible.
        print(f"KB-sourced Table 2/3: 0 row(s) built. "
              f"{n_virulence_excluded} real KW-0843 match(es) excluded as conflicting with the study "
              f"context per the LLM's Include/Exclude judgment (see log above for reasons).")
    return {"part_a": part_a_rows, "part_b": part_b_rows}

def build_table2_coshift(sample: str, elements: list, out_dir: Path, context: str = "disease"):
    """Prompt 2's output: Table 2 (Part A -- one row per real biological
    relationship, 2+ elements, supported by a single PMID) and the new
    Table 3 (Part B -- that same evidence broken down into pairwise
    Element A/Relation/Element B statements), PLUS KB-sourced rows
    (ImmuneXpresso/UniProt -- this pipeline's only 2 KB sources, see
    build_kb_sourced_table2_rows) built separately by
    build_kb_sourced_table2_rows -- no LLM call for ImmuneXpresso rows,
    one batched LLM call total for UniProt rows' Relation text (see that
    function's docstring). Per Prompt
    3's own Table 2/Table 3 description, evidence from different
    sources/abstracts/KB entries is NEVER merged into one row -- these are
    a straight concatenation of every batch's validated PubMed rows plus
    the KB-sourced rows (each de-duplicated on its own terms -- see
    _dedupe_evidence_rows and build_kb_sourced_table2_rows), not a
    group-by-name merge like the old Table 2 design.

    Saves {sample}_table2.csv and {sample}_table3.csv (both CSV-only --
    no .txt rendering, no appended study-context note, for either table).
    Returns (table2_df, table3_df);
    table2_df.attrs["edges_df"]
    carries a
    Source/Target/Basis/Evidence Source breakdown built from table3_df
    (Element A -> Element B per row) for build_combined_network, which
    expects that shape from the old design."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    gene_info = find_gene_identity_info(elements, SAMPLE_MODEL)

    # PUBMED_EXTRACTION_RUNS (config.txt) independent passes over the same
    # shared abstract pool -- reusing the same config knob Table 1's own
    # extraction ensemble already uses. The model can cite evidence in one
    # batch but skip the exact same available evidence in another, purely
    # by chance; more independent passes give it more chances to notice/
    # cite evidence it happens to skip on any single pass. Every run
    # reuses the identical abstract pool (PubMed is only ever searched
    # once regardless -- see fetch_ranked_combined_pool), so this is pure
    # resampling of the LLM's own extraction, not additional retrieval.
    all_runs = run_coshift_batches(elements, gene_info, out_dir, context, n_runs=PUBMED_EXTRACTION_RUNS)
    all_part_a = [row for run in all_runs for row in run["part_a"]]
    all_part_b = [row for run in all_runs for row in run["part_b"]]

    # KB-sourced rows (ImmuneXpresso/UniProt), built directly in
    # Python with no LLM call at all -- see build_kb_sourced_table2_rows'
    # own docstring. Called once here (not once per PUBMED_EXTRACTION_RUNS
    # run like the LLM batches above), since it's fully deterministic and
    # has no sampling variance to resample.
    kb_rows = build_kb_sourced_table2_rows(elements, gene_info, SAMPLE_MODEL)
    all_part_a.extend(kb_rows["part_a"])
    all_part_b.extend(kb_rows["part_b"])

    # Same real relationship (same Evidence Source + same real PMID/KB ID
    # + same element set) can now legitimately come back from more than
    # one pass -- collapsed to one row here; see _dedupe_evidence_rows
    # for why this now also has to cover PMID rows, not just KB ones.
    part_a_rows = _dedupe_evidence_rows(all_part_a, id_key="id", source_key="evidence_source",
                                         extra_keys=("elements",))
    part_b_rows = _dedupe_evidence_rows(all_part_b, id_key="id", source_key="evidence_source",
                                         extra_keys=("element_a", "element_b"))
    print(f"Table 2: {len(part_a_rows)} validated relationship(s) after de-duplication across "
          f"{PUBMED_EXTRACTION_RUNS} PubMed run(s) + KB-sourced rows ({len(all_part_a)} before). Table 3: "
          f"{len(part_b_rows)} validated pairwise row(s) after de-duplication "
          f"({len(all_part_b)} before).")

    table2 = pd.DataFrame([{
        "Biological Relationship Name": r["relationship_name"],
        "PMID or Knowledge Base ID": r["id"],
        "Evidence Source": r["evidence_source"],
        "List of Elements": "; ".join(str(e) for e in r["elements"]),
        "Quoted Evidence": r["quote"],
    } for r in part_a_rows])

    # Table 2 is CSV-only, same as Table 1 above -- no .txt rendering, no
    # appended "Study Context" note. Table 3 (right below) is also
    # CSV-only.
    csv_file2 = Path(out_dir) / f"{sample}_table2.csv"
    table2.to_csv(csv_file2, index=False, encoding="utf-8")
    print(f"Table2 CSV saved: {csv_file2}")

    # Table 3's own column name is "Supporting Evidence" per Prompt 3's
    # Table 3 description in BioShift_Prompts_0729_PD -- even though
    # Prompt 2's Part B itself calls the same value "Quoted Evidence".
    # This is a real naming inconsistency IN THE SOURCE PROMPT DOCUMENT
    # (flagged to the user separately); "Supporting Evidence" is used here
    # since Table 3 is what gets shown back to Prompt 3 under that name.
    table3 = pd.DataFrame([{
        "PMID or Knowledge Base ID": r["id"],
        "Evidence Source": r["evidence_source"],
        "Element A": r["element_a"],
        "Relation": r["relation"],
        "Element B": r["element_b"],
        "Supporting Evidence": r["quote"],
    } for r in part_b_rows])
    # Table 3 is CSV-only too, same as Table 1/2 above -- no .txt
    # rendering, no appended "Study Context" note. Prompt 3 itself never
    # reads this file -- build_table3_interpretation passes
    # table3_df.to_csv() directly from the in-memory DataFrame.
    csv_file3 = Path(out_dir) / f"{sample}_table3.csv"
    table3.to_csv(csv_file3, index=False, encoding="utf-8")
    print(f"Table3 CSV saved: {csv_file3}")

    # Source/Target/Basis breakdown for build_combined_network, built
    # straight from Table 3's own pairwise rows (Element A -> Element B),
    # kept in-memory only (table2.attrs), never written to disk.
    # Basis is set to the constant "Table-documented" (not the raw source
    # name) so build_table3_interpretation/build_combined_network's
    # existing solid-vs-dashed edge styling (which branches on that exact
    # string) keeps working unchanged -- every row here is already a real,
    # validated relationship (PMID-grounded or KB-ID-verified), never
    # speculative, so uniform "Table-documented" styling is correct. The
    # actual source name/ID is still visible via Citation.
    edge_rows_out = [{
        "Source": r["element_a"], "Target": r["element_b"], "Basis": "Table-documented",
        "Relationship": r["relation"], "Citation": f"{r['evidence_source']}:{r['id']}",
    } for r in part_b_rows]
    table2.attrs["edges_df"] = pd.DataFrame(
        edge_rows_out, columns=["Source", "Target", "Basis", "Relationship", "Citation"])

    return table2, table3


# ─────────────────── Table 3 knowledge-evidence graph (Graphviz) ───────────
# Strategy: this figure is built DIRECTLY from Table 3's own three core
# columns -- Element A, Relation, Element B. No neighborhood expansion (no
# pulling in an element's other real KB partners the way BioShift.py's full
# build_combined_network does), no Prompt 3 general-knowledge layer -- every
# edge drawn here already passed Table 3's own grounding checks (PMID
# quote-match or KB-ID verification in extract_and_group_coshift_from_
# batch), so this is a strict, real subset of a real, already-validated
# table, never a new inference layer. The figure does not repeat Table
# 3's own PMID/Knowledge Base ID or Evidence Source columns -- that
# citation trail already lives in Table 3 itself; the figure's only job
# is to show the real relationship shape (which element relates to
# which, and how), uniformly styled.

# Generic ImmuneXpresso relationship name (see build_kb_sourced_table2_rows)
# -- real and correct in Table 3 itself, but on the figure it's repeated,
# near-verbatim, on almost every ImmuneXpresso edge, so it reads as visual
# clutter rather than a distinguishing label. Stripped from the graph's
# edge labels only -- Table 3's own "Relation" column keeps the full,
# real text unchanged.
_GRAPH_LABEL_STRIP_PHRASES = ["cell-cytokine interaction"]

def _graph_edge_label(relation: str, evidence_source: str = "") -> str:
    """Strip any generic, non-distinguishing relationship phrase
    (_GRAPH_LABEL_STRIP_PHRASES) from a Table 3 row's real Relation text,
    for use as this edge's label on the knowledge graph only. Gated on
    evidence_source == "ImmuneXpresso" (case-insensitive) -- that's the
    ONLY source this fixed boilerplate phrase can ever legitimately come
    from (see build_kb_sourced_table2_rows, where it's a hardcoded
    Relationship value for every ImmuneXpresso edge); a PubMed- or
    UniProt-sourced Relation is passed through completely untouched even
    if it happens to contain the same words, so this can never silently
    mangle a real, LLM-authored relationship phrase. A real
    sentiment/direction appended in parentheses (e.g. 'cell-cytokine
    interaction (Positive)' -> '(Positive)') is kept, since that part IS
    real, edge-specific signal, not boilerplate. Returns '' (an unlabeled
    edge) if nothing is left after stripping -- the connection itself,
    drawn between two already-typed real nodes, still shows the real
    relationship; an empty label is never a fabricated one."""
    text = (relation or "").strip()
    if str(evidence_source or "").strip().lower() != "immunexpresso":
        return text
    for phrase in _GRAPH_LABEL_STRIP_PHRASES:
        text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE).strip()
    return text

_GRAPH_LABEL_MAX_WORDS = 6

def _truncate_for_graph_label(text: str, max_words: int = _GRAPH_LABEL_MAX_WORDS) -> str:
    """Defensive safety net only, not the primary shortening mechanism.
    PROMPT_UNIPROT_VIRULENCE_DESCRIPTION already instructs the LLM to
    return a short (2-5 word) meaningful phrase (e.g. "collagen
    degradation") rather than a full sentence -- mechanically chopping a
    full sentence down to N words produces awkward, meaningless fragments,
    not a real distinguishing label. This function only guards against an
    LLM response that doesn't follow the phrase-length instruction (real
    models occasionally do this); it should rarely if ever actually cut
    real text in normal operation. Table 3's own CSV always keeps
    whatever the LLM actually returned, untruncated -- this is a
    display-only trim for the graph specifically. Appends an ellipsis
    only when something real was actually cut off, never when the text
    already fit. Returns '' unchanged if given empty/whitespace-only
    text -- callers must not treat that as a fabricated label."""
    text = (text or "").strip()
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "..."

def _virulence_graph_label(relation: str, supporting_evidence: str) -> str:
    """A Table 3 virulence-protein row (see find_uniprot_virulence_mentions/
    VIRULENCE_RELATION_LABEL) shows its own per-edge Functions phrase
    (Table 3's 'Supporting Evidence' column, LLM-authored -- see
    _label_uniprot_virulence_descriptions_via_llm -- a short 2-5 word
    phrase like "collagen degradation", grounded in that protein's
    UniProt Function/GO text) as its knowledge-graph edge label, instead
    of the fixed VIRULENCE_RELATION_LABEL text: that label is identical
    on every virulence edge in the figure ('UniProt-annotated virulence
    factor (KW-0843)', repeated), which is the same label-repetition-as-
    clutter problem _graph_edge_label's ImmuneXpresso-boilerplate-
    stripping addresses for that source -- except here the fix is to show
    the real, edge-specific text that already exists instead of stripping
    down to nothing, since a virulence edge's distinguishing information
    lives in Supporting Evidence, not in its (deliberately fixed,
    non-distinguishing) Relation. Since the LLM is instructed to already
    return a short phrase (not a sentence), _truncate_for_graph_label
    below is a defensive safety net, not the primary shortening step --
    see that function's own docstring.

    Falls back to the untruncated VIRULENCE_RELATION_LABEL only if
    Supporting Evidence is somehow empty for this row -- should not
    happen in practice (build_kb_sourced_table2_rows' own fallback chain
    already guarantees a non-blank quote/description for every real
    virulence row), but the graph must never show a blank edge label
    just because this one display step found nothing to truncate."""
    truncated = _truncate_for_graph_label(supporting_evidence)
    return truncated or (relation or "").strip() or VIRULENCE_RELATION_LABEL

def build_table3_knowledge_graph(sample: str, table3_df: pd.DataFrame, obs_df: pd.DataFrame, out_dir: Path):
    """Pairwise-relationship figure built directly from Table 3's own
    validated rows: one edge per row, Element A -> Element B, labeled with
    that row's real Relation text (no citation ID, no Evidence Source --
    both already shown in Table 3 itself, not repeated here), minus any
    generic non-distinguishing phrase stripped by _graph_edge_label (e.g.
    the repeated "cell-cytokine interaction" boilerplate on ImmuneXpresso
    edges -- unnecessary clutter on the figure; Table 3's own Relation
    column is untouched). Multiple
    real rows for the same (Element A, Element B) pair from different
    sources/abstracts are still drawn as separate parallel edges, never
    collapsed into one -- that mirrors Table 3's own "each source gets its
    own row" convention, and collapsing them would hide that more than one
    independent piece of evidence exists for that pair.

    Node shape (real-data typed, never guessed): hexagon = ImmPort-
    registry-matched cytokine/protein, box = microbe (matched against
    organism_taxonomy_ids.csv's user-verified NCBI Taxonomy ID map),
    ellipse (default) = anything else (immune cell, etc.), note (light
    yellow) = a virulence protein's real Function phrase, drawn as its
    own downstream node -- see below. Node fill color: green = Observed
    Shift 1 (increase), skyblue = Observed Shift -1 (decrease), white =
    not found in this sample's observed-shift CSV (also the default for
    Function nodes, which have no Observed Shift of their own).

    Microbe/UniProt-virulence rows (Evidence Source "uniprot", Relation ==
    VIRULENCE_RELATION_LABEL) draw as a three-node chain -- Microbe ->
    Protein -> Function -- instead of a single Microbe -> Protein edge:
    the Microbe -> Protein edge is labeled
    "virulence factor" (fixed, since the real distinguishing content now
    lives downstream), and a second Protein -> Function edge points to a
    new note-shaped node holding that row's real, per-edge Functions
    phrase (same text as Table 3's own Supporting Evidence column). This
    chain is scoped to virulence rows only -- the separate protein/
    cytokine co-mention UniProt layer never uses VIRULENCE_RELATION_LABEL,
    so it keeps the single-edge form below unchanged.

    All edges are drawn uniformly (solid black) -- since every edge is
    already a real, Table-3-validated relationship regardless of which
    source produced it, there's no meaningful distinction left to encode
    in edge color/style once the citation itself is left off the figure.

    Saved as {sample}_Table3_KnowledgeGraph.jpg. Requires the 'dot'
    executable (Graphviz) on PATH. Returns None (with a printed message,
    never a fabricated figure) if Table 3 has no usable rows or 'dot'
    isn't available."""
    if table3_df is None or table3_df.empty:
        print(f"Table 3 has no rows for {sample}; skipping Table 3 knowledge graph.")
        return None
    required_cols = {"Element A", "Relation", "Element B"}
    # "Evidence Source" is checked below (not required here) -- gates
    # _graph_edge_label's stripping to ImmuneXpresso rows only; its
    # absence just means no stripping happens (row.get default), never a
    # crash.
    missing = required_cols - set(table3_df.columns)
    if missing:
        print(f"Table 3 is missing expected column(s) {missing}; skipping Table 3 knowledge graph.")
        return None

    shift_map = {}
    if obs_df is not None and "Element" in obs_df.columns and "Observed Shift" in obs_df.columns:
        for _, r in obs_df.iterrows():
            shift_map[str(r["Element"]).strip()] = re.sub(r"\.0+$", "", str(r["Observed Shift"]).strip())

    # Table 3 can legitimately carry different real spellings of the same
    # gene across rows (e.g. a PubMed abstract quoting "IL-6" verbatim vs.
    # a UniProt Function text quoting "IL6" verbatim) -- both are genuine
    # source text, so Table 3's own CSV correctly keeps both as-is. Using
    # each row's literal Element A/B string as node identity would draw
    # two different nodes for what is actually one gene. Fix: any node
    # name that is not itself one of this sample's own master-list
    # elements (an "external" node named only via a KB/UniProt/PubMed
    # partner mention; a real master-list element is never renamed) gets
    # canonicalized to its ImmPort Cytokine Registry gene symbol
    # (find_gene_identity_info -- matched by exact-normalized symbol/
    # alias, so 'IL-6' and 'IL6' both resolve to the same registry row)
    # before it becomes a graph node -- so both spellings collapse into
    # one node, while Table 3's own CSV rows keep their original quoted
    # text untouched.
    master_elements = set(shift_map.keys())
    _raw_node_names = set()
    for _, _r in table3_df.iterrows():
        _raw_node_names.add(str(_r.get("Element A", "")).strip())
        _raw_node_names.add(str(_r.get("Element B", "")).strip())
    _raw_node_names.discard("")
    _external_gene_info = find_gene_identity_info(
        list(_raw_node_names - master_elements), SAMPLE_MODEL)

    def _canon_node_name(name: str) -> str:
        if not name or name in master_elements:
            return name  # never rename a real master-list element
        symbol = (_external_gene_info.get(name) or {}).get("gene_symbol", "").strip()
        return symbol if symbol else name

    nodes = set()
    # Table 3 legitimately has multiple DIFFERENT real citations for the
    # same (Element A, Relation, Element B) triple (e.g. 4 separate real
    # PMIDs all reporting "Porphyromonas gingivalis co-exists with
    # Treponema denticola") -- each row is a genuinely distinct piece of
    # evidence, never an accidental duplicate row (verified: Table 3 has
    # zero exact full-row duplicates, since same-ID+same-elements repeats
    # -- whether PMID or KB -- are collapsed by _dedupe_evidence_rows,
    # while two DIFFERENT real PMIDs/KB records for the same triple stay
    # distinct rows on purpose). But once the citation ID is
    # left off the figure, drawing one parallel edge per citation just
    # produces several visually identical lines with the same label,
    # reading as clutter rather than signal -- so here, edges are
    # collapsed to one per unique (Element A, Relation, Element B) triple
    # for the figure only (Table 3 itself keeps every real row).
    seen_edges = set()
    edge_specs = []  # (src, tgt, label)
    # A virulence-protein row draws as a three-node chain -- Microbe ->
    # Protein -> Function -- instead of a single Microbe -> Protein edge
    # carrying the function text only as an edge label. The Function
    # node's text is the same per-edge short phrase already shown in
    # Table 3's Supporting Evidence column (see _virulence_graph_label/
    # PROMPT_UNIPROT_VIRULENCE_DESCRIPTION), also surfaced as its own
    # node so the mechanism reads as a chain of evidence rather than
    # clutter on a single edge label. This is scoped to virulence rows
    # only (evidence_source == "uniprot" and relation ==
    # VIRULENCE_RELATION_LABEL) -- that relation string is unique to the
    # microbe/UniProt-virulence layer (find_uniprot_virulence_mentions),
    # never used by the separate protein/cytokine co-mention UniProt
    # layer, so this never fires for non-microbe rows.
    function_nodes = set()  # real per-edge Function phrase nodes, virulence-only
    for _, row in table3_df.iterrows():
        src = str(row.get("Element A", "")).strip()
        tgt = str(row.get("Element B", "")).strip()
        if not src or not tgt or src.lower() == "nan" or tgt.lower() == "nan":
            continue
        src, tgt = _canon_node_name(src), _canon_node_name(tgt)
        relation = str(row.get("Relation", "")).strip()
        nodes.add(src)
        nodes.add(tgt)
        # Dedup key uses the real, full Relation text (not the stripped
        # display label) -- two rows that differ only in a part
        # _graph_edge_label strips should still count as the same real
        # relationship for this figure-only collapsing step.
        key = (src, relation, tgt)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        evidence_source = str(row.get("Evidence Source", "")).strip()
        if evidence_source.lower() == "uniprot" and relation == VIRULENCE_RELATION_LABEL:
            # Microbe -> Protein edge: the fixed, real UniProt KW-0843
            # relation is now a short, consistent label -- the edge no
            # longer needs to carry the distinguishing Function text,
            # since that now lives on its own downstream node.
            edge_specs.append((src, tgt, "virulence factor"))
            # Protein -> Function edge: the real, per-edge Functions
            # phrase (Table 3's own Supporting Evidence column,
            # LLM-authored -- see _virulence_graph_label's docstring)
            # becomes its own node instead of just an edge label.
            supporting_evidence = str(row.get("Supporting Evidence", "")).strip()
            func_label = _virulence_graph_label(relation, supporting_evidence)
            if func_label:
                function_nodes.add(func_label)
                edge_specs.append((tgt, func_label, ""))
            continue
        label = _graph_edge_label(relation, evidence_source)
        edge_specs.append((src, tgt, label))

    if not edge_specs:
        print(f"No renderable Table 3 rows for {sample}; skipping Table 3 knowledge graph.")
        return None

    # Real-data node typing, same lookups build_table2_coshift already uses.
    gene_info = find_gene_identity_info(list(nodes), SAMPLE_MODEL)
    # Microbe detection for node shape: organism_taxonomy_ids.csv (the
    # same user-verified NCBI Taxonomy ID map find_uniprot_virulence_
    # mentions uses).
    taxonomy_ids = _load_organism_taxonomy_ids()

    def node_shape(name: str) -> str:
        if name in taxonomy_ids:
            return "box"        # microbe
        if name in gene_info:
            return "hexagon"    # cytokine/protein
        return "ellipse"        # immune cell / anything else (default)

    def node_color(name: str) -> str:
        s = shift_map.get(name, "")
        return "green" if s == "1" else ("skyblue" if s == "-1" else "white")

    dot_lines = ["digraph Table3KnowledgeGraph {", "  rankdir=LR;",
                 '  node [style=filled, fontname=Helvetica];',
                 '  edge [fontname=Helvetica, fontsize=9];']
    for n in sorted(nodes):
        safe_n = n.replace('"', "'")
        dot_lines.append(f'  "{safe_n}" [shape={node_shape(n)}, fillcolor={node_color(n)}];')
    for n in sorted(function_nodes):
        # Distinct shape/fill (note = dog-eared page, common "mechanism/
        # note" convention) so a Function node is visually unmistakable
        # from a real biological element (box/hexagon/ellipse above) --
        # it represents a protein's real, quoted mechanism text, not a
        # tracked element with its own Observed Shift.
        safe_n = n.replace('"', "'")
        dot_lines.append(f'  "{safe_n}" [shape=note, fillcolor=lightyellow];')
    for src, tgt, label in edge_specs:
        safe_src, safe_tgt = src.replace('"', "'"), tgt.replace('"', "'")
        safe_label = label.replace('"', "'")
        dot_lines.append(f'  "{safe_src}" -> "{safe_tgt}" [style=solid, color=black, label="{safe_label}"];')
    dot_lines.append("}")
    dot_text = "\n".join(dot_lines)

    ensure_dir(out_dir)
    jpg_out = Path(out_dir) / f"{sample}_Table3_KnowledgeGraph.jpg"
    with NamedTemporaryFile("w", delete=False, suffix=".dot", encoding="utf-8") as tmp:
        tmp.write(dot_text)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(["dot", "-Tjpg", str(tmp_path), "-o", str(jpg_out)], check=True, capture_output=True)
        print(f"Table 3 knowledge graph saved: {jpg_out}")
    except FileNotFoundError:
        print("Graphviz 'dot' not found. Install Graphviz and ensure 'dot' is on PATH.")
        return None
    except subprocess.CalledProcessError as e:
        print(f"Graphviz 'dot' error building Table 3 knowledge graph for {sample}:\n{e.stderr.decode(errors='ignore')}")
        return None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
    return jpg_out


# ─────────────────── Prompt 3: Biological Interpretation ───────────────────
PROMPT_TABLE_INTERPRETATION = """AI Role
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Data
Below is the "study context." Study Context has the information that describes the dataset being analyzed. It is metadata, not scientific evidence. It specifies the conditions under which the data were collected, including, disease, disease stage, tissue site, host species, experimental modality, taxonomic resolution, and the dataset's Baseline Group and Target Group.

Study context:
{study_context}

Below is the "element list". For each element, the Observed Shift represents a comparison between the dataset's Baseline Group and Target Group, both defined in the Study Context above. An Observed Shift of 1 means the element's value is higher in the Target Group than in the Baseline Group (an increase); -1 means it is lower (a decrease).

Element list:
{element_list}

Below is Table 1. For each element, the Observed Shift represents a comparison between the dataset's Baseline Group and Target Group, both defined in the Study Context above. An Observed Shift of 1 means the element's value is higher in the Target Group than in the Baseline Group (an increase); -1 means it is lower (a decrease). Under the column of Evidence for Up, Evidence for Down, and Evidence for Mixed, each listing the PMIDs whose real abstract text reported that direction. % Support with Observed Shift is the percentage of that element's total literature evidence whose direction agrees with its Observed Shift.

Table 1:
{table1}

Below is Table 2. Table 2 summarizes biological relationships identified from either a single PubMed abstract or a single Knowledge Base entry. Each row represents one biological relationship supported by one evidence source; evidence from different abstracts or Knowledge Base entries is never merged into the same row. Biological relationships without supporting evidence are omitted. The table contains the following columns:
Biological Relationship Name: specific name describing the biological relationship.
PMID or Knowledge Base ID: The identifier of the evidence source.
Evidence Source: The source of the evidence.
List of Elements: All biological elements participating in the reported relationship.
Quoted Evidence: The evidence supporting the reported biological relationship.

Table 2:
{table2}

Below is Table 3. Table 3 summarizes pairwise biological relationships. Each row represents one directed or symmetric relationship between two biological elements supported by a single PubMed abstract or a single Knowledge Base entry. If multiple evidence sources support the same pairwise relationship, each source is reported in a separate row. Pairs without compatible supporting evidence are omitted. The table contains the following columns:
PMID or Knowledge Base ID: The identifier of the evidence source.
Evidence Source: The source of the evidence.
Element A: The first biological element in the reported relationship.
Relation: The biological relationship between Element A and Element B. The expression forms a complete statement: Element A Relation Element B.
Element B: The second biological element in the reported relationship.
Supporting Evidence: The evidence supporting the reported pairwise relationship.

Table 3:
{table3}

Analysis Instructions
Use Tables 2 and 3 as the primary source for identifying biological relationships and organizing them into biologically coherent functional mechanisms. Derive candidate biological mechanisms only from the reported biological relationships and supporting evidence; do not use the observed shifts in the Element List or Table 1 to construct the mechanisms.

After constructing each candidate mechanism, use Table 1 and the Element List to evaluate whether the observed and literature-supported changes are consistent, partially consistent, mixed, or inconsistent with the proposed mechanism. Do not modify or discard a mechanism solely because the observed and literature-supported changes disagree with it. Instead, explicitly report any inconsistencies and, when appropriate, note that they may reflect context-dependent biology, conflicting literature, or incomplete mechanistic knowledge.

When a candidate mechanism includes a microbial virulence factor reported in Table 3 (rows with Evidence Source UniProt and a Relation naming a virulence factor), explicitly explain how that virulence factor affects host cells, grounded only in that row's real Supporting Evidence text -- never invent a host-cell effect the Supporting Evidence does not itself state.

Every row in Table 3 must be represented inside a group's "List of elements" -- never silently dropped. A row that reports only a single relationship and does not connect to a larger cluster (for example, one microbe and its one reported virulence factor) is still a valid, complete mechanism on its own -- form a minimal group from it rather than omitting it for being sparse or isolated from the rest of the network. Only an element Table 3 never mentions at all belongs in the separate Unsupported Elements section below, not an element you simply chose not to group.

Reporting Instructions
1. Table:
Summarize the results in a table. Columns should be arranged with this order: "Group Name," "List of PMID and Knowledge Base IDs (Evidence Source)," "List of elements," " Evidence Summary," and "Observation Summary."

- "Group Name": A short (3-8 words), specific biological name (never generic labels such as "Group 1").
- "PMID or Knowledge Base ID (Evidence Source)": For ID, bare numeric ID, no prefix. For each ID, write the name of database within a parenthesis. When the database is Abstract, write "PMID."
- "List of elements": All in the biological mechanism. When it is found in the "element list," copy EXACTLY as in the "element list."
- " Evidence Summary": Summarize the information so that it explicitly supports the reported group. If the group includes a microbial virulence factor from Table 3, state how it affects host cells, grounded in that row's real Supporting Evidence.
- "Observation Summary": Evaluate whether the observed shifts in the Element List are consistent, partially consistent, mixed, or inconsistent with the proposed biological mechanism. Also, briefly explain the comparison using the literature evidence summarized in Table 1. If the literature itself is conflicting, state this explicitly.

The table should be pipe-separated ("|") with header row without divider nor extra spaces.

2. Evidence-based narrative:
Develop one or more literature-supported mechanistic hypotheses from the biological relationships summarized in Tables 2 and 3. Then evaluate how well each hypothesis explains the observed and literature-supported changes in Table 1. When inconsistencies exist, report them rather than resolving them by inference. For the citation format, use this format, (PMID: 12345, UniProt:P12345). Clearly indicate when supporting evidence comes from a different Study Context. When evidence conflicts, say "Literature is inconsistent." When a hypothesis includes a microbial virulence factor from Table 3, explicitly explain how that virulence factor affects host cells, grounded only in that row's real Supporting Evidence.


3. Unsupported Elements in grouping:
Write the heading "# Unsupported Elements in grouping" (a single "#", not "###") EXACTLY ONCE, as the very last line of your entire response -- never repeat this heading anywhere else in the response. On the line(s) below that one heading, list every element from the Element List that is NOT mentioned as Element A or Element B in any row of Table 3 -- check this directly against Table 3's own Element A/Element B columns, not against the groups you built above. These are elements with no pairwise relationship evidence available at all. (An element that IS mentioned in Table 3 belongs in a group above, per the completeness rule stated earlier -- even a single, isolated relationship still forms its own minimal group -- so it should not also appear here.) If every Element List element is mentioned somewhere in Table 3, write "None" under that same single heading instead of a list.

"""

def get_table_interpretation_prompt(context: str) -> str:
    """context is accepted for call-site compatibility (build_table3_
    interpretation still threads a disease/healthy context through the
    rest of the pipeline), but the interpretation prompt itself is
    context-neutral."""
    return PROMPT_TABLE_INTERPRETATION


def _build_observed_shift_df(elements: list, obs_df: pd.DataFrame) -> pd.DataFrame:
    """Real Element/Observed-Shift DataFrame (exact column names "Element",
    "Observed Shift" -- what build_combined_network expects for its
    shift-based node coloring), same column-detection convention used in
    build_table1_evidence, so this reflects the exact same real data
    Table 1's own '% Support' was computed against."""
    obs_df = obs_df.copy()
    obs_df.columns = [str(c).strip() for c in obs_df.columns]
    obs_cols = [c for c in obs_df.columns if c.lower().startswith("element")]
    if obs_cols:
        obs_df.rename(columns={obs_cols[0]: "Element"}, inplace=True)
        obs_df["Element"] = obs_df["Element"].astype(str).map(lambda x: x.strip())
    obs_shift_col = next((c for c in obs_df.columns if "observed" in c.lower() and "shift" in c.lower()), None)
    observed_map = {}
    if "Element" in obs_df.columns and obs_shift_col:
        observed_map = dict(zip(obs_df["Element"], obs_df[obs_shift_col]))
    return pd.DataFrame([{"Element": e, "Observed Shift": observed_map.get(e, "")} for e in elements])

def _build_observed_shift_block(elements: list, obs_df: pd.DataFrame) -> str:
    """Text-block (CSV) version of _build_observed_shift_df, for embedding
    directly in the Prompt 3 template."""
    return _build_observed_shift_df(elements, obs_df).to_csv(index=False)


def _compute_unsupported_elements(elements: list, table3_df: pd.DataFrame) -> list:
    """Deterministic (Python, never LLM-trusted) completeness check for
    Prompt 3's '# Unsupported Elements in grouping' section: an element
    from the Element List is unsupported if and only if it never appears
    as Element A or Element B in any row of the real Table 3 -- a plain
    mechanical set-difference, not a judgment call. The LLM has been
    observed writing "None" here even when a real Table 3 element had
    zero mentions in that run's own rows, despite explicit prompt
    instructions. Since this is something Python can compute with 100%
    reliability, it's computed here instead of trusted to the LLM -- same
    principle as this pipeline's other Python-verified checks (e.g.
    build_kb_sourced_table2_rows)."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    if table3_df is None or table3_df.empty:
        return elements
    mentioned = set()
    for col in ("Element A", "Element B"):
        if col in table3_df.columns:
            mentioned.update(str(v).strip() for v in table3_df[col].tolist())
    return [e for e in elements if e not in mentioned]


def _replace_prompt3_trailing_sections(interp: str, unsupported: list) -> str:
    """Strips whatever the LLM itself wrote under a '# Unsupported
    Elements in grouping' (or older '###' or unqualified 'Unsupported
    Elements' variants, or a stray '# Complete Agreement' heading the
    model might still write out of old habit even though the prompt no
    longer asks for one) heading and appends the deterministically
    computed section instead (see _compute_unsupported_elements). Splits
    at the FIRST occurrence of either heading, so a stray Complete
    Agreement heading the model might still emit is discarded along with
    everything after it, never re-emitted; only the table + narrative
    content above survives. If the LLM never wrote the heading at all,
    this just appends the section onto the end.

    Prompt 3's Reporting Instructions no longer include a '4. Complete
    Agreement' item, so this function no longer emits its own '#
    Complete Agreement' output."""
    body = re.split(r"\n\s*#{1,3}\s*(?:Complete Agreement|Unsupported Elements(?:\s+in\s+grouping)?)\b",
                     interp, maxsplit=1)[0].rstrip()
    ue_listing = "\n".join(unsupported) if unsupported else "None"
    return f"{body}\n\n# Unsupported Elements in grouping\n{ue_listing}\n"


def _build_id_to_elements_map(table2_df: pd.DataFrame, table3_df: pd.DataFrame) -> dict:
    """Maps every real PMID/Knowledge Base ID that actually appears in
    Table 2 or Table 3 to the real set of elements it evidences there --
    Table 2's own "List of Elements" column, or Table 3's own Element
    A/Element B pair. This is the real ground truth Prompt 3's own Table
    (Reporting Instructions item 1) and narrative (item 2) get checked
    against below: an ID the LLM cites for a claimed relationship is only
    real if this map actually connects at least two of that claim's
    elements under that same ID -- not just any ID that happens to exist
    somewhere in Table 1 for one of the elements alone. The LLM has been
    observed citing a real PMID that only appears in Table 1 as
    single-element evidence to back a fabricated two-element group, since
    a real citation alone doesn't guarantee the claimed relationship is
    real."""
    id_to_elements: dict = {}

    def _add(id_val, elems):
        id_val = str(id_val).strip()
        if not id_val or id_val.lower() == "nan":
            return
        id_to_elements.setdefault(id_val, set()).update(
            e.strip() for e in elems if str(e).strip())

    if table2_df is not None and not table2_df.empty:
        for _, row in table2_df.iterrows():
            elems = str(row.get("List of Elements", "")).split(";")
            _add(row.get("PMID or Knowledge Base ID", ""), elems)

    if table3_df is not None and not table3_df.empty:
        for _, row in table3_df.iterrows():
            _add(row.get("PMID or Knowledge Base ID", ""),
                 [row.get("Element A", ""), row.get("Element B", "")])

    return id_to_elements


_PROMPT3_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_PROMPT3_DIVIDER_ROW_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")
_PROMPT3_ID_CELL_RE = re.compile(r"([A-Za-z0-9_\-\.:]+)\s*\(([^)]*)\)")


def _ground_prompt3_table(interp: str, id_to_elements: dict):
    """Deterministic (Python, never LLM-trusted) citation-grounding pass
    over Prompt 3's own Reporting Instructions item-1 table: for every
    row, every cited ID is checked against _build_id_to_elements_map's
    real Table 2/3 evidence. An ID only counts as grounding a row if it
    real-evidences at least 2 of that row's own claimed "List of
    elements" -- one element alone isn't a relationship. Ungrounded IDs
    are stripped from a row's citation list; if a row ends up with zero
    grounded IDs, the entire row is dropped rather than left in place
    with a fabricated relationship and no real support (its matching
    narrative bullet is then dropped too -- see _ground_prompt3_narrative).
    Tolerates a divider row even though the prompt asks for none, since
    real LLM output doesn't always comply. Returns (rewritten interp
    text, dropped group names). Scope note: this catches a group with NO
    real grounding for any 2-element pair among its claims (the exact
    fabrication observed) -- it does not individually re-verify every
    single element listed in an otherwise-grounded group."""
    lines = interp.split("\n")
    out_lines = []
    dropped_groups = []
    header_seen = False
    in_table = False
    for line in lines:
        m = _PROMPT3_TABLE_ROW_RE.match(line)
        if m and "Group Name" in line and not header_seen:
            header_seen = True
            in_table = True
            out_lines.append(line)
            continue
        if in_table and _PROMPT3_DIVIDER_ROW_RE.match(line):
            out_lines.append(line)
            continue
        if in_table and m:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5:
                out_lines.append(line)
                continue
            group_name, id_cell, elements_cell, evidence_cell, obs_cell = cells[:5]
            group_elements = {e.strip() for e in elements_cell.split(";") if e.strip()}
            cited = _PROMPT3_ID_CELL_RE.findall(id_cell)
            grounded_ids = []
            for id_val, source_label in cited:
                real_elements = id_to_elements.get(id_val.strip(), set())
                if len(real_elements & group_elements) >= 2:
                    grounded_ids.append(f"{id_val.strip()} ({source_label.strip()})")
            if not grounded_ids:
                dropped_groups.append(group_name)
                print(f"[Prompt 3] Dropped fabricated group \"{group_name}\" -- "
                      f"none of its cited ID(s) ({id_cell}) actually connect 2+ of "
                      f"its claimed elements ({elements_cell}) in real Table 2/3 rows.")
                continue
            new_id_cell = "; ".join(grounded_ids)
            out_lines.append(f"| {group_name} | {new_id_cell} | {elements_cell} | {evidence_cell} | {obs_cell} |")
            continue
        if in_table and not m:
            in_table = False
        out_lines.append(line)
    return "\n".join(out_lines), dropped_groups


def _ground_prompt3_narrative(interp: str, dropped_groups: list) -> str:
    """Removes the narrative bullet(s) (Reporting Instructions item 2)
    that describe a group _ground_prompt3_table already dropped as
    fabricated -- narrative bullets are written as "N. **Group Name**:
    ..." matching the table's own Group Name, so a dropped group's
    narrative would otherwise restate the same fabricated relationship in
    prose even after its table row is gone. Matches by normalized
    (lowercased, stripped) title text against the dropped Group Name
    list, then renumbers the remaining bullets sequentially so there's no
    visible gap in the list."""
    if not dropped_groups:
        return interp
    dropped_norm = {g.strip().lower() for g in dropped_groups}
    bullet_re = re.compile(r"^(\d+)\.\s+\*\*(.+?)\*\*\s*:")
    kept = []
    next_num = 1
    for line in interp.split("\n"):
        m = bullet_re.match(line.strip())
        if m and m.group(2).strip().lower() in dropped_norm:
            continue
        if m:
            line = bullet_re.sub(f"{next_num}. **{m.group(2)}**:", line.strip(), count=1)
            next_num += 1
        kept.append(line)
    return "\n".join(kept)


def build_table3_interpretation(sample: str, elements: list, obs_df: pd.DataFrame, table1_df: pd.DataFrame,
                                 table2_df: pd.DataFrame, table3_df: pd.DataFrame, out_dir: Path,
                                 context: str = "disease") -> str:
    """Prompt 3, verbatim per BioShift_Prompts_0729_PD -- interprets Table
    1/Table 2/Table 3 against this dataset's real Observed Shift values.
    Trimmed version of BioShift.py's own build_table3_interpretation: same
    prompt construction and the same real citations-only guarantee (every
    citation the LLM uses must already exist in Table 1/2/3's own columns),
    but deliberately does NOT call build_combined_network at the end --
    the full multi-layer network figure is out of scope for this trimmed
    script, so this saves and returns just the raw interpretation text."""
    elements = [str(e).strip() for e in elements if str(e).strip()]
    table1_text = table1_df.to_csv(index=False) if table1_df is not None and not table1_df.empty else "(no Table 1 rows)"
    table2_text = table2_df.to_csv(index=False) if table2_df is not None and not table2_df.empty else "(no Table 2 relationships found)"
    table3_text = table3_df.to_csv(index=False) if table3_df is not None and not table3_df.empty else "(no Table 3 pairwise relationships found)"
    element_list_text = _build_observed_shift_block(elements, obs_df)
    study_context_text = _study_context_block_for_prompt()

    prompt_template = get_table_interpretation_prompt(context)
    prompt = prompt_template.format(table1=table1_text, table2=table2_text, table3=table3_text,
                                     element_list=element_list_text, study_context=study_context_text)
    interp = call_openai(prompt)

    # Deterministic citation-grounding pass over the LLM's own Reporting
    # Instructions item-1 table and item-2 narrative -- drops any
    # group/citation the LLM fabricated (a real ID that doesn't actually
    # connect 2+ of that group's claimed elements in Table 2/3). See
    # _ground_prompt3_table's docstring.
    id_to_elements = _build_id_to_elements_map(table2_df, table3_df)
    interp, dropped_groups = _ground_prompt3_table(interp, id_to_elements)
    interp = _ground_prompt3_narrative(interp, dropped_groups)

    # Deterministic override of the LLM's own "# Unsupported Elements in
    # grouping" section -- see _compute_unsupported_elements' docstring.
    # Prompt 3 no longer has a "# Complete Agreement" section at all (see
    # _replace_prompt3_trailing_sections' docstring).
    real_unsupported = _compute_unsupported_elements(elements, table3_df)
    interp = _replace_prompt3_trailing_sections(interp, real_unsupported)

    ensure_dir(Path(out_dir))
    out_file = Path(out_dir) / f"{sample}_Prompt3_output.txt"
    out_file.write_text(interp, encoding="utf-8")
    print(f"Prompt 3 (Table1+Table2+Table3 interpretation) saved: {out_file}")

    return interp


# ─────────────────── MAIN (Prompt 1/2/3, run individually or together) ─────
class _TimestampedTee:
    """Duplicates everything printed during this run into both the real
    terminal and this sample's <sample>_log.txt, prefixing each line with
    a wall-clock timestamp -- same convention as BioShift.py's own
    _TimestampedTee, so log.txt shows the identical step-by-step progress
    the terminal shows (PubMed fetch, each extraction run/batch, Table
    1/2/3 saves) plus exactly when each line happened and, at the end, the
    total run time -- without touching every individual print() call."""
    def __init__(self, real_stream, log_fh):
        self._real = real_stream
        self._log = log_fh

    def write(self, text):
        self._real.write(text)
        for i, line in enumerate(text.split("\n")):
            if i > 0:
                self._log.write("\n")
            if line.strip():
                self._log.write(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")
        self._log.flush()

    def flush(self):
        self._real.flush()
        self._log.flush()


def _run_one_sample(sample: str, mode: str, context: str) -> None:
    """Runs the full P1/P12/P123 pipeline for one sample. Split out of
    main() so --sample all can call this once per CSV in ObservedShift/
    instead of once per process invocation. Each sample still gets its
    own <sample>_log.txt and its own real cost total (via
    get_cost_summary_since), not the cumulative total across a batch."""
    observed_path = FOLDERS["observed"] / f"{sample}.csv"
    if not observed_path.exists():
        sys.exit(f"Observed-shift CSV not found: {observed_path}")

    out_dir = FOLDERS["output"] / sample
    ensure_dir(out_dir)

    # Every print() made during this run is duplicated, timestamped, into
    # <sample>_log.txt in this sample's own out_dir -- same convention as
    # BioShift.py's own per-sample log.txt -- so the total run time (and
    # everything that led to it) is recorded on disk, not just shown once
    # in the terminal.
    log_path = out_dir / f"{sample}_log.txt"
    log_fh = open(log_path, "w", encoding="utf-8")
    real_stdout = sys.stdout
    sys.stdout = _TimestampedTee(real_stdout, log_fh)
    run_start = time.time()
    cost_snapshot = _snapshot_cost_totals()
    log_fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                 f"BioShiftUpdated run started -- sample={sample}  mode={mode}  "
                 f"context={context}\n")
    log_fh.flush()

    try:
        elements, obs_df = extract_elements(observed_path)
        print(f"Loaded {len(elements)} element(s) from {observed_path}: {list(elements)}")

        table1 = table2 = table3 = None

        if mode in ("P1", "P12", "P123"):
            table1 = build_table1_evidence(sample, list(elements), obs_df, out_dir, context=context)
            print("\n=== Table 1 (Prompt 1 -> PubMed + KG evidence) ===")
            print(table1.to_string(index=False))

        if mode in ("P12", "P123"):
            table2, table3 = build_table2_coshift(sample, list(elements), out_dir, context=context)
            print("\n=== Table 2 (Prompt 2 Part A -> biological relationships) ===")
            print(table2.to_string(index=False) if not table2.empty else "(no rows)")
            print("\n=== Table 3 (Prompt 2 Part B -> pairwise relationships) ===")
            print(table3.to_string(index=False) if not table3.empty else "(no rows)")
            print("\n=== Table 3 knowledge-evidence graph (Graphviz) ===")
            build_table3_knowledge_graph(sample, table3, obs_df, out_dir)

        if mode == "P123":
            interp = build_table3_interpretation(
                sample, list(elements), obs_df, table1, table2, table3, out_dir, context=context)
            print("\n=== Prompt 3 (biological interpretation -- Table 3 knowledge graph only; "
                  "the full multi-layer network figure is out of scope for this trimmed script) ===")
            print(interp)
    finally:
        # Printed (and so logged, via the tee above) whether the run
        # finished cleanly or hit an error -- so log.txt always records how
        # long the attempt actually took, and how much it really cost.
        # This is THIS sample's own real cost since cost_snapshot, not the
        # running total across a --sample all batch (main() prints that
        # grand total separately once the whole batch finishes).
        elapsed = time.time() - run_start
        total_cost, cost_breakdown = get_cost_summary_since(cost_snapshot)
        print(f"\nTotal time: {elapsed:.1f}s ({elapsed/60:.1f} min).")
        if cost_breakdown:
            print(f"Total cost: ${total_cost:.4f}\n{cost_breakdown}")
        else:
            print("Total cost: $0.0000 (no real OpenAI calls were made this run).")
        sys.stdout = real_stdout
        log_fh.close()
    print(f"Log saved: {log_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Isolated review: Prompt 1 -> Table 1, Prompt 2 -> Table 2/Table 3, "
                    "Prompt 3 -> biological interpretation (network figure NOT included -- "
                    "out of scope for this trimmed script).")
    parser.add_argument("--sample", required=True,
                         help="Sample name, e.g. Testdata (reads ObservedShift/<sample>.csv), "
                              "or 'all' to run every *.csv already in ObservedShift/, one "
                              "after another.")
    parser.add_argument("--mode", default="P1", choices=["P1", "P12", "P123"],
                         help="Cumulative, in the order the real pipeline runs: "
                              "P1 = Prompt 1 only -> Table 1 (default); "
                              "P12 = Prompt 1 -> Table 1, then Prompt 2 -> Table 2/Table 3; "
                              "P123 = Prompt 1 -> Prompt 2 -> Prompt 3 (full run, same as "
                              "the old 'all').")
    args = parser.parse_args()

    # Study context is fixed to "disease" -- it was the only choice
    # --context ever accepted (choices=["disease"]), so it's no longer a
    # CLI flag at all; build_table1_evidence/build_table2_coshift/
    # build_table3_interpretation still take a context= parameter, it's
    # just always "disease" here now.
    context = "disease"

    if args.sample.strip().lower() == "all":
        sample_paths = sorted(FOLDERS["observed"].glob("*.csv"))
        if not sample_paths:
            sys.exit(f"--sample all: no *.csv files found in {FOLDERS['observed']}")
        samples = [p.stem for p in sample_paths]
        print(f"--sample all: found {len(samples)} sample(s): {samples}")
    else:
        samples = [args.sample]

    batch_start = time.time()
    for i, sample in enumerate(samples, 1):
        if len(samples) > 1:
            print(f"\n{'=' * 70}\n[{i}/{len(samples)}] Sample: {sample}\n{'=' * 70}")
        _run_one_sample(sample, args.mode, context)

    if len(samples) > 1:
        elapsed = time.time() - batch_start
        total_cost, cost_breakdown = get_cost_summary()
        print(f"\n{'=' * 70}\nBatch finished: {len(samples)} sample(s) in "
              f"{elapsed:.1f}s ({elapsed / 60:.1f} min).")
        if cost_breakdown:
            print(f"Batch total cost: ${total_cost:.4f}\n{cost_breakdown}")
        else:
            print("Batch total cost: $0.0000 (no real OpenAI calls were made this run).")


if __name__ == "__main__":
    main()
