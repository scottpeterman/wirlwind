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
    parser.add_argument("--templates", default=None, help="Path to custom templates directory")
    parser.add_argument("--legacy", action="store_true", default=True, help="Enable legacy cipher/KEX support (default: on)")
    parser.add_argument("--no-legacy", dest="legacy", action="store_false", help="Disable legacy cipher support")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Prompt for password if needed
    password = args.password
    if not password and not args.key:
        import getpass
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

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
