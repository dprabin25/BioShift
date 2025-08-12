# -*- coding: utf-8 -*-
"""
Created on Sat Aug  2 13:29:30 2025
@author: prabi
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
import openai
import pandas as pd
from io import StringIO

# ─────────────────── CONFIG ────────────────────────────────────────────────
INPUTS = Path("inputs")
FOLDERS = {
    "observed": INPUTS / "observed",
    "graphviz": INPUTS / "graphviz",
    "output": Path("outputs"),
}

# API key once via environment (Windows CMD:  setx OPENAI_API_KEY "your_api_key_here")
openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    sys.exit(
        "❌ OPENAI_API_KEY not set.\n"
        "Windows (CMD):  setx OPENAI_API_KEY \"your_api_key_here\"  then reopen terminal.\n"
        "Linux/macOS:    export OPENAI_API_KEY=\"your_api_key_here\"\n"
    )

# ─────────────────── PROMPTS (place-holders) ───────────────────────────────
PROMPT_D1 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
{elements}

Reporting Instructions:
Create a summary table “A” of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.
The table should contain columns “Element” and “GPT shift 1”. Also make sure each element should have its own row. 
( Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_D2 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
{elements}

Reporting Instructions:
Create a summary table “B” of your findings. Include columns “Element”, “GPT shift 2”, “Biological Group”, “Group ID based on Biological Group”, “Notes (if any”)...
( Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_D3 = """These are groups of jointly shifted microbiomes, immune cells, and/or proteins observed in gum disease that you are interested in. What is the biological interpretation?

{table3}

Provide your analysis in a clear, structured format."""

PROMPT_H1 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
{elements}

Reporting Instructions:
Create a summary table “P” of your findings. Include only the items with established associations. The table should contain columns “Element” and “GPT shift 1”...
( Always use numbers in output as increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_H2 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
{elements}

Reporting Instructions:
Create a summary table “Q” of your findings. Include columns “Element”, “GPT shift 2”, “Biological Group”, “Group ID based on Biological Group”, “Notes (if any”)...
( Always use numbers in output as increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.) 
"""

PROMPT_H3 = """These are lists of groups of jointly shifted microbiomes, cells, and/or proteins in recovery from healthy gum that you are interested in. What is the biological interpretation?

{table3}

Provide your analysis in a clear, structured format."""

PROMPTS = {
    "disease": {"A": PROMPT_D1, "B": PROMPT_D2, "INT": PROMPT_D3},
    "healthy": {"A": PROMPT_H1, "B": PROMPT_H2, "INT": PROMPT_H3},
}

# ─────────────────── UTILITIES ─────────────────────────────────────────────

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def call_openai(prompt: str, model: str = "gpt-4o-mini", temperature: float = 0.5):
    """Call OpenAI w/ retry."""
    for attempt in range(3):
        try:
            resp = openai.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=2048,
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"⚠️  OpenAI error ({attempt+1}/3): {e}")
            time.sleep(2)
    return ""

def extract_elements(observed_path: Path, elements_path: Path):
    df = pd.read_csv(observed_path)
    cols = [c for c in df.columns if c.lower().startswith("element")]
    if not cols:
        raise ValueError(f"No 'Element' column found in {observed_path}")
    elements = df[cols[0]].astype(str).str.strip().unique()
    ensure_dir(elements_path.parent)
    elements_path.write_text("\n".join(elements), encoding="utf-8")
    return elements, df

def run_prompt_set(elements, context: str, prompt_dir: Path, model: str):
    t = PROMPTS[context]
    outA = call_openai(t["A"].format(elements="\n".join(elements)), model=model)
    outB = call_openai(t["B"].format(elements="\n".join(elements)), model=model)
    ensure_dir(prompt_dir)
    (prompt_dir / "PromptA_output.txt").write_text(outA, encoding="utf-8")
    (prompt_dir / "PromptB_output.txt").write_text(outB, encoding="utf-8")
    return outA, outB

# ── Robust parsing helpers ─────────────────────────────────────────────────

