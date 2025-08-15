# BioShift
BioShift is a structured, LLM-powered framework that guides immune–microbiome analysis through an expert-like workflow. It begins by scanning data for known shifts and coordinated patterns to validate biological signals, then generates AI-driven interpretations, which users confirm through manual literature review to ensure scientific rigor. This approach not only produces reliable, interpretable results but also serves as a training tool for researchers, building knowledge of immune–microbiome interactions.

## Setting up Your API credits and getting API key
You may modify codes in BioShift.py based on your Large Language model.

We working with OpenAI Account and this way we can get API key:
1. Create an OpenAI account. https://platform.openai.com/
2. Once logged in, click your profile icon (top-right corner) → Manage Account → Billing.
3. In the Billing section, set up Prepaid Billing or Auto Recharge
   Prepaid: Manually add credit (e.g., $5, $10).
   Auto Recharge: Automatically top up when balance is low.
4. Check Your Usage
   Open Usage from the left-hand menu to monitor your monthly spend and remaining balance
   Link for pricing: https://openai.com/api/pricing/
5. Go to OpenAI API keys: https://platform.openai.com/api-keys
6. Click “Create new secret key” → Copy the key (it looks like sk-...).
   ⚠ Important: Treat this key like a password — never share it or commit it to public code repositories.

   
## Working directory with essential folders and files
Your working directory should contain BioShiftBioShift.py 
In your working directory, you can have input folders 
	1. Folder name "observed" containing csv files with Elements and Observed Shifts 
	2. Folder name "graphviz" containing .dot files 
	3. Table 3 if you want to get Prompt 3 output or Graphviz jpg figure
    4. "config" folder that contains API key in text and gpt options in .json format

# Requirements

Based on User's interest, 

Observed Shift File(s) (inputs/observed/) – Raw change data; supports full runs, shifts-only, shift+interpretation, interpretation and direct illustration.
Table 3 File(s) (inputs/Table3/) – Pre-processed data; used for interpretation or illustration without upstream processing.

project-root/
│
├── BioShift.py
├── inputs/
│   ├── observed/             # Your input CSVs with elements & observed shifts
│   ├── graphviz/             # Input Graphviz .dot files (for highlighting)
│   ├──  Table 3/             # Input Table 3 (According to need) CSVs (for interpretation/highlighting)
├── config.txt



Note:
You can find guide to install Graphviz : https://graphviz.org/download/ or could install Graphviz package. 

### Troublbleshooting for Windows Users
If you're Windows user and having issue:
	1. Manually graphviz-13.1.1 (64-bit) ZIP archive [sha256] from https://graphviz.org/download/ [This is what we used for this BioShifts]
	2. Extract the zip file and save it in a specific directory. [For e.g. C:/Graphviz] 
	3. From Search go to "Edit the system environmental variables"
	4. Click "Environmental variables"  
	4. Click on "Path"
	5. Add a "New" path where you Graphviz bin is located [For e.g.C:\Graphviz\Graphviz-13.1.1-win64\bin]
	6. Apply the changes

## Editing API_key and default AI model

You can edit "config.txt" by adding your own API key.
Replace "sk ............."

You can change "default_model" according to your need. Also you can change "temperature" and maximum number of tokens. 

We originally used OpenAI, but you can adjust the contents of these files to work with any AI tool you choose.


## FOR RUNNING BioShift.py

   

### Selection of pipelines based on input files

| **Input Type**                                                      | **Use these modes**                                                 |
| ------------------------------------------------------------------- | ------------------------------------------------------------------- |
| **Observed Data** (`inputs/observed/*.csv`)                         | `shift_only`, `full_with_graphviz`, `full_no_graphviz`, `prompt_co` |
| **Table 3 Direct (CSV)** (`inputs/table3/*.csv` or `--table3 PATH`) | `table3_direct`, `table3_batch`                                     |



### Running command in terminal: 

You can choose --context disease or healthy based on your need. 

1. Observed Data – Bulk (All files in inputs/observed/)

