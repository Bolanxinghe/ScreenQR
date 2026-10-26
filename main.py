"""ScreenQR desktop launcher and local image command line reader."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from screenqr import __version__ as VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ScreenQR — local Windows screen QR reader")
    parser.add_argument("--version", action="version", version=f"ScreenQR {VERSION}")
    parser.add_argument("--image", metavar="PATH", help="decode a local image and print JSON; never open its contents")
    parser.add_argument("--diagnostics-file", metavar="PATH", help="opt-in fixed native error codes only; overwrite one result, never save QR contents")
    parser.add_argument('--environment-check',action='store_true',help='read-only Windows/display/WeChat version report; no screen capture or input')
    parser.add_argument('--environment-report',metavar='PATH',help='write one local read-only environment report; never overwrite an existing file')
    parser.add_argument('--save-environment-report',action='store_true',help='choose a local file for an environment report; no Python installation needed')
    parser.add_argument('--measurement-file',metavar='PATH',help='opt-in local numeric timings for public test samples only; no payloads or pixels')
    parser.add_argument('--measurement-run',metavar='HEX16',help='public test run identifier, used only with --measurement-file')
    args = parser.parse_args(argv)
    if args.save_environment_report:
        from screenqr.reporting import save_dialog
        return 0 if save_dialog() else 1
    if args.environment_check or args.environment_report:
        from screenqr.reporting import collect,save_report
        try:
            report=save_report(args.environment_report) if args.environment_report else collect()
            print(json.dumps(report,ensure_ascii=True))
            return 0
        except Exception:
            print(json.dumps(dict(error='environment_query_unavailable',screen_captured=False,input_sent=False)))
            return 1
    if args.image:
        try:
            from screenqr.core import decode_image
            from screenqr.image_io import load_image_for_decode

            with load_image_for_decode(args.image) as image:
                results = decode_image(image)
            print(json.dumps([asdict(result) for result in results], ensure_ascii=True))
            return 0 if results else 2
        except Exception as exc:
            print(f"ScreenQR: {exc}", file=sys.stderr)
            return 1
    from screenqr.app import launch

    from screenqr.diagnostics import NativeDiagnostics
    measurement=None
    if args.measurement_file or args.measurement_run:
        if not args.measurement_file or not args.measurement_run:
            parser.error('measurement requires both file and run identifier')
        from screenqr.measurement import Measurement
        measurement=Measurement(args.measurement_file,args.measurement_run)
    launch(diagnostics=NativeDiagnostics(args.diagnostics_file) if args.diagnostics_file else None,measurement=measurement)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