def _sanitize_table_text(txt: str) -> str:
    """Remove code fences and keep only pipe-table lines."""
    if not isinstance(txt, str):
        return ""
    # Remove starting/ending code fences if present
    txt = re.sub(r"^```[\w-]*\s*", "", txt.strip())
    txt = re.sub(r"\s*```$", "", txt.strip())
    # Keep lines with pipes; drop pure dash separators
    lines = []
    for ln in txt.splitlines():
        if "|" not in ln:
            continue
        if re.fullmatch(r"\s*\|?\s*-{2,}.*", ln):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()

def _coerce_element_column(df: pd.DataFrame, label_hint: str) -> pd.DataFrame:
    """Ensure there is an 'Element' column; rename a close match or the first real column."""
    if "Element" in df.columns:
        return df
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    if "element" in lower_map:
        return df.rename(columns={lower_map["element"]: "Element"})
    candidates = [c for c in df.columns if c and not str(c).strip().lower().startswith("unnamed")]
    if candidates:
        return df.rename(columns={candidates[0]: "Element"})
    raise KeyError(f"Element column missing in {label_hint}. Columns found: {list(df.columns)}")

def clean_and_save_table_ab(promptA, promptB, out_csv):
    """
    Merge PromptA and PromptB, keep all elements, clean whitespace and NaNs, remove Unnamed columns.
    Save to out_csv and return DataFrame.
    """
    # Sanitize raw model text
    promptA_s = _sanitize_table_text(promptA)
    promptB_s = _sanitize_table_text(promptB)

    # Parse PromptA and PromptB
    a = pd.read_csv(StringIO(promptA_s), sep="|", engine="python", skipinitialspace=True, comment="#")
    b = pd.read_csv(StringIO(promptB_s), sep="|", engine="python", skipinitialspace=True, comment="#")

    # Drop blank columns (from leading/trailing pipes)
    a = a.loc[:, [col for col in a.columns if str(col).strip()]]
    b = b.loc[:, [col for col in b.columns if str(col).strip()]]

    # Remove any "Unnamed" columns
    a = a.loc[:, ~a.columns.str.contains("^Unnamed", case=False, na=False)]
    b = b.loc[:, ~b.columns.str.contains("^Unnamed", case=False, na=False)]

    # Strip headers and values
    a.columns = a.columns.astype(str).str.strip()
    b.columns = b.columns.astype(str).str.strip()
    for df in [a, b]:
        for col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace({"nan": "", "NaN": ""})

    # Ensure "Element" exists
    a = _coerce_element_column(a, "PromptA")
    b = _coerce_element_column(b, "PromptB")

    # Drop junk rows
    drop_patterns = [r"^[-\s]*$", r"^element$", r"^$"]
    pattern = "|".join(drop_patterns)
    a = a[~a["Element"].astype(str).str.strip().str.lower().str.match(pattern, na=False)]
    b = b[~b["Element"].astype(str).str.strip().str.lower().str.match(pattern, na=False)]

    # Outer merge (all rows)
    table_ab = pd.merge(a, b, on="Element", how="outer", suffixes=('_A', '_B'))

    # Column order: Element, A..., B...
    a_cols = [col for col in a.columns if col != "Element"]
    b_cols = [col for col in b.columns if col != "Element"]
    col_order = ["Element"] + [c for c in a_cols if c in table_ab.columns] + [c for c in b_cols if c in table_ab.columns]
    table_ab = table_ab[col_order]

    # Final cleanup
    table_ab = table_ab.loc[:, ~table_ab.columns.str.contains("^Unnamed", case=False, na=False)]
    for col in table_ab.select_dtypes(include="object").columns:
        table_ab[col] = table_ab[col].replace({"nan": "", "NaN": ""}).fillna("").astype(str).str.strip()
    table_ab = table_ab.fillna("")
    table_ab = table_ab.sort_values("Element", kind="stable").reset_index(drop=True)

    table_ab.to_csv(out_csv, index=False)
    print(f"✅ Clean TableAB saved as: {out_csv} | shape: {table_ab.shape}")
    print("🔎 TableAB head:\n", table_ab.head())
    return table_ab

