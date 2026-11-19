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
    parser.add_argument("--scenario", default="delayed_contact")
    parser.add_argument("--output", type=Path, default=Path("runs/delayed_contact"))
    parser.add_argument("--backend", choices=["ioa", "offline"], default="ioa")
    parser.add_argument("--planner", choices=["rule", "llm"], default=None)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    load_environment(args.env_file)
    result = run_experiment(load_scenario(args.scenario), args.output, backend=args.backend, planner=args.planner)
    print(json.dumps({k: result[k] for k in ("scenario", "outcome", "reason", "ended_at", "metrics", "calls", "messages")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