| Command                                                                                                     | What it does                                                                                                              |       | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------
| `python BioShift.py --context disease --mode shift_only`                                            |**Prompt A & B only** on **all** observed CSVs (builds `tableAB`/`table1`; **no** Table2/3, INT, Graphviz, or Prompt\_Co). |
| `python BioShift.py --context healthy --mode full_with_graphviz`                                    |**full pipeline** (A/B → Tables **AB/1/2/3** → **Prompt 3/INT**) **+ Graphviz** (**includes Prompt\_Co**).                 |
| `python BioShift.py --context disease --mode full_no_graphviz`                                      |**full pipeline** (A/B → Tables **AB/1/2/3** → **Prompt 3/INT**) **without Graphviz** (**includes Prompt\_Co**).           |
| `python BioShift.py --context healthy --mode prompt_co`                                             |**Prompt\_Co only** on **all** observed CSVs (outputs to each sample’s `Prompt_Co_Output` folder).                         |
| `python BioShift.py --context disease --mode full_no_graphviz --observed_dir "C:/path/to/observed"` | If you want **custom directory**. Works with **any** observed-data mode (`shift_only`, `full_*`, or `prompt_co`).         |



2. Observed Data – Single Sample (Specify single file in inputs/observed/) : Examples
| Command                                                                                                 | What it does                                                                                                         |
| ------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `python BioShift.py --context disease --mode shift_only --sample 10737_progressing`             | Runs **shift calculation only** for the specified observed CSV.                                                      |

The commnads are similar to (1), you just need to mention your sample for the modes you want to run. 



3. Table 3 Direct (Operate directly on a Table 3 CSV files; no Observed CSV required) – Bulk (All files in inputs/table3/)
| Command                                                                                      | What it does                                                               |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `python BioShift.py --context disease --mode table3_direct --run interpret`          | Scans `inputs/table3/*.csv`; runs **Interpretation only** for all.         |
| `python BioShift.py --context healthy --mode table3_direct --run graphviz`           | Scans `inputs/table3/*.csv`; runs **Graphviz only** for all.               |
| `python BioShift.py --context disease --mode table3_direct --run interpret_graphviz` | Scans `inputs/table3/*.csv`; runs **Interpretation + Graphviz** for all.   |
| `python BioShift.py --context healthy --mode table3_batch --run interpret`           | Explicit batch runner: **Interpretation only** for all Table 3 CSVs.       |
| `python BioShift.py --context disease --mode table3_batch --run graphviz`            | Explicit batch runner: **Graphviz only** for all Table 3 CSVs.             |
| `python BioShift.py --context healthy --mode table3_batch --run interpret_graphviz`  | Explicit batch runner: **Interpretation + Graphviz** for all Table 3 CSVs. |



4. Table 3 Direct – Single File 

| Command                                                                                                                                       | What it does                                                   |
| --------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `python BioShift.py --context disease --mode table3_direct --table3 "inputs/table3/10737_stable_table3.csv" --run interpret`          | **Interpretation only** (Prompt 3) for the given Table 3 file. |


The commnads are similar to (3), you just need to mention your sample for the modes you want to run. 


### Both contexts running example (back-to-back):
python BioShift.py --context disease --mode full_with_graphviz  && \
python BioShift.py --context healthy --mode full_with_graphviz


## Output folder
 This is example of how outputs folders appear
project-root/
inputs/
project-root/
inputs/
├─ observed/
│  ├─ MySample123.csv
│  ├─ MySample124.csv
│  └─ ProjectX_Patient01.csv
├─ graphviz/
│  ├─ Graphviz1.txt
│  └─ Graphviz2.dot
└─ table3/
   ├─ Sample3_Table3.csv
   └─ ...

**outputs/**
└─ Disease/                                 # or Healthy/
   ├─ MySample123/
   │  ├─ prompts/
   │  │  ├─ PromptA_output.txt
   │  │  ├─ PromptB_output.txt
   │  │  └─ MySample123_Prompt3_output.txt
   │  ├─ Prompt_Co_Output/
   │  │  └─ MySample123_PromptCo_output.txt     # ← Fourth prompt output that use only observed shifts
   │  ├─ tables/
   │  │  ├─ MySample123_tableAB.csv
   │  │  ├─ MySample123_table1.csv
   │  │  ├─ MySample123_table2.csv
   │  │  └─ MySample123_table3.csv
   │  ├─ graphviz/                               
   │  │  ├─ MySample123_Graphviz1_highlighted.jpg
   │  │  └─ MySample123_Graphviz2_highlighted.jpg
   │  ├─ elements/
   │  │  └─ MySample123_Elements.txt
   │  └─ table3direct/
   │     └─ MySample123_table3/
   │        ├─ interpret/
   │        └─ graphviz/
   ├─ MySample124/
   └─ ProjectX_Patient01/


**Reference:**
© https://github.com/SayakaMiura
