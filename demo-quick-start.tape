# Quick Start: Define & Run a Multi-Agent Workflow in 60 Seconds
# Generate with: vhs < demo-quick-start.tape > demo-quick-start.gif

Output demo-quick-start.gif
Set FontSize 14
Set Width 1200
Set Height 600
Set Theme Dracula

# Title
Type "# Armature: YAML-First Multi-Agent Orchestration"
Enter
Sleep 1

# Show directory
Type "ls -la my-research-workflow/"
Enter
Sleep 1.5

# Cat the YAML spec
Type "cat my-research-workflow/spec.yaml"
Enter
Sleep 3

# Show it's clean (no Python boilerplate)
Type "# Just YAML. No Python. No class subclassing."
Enter
Sleep 1.5

# Run the workflow
Type "armature run my-research-workflow/spec.yaml --input topic='quantum error correction'"
Enter
Sleep 4

# Show the trace output
Sleep 2

# Final message
Type "# Self-improving on the next run..."
Enter
Sleep 1