def make_merged_table(stem, out_base, outA, outB, obs_df):
    """
    Clean merge of PromptA and PromptB (TableAB), then with observed data (Table1).
    Always saves TableAB.csv and Table1.csv in output/tables.
    Returns: (table1 DataFrame, tables_dir)
    """
    tables_dir = out_base / "tables"
    ensure_dir(tables_dir)
    tab_ab_file = tables_dir / f"{stem}_tableAB.csv"
    table_ab = clean_and_save_table_ab(outA, outB, tab_ab_file)
    # Merge TableAB with observed (left join)
    obs_df["Element"] = obs_df["Element"].astype(str).str.strip()
    table1 = pd.merge(table_ab, obs_df, on="Element", how="left")
    # Logical column order: Element, shifts, group, then observed columns
    obs_cols = [c for c in obs_df.columns if c != "Element"]
    out_cols = [c for c in table1.columns if c not in obs_cols]
    col_order = ["Element"] + [c for c in out_cols if c != "Element"] + obs_cols
    table1 = table1[col_order]
    table1 = table1.sort_values("Element", kind="stable").reset_index(drop=True)
    for col in table1.select_dtypes(include="object").columns:
        table1[col] = table1[col].str.strip().replace("nan", "")
    table1.fillna("", inplace=True)
    tab1_file = tables_dir / f"{stem}_table1.csv"
    table1.to_csv(tab1_file, index=False)
    print(f"✅ Table1 saved (TableAB + observed): {tab1_file} | shape: {table1.shape}")
    print("🔎 Table1 head:\n", table1.head())
    return table1, tables_dir

def build_table2_3(
    sample: str,
    context: str,
    table1: pd.DataFrame,
    tables_dir: Path,
    prompt_dir: Path,
    model: str = "gpt-4o-mini",
):
    """
    Builds Table2 from Table1, then Table3 from Table2.
    """
    req_cols = ["Element", "Observed Shift", "GPT shift 2", "Biological Group"]
    if not all(col in table1.columns for col in req_cols):
        print("⚠️  Missing columns for Table2 & Table3. Skipping.")
        return None
    # ---------- Table 2 ----------
    t2 = table1[req_cols].copy()
    t2.rename(columns={"GPT shift 2": "Expected Shift"}, inplace=True)
    # Clean up
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
    print("🔎 Table2 head:\n", t2.head())
    # ---------- Table 3 ----------
    mask = (
        (t2["Observed Shift"] == t2["Expected Shift"]) &
        (t2.groupby("Biological Group")["Biological Group"].transform("size") > 1)
    )
    t3 = t2[mask].reset_index(drop=True)
    t3_path = tables_dir / f"{sample}_table3.csv"
    t3.to_csv(t3_path, index=False)
    print(f"✅ Table3 saved: {t3_path} | shape: {t3.shape}")
    print("🔎 Table3 head:\n", t3.head())
    # ---------- Interpretation prompt ----------
    interp_prompt = PROMPTS[context]["INT"].format(table3=t3.to_csv(index=False))
    interp = call_openai(interp_prompt, model=model)
    (prompt_dir / f"{sample}_Prompt3_output.txt").write_text(interp, encoding="utf-8")
    return t3_path

# ─────────────────── GRAPHVIZ  (JPEG-only; use all files) ──────────────────

def _list_graph_sources() -> list[Path]:
    """Return all .dot and .txt in inputs/graphviz (sorted)."""
    base = FOLDERS["graphviz"]
    files = sorted(list(base.glob("*.dot")) + list(base.glob("*.txt")))
    return files

def _render_highlighted(dot_src: Path, df: pd.DataFrame, jpg_out: Path):
    df = df.copy()
    df["Element"] = df["Element"].astype(str).str.strip()
    df["Observed Shift"] = df["Observed Shift"].astype(str).str.strip()
    colors = {"1": "green", "-1": "blue"}

    with dot_src.open(encoding="utf-8") as f:
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
    """Highlight using the sample's Table3 across ALL .dot/.txt in inputs/graphviz."""
    sources = _list_graph_sources()
    if not sources:
        print(f"⚠️  No DOT/TXT files found in {FOLDERS['graphviz']}; graphviz skipped.")
        return
    df = pd.read_csv(t3_path)
    for src in sources:
        tag = src.stem  # graph name
        jpg_out = out_graph_dir / f"{sample}_{tag}_highlighted.jpg"
        _render_highlighted(src, df, jpg_out)

