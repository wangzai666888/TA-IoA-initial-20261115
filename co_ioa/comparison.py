"""Validate frozen stage 1-3 inputs and display method readiness (no model calls)."""
import argparse
import json
from pathlib import Path
from .comparison_protocol import DEFAULT_PROTOCOL, load_protocol, validate_files, model_settings, validate_formal_budget
from .methods import method_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--env-file", type=Path, help="Check non-secret model settings for formal protocol")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    protocol = load_protocol(args.protocol)
    mismatches = validate_files(protocol, args.upstream_root)
    methods = method_catalog()
    native_remaining = next(m["missing_steps"] for m in methods if m["method_id"] == "ioa_original")
    # The frozen protocol records gates as they stood when stages 1-3 were
    # frozen; show live implementation progress without rewriting that input.
    gate_names = {4: "native_simulation_tools", 5: "native_clock",
                  6: "native_window_routing", 7: "external_event_delivery",
                  8: "common_independent_evaluator", 9: "native_mechanism_and_llm_trial"}
    formal = protocol.get('protocol_id') in {'co_ioa_13_formal_v1', 'co_ioa_13_formal_v2', 'co_ioa_13_formal_v3', 'co_ioa_13_formal_v4', 'co_ioa_13_formal_v5'}
    current_gates = [f"step{number}_{gate_names[number]}" for number in native_remaining]
    if formal:
        validate_formal_budget(protocol)
        from .formal_experiments import validate_formal_sources
        from dotenv import dotenv_values
        source_mismatches = validate_formal_sources(protocol)
        current_gates.extend(f"source_mismatch:{name}" for name in source_mismatches)
        if args.env_file is None:
            current_gates.append('formal_env_file_not_checked')
        else:
            values = dict(dotenv_values(args.env_file))
            if model_settings(values) != protocol['co_model_configuration']:
                current_gates.append('formal_model_settings_mismatch')
            if not values.get('CO_IOA_API_KEY'):
                current_gates.append('formal_model_credentials_missing')
    else:
        current_gates.extend(protocol["remaining_gates"][2:])
    report = {"protocol_id": protocol["protocol_id"], "frozen_inputs_valid": not mismatches,
              "scenario_count": len(protocol["scenarios"]), "methods": methods,
              "formal_comparison_ready": formal and not mismatches and not current_gates,
              "remaining_gates": current_gates,
              "protocol_original_remaining_gates": protocol["remaining_gates"],
              "mismatches": mismatches}
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end="")
    raise SystemExit(bool(mismatches))


if __name__ == "__main__":
    main()
