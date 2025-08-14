# BioShift
BioShift is a structured prompt based framework that leverages LLMs that are broadly trained text on the internet, e.g., ChatGPT . BioShift is designed as interpretive engines to mimic expert reasoning in immune-microbiome analysis. Our approach guides users through a structured analysis workflow that begins by scanning for well-known shifts and coordinated patterns, mirroring initial expert steps of validating biological signals and ensuring the observations are meaningful. This is followed by the generation of biological interpretations, which users are then required to validate through manual literature review. This review process ensures scientific rigor and also serves as a valuable training exercise for researchers new to the field, helping them develop critical knowledge of immune–microbiome interactions. As a case study, we applied BioShift to a real-world, longitudinal multi-omics dataset from a patient with periodontitis. This application demonstrates how a prompt-based approach facilitates the extraction of interpretable, literature-grounded insights from immunological and microbial data. 


## Setting up Your API credits and getting API key
You may modify codes in BioShiftPipeline.py based on your Large Language model.

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
Your working directory should contain BioShiftBioShiftPipeline.py 
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
├── BioShiftPipeline.py
├── inputs/
│   ├── observed/             # Your input CSVs with elements & observed shifts
│   ├── graphviz/             # Input Graphviz .dot files (for highlighting)
│   ├──  Table 3/             # Input Table 3 (According to need) CSVs (for interpretation/highlighting)
├── config/
│   ├── API_key.txt 
│   ├── gpt_options.json 


Note:
You can find guide to install Graphviz : https://graphviz.org/download/ or could install Graphviz package. 

### Troublbleshooting for Windows Users
If you're Windows user and having issue:
	1. Manually graphviz-13.1.1 (64-bit) ZIP archive [sha256] from https://graphviz.org/download/ [This is what we used for this BioShiftPipelines]
	2. Extract the zip file and save it in a specific directory. [For e.g. C:/Graphviz] 
	3. From Search go to "Edit the system environmental variables"
	4. Click "Environmental variables"  
	4. Click on "Path"
	5. Add a "New" path where you Graphviz bin is located [For e.g.C:\Graphviz\Graphviz-13.1.1-win64\bin]
	6. Apply the changes

## Editing API_key and default AI model

project-root/
├── config/
│   ├── API_key.txt 
│   ├── gpt_options.json 

You can edit API_key.txt to add your own API key.
Replace "sk ............."

You can also edit the .json file to change the default mode. 
{
    "models": {
        "gpt-4o-mini":      { "temperature": 0.5, "max_tokens": 2048 },
        "gpt-4o":           { "temperature": 0.4, "max_tokens": 3000 },
        "gpt-4.1-mini":     { "temperature": 0.5, "max_tokens": 2048 },
        "gpt-4.1":          { "temperature": 0.3, "max_tokens": 4000 },
        "gpt-3.5-turbo":    { "temperature": 0.6, "max_tokens": 1500 },
        "o1-mini":          { "temperature": 0.5, "max_tokens": 2048 },
        "o1-preview":       { "temperature": 0.5, "max_tokens": 2048 },
        "o3-mini":          { "temperature": 0.5, "max_tokens": 2048 }
    },
    "default_model": "gpt-4o-mini"
}

You can change "default_model" according to your need. Also you can change "temperature" and maximum number of tokens. 


We originally used OpenAI, but you can adjust the contents of these files to work with any AI tool you choose.


## FOR RUNNING BishiftPipeline.py

### Set the API Key in Your Terminal for the current session.
1. For Windows 
   setx OPENAI_API_KEY "your_api_key_here"

2. For Linux/ mac
   export OPENAI_API_KEY="your_api_key_here"
   

### Selection of pipelines based on input files

| **Input Type**           | **Available Modes for disease and/or healthy condition**                                                                                                 |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| **Observed Data**        | `shift_only`, `full_with_graphviz`, `full_no_graphviz`, `interpret_only`, `interpret_and_graphviz`, `graphviz_only` |
| **Table 3 Direct (CSV)** | `interpret_only`, `graphviz_only`, `interpret_graphviz`                                                             |


### Running command in terminal: 

1. Observed Data – Bulk (All files in inputs/observed/)

