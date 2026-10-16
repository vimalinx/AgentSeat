"""Command-line parsing and dispatch."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import (
    actions,
    common,
    diagnostics,
    i18n,
    instances,
    lifecycle,
    observe,
    paths,
    procs,
    semantic,
    transport,
)
from .i18n import _


def main() -> int:
    cli_language = i18n.language_override(sys.argv[1:])
    i18n.configure_i18n(cli_language or os.environ.get("AGENTSEAT_LANGUAGE", "auto"))
    common.PREFERENCES = common.load_preferences()
    i18n.configure_i18n(
        cli_language
        or os.environ.get("AGENTSEAT_LANGUAGE")
        or str(common.PREFERENCES["language"])
    )
    argparse._ = _
    parser = argparse.ArgumentParser(
        prog="agentseat",
        description=_("Operate one local application with a private AI pointer and keyboard."),
    )
    parser.add_argument(
        "--language",
        type=i18n.language_option,
        metavar="LANG",
        help=_("interface language: auto, en, zh-CN, or zh-TW"),
    )
    parser.add_argument(
        "--instance",
        type=instances.normalize_instance_id,
        metavar="ID",
        help=_("operate one named AgentSeat application instance"),
    )
    parser.add_argument("--controller", help="stable identity for this application's write controller")
    commands = parser.add_subparsers(dest="command", required=True)
    control_parser = commands.add_parser("control", help="inspect, acquire or release the application write lease")
    control_parser.add_argument("action", choices=("status", "acquire", "release"), nargs="?", default="status")
    window_parser = commands.add_parser("window", help="change a real application window")
    window_commands = window_parser.add_subparsers(dest="action", required=True)
    for action_name, parameters in (("focus", ()), ("close", ()), ("move", ("x", "y")), ("resize", ("width", "height"))):
        action_parser = window_commands.add_parser(action_name)
        action_parser.add_argument("window_id")
        for name in parameters:
            action_parser.add_argument(name, type=int)
    request_parser = commands.add_parser("receipt", help="read a retained write receipt without replaying it")
    request_parser.add_argument("request_id", type=int)
    request_parser.add_argument("--owner", help="controller identity that submitted the request")

    start_parser = commands.add_parser("start", help=_("start a legacy verified demo"))
    start_parser.add_argument("preset", choices=("onlyoffice",), nargs="?", default="onlyoffice")
    start_parser.add_argument(
        "--host-workspace",
        type=int,
        default=common.PREFERENCES["default_workspace"],
        help=_("place the outer window on this host workspace"),
    )
    run_parser = commands.add_parser("run", help=_("wrap an arbitrary application command"))
    run_parser.add_argument(
        "--x11",
        action="store_true",
        help=_("force the generic private-XWayland compatibility lane"),
    )
    run_parser.add_argument(
        "--private-dbus",
        action="store_true",
        help=_("run the application on a private D-Bus session bus"),
    )
    run_parser.add_argument("--ime", choices=("auto", "simple", "fcitx5", "desktop", "inherit"), default="auto", help="input method: auto preserves user settings; fcitx5 starts an explicit private copy")
    run_parser.add_argument("--a11y", action="store_true", help="enable private accessibility reads and guarded actions")
    run_parser.add_argument("--cwd", help=_("working directory for the wrapped application"))
    run_parser.add_argument(
        "--host-workspace",
        type=int,
        default=common.PREFERENCES["default_workspace"],
        help=_("place the outer window on this host workspace"),
    )
    run_parser.add_argument("argv", nargs=argparse.REMAINDER, help=_("application command after --"))
    launch_parser = commands.add_parser(
        "launch",
        help=_("wrap an application in a new automatically named instance"),
    )
    launch_parser.add_argument(
        "--x11",
        action="store_true",
        help=_("force the generic private-XWayland compatibility lane"),
    )
    launch_parser.add_argument(
        "--private-dbus",
        action="store_true",
        help=_("run the application on a private D-Bus session bus"),
    )
    launch_parser.add_argument("--ime", choices=("auto", "simple", "fcitx5", "desktop", "inherit"), default="auto", help="input method: auto preserves user settings; fcitx5 starts an explicit private copy")
    launch_parser.add_argument("--a11y", action="store_true", help="enable private accessibility reads and guarded actions")
    launch_parser.add_argument("--cwd", help=_("working directory for the wrapped application"))
    launch_parser.add_argument(
        "--host-workspace",
        default="current",
        metavar="N|current",
        help=_("place the outer window on the current or named host workspace"),
    )
    launch_parser.add_argument("argv", nargs=argparse.REMAINDER, help=_("application command after --"))
    commands.add_parser("version", help=_("show the controller and protocol version"))
    commands.add_parser("paths", help=_("show AgentSeat's XDG storage locations"))
    commands.add_parser("config", help=_("show the effective AgentSeat configuration"))
    commands.add_parser("status", help=_("show the current AgentSeat session"))
    commands.add_parser("instances", help=_("list named AgentSeat application instances"))
    heartbeat_parser = commands.add_parser(
        "heartbeat",
        help=_("verify that the current application session can be reconnected"),
    )
    heartbeat_parser.add_argument(
        "--expect-session",
        help=_("fail if the connected session does not have this identity"),
    )
    watch_parser = commands.add_parser(
        "watch",
        help=_("continuously reconnect and stream session heartbeats"),
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help=_("seconds between reconnect checks"),
    )
    watch_parser.add_argument(
        "--count",
        type=int,
        default=0,
        help=_("number of checks; zero keeps watching until interrupted"),
    )
    watch_parser.add_argument(
        "--expect-session",
        help=_("fail if the connected session does not have this identity"),
    )
    hud_parser = commands.add_parser("hud", help=_("control the embedded private-microhost AI head"))
    hud_parser.add_argument("action", choices=("start", "stop", "status"))
    commands.add_parser("collaboration", help=_("show human-priority and AI activity state"))
    activity_parser = commands.add_parser("activity", help=_("publish the Agent's current AgentSeat action"))
    activity_parser.add_argument("summary")
    activity_parser.add_argument("--detail", default="")
    messages_parser = commands.add_parser("messages", help=_("read messages sent from the eye HUD"))
    messages_parser.add_argument("--after", type=int, default=0)
    reply_parser = commands.add_parser("reply", help=_("reply to the human in the eye HUD"))
    reply_parser.add_argument("text")
    a11y_parser = commands.add_parser("a11y", help="scoped controls, native semantic actions and condition waits")
    a11y_parser.add_argument("action", choices=("status", "snapshot", "query", "get", "text", "text-state", "perform", "set-text", "set-value", "focus", "select", "scroll-to", "subscribe", "events", "wait", "wait-start", "wait-get", "wait-cancel"))
    a11y_parser.add_argument("value", nargs="?")
    a11y_parser.add_argument("argument", nargs="?")
    a11y_parser.add_argument("--snapshot-id")
    a11y_parser.add_argument("--root-ref")
    a11y_parser.add_argument("--max-nodes", type=int, default=512)
    a11y_parser.add_argument("--max-depth", type=int, default=16)
    a11y_parser.add_argument("--offset", type=int, default=0)
    a11y_parser.add_argument("--limit", type=int, default=64)
    a11y_parser.add_argument("--exact", action="store_true")
    a11y_parser.add_argument("--role")
    a11y_parser.add_argument("--interface")
    a11y_parser.add_argument("--expect-scope")
    a11y_parser.add_argument("--state", action="append", default=[])
    a11y_parser.add_argument("--start", type=int, default=0)
    a11y_parser.add_argument("--count", type=int, default=1024)
    a11y_parser.add_argument("--after", type=int, default=0)
    a11y_parser.add_argument("--timeout-ms", type=int, default=5000)
    a11y_parser.add_argument("--condition", help="JSON condition for wait or optional post-action verification")
    commands.add_parser("windows", help=_("list windows in the wrapped application tree"))
    commands.add_parser("pause", help=_("pause Agent input"))
    commands.add_parser("resume", help=_("resume Agent input"))
    move_parser = commands.add_parser("move", help=_("move the private pointer using normalized coordinates"))
    move_parser.add_argument("x", type=float)
    move_parser.add_argument("y", type=float)
    click_parser = commands.add_parser("click", help=_("click with the private pointer"))
    click_parser.add_argument("button", type=int, nargs="?", default=272)
    scroll_parser = commands.add_parser("scroll", help=_("scroll inside the wrapped application"))
    scroll_parser.add_argument("axis", choices=("vertical", "horizontal"))
    scroll_parser.add_argument("value120", type=int)
    key_parser = commands.add_parser("key", help=_("send one scoped evdev key event"))
    key_parser.add_argument("keycode", type=int)
    key_parser.add_argument("state", choices=("down", "up"))
    type_parser = commands.add_parser("type", help=_("type text with the private keyboard"))
    type_parser.add_argument("text")
    type_parser.add_argument(
        "--keys", action="store_true",
        help=_("send key events instead of a private paste, for applications that ignore paste"))
    observe_parser = commands.add_parser("observe", help=_("capture the private application output"))
    observe_parser.add_argument("path", nargs="?")
    doctor_parser = commands.add_parser("doctor", help="diagnose dependencies, scope, mapping and degradations")
    doctor_parser.add_argument("--probe-capture", action="store_true", help="time one private frame capture")
    commands.add_parser("capabilities", help="list implemented, exposed, authorized and available capabilities")
    wait_change_parser = commands.add_parser("wait-change", help="wait until the application output changes")
    wait_change_parser.add_argument("--since", type=int, help="content_serial from observe; default is now")
    wait_change_parser.add_argument("--timeout-ms", type=int, default=5000)
    wait_change_parser.add_argument("--settle-ms", type=int, default=0, help="then wait until changes stop for this long")
    wait_change_parser.add_argument("--region", type=float, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    wait_change_parser.add_argument("--min-area-ratio", type=float, default=0.0)
    wait_change_parser.add_argument("--min-extent", type=float, default=observe.WAIT_CHANGE_DEFAULT_MIN_EXTENT,
        help="minimum damage width and height in logical pixels")
    commands.add_parser("stop", help=_("stop the current AgentSeat session"))
    args = parser.parse_args()
    if args.controller:
        if len(args.controller) > 128 or "\0" in args.controller:
            raise common.AgentSeatError("controller identity must be a NUL-free string of at most 128 characters")
        transport.RPC_CONTROLLER_ID = args.controller


    if args.command in {"launch", "instances"} and args.instance:
        parser.error(_("--instance cannot be combined with {command}").format(command=args.command))
    if args.instance and paths.SELECTED_INSTANCE != args.instance:
        instances.execute_for_instance(args.instance, sys.argv[1:])
        return 0

    if args.command == "launch":
        application_argv = common.normalize_argv(args.argv)
        instance_id = instances.allocate_instance_id(application_argv)
        forwarded: list[str] = []
        if args.language:
            forwarded.extend(["--language", str(args.language)])
        forwarded.extend(["--instance", instance_id, "run"])
        if args.x11:
            forwarded.append("--x11")
        if args.private_dbus:
            forwarded.append("--private-dbus")
        if args.a11y:
            forwarded.append("--a11y")
        forwarded.extend(["--ime", args.ime])
        if args.cwd:
            forwarded.extend(["--cwd", args.cwd])
        forwarded.extend([
            "--host-workspace",
            str(instances.launch_workspace(args.host_workspace)),
            "--",
            *application_argv,
        ])
        instances.execute_for_instance(instance_id, forwarded)
        return 0
    if args.command == "start":
        argv, mode, compat_type = lifecycle.legacy_command(args.preset)
        result = lifecycle.start_safely(
            argv,
            mode=mode,
            cwd=common.normalized_cwd(None),
            host_workspace=args.host_workspace,
            legacy_preset=args.preset,
            compat_type=compat_type,
            ime=lifecycle.resolve_ime("auto"),
        )
    elif args.command == "run":
        result = lifecycle.start_safely(
            common.normalize_argv(args.argv),
            mode="x11" if args.x11 else "wayland",
            cwd=common.normalized_cwd(args.cwd),
            host_workspace=args.host_workspace,
            private_dbus=args.private_dbus,
            a11y=args.a11y,
            ime=lifecycle.resolve_ime(args.ime, args.private_dbus),
        )
    elif args.command == "version":
        result = transport.version()
    elif args.command == "paths":
        result = common.storage_paths()
    elif args.command == "config":
        result = common.configuration_status()
    elif args.command == "status":
        result = lifecycle.status()
    elif args.command == "instances":
        result = instances.list_instances()
    elif args.command == "heartbeat":
        result = lifecycle.session_heartbeat(args.expect_session)
    elif args.command == "watch":
        lifecycle.watch_session(
            interval=args.interval,
            count=args.count,
            expected_session_id=args.expect_session,
        )
        return 0
    elif args.command == "hud":
        if args.action == "start":
            result = lifecycle.start_hud()
        elif args.action == "stop":
            result = lifecycle.stop_hud()
        else:
            result = {
                "running": procs.eye_pid() is not None,
                "pid": procs.eye_pid(),
                "connected": paths.CONTROL_SOCKET.exists(),
                "enabled_by_config": bool(common.PREFERENCES["eye_hud"]),
                "scope": "private-microhost",
                "default_corner": "bottom-right",
            }
    elif args.command == "window":
        parameters = {name: getattr(args, name) for name in ("window_id", "x", "y", "width", "height") if hasattr(args, name)}
        result = actions.window_action("window." + args.action, parameters)
    elif args.command == "control":
        if args.action == "release":
            result = transport.release_write_control()
        else:
            result = transport.rpc("control." + args.action)
    elif args.command == "receipt":
        result = transport.rpc("request.get", {"controller_id": args.owner or transport.RPC_CONTROLLER_ID, "request_id": args.request_id})
    elif args.command == "collaboration":
        result = semantic.collaboration_status()
    elif args.command == "activity":
        result = semantic.set_activity(args.summary, args.detail)
    elif args.command == "messages":
        result = semantic.list_messages(args.after)
    elif args.command == "reply":
        result = semantic.reply_message(args.text)
    elif args.command == "a11y":
        condition = json.loads(args.condition) if args.condition else None
        semantic.EXPECTED_A11Y_SCOPE = args.expect_scope
        if args.action in {"perform", "set-text", "set-value", "focus", "select", "scroll-to"}:
            if not args.value: raise common.AgentSeatError("node_ref is required")
            params = {"node_ref": args.value, "timeout_ms": args.timeout_ms}
            if args.action == "perform":
                if args.argument is None: raise common.AgentSeatError("an actual action name or index is required; inspect a11y get first")
                params["action"] = int(args.argument) if args.argument.isdecimal() else args.argument
            elif args.action == "set-text":
                if args.argument is None: raise common.AgentSeatError("text is required")
                params["text"] = args.argument
            elif args.action == "set-value": params["value"] = float(args.argument)
            elif args.action == "select": params["index"] = int(args.argument)
            result = semantic.semantic_action("a11y." + args.action.replace("-", "_"), params)
            if condition is not None and result["execution"] == "finished":
                result["postcondition"] = semantic.semantic_wait(condition, args.timeout_ms)
                result["verification"] = "condition_satisfied" if result["postcondition"]["status"] == "satisfied" else "condition_not_satisfied"
        elif args.action in {"wait", "wait-start"}:
            if condition is None: raise common.AgentSeatError("--condition JSON is required")
            result = semantic.semantic_wait(condition, args.timeout_ms) if args.action == "wait" else semantic.a11y_read("wait.start", condition=condition, timeout_ms=args.timeout_ms)
        elif args.action in {"wait-get", "wait-cancel"}:
            result = semantic.a11y_read(args.action.replace("-", "."), wait_id=args.value)
        elif args.action == "subscribe": result = semantic.a11y_read("events.subscribe")
        elif args.action == "events": result = semantic.a11y_read("events.read", after=args.after, limit=args.limit)
        elif args.action == "text": result = semantic.a11y_read("text", value=args.value, start=args.start, count=args.count)
        elif args.action == "text-state": result = semantic.a11y_read("text_state", value=args.value)
        elif args.action == "get": result = semantic.a11y_read("get", value=args.value)
        elif args.action == "status": result = semantic.a11y_read("status")
        else:
            params = {"limit": args.limit, "offset": args.offset, "max_nodes": args.max_nodes, "max_depth": args.max_depth}
            if args.snapshot_id: params["snapshot_id"] = args.snapshot_id
            if args.root_ref: params["root_ref"] = args.root_ref
            if args.action == "query":
                if args.exact or args.role or args.state or args.interface:
                    params["selector"] = {"name": args.value or "", "exact": args.exact, "role": args.role, "states": args.state, "interface": args.interface}
                else: params["value"] = args.value
            result = semantic.a11y_read(args.action, **params)
    elif args.command == "windows":
        result = actions.sync_window_tree()
    elif args.command == "pause":
        result = actions.pause_seat()
    elif args.command == "resume":
        result = transport.rpc("seat.resume")
    elif args.command == "move":
        result = actions.pointer_move(args.x, args.y)
    elif args.command == "click":
        result = actions.pointer_click(args.button)
    elif args.command == "scroll":
        actions.sync_window_tree()
        result = actions.pointer_scroll(args.axis, args.value120)
    elif args.command == "key":
        result = actions.keyboard_key(args.keycode, args.state == "down")
    elif args.command == "type":
        actions.sync_window_tree()
        try:
            result = actions.type_text(args.text, keys=args.keys)
        except common.AgentSeatError as exc:
            if args.keys or not str(exc).startswith("UNSUPPORTED_CHARACTER:"):
                raise
            result = actions.private_unicode_paste(args.text)
    elif args.command == "observe":
        result = observe.observe(args.path)
    elif args.command == "doctor":
        result = diagnostics.doctor(args.probe_capture)
    elif args.command == "capabilities":
        result = diagnostics.capabilities()
    elif args.command == "wait-change":
        result = observe.wait_for_change(args.since, args.timeout_ms, tuple(args.region) if args.region else None,
            args.min_area_ratio, args.settle_ms, args.min_extent)
    elif args.command == "stop":
        result = lifecycle.stop()
    else:
        raise common.AgentSeatError(_("unknown command"))
    common.output_json(result)
    return 0
