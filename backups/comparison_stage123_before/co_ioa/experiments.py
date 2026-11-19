"""Run saved scenarios over IoA services or offline and export an aggregate."""
import argparse
import json
from pathlib import Path
from .run import load_scenario
from .runner import run_experiment
from .config import load_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/basic_experiments"))
    parser.add_argument("--backend", choices=["ioa", "offline"], default="ioa")
    parser.add_argument("--planner", choices=["rule", "llm"], default="rule")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    load_environment(args.env_file)
    names = sorted(path.stem for path in (Path(__file__).parent / "scenarios").glob("*.json"))
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    summary = []
    for name in names:
        for repetition in range(args.repetitions):
            scenario = load_scenario(name)
            scenario["seed"] = args.seed + repetition
            destination = args.output / name if args.repetitions == 1 else args.output / name / f"repeat_{repetition:03d}"
            result = run_experiment(scenario, destination, backend=args.backend, planner=args.planner)
            row = {"scenario": name, "repetition": repetition, "seed": scenario["seed"],
                   "planner": result["planner"], "outcome": result["outcome"], "ended_at": result["ended_at"],
                   "reason": result["reason"], "error_kind": result["error_kind"],
                   "evaluation_valid": result["evaluation_valid"], **result["metrics"],
                   "unreleased_reservations": len(result["unreleased_reservations"]),
                   "pending_cleanup": len(result["pending_cleanup"])}
            summary.append(row)
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            print(f"{name}/{repetition}: {result['outcome']}; ended_at={result['ended_at']}; reason={result['reason']}", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
