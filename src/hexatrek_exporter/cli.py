"""Local HexaTrek export workflow and explicitly confirmed anonymous session login."""

import argparse
import json
import os
from pathlib import Path
import sys

from hexatrek_exporter.summaries import normalize_language
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
    status.add_argument("--format", choices=("text", "json"), default="json",
                        help="status output format (default: json)")
    summaries = commands.add_parser("summaries", help="draft or explicitly generate POI summaries")
    summaries.add_argument("--stage", type=_stage, required=True)
    summaries.add_argument("--output", type=Path, required=True)
    summaries.add_argument("--language", type=normalize_language, default="de",
                           help="summary language (default: de)")
    summaries.add_argument("--generate", action="store_true",
                           help="generate summaries with the explicitly configured model endpoint")
    summaries.add_argument("--model", default=os.environ.get("HEXATREK_MODEL"),
                           help="model name (default: HEXATREK_MODEL; used only with --generate)")
    summaries.add_argument("--api-base", default=os.environ.get("HEXATREK_API_BASE"),
                           help="OpenAI-compatible API base (default: HEXATREK_API_BASE; used only with --generate)")
    adopt = commands.add_parser("adopt", help="copy an existing source snapshot into storage")
    adopt.add_argument("--manifest", required=True)
    login = commands.add_parser("session-login", help="reuse or create a private anonymous session")
    login.add_argument("--api-key", required=True)
    login.add_argument("--session-file", required=True)
    login.add_argument("--confirm", action="store_true", required=True,
                       help="confirm anonymous account creation if the session does not exist")
    update = commands.add_parser("update", help="update an exported stage without replacing its original snapshot")
    update.add_argument("--stage", type=_stage, nargs="+", required=True)
    update.add_argument("--session-file",
                        help="existing private session (required for a fresh update)")
    update.add_argument("--photo-mode", choices=("none", "files", "links"), default=None,
                        help="photo handling (default: inherit the existing export)")
    update.add_argument("--photo-quality", choices=("hi", "med", "lo"), default=None,
                        help="cached photos: hi 1000px/JPEG55, med 800px/JPEG50, lo 640px/JPEG45 "
                             "(longest side; default: inherit selected quality, otherwise med)")
    update.add_argument("--target", type=_targets, default=None,
                        help="export targets (default: inherit the existing export)")
    update.add_argument("--language", type=normalize_language, default=None,
                        help="summary language (default: inherit, otherwise de)")
    update.add_argument("--generate", action="store_true",
                        help="generate summaries with the explicitly configured model endpoint")
    update.add_argument("--model", default=os.environ.get("HEXATREK_MODEL"),
                        help="model name (default: HEXATREK_MODEL; used only with --generate)")
    update.add_argument("--api-base", default=os.environ.get("HEXATREK_API_BASE"),
                        help="OpenAI-compatible API base (default: HEXATREK_API_BASE; used only with --generate)")
    update.add_argument("--summaries-file", type=Path,
                        help="reviewed POI text JSON for exactly one stage")
    update.add_argument("--resume", help="technical update id to resume for exactly one stage")
    for name in ("collect", "refresh", "photos", "prepare", "export", "adopt-preparation"):
        command = commands.add_parser(name)
        command.add_argument("--stage", type=_stage,
                             **({} if name.startswith("adopt-") else {"nargs": "+"}),
                             required=True)
        if name == "collect":
            command.add_argument("--cache-path", required=True)
        elif name == "refresh":
            command.add_argument("--session-file", required=True)
            command.add_argument("--photo-mode", choices=("none", "files", "links"), default="none")
            command.add_argument("--photo-quality", choices=("hi", "med", "lo"), default="med",
                                 help="cached photos: hi 1000px/JPEG55, med 800px/JPEG50, lo 640px/JPEG45 "
                                      "(longest side; default: med)")
        elif name == "photos":
            command.add_argument("--session-file",
                                 help="existing private session (omit for offline cache compression)")
            command.add_argument("--photo-quality", choices=("hi", "med", "lo"), default="med",
                                 help="cached photos: hi 1000px/JPEG55, med 800px/JPEG50, lo 640px/JPEG45 "
                                      "(longest side; default: med)")
        elif name == "prepare":
            command.add_argument("--language", type=normalize_language, default="de",
                                 help="summary language (default: de)")
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
    if args.command == "summaries":
        return workflow.write_summaries(
            args.stage, args.output, language=args.language, generate=args.generate,
            model=args.model, api_base=args.api_base,
        )
    if args.command == "adopt-preparation":
        return workflow.adopt_preparation(args.stage, args.prepared_path)
    results = []
    for stage in dict.fromkeys(args.stage):
        if args.command == "collect":
            result = workflow.collect(stage, args.cache_path)
        elif args.command == "refresh":
            result = workflow.refresh(stage, args.session_file, args.photo_mode,
                                      photo_quality=args.photo_quality)
        elif args.command == "photos":
            result = workflow.photos(stage, session_file=args.session_file,
                                     photo_quality=args.photo_quality)
        elif args.command == "prepare":
            result = workflow.prepare(stage, summaries_file=args.summaries_file,
                                      include_photos=args.include_photos, language=args.language)
        elif args.command == "export":
            result = workflow.export(stage, args.target)
        elif args.command == "update":
            result = workflow.update(
                stage, session_file=args.session_file, photo_mode=args.photo_mode,
                photo_quality=args.photo_quality,
                targets=args.target, language=args.language, generate=args.generate,
                model=args.model, api_base=args.api_base,
                summaries_file=args.summaries_file, resume=args.resume,
            )
        results.append({"stage": stage, "result": result})
    return {"stages": results}


def _status_text(result: dict) -> str:
    lines = [f"Data root: {result['data_root']}", f"Exports root: {result['exports_root']}"]
    if not result["stages"]:
        lines.append("No stages collected.")
    for stage in result["stages"]:
        lines.extend(("", f"Stage {stage['stage']:02d}"))
        if "snapshot_path" in stage:
            lines.append(f"  Snapshot: {stage['routes']} routes, {stage['pois']} POIs")
            lines.append(f"  Snapshot path: {stage['snapshot_path']}")
            if stage.get("comments_coverage") is not None:
                lines.append(f"  Comments coverage: {stage['comments_coverage']}")
            if stage.get("comments_retrieved_at") is not None:
                lines.append(f"  Comments retrieved: {stage['comments_retrieved_at']}")
        else:
            lines.append("  Snapshot: missing")
        if "preparation_path" in stage:
            lines.append(f"  Preparation: available ({stage['preparation_path']})")
        else:
            lines.append("  Preparation: missing")
        if "summary_language" in stage:
            lines.append(f"  Summary language: {stage['summary_language']}")
        for update in stage.get("pending_updates", []):
            lines.append(f"  Update: {update['status']} (--resume {update['update_id']})")
        outputs = stage.get("outputs", {})
        if outputs:
            lines.append("  Visible exports:")
            for target, path in outputs.items():
                lines.append(f"    {target}: {path}")
        else:
            lines.append("  Visible exports: none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "prepare" and args.summaries_file is not None and len(set(args.stage)) != 1:
        parser.error("--summaries-file requires exactly one stage")
    if args.command == "update":
        if args.summaries_file is not None and len(set(args.stage)) != 1:
            parser.error("--summaries-file requires exactly one stage")
        if args.resume is not None and len(set(args.stage)) != 1:
            parser.error("--resume requires exactly one stage")
    try:
        result = _execute(args)
    except Exception as exc:
        print(f"hexatrek: {exc}", file=sys.stderr)
        return 1
    if args.command == "status" and args.format == "text":
        print(_status_text(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
