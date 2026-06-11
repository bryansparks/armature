# Armature hero demo — REAL commands, real output (nothing faked)
#
# Prereqs to regenerate:
#   brew install vhs ffmpeg ttyd
#   python3 -m venv /tmp/armdemo/venv && cp examples/01_hello_world.yml /tmp/armdemo/hello_world.yml
#   (edit model_tiers to a provider you have a key for; pre-warm deps:
#    /tmp/armdemo/venv/bin/pip install armature-agents && /tmp/armdemo/venv/bin/pip uninstall -y armature-agents)
#   vhs demo-hero.tape

Output demo-hero.gif

Set Shell bash
Set FontSize 15
Set Width 1100
Set Height 640
Set Padding 16
Set WindowBar Colorful
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "source /tmp/armdemo/venv/bin/activate && cd /tmp/armdemo && export PS1='❯ ' && clear"
Enter
Sleep 1
Show

Type "pip install armature-agents"
Sleep 500ms
Enter
Sleep 7

Type "clear"
Enter
Type "cat hello_world.yml"
Sleep 500ms
Enter
Sleep 4

Type "armature run hello_world.yml --input topic='why agents need a harness'"
Sleep 500ms
Enter
Sleep 14

Sleep 4
