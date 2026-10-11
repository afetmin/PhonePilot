import argparse
import json
import sys
from pathlib import Path

from .benchmark import benchmark
from .runner import PhoneRunner
from .schemas import RunRequest
from .settings import Settings
from .transport import PhoneTransport


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Phone Bridge + Jev task runner")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check configuration without calling a model")
    commands.add_parser("devices", help="Discover paired phones and apps")
    pair = commands.add_parser("pair", help="Begin or finish one-time Phone Bridge pairing")
    pair.add_argument("--address")
    pair.add_argument("--device-id")
    pair.add_argument("--confirm", action="store_true", help="Only after the user confirmed all pairing digits match")
    run = commands.add_parser("run", help="Run a JSON task until completion or an agent-context handoff")
    run.add_argument("task", type=Path)
    check = commands.add_parser("validate", help="Validate task JSON without phone or model calls")
    check.add_argument("task", type=Path)
    measure = commands.add_parser("benchmark", help="Read-only tree/both latency benchmark on a paired phone")
    measure.add_argument("--device-id", required=True)
    measure.add_argument("--samples", type=int, default=20)
    measure.add_argument("--output", type=Path)
    commands.add_parser("mcp", help="Start the persistent task-level MCP stdio server")
    args = parser.parse_args()
    settings = Settings.load()
    try:
        if args.command == "doctor":
            emit({"python": sys.version.split()[0], "jev_model": settings.model,
                  "typesafe_key_configured": bool(settings.api_key), "phone_config": str(settings.config),
                  "phone_config_exists": settings.config.exists(), "runs": str(settings.runs),
                  "ready": bool(settings.api_key and settings.config.exists())})
        elif args.command == "validate":
            task = RunRequest.model_validate_json(args.task.read_text(encoding="utf-8"))
            emit({"valid": True, "subgoals": len(task.subgoals)})
        elif args.command == "pair":
            if not args.address and not args.device_id:
                parser.error("pair needs --address or --device-id")
            emit(PhoneTransport(settings.config).connect(args.address, args.device_id, args.confirm))
        elif args.command == "devices":
            with PhoneRunner(settings) as runner:
                emit(runner.devices())
        elif args.command == "run":
            request = RunRequest.model_validate_json(args.task.read_text(encoding="utf-8"))
            with PhoneRunner(settings) as runner:
                state = runner.run(request)
                emit(state)
                while state["status"] == "running":
                    state = runner.wait(state["task_id"])
                    emit(state)
                if state["status"] != "completed":
                    raise SystemExit(2)
        elif args.command == "benchmark":
            if not 1 <= args.samples <= 100:
                parser.error("samples must be between 1 and 100")
            emit(benchmark(PhoneTransport(settings.config), args.device_id, args.samples,
                           args.output or settings.runs / "benchmark.json"))
        else:
            from .mcp_server import main as serve

            serve()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ValueError as error:
        emit({"error": "INVALID_REQUEST", "message": str(error)})
        raise SystemExit(2) from None
    except Exception as error:
        emit({"error": "COMMAND_FAILED", "error_type": type(error).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
