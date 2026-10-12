# harness-pad-4

Software for harness-pad-4, a 4-key pad that tells you when an AI agent is waiting for you.

Agents such as Claude Code stop and wait when they need permission or an answer. If you are
working on something else, you don't notice. This pad shows the state of each session with the
color of a key's LED, and shows what is being asked on its display. Pressing a key brings that
session's terminal to the front.

**The pad only notifies and navigates. It never sends an approval or any input.**
Read the request and decide in the terminal.

> The tools print their messages in Japanese for now. The sample output below is shown as is.

## Requirements

| | |
|---|---|
| Pad | harness-pad-4 (firmware already flashed) |
| OS | **macOS**, or **Windows 10 / 11** (experimental — see [Limitations on Windows](#limitations-on-windows)) |
| Python | 3.9 or later |
| Agent | Claude Code (for others, see [Connecting other agents](#connecting-other-agents)) |

macOS also needs Homebrew, to install hidapi. Windows needs nothing besides Python.

## Setup (macOS)

```sh
brew install hidapi
git clone https://github.com/SoyaManabe/harness-pad-4.git
cd harness-pad-4
./install.sh
```

`install.sh` does three things. It is safe to run more than once.

1. Creates a Python environment in `daemon/.venv` and installs the dependency (`hid`)
2. Installs the Claude Code skill `/pad` into `~/.claude/skills/pad/`
3. Registers the daemon as a service (starts at login, restarts if it crashes)

### Set up the hooks

The agent's state reaches the daemon through Claude Code hooks. **On macOS this step is manual.**

1. Merge the contents of `"hooks"` in [`daemon/hooks.example.json`](daemon/hooks.example.json)
   into `"hooks"` in `~/.claude/settings.json`. If you already have other hooks, keep them and add these alongside
2. Open `/hooks` in Claude Code, or start a new session

The hooks only forward what they receive to the daemon on your own machine (`127.0.0.1:8787`).
They do not change what the agent does. If the daemon is not running, the agent is not held up.

### Check that it works

Plug the pad in over USB and ask Claude Code to do something.

```sh
./daemon/agentpadctl status
```

```
常駐: 稼働中 (com.keyboard2026.agentpad)
パッド: 接続中
  S1* BUSY     my-project       /Users/you/my-project  [8b278297]
  S2  (空き)
  S3  (空き)
  S4  (空き)
```

If the S1 key lights up blue, it works.

## Setup (Windows)

Install the 64-bit Python from [python.org](https://www.python.org/downloads/windows/) first.
Then, in PowerShell or Command Prompt:

```bat
git clone https://github.com/SoyaManabe/harness-pad-4.git
cd harness-pad-4
python install.py
```

If `python` is not found, try `py install.py`.
`install.py` does four things. It is safe to run more than once, and needs no administrator rights.

1. Creates a Python environment in `daemon\.venv` and installs the dependencies (`hidapi`, `psutil`).
   **The `hidapi` package bundles the native library, so there is nothing else to install**
2. Installs the Claude Code skill `/pad` into `%USERPROFILE%\.claude\skills\pad\`
3. Adds the hooks to `%USERPROFILE%\.claude\settings.json` (details below)
4. Starts the daemon and registers it to start at login
   (adds `AgentPad` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`)

At the end it checks whether the hooks are registered and whether the pad is visible.

**After it finishes, open `/hooks` in Claude Code or start a new session** so the hooks are loaded.

### What it does to your Claude Code settings

- Existing settings and hooks are kept. Only this pad's hooks are added
- Running it again does not add duplicates. If you moved this folder, the old entries are updated in place
- The previous contents are saved as `settings.json.bak` before the file is changed
- If `settings.json` cannot be parsed, it is left untouched. `install.py` then tells you to merge
  `daemon\hooks.windows.json` by hand, and `agentpadctl status` keeps warning until the hooks are registered

On Windows the hooks send through the bundled [`daemon/hook_send.py`](daemon/hook_send.py) instead of
`curl`, so that they do not depend on which shell Claude Code uses to run hooks (Git Bash or PowerShell).
The hook entries contain the location of this folder, so **run `install.py` again if you move it.**

To enable the pad for a single project only, remove the entries from `%USERPROFILE%\.claude\settings.json`
and merge `daemon\hooks.windows.json` into that project's `.claude\settings.json` instead.
`agentpadctl` only looks at the user-level file, so it will keep reporting the hooks as unregistered.

### Check that it works

Plug the pad in over USB and ask Claude Code to do something.

```bat
daemon\agentpadctl status
```

```
自動起動: 登録済み
デーモン: 稼働中 (PID 12345)
パッド: 接続中 (PID 0x0005, LED 4 個)
  S1* BUSY     my-project       C:\Users\you\my-project  [8b278297]
  S2  (空き)
  S3  (空き)
  S4  (空き)
```

If the S1 key lights up blue, it works. If not, look at the output of `daemon\agentpadctl check`
(hooks registered? pad visible?) and `daemon\agentpadctl logs`.

### Limitations on Windows

The Windows version is experimental.

- **Bringing the terminal to the front works at the window level only.** Tabs cannot be selected.
  Sessions running inside WSL cannot be found.
- **The daemon is not restarted if it crashes.** Start it with `daemon\agentpadctl start`.
  It does start automatically at every login.
- There is no hidapi build for Windows on ARM. Use the x64 Python.

## Usage

### LED colors

Sessions take the free keys (S1–S4) in the order they start.

| Color | State | |
|---|---|---|
| Off | Empty | No session assigned |
| Dim white | Idle | |
| Blue | Busy | The agent is working |
| **Blinking orange** | **Waiting** | **Waiting for permission or an answer. Needs you** |
| Green | Done | Returns to idle after 30 seconds |
| Red | Error | |

### Keys

| Action | Result |
|---|---|
| Short press | Bring that session's terminal to the front |
| Long press (0.8 s) | Assign the session you most recently typed in to that key. If the key is taken, the two swap |

What "bring to the front" can do depends on the application. On macOS:

| Application | Result |
|---|---|
| Terminal, iTerm2 | Selects the session's tab and brings it to the front |
| VS Code, Cursor, Windsurf | Brings the window that has the folder open to the front (cannot select the tab) |
| Others | Brings the application to the front |

The first time you press a key, macOS asks for permission to control other applications. Allow it.

On Windows, the window showing the session is brought to the front. Tabs cannot be selected.
If the application has several windows, the one whose title contains the working folder's name is chosen.
Windows sometimes refuses the switch; the taskbar button flashes instead.

### Display

The top four lines show the state of each key. The bottom four show the details of a session that needs you.

```
1 BUSY api-server          ← state of S1–S4, and the session (name of its working folder)
2 WAIT!my-project
3 WAIT docs

S2 WAIT 0:42    ! 1/2      ← which key the details are for, its state, how long it has waited
Force push                 ← the description the agent gave
$ git push --force or      ← the operation itself
igin main                     (scrolls line by line if it does not fit)
```

- If several sessions are waiting or in error, their details are shown in turn (`1/2` at the top right).
- **`!` marks an operation that looks irreversible** (`rm -r`, `sudo`, `git push --force`, and so on).
  It is a fixed set of rules, so **the absence of `!` does not mean the operation is safe.**
- Idle sessions stay listed (`1 IDLE my-project`), so you can always see which session is on which key.
  The display turns off only when no session is assigned, or when the daemon is not running.

### The `/pad` skill

You can operate the pad from inside Claude Code.

| Input | Result |
|---|---|
| `/pad 2` | Assign **the current session** to S2 |
| `/pad release 2` | Free S2 |
| `/pad status` | Show the assignments |
| `/pad start`, `/pad stop`, `/pad restart` | Start, stop or restart the daemon |

Asking in plain words ("put this on S3 of the pad") works too.

### Commands

```sh
./daemon/agentpadctl status      # service state and assignments
./daemon/agentpadctl restart     # restart (assignments are lost)
./daemon/agentpadctl logs        # follow the log
./daemon/agentpadctl stop        # stop (starts again at next login)
./daemon/agentpadctl uninstall   # stop running as a service
```

On Windows, write `daemon\agentpadctl`. It has the same commands plus `daemon\agentpadctl check`
(can hidapi be loaded, is the pad visible, are the hooks registered).
The log is written to `%LOCALAPPDATA%\AgentPad\agentpad.log`.

## Good to know

- **Unplugging the pad does not stop the daemon.** Plug it back in and it shows the current state.
- **If the daemon stops, the LEDs and display turn off after about 10 seconds.**
  The keys do not type anything, so nothing happens when you press them while the daemon is down.
- **Up to four sessions.** When a fifth starts, an idle or done session is pushed out.
  If all four are busy or waiting, the fifth is not shown.
- **Restarting the daemon forgets the state.** Each session comes back the next time something happens in it.
  A session that is already waiting does not reappear until you respond to it.
- The daemon only accepts connections from your own machine (`127.0.0.1`). It sends nothing outside.

## Troubleshooting

| Symptom | What to check |
|---|---|
| LEDs do not light | Does `agentpadctl status` say the daemon is running (稼働中) and the pad is connected (接続中)? |
| Pad not found (未接続) | Is the USB cable a data cable? Try another port. On Windows, `daemon\agentpadctl check` tells you if a different pad version is plugged in |
| Running, but no sessions appear | The hooks are not registered or not loaded. On Windows, run `daemon\agentpadctl check`. After registering, open `/hooks` or start a new session |
| Display stays off | It is off when no session is assigned. Start a Claude Code session, then check `agentpadctl status` |
| Cannot load `libhidapi` (macOS) | Run `brew install hidapi`, then `./install.sh` again |
| Key press does not bring the terminal forward (macOS) | System Settings → Privacy & Security → Automation: is it allowed? |
| Key press does not bring the terminal forward (Windows) | See [below](#when-the-terminal-does-not-come-forward-windows) |
| Stopped working after moving the folder | Run `./install.sh` again (Windows: `python install.py`) |
| Cannot load `hid` (Windows) | Look at `daemon\agentpadctl check`. Run `python install.py` again |
| Hooks fail with an error (Windows) | An old Claude Code cannot read `args` in the hook settings. Update Claude Code |
| Started but no response (Windows) | Look at the end of `%LOCALAPPDATA%\AgentPad\agentpad.log`. Is something else using port 8787? |

### When the terminal does not come forward (Windows)

You can see where it loses track. `C:\path\to\project` is the folder the session is running in.

```bat
cd daemon
.venv\Scripts\python -m agentpad.focus C:\path\to\project
```

It prints the agent processes it found, their chain of parent processes, and the window it chose.

## Connecting other agents

The daemon accepts events in a common format that does not depend on the kind of agent.
If an agent can run a command when it starts waiting and when it stops waiting, it can be connected.
The format and how to send it are in [EVENTS.md](EVENTS.md) (in Japanese).

There is also support for receiving the state of agents running on other machines (told apart by host name),
but **it is experimental.** See §6 and §7 of [EVENTS.md](EVENTS.md).

## Uninstall

macOS:

```sh
./daemon/agentpadctl uninstall
rm -r ~/.claude/skills/pad
```

Windows:

```bat
daemon\agentpadctl uninstall
rmdir /s /q "%USERPROFILE%\.claude\skills\pad"
rmdir /s /q "%LOCALAPPDATA%\AgentPad"
```

Then remove the hooks you added from `~/.claude/settings.json`
(on Windows, the entries that call `hook_send.py`).

## License

[MIT](LICENSE)
