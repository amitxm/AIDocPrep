#!/bin/bash
# A script to generate a macOS quick action that passes the selected file/folder to the built app.
# Automator workflows are complex bundles, so this script provides the exact setup instructions.

echo "To create a macOS Quick Action:"
echo "1. Open Automator and create a new 'Quick Action'"
echo "2. Set 'Workflow receives current' to 'files or folders' in 'Finder'"
echo "3. Add a 'Run Shell Script' action."
echo "4. Set 'Pass input' to 'as arguments'."
echo "5. Add this bash script:"
echo ""
echo "   /Applications/AIDocPrep.app/Contents/MacOS/AIDocPrep \"\$1\""
echo ""
echo "6. Save the workflow as 'Convert to Markdown'."
echo "It will now appear when you right-click files in Finder."
