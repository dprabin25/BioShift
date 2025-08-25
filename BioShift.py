# -*- coding: utf-8 -*-
"""
Batch GPT + Graphviz (single-file, config.txt)
Created: 2025-08-15

Features
- Reads KEY, DEFAULT_MODEL, TEMPERATURE, MAX_TOKENS from config.txt
  (KEY may be empty -> fallback to env var OPENAI_API_KEY)
- Modes:
  Table3 flows:
    • table3_direct   (with optional --table3; if omitted, scans inputs/table3/)
    • table3_batch    (process all *.csv under inputs/table3/)
  Observed flows:
    • shift_only
    • full_with_graphviz
    • full_no_graphviz
    • interpret_only
    • interpret_and_graphviz
    • graphviz_only
    • prompt_co
- Optional --observed_dir to override observed folder
- Output layout auto-created:
    inputs/
      observed/*.csv
      graphviz/*.dot (or .txt)
      table3/*.csv
    outputs/

Requirements
- Python 3.9+
- pip install openai pandas
- Graphviz installed & 'dot' available on PATH for highlighting output images

Notes
- Works with BOTH openai>=1.x (OpenAI client) and older openai<=0.x libraries.
- Observed CSVs must have a column starting with 'Element' (e.g. 'Element').
- To build Table2/3, the observed CSV must have 'Observed Shift'.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile

# ─────────────────── Third-party imports (with friendly error) ─────────────
try:
    import pandas as pd
except Exception:
    sys.exit("❌ The 'pandas' package is required. Install with: pip install pandas")

try:
    import openai  # We support both OpenAI 1.x and legacy 0.x
except Exception:
    sys.exit("❌ The 'openai' package is required. Install with: pip install openai")


# ─────────────────── Config (single config.txt beside this .py) ────────────
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
        sys.exit("❌ No API key found. Put KEY=... in config.txt (same folder) "
                 "or set the OPENAI_API_KEY environment variable.")
    return key

def load_gpt_options() -> dict:
    kv = _parse_simple_kv(CONFIG_TXT)
    default_model = (kv.get("DEFAULT_MODEL") or "").strip()
    if not default_model:
        sys.exit("❌ DEFAULT_MODEL not set in config.txt.")
    try:
        temperature = float(kv.get("TEMPERATURE", "0.2"))
    except ValueError:
        temperature = 0.2
    try:
        max_tokens = int(kv.get("MAX_TOKENS", "2000"))
    except ValueError:
        max_tokens = 2000
    return {
        "default_model": default_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

API_KEY = load_api_key()
GPT_CFG = load_gpt_options()
DEFAULT_MODEL = GPT_CFG["default_model"]
TEMPERATURE = GPT_CFG["temperature"]
MAX_TOKENS = GPT_CFG["max_tokens"]

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
INPUTS = HERE / "inputs"
FOLDERS = {
    "observed": INPUTS / "observed",
    "graphviz": INPUTS / "graphviz",
    "table3":   INPUTS / "table3",
    "output":   HERE / "outputs",
}
for p in FOLDERS.values():
    p.mkdir(parents=True, exist_ok=True)


# ─────────────────── Prompts ───────────────────────────────────────────────
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

USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.
"""

PROMPT_D2 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Input Elements:
A plain text file has been uploaded containing a list of microbial organisms, immune cells, and cytokines. These are elements I would like to investigate to determine whether any of them are commonly reported to shift together in gum disease.
{elements}

Analysis Instructions:
For each item in the input list, identify any groups or pairs of elements that are commonly reported to shift jointly in gum disease. If there is no established evidence of joint shifts, do not report any.
For each identified group or pair, determine whether each element is typically reported to increase, decrease, or show mixed patterns (i.e., both increased and decreased in different studies). The direction of the shift may differ among elements within a group. If the direction of change is unknown or unclear, indicate this.

Reporting Instructions:
Create a summary table of your findings. Include only those groups or pairs with established evidence of joint shifts.

(Summary table 'B', Include columns “Element”, “GPT shift 2”, “Biological Group”, "Group ID based on Biological Group", “Notes (if any)”)
"Biological Group" should be based on joint functional and mechanistic activity of grouped elements and completely without generic labels.
“Group ID based on Biological Group” should have same group ID based on "Biological Group". Every element should be categorized on shared biological activities.
Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.)

USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.
"""

PROMPT_D3 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Analysis and Reporting Instructions:

These are groups of jointly shifted microbiomes, immune cells, and/or proteins observed in gum disease that you are interested in. What is the biological interpretation? If well-established pathways of immune–microbiome interaction or immune regulation are involved, describe them and integrate them into the interpretation. Be sure to consider the direction of the shifts.

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

Reporting Instructions:
Create a summary table of your findings. Include only the items with established associations. For each item, report the observed direction of change (increase, decrease, mixed, or unknown). Exclude items with no prior evidence of association from the table.
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
For each identified group or pair, determine whether each element is typically reported to increase, decrease, or show mixed patterns. If the direction is unknown or unclear, indicate this as well.

Reporting Instructions:
(Summary table 'B', Include columns “Element”, “GPT shift 2”, “Biological Group”, "Group ID based on Biological Group", “Notes (if any)”)
"Biological Group" should be based on joint functional and mechanistic activity of grouped elements and completely without generic labels.
“Group ID based on Biological Group” should have same group ID based on "Biological Group". Every element should be categorized on shared biological activities.
Always use numbers in real output if increase = 1, decrease = -1, Others = 0, Information not available = X, present the table using "|" (pipe) as the column separator, and ensure there are no extra spaces.)

USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.
"""

PROMPT_H3 = """AI Role:
You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis.

Analysis and Reporting Instructions:
These are lists of groups of jointly shifted microbiomes, cells, and/or proteins in recovery / healthy gum that you are interested in ((1 represents increase and -1 represents decrease). What is the biological interpretation? If well-established pathways of immune–microbiome interaction or immune pathways are involved, describe them and integrate them into the interpretation. Be aware of the direction of shifts.

{table3}

Provide your analysis in a clear, structured format.
USE SAME NAME OF INPUT ELEMENTS FOR WHOLE PART.
"""

PROMPT_CO = """ AI Role: You are a professor with the highest academic standards, possessing expert knowledge in immunology, microbiology, and the pathophysiology of periodontitis. 
 
 Input Elements: An input csv file has been uploaded containing a list of microbial organisms, immune cells, cytokines, and/or other biological element. These are elements I would like to investigate to determine whether any are commonly reported to shift together. In the uploaded file, the first column lists the biological element and the second column lists the observed direction of the shift.
 {csv_data}
Analysis and Reporting Instructions:
For each item in the input list (1 represents increase and -1 represents decrease), identify any groups or pairs of elements that are commonly reported to shift jointly. What is the biological interpretation? If well-established pathways of immune–microbiome interaction or immune pathways are involved, describe them and integrate them into the biological interpretation. The expected directions of the shifts need to agree with the observed ones, and 
the biological interpretation should always include this information, i.e., observed and expected direction of the shifts.
"""

PROMPTS = {
    "disease": {"A": PROMPT_D1, "B": PROMPT_D2, "INT": PROMPT_D3},
    "healthy": {"A": PROMPT_H1, "B": PROMPT_H2, "INT": PROMPT_H3},
}


# ─────────────────── Utilities ─────────────────────────────────────────────
def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def call_openai(prompt: str) -> str:
    """
    Fireproof OpenAI call:
    - Try new client (openai>=1.x) first
    - Fallback to legacy openai.ChatCompletion (<=0.x)
    - Retry 3 times with small backoff
    """
    last_err = None
    for attempt in range(1, 4):
        try:
            if _OPENAI_NEW_CLIENT is not None:
                resp = _OPENAI_NEW_CLIENT.chat.completions.create(
                    model=DEFAULT_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=TEMPERATURE,
                    max_tokens=MAX_TOKENS,
                )
                text = resp.choices[0].message.content or ""
                return text.strip()
            else:
                resp = openai.ChatCompletion.create(
                    model=DEFAULT_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=TEMPERATURE,
                    max_tokens=MAX_TOKENS,
                )
                text = resp["choices"][0]["message"]["content"] or ""
                return text.strip()
        except Exception as e:
            print(f"⚠️  OpenAI error ({attempt}/3): {e}")
            last_err = e
            time.sleep(2 * attempt)
    print("⚠️  Returning empty string after repeated OpenAI failures.")
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

