# BioShift

## Description

BioShift produces biological interpretations of observed shifts in biological elements (cytokines, cells, microbes) using PubMed literature evidence, curated knowledge bases (ImmuneXpresso, UniProt), and LLM-guided analysis. See Dawadi et al. (ref. 1) for details. It is written in Python. You are free to download, modify, and expand this code under a permissive license similar to the BSD 2-Clause License (see below).

## Dependencies

### 1. Anaconda

Please install Anaconda: https://www.anaconda.com/distribution/

Open the Anaconda terminal and create a conda environment for BioShift:

```
conda create -n bioshift python=3.12 -y
conda activate bioshift
```

Note: We tested with Python 3.12.1 and 3.12.2.

### 2. Python packages

```
openai>=1.42.0,<2.0.0
pandas>=2.2.2,<3.0.0
numpy>=1.26.4,<3.0.0
```

You can install these with:

```
python3 -m pip install -r requirements.txt
```

(No other third-party packages are required — PubMed retrieval uses the Python standard library only.)

### 3. API key

Sign up for OpenAI: https://platform.openai.com/

**Note for new users:** Sign up, create an account, and generate an API key by providing an API Key Name and a Project Name when prompted. Copy the generated key and store it somewhere safe — you'll need it to access the API.

Once logged in, click your profile icon (top-right corner) → **Manage Account** → **Billing**.

In the Billing section, set up **Prepaid Billing** or **Auto Recharge**:
- Prepaid: manually add credit (e.g., $5, $10).
- Auto Recharge: automatically top up when your balance is low.

Check your usage: open **Usage** from the left-hand menu to monitor monthly spend and remaining balance. Pricing: https://openai.com/api/pricing/

Go to OpenAI API keys: https://platform.openai.com/api-keys → **Create new secret key** → copy the key (it looks like `sk-...`).

⚠ **Important:** Treat this key like a password — never share it or commit it to a public code repository.

### 4. Graphviz

The knowledge-graph output (`<sample>_Table3_KnowledgeGraph.jpg`) is rendered by shelling out to the `dot` command-line tool, so `dot` needs to be on your `PATH`. Install it through conda:

```
conda install anaconda::graphviz
```

If that doesn't put `dot` on your `PATH`, install Graphviz for your OS directly (e.g. `apt install graphviz`, `brew install graphviz`, or the Windows installer from graphviz.org) and confirm it worked with `dot -V`.

### 5. (Optional) NCBI API key

PubMed's E-utilities work without a key, but registering a free one raises your rate limit. Get one at https://www.ncbi.nlm.nih.gov/account/ and add it to `config.txt` as `NCBI_API_KEY` if you plan to run many samples back-to-back.

## How to prepare input files

### 1. Observed shift (required)

