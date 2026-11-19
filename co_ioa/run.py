import argparse
import json
from pathlib import Path
from .runner import run_experiment
from .config import load_environment


def load_scenario(name):
    path = Path(name)
    if not path.is_file():
        path = Path(__file__).parent / "scenarios" / (name + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="CO-IoA with IoA service infrastructure or offline transport")
    from .methods import METHODS, require_runnable, AdapterNotReadyError
    parser.add_argument("--method", choices=list(METHODS), default="co_ioa")
    parser.add_argument("--protocol", type=Path, help="Validate scenario against a frozen comparison protocol")
    parser.add_argument("--scenario", default="delayed_contact")
    parser.add_argument("--seed", type=int, help="Override the saved scenario seed for paired repetitions")
    parser.add_argument("--output", type=Path, default=Path("runs/delayed_contact"))
    parser.add_argument("--backend", choices=["ioa", "offline"], default="ioa")
    parser.add_argument("--planner", choices=["rule", "llm"], default=None)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--native-model", help="Original IoA chat model when --env-file does not specify one")
    parser.add_argument("--native-embedding-model")
    parser.add_argument("--native-embedding-dimension", type=int)
    parser.add_argument("--native-max-turns", type=int, default=20)
    parser.add_argument("--native-wall-wait-seconds", type=float, default=1200)
    args = parser.parse_args()
    try:
        require_runnable(args.method)
    except AdapterNotReadyError as exc:
        parser.error(str(exc))
    load_environment(args.env_file)
    scenario = load_scenario(args.scenario)
    if args.seed is not None:
        scenario["seed"] = args.seed
    if args.method == "ioa_original":
        print(f"Starting {scenario['name']}/0 (1/1); output={args.output}", flush=True)
    result = run_experiment(scenario, args.output, backend=args.backend, planner=args.planner,
        method=args.method, protocol=args.protocol, native_env_file=args.env_file,
        native_model=args.native_model, native_embedding_model=args.native_embedding_model,
        native_embedding_dimension=args.native_embedding_dimension,
        native_max_turns=args.native_max_turns,
        native_wall_wait_seconds=args.native_wall_wait_seconds)
    print(json.dumps({k: result[k] for k in ("method", "comparison_ready", "scenario", "outcome", "reason", "ended_at", "metrics", "calls", "messages")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
