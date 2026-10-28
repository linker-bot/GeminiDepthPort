# Explicit USBFS resource control

The formal management configuration still accepts at most three cameras.
`usbfs_minimum_mb` is an optional, explicitly chosen 1..65536 MiB inventory
requirement. `geminidepthport usbfs-status` reads the kernel parameter and reports
the declared requirement; preflight rejects a positive lower limit below it.
Without a requirement it reports `USBFS_BUDGET_UNDECLARED`. No camera-count
formula or hardware capacity certification is inferred.

The [current Linux USBFS implementation](https://github.com/torvalds/linux/blob/master/drivers/usb/core/devio.c)
uses a global transfer-buffer budget and treats zero as no limit from that
parameter. This does not remove other allocation/transfer bounds or certify free
memory. The [kernel parameter documentation](https://docs.kernel.org/admin-guide/kernel-parameters.html)
still describes an older 2047 MB maximum, so do not infer modern host-wide
capacity from that older description. This tool's 65536 MiB operational bound
is a deliberate tool limit, not a claim about every kernel's maximum.

The standalone administrator tool defaults to a read-only plan:

```
python3 scripts/host/usbfs_control.py --expected-current 16 --megabytes 256 --persist
sudo python3 scripts/host/usbfs_control.py --management-root /absolute/deployment --expected-current 16 --megabytes 256 --persist --apply
sudo python3 scripts/host/usbfs_control.py --management-root /absolute/deployment --expected-current 256 --restore
```

The values above are examples, not a universal sizing recommendation. Apply and
restore require administrator privileges, compare the actual current value,
hold the deployment's existing `state/control.lock`, and refuse any recording
guard. The receipt is written privately before changing the runtime parameter.
Restoration works after a partially completed operation and restores the prior
parameter and the exact previous managed modprobe file. Unexpected third-party
changes stop restoration rather than overwriting them. Path traversal, symlink
components and a conflicting lifecycle lock are rejected. The `--root` option
supports a real isolated filesystem/chroot, never changes the managed relative
parameter/config/receipt paths, and does not disable privilege checks.

`--persist` manages only `/etc/modprobe.d/geminidepthport-usbfs.conf`. It applies
on a later module load; a kernel with built-in USB core may need a separately
approved boot configuration. The tool explicitly reports that builtin boot
application is unverified. It never invokes systemctl, enables units, reloads a
module, modifies a bootloader, or restarts the host. Existing deployment/systemd
startup remains unchanged and uses the same lifecycle lock. After reboot run
`usbfs-status` again before enabling capture; a file existing is not evidence
that the running kernel applied it.
