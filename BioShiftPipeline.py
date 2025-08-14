# -*- coding: utf-8 -*-
"""
Batch GPT + Graphviz (merged)
Created on Sat Aug  2 13:29:30 2025
@author: prabi

This single script merges your two versions and keeps all features:
- Uses OpenAI API via key from config/api_key.txt (or OPENAI_API_KEY env)
- Loads model settings from config/gpt_options.json
- Observed flows: shift_only, full_with_graphviz, full_no_graphviz,
  interpret_only, interpret_and_graphviz, graphviz_only
- Table3-direct flows:
  • table3_direct: run Prompt 3 and/or Graphviz on a specific Table3 CSV (or scan inputs/table3 if --table3 omitted)
  • table3_batch: process all *.csv under inputs/table3/
- Optional --observed_dir override for custom observed folder

Expected layout:
  config/
    api_key.txt
    gpt_options.json
  inputs/
    observed/*.csv
    graphviz/*.dot (or .txt)
    table3/*.csv             # NEW: drop Table3 CSVs here for direct mode
  outputs/

Example usages:
  python batch_gpt_graphviz.py --context disease --mode shift_only
  python batch_gpt_graphviz.py --context healthy --mode full_with_graphviz --sample 10737_stable
  python batch_gpt_graphviz.py --context disease --mode interpret_and_graphviz --sample 10737_progressing
  python batch_gpt_graphviz.py --context healthy --mode graphviz_only --sample 10737_stable
  python batch_gpt_graphviz.py --context disease --mode table3_direct --table3 "inputs/table3/10737_stable_table3.csv" --run interpret_graphviz
  python batch_gpt_graphviz.py --context disease --mode table3_direct --run graphviz      # scans inputs/table3/*.csv
  python batch_gpt_graphviz.py --context disease --mode table3_batch --run interpret
  python batch_gpt_graphviz.py --context disease --mode full_no_graphviz --observed_dir "C:/path/to/observed"

config/gpt_options.json example:
{
  "default_model": "gpt-4o-mini",
  "models": {
    "gpt-4o-mini": {"temperature": 0.2, "max_tokens": 2000}
  }
}
"""

import argparse
import re
import subprocess
import sys
import time
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from io import StringIO

import pandas as pd
import openai

# ─────────────────── CONFIG ────────────────────────────────────────────────
CONFIG_DIR = Path("config")
API_KEY_FILE = CONFIG_DIR / "api_key.txt"
GPT_OPTIONS_FILE = CONFIG_DIR / "gpt_options.json"

INPUTS = Path("inputs")
FOLDERS = {
    "observed": INPUTS / "observed",
    "graphviz": INPUTS / "graphviz",
    "table3": INPUTS / "table3",  # NEW: Table3 CSV source folder
    "output": Path("outputs"),
}


def load_api_key() -> str:
    """Return API key from file or env; exit with clear help if missing."""
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if key:
        return key
    sys.exit("❌ No API key found. Create config/api_key.txt or set OPENAI_API_KEY.")


def load_gpt_options() -> dict:
    if not GPT_OPTIONS_FILE.exists():
        sys.exit(f"❌ Missing GPT options: {GPT_OPTIONS_FILE}")
    with open(GPT_OPTIONS_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "models" not in cfg or "default_model" not in cfg:
        sys.exit("❌ Invalid GPT options: need 'models' and 'default_model'.")
    if cfg["default_model"] not in cfg["models"]:
        sys.exit(f"❌ Default model '{cfg['default_model']}' not in models.")
    return cfg


openai.api_key = load_api_key()
GPT_CONFIG = load_gpt_options()
DEFAULT_MODEL = GPT_CONFIG["default_model"]
MODEL_SETTINGS = GPT_CONFIG["models"][DEFAULT_MODEL]

# ─────────────────── PROMPTS (place-holders) ───────────────────────────────
PROMPT_D1 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I want to investigate for potential associations with gum disease.

{elements}

Analysis Instructions:
For each item in the input list, determine whether it is commonly reported to be associated with gum disease. If there is no established association, do not report any.
For those with known associations, identify whether they are typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased in different studies) in gum disease. If the direction of the association is unknown or unclear, indicate this as well.

Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.