def extract_elements(observed_path: Path, elements_path: Path):
    df = pd.read_csv(observed_path)
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
    ensure_dir(elements_path.parent)
    Path(elements_path).write_text("\n".join([e for e in elements if e]), encoding="utf-8")
    return elements, df

def _read_pipe_table_or_empty(text: str, expected_cols_min=2) -> pd.DataFrame:
    """
    Read a pipe table safely; return empty DataFrame on failure.
    """
    cleaned = _extract_clean_table(text, min_cols=expected_cols_min)
    if not cleaned.strip():
        return pd.DataFrame()
    try:
        df = pd.read_csv(
            StringIO(cleaned),
            sep="|",
            engine="python",
            skipinitialspace=True
        )
        # Drop autogenerated columns and trim
        df = df.loc[:, ~df.columns.astype(str).str.contains("^Unnamed")]
        df.columns = df.columns.map(lambda c: str(c).strip())
        for col in df.columns:
            df[col] = df[col].astype(str).map(lambda x: x.strip()).replace("nan", "")
        return df
    except Exception as e:
        print(f"⚠️  Could not parse LLM table: {e}")
        return pd.DataFrame()

def clean_and_save_table_ab(promptA: str, promptB: str, out_csv: Path) -> pd.DataFrame:
    a = _read_pipe_table_or_empty(promptA, expected_cols_min=2)
    b = _read_pipe_table_or_empty(promptB, expected_cols_min=2)

    # Ensure required columns exist (best-effort)
    if "Element" not in a.columns:
        # Try to infer case-insensitive header
        cand = [c for c in a.columns if c.strip().lower() == "element"]
        if cand:
            a.rename(columns={cand[0]: "Element"}, inplace=True)
    if "Element" not in b.columns:
        cand = [c for c in b.columns if c.strip().lower() == "element"]
        if cand:
            b.rename(columns={cand[0]: "Element"}, inplace=True)

    # Drop rows with empty/invalid Element
    def _drop_bad(df):
        if "Element" not in df.columns:
            return df.iloc[0:0]  # empty
        drop_patterns = [r"^[-\s]*$", r"^element$", r"^$"]
        pattern = "|".join(drop_patterns)
        mask = ~df["Element"].astype(str).str.strip().str.lower().str.match(pattern)
        return df[mask].copy()

    a = _drop_bad(a)
    b = _drop_bad(b)

    # Outer merge
    if "Element" not in a.columns and "Element" not in b.columns:
        table_ab = pd.DataFrame(columns=["Element"])
    elif "Element" in a.columns and "Element" in b.columns:
        table_ab = pd.merge(a, b, on="Element", how="outer", suffixes=("_A", "_B"))
    elif "Element" in a.columns:
        table_ab = a.copy()
    else:
        table_ab = b.copy()

    # Order columns: Element first, then A's then B's
    a_cols = [c for c in a.columns if c != "Element"]
    b_cols = [c for c in b.columns if c != "Element"]
    col_order = ["Element"] + a_cols + b_cols
    table_ab = table_ab.reindex(columns=col_order, fill_value="")

    # Strip junk
    table_ab = table_ab.loc[:, ~table_ab.columns.astype(str).str.contains("^Unnamed")]
    for col in table_ab.columns:
        table_ab[col] = (
            table_ab[col]
            .astype(str)
            .map(lambda x: x.strip())
            .replace("nan", "")
        )

    table_ab = table_ab.fillna("").sort_values("Element", kind="stable").reset_index(drop=True)
    ensure_dir(Path(out_csv).parent)
    table_ab.to_csv(out_csv, index=False, encoding="utf-8")
    print(f"✅ Clean TableAB saved: {out_csv}")
    return table_ab

def run_prompt_co(csv_path: Path, out_base: Path):
    """Run Prompt_Co on a CSV file and save output to 'Prompt_Co_Output/<stem>_PromptCo_output.txt'."""
    if not csv_path.exists():
        print(f"❌ CSV not found: {csv_path}")
        return
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"❌ Failed to read {csv_path}: {e}")
        return

    csv_text = df.to_csv(index=False)
    prompt_text = PROMPT_CO.format(csv_data=csv_text)
    output_text = call_openai(prompt_text)

    out_dir = Path(out_base) / "Prompt_Co_Output"
    ensure_dir(out_dir)
    out_file = out_dir / f"{csv_path.stem}_PromptCo_output.txt"
    out_file.write_text(output_text, encoding="utf-8")
    print(f"✅ Prompt_Co output saved: {out_file}")


