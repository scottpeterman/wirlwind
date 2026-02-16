"""
Standalone launcher for Wirlwind Telemetry.

Usage:
    python -m wirlwind_telemetry --host 10.0.0.1 --vendor cisco_ios_xe --user admin
    python -m wirlwind_telemetry --host router1.lab --vendor arista_eos --user admin --key ~/.ssh/id_rsa
"""

import sys
import argparse
import logging

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from .auth_interface import SimpleAuthProvider, DeviceTarget
from .widget import TelemetryWidget

logger = logging.getLogger(__name__)


# ── Preflight ────────────────────────────────────────────────────────

def preflight_check(vendor: str, template_dir: str = None, verbose: bool = False):
    """
    Validate parser chain readiness before connecting to a device.

    Checks:
      1. Required Python packages (textfsm, ntc-templates)
      2. Collection configs exist for the target vendor
      3. Every TextFSM template referenced in configs resolves to a real file
      4. Schema files exist for each collection
      5. Vendor driver is registered

    Returns True if all critical checks pass. Warnings are non-fatal.
    """
    from pathlib import Path

    ok = True
    warnings = []
    errors = []

    log = logger.info if verbose else logger.debug

    log("─" * 60)
    log("Preflight check: parser chain readiness")
    log("─" * 60)

    # ── 1. Package availability ──────────────────────────────────

    try:
        import textfsm
        log("  ✓ textfsm installed")
    except ImportError:
        warnings.append("textfsm not installed — TextFSM parser unavailable, regex fallback only")

    ntc_templates_path = None
    try:
        import ntc_templates
        ntc_templates_path = Path(ntc_templates.__file__).parent / "templates"
        if ntc_templates_path.exists():
            count = len(list(ntc_templates_path.glob("*.textfsm")))
            log(f"  ✓ ntc-templates installed ({count} templates at {ntc_templates_path})")
        else:
            warnings.append(f"ntc-templates installed but templates dir missing: {ntc_templates_path}")
            ntc_templates_path = None
    except ImportError:
        warnings.append("ntc-templates not installed — no community TextFSM templates available")

    try:
        from ttp import ttp
        log("  ✓ ttp installed")
    except ImportError:
        log("  · ttp not installed (optional)")

    # ── 2. Vendor driver ─────────────────────────────────────────

    from .drivers import get_driver, list_drivers, BaseDriver
    driver = get_driver(vendor)
    if isinstance(driver, BaseDriver):
        warnings.append(
            f"No dedicated driver for '{vendor}' — using BaseDriver. "
            f"Registered drivers: {list(list_drivers().keys())}"
        )
    else:
        log(f"  ✓ Vendor driver: {driver}")

    # ── 3. Collection configs ────────────────────────────────────

    from .parser_chain import CollectionLoader, ParserChain, TemplateResolver

    loader = CollectionLoader()
    collections = loader.list_collections(vendor)

    if not collections:
        errors.append(f"No collection configs found for vendor '{vendor}'")
    else:
        log(f"  ✓ {len(collections)} collection configs for {vendor}: {', '.join(collections)}")

    # ── 4. Template resolution ───────────────────────────────────

    search_paths = []
    if template_dir:
        search_paths.append(template_dir)

    local_fsm = Path(__file__).parent / "templates" / "textfsm"
    if local_fsm.exists():
        search_paths.insert(0, str(local_fsm))
        log(f"  ✓ Local TextFSM overrides: {local_fsm}")

    resolver = TemplateResolver(search_paths)

    for collection in collections:
        config = loader.get_config(collection, vendor)
        if not config:
            continue

        schema = loader.get_schema(collection)
        if not schema:
            warnings.append(f"  [{collection}] missing _schema.yaml — no type coercion")

        for parser_def in config.get("parsers", []):
            ptype = parser_def.get("type", "")

            if ptype in ("textfsm", "ttp"):
                templates = parser_def.get("templates", [])
                for tname in templates:
                    resolved = resolver.resolve(tname)
                    if resolved:
                        log(f"  ✓ [{collection}] {ptype} → {tname}")
                        log(f"      resolved: {resolved}")
                    else:
                        errors.append(
                            f"  [{collection}] {ptype} template NOT FOUND: {tname}"
                        )
                        if ntc_templates_path:
                            _suggest_match(tname, ntc_templates_path, errors)

            elif ptype == "regex":
                pattern = parser_def.get("pattern", "")
                if pattern:
                    log(f"  ✓ [{collection}] regex fallback defined")
                else:
                    warnings.append(f"  [{collection}] regex parser has no pattern")

    # ── 5. Normalize map sanity ──────────────────────────────────

    for collection in collections:
        config = loader.get_config(collection, vendor)
        if not config:
            continue

        norm = config.get("normalize")
        if not norm:
            log(f"  · [{collection}] no normalize map (fields pass through as-is)")
            continue

        schema = loader.get_schema(collection)
        if schema:
            schema_fields = set(schema.get("fields", {}).keys())
            norm_targets = set(norm.keys())
            unmapped = schema_fields - norm_targets
            computed = {"used_pct", "processes"}
            unmapped = unmapped - computed
            if unmapped:
                log(f"  · [{collection}] schema fields not in normalize map "
                    f"(may be computed): {unmapped}")

    # ── Report ───────────────────────────────────────────────────

    _report(errors, warnings, verbose)

    if errors:
        ok = False

    return ok


