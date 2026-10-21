# Olive Automation

Windows automation, always against a target you chose: record and play back macros, watch the screen (a pixel's
colour, a picture, text) and decide what to do next -- WAIT UNTIL, IF / THEN / ELSE -- for a window on the Windows
desktop, the whole desktop, or an LDPlayer instance.

> **Beta.** Things may change between versions. Your macros are kept across updates.

## Install (64-bit Windows 10/11)

Open PowerShell and run:

```
irm https://raw.githubusercontent.com/maksoffensive-collab/olive-automation/main/scripts/install.ps1 | iex
```

Or download `Olive-<version>-win64.zip` from [Releases](https://github.com/maksoffensive-collab/olive-automation/releases),
unpack it anywhere and start `Olive\Olive.exe`. Nothing else needs to be installed, and no administrator rights are
needed.

- The program goes to `%LOCALAPPDATA%\Programs\Olive`, with "Olive Automation (beta)" in the Start menu and on the
  desktop. Run the command again to update.
- Your macros, settings and history live in `%LOCALAPPDATA%\Olive\data`. Installing, updating and removing never
  touch them. To move them to another PC, copy that folder while Olive is closed.
- The beta is not signed: on the first start Windows may say "Windows protected your PC". Choose "More info", then
  "Run anyway".
- Remove: `powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Olive\Uninstall.ps1"`.

## What it does

- **Targets**: a window, the whole desktop, or an LDPlayer instance -- explicitly chosen and checked before every run;
  never "all", never guessed.
- **Recorded macros**: clicks, drags, keys and text, played back with a dry run that sends nothing first.
- **Scripts**: blocks connected on a canvas -- WAIT UNTIL and IF over a pixel's colour, a picture or text on the screen
  (Windows' own text recognition), with "yes" and "no" branches and a start delay.
- **Adapting to the size**: clicks, areas and pictures follow the target's size when its proportions stay the same.
- **Emergency stop**: the red stop bar, or Esc in Olive's window, stops a run at once.

## Building from source

Python 3.12 on Windows:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-vision.txt     # optional: fast picture search
.venv\Scripts\python.exe -m app                                         # start
.venv\Scripts\python.exe -m unittest discover -s tests -t .             # tests
```

The release is built with `scripts\build_release.py` (needs `requirements-dev.txt` too).
