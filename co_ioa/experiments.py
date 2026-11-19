"""Run saved scenarios over IoA services or offline and export an aggregate."""
import argparse
import json
from pathlib import Path
from .run import load_scenario
from .runner import run_experiment
from .config import load_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    from .methods import METHODS, require_runnable
    parser.add_argument("--method", choices=list(METHODS), default="co_ioa")
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runs/basic_experiments"))
    parser.add_argument("--backend", choices=["ioa", "offline"], default="ioa")
    parser.add_argument("--planner", choices=["rule", "llm"], default="rule")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--native-model")
    parser.add_argument("--native-embedding-model")
    parser.add_argument("--native-embedding-dimension", type=int)
    parser.add_argument("--native-max-turns", type=int, default=20)
    parser.add_argument("--native-wall-wait-seconds", type=float, default=1200)
    args = parser.parse_args()
    load_environment(args.env_file)
    require_runnable(args.method)
    names = sorted(path.stem for path in (Path(__file__).parent / "scenarios").glob("*.json"))
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    summary = []
    total = len(names) * args.repetitions
    for scenario_index, name in enumerate(names):
        for repetition in range(args.repetitions):
            scenario = load_scenario(name)
            scenario["seed"] = args.seed + repetition
            destination = args.output / name if args.repetitions == 1 else args.output / name / f"repeat_{repetition:03d}"
            print(f"Starting {name}/{repetition} ({scenario_index * args.repetitions + repetition + 1}/{total}); "
                  f"output={destination}", flush=True)
            result = run_experiment(scenario, destination, backend=args.backend, planner=args.planner,
                method=args.method, protocol=args.protocol, native_env_file=args.env_file,
                native_model=args.native_model, native_embedding_model=args.native_embedding_model,
                native_embedding_dimension=args.native_embedding_dimension,
                native_max_turns=args.native_max_turns,
                native_wall_wait_seconds=args.native_wall_wait_seconds)
            row = {"method": args.method, "comparison_ready": result["comparison_ready"], "scenario": name, "repetition": repetition, "seed": scenario["seed"],
                   "planner": result["planner"], "outcome": result["outcome"], "ended_at": result["ended_at"],
                   "reason": result["reason"], "error_kind": result["error_kind"],
                   "evaluation_valid": result["evaluation_valid"], **result["metrics"],
                   "unreleased_reservations": len(result["unreleased_reservations"]),
                   "pending_cleanup": len(result["pending_cleanup"])}
            judgement = result["external_judgement"]
            row.update(external_outcome=judgement["outcome"],
                       external_ended_at=judgement["ended_at"],
                       external_on_time_execution_completion_rate=judgement["on_time_execution_completion_rate"],
                       external_all_required_results_received_at=judgement["all_required_results_received_at"])
            summary.append(row)
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            print(f"{name}/{repetition}: {result['outcome']}; ended_at={result['ended_at']}; reason={result['reason']}", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