List each biological element with its shift direction. Increased shifts are `1`, decreased shifts are `-1`. Columns are comma-separated. Save the file as `ObservedShift/<sample>.csv` (the file's base name, without `.csv`, is the `<sample>` you pass on the command line).

| Element | Observed Shift |
|---|---|
| IL-1B | 1 |
| Mononuclear phagocytes | -1 |
| Th17 | 1 |

### 2. Config file (required)

Edit `config.txt` for your API key, model choice, and run parameters.

```
KEY=["WRITE YOUR KEY HERE"]
DEFAULT_MODEL=gpt-4o-mini
TEMPERATURE=0.5
MAX_TOKENS=2000
```

We originally used OpenAI, but you can adjust these values to work with any AI tool that exposes a comparable chat-completion API.

Optional settings you can also set in `config.txt`:

| Key | Default | Controls |
|---|---|---|
| `TOP_P` | 1.0 | Sampling nucleus for the LLM calls |
| `SEED` | (none) | Fixed sampling seed, if your model supports it |
| `COSHIFT_MODEL` | same as `DEFAULT_MODEL` | Model used for the co-shift (Table 2/3) prompts |
| `COSHIFT_MAX_TOKENS` | same as `MAX_TOKENS` | Max output tokens for the co-shift prompts |
| `KNOWLEDGE_BASE` | On | Set to `Off` to skip ImmuneXpresso/UniProt lookups in Table 2/3 |
| `SAMPLE_MODEL` | Human | `Human` or `Mouse` — used to match the ImmPort Cytokine Registry |
| `PUBMED_SEARCH_POOL_SIZE` | 1000 | Max abstracts pulled from PubMed's initial search, before ranking |
| `PUBMED_MAX_ABSTRACTS` | 1000 | How many of the ranked abstracts are actually sent to the LLM for extraction |
| `PUBMED_EXTRACTION_RUNS` | 5 | Independent LLM passes per batch of abstracts (majority vote decides the final direction) |
| `MAX_CONCURRENT_LLM_CALLS` | 5 | Concurrent LLM calls during Prompt 1 (literature) extraction |
| `COSHIFT_MAX_CONCURRENT_LLM_CALLS` | 2 | Concurrent LLM calls during Prompt 2 (co-shift) extraction |
| `PUBMED_USE_CACHE` | false | Reuse a previously cached PubMed fetch instead of a fresh search |
| `NCBI_API_KEY` | (none) | Optional NCBI key — raises the PubMed E-utilities rate limit |

Also fill in the study-context fields, which are carried alongside every table as metadata (they describe your dataset but never filter or alter evidence): `DISEASE_NAME`, `DISEASE_STAGE`, `TISSUE_SITE`, `HOST_SPECIES`, `EXPERIMENTAL_MODALITY`, `TAXONOMIC_RESOLUTION`, `BASELINE_GROUP`, `TARGET_GROUP`.

### 3. Knowledge-base reference files (required for `--mode P12` / `P123`)

Place the following curated reference files in a `Database/` folder next to the script:

| File | Source |
|---|---|
| `ImmuneXpressoResults_Interactions.csv` | ImmuneXpresso cell↔cytokine interaction records |
| `ImmPort_CytokineRegistry.November_2015.xls` | ImmPort Cytokine Registry (Human/Mouse symbol lookup) |

UniProt is queried live over its own API (results are cached locally) rather than from a static file, so no separate UniProt download is needed.

> Note: earlier versions of this pipeline also used MASI and MiMeDB as knowledge-base sources and read a curated Graphviz "immune pathway" template from `inputs/graphviz/`. Both have been removed — ImmuneXpresso and UniProt are the only two knowledge-base sources now, and the knowledge graph (`<sample>_Table3_KnowledgeGraph.jpg`) is built automatically from that sample's own Table 3, not from a hand-curated pathway file.

## How to use

BioShift runs one sample at a time, reading `ObservedShift/<sample>.csv`:

```
python BioShiftUpdated.py --sample <sample> --context disease --mode <mode>
```

For example, to run the full pipeline (literature evidence, co-shift/knowledge-base evidence, and biological interpretation) on `Testdata`:

```
python BioShiftUpdated.py --sample Testdata --context disease --mode P123
```

Note: for Linux and Mac users, use `python3` instead of `python`.

## Output files

A folder is created for each sample at `outputs/<sample>/`. All output files for that sample are saved directly there (no further subfolders):

| File | Produced by mode | Description |
|---|---|---|
| `<sample>_table1.csv` | P1, P12, P123 | Literature evidence per element: Up/Down/Mixed citations, abstracts screened, % support vs. the observed shift |
| `<sample>_table2.csv` | P12, P123 | Relationships between elements (co-shift + knowledge-base evidence) |
| `<sample>_table3.csv` | P12, P123 | Pairwise relationship detail behind Table 2, plus the rows the knowledge graph is drawn from |
| `<sample>_Table3_KnowledgeGraph.jpg` | P12, P123 | Graphviz figure built directly from that sample's Table 3 |
| `<sample>_Prompt3_output.txt` | P123 | Narrative biological interpretation, grouped by mechanism and checked against Table 1/2/3 |
| `<sample>_log.txt` | always | Full timestamped run log, including the per-model cost summary for that run |

## Options for `--mode`

`--mode` is cumulative — each mode runs everything the mode before it does, plus one more stage:

| Mode | Runs | Produces |
|---|---|---|
| `P1` (default) | Prompt 1 (literature evidence) | Table 1 |
| `P12` | Prompt 1, then Prompt 2 (co-shift + knowledge base) | Table 1, Table 2, Table 3, knowledge graph |
| `P123` | Prompt 1, Prompt 2, then Prompt 3 (interpretation) | Table 1, Table 2, Table 3, knowledge graph, biological interpretation |

## Reference

[1] Prabin Dawadi, Josh Gililland, Sayaka Miura, and Flavia Teles, *BioShift: Prompt-Guided Workflow for Interpreting Immune–Microbiome Shifts.* (2025) Under Review

Copyright 2025, Authors and University of Mississippi

BSD 3-Clause "New" or "Revised" License, which is a permissive license similar to the BSD 2-Clause License except that it prohibits others from using the name of the project or its contributors to promote derived products without written consent. Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
- Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
- Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
