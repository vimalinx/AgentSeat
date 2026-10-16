#!/usr/bin/env python3
"""Read-only AT-SPI feasibility probe inside a disposable --private-dbus GUI.

Refuses a session bus outside its own dbus-run-session parent and refuses an
accessibility bus outside that session's process ancestry before enumerating
Registry applications. Reads only the exact spawned fixture PID's objects.
This is a probe, not a production ApplicationScope implementation.
"""

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]


def connection(address):
    return Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None,
    )


def call(bus, destination, path, interface, method, signature=None, values=None):
    params = GLib.Variant(signature, values) if signature else None
    return bus.call_sync(destination, path, interface, method, params, None, Gio.DBusCallFlags.NONE, 800, None).unpack()


def bus_pid(bus, name="org.freedesktop.DBus"):
    return call(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID", "(s)", (name,))[0]


def parent(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    raise RuntimeError("process parent is unavailable")


def ancestry(pid):
    result = []
    for _ in range(12):
        if pid <= 1 or pid in result:
            break
        result.append(pid)
        pid = parent(pid)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {"status": "RUNNING", "semantic_writes": False, "host_enumeration": False}
    fixture = launcher = registry = None
    session = a11y = None

    def terminate(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    try:
        session = connection(os.environ["DBUS_SESSION_BUS_ADDRESS"])
        session_pid = bus_pid(session)
        if parent(session_pid) != os.getppid():
            raise RuntimeError("session bus is not owned by this probe's dbus-run-session")
        result["session_bus_pid"] = session_pid
        # Own the launcher directly instead of trusting service-activation PPid
        # (an activation helper can be reparented to the user service manager).
        with (args.out / "bus-launcher.log").open("w") as log:
            launcher = subprocess.Popen(["/usr/lib/at-spi-bus-launcher", "--launch-immediately"],
                                        stdout=log, stderr=log)
        deadline = time.monotonic() + 5
        while True:
            try:
                owner_pid = bus_pid(session, "org.a11y.Bus")
                break
            except GLib.Error:
                if time.monotonic() >= deadline or launcher.poll() is not None:
                    raise RuntimeError("owned accessibility launcher did not acquire its private session name")
                time.sleep(.02)
        if owner_pid != launcher.pid:
            raise RuntimeError("org.a11y.Bus is not owned by the spawned test launcher")
        result["a11y_launcher_pid"] = launcher.pid
        address = call(session, "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")[0]
        a11y = connection(address)
        a11y_pid = bus_pid(a11y)
        lineage = ancestry(a11y_pid)
        result.update({"a11y_bus_pid": a11y_pid, "a11y_bus_ancestry": lineage,
                       "probe_parent": os.getppid(), "bus_address": address})
        if launcher.pid not in lineage:
            raise RuntimeError("accessibility bus ancestry is outside the private test session")
        result.update({"a11y_bus_pid": a11y_pid, "separate_bus": session_pid != a11y_pid,
                       "a11y_bus_ancestry": lineage, "scope_verified_before_enumeration": True})
        env = {**os.environ, "GTK_A11Y": "atspi", "GSK_RENDERER": "cairo", "AT_SPI_BUS_ADDRESS": address}
        env.pop("NO_AT_BRIDGE", None)
        with (args.out / "registry.log").open("w") as log:
            registry = subprocess.Popen(["/usr/lib/at-spi2-registryd"], env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 5
        while True:
            try:
                registry_pid = bus_pid(a11y, "org.a11y.atspi.Registry")
                break
            except GLib.Error:
                if time.monotonic() >= deadline or registry.poll() is not None:
                    raise RuntimeError("owned accessibility registry did not acquire its name")
                time.sleep(.02)
        if registry_pid != registry.pid:
            raise RuntimeError("accessibility registry is not owned by the test process")
        result["registry_pid"] = registry.pid
        with (args.out / "fixture.log").open("w") as log:
            fixture = subprocess.Popen(["/usr/bin/python3", str(ROOT / "tests/control_fixture.py"),
                                        "--state", str(args.out / "ui.json")], env=env, stdout=log, stderr=log)
        result["fixture_pid"] = fixture.pid
        deadline = time.monotonic() + 8
        target = None
        while time.monotonic() < deadline and fixture.poll() is None:
            children = call(a11y, "org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root",
                            "org.a11y.atspi.Accessible", "GetChildren")[0]
            for owner, path in children:
                if bus_pid(a11y, owner) == fixture.pid:
                    target = (owner, path)
                    break
            if target:
                break
            time.sleep(.05)
        if target is None:
            raise RuntimeError("the exact fixture process did not register an accessible root")

        nodes, pending, seen = [], [(target, 0)], set()
        deadline = time.monotonic() + 5
        while pending and len(nodes) < 64 and time.monotonic() < deadline:
            (owner, path), depth = pending.pop(0)
            if (owner, path) in seen:
                continue
            seen.add((owner, path))
            if bus_pid(a11y, owner) != fixture.pid:
                raise RuntimeError("accessible reference escaped the fixture PID")
            props = call(a11y, owner, path, "org.freedesktop.DBus.Properties", "GetAll", "(s)", ("org.a11y.atspi.Accessible",))[0]
            role = call(a11y, owner, path, "org.a11y.atspi.Accessible", "GetRoleName")[0]
            interfaces = call(a11y, owner, path, "org.a11y.atspi.Accessible", "GetInterfaces")[0]
            nodes.append({"name": props.get("Name"), "role": role, "interfaces": interfaces, "depth": depth})
            if depth < 6:
                children = call(a11y, owner, path, "org.a11y.atspi.Accessible", "GetChildren")[0]
                pending.extend(((child_owner, child_path), depth + 1) for child_owner, child_path in children)
        result["nodes"] = nodes
        result["truncated"] = bool(pending)
        if not any(node["name"] == "Count click" for node in nodes):
            raise RuntimeError("fixture button is absent from the accessible tree")
        result["status"] = "PASS"
        (args.out / "probe.json").write_text(json.dumps(result, indent=2) + "\n")
        # Keep the owning PROGRAM alive for the test runner's observation.
        # Test teardown kills only this owned application group.
        signal.pause()
    except Exception as error:
        result["status"] = "FAIL"
        result["error"] = repr(error)
        (args.out / "probe.json").write_text(json.dumps(result, indent=2) + "\n")
        raise
    finally:
        if fixture is not None and fixture.poll() is None:
            fixture.terminate()
            fixture.wait(timeout=3)
        if registry is not None and registry.poll() is None:
            registry.terminate()
            registry.wait(timeout=3)
        if launcher is not None and launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=3)
        for bus in [a11y, session]:
            if bus:
                bus.close_sync(None)


if __name__ == "__main__":
    main()
