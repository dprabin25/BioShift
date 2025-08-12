# BioShift
BioShift is a structured prompt based framework that leverages LLMs that are broadly trained text on the internet, e.g., ChatGPT . BioShift is designed as interpretive engines to mimic expert reasoning in immune-microbiome analysis. Our approach guides users through a structured analysis workflow that begins by scanning for well-known shifts and coordinated patterns, mirroring initial expert steps of validating biological signals and ensuring the observations are meaningful. This is followed by the generation of biological interpretations, which users are then required to validate through manual literature review. This review process ensures scientific rigor and also serves as a valuable training exercise for researchers new to the field, helping them develop critical knowledge of immune–microbiome interactions. As a case study, we applied BioShift to a real-world, longitudinal multi-omics dataset from a patient with periodontitis. This application demonstrates how a prompt-based approach facilitates the extraction of interpretable, literature-grounded insights from immunological and microbial data. 

## Working directory with essential folders and files
Your working directory should contain BioShiftBioShiftPipeline.py 
In your working directory, you can have input folders 
	1. Folder name "observed" containing csv files with Elements and Observed Shifts 
	2. Folder name "graphviz" containing .dot files 
	3. Table 3 if you want to get Prompt 3 output or Graphviz jpg figure

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



## Apperance of working directory
This is what your initial working directory looks like 

project-root/
│
├── BioShiftPipeline.py
├── inputs/
│   ├── observed/   # Place your observed CSV files here    
│   └── graphviz/   # Place your Graphviz .dot files here (for network highlighting)
│   └── Table 3/      # Place your Table 3 csv containing cols "Element", "Observed Shift", "Expected Shift" and "Biological Group"


## Running BioShift
Running the BioShiftPipeline You can run the BioShiftPipeline in various modes depending on your needs: Command Structure All commands are run from your working directory (where BioShiftBioShiftPipeline.py is located). Use Command Prompt, PowerShell, Bash, or Git Bash)

### Full BioShiftPipeline (with Graphviz highlighting): Context disease
 python BioShiftPipeline.py --context disease --mode full_with_graphviz

### Full BioShiftPipeline (with Graphviz highlighting): Context healthy 
python BioShiftPipeline.py --context healthy --mode full_with_graphviz

### Both contexts (back-to-back):
python BioShiftPipeline.py --context disease --mode full_with_graphviz && python BioShiftPipeline.py --context healthy --mode full_with_graphviz


### Other Modes
| Mode                             | Command                                                    | Description                                  |
| -------------------------------- | -------------------------------------------------------------------- | -------------------------------------------- |
| shift\_only (Prompt A/B, Tables) | `python BioShiftPipeline.py --context disease --mode shift_only`             | Only runs observed shift analysis            |
| full\_no\_graphviz               | `python BioShiftPipeline.py --context disease --mode full_no_graphviz`       | Full BioShiftPipeline, skips graph rendering         |
| interpret\_only (Prompt 3 only)  | `python BioShiftPipeline.py --context disease --mode interpret_only`         | Runs Prompt 3 interpretation on Table 3 only |
| interpret\_and\_graphviz         | `python BioShiftPipeline.py --context disease --mode interpret_and_graphviz` | Prompt 3 interpretation + graph highlighting |
| graphviz\_only                   | `python BioShiftPipeline.py --context disease --mode graphviz_only`          | Only highlights Table 3 in Graphviz network  |
| Run on single sample             | Add `--sample MySample123` to any command                            | Run on a specific sample file                |
| Use custom Table 3               | Add `--table3 "C:/path/to/Table3.csv"` to any command                | Use your own Table 3 CSV                     |

**Note:** You can do the same for healthy context.

**Special Notes**
Direct Table 3 Use:
When running with a custom Table 3, for example:
python BioShiftPipeline.py --context disease --mode interpret_only --sample MySample123 --table3 "C:/path/to/Table3.csv"


python BioShiftPipeline.py --context disease --mode graphviz_only --sample MySample123 --table3 "C:/path/to/Table3.csv"
The BioShiftPipeline will use your specified Table 3 for the analysis/highlighting.

It will look for a corresponding .dot file in inputs/graphviz/ named MySample123.dot.

If not found, it will use any available .dot file in the folder (and print which one it picked).


## Output folder
 This is how outputs folder appear
project-root/
│
├── BioShiftPipeline.py
├── inputs/
│   ├── observed/             # Your input CSVs with elements & observed shifts
│   ├── graphviz/             # Input Graphviz .dot files (for highlighting)
│   └── Table 3/              # Input Table 3 CSVs (for interpretation/highlighting)
│
├── outputs/
│   ├── [sample_name]/        # Output folder for each sample or run
│   │    ├── TableA.csv       
│   │    ├── TableB.csv
│   │    ├── TableAB.csv
│   │    ├── Table1.csv
│   │    ├── Table2.csv
│   │    ├── Table3.csv       
│   │    ├── PromptA.txt      
│   │    ├── PromptB.txt
│   │    ├── Prompt3.txt      
│   │    └── [sample].jpg     

