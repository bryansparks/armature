# Multi-Agent Consensus: Parallel Agents + Intelligent Synthesis
# Generate with: vhs < demo-multi-agent.tape > demo-multi-agent.gif

Output demo-multi-agent.gif
Set FontSize 13
Set Width 1400
Set Height 700
Set Theme Dracula

Type "# Multi-Agent Deliberation with Fan-Out & Consensus"
Enter
Sleep 1

Type "# Define your team once in YAML..."
Enter
Sleep 0.5
Type "cat consensus-workflow.yaml"
Enter
Sleep 2.5
Type "stages:"
Enter
Type "  - id: research"
Enter
Type "    fan_out:"
Enter
Type "      partition_source: perspectives"
Enter
Type "      branches:"
Enter
Type "        - role: {type: researcher, description: 'Technical perspective'}"
Enter
Type "        - role: {type: researcher, description: 'Business perspective'}"
Enter
Type "        - role: {type: researcher, description: 'Risk perspective'}"
Enter
Type "  - id: synthesis"
Enter
Type "    fan_in: consensus"
Enter
Type "    role: {type: judge, description: 'Synthesize all three views'}"
Enter
Sleep 2

Type "# Run it..."
Enter
Sleep 0.5
Type "armature run consensus-workflow.yaml --input topic='expand to AWS'"
Enter
Sleep 3

Type "✓ Technical agent: 'Use multi-region autoscaling'"
Enter
Sleep 0.8
Type "✓ Business agent: 'ROI breakeven in 18 months'"
Enter
Sleep 0.8
Type "✓ Risk agent: 'Compliance gaps in region-X'"
Enter
Sleep 1.5

Type "# Judge synthesizes all three dissenting opinions..."
Enter
Sleep 1
Type "✓ Recommendation: AWS multi-region with regional compliance review"
Enter
Sleep 1
Type "✓ Decision preserved: What was debated, what was decided, why."
Enter
Sleep 2
