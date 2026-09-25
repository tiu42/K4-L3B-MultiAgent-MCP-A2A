from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import anyio
import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


MAX_RECONNECTS = 3
RECONNECT_BACKOFF_SECONDS = 5.0
CONNECTION_ERRORS = (
    httpx2.TransportError, OSError, TimeoutError,
    anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream,
)


def _is_connection_error(exc: BaseException) -> bool:
    """True when every leaf of the (possibly grouped) exception is a transport failure."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_is_connection_error(e) for e in exc.exceptions)
    return isinstance(exc, CONNECTION_ERRORS)


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    done = {path.stem for path in output_root.glob("*.json")}
    if resume and trace_path.exists():
        # Drop events of cases that never produced an output (e.g. interrupted mid-case).
        kept = [line for line in trace_path.read_text(encoding="utf-8").splitlines(True)
                if line.strip() and json.loads(line).get("case_id") in done]
        trace_path.write_text("".join(kept), encoding="utf-8")
    pending = [case_id for case_id in case_set.case_ids if case_id not in done]
    trace = TraceWriter(trace_path, contracts)

    failures = 0
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    await _solve_one(case_set.cases[pending[0]], gateway, trace, contracts,
                                     output_root)
                    pending.pop(0)
                    failures = 0
        except BaseException as exc:
            trace.discard()  # the interrupted case leaves no partial trace
            if not _is_connection_error(exc) or failures >= MAX_RECONNECTS:
                raise
            failures += 1
            print(f"connection lost at {pending[0]}; reconnecting "
                  f"({failures}/{MAX_RECONNECTS})", file=sys.stderr)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS * failures)


async def _solve_one(case, gateway, trace: TraceWriter, contracts: Contracts,
                     output_root: Path) -> None:
    case_id = case["case_id"]
    trace.begin_case()
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    output = await solve_case(case, gateway, trace)
    contracts.validate_output(output, f"outputs/{case_id}.json")
    if output.get("case_id") != case_id:
        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
    target = output_root / f"{case_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
    trace.commit()  # before the output appears, so --resume can always drop orphan events
    temporary.replace(target)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--resume", action="store_true",
                     help="keep existing outputs/trace and only solve cases without output")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
