# Self-Improvement in Action: Watch Your Workflow Optimize Itself
# Generate with: vhs < demo-self-improvement.tape > demo-self-improvement.gif

Output demo-self-improvement.gif
Set FontSize 13
Set Width 1400
Set Height 700
Set Theme Dracula

Type "# Armature Self-Improvement Loop"
Enter
Sleep 1

Type "# First run: IHR = 0.62 (low)"
Enter
Sleep 0.5

Type "armature run workflow.yml --input prompt='analyze this dataset' | grep IHR"
Enter
Sleep 2
Type "IHR: 0.62 (low) — triggers improvement"
Enter
Sleep 1.5

Type "# Automatic analysis of failure signatures..."
Enter
Sleep 0.5
Type "armature improve"
Enter
Sleep 2.5
Type "✓ DiagnosticAnalyzer: Output validation failing at 40% rate"
Enter
Sleep 1
Type "✓ SpecRefiner: Proposing more specific prompts for researcher stage"
Enter
Sleep 1
Type "✓ Applied 3 safe changes to workflow.yml"
Enter
Sleep 1.5

Type "# Second run with improved spec..."
Enter
Sleep 0.5
Type "armature run workflow.yml --input prompt='analyze this dataset' | grep IHR"
Enter
Sleep 2
Type "IHR: 0.81 (improved)"
Enter
Sleep 1.5

Type "# That's it. Your workflow got better on its own. No manual intervention."
Enter
Sleep 2