# ─────────────────── Graphviz highlighting ─────────────────────────────────
def graph_highlight(sample: str, t3_path: Path, out_graph_dir: Path):
    """
    Highlight matched elements in ALL Graphviz files under inputs/graphviz.
    Output files: <out_graph_dir>/<sample>_<graphfile_stem>_highlighted.jpg

    Colors:
      Observed Shift == "1"   -> green
      Observed Shift == "-1"  -> blue
      else -> leave node unchanged
    """
    try:
        df = pd.read_csv(t3_path)
    except Exception as e:
        print(f"❌ Failed to read Table3 at {t3_path}: {e}")
        return

    for col in ("Element", "Observed Shift"):
        if col not in df.columns:
            print(f"⚠️  Table3 missing '{col}'. Skipping highlight.")
            return

    df["Element"] = df["Element"].astype(str).map(lambda x: x.strip())
    # Normalize shift like "1.0" -> "1"
    df["Observed Shift"] = (
        df["Observed Shift"]
        .astype(str)
        .map(lambda x: re.sub(r"\.0+$", "", x.strip()))
    )

    ensure_dir(out_graph_dir)
    graph_files = list(FOLDERS["graphviz"].glob("*.dot")) + list(FOLDERS["graphviz"].glob("*.txt"))
    if not graph_files:
        print(f"⚠️ No Graphviz files found in {FOLDERS['graphviz']}. Put .dot/.txt files there.")
        return

    for graph_file in graph_files:
        try:
            text = graph_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as e:
            print(f"⚠️ Could not read {graph_file}: {e}")
            continue

        new_lines = []
        for ln in text:
            hit = False
            for el, s in zip(df["Element"], df["Observed Shift"]):
                # Match lines like  "NodeName" [ ... ]
                if re.match(rf'\s*"{re.escape(el)}"\s*\[', ln):
                    color = "green" if s == "1" else ("blue" if s == "-1" else None)
                    if color:
                        try:
                            attrs = ln.split("[", 1)[1].rsplit("]", 1)[0]
                            # Append (do not remove existing attrs)
                            nl = f'"{el}" [{attrs}, style=filled, fillcolor={color}]'
                            # Preserve trailing newline if any
                            if ln.endswith("\n"):
                                nl += "\n"
                            new_lines.append(nl)
                        except Exception:
                            new_lines.append(ln)  # fail-safe
                    else:
                        new_lines.append(ln)
                    hit = True
                    break
            if not hit:
                new_lines.append(ln)

        jpg_out = Path(out_graph_dir) / f"{sample}_{graph_file.stem}_highlighted.jpg"
        with NamedTemporaryFile("w", delete=False, suffix=".dot", encoding="utf-8") as tmp:
            tmp.write("\n".join(new_lines))
            tmp_path = Path(tmp.name)

        try:
            # Requires Graphviz 'dot' executable on PATH
            subprocess.run(["dot", "-Tjpg", str(tmp_path), "-o", str(jpg_out)],
                           check=True, capture_output=True)
            print(f"✅ Highlighted graph saved: {jpg_out}")
        except FileNotFoundError:
            print("❌ Graphviz 'dot' not found. Install Graphviz and ensure 'dot' is on PATH.")
            return
        except subprocess.CalledProcessError as e:
            print(f"❌ Graphviz 'dot' error for {graph_file}:\n{e.stderr.decode(errors='ignore')}")
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


# ─────────────────── Observed workflow helpers ─────────────────────────────
def run_prompt_set(elements, context: str, prompt_dir: Path):
    t = PROMPTS[context]
    outA = call_openai(t["A"].format(elements="\n".join(elements)))
    outB = call_openai(t["B"].format(elements="\n".join(elements)))
    ensure_dir(prompt_dir)
    (Path(prompt_dir) / "PromptA_output.txt").write_text(outA, encoding="utf-8")
    (Path(prompt_dir) / "PromptB_output.txt").write_text(outB, encoding="utf-8")
    return outA, outB

