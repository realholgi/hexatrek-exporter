"""Local HexaTrek export workflow and explicitly confirmed anonymous session login."""

import argparse
import json
from pathlib import Path
import sys

from hexatrek_exporter.workflow import Workflow


def _stage(value: str) -> int:
    try:
        stage = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("stage must be a positive integer") from exc
    if stage < 1:
        raise argparse.ArgumentTypeError("stage must be a positive integer")
    return stage


def _targets(value: str) -> tuple[str, ...]:
    targets = tuple(value.split(","))
    if not targets or any(target not in ("osmand", "komoot") for target in targets):
        raise argparse.ArgumentTypeError("target must be osmand, komoot, or osmand,komoot")
    if len(set(targets)) != len(targets):
        raise argparse.ArgumentTypeError("targets must not be repeated")
    return targets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=None,
                        help="workflow storage (default: ./data)")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="show locally recorded stage state")
    status.add_argument("--stage", type=_stage, nargs="+")
    adopt = commands.add_parser("adopt", help="copy an existing source snapshot into storage")
    adopt.add_argument("--manifest", required=True)
    login = commands.add_parser("session-login", help="reuse or create a private anonymous session")
    login.add_argument("--api-key", required=True)
    login.add_argument("--session-file", required=True)
    login.add_argument("--confirm", action="store_true", required=True,
                       help="confirm anonymous account creation if the session does not exist")
    for name in ("collect", "refresh", "prepare", "export", "adopt-preparation"):
        command = commands.add_parser(name)
        command.add_argument("--stage", type=_stage,
                             **({} if name.startswith("adopt-") else {"nargs": "+"}),
                             required=True)
        if name == "collect":
            command.add_argument("--cache-path", required=True)
        elif name == "refresh":
            command.add_argument("--session-file", required=True)
            command.add_argument("--photo-mode", choices=("none", "files", "links"), default="none")
        elif name == "prepare":
            command.add_argument("--summaries-file", type=Path,
                                 help="complete POI text JSON for exactly one stage")
            command.add_argument("--include-photos", action="store_true",
                                 help="include existing local photos in Komoot detail archives")
        elif name == "export":
            command.add_argument("--target", type=_targets, default=("osmand", "komoot"))
        elif name == "adopt-preparation":
            command.add_argument("--prepared-path", required=True)
    return parser


def _execute(args) -> dict:
    if args.command == "session-login":
        from hexatrek_exporter.hexatrek_session import sign_in_anonymously

        return sign_in_anonymously(args.api_key, args.session_file)
    workflow = Workflow(args.data_root if args.data_root is not None else Path.cwd() / "data")
    if args.command == "status":
        return workflow.status(args.stage)
    if args.command == "adopt":
        return workflow.adopt_snapshot(args.manifest)
    if args.command == "adopt-preparation":
        return workflow.adopt_preparation(args.stage, args.prepared_path)
    results = []
    for stage in dict.fromkeys(args.stage):
        if args.command == "collect":
            result = workflow.collect(stage, args.cache_path)
        elif args.command == "refresh":
            result = workflow.refresh(stage, args.session_file, args.photo_mode)
        elif args.command == "prepare":
            result = workflow.prepare(stage, summaries_file=args.summaries_file,
                                      include_photos=args.include_photos)
        elif args.command == "export":
            result = workflow.export(stage, args.target)
        results.append({"stage": stage, "result": result})
    return {"stages": results}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "prepare" and args.summaries_file is not None and len(set(args.stage)) != 1:
        parser.error("--summaries-file requires exactly one stage")
    try:
        result = _execute(args)
    except Exception as exc:
        print(f"hexatrek: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
