# Keeping IB Gateway running (IBC)

The [daily paper run](live-paper.md) talks to Interactive Brokers through TWS or IB Gateway,
which must be open and logged in when the run starts. If it is not, the run stops with
`broker_unreachable` (exit code 6) and nothing is proposed. IB Gateway does not stay open
by itself:

- it must be **restarted every day** (IB's rule), and it closes if nobody does it;
- IB ends every session **once a week**, on Sunday after 01:00 New York, and a full login is
  needed again;
- after a reboot, or a Mac that logged out, nothing starts it.

[IBC](https://github.com/IbcAlpha/IBC) (open source, GPL-3.0, not part of Tradewind) starts
TWS or IB Gateway, fills in the login dialog from a configuration file, answers the usual
dialogs, sets the API options, and handles the daily restart and the Sunday logout. A
scheduler then only has to start IBC when Gateway is not running.

!!! warning "Your login stays in one file, on your machine"
    IBC reads the IB login and password from its `config.ini`. Keep that file in your home
    folder, readable by you only (`chmod 600`), on an encrypted disk, and never under version
    control. The examples here leave both fields empty.

## Install IBC

1. Install IB Gateway, the **offline / standalone** build (IBC does not drive the
   self-updating one). On a Mac it goes to `~/Applications/IB Gateway 10.NN/`. Note its
   major version (*Help > About*: `Build 10.52.1g` is `10.52`).
2. Download the latest release for your system from
   [IBC's Releases page](https://github.com/IbcAlpha/IBC/releases) (`IBCMacos-x.y.z.zip`,
   `IBCLinux-x.y.z.zip`) and compare its SHA-256 with the one shown on the page:

    ```bash
    shasum -a 256 IBCMacos-3.24.2.zip          # Linux: sha256sum
    ```

3. Unzip it into a folder of your own. This page uses `~/ibc/` for IBC's files, its
   `config.ini` and its logs (IBC's own default for the program is `/opt/ibc`, which needs
   `sudo`):

    ```bash
    mkdir -p ~/ibc && unzip IBCMacos-3.24.2.zip -d ~/ibc
    chmod u+x ~/ibc/*.sh ~/ibc/scripts/*.sh && chmod 700 ~/ibc
    ```

4. Edit the top of the start script (`gatewaystartmacos.sh` on a Mac, `gatewaystart.sh` on
   Linux), and nothing below the line that says not to:

    | setting | Mac | Linux |
    |---|---|---|
    | `TWS_MAJOR_VRSN` | `10.52` (with the dot) | `1052` (without) |
    | `IBC_INI` | `~/ibc/config.ini` | `~/ibc/config.ini` |
    | `TRADING_MODE` | `paper` | `paper` |
    | `TWOFA_TIMEOUT_ACTION` | `exit` | `exit` |
    | `IBC_PATH` | `~/ibc` | `~/ibc` |
    | `TWS_PATH` | `~/Applications` (default) | `~/Jts` (default) |
    | `LOG_PATH` | `~/ibc/logs` | `~/ibc/logs` |

    Leave `TWSUSERID` and `TWSPASSWORD` empty: the login goes in `config.ini`.

On its first start on a Mac, IBC renames `IB Gateway 10.NN.app` to `IB Gateway 10.NN-1.app`,
so that Gateway is not started by hand without IBC by mistake. To start it by hand anyway,
open the renamed application.

## `config.ini`

Start from the `config.ini` shipped in the release (it documents every setting), save it as
`~/ibc/config.ini`, and set the lines of
[`config-paper.ini`](examples/ibc/config-paper.ini) in it:

| setting | value | why |
|---|---|---|
| `IbLoginId`, `IbPassword` | yours | the paper account's **own username** (the live login works with `TradingMode=paper` but asks for a second factor, see Limits) |
| `TradingMode` | `paper` | the paper account; the daily run refuses any other |
| `ReadOnlyApi` | `no` | orders can be placed (the run sends only with `--send`) |
| `OverrideTwsApiPort` | `4002` | IB Gateway's paper port, the one the run accepts; IBC sets it at every start |
| `AcceptIncomingConnectionAction` | `reject` | 127.0.0.1 is always accepted; any other address is refused without a dialog |
| `AutoRestartTime` | `06:00 AM` | the daily restart, without a new login; pick a time away from the run |
| `ColdRestartTime` | `07:05` | Sundays: close and log in again, after 01:00 New York (07:05 Paris is 01:05 or 02:05 there) |
| `ExistingSessionDetectedAction` | `primary` | a login elsewhere with the same user does not take the session away for good |
| `AcceptNonBrokerageAccountWarning` | `yes` | the paper account's warning dialog |
| `DismissPasswordExpiryWarning`, `BypassOrderPrecautions` | `yes` | dialogs nobody is there to close |
| `MinimizeMainWindow` | `yes` | Gateway's window stays out of the way |

```bash
chmod 600 ~/ibc/config.ini
```

Gateway's own settings (port, trusted IPs, *Read-Only API*) live in its settings folder
(`~/Jts` by default) and persist; the IBC settings above re-apply them at every start, so a
fresh install needs no visit to *Configure > Settings > API*.

## Start it by hand once

```bash
~/ibc/gatewaystartmacos.sh -inline      # Linux: ~/ibc/gatewaystart.sh -inline
```

Gateway opens, logs in and shows the paper account. Check the port, then run the daily
command with `--propose`:

```bash
lsof -nP -iTCP:4002 -sTCP:LISTEN
```

IBC's log is in `~/ibc/logs/` (one file per weekday, overwritten a week later). Stop it with
*File > Close* in Gateway, or `pkill -f "java.*ibc/config.ini"`.

## Schedule it

[`start-gateway-if-needed.sh`](examples/ibc/start-gateway-if-needed.sh) is what the
scheduler calls. It starts IBC unless Gateway is already running (started by IBC or by hand,
or something listens on 4002), and it starts nothing while `IbLoginId` is empty: a scheduler
loaded before the login is filled in does not fail to log in again and again.

```bash
cp docs/examples/ibc/start-gateway-if-needed.sh ~/ibc/ && chmod 700 ~/ibc/start-gateway-if-needed.sh
```

### macOS (launchd)

[`com.paperswithbacktest.ibc-gateway.plist`](examples/ibc/com.paperswithbacktest.ibc-gateway.plist)
runs the script at login and on weekdays at 22:15, half an hour before the daily run of
22:45 (launchd reads the Mac's clock). Gateway has windows, so the job runs in the logged-in
session (`LimitLoadToSessionType Aqua`): the Mac must stay logged in, its screen may be
locked.

**Load it only once `config.ini` holds the login** (edit the two paths first):

```bash
cp docs/examples/ibc/com.paperswithbacktest.ibc-gateway.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.paperswithbacktest.ibc-gateway.plist
launchctl print gui/$(id -u)/com.paperswithbacktest.ibc-gateway | grep -E 'state|last exit'
sudo pmset repeat wakeorpoweron MTWRF 22:10:00     # wake the Mac before 22:15
```

`pmset repeat` holds a single schedule: this one replaces any earlier `pmset repeat` (for
instance the 22:40 of the daily run's page). 22:10 wakes the Mac for both jobs.

To remove it:

```bash
launchctl bootout gui/$(id -u)/com.paperswithbacktest.ibc-gateway
rm ~/Library/LaunchAgents/com.paperswithbacktest.ibc-gateway.plist
sudo pmset repeat cancel
```

### Linux (systemd)

[`ibc-gateway.service`](examples/ibc/ibc-gateway.service) is a user unit. Gateway is a
desktop program; on a server without a screen, `xvfb-run` gives it a virtual display
(`apt install xvfb`). The unit starts the script at boot and starts it again ten minutes
after Gateway stops; IBC does the daily restart and the Sunday login in between.

```bash
cp docs/examples/ibc/ibc-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ibc-gateway.service
loginctl enable-linger $USER
journalctl --user -u ibc-gateway -f
```

## Limits

- **A live login asks for a second factor** (IBKR Mobile) at every full login: at the
  first start and every Sunday, **even with `TradingMode=paper`**. IBC cannot answer it;
  someone must, within three minutes, or Gateway waits at the login dialog until someone logs
  in by hand (`ReloginAfterSecondFactorAuthenticationTimeout=yes` makes IBC start the login
  again instead). The daily auto-restart does not ask for it. Use the paper account's **own
  username** (Client Portal: *Settings > Paper Trading Account*), which has no second factor:
  then nothing needs a person. The daily run trades paper accounts only.
- **The machine must be on, awake and logged in** at the run's time (on a Mac:
  `pmset repeat`, and *Energy* settings that do not log the user out). A Mac asleep through
  `AutoRestartTime` may find Gateway disconnected at wake; the 22:15 job does not restart a
  Gateway that is still running, so check the daily run's `broker_unreachable` alert.
- **One login, one session.** IB allows one trading session per user. With
  `ExistingSessionDetectedAction=primary`, a later login of the same user elsewhere (TWS, the
  mobile app) is the one that ends, and if it is told to end Gateway's session, Gateway
  reconnects. For manual checks, use the Client Portal's read-only views or a second user.
- **Gateway updates** install to a new folder (`IB Gateway 10.NN`): change
  `TWS_MAJOR_VRSN` in the start script after each update.
- IBC is a third-party project: its options and behaviour are documented in its
  [user guide](https://github.com/IbcAlpha/IBC/blob/master/userguide.md), which is the
  reference when this page and IBC disagree.