def make_merged_table(stem, out_base, outA, outB, obs_df):
    tables_dir = Path(out_base) / "tables"
    ensure_dir(tables_dir)

    tab_ab_file = tables_dir / f"{stem}_tableAB.csv"
    table_ab = clean_and_save_table_ab(outA, outB, tab_ab_file)

    # Merge Observed onto Element
    obs_df = obs_df.copy()
    obs_df.columns = [str(c).strip() for c in obs_df.columns]
    if "Element" not in obs_df.columns:
        # Locate first column starting with 'Element'
        cols = [c for c in obs_df.columns if c.lower().startswith("element")]
        if not cols:
            print("⚠️  Observed file has no 'Element' column. Skipping Table1 merge.")
            merged = table_ab
        else:
            obs_df.rename(columns={cols[0]: "Element"}, inplace=True)

    if "Element" in obs_df.columns:
        obs_df["Element"] = obs_df["Element"].astype(str).map(lambda x: x.strip())
        merged = pd.merge(table_ab, obs_df, on="Element", how="left")
        obs_cols = [c for c in obs_df.columns if c != "Element"]
        out_cols = [c for c in merged.columns if c not in obs_cols]
        col_order = ["Element"] + [c for c in out_cols if c != "Element"] + obs_cols
        merged = merged[col_order]
    else:
        merged = table_ab

    # Strip spaces/nans
    for col in merged.columns:
        if merged[col].dtype == "object":
            merged[col] = merged[col].astype(str).map(lambda x: x.strip()).replace("nan", "")
    merged.fillna("", inplace=True)

    tab1_file = tables_dir / f"{stem}_table1.csv"
    merged.to_csv(tab1_file, index=False, encoding="utf-8")
    print(f"✅ Table1 saved: {tab1_file}")
    return merged, tables_dir

def build_table2_3(sample, context, table1, tables_dir, prompt_dir):
    req_cols = ["Element", "Observed Shift", "GPT shift 2", "Biological Group"]
    missing = [c for c in req_cols if c not in table1.columns]
    if missing:
        print(f"⚠️  Missing columns for Table2/3: {missing}. Skipping.")
        return None

    t2 = table1[req_cols].copy()
    t2.rename(columns={"GPT shift 2": "Expected Shift"}, inplace=True)

    # Normalize shift values (e.g., "1.0" -> "1")
    for col in ["Observed Shift", "Expected Shift"]:
        t2[col] = (
            t2[col]
            .astype(str)
            .map(lambda x: re.sub(r"\.0+$", "", x.strip()))
            .replace({"nan": ""})
        )

    for col in ["Biological Group", "Element"]:
        t2[col] = t2[col].astype(str).map(lambda x: x.strip())

    t2 = t2.sort_values(["Biological Group", "Element"], kind="stable").reset_index(drop=True)
    t2.fillna("", inplace=True)

    t2_path = Path(tables_dir) / f"{sample}_table2.csv"
    t2.to_csv(t2_path, index=False, encoding="utf-8")
    print(f"✅ Table2 saved: {t2_path}")

    # Strict Table3 rule: only rows where Observed == Expected; groups must have >1 item
    grp_sizes = t2.groupby("Biological Group")["Biological Group"].transform("size")
    mask = (t2["Observed Shift"] == t2["Expected Shift"]) & (grp_sizes > 1)
    t3 = t2[mask].reset_index(drop=True)

    t3_path = Path(tables_dir) / f"{sample}_table3.csv"
    t3.to_csv(t3_path, index=False, encoding="utf-8")
    print(f"✅ Table3 saved: {t3_path}")

    # Interpret (Prompt 3)
    interp_prompt = PROMPTS[context]["INT"].format(table3=t3.to_csv(index=False))
    interp = call_openai(interp_prompt)
    ensure_dir(prompt_dir)
    (Path(prompt_dir) / f"{sample}_Prompt3_output.txt").write_text(interp, encoding="utf-8")
    print(f"✅ Prompt 3 saved: {Path(prompt_dir) / f'{sample}_Prompt3_output.txt'}")

    return t3_path