| Command                                                                                                       | What it does                                                                       |
| ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `python BioShiftPipeline.py --context disease --mode shift_only`                                            | Runs **shift calculation only** on **all** observed CSVs.                          |
| `python BioShiftPipeline.py --context healthy --mode full_with_graphviz`                                    | Runs **full pipeline + Graphviz** on all observed CSVs.                            |
| `python BioShiftPipeline.py --context disease --mode full_no_graphviz`                                      | Runs **full pipeline without Graphviz** on all observed CSVs.                      |
| `python BioShiftPipeline.py --context healthy --mode interpret_only`                                        | Runs **Prompt 3 only** on all samples (expects existing Table 3 per sample).       |
| `python BioShiftPipeline.py --context disease --mode interpret_and_graphviz`                                | Runs **Prompt 3 + Graphviz** on all samples (expects existing Table 3 per sample). |
| `python BioShiftPipeline.py --context healthy --mode graphviz_only`                                         | Runs **Graphviz only** on all samples (expects existing Table 3 per sample).       |
| `python BioShiftPipeline.py --context disease --mode full_no_graphviz --observed_dir "C:/path/to/observed"` | Same as above but reads observed CSVs from a **custom directory**.                 |


Offers shifts_only, full_with_graphviz, full_no_graphviz, interpret_only and graphviz_only. You can apply "healthy" and "disease" contexts according to your need. 

2. Observed Data – Single Sample (Specify single file in inputs/observed/) : Examples
| Command                                                                                                   | What it does                                                                                                     |
| --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `python BioShiftPipeline.py --context disease --mode shift_only --sample 10737_progressing`             | Runs **shift calculation only** for the specified observed CSV.                                                  |
| `python BioShiftPipeline.py --context healthy --mode full_with_graphviz --sample 10737_stable`          | Runs **full pipeline** (Prompts A/B → Tables AB/1/2/3 → Prompt 3) **+ Graphviz** for the specified observed CSV. |
| `python BioShiftPipeline.py --context disease --mode full_no_graphviz --sample 10737_progressing`       | Runs **full pipeline without Graphviz** for the specified observed CSV.                                          |
| `python BioShiftPipeline.py --context healthy --mode interpret_only --sample 10737_stable`              | Runs **Prompt 3 only** using the sample’s generated Table 3.                                                     |
| `python BioShiftPipeline.py --context disease --mode interpret_and_graphviz --sample 10737_progressing` | Runs **Prompt 3 + Graphviz** using the sample’s generated Table 3.                                               |
| `python BioShiftPipeline.py --context healthy --mode graphviz_only --sample 10737_stable`               | Runs **Graphviz only** using the sample’s generated Table 3.                                                     |

Offers shifts_only, full_with_graphviz, full_no_graphviz, interpret_only and graphviz_only. You can apply "healthy" and "disease" contexts according to your need. 

3. Table 3 Direct (Operate directly on a Table 3 CSV files; no Observed CSV required) – Bulk (All files in inputs/table3/)
   | Command                                                                                        | What it does                                                               |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `python BioShiftPipeline.py --context disease --mode table3_direct --run interpret`          | Scans `inputs/table3/*.csv`; runs **Interpretation only** for all.         |
| `python BioShiftPipeline.py --context healthy --mode table3_direct --run graphviz`           | Scans `inputs/table3/*.csv`; runs **Graphviz only** for all.               |
| `python BioShiftPipeline.py --context disease --mode table3_direct --run interpret_graphviz` | Scans `inputs/table3/*.csv`; runs **Interpretation + Graphviz** for all.   |
| `python BioShiftPipeline.py --context healthy --mode table3_batch --run interpret`           | Explicit batch runner: **Interpretation only** for all Table 3 CSVs.       |
| `python BioShiftPipeline.py --context disease --mode table3_batch --run graphviz`            | Explicit batch runner: **Graphviz only** for all Table 3 CSVs.             |
| `python BioShiftPipeline.py --context healthy --mode table3_batch --run interpret_graphviz`  | Explicit batch runner: **Interpretation + Graphviz** for all Table 3 CSVs. |

Offers interpret_only and graphviz_only or interpret_graphviz. You can apply "healthy" and "disease" contexts according to your need. 

4. Table 3 Direct – Single File 

| Command                                                                                                                                         | What it does                                                   |
| ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `python BioShiftPipeline.py --context disease --mode table3_direct --table3 "inputs/table3/10737_stable_table3.csv" --run interpret`          | **Interpretation only** (Prompt 3) for the given Table 3 file. |
| `python BioShiftPipeline.py --context healthy --mode table3_direct --table3 "inputs/table3/10737_stable_table3.csv" --run graphviz`           | **Graphviz only** for the given Table 3 file.                  |
| `python BioShiftPipeline.py --context disease --mode table3_direct --table3 "inputs/table3/10737_stable_table3.csv" --run interpret_graphviz` | **Interpretation + Graphviz** for the given Table 3 file.      |