def _suggest_match(target: str, ntc_path, errors: list):
    """Suggest similar template names from ntc-templates."""
    from pathlib import Path

    parts = target.replace(".textfsm", "").split("_show_", 1)
    if len(parts) == 2:
        platform = parts[0]
        candidates = sorted(ntc_path.glob(f"{platform}_show_*"))
        cmd_words = parts[1].replace("_", " ").split()
        matches = []
        for c in candidates:
            cname = c.name.replace(".textfsm", "")
            if all(w in cname for w in cmd_words[:2]):
                matches.append(c.name)

        if matches:
            errors.append(f"      did you mean: {matches[0]}")
            for m in matches[1:3]:
                errors.append(f"                    {m}")


def _report(errors: list, warnings: list, verbose: bool):
    """Print preflight results."""
    log = logger.info if verbose else logger.debug

    if warnings:
        log("")
        for w in warnings:
            logger.warning(f"  ⚠ {w}")

    if errors:
        log("")
        for e in errors:
            logger.error(f"  ✗ {e}")
        log("")
        logger.error("Preflight: template resolution errors detected — "
                      "TextFSM may fall back to regex")
    else:
        log("")
        log("Preflight: all templates resolved ✓")

    log("─" * 60)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Wirlwind Telemetry — Real-time network device dashboard"
    )
    parser.add_argument("--host", required=True, help="Device hostname or IP")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--vendor", required=True,
                        choices=["cisco_ios_xe", "cisco_nxos", "arista_eos", "juniper_junos"],
                        help="Device vendor/platform")
    parser.add_argument("--user", required=True, help="SSH username")
    parser.add_argument("--password", default=None, help="SSH password (will prompt if not provided)")
    parser.add_argument("--key", default=None, help="Path to SSH private key")
    parser.add_argument("--name", default=None, help="Display name for device")
    parser.add_argument("--templates", default=None, help="Path to custom TextFSM templates directory")
    parser.add_argument("--legacy", action="store_true", default=True, help="Enable legacy cipher/KEX support (default: on)")
    parser.add_argument("--no-legacy", dest="legacy", action="store_false", help="Disable legacy cipher support")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Run preflight checks and exit (no connection)")
    parser.add_argument("--remote-debugging-port", type=int, default=None,
                        help="Enable QtWebEngine remote debugging on this port (e.g., 9222)")

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Preflight check ──────────────────────────────────────────
    preflight_ok = preflight_check(
        vendor=args.vendor,
        template_dir=args.templates,
        verbose=args.debug or args.preflight_only,
    )

    if args.preflight_only:
        sys.exit(0 if preflight_ok else 1)

    # Prompt for password if needed
    password = args.password
    if not password and not args.key:
        import getpass
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

    # Remote debugging
    if args.remote_debugging_port:
        import os
        os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = str(args.remote_debugging_port)
        logging.info(f"Remote debugging enabled: http://127.0.0.1:{args.remote_debugging_port}")

    # Create app
    app = QApplication(sys.argv)
    app.setApplicationName("Wirlwind Telemetry")
    app.setStyle("Fusion")

    # Dark palette
    from PyQt6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(10, 14, 20))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(200, 214, 229))
    palette.setColor(QPalette.ColorRole.Base, QColor(17, 24, 32))
    palette.setColor(QPalette.ColorRole.Text, QColor(200, 214, 229))
    palette.setColor(QPalette.ColorRole.Button, QColor(21, 29, 39))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(200, 214, 229))
    app.setPalette(palette)

    # Auth
    auth = SimpleAuthProvider(
        username=args.user,
        password=password,
        key_path=args.key,
    )

    # Target
    target = DeviceTarget(
        hostname=args.host,
        port=args.port,
        display_name=args.name or args.host,
        vendor=args.vendor,
    )

    # Widget
    widget = TelemetryWidget(
        auth_provider=auth,
        template_dir=args.templates,
        legacy_mode=args.legacy,
    )
    widget.setWindowTitle(f"Wirlwind Telemetry — {target.display_name}")
    widget.resize(1400, 900)
    widget.show()

    # Start telemetry
    widget.start(target)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