def run_shift_only(stem, ctx, out_base):
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, Path(out_base) / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, Path(out_base) / "prompts")
    make_merged_table(stem, out_base, outA, outB, obs_df)

def run_full(stem, ctx, with_graph, out_base):
    """
    Full mode ALWAYS runs:
      - Prompt A
      - Prompt B
      - TableAB → Table1 → Table2 → Table3
      - Prompt 3 (Interpretation)
      - Graphviz (if requested and Table3 exists)
      - Prompt_Co on the observed CSV
    """
    obs_path = FOLDERS["observed"] / f"{stem}.csv"
    elements, obs_df = extract_elements(obs_path, Path(out_base) / "elements" / f"{stem}_Elements.txt")
    outA, outB = run_prompt_set(elements, ctx, Path(out_base) / "prompts")
    merged, tables_dir = make_merged_table(stem, out_base, outA, outB, obs_df)
    t3_path = build_table2_3(stem, ctx, merged, tables_dir, Path(out_base) / "prompts")
    if with_graph and t3_path is not None:
        graph_highlight(stem, t3_path, Path(out_base) / "graphviz")
    # Always run Prompt_Co in full modes
    run_prompt_co(obs_path, out_base)

def run_interpret(stem, ctx, dot_required, out_base, table3_path=None):
    t3_path = Path(table3_path) if table3_path else Path(out_base) / "tables" / f"{stem}_table3.csv"
    if not t3_path.exists():
        print(f"⚠️  Missing Table3 for {stem}: {t3_path}")
        return
    try:
        df = pd.read_csv(t3_path)
    except Exception as e:
        print(f"❌ Could not read {t3_path}: {e}")
        return

    interp_prompt = PROMPTS[ctx]["INT"].format(table3=df.to_csv(index=False))
    interp = call_openai(interp_prompt)
    ensure_dir(Path(out_base) / "prompts")
    (Path(out_base) / "prompts" / f"{stem}_Prompt3_output.txt").write_text(interp, encoding="utf-8")
    print(f"✅ Prompt 3 saved: {Path(out_base) / 'prompts' / f'{stem}_Prompt3_output.txt'}")

    if dot_required:
        graph_highlight(stem, t3_path, Path(out_base) / "graphviz")


# ─────────────────── Table3 direct flows ───────────────────────────────────
def run_table3_direct(table3_path: Path, ctx: str, run_mode: str = "interpret_graphviz"):
    """
    Consume a single Table3 CSV and (optionally) run Prompt 3 and/or Graphviz.
    run_mode ∈ {"interpret", "graphviz", "interpret_graphviz"}.
    Outputs go to: outputs/<Disease|Healthy>/table3direct/<file_stem>/<run_mode>/
    """
    t3_path = Path(table3_path)
    if not t3_path.exists():
        sys.exit(f"❌ Table3 file not found: {t3_path}")

    sample = t3_path.stem  # preserve full stem
    parent_ctx = "Disease" if ctx == "disease" else "Healthy"

    base_dir = FOLDERS["output"] / parent_ctx / "table3direct" / sample / run_mode
    ensure_dir(base_dir)

    try:
        df = pd.read_csv(t3_path)
    except Exception as e:
        sys.exit(f"❌ Failed to read {t3_path}: {e}")

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
        # Table3 flows
        "table3_direct",
        "table3_batch",
        # Observed flows
        "shift_only", "full_with_graphviz", "full_no_graphviz",
        "interpret_only", "interpret_and_graphviz", "graphviz_only",
        # Prompt_Co only
        "prompt_co",
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
def main():
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
        return

    if args.mode == "table3_batch":
        run_table3_batch(args.context, args.run)
        return

    # Observed flows (including prompt_co and full_* running all prompts)
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
            t3_path = Path(out_base) / "tables" / f"{stem}_table3.csv"
            if t3_path.exists():
                graph_highlight(stem, t3_path, Path(out_base) / "graphviz")
            else:
                print(f"⚠️  Skipping graphviz_only for {stem}; missing {t3_path}")
        elif args.mode == "prompt_co":
            obs_path = FOLDERS["observed"] / f"{stem}.csv"
            run_prompt_co(obs_path, out_base)

    print("\n✅ Pipeline finished for all samples.")

if __name__ == "__main__":
    main()

