# -*- coding: utf-8 -*-
"""
BioShiftPipeline (merged)
Created on Sat Aug  2 13:29:30 2025
@author: prabi
"""

import argparse
import os
import re
import sys
import time
import json
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from io import StringIO

import openai
import pandas as pd

# ─────────────────── CONFIG ────────────────────────────────────────────────
CONFIG_DIR = Path("config")
API_KEY_FILE = CONFIG_DIR / "api_key.txt"
GPT_OPTIONS_FILE = CONFIG_DIR / "gpt_options.json"

INPUTS = Path("inputs")
FOLDERS = {
    "observed": INPUTS / "observed",
    "graphviz": INPUTS / "graphviz",
    "output": Path("outputs"),
}

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def load_api_key() -> str:
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
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I would like to investigate to determine whether any of them are commonly reported to shift together in gum disease.
{elements}

Analysis Instructions:
For each item in the input list, identify any groups or pairs of elements that are commonly reported to shift jointly in gum disease. If there is no established evidence of joint shifts, do not report any.
For each identified group or pair, determine whether each element is typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased in different studies). The direction of the shift may differ among elements within a group. If the direction of change is unknown or unclear, indicate this.

Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.
The table should contain columns “Element” and “GPT shift 1”. Also make sure each element should have its own row. 
( Summary table 'A', Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_D2 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I would like to investigate to determine whether any of them are commonly reported to shift together in gum disease.
{elements}

Analysis Instructions:
For each item in the input list, determine whether it is commonly reported to be associated with healthy gums. If there is no established association, do not report it.
For those with known associations, identify whether they are typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased in different studies) in healthy gum status. If the direction of the association is unknown or unclear, indicate this as well.

Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.
(Summary table 'B', Include columns “Element”, “GPT shift 2”, “Biological Group”, "Group ID based on Biological Group', “Notes (if any”)
"Biological Group" should be based on joint functional and mechanistic activity of grouped elements and completely without generic labels. 
“Group ID based on Biological Group” should have same group ID based on "Biological Group". Every element should be categorized on shared biological activities.  
 Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_D3 = """These are groups of jointly shifted microbiomes, immune cells, and/or proteins observed in gum disease that you are interested in. What is the biological interpretation?

{table3}

Provide your analysis in a clear, structured format."""

PROMPT_H1 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I want to investigate for potential associations with healthy gum status, including recovery from gum disease.

Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. The table should contain columns “Element” and “GPT shift 1”...
( Summary table 'A', Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_H2 = """AI Role:
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

(Summary table 'B', Include columns “Element”, “GPT shift 2”, “Biological Group”, "Group ID based on Biological Group', “Notes (if any”)
"Biological Group" should be based on joint functional and mechanistic activity of grouped elements and completely without generic labels. 
“Group ID based on Biological Group” should have same group ID based on "Biological Group". Every element should be categorized on shared biological activities.  
 Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_H3 = """These are lists of groups of jointly shifted microbiomes, cells, and/or proteins in recovery from healthy gum that you are interested in. What is the biological interpretation?

{table3}

Provide your analysis in a clear, structured format."""

PROMPTS = {
    "disease": {"A": PROMPT_D1, "B": PROMPT_D2, "INT": PROMPT_D3},
    "healthy": {"A": PROMPT_H1, "B": PROMPT_H2, "INT": PROMPT_H3},
}

# ─────────────────── OpenAI helper ─────────────────────────────────────────
def call_openai(prompt: str, model: str = None):
    model = model or DEFAULT_MODEL
    settings = GPT_CONFIG["models"].get(model, MODEL_SETTINGS)
    temperature = settings.get("temperature", 0.5)
    max_tokens = settings.get("max_tokens", 2048)

    for attempt in range(3):
        try:
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"⚠️ OpenAI error ({attempt+1}/3): {e}")
            time.sleep(2)
    return ""

# ── Robust parsing helpers ─────────────────────────────────────────────────
def _sanitize_table_text(txt: str) -> str:
    """Remove code fences and keep only pipe-table lines."""
    if not isinstance(txt, str):
        return ""
    txt = re.sub(r"^```[\w-]*\s*", "", txt.strip())
    txt = re.sub(r"\s*```$", "", txt.strip())
    lines = []
    for ln in txt.splitlines():
        if "|" not in ln:
            continue
        if re.fullmatch(r"\s*\|?\s*-{2,}.*", ln):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()

def _coerce_element_column(df: pd.DataFrame, label_hint: str) -> pd.DataFrame:
    """Ensure an 'Element' column exists; otherwise rename a candidate."""
    if "Element" in df.columns:
        return df
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    if "element" in lower_map:
        return df.rename(columns={lower_map["element"]: "Element"})
    candidates = [c for c in df.columns if c and not str(c).strip().lower().startswith("unnamed")]
    if candidates:
        return df.rename(columns={candidates[0]: "Element"})
    raise KeyError(f"Element column missing in {label_hint}. Columns found: {list(df.columns)}")

# ─────────────────── Core data utilities ───────────────────────────────────
def extract_elements(observed_path: Path, elements_path: Path):
    df = pd.read_csv(observed_path)
    cols = [c for c in df.columns if str(c).lower().startswith("element")]
    if not cols:
        raise ValueError(f"No 'Element' column found in {observed_path}")
    elements = df[cols[0]].astype(str).str.strip().unique()
    ensure_dir(elements_path.parent)
    elements_path.write_text("\n".join(elements), encoding="utf-8")
    return elements, df

def clean_and_save_table_ab(promptA: str, promptB: str, out_csv: Path) -> pd.DataFrame:
    """
    Merge PromptA and PromptB, keep all elements, clean whitespace/NaNs, remove Unnamed cols.
    """
    promptA_s = _sanitize_table_text(promptA)
    promptB_s = _sanitize_table_text(promptB)

    a = pd.read_csv(StringIO(promptA_s), sep="|", engine="python", skipinitialspace=True, comment="#")
    b = pd.read_csv(StringIO(promptB_s), sep="|", engine="python", skipinitialspace=True, comment="#")

    a = a.loc[:, [col for col in a.columns if str(col).strip()]]
    b = b.loc[:, [col for col in b.columns if str(col).strip()]]

    a = a.loc[:, ~a.columns.str.contains("^Unnamed", case=False, na=False)]
    b = b.loc[:, ~b.columns.str.contains("^Unnamed", case=False, na=False)]

    a.columns = a.columns.astype(str).str.strip()
    b.columns = b.columns.astype(str).str.strip()
    for df in (a, b):
        for col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace({"nan": "", "NaN": ""})

    a = _coerce_element_column(a, "PromptA")
    b = _coerce_element_column(b, "PromptB")

    drop_patterns = [r"^[-\s]*$", r"^element$", r"^$"]
    pattern = "|".join(drop_patterns)
    a = a[~a["Element"].astype(str).str.strip().str.lower().str.match(pattern, na=False)]
    b = b[~b["Element"].astype(str).str.strip().str.lower().str.match(pattern, na=False)]

    table_ab = pd.merge(a, b, on="Element", how="outer", suffixes=('_A', '_B'))

    a_cols = [col for col in a.columns if col != "Element"]
    b_cols = [col for col in b.columns if col != "Element"]
    col_order = ["Element"] + [c for c in a_cols if c in table_ab.columns] + [c for c in b_cols if c in table_ab.columns]
    table_ab = table_ab[col_order]

    table_ab = table_ab.loc[:, ~table_ab.columns.str.contains("^Unnamed", case=False, na=False)]
    for col in table_ab.select_dtypes(include="object").columns:
        table_ab[col] = table_ab[col].replace({"nan": "", "NaN": ""}).fillna("").astype(str).str.strip()
    table_ab = table_ab.fillna("").sort_values("Element", kind="stable").reset_index(drop=True)

    ensure_dir(out_csv.parent)
    table_ab.to_csv(out_csv, index=False)
    print(f"✅ Clean TableAB saved: {out_csv} | shape: {table_ab.shape}")
    return table_ab

def make_merged_table(stem: str, out_base: Path, outA: str, outB: str, obs_df: pd.DataFrame):
    tables_dir = out_base / "tables"
    ensure_dir(tables_dir)
    tab_ab_file = tables_dir / f"{stem}_tableAB.csv"
    table_ab = clean_and_save_table_ab(outA, outB, tab_ab_file)

    obs_df = obs_df.copy()
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
    print(f"✅ Table1 saved (TableAB + observed): {tab1_file} | shape: {table1.shape}")
    return table1, tables_dir

def build_table2_3(
    sample: str,
    context: str,
    table1: pd.DataFrame,
    tables_dir: Path,
    prompt_dir: Path,
    model: str = None,
):
    req_cols = ["Element", "Observed Shift", "GPT shift 2", "Biological Group"]
    if not all(col in table1.columns for col in req_cols):
        print("⚠️  Missing columns for Table2 & Table3. Skipping.")
        return None

    t2 = table1[req_cols].copy()
    t2.rename(columns={"GPT shift 2": "Expected Shift"}, inplace=True)

    for col in ["Observed Shift", "Expected Shift"]:
        t2[col] = (
            t2[col]
            .astype(str)
            .str.strip()
            .replace({"nan": ""})
            .str.replace(r"\.0+$", "", regex=True)
        )

    t2["Biological Group"] = t2["Biological Group"].astype(str).str.strip()
    t2["Element"] = t2["Element"].astype(str).str.strip()
    t2 = t2.sort_values(["Biological Group", "Element"], kind="stable").reset_index(drop=True)
    t2.fillna("", inplace=True)

    t2_path = tables_dir / f"{sample}_table2.csv"
    t2.to_csv(t2_path, index=False)
    print(f"✅ Table2 saved: {t2_path} | shape: {t2.shape}")

    mask = (
        (t2["Observed Shift"] == t2["Expected Shift"]) &
        (t2.groupby("Biological Group")["Biological Group"].transform("size") > 1)
    )
    t3 = t2[mask].reset_index(drop=True)
    t3_path = tables_dir / f"{sample}_table3.csv"
    t3.to_csv(t3_path, index=False)
    print(f"✅ Table3 saved: {t3_path} | shape: {t3.shape}")

    ensure_dir(prompt_dir)
    interp_prompt = PROMPTS[context]["INT"].format(table3=t3.to_csv(index=False))
    interp = call_openai(interp_prompt, model=model)
    (prompt_dir / f"{sample}_Prompt3_output.txt").write_text(interp, encoding="utf-8")

    return t3_path

# ─────────────────── GRAPHVIZ (all sources) ────────────────────────────────
def _list_graph_sources() -> list[Path]:
    base = FOLDERS["graphviz"]
    return sorted(list(base.glob("*.dot")) + list(base.glob("*.txt")))

def _render_highlighted(dot_src: Path, df: pd.DataFrame, jpg_out: Path):
    df = df.copy()
    df["Element"] = df["Element"].astype(str).str.strip()
    df["Observed Shift"] = df["Observed Shift"].astype(str).str.strip()
    colors = {"1": "green", "-1": "blue"}

    with dot_src.open(encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    new_lines = []
    for ln in lines:
        hit = False
        for el, s in zip(df["Element"], df["Observed Shift"]):
            if re.match(rf'\s*"{re.escape(el)}"\s*\[', ln):
                attrs = ln.split("[", 1)[1].rsplit("]", 1)[0]
                if "fillcolor=" not in attrs:
                    new_lines.append(f'"{el}" [{attrs}, style=filled, fillcolor={colors.get(s, "yellow")}]\n')
                else:
                    new_lines.append(ln)
                hit = True
                break
        if not hit:
            new_lines.append(ln)

    ensure_dir(jpg_out.parent)
    with NamedTemporaryFile("w", delete=False, suffix=".dot", encoding="utf-8") as tmp:
        tmp.write("".join(new_lines))
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(["dot", "-Tjpg", str(tmp_path), "-o", str(jpg_out)], check=True)
        print(f"✅ Graphviz JPEG saved → {jpg_out}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Graphviz failed for {dot_src.name}: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)

def graph_highlight_all(sample: str, t3_path: Path, out_graph_dir: Path):
    sources = _list_graph_sources()
    if not sources:
        print(f"⚠️  No DOT/TXT files found in {FOLDERS['graphviz']}; graphviz skipped.")
        return
    df = pd.read_csv(t3_path)
    for src in sources:
        tag = src.stem
        jpg_out = out_graph_dir / f"{sample}_{tag}_highlighted.jpg"
        _render_highlighted(src, df, jpg_out)

# ─────────────────── PROMPT RUNNERS ────────────────────────────────────────
def run_prompt_set(elements, context: str, prompt_dir: Path, model: str = None):
    t = PROMPTS[context]
    outA = call_openai(t["A"].format(elements="\n".join(elements)), model=model)
    outB = call_openai(t["B"].format(elements="\n".join(elements)), model=model)
    ensure_dir(prompt_dir)
    (prompt_dir / "PromptA_output.txt").write_text(outA, encoding="utf-8")
    (prompt_dir / "PromptB_output.txt").write_text(outB, encoding="utf-8")
    return outA, outB

# ─────────────────── MODE EXECUTION ────────────────────────────────────────
def run_shift_only(stem: str, ctx: str, out_base: Path, model: str = None):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, out_base / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, out_base / "prompts", model=model)
    make_merged_table(stem, out_base, outA, outB, obs_df)

def run_full(stem: str, ctx: str, with_graph: bool, out_base: Path, model: str = None):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, out_base / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, out_base / "prompts", model=model)
    merged, tables_dir = make_merged_table(stem, out_base, outA, outB, obs_df)
    t3_path = build_table2_3(stem, ctx, merged, tables_dir, out_base / "prompts", model=model)
    if with_graph and t3_path is not None:
        graph_highlight_all(stem, t3_path, out_base / "graphviz")

def run_interpret(stem: str, ctx: str, dot_required: bool, out_base: Path, model: str = None, table3_path: str | None = None):
    t3_path = Path(table3_path) if table3_path else out_base / "tables" / f"{stem}_table3.csv"
    if not t3_path.exists():
        print(f"❌ Table3 not found for {stem}: {t3_path}")
        return
    df = pd.read_csv(t3_path)
    interp_prompt = PROMPTS[ctx]["INT"].format(table3=df.to_csv(index=False))
    interp = call_openai(interp_prompt, model=model)
    ensure_dir(out_base / "prompts")
    (out_base / "prompts" / f"{stem}_Prompt3_output.txt").write_text(interp, encoding="utf-8")
    if dot_required:
        graph_highlight_all(stem, t3_path, out_base / "graphviz")

def run_table3_direct(table3_path: str, ctx: str, run_mode: str = "interpret_graphviz", model: str = None):
    """
    Consume a Table3 CSV and (optionally) run Prompt 3 and/or Graphviz.
    run_mode ∈ {"interpret", "graphviz", "interpret_graphviz"}.
    Outputs: outputs/<Disease|Healthy>/table3direct/<run_mode>/
    """
    t3_path = Path(table3_path)
    if not t3_path.exists():
        sys.exit(f"❌ Table3 file not found: {t3_path}")

    sample = t3_path.stem.replace("_table3", "")
    parent_ctx = "Disease" if ctx == "disease" else "Healthy"

    base_dir = FOLDERS["output"] / parent_ctx / "table3direct" / run_mode
    ensure_dir(base_dir)

    df = pd.read_csv(t3_path)

    if run_mode in ("interpret_graphviz", "interpret"):
        interp_prompt = PROMPTS[ctx]["INT"].format(table3=df.to_csv(index=False))
        interp_text = call_openai(interp_prompt, model=model)
        out_file = base_dir / f"{sample}_Prompt3_output.txt"
        out_file.write_text(interp_text, encoding="utf-8")
        print(f"✅ Prompt 3 saved: {out_file}")

    if run_mode in ("interpret_graphviz", "graphviz"):
        graph_highlight_all(sample, t3_path, base_dir)
        print(f"✅ Graphviz highlights saved in: {base_dir}")

# ─────────────────── CLI ───────────────────────────────────────────────────
def parse_cli():
    p = argparse.ArgumentParser("BioShiftPipeline")
    p.add_argument("--context", choices=["disease", "healthy"], required=True)

    p.add_argument("--mode", choices=[
        # Table3 direct
        "table3_direct",
        # Observed flows
        "shift_only", "full_with_graphviz", "full_no_graphviz",
        "interpret_only", "interpret_and_graphviz", "graphviz_only",
    ], required=True)

    # Table3-direct options
    p.add_argument("--run", choices=["interpret", "graphviz", "interpret_graphviz"],
                   default="interpret_graphviz",
                   help="What to run in table3_direct mode")
    p.add_argument("--table3", help="Path to a Table3 CSV (required for table3_direct)")

    # Observed options
    p.add_argument("--sample", help="Process single CSV stem (no .csv); else process ALL in observed/")
    p.add_argument("--observed_dir", help="Override path to observed CSV folder")

    # Optional: override the configured model for this run
    p.add_argument("--model", help="Model override (e.g., gpt-4o-mini, gpt-4o, gpt-4.1)")

    return p.parse_args()

# ─────────────────── MAIN ──────────────────────────────────────────────────
if __name__ == "__main__":
    args = parse_cli()

    # If the user overrides observed folder, apply it
    if args.observed_dir:
        FOLDERS["observed"] = Path(args.observed_dir)

    # Optional per-run model override
    run_model = args.model or DEFAULT_MODEL

    if args.mode == "table3_direct":
        if not args.table3:
            sys.exit("❌ --table3 path required for table3_direct mode.")
        run_table3_direct(args.table3, args.context, args.run, model=run_model)
        print("\n✅ Table3-direct pipeline finished.")
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
            run_shift_only(stem, args.context, out_base, model=run_model)

        elif args.mode == "full_with_graphviz":
            run_full(stem, args.context, with_graph=True, out_base=out_base, model=run_model)

        elif args.mode == "full_no_graphviz":
            run_full(stem, args.context, with_graph=False, out_base=out_base, model=run_model)

        elif args.mode == "interpret_only":
            run_interpret(stem, args.context, dot_required=False, out_base=out_base, model=run_model, table3_path=args.table3)

        elif args.mode == "interpret_and_graphviz":
            run_interpret(stem, args.context, dot_required=True, out_base=out_base, model=run_model, table3_path=args.table3)

        elif args.mode == "graphviz_only":
            t3_path = Path(args.table3) if args.table3 else (out_base / "tables" / f"{stem}_table3.csv")
            if t3_path.exists():
                graph_highlight_all(stem, t3_path, out_base / "graphviz")
            else:
                print(f"⚠️  Skipping graphviz_only for {stem}; missing {t3_path}")

        else:
            print(f"⚠️ Unknown mode {args.mode}")

    print("\n✅ Pipeline finished for all samples.")
