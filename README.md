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



project-root/
│
├── BioShiftPipeline.py
├── inputs/
│   ├── observed/             # Your input CSVs with elements & observed shifts
│   ├── graphviz/             # Input Graphviz .dot files (for highlighting)
│   └── Table 3/              # Input Table 3 (According to need) CSVs (for interpretation/highlighting)





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



### Set the API Key in Your Terminal for the current session.
1. For Windows 
   setx OPENAI_API_KEY "your_api_key_here"

2. For Linux/ mac
   export OPENAI_API_KEY="your_api_key_here"
   

## Apperance of working directory
This is what your initial working directory looks like 

project-root/
│
├── BioShiftPipeline.py
├── inputs/
│   ├── observed/   # Place your observed CSV files here    
│   └── graphviz/   # Place your Graphviz .dot or .txt files here for network highlighting / Shiftmapper
│   └── Table 3/      # Place your Table 3 csv containing cols "Element", "Observed Shift", "Expected Shift" and "Biological Group"


## Running BioShift and choosing OpenAI model
Running the BioShiftPipeline You can run the BioShiftPipeline in various modes depending on your needs: Command Structure All commands run from your working directory (where BioShiftBioShiftPipeline.py is located). 

Use Command Terminal.

### Supported model options 
| Model Name      | Description                                                              
| --------------- | ------------------------------------------------------------------------- 
| `gpt-4o`        | Latest flagship GPT-4o model, highly accurate and reasoning-strong        
| `gpt-4o-mini`   | Lightweight GPT-4o version, faster and cheaper, good for quick iterations 
| `gpt-3.5-turbo` | Older GPT-3.5 turbo model, very fast and cheapest option                  
| `o1-mini`       | Ultra-light, low-cost model for basic tasks                              

If --model is not specified, the pipeline will prompt you to type a model name before running.
Model choice can impact speed, cost, and output quality.

**Important note:** You should check avaialbilty and cost of the models that aligns with your need. 

### Full BioShiftPipeline (with Graphviz highlighting): Context disease
 python BioShiftPipeline.py --context disease --mode full_with_graphviz --model gpt-4o-mini


### Full BioShiftPipeline (with Graphviz highlighting): Context healthy 
python BioShiftPipeline.py --context healthy --mode full_with_graphviz --model gpt-4o-mini


### Both contexts (back-to-back):
python BioShiftPipeline.py --context disease --mode full_with_graphviz --model gpt-4o-mini && python BioShiftPipeline.py --context healthy --mode full_with_graphviz --model gpt-4o-mini


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
│
├── BioShiftPipeline.py
├── inputs/
│   ├── observed/
│   │   └── MySample123.csv
│   ├── graphviz/
│   │   └── Graphviz1.dot
		└── Graphviz2.dot	
│   └── Table 3/
│
├── outputs/
│   └── disease/
│       └── MySample123/
│           ├── prompts/
│           │   ├── PromptA_output.txt
│           │   ├── PromptB_output.txt
│           │   ├──MySample123_Prompt3_output
│           │
│           ├── tables/
│           │   ├── MySample123_TableAB.csv
│           │   ├── MySample123_Table1.csv
│           │   ├── MySample123_Table2.csv
│           │   └── MySample123_Table3.csv
│           │
│           ├── graphviz/
│           │   ├── MySample123_Graphviz1_highlighted.jpg
│           │   └── MySample123_Graphviz2_highlighted.jpg
│           │
│           ├── elements/
│           │   ├── MySample123.txt

