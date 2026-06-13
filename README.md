<p align="center">
  <img src="icon.png" alt="AI DocPrep Logo" width="128"/>
</p>

# AI DocPrep

AI DocPrep is an offline desktop utility that converts office files (`.docx`, `.pdf`, `.pptx`, `.xlsx`, `.html`, `.vtt`) into clean, token-efficient Markdown. 

If you frequently feed custom documents, manuals, or folders of notes into LLMs (like Claude, GPT-4, or local models via Ollama), AI DocPrep helps you prepare that text so you use fewer tokens and get better responses.

## Why clean your documents first?

Large Language Models parse Markdown much better than raw PDF or Word files. Directly uploading PDFs or spreadsheets often wastes context window space on layout metadata, XML junk, and style tags.

By converting to Markdown first:
- **Save Tokens:** Stripping hidden formatting and structure metadata reduces the token count of your files.
- **Better Answers:** LLMs understand semantic Markdown (headers, lists, tables) natively.
- **Consolidate Sources:** Merge an entire folder of files into a single `combined.md` file with a table of contents.
- **Privacy:** Scrub PII (SSNs, credit cards, emails, phone numbers) locally on your machine before uploading anything to a cloud provider.

## Features

- **Drag & Drop UI:** Drop files or entire folders into the window to start converting.
- **Format Detection:** Powered by Microsoft's `MarkItDown` and Google's `Magika`. It inspects file signatures rather than relying on extension names, so it handles mislabeled files correctly.
- **Local Redaction:** A built-in regex-based PII scrubber removes sensitive data. Nothing is sent to the internet.
- **Folder Combiner:** Merge multiple documents into a single master Markdown file, complete with a table of contents and anchor links.
- **Multi-threaded Processing:** Conversions run in parallel across CPU threads (capped at 75% usage to prevent system slowdowns).
- **Metadata Insertion:** Adds YAML frontmatter (timestamps, original paths, file sizes) to the top of each file, which is useful for Obsidian, Notion, or custom scripts.
- **OS Integration:** Add "Convert to Markdown" options directly to the Windows right-click menu or macOS Quick Actions.

## FAQ: Why use this instead of RAG?

Retrieval-Augmented Generation (RAG) is useful for searching across massive datasets (like millions of customer service logs), but it is often overkill and less effective for smaller knowledge bases (like code documentation, project folders, or product manuals).

### 1. Modern LLMs have massive context windows
Models like Claude 3.5 Sonnet and Gemini 1.5 Pro support context windows from 200k to 2 million tokens. You can fit hundreds of documents directly into the prompt. If your data fits in the context window, you don't need RAG.

### 2. RAG misses the "Big Picture"
RAG works by cutting documents into small chunks and retrieving only the most relevant-looking chunks. If a question requires synthesizing information spread across multiple pages, chapters, or files, RAG often fails to retrieve all the pieces. Giving the LLM the entire compiled file lets it reason across the whole dataset at once.

### 3. No retrieval failures or vector database setup
Vector search is imprecise and depends on the user using the right search terms. RAG also requires setting up database infrastructure, managing embedding models, and tuning chunk sizes. AI DocPrep has zero setup: just convert your documents, merge them into one file, and start chatting.

### 4. RAG is better with clean Markdown
If your dataset is so large that you *must* use RAG, chunking raw PDFs or HTML is notoriously messy. Running your documents through AI DocPrep first gives you clean Markdown tables, lists, and headers, which makes your embedding model and chunking strategy much more accurate.

## Installation

### Windows
1. Download `AIDocPrep_Setup.exe` from the [Releases page](https://github.com/amitxm/AIDocPrep/releases).
2. Run the installer. It will add "Convert to Markdown" to your Windows explorer context menu.

### macOS
1. Download `AIDocPrep.app` from the [Releases page](https://github.com/amitxm/AIDocPrep/releases).
2. Move it to your Applications folder.

## Building from Source

### Prerequisites
- Python 3.11+

### Setup
```bash
# Clone the repository
git clone https://github.com/amitxm/AIDocPrep.git
cd AIDocPrep

# Set up virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Build Commands
- **Windows:** Run `.\build_win.ps1` to build the optimized PyInstaller executable, then compile `installer.iss` with Inno Setup.
- **macOS:** Run `bash build_mac.sh` to generate the macOS App bundle.
