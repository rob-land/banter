# Running Banter in the background

Banter can stay loaded after you close its window so message
notifications keep arriving in the GNOME notification list / Phosh
notification drawer. Same idea as the desktop tray-icon-as-daemon
pattern, expressed with `Gio.Notification` instead of a tray icon
(GNOME and Phosh don't ship native system trays).

## Enable it

Open Banter → ⋮ menu → **Preferences** → **Background activity**:

- **Run in background** — when on, closing the window hides it
  instead of quitting. Banter's push-notification client stays
  connected to GroupMe, so new messages still pop notifications.
- **Run at startup** — when on, your session brings Banter up in
  the background at login. The first time you flip this, your
  desktop's portal asks "allow Banter to run in the background?"
  — answer yes.

The two switches are independent: you can run in background on
close without autostart (and vice versa).

## What close-to-background looks like

When you close the window with "Run in background" on:

1. The window hides.
2. Banter posts a one-shot notification "Banter is running in the
   background — you'll still get message notifications. Click to
   reopen."
3. The push client stays connected. New messages fire their normal
   per-conversation notifications.
4. Clicking the persistent "running in background" notification
   (or relaunching Banter from your app launcher) brings the
   window back.

The notification is just a presence indicator — it disappears
as soon as the window comes back, and it doesn't re-fire on
every close.

## What "Run at startup" actually does

It writes a host autostart entry at
`~/.config/autostart/land.rob.banter.desktop`. The file points at
`flatpak run --command=banter land.rob.banter --background` (or
just `banter --background` if you're running from source).

`--background` mode is headless: Banter starts up, connects its
push client, registers for GroupMe notifications, and holds the
process alive without ever showing a window. Notifications open
the window on demand.

If autostart didn't work after a reboot, check:

- `~/.config/autostart/land.rob.banter.desktop` exists.
- Your session honors XDG autostart (any GNOME / Phosh / KDE
  setup does).
- Banter has actually completed sign-in once before — `--background`
  needs a stored OAuth token to be useful.

To turn it off, just flip the **Run at startup** switch back off
in Preferences; Banter removes the autostart file.

## Why no system tray

GNOME Shell upstream doesn't ship the `StatusNotifierItem` /
AppIndicator protocol. Phosh follows the same. Adding a tray would
require either:

- A GNOME extension (we'd be telling users to install an extension),
- Or shipping our own `StatusNotifierItem` D-Bus service that does
  nothing on stock GNOME and works on KDE / XFCE / etc.

`Gio.Notification` is the GNOME-native indicator, works on every
target shell we care about, and is what other GNOME-circle apps
(Polari, Geary) do for the same use case.

## Saving battery

On phones, the push-notification socket is the main background
cost. It's a single long-lived WebSocket — much cheaper than
periodic REST polling — but it does keep the radio modem warm.

If you don't want background notifications, leave **Run in
background** off and quit Banter when you're done. New messages
will be visible the next time you open the app.
