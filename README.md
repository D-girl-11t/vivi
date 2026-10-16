# Bring-up Studio

A step-by-step guide for adding **any** peripheral to an embedded Linux board.
It looks at your board, your kernel source tree and a short form you fill in,
then walks you through every layer with the reasoning behind each step, and
checks each step for you.

```
┌ LEFT: what exists ─┬ CENTER: workspace ───────────┬ RIGHT: what to do next ────┐
│ Board  (live scan) │ file viewer + proposed change│ peripheral ▾   ✎ Form      │
│ Source (DTS chain) │ examples from other boards   │ Hardware·Pinmux·…·App      │
│ Pins   (pad→funcs) │ Live plot of sysfs values    │ ① ✓  ② ✗ ← current  ③ …   │
└────────────────────┴──────────────────────────────┴────────────────────────────┘
```

## Run

```sh
./run.sh                 # → http://127.0.0.1:8765  (home page)
```

The **home page** shows setup state (SoC, kernel source, board), your devices with
their progress, and **＋ Add a device**: a wizard that searches your kernel tree for the
part's compatible string, tells you whether a driver exists, pre-fills bus and subsystem,
and asks only for the wiring. The workspace (three panels) is at `/app`.

Only needs `python3` with `pyyaml` and `jinja2` (Ubuntu ships both). No npm, no build step.

## First-time setup (⚙ in the UI, or edit `workspace.yaml`)

1. **`kernel_src`**: the kernel tree your board actually runs (NXP `linux-imx` for the
   Yocto BSP, or mainline), plus `board_dts` inside it.
2. **Board access**: `ssh-copy-id root@<board-ip>`, then set `board.transport: ssh` and `host`.
   Press **Rescan board**. Kconfig checks need `CONFIG_IKCONFIG_PROC=y` (/proc/config.gz);
   pin-ownership and deferred-probe info need debugfs (`mount -t debugfs none /sys/kernel/debug`).
3. Until the board is connected, `transport: snapshot` uses `snapshots/demo-imx93-frdm.json`.
   That file was compiled from the real FRDM DTS, but its drivers, kconfig and dmesg data are **not real**.

## The method (the same for every peripheral)

| # | Layer | Question | How it's checked |
|---|---|---|---|
| 1 | hardware | Which pads? Free? Voltage? | pinfunc header + other users of the pad in source + live pinmux |
| 2 | pinmux | Pads muxed to the right function? | mux macro present in the include chain |
| 3 | controller | Bus controller `status = "okay"`? | effective status after all overrides |
| 4 | node | Device described with the right `compatible`? | node present in source, and it has every supply/clock/gpio/endpoint the **driver code** asks for |
| 4b | driver | No driver in your kernel? Bring one in | `driver_src` found, Makefile/Kconfig hooks present, `.o` builds |
| 5 | kconfig | Driver built? | compatible → driver .c → Makefile → `CONFIG_` → board's /proc/config.gz |
| 6 | deploy | Is the new .dtb actually booted? | live /sys/firmware/devicetree |
| 7 | probe | Driver bound? Deferred? | /sys/bus/*/devices/*/driver + devices_deferred + dmesg |
| 8 | subsystem | Shows up in ALSA/V4L2/IIO/input…? | subsystem listing from the scan |
| 9 | userspace | Standard tool works? | runs the command on the board when you press Check |
| 10 | app | Your code can use it | sample code; you mark it done |

The **current** step is always the first one that isn't green. When something
breaks, the first red step tells you which layer to debug.

## When your kernel has no driver for the part

Set `driver_src:` in the form (or in the wizard) to the driver's `.c` file, for example
one from your vendor's BSP, the module maker's kernel fork, or a sibling driver you're adapting.
The guide then adds a **Driver** layer: get the source, add the Makefile/Kconfig hooks
(with `select`s derived from the APIs the driver calls), compile it against your kernel
(with hints for known API changes between versions), then enable and probe it as usual.

The tool also **reads the driver's C code** (`kernel.py: driver_requirements`) for the
supplies, clocks (and required rate), GPIOs, graph endpoints, lane counts and link
frequencies it looks up. Those values fill in the device-tree snippet, and the `driver_props`
check compares your node against them. This works the same for in-tree and out-of-tree drivers.

## Cameras (MIPI CSI-2)

