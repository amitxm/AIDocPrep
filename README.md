# AI DocPrep

*The ultimate pre-processor for feeding documents to LLMs.*

AI DocPrep is a native desktop utility that wraps Microsoft's MarkItDown to convert bloated office files (.docx, .pdf, .pptx) into token-efficient Markdown. Perfect for ChatGPT, Claude, or local Ollama workflows, it features batch conversion, privacy redaction, and OS right-click integration to supercharge your AI ingestion pipeline.

## Features

- **Drag & Drop Workflow**: Easily drop single files or entire folders into the app.
- **Batch Processing**: Recursively scans and converts `.docx`, `.pdf`, `.pptx`, `.xlsx`, `.vtt`, and `.html` files using full CPU multithreading.
- **Combined Master Document**: Instantly merges all converted files into a single master document with an Auto-Generated Table of Contents linking to each sub-file.
- **YAML Frontmatter**: Injects rich metadata into the Markdown output.
- **Privacy First**: Completely local processing. No files are uploaded to the cloud unless you explicitly enable a cloud LLM for image descriptions.

## Installation

### Windows
Download the `AIDocPrep_Setup.exe` from the Releases page. The installer will automatically add a "Convert to Markdown" option to your Windows right-click menu.

### macOS
Download `AIDocPrep.app` from the Releases page and drag it to your Applications folder.

## Building from Source

**Requirements:**
- Python 3.11+

```bash
# Clone the repository
git clone https://github.com/yourusername/AI-DocPrep.git
cd AI-DocPrep

# Setup Virtual Environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate

# Install Dependencies
pip install -r requirements.txt
```

**Build Commands:**
- **Windows:** Run `.\build_win.ps1` to build the folder executable, then compile `installer.iss` using Inno Setup.
- **macOS:** Run `bash build_mac.sh` to generate the native App bundle.
