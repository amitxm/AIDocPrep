<p align="center">
  <img src="icon.png" alt="AI DocPrep Logo" width="128"/>
</p>

# AI DocPrep

*The ultimate pre-processor for feeding documents to Large Language Models.*

**AI DocPrep** is a blazing fast, completely offline desktop utility that batch-converts bloated office files (`.docx`, `.pdf`, `.pptx`, `.xlsx`) into ultra-clean, token-efficient Markdown. Whether you're feeding context into ChatGPT, Anthropic's Claude, or running local AI agents with Ollama, AI DocPrep ensures your LLM ingestion pipeline is fast, private, and optimized.

## Why Pre-Process Documents?

Large Language Models thrive on clean text. When you upload a raw PDF, Word Document, or Excel Spreadsheet directly into an AI, the model wastes valuable "Context Window" space (and API tokens) trying to parse invisible styling, XML tags, and formatting junk. 

By pre-processing your files into Markdown with **AI DocPrep**:
- **Drastically Reduce Token Costs:** Markdown strips away thousands of hidden formatting characters, saving you money on API calls.
- **Improve AI Comprehension:** LLMs natively understand Markdown. Clean, semantic structures (like `# Headers` and `| Tables |`) result in drastically better AI answers.
- **Merge Knowledge Bases:** Combine hundreds of scattered files into one single `combined.md` file that an AI can read instantly.
- **Protect Privacy:** Automatically scrub sensitive PII (Social Security Numbers, Credit Cards, Emails, Phone Numbers) *before* handing the text over to a cloud AI.

## Core Capabilities

- **Drag & Drop Simplicity**: Drop single files or massive, nested directory trees directly into the UI.
- **Intelligent Format Support**: Powered by Microsoft's `MarkItDown` engine and Google's `Magika` ML model. Accurately parses `.docx`, `.pdf`, `.pptx`, `.xlsx`, `.vtt`, and `.html`—even if the file extensions are missing or incorrect.
- **Privacy Redaction Engine**: An onboard Regex engine instantly scrubs sensitive PII from the text output. 100% offline.
- **Master Document Combiner**: Automatically merges hundreds of processed files into a single `folder-combined.md` master document, complete with an Auto-Generated Table of Contents and anchor links.
- **Multi-Threaded Speed**: Recursively scans and converts files using optimized CPU multithreading (capped at 75% of your CPU to keep your machine responsive).
- **YAML Frontmatter**: Injects rich metadata (timestamps, file sizes, original paths) into the output for Notion, Obsidian, and programmatic parsing.
- **OS Context Menus**: Natively injects "Convert to Markdown" into your Windows right-click menu or macOS Quick Actions.

## Installation

### Windows
1. Download the `AIDocPrep_Setup.exe` from the [Releases page](https://github.com/amitxm/AIDocPrep/releases).
2. Run the installer. It will automatically add a "Convert to Markdown" option to your Windows right-click menu.

### macOS
1. Download `AIDocPrep.app` from the [Releases page](https://github.com/amitxm/AIDocPrep/releases).
2. Drag it to your Applications folder.

## Building from Source

**Requirements:**
- Python 3.11+

```bash
# Clone the repository
git clone https://github.com/amitxm/AIDocPrep.git
cd AIDocPrep

# Setup Virtual Environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate

# Install Dependencies
pip install -r requirements.txt
```

**Build Commands:**
- **Windows:** Run `.\build_win.ps1` to automatically build the stripped-down, optimized PyInstaller executable. Then compile `installer.iss` using Inno Setup.
- **macOS:** Run `bash build_mac.sh` to generate the native Mac App bundle.
