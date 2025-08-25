# BioShift

## Description
BioShift produces biological interpretations to given observed shifts of biological elements. See Dawadi et al. (ref. 1) for the detail. It is written in Python. You are free to download, modify, and expand this code under a permissive license similar to the BSD 2-Clause License (see below).

## Dependencies

### 1. Anaconda
Please install Anaconda: https://www.anaconda.com/distribution/

Open Anaconda terminal and then create conda environment for Bioshift. 

Type the foloowing to create Conda Environment for BioShift

conda create -n bioshift python=3.12 -y

conda activate bioshift

Note: We tested and 3.12.1 and 3.12.2 

### 2. Python packages

 python packages: 
 
 openai>=1.42.0,<2.0.0

pandas>=2.2.2,<3.0.0
  
 numpy>=1.26.4,<3.0.0
 

You can try

python3 -m pip install -r requirements.txt


### 3. API key
1. Create an OpenAI account. https://platform.openai.com/
   
2. Once logged in, click your profile icon (top-right corner) → Manage Account → Billing.
   
2. In the Billing section, set up Prepaid Billing or Auto Recharge
   Prepaid: Manually add credit (e.g., $5, $10).
   Auto Recharge: Automatically top up when balance is low.
   
 3. Check Your Usage
   Open Usage from the left-hand menu to monitor your monthly spend and remaining balance
   Link for pricing: https://openai.com/api/pricing/

 4. Go to OpenAI API keys: https://platform.openai.com/api-keys

 5. Click “Create new secret key” → Copy the key (it looks like sk-...).
    
  ⚠ Important: Treat this key like a password — never share it or commit it to public code repositories.

### 4. Graphviz
 > You can install it through conda. Run the following:
 >> conda install anaconda::graphviz 
 

## How to prepare input files
### 1. Observed shift
Please list biological element with shift direction. Increased shifts should be indicated with "1", and decreased shifts are "-1." The columns should be separated by comma. Please see Testdata.csv as an example. Please place it inputs/observed.

|Element | Observed Shift|
|----------|------|
|IL-1B | 1|
|Mononuclear phagocytes |-1|
|Th17 | 1|

### 2. Config file
Please edit config.txt for the parameter settings. You can change "default_model" according to your need. Also you can change "temperature" and maximum number of tokens. 

We originally used OpenAI, but you can adjust the contents of these files to work with any AI tool you choose.

|KEY=["WRITE YOUR KEY HERE"]
|------------------------|
|DEFAULT_MODEL=gpt-4o-mini|
|TEMPERATURE=0.5|
|MAX_TOKENS=2000|

### 3. Immune pathway files
If you wish to map biological elements on your immune pathways, please prepare dot files and place them in the graphviz folder. It already contains our curated pathways, and the format can be found there. Please place it inputs/graphviz.

### 4. Co-shifted elements (optional)
When you have groups of co-shifted elements, please list element, shift direction, and group ID. Increased shifts should be indicated with "1", and decreased shifts are "-1." The columns should be separated by comma. Please see MySample_table3.csv as an example. Please place it inputs/Table 3.

|Element|Observed Shift|Expected Shift|Biological Group|
|----------|-----------|--------------|------------|
|IL-1B|1|1|Pro-inflammatory Cytokines|
|IL-6|1|1|Pro-inflammatory Cytokines|

## How to use


To perform the analysis, please assign "context" (healthy or disease) and mode (see options for the mode for the detail). It will analyze all input files in the input folder.    

`python BioShift.py --context [healthy or disease] --mode [select mode]` 

Below is an example to analyze all files in the input folder under the context of disease with "full_with_graphviz" mode.

`python BioShift.py --context disease --mode full_with_graphviz` 

If you wish to analyze only one file in the input folder, please add "--sample". For example, to analyze only 10737_progressing.csv file in the input file, below is an example.

`python BioShift.py --context disease --mode full_with_graphviz --sample 10737_progressing` 

Note: 
For Linux and Mac users
Use python3 instead of 'python'. For example, 
`python3 BioShift.py --context [healthy or disease] --mode [select mode]`


### Output files
A folder will be created for each input file within the "outputs" folder will be created, and all output files will be stored there. Within the folder, sub-folders will be created. 

|Sub-folder name|output file names|description|
|--------------|------------------|-----------|
| prompts     |PromptA_output.txt|Expected individual shift direction (per element)|
|       |PromptB_output.txt|Expected joint shift direction (groups/pairs that commonly shift together)|
|       |MySample123_Prompt3_output.txt|Biological interpretation of grouped elements, refined to rows where Observed Shift = Expected Shift|
|Prompt_Co_Output |MySample123_PromptCo_output.txt |Biological interpretation without filtering (co-regulation/cascades summary from the observed CSV)|
|tables |MySample123_tableAB.csv|Table merged from Prompt A and Prompt B outputs|
|         |MySample123_table1.csv|Table merging Prompt A & B with the observed shift for each element|



### Options for the mode

|Mode|Description|Required input file|
|-----|-------|--------------------|
|`shift_only`|**Prompt A & B**| `inputs/observed/*.csv` |
|`full_with_graphviz`|**Prompt A, B & C + Pathway** |`inputs/observed/*.csv` |
|`full_no_graphviz`|**Prompt A, B & C**|`inputs/observed/*.csv` |
|`table3_direct`|**Prompt C**|**Table 3 Direct (CSV)** (`inputs/table3/*.csv` or `--table3 PATH`)|
|`table3_batch` |**Prompt C**|**Table 3 Direct (CSV)** (`inputs/table3/*.csv` or `--table3 PATH`)|

>> table3_direct — Runs on a single Table 3 CSV passed via --table3; if omitted, it processes all files in inputs/table3/ (batch-like).
>> table3_batch — Processes every *.csv in inputs/table3/

When `table3_direct` or `table3_batch` is selected, assign "--run" which should be interpret (only biological interpretation), graphviz (only pathway mapping), or interpret_graphviz (both of them).

`python BioShift.py --context disease --mode table3_direct --run interpret` 

`python BioShift.py --context disease --mode table3_direct --run graphviz`   

`python BioShift.py --context disease --mode table3_direct --run interpret_graphviz` 





--------
### Reference
[1] Prabin Dawadi, Josh Gililland, Sayaka Miura, and Flavia Teles, BioShift: Prompt-Guided Workflow for Interpreting Immune–Microbiome Shifts. (2025) Under Review

--------
### Copyright 2025, Authors and University of Mississippi
BSD 3-Clause "New" or "Revised" License, which is a permissive license similar to the BSD 2-Clause License except that that it prohibits others from using the name of the project or its contributors to promote derived products without written consent. 
Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