# ─────────────────── MODE EXECUTION ────────────────────────────────────────

def run_shift_only(stem, ctx, out_base, model):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, out_base / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, out_base / "prompts", model=model)
    make_merged_table(stem, out_base, outA, outB, obs_df)
    # No further tables built in shift_only mode

def run_full(stem, ctx, with_graph, out_base, model):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, out_base / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, out_base / "prompts", model=model)
    merged, tables_dir = make_merged_table(stem, out_base, outA, outB, obs_df)
    t3_path = build_table2_3(stem, ctx, merged, tables_dir, out_base / "prompts", model=model)
    if with_graph and t3_path is not None:
        graph_highlight_all(stem, t3_path, out_base / "graphviz")

def run_interpret(stem, ctx, dot_required, out_base, model):
    default_t3 = out_base / "tables" / f"{stem}_table3.csv"
    t3_path = Path(args.table3) if args.table3 else default_t3
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

# ─────────────────── CLI ───────────────────────────────────────────────────

def parse_cli():
    p = argparse.ArgumentParser("Batch GPT + Graphviz")
    p.add_argument("--context", choices=["disease", "healthy"], required=True)
    p.add_argument("--mode", choices=[
        "shift_only",
        "full_with_graphviz",
        "full_no_graphviz",
        "interpret_only",
        "interpret_and_graphviz",
        "graphviz_only",
    ], required=True)
    p.add_argument("--sample", help="Process single CSV stem (no .csv)")
    p.add_argument("--table3", help="Custom Table3 path (interpret modes)")
    p.add_argument("--model", default="gpt-4o-mini",
                   help="Model to use (e.g., gpt-4o-mini, gpt-4o, gpt-4.1-mini, gpt-3.5-turbo)")
    return p.parse_args()

# ─────────────────── MAIN ──────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_cli()
    # gather sample names ---------------------------------------------------
    csv_stems = [p.stem for p in FOLDERS["observed"].glob("*.csv")]
    if args.sample:
        if args.sample not in csv_stems:
            sys.exit(f"❌ Sample '{args.sample}' not found in inputs/observed/")
        csv_stems = [args.sample]
    if not csv_stems:
        sys.exit("❌ No CSV files in inputs/observed/")
    for stem in csv_stems:
        parent_ctx = "Disease" if args.context == "disease" else "Healthy"
        out_base = FOLDERS["output"] / parent_ctx / stem
        ensure_dir(out_base)
        print(f"\n🔄 Processing {stem}.csv as {args.context} → {out_base}")
        if args.mode == "shift_only":
            run_shift_only(stem, args.context, out_base, model=args.model)
        elif args.mode == "full_with_graphviz":
            run_full(stem, args.context, with_graph=True, out_base=out_base, model=args.model)
        elif args.mode == "full_no_graphviz":
            run_full(stem, args.context, with_graph=False, out_base=out_base, model=args.model)
        elif args.mode == "interpret_only":
            run_interpret(stem, args.context, dot_required=False, out_base=out_base, model=args.model)
        elif args.mode == "interpret_and_graphviz":
            run_interpret(stem, args.context, dot_required=True, out_base=out_base, model=args.model)
        elif args.mode == "graphviz_only":
            t3_path = Path(args.table3) if args.table3 else (
                out_base / "tables" / f"{stem}_table3.csv"
            )
            if not t3_path.exists():
                print(f"❌ Table3 not found for {stem}: {t3_path}")
            else:
                graph_highlight_all(stem, t3_path, out_base / "graphviz")
        else:
            print(f"⚠️  Unknown mode {args.mode}")
    print("\n✅ Pipeline finished for all samples.")