`bus: csi2` covers any CSI-2 sensor: the I2C control bus, the module's fixed clock and
regulators, the sensor node with its endpoint, freeing a receiver input that another camera
already uses, the receiver link, the capture engine, and the media pipeline.
The SoC-specific node names and layout live in the SoC profile under `camera.styles`.
NXP changed its layout between BSP releases, so the planner uses whichever one exists in your tree.

## Optional AI helper

Each step has **✦ Ask AI**. It's off by default; turn it on with `ai: {enabled: true}` in
`workspace.yaml` and start with `ANTHROPIC_API_KEY=… ./run.sh` (the key is never written
to disk). It sends the step plus the tool's evidence (form, snippet, check results, what the
driver reads, matching dmesg lines, the board-file block) and shows you exactly what was sent.
The guide itself never depends on it: steps and checks stay rule-based, so they're
reproducible and can't make up properties.

## How it generalizes: three kinds of knowledge

```
peripherals/<name>.yaml   ← you: ~10 lines from the schematic & datasheet
knowledge/bus/*.yaml      ← how a device connects   (i2c, spi, gpio, sai, usb, uart, csi2)
knowledge/subsystem/*.yaml← how Linux exposes it    (alsa, v4l2, iio, input, counter, hwmon, tty, other)
knowledge/soc/*.yaml      ← how this chip spells things (pinmux syntax, controller names, camera layout, deploy)
knowledge/core.yaml       ← the shared ladder steps
```

- **New device on a known bus/subsystem**: write a form. Nothing else.
- **New bus or subsystem**: add one YAML template (steps with `why`, `snippet`, `check`).
- **New SoC** (STM32MP, Qualcomm…): add one SoC profile. `stm32mp15.yaml` is a starter
  that shows a completely different pinmux model (port + AF number instead of a macro header).

Facts come from **your** trees, never from memory: pad functions from the pinfunc header,
required properties from `Documentation/devicetree/bindings`, the CONFIG_ symbol from the
driver's Makefile, and "how others do it" from other board files of the same SoC.

### Check types (for templates)

`source_text` · `source_status` · `source_compatible` · `pins_muxed` · `pads_free` ·
`kconfig` · `dt_live` · `probe` · `scan` (regex on a scan section) · `board_cmd` (runs on
Check) · `driver_source` · `driver_props` · `kernel_text` · `kernel_file` · `all_status` · `manual`

## Tests

```sh
python3 -m unittest tests.test_units -v
BS_KERNEL=/path/to/linux python3 -m unittest tests.test_e2e -v
```

`test_e2e` applies every example peripheral's generated snippets to a **copy** of the
board DTS, compiles it with `cpp` + `dtc`, requires zero new dtc warnings, and checks
that every source-side step then turns green.

## Layout

```
server/  dts.py (DTS include-chain indexer) · kernel.py (binding/driver/Kconfig lookup, driver reader)
         board.py (ssh/local/snapshot + scan) · planner.py · checks.py · ai.py (optional) · app.py (HTTP, stdlib)
web/     home.html · home.js (home + wizard) · index.html · app.js (workspace) · style.css  (plain JS)
tools/   make_demo_snapshot.py
```

## Roadmap

- Live views per subsystem: audio waveform (ALSA), video preview (V4L2), input event stream
- Remoteproc/RPMsg path for real-time work on the M33 (fast encoders, motor control)
- DT overlay (.dtso) output, so you can try changes without rebuilding the base dtb
- A Qualcomm SoC profile

## Boot files (workspace → Boot tab)

Lists every `.dtb` in the board's boot partition and works out which one U-Boot loads
(from `fdtfile`, or by comparing each file with the live tree in `/sys/firmware/fdt`).
Compare any two (or your Kernel source build) node by node: phandle renumbering is
ignored, and what the bootloader adds at boot (bootargs, MACs, memory, OP-TEE) is hidden
unless asked for. **Install from my source** builds the board `.dtb` with labels (`-@`)
and uploads it under a new name. **Boot this one** sets `fdtfile` when `fw_setenv` exists,
otherwise backs up the loaded file and swaps it in. It always shows the undo command and
the U-Boot rescue steps, and **Reboot now and check** waits for the board and rescans.

In Setup → Kernel source, **Use a .dts from the internet** downloads a board file from a
link into your tree (new name, registered in the Makefile) and checks it.