( The table should contain columns “Element” and “GPT shift 1”. Also make sure each element should have its own row. Summary table 'A', Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
 
USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART."""

PROMPT_D2 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I would like to investigate to determine whether any of them are commonly reported to shift together in gum disease.
{elements}

Analysis Instructions:
For each item in the input list, identify any groups or pairs of elements that are commonly reported to shift jointly in gum disease. If there is no established evidence of joint shifts, do not report any.
For each identified group or pair, determine whether each element is typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased in different studies). The direction of the shift may differ among elements within a group. If the direction of change is unknown or unclear, indicate this.

Reporting Instructions:
Create a summary table of your findings. Include only those groups or pairs with established evidence of joint shifts. Each row should represent a group or pair, with the direction of change (increase, decrease, mixed, or unknown) noted for each element. Exclude items with no known evidence of joint shifts.

(Summary table 'B', Include columns “Element”, “GPT shift 2”, “Biological Group”, "Group ID based on Biological Group', “Notes (if any”)
"Biological Group" should be based on joint functional and mechanistic activity of grouped elements and completely without generic labels. 
“Group ID based on Biological Group” should have same group ID based on "Biological Group". Every element should be categorized on shared biological activities.  
 Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
 
USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART."""

PROMPT_D3 = """These are groups of jointly shifted microbiomes, immune cells, and/or proteins observed in gum disease that you are interested in. What is the biological interpretation? If well-established pathways of immune–microbiome interaction or immune regulation are involved, describe them and integrate them into the interpretation. Be sure to consider the direction of the shifts.

{table3}

Provide your analysis in a clear, structured format.
 
USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.

"""


PROMPT_H1 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I want to investigate for potential associations with healthy gum status, including recovery from gum disease.
{elements}

Analysis Instructions:
For each item in the input list, determine whether it is commonly reported to be associated with healthy gums. If there is no established association, do not report it.
For those with known associations, identify whether they are typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased in different studies) in healthy gum status. If the direction of the association is unknown or unclear, indicate this as well.
---------------------------------------------------------------------------------------------------------------------------


Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.
The table should contain columns “Element” and “GPT shift 1”. Also make sure each element should have its own row. 
( Summary table 'A', Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 

USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.
"""

PROMPT_H2 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I want to investigate for potential associations with healthy gum status, including recovery from gum disease.
{elements}

Analysis Instructions:
For each item in the input list, identify any groups or pairs of elements that are commonly reported to shift jointly in healthy gum status or recovery from gum disease. If there is no established evidence of joint shifts, do not report them.
For each identified group or pair, determine whether each element is typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased across studies). The direction of change may differ among elements within a group. If the direction is unknown or unclear, indicate this as well.
---------------------------------------------------------------------------------------------------------------------------

Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.

(Summary table 'B', Include columns “Element”, “GPT shift 2”, “Biological Group”, "Group ID based on Biological Group', “Notes (if any”)
"Biological Group" should be based on joint functional and mechanistic activity of grouped elements and completely without generic labels. 
“Group ID based on Biological Group” should have same group ID based on "Biological Group". Every element should be categorized on shared biological activities.  
 Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.
"""

PROMPT_H3 = """These are lists of groups of jointly shifted microbiomes, cells, and/or proteins in recovery from healthy gum that you are interested in. What is the biological interpretation? If well-established pathways of immune–microbiome interaction or immune pathways are involved, describe them and integrate them into the biological interpretation. Be aware of the direction of shifts, and the biological interpretation should include this information.


{table3}

Provide your analysis in a clear, structured format.
USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART."""

PROMPTS = {
    "disease": {"A": PROMPT_D1, "B": PROMPT_D2, "INT": PROMPT_D3},
    "healthy": {"A": PROMPT_H1, "B": PROMPT_H2, "INT": PROMPT_H3},
}

# ─────────────────── UTILITIES ─────────────────────────────────────────────

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def call_openai(prompt: str) -> str:
    """Call OpenAI ChatCompletion using settings from gpt_options.json."""
    for attempt in range(3):
        try:
            resp = openai.ChatCompletion.create(
                model=DEFAULT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=MODEL_SETTINGS.get("temperature", 0.2),
                max_tokens=MODEL_SETTINGS.get("max_tokens", 2000),
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"⚠️  OpenAI error ({attempt+1}/3): {e}")
            time.sleep(2)
    return ""


def _extract_clean_table(raw: str, min_cols: int = 2) -> str:
    """Extract only pipe-separated rows from an LLM response."""
    lines = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= min_cols and any(parts):
            lines.append("|".join(parts))
    return "\n".join(lines)


def extract_elements(observed_path: Path, elements_path: Path):
    df = pd.read_csv(observed_path)
    cols = [c for c in df.columns if c.lower().startswith("element")]
    if not cols:
        raise ValueError(f"No 'Element' column found in {observed_path}")
    elements = df[cols[0]].astype(str).str.strip().unique()
    ensure_dir(elements_path.parent)
    elements_path.write_text("\n".join(elements), encoding="utf-8")
    return elements, df


def clean_and_save_table_ab(promptA: str, promptB: str, out_csv: Path) -> pd.DataFrame:
    # Strip LLM chatter; keep only the tables
    promptA_clean = _extract_clean_table(promptA)
    promptB_clean = _extract_clean_table(promptB)

    a = pd.read_csv(StringIO(promptA_clean), sep="|", engine="python", skipinitialspace=True)
    b = pd.read_csv(StringIO(promptB_clean), sep="|", engine="python", skipinitialspace=True)

    a = a.loc[:, ~a.columns.str.contains("^Unnamed")]
    b = b.loc[:, ~b.columns.str.contains("^Unnamed")]
    a.columns = a.columns.str.strip()
    b.columns = b.columns.str.strip()

    for df in [a, b]:
        for col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace("nan", "")

    a["Element"] = a["Element"].astype(str).str.strip()
    b["Element"] = b["Element"].astype(str).str.strip()

    drop_patterns = [r"^[-\s]*$", r"^element$", r"^$"]
    pattern = "|".join(drop_patterns)
    a = a[~a["Element"].str.strip().str.lower().str.match(pattern)]
    b = b[~b["Element"].str.strip().str.lower().str.match(pattern)]

    table_ab = pd.merge(a, b, on="Element", how="outer", suffixes=("_A", "_B"))

    a_cols = [col for col in a.columns if col != "Element"]
    b_cols = [col for col in b.columns if col != "Element"]
    col_order = ["Element"] + a_cols + b_cols
    table_ab = table_ab[col_order]

    table_ab = table_ab.loc[:, ~table_ab.columns.str.contains("^Unnamed")]
    for col in table_ab.select_dtypes(include="object").columns:
        table_ab[col] = (
            table_ab[col]
            .replace("nan", "")
            .fillna("")
            .astype(str)
            .str.strip()
        )

    table_ab = table_ab.fillna("").sort_values("Element", kind="stable").reset_index(drop=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    table_ab.to_csv(out_csv, index=False)
    print(f"✅ Clean TableAB saved: {out_csv}")
    return table_ab

# ─────────────────── GRAPHVIZ ──────────────────────────────────────────────

# ─────────────────── GRAPHVIZ ──────────────────────────────────────────────

def graph_highlight(sample: str, t3_path: Path, out_graph_dir: Path):
    """Highlight matched elements in ALL Graphviz files; output <sample>_<graph>_highlighted.jpg."""
    df = pd.read_csv(t3_path)
    df["Element"] = df["Element"].astype(str).str.strip()
    df["Observed Shift"] = df["Observed Shift"].astype(str).str.strip()

    ensure_dir(out_graph_dir)
    graph_files = list(FOLDERS["graphviz"].glob("*.dot")) + list(FOLDERS["graphviz"].glob("*.txt"))
    if not graph_files:
        print(f"⚠️ No Graphviz files found in {FOLDERS['graphviz']}")
        return

    for graph_file in graph_files:
        with graph_file.open(encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        new_lines = []
        for ln in lines:
            hit = False
            for el, s in zip(df["Element"], df["Observed Shift"]):
                if re.match(rf'\s*"{re.escape(el)}"\s*\[', ln):
                    # Normalize shift values like '1.0' → '1'
                    s_norm = re.sub(r"\.0+$", "", str(s).strip())
                    color = "green" if s_norm == "1" else ("blue" if s_norm == "-1" else None)

                    if color:
                        attrs = ln.split("[", 1)[1].rsplit("]", 1)[0]
                        new_lines.append(f'"{el}" [{attrs}, style=filled, fillcolor={color}]\n')
                    else:
                        # Leave untouched if not +1 or -1
                        new_lines.append(ln)

                    hit = True
                    break
            if not hit:
                new_lines.append(ln)

        jpg_out = out_graph_dir / f"{sample}_{graph_file.stem}_highlighted.jpg"
        with NamedTemporaryFile("w", delete=False, suffix=".dot", encoding="utf-8") as tmp:
            tmp.write("".join(new_lines))
            tmp_path = Path(tmp.name)

        try:
            subprocess.run(["dot", "-Tjpg", str(tmp_path), "-o", str(jpg_out)], check=True)
            print(f"✅ Highlighted graph saved: {jpg_out}")
        finally:
            tmp_path.unlink(missing_ok=True)


# ─────────────────── OBSERVED WORKFLOW HELPERS ─────────────────────────────

def run_prompt_set(elements, context: str, prompt_dir: Path):
    t = PROMPTS[context]
    outA = call_openai(t["A"].format(elements="\n".join(elements)))
    outB = call_openai(t["B"].format(elements="\n".join(elements)))
    ensure_dir(prompt_dir)
    (prompt_dir / "PromptA_output.txt").write_text(outA, encoding="utf-8")
    (prompt_dir / "PromptB_output.txt").write_text(outB, encoding="utf-8")
    return outA, outB


def make_merged_table(stem, out_base, outA, outB, obs_df):
    tables_dir = out_base / "tables"
    ensure_dir(tables_dir)
    tab_ab_file = tables_dir / f"{stem}_tableAB.csv"
    table_ab = clean_and_save_table_ab(outA, outB, tab_ab_file)

    obs_df["Element"] = obs_df["Element"].astype(str).str.strip()
    table1 = pd.merge(table_ab, obs_df, on="Element", how="left")

    obs_cols = [c for c in obs_df.columns if c != "Element"]
    out_cols = [c for c in table1.columns if c not in obs_cols]
    col_order = ["Element"] + [c for c in out_cols if c != "Element"] + obs_cols
    table1 = table1[col_order].sort_values("Element", kind="stable").reset_index(drop=True)

    for col in table1.select_dtypes(include="object").columns:
        table1[col] = table1[col].str.strip().replace("nan", "")
    table1.fillna("", inplace=True)

    tab1_file = tables_dir / f"{stem}_table1.csv"
    table1.to_csv(tab1_file, index=False)
    print(f"✅ Table1 saved: {tab1_file}")
    return table1, tables_dir


def build_table2_3(sample, context, table1, tables_dir, prompt_dir):
    req_cols = ["Element", "Observed Shift", "GPT shift 2", "Biological Group"]
    if not all(col in table1.columns for col in req_cols):
        print("⚠️  Missing columns for Table2 & Table3.")
        return None

    t2 = table1[req_cols].copy()
    t2.rename(columns={"GPT shift 2": "Expected Shift"}, inplace=True)

    for col in ["Observed Shift", "Expected Shift"]:
        t2[col] = (
            t2[col].astype(str).str.strip().replace({"nan": ""}).str.replace(r"\.0+$", "", regex=True)
        )

    t2["Biological Group"] = t2["Biological Group"].astype(str).str.strip()
    t2["Element"] = t2["Element"].astype(str).str.strip()
    t2 = t2.sort_values(["Biological Group", "Element"], kind="stable").reset_index(drop=True)
    t2.fillna("", inplace=True)

    t2_path = tables_dir / f"{sample}_table2.csv"
    t2.to_csv(t2_path, index=False)

    mask = (
        (t2["Observed Shift"] == t2["Expected Shift"]) &
        (t2.groupby("Biological Group")["Biological Group"].transform("size") > 1)
    )
    t3 = t2[mask].reset_index(drop=True)
    t3_path = tables_dir / f"{sample}_table3.csv"
    t3.to_csv(t3_path, index=False)

    interp_prompt = PROMPTS[context]["INT"].format(table3=t3.to_csv(index=False))
    interp = call_openai(interp_prompt)
    (prompt_dir / f"{sample}_Prompt3_output.txt").write_text(interp, encoding="utf-8")
    return t3_path


def run_shift_only(stem, ctx, out_base):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, out_base / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, out_base / "prompts")
    make_merged_table(stem, out_base, outA, outB, obs_df)


def run_full(stem, ctx, with_graph, out_base):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, out_base / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, out_base / "prompts")
    merged, tables_dir = make_merged_table(stem, out_base, outA, outB, obs_df)
    t3_path = build_table2_3(stem, ctx, merged, tables_dir, out_base / "prompts")
    if with_graph and t3_path is not None:
        graph_highlight(stem, t3_path, out_base / "graphviz")


def run_interpret(stem, ctx, dot_required, out_base, table3_path=None):
    t3_path = Path(table3_path) if table3_path else out_base / "tables" / f"{stem}_table3.csv"
    if not t3_path.exists():
        print(f"⚠️  Missing Table3 for {stem}: {t3_path}")
        return
    df = pd.read_csv(t3_path)
    interp_prompt = PROMPTS[ctx]["INT"].format(table3=df.to_csv(index=False))
    interp = call_openai(interp_prompt)
    ensure_dir(out_base / "prompts")
    (out_base / "prompts" / f"{stem}_Prompt3_output.txt").write_text(interp, encoding="utf-8")
    if dot_required:
        graph_highlight(stem, t3_path, out_base / "graphviz")

# ─────────────────── TABLE3 DIRECT ─────────────────────────────────────────

def run_table3_direct(table3_path: Path, ctx: str, run_mode: str = "interpret_graphviz"):
    """
    Consume a single Table3 CSV and (optionally) run Prompt 3 and/or Graphviz.
    run_mode ∈ {"interpret", "graphviz", "interpret_graphviz"}.
    Outputs go to: outputs/<Disease|Healthy>/table3direct/<file_stem>/<run_mode>/
    """
    t3_path = Path(table3_path)
    if not t3_path.exists():
        sys.exit(f"❌ Table3 file not found: {t3_path}")

    sample = t3_path.stem  # keep full file stem
    parent_ctx = "Disease" if ctx == "disease" else "Healthy"

    base_dir = FOLDERS["output"] / parent_ctx / "table3direct" / sample / run_mode
    ensure_dir(base_dir)

    df = pd.read_csv(t3_path)

    if run_mode in ("interpret_graphviz", "interpret"):
        interp_prompt = PROMPTS[ctx]["INT"].format(table3=df.to_csv(index=False))
        interp_text = call_openai(interp_prompt)
        out_file = base_dir / f"{sample}_Prompt3_output.txt"
        out_file.write_text(interp_text, encoding="utf-8")
        print(f"✅ Prompt 3 saved: {out_file}")

    if run_mode in ("interpret_graphviz", "graphviz"):
        graph_highlight(sample, t3_path, base_dir)
        print(f"✅ Graphviz highlights saved in: {base_dir}")


def run_table3_batch(ctx: str, run_mode: str = "interpret_graphviz"):
    """Process all *.csv in inputs/table3/ with table3_direct pipeline."""
    in_dir = FOLDERS["table3"]
    if not in_dir.exists():
        sys.exit(f"❌ inputs/table3 folder not found: {in_dir}")
    files = sorted(in_dir.glob("*.csv"))
    if not files:
        sys.exit(f"❌ No CSV files found in {in_dir}")
    for csv in files:
        print(f"\n🔎 Table3-direct: processing {csv.name}")
        run_table3_direct(csv, ctx, run_mode)
    print("\n✅ Table3-direct batch finished.")

# ─────────────────── CLI ───────────────────────────────────────────────────

def parse_cli():
    p = argparse.ArgumentParser("Batch GPT + Graphviz")
    p.add_argument("--context", choices=["disease", "healthy"], required=True)

    p.add_argument("--mode", choices=[
        # Table3 direct flow
        "table3_direct",           # single file or scans inputs/table3 if --table3 omitted
        "table3_batch",            # explicit batch over inputs/table3/*.csv
        # Observed flows
        "shift_only", "full_with_graphviz", "full_no_graphviz",
        "interpret_only", "interpret_and_graphviz", "graphviz_only",
    ], required=True)

    # Table3-direct/batch options
    p.add_argument("--run", choices=["interpret", "graphviz", "interpret_graphviz"],
                   default="interpret_graphviz",
                   help="What to run for table3_direct/batch modes")
    p.add_argument("--table3", help="Path to a Table3 CSV (optional for table3_direct). If omitted, scans inputs/table3/.")

    # Observed options
    p.add_argument("--sample", help="Process single CSV stem (no .csv); else process ALL in observed/")
    p.add_argument("--observed_dir", help="Override path to observed CSV folder")

    return p.parse_args()

# ─────────────────── MAIN ──────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_cli()

    # Allow overriding observed folder
    if args.observed_dir:
        FOLDERS["observed"] = Path(args.observed_dir)

    # Table3 flows
    if args.mode == "table3_direct":
        if args.table3:
            run_table3_direct(Path(args.table3), args.context, args.run)
        else:
            # Scan inputs/table3/
            run_table3_batch(args.context, args.run)
        print("\n✅ Table3-direct pipeline finished.")
        sys.exit(0)

    if args.mode == "table3_batch":
        run_table3_batch(args.context, args.run)
        sys.exit(0)

    # Observed flows
    csv_stems = sorted([p.stem for p in FOLDERS["observed"].glob("*.csv")])
    if args.sample:
        if args.sample not in csv_stems:
            sys.exit(f"❌ Sample '{args.sample}' not found in {FOLDERS['observed']}/")
        csv_stems = [args.sample]
    if not csv_stems:
        sys.exit(f"❌ No CSV files in {FOLDERS['observed']}/")

    for stem in csv_stems:
        parent_ctx = "Disease" if args.context == "disease" else "Healthy"
        out_base = FOLDERS["output"] / parent_ctx / stem
        ensure_dir(out_base)
        print(f"\n🔄 Processing {stem}.csv as {args.context} → {out_base}")

        if args.mode == "shift_only":
            run_shift_only(stem, args.context, out_base)
        elif args.mode == "full_with_graphviz":
            run_full(stem, args.context, with_graph=True, out_base=out_base)
        elif args.mode == "full_no_graphviz":
            run_full(stem, args.context, with_graph=False, out_base=out_base)
        elif args.mode == "interpret_only":
            run_interpret(stem, args.context, dot_required=False, out_base=out_base)
        elif args.mode == "interpret_and_graphviz":
            run_interpret(stem, args.context, dot_required=True, out_base=out_base)
        elif args.mode == "graphviz_only":
            t3_path = out_base / "tables" / f"{stem}_table3.csv"
            if t3_path.exists():
                graph_highlight(stem, t3_path, out_base / "graphviz")
            else:
                print(f"⚠️  Skipping graphviz_only for {stem}; missing {t3_path}")

    print("\n✅ Pipeline finished for all samples.")