Offers interpret_only and graphviz_only or interpret_graphviz. You can apply "healthy" and "disease" contexts according to your need. 





### Both contexts (back-to-back):
python BioShiftPipeline.py --context disease --mode full_with_graphviz  && python BioShiftPipeline.py --context healthy --mode full_with_graphviz 


### Other Modes
| Mode                                 | Command Example                                                                                                           | Description                                             |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| **shift\_only** (Prompt A/B, Tables) | `python BioShiftPipeline.py --context disease --mode shift_only --model gpt-4o-mini`                                      | Only runs observed shift analysis (Prompt A/B + tables) |
| **full\_no\_graphviz**               | `python BioShiftPipeline.py --context disease --mode full_no_graphviz --model gpt-4o-mini`                                | Full BioShiftPipeline without Graphviz rendering        |
| **interpret\_only** (Prompt 3 only)  | `python BioShiftPipeline.py --context disease --mode interpret_only --model gpt-4o-mini`                                  | Runs Prompt 3 interpretation on Table 3 only            |
| **interpret\_and\_graphviz**         | `python BioShiftPipeline.py --context disease --mode interpret_and_graphviz --model gpt-4o-mini`                          | Runs Prompt 3 interpretation + Graphviz highlighting    |
| **graphviz\_only**                   | `python BioShiftPipeline.py --context disease --mode graphviz_only --model gpt-4o-mini`                                   | Only highlights Table 3 in an existing Graphviz network |
| **Run on single sample**             | `python BioShiftPipeline.py --context disease --mode full_no_graphviz --model gpt-4o-mini --sample MySample123`           | Runs pipeline on a specific sample file                 |
| **Use custom Table 3**               | `python BioShiftPipeline.py --context disease --mode interpret_only --model gpt-4o-mini --table3 "C:/path/to/Table3.csv"` | Uses your own Table 3 CSV file                          |

**Note:** You can do the same for healthy context.

**Special Notes**
Direct Table 3 Use:
When running with a custom Table 3, for example:

python BioShiftPipeline.py --context disease --mode interpret_only --model gpt-4o-mini --sample MySample123 --table3 "C:/path/to/Table3.csv"
							OR
python3 BioShiftPipeline.py --context disease --mode graphviz_only --model gpt-4o-mini --sample MySample123 --table3 /path/to/Table3.csv

The BioShiftPipeline will use your specified Table 3 for Prompt 3 and ShiftMapper. 



## Output folder
 This is example of how outputs folders appear
project-root/
inputs/
├─ observed/                               # Full run, shifts-only, shift+interpretation, interpretation-only, or Graphviz-only
│  ├─ MySample123.csv                      # → outputs/disease/MySample123/
│  ├─ MySample124.csv                      # → outputs/disease/MySample124/
│  ├─ ProjectX_Patient01.csv               # → outputs/disease/ProjectX_Patient01/
│  └─ ...                                  # Add as many *.csv files as needed
│
├─ graphviz/
│  ├─ Graphviz1.txt
│  └─ Graphviz2.txt
│  └─ .......                             # Add as many *txt or .dot Graphviz files as needed
│
└─ Table3/                                 # Use when Table 3 is your starting input
│  ├─ Sample3_Table3.csv                   # Add as many *.csv files as needed
│   └─ ...
│
**outputs/**
└─ disease/                                # Windows path: E.g. C:\BioShift\outputs\Disease\
   ├─ MySample123/                          # Named after MySample123.csv
   │  ├─ prompts/
   │  │  ├─ PromptA_output.txt
   │  │  ├─ PromptB_output.txt
   │  │  └─ MySample123_Prompt3_output.txt
   │  │
   │  ├─ tables/
   │  │  ├─ MySample123_TableAB.csv
   │  │  ├─ MySample123_Table1.csv
   │  │  ├─ MySample123_Table2.csv
   │  │  └─ MySample123_Table3.csv
   │  │
   │  ├─ graphviz/
   │  │  ├─ MySample123_Graphviz1_highlighted.jpg
   │  │  └─ MySample123_Graphviz2_highlighted.jpg
   │  │
   │  ├─ elements/
   │  │  └─ MySample123.txt
   │  │
   │  └─ table3direct/                      # When Table 3 is directly used as input
   │     └─ MySample_Table3/
   │        ├─ interpret/
   │        │  └─ <interpretation_files_here>
   │        │
   │        └─ graphviz/
   │           └─ <graphviz_files_here>
   │
   ├─ MySample124/                          # Named after MySample124.csv
   │  └─ ...
   │
   └─ ProjectX_Patient01/                   # Named after ProjectX_Patient01.csv
      └─ ...

